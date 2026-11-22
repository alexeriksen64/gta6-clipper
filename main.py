"""GTA VI auto-clipper: watch top Twitch streams, clip chat explosions, auto-caption them.

    python main.py                       # auto-discover top GTA VI streams (needs Twitch app keys)
    python main.py --channels xqc,kaicenat   # watch specific channels, no keys needed
    python main.py --minutes 340 --set discovery.top_n=5   # timed run with config overrides
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import yaml

from chat import ChatClient
from detector import Spike, SpikeDetector
from publisher import Publisher
from recorder import StreamRecorder

log = logging.getLogger("main")


class Clipper:
    def __init__(self, cfg: dict, fixed_channels: list[str] | None, edit: bool, minutes: float = 0):
        self.cfg = cfg
        self.minutes = minutes
        self.stopping = False
        self.publisher = Publisher()
        self.fixed = [c.lower() for c in fixed_channels] if fixed_channels else None
        self.edit_enabled = edit and cfg["edit"].get("enabled", True)
        self.work = Path(cfg.get("work_dir", "work"))
        self.out = Path(cfg.get("output_dir", "clips"))
        self.out.mkdir(parents=True, exist_ok=True)
        self.detectors: dict[str, SpikeDetector] = {}
        self.recorders: dict[str, StreamRecorder] = {}
        self.info: dict[str, dict] = {}
        self.pending: dict[str, int] = {}
        self.retired: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.chat = ChatClient(self.on_message)
        self.twitch = None
        t = cfg["twitch"]
        t["client_id"] = os.environ.get("TWITCH_CLIENT_ID") or t.get("client_id")
        t["client_secret"] = os.environ.get("TWITCH_CLIENT_SECRET") or t.get("client_secret")
        if self.fixed or cfg.get("campaign_mode"):
            if t.get("client_id") and t.get("client_secret"):
                from discovery import Twitch
                self.twitch = Twitch(t["client_id"], t["client_secret"])
        else:
            from discovery import Twitch
            if not t.get("client_id") or not t.get("client_secret"):
                raise SystemExit("Set twitch.client_id/client_secret in config.yaml, or use --channels.")
            self.twitch = Twitch(t["client_id"], t["client_secret"])

    # -- chat ------------------------------------------------------------------
    def on_message(self, channel, user, text, ts):
        det = self.detectors.get(channel)
        if det:
            det.add(ts, user, text)

    # -- which streams to watch ------------------------------------------------
    def campaign_for(self, channel: str) -> dict | None:
        for c in self.cfg.get("campaigns") or []:
            if channel in [x.lower() for x in c.get("channels", [])]:
                return c
        return None

    async def pick_channels(self) -> list[str]:
        d = self.cfg["discovery"]
        if self.fixed:
            chans = self.fixed
        elif self.cfg.get("campaign_mode"):
            chans = sorted({x.lower() for c in self.cfg.get("campaigns") or [] for x in c["channels"]})
        else:
            chans = None
        if chans is not None:
            if self.twitch:  # refresh live status / current game
                live = await asyncio.to_thread(self.twitch.channel_status, chans)
                for c in chans:
                    self.info[c] = live.get(c, {"login": c, "offline": True})
            return chans
        top = await asyncio.to_thread(
            self.twitch.top_streams, self.cfg["game_names"], d["top_n"] + 3,
            d.get("min_viewers", 0), d.get("languages") or [])
        for s in top:
            self.info[s["login"]] = s
        excl = {c.lower() for c in d.get("exclude_channels", [])}
        ranked = [s["login"] for s in top if s["login"] not in excl]
        # hysteresis: keep a stream we already watch while it's within top_n+3
        keep = [c for c in ranked if c in self.detectors]
        new = [c for c in ranked if c not in self.detectors]
        chosen = (keep + new)[: d["top_n"]] if len(keep) >= d["top_n"] else keep + new[: d["top_n"] - len(keep)]
        chosen += [c.lower() for c in d.get("include_channels", []) if c.lower() not in chosen]
        return chosen

    async def discovery_loop(self):
        while True:
            if self.stopping:  # keep the task alive; run() exits via stop_after()
                await asyncio.sleep(5)
                continue
            try:
                wanted = set(await self.pick_channels())
                now = time.time()
                for c in wanted - set(self.detectors):
                    camp = self.campaign_for(c) or {}
                    det_cfg = {**self.cfg["detection"], **(camp.get("detection") or {})}
                    self.detectors[c] = SpikeDetector(c, det_cfg, now)
                    if c not in self.recorders:
                        self.recorders[c] = StreamRecorder(c, self.work, self.cfg["recording"])
                        self.recorders[c].start()
                    self.retired.discard(c)
                for c in set(self.detectors) - wanted:
                    del self.detectors[c]
                    self.retired.add(c)  # recorder stops once pending clips finish
                await self.chat.set_channels(set(self.detectors))
                log.info("watching: %s", ", ".join(
                    f"{c}({self.info.get(c, {}).get('viewers', '?')})" for c in sorted(self.detectors)))
            except Exception as e:  # noqa: BLE001
                log.error("discovery failed: %s", e)
            await asyncio.sleep(self.cfg["discovery"].get("refresh_seconds", 300))

    # -- housekeeping ----------------------------------------------------------
    async def maintain_loop(self):
        while True:
            for c, rec in list(self.recorders.items()):
                if c in self.retired and not self.pending.get(c):
                    rec.stop()
                    del self.recorders[c]
                    self.retired.discard(c)
                    log.info("stopped %s", c)
                else:
                    await asyncio.to_thread(rec.maintain)
            await asyncio.sleep(5)

    async def tick_loop(self):
        while True:
            now = time.time()
            for det in ([] if self.stopping else list(self.detectors.values())):
                spike = det.tick(now)
                if spike:
                    asyncio.create_task(self.handle_spike(spike))
            await asyncio.sleep(0.5)

    # -- clipping --------------------------------------------------------------
    async def handle_spike(self, s: Spike):
        c = s.channel
        rec = self.recorders.get(c)
        if not rec:
            return
        camp = self.campaign_for(c)
        info = self.info.get(c, {})
        if camp and camp.get("require_game") and info.get("game") is not None and "game" in info:
            if not any(g.lower() in info["game"].lower() or info["game"].lower() in g.lower()
                       for g in self.cfg["game_names"]):
                log.info("skip %s spike: streaming '%s', campaign needs GTA VI", c, info["game"])
                return
        self.pending[c] = self.pending.get(c, 0) + 1
        try:
            cc = self.cfg["clip"]
            off = float(cc.get("latency_offset_seconds", 0))
            start = s.start_ts - cc["pre_seconds"] + off
            end = s.trigger_ts + cc["post_seconds"] + off
            tokens = " ".join(t for t, _ in s.top_tokens[:4])
            log.info("🔥 %s chat x%.1f (%.1f/s vs %.1f/s) — %s", c, s.ratio, s.rate, s.baseline, tokens)
            deadline = time.time() + cc["post_seconds"] + 60
            while rec.covered_until() < end and time.time() < deadline:
                await asyncio.sleep(2)
            clip_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(s.start_ts))}_{c}_x{s.ratio:.1f}"
            jobdir = self.work / "jobs" / clip_id
            jobdir.mkdir(parents=True, exist_ok=True)
            raw = jobdir / "raw.mp4"
            if not await asyncio.to_thread(rec.cut, start, end, raw):
                return
            meta = {
                "id": clip_id, "channel": c, "url": f"https://twitch.tv/{c}",
                "viewers": self.info.get(c, {}).get("viewers"),
                "stream_title": self.info.get(c, {}).get("title"),
                "clip_start": start, "clip_end": end,
                "chat_rate": s.rate, "chat_baseline": s.baseline, "hype_ratio": s.ratio,
                "top_chat_tokens": s.top_tokens, "chat_sample": s.sample_messages,
                "campaign": camp.get("name") if camp else None,
                "campaign_url": camp.get("url") if camp else None,
                "hashtags": (camp or {}).get("hashtags", ["#GTA6", "#GTAVI"]),
                "credit": (camp or {}).get("credit"),
                "caption_text": (camp or {}).get("caption_text"),
                "platforms": (camp or {}).get("platforms"),
                # chat reacts ~3s after the moment; position of that moment inside the clip
                "moment_offset": round(min(max(s.start_ts - 3 - start, 1.5), (end - start) - 1.5), 2),
            }
            if camp and camp.get("logo"):
                lp = find_logo(camp["logo"])
                if lp:
                    meta["logo"] = str(lp)
                    meta["logo_position"] = camp.get("logo_position", "top-right")
                    meta["logo_width"] = camp.get("logo_width")
                else:
                    meta["warning"] = (f"Logo file {camp['logo']} missing — this clip has NO logo overlay "
                                       "and will be rejected. Don't submit it.")
                    log.error(meta["warning"])
            raw_out = self.out / "raw" / f"{clip_id}.mp4"
            raw_out.parent.mkdir(parents=True, exist_ok=True)
            raw_out.write_bytes(raw.read_bytes())
            meta["raw_file"] = str(raw_out)
            self.pending[c] += 1  # held until the edit finishes
            await self.queue.put((jobdir, meta, end - start))
        except Exception as e:  # noqa: BLE001
            log.exception("clip failed for %s: %s", c, e)
        finally:
            self.pending[c] -= 1

    async def edit_worker(self):
        from editor import render
        while True:
            jobdir, meta, dur = await self.queue.get()
            c = meta["channel"]
            try:
                if self.edit_enabled:
                    final = self.out / f"{meta['id']}.mp4"
                    res = await asyncio.to_thread(
                        render, jobdir / "raw.mp4", final, self.cfg["edit"], c, dur, meta.get("credit"),
                        meta.get("moment_offset"), meta.get("logo"), meta.get("logo_position", "top-right"),
                        meta.get("logo_width"))
                    meta["file"] = str(final)
                    meta["hook"] = res["hook"]
                    meta["transcript"] = " ".join(w["word"] for w in res["words"])
                    log.info("✅ %s", final)
                meta["post_caption"] = make_caption(meta)
                (self.out / f"{meta['id']}.json").write_text(json.dumps(meta, indent=2))
                await asyncio.to_thread(self.publisher.publish, meta)
                with open(self.out / "clips.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({k: meta.get(k) for k in (
                        "id", "channel", "campaign", "viewers", "hype_ratio", "file", "raw_file",
                        "post_caption", "transcript")}) + "\n")
            except Exception as e:  # noqa: BLE001
                log.exception("edit failed for %s: %s", meta["id"], e)
                # never lose the moment: publish the uncaptioned original instead
                try:
                    meta["file"] = None
                    meta["warning"] = ((meta.get("warning") or "") +
                                       " Editing failed — this is the raw clip without captions/overlay.").strip()
                    meta.setdefault("post_caption", make_caption(meta))
                    (self.out / f"{meta['id']}.json").write_text(json.dumps(meta, indent=2))
                    await asyncio.to_thread(self.publisher.publish, meta)
                except Exception:  # noqa: BLE001
                    log.exception("fallback publish failed for %s", meta["id"])
            finally:
                self.pending[c] = self.pending.get(c, 1) - 1
                self.queue.task_done()

    async def stop_after(self):
        """Timed runs (GitHub Actions): stop clipping, let in-flight clips finish, then exit."""
        await asyncio.sleep(self.minutes * 60)
        log.info("time is up — finishing %d pending clip(s)", sum(self.pending.values()))
        self.stopping = True
        deadline = time.time() + 15 * 60
        while (sum(self.pending.values()) > 0 or not self.queue.empty()) and time.time() < deadline:
            await asyncio.sleep(3)

    async def run(self):
        tasks = [self.chat.run(), self.discovery_loop(), self.maintain_loop(), self.tick_loop()]
        tasks += [self.edit_worker() for _ in range(int(self.cfg["edit"].get("workers", 1)))]
        tasks = [asyncio.ensure_future(t) for t in tasks]
        if self.minutes:
            tasks.append(asyncio.ensure_future(self.stop_after()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            for r in self.recorders.values():
                r.stop()


def find_logo(path: str) -> Path | None:
    """Find a logo even if its name got a different/duplicated ending, e.g.
    logos/kreekcraft.png.PNG or logos/KreekCraft.jpg for logos/kreekcraft.png."""
    p = Path(path)
    if p.exists():
        return p
    stem = p.name.split(".")[0].lower()
    if p.parent.is_dir():
        for f in sorted(p.parent.iterdir()):
            if f.is_file() and f.name.lower().split(".")[0] == stem:
                return f
    return None


def make_caption(meta: dict) -> str:
    """Short hook from what was said + the campaign's required hashtags."""
    words = (meta.get("transcript") or "").split()
    hook = " ".join(words[:9]).strip(" ,.") if words else ""
    if len(words) > 9:
        hook += "…"
    line1 = f"{hook} 😭" if hook else "chat went crazy 😭"
    parts = [line1]
    ct = meta.get("caption_text")
    if isinstance(ct, list) and ct:
        import random
        ct = random.choice(ct)  # rotate required lines so posts don't look identical
    if ct:
        parts.append(ct)
    parts.append(" ".join(meta.get("hashtags") or []))
    return "\n".join(p for p in parts if p).strip()


def apply_overrides(cfg: dict, items: list[str]):
    for item in items:
        key, _, val = item.partition("=")
        node = cfg
        *path, last = key.strip().split(".")
        for k in path:
            node = node.setdefault(k, {})
        node[last] = yaml.safe_load(val) if val.strip() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--channels", help="comma-separated channels (skips auto-discovery)")
    ap.add_argument("--no-edit", action="store_true", help="only save raw clips")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (0 = forever)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override config, e.g. --set discovery.top_n=5 --set edit.whisper_model=base")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    apply_overrides(cfg, a.set)
    chans = [c.strip() for c in a.channels.split(",") if c.strip()] if a.channels else None
    try:
        asyncio.run(Clipper(cfg, chans or None, not a.no_edit, a.minutes).run())
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
