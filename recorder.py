"""Rolling on-disk buffer of each watched stream (streamlink -> ffmpeg segmenter) and clip cutting."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("recorder")


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


class StreamRecorder:
    """Keeps the last `buffer_seconds` of a live stream as small .ts segments.

    A segment's wall-clock end time is its file mtime (when its last bytes arrived),
    which is the same clock chat messages are stamped with.
    """

    def __init__(self, channel: str, work_dir: Path, cfg: dict, url: str | None = None):
        self.channel = channel
        self.url = url or f"https://twitch.tv/{channel}"
        self.dir = Path(work_dir) / "buffer" / channel
        self.quality = cfg.get("quality", "720p60,720p,best")
        self.seg = int(cfg.get("segment_seconds", 4))
        self.buffer = float(cfg.get("buffer_seconds", 180))
        self.procs: list[subprocess.Popen] = []
        self.restarts = 0
        self.next_start = 0.0
        self._durs: dict[str, float] = {}

    # -- process management ----------------------------------------------------
    def start(self):
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        sl = [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
              "--twitch-disable-ads", "--twitch-low-latency",
              self.url, self.quality]
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
              "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(self.seg), "-segment_format", "mpegts",
              "-reset_timestamps", "1", str(self.dir / "seg_%07d.ts")]
        p1 = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(ff, stdin=p1.stdout, stderr=subprocess.DEVNULL)
        p1.stdout.close()
        self.procs = [p1, p2]
        log.info("recording %s", self.channel)

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []

    def alive(self) -> bool:
        return bool(self.procs) and all(p.poll() is None for p in self.procs)

    def maintain(self):
        """Restart dead pipelines (with backoff) and drop segments older than the buffer."""
        now = time.time()
        if not self.alive() and now >= self.next_start:
            self.stop()
            self.restarts += 1
            self.next_start = now + min(10 * self.restarts, 120)
            self.start()
        elif self.alive() and self.segments():
            self.restarts = 0
        for f in self.dir.glob("seg_*.ts"):
            try:
                if now - f.stat().st_mtime > self.buffer:
                    f.unlink()
                    self._durs.pop(f.name, None)
            except FileNotFoundError:
                pass

    # -- cutting ---------------------------------------------------------------
    def segments(self, closed_only=True):
        files = sorted(self.dir.glob("seg_*.ts"))
        if closed_only:
            files = files[:-1]  # newest one is still being written
        out = []
        for f in files:
            try:
                end = f.stat().st_mtime
            except FileNotFoundError:
                continue
            if f.name not in self._durs:
                self._durs[f.name] = probe_duration(f) or self.seg
            out.append((f, end - self._durs[f.name], end))
        return out

    def covered_until(self) -> float:
        segs = self.segments()
        return segs[-1][2] if segs else 0.0

    def cut(self, start: float, end: float, out_path: Path) -> bool:
        segs = [s for s in self.segments() if s[2] > start and s[1] < end]
        if not segs:
            log.warning("%s: no buffered video for %.0f-%.0f", self.channel, start, end)
            return False
        # copy first so the cleaner can't delete them mid-cut
        tmp = out_path.parent / (out_path.stem + "_src")
        tmp.mkdir(parents=True, exist_ok=True)
        local = []
        for f, _, _ in segs:
            dst = tmp / f.name
            shutil.copy2(f, dst)
            local.append(dst)
        offset = max(0.0, start - segs[0][1])
        dur = min(end, segs[-1][2]) - max(start, segs[0][1])
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", "concat:" + "|".join(str(p) for p in local),
               "-ss", f"{offset:.2f}", "-t", f"{dur:.2f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)]
        ok = subprocess.run(cmd).returncode == 0 and out_path.exists()
        shutil.rmtree(tmp, ignore_errors=True)
        return ok
