"""Auto-edit: transcribe with Whisper, build word-by-word 'TikTok style' captions, render vertical/horizontal."""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("editor")

_model = None
_model_lock = threading.Lock()


def transcribe(video: Path, cfg: dict) -> list[dict]:
    """Return [{'word', 'start', 'end'}]. Empty list if faster-whisper isn't installed."""
    global _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper not installed — rendering without captions")
        return []
    with _model_lock:
        if _model is None:
            dev = cfg.get("whisper_device", "auto")
            log.info("loading whisper model %s (%s)", cfg.get("whisper_model", "small"), dev)
            _model = WhisperModel(cfg.get("whisper_model", "small"), device=dev,
                                  compute_type="auto")
        segs, _ = _model.transcribe(str(video), language=cfg.get("language") or None,
                                    word_timestamps=True, vad_filter=True, beam_size=5)
        words = []
        for s in segs:
            for w in s.words or []:
                t = w.word.strip()
                if t:
                    words.append({"word": t, "start": float(w.start), "end": float(w.end)})
    return words


# -- captions -------------------------------------------------------------------
def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(word: str) -> str:
    w = word.replace("\\", "/").replace("{", "(").replace("}", ")")
    w = re.sub(r"[\"“”,.;:]+$", "", w)
    return w.upper()


def chunk_words(words: list[dict], per: int) -> list[list[dict]]:
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 99
        if len(cur) >= per or gap > 0.7 or re.search(r"[.!?]$", w["word"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def layout(cfg: dict) -> dict:
    size = int(cfg.get("caption_size", 88))
    if cfg.get("format", "vertical") == "horizontal":
        return dict(W=1920, H=1080, size=int(size * 0.8), cap_margin=110, credit_align=7,
                    credit_margin=40, credit_size=38, hook_margin=110, hook_size=72, logo_w=220)
    if cfg.get("layout", "blur") == "crop":
        return dict(W=1080, H=1920, size=size, cap_margin=560, credit_align=8,
                    credit_margin=170, credit_size=44, hook_margin=250, hook_size=74, logo_w=240)
    # blur: 16:9 gameplay sits in the middle band (y≈656–1264); captions just below it
    return dict(W=1080, H=1920, size=size, cap_margin=470, credit_align=8,
                credit_margin=520, credit_size=44, hook_margin=300, hook_size=76, logo_w=240)


def build_ass(words: list[dict], cfg: dict, credit: str | None, duration: float,
              hook: str | None = None) -> str:
    L = layout(cfg)
    font = cfg.get("caption_font", "Poppins")
    hi = cfg.get("highlight_color", "&H0000E5FF")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {L['W']}
PlayResY: {L['H']}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{L['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{L['cap_margin']},1
Style: Hook,{font},{L['hook_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,4,8,70,70,{L['hook_margin']},1
Style: Credit,{font},{L['credit_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,3,10,0,{L['credit_align']},50,50,{L['credit_margin']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Hook,,0,0,0,,{{\\fad(120,0)}}{hook}")
    if credit:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{credit}")
    chunks = chunk_words(words, int(cfg.get("words_per_caption", 3)))
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration
        chunk_end = min(ch[-1]["end"] + 0.25, nxt)
        texts = [_clean(w["word"]) for w in ch]
        for wi, w in enumerate(ch):
            st = w["start"]
            en = ch[wi + 1]["start"] if wi + 1 < len(ch) else chunk_end
            if en <= st:
                continue
            parts = []
            for k, t in enumerate(texts):
                if k == wi:
                    parts.append(r"{\c" + hi + r"&\fscx112\fscy112}" + t + r"{\c&H00FFFFFF&\fscx100\fscy100}")
                else:
                    parts.append(t)
            pop = r"{\fad(40,0)}" if wi == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(st)},{_ts(en)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


# -- render ---------------------------------------------------------------------
def cold_open(raw: Path, moment: float, duration: float, pre: float = 1.2, post: float = 1.3) -> tuple[Path, float]:
    """Put the peak moment first (a 2.5s 'teaser'), then play the clip from the start."""
    a, b = max(0.0, moment - pre), min(duration, moment + post)
    out = raw.parent / "cold_open.mp4"
    fc = (f"[0:v]trim={a:.2f}:{b:.2f},setpts=PTS-STARTPTS[v1];[0:a]atrim={a:.2f}:{b:.2f},asetpts=PTS-STARTPTS[a1];"
          "[0:v]setpts=PTS-STARTPTS[v2];[0:a]asetpts=PTS-STARTPTS[a2];"
          "[v1][a1][v2][a2]concat=n=2:v=1:a=1[v][a]")
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", raw.name,
                        "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", out.name],
                       cwd=raw.parent)
    if r.returncode:  # e.g. no audio track — fall back to the plain clip
        return raw, duration
    return out, duration + (b - a)


def hook_from(words: list[dict], start: float, end: float, max_words: int = 6) -> str | None:
    """Hook title = the streamer's own words around the moment (never invented)."""
    picked = [_clean(w["word"]) for w in words if start - 0.2 <= w["start"] <= end]
    picked = [w for w in picked if w]
    if len(picked) < 2:
        return None
    text = " ".join(picked[:max_words])
    return f"“{text}{'…' if len(picked) > max_words else ''}”"


def render(raw: Path, out: Path, cfg: dict, channel: str, duration: float,
           credit_text: str | None = None, moment: float | None = None,
           logo: Path | None = None, logo_position: str = "top-right") -> dict:
    src, dur, teaser = raw, duration, 0.0
    if cfg.get("cold_open", True) and moment is not None:
        src, dur = cold_open(raw, moment, duration)
        teaser = dur - duration
    words = transcribe(src, cfg)
    credit = (credit_text or f"twitch.tv/{channel}") if cfg.get("credit", True) else None
    hook = None
    if cfg.get("hook_title", True):
        hook = hook_from(words, 0, teaser) if teaser else None
        if not hook and moment is not None:
            hook = hook_from(words, teaser + moment - 1.5, teaser + moment + 2.5)
    ass = src.parent / "captions.ass"
    ass.write_text(build_ass(words, cfg, credit, dur, hook), encoding="utf-8")

    L = layout(cfg)
    if L["W"] == 1920:
        vf = "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,ass=captions.ass"
    elif cfg.get("layout", "blur") == "crop":
        vf = "[0:v]scale=-2:1920,crop=1080:1920,ass=captions.ass"
    else:
        vf = ("[0:v]split=2[a][b];"
              "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=24:2,eq=brightness=-0.12[bg];"
              "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=captions.ass")
    inputs = ["-i", src.name]
    if logo:
        m = 50
        pos = {"top-right": (f"W-w-{m}", f"{m}"), "top-left": (f"{m}", f"{m}"),
               "bottom-right": (f"W-w-{m}", f"H-h-{m}"), "bottom-left": (f"{m}", f"H-h-{m}")}
        x, y = pos.get(logo_position, pos["top-right"])
        inputs += ["-i", str(Path(logo).resolve())]
        vf += f"[base];[1:v]scale={L['logo_w']}:-1,format=rgba[lg];[base][lg]overlay={x}:{y}:format=auto[v]"
    else:
        vf += "[v]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(out.resolve())]
    subprocess.run(cmd, cwd=src.parent, check=True)
    return {"words": words, "hook": hook, "duration": dur}
