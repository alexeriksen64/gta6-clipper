"""Chat-spike detection: learns each channel's normal chat speed and fires when it explodes."""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field


@dataclass
class Spike:
    channel: str
    start_ts: float          # when the surge began (wall clock)
    trigger_ts: float        # when we were confident enough to fire
    rate: float              # weighted msgs/sec in the trigger window
    baseline: float          # normal msgs/sec
    ratio: float
    top_tokens: list = field(default_factory=list)
    sample_messages: list = field(default_factory=list)


_WORD = re.compile(r"\S+")


class SpikeDetector:
    def __init__(self, channel: str, cfg: dict, now: float):
        self.channel = channel
        self.bin = float(cfg.get("bin_seconds", 1))
        self.window_bins = max(1, int(cfg.get("window_seconds", 6) / self.bin))
        self.alpha = self.bin / float(cfg.get("baseline_seconds", 300))
        self.warmup = float(cfg.get("warmup_seconds", 90))
        self.z = float(cfg.get("z_threshold", 3.5))
        self.ratio = float(cfg.get("ratio_threshold", 2.3))
        self.min_rate = float(cfg.get("min_msgs_per_sec", 3))
        self.cooldown = float(cfg.get("cooldown_seconds", 90))
        self.hype = {t.lower() for t in cfg.get("hype_tokens", [])}

        self.started = now
        self.bin_start = math.floor(now / self.bin) * self.bin
        self.cur = 0.0
        self.bins: deque[tuple[float, float]] = deque(maxlen=600)  # (bin_start, score)
        self.mean = None
        self.var = 0.0
        self.last_trigger = -1e18
        self.msgs: deque[tuple[float, str, str]] = deque(maxlen=5000)

    # -- scoring -------------------------------------------------------------
    def _weight(self, text: str) -> float:
        words = _WORD.findall(text)
        if not words:
            return 1.0
        w = 1.0
        if any(x.lower() in self.hype for x in words):
            w += 0.5
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            w += 0.25  # ALL CAPS
        if re.search(r"(\?{3,}|!{3,})", text):
            w += 0.25
        return w

    # -- bins / baseline -----------------------------------------------------
    def _close_bins(self, now: float):
        while now >= self.bin_start + self.bin:
            score = self.cur / self.bin
            self.bins.append((self.bin_start, score))
            if self.mean is None:
                self.mean, self.var = score, 0.0
            else:
                std = math.sqrt(self.var)
                # clamp so a spike doesn't immediately become the new "normal"
                x = min(score, self.mean + 3 * std + 1) if self._warm(now) else score
                d = x - self.mean
                self.mean += self.alpha * d
                self.var = (1 - self.alpha) * (self.var + self.alpha * d * d)
            self.cur = 0.0
            self.bin_start += self.bin

    def _warm(self, now: float) -> bool:
        return now - self.started >= self.warmup

    # -- public --------------------------------------------------------------
    def add(self, ts: float, user: str, text: str):
        self._close_bins(ts)
        self.cur += self._weight(text)
        self.msgs.append((ts, user, text))

    def tick(self, now: float) -> Spike | None:
        self._close_bins(now)
        if not self._warm(now) or len(self.bins) < self.window_bins or self.mean is None:
            return None
        if now - self.last_trigger < self.cooldown:
            return None
        recent = list(self.bins)[-self.window_bins:]
        rate = sum(s for _, s in recent) / len(recent)
        base = max(self.mean, 0.2)
        std = max(math.sqrt(self.var), 0.5)
        # std of a window average is smaller than a single bin's
        z = (rate - base) / (std / math.sqrt(len(recent)))
        if rate < self.min_rate or rate < self.ratio * base or z < self.z:
            return None

        self.last_trigger = now
        # walk back to where the surge started (first bin clearly above normal)
        start = recent[-1][0]
        for b_start, s in reversed(list(self.bins)):
            if s > base + 2 * std or s > 1.6 * base:
                start = b_start
            else:
                break
            if now - b_start > 30:
                break
        window_msgs = [m for m in self.msgs if m[0] >= start - 2]
        counts = Counter()
        for _, _, t in window_msgs:
            counts.update({w for w in _WORD.findall(t)})  # once per message
        return Spike(
            channel=self.channel,
            start_ts=start,
            trigger_ts=now,
            rate=round(rate, 2),
            baseline=round(base, 2),
            ratio=round(rate / base, 2),
            top_tokens=counts.most_common(8),
            sample_messages=[f"{u}: {t}" for _, u, t in window_msgs[-40:]],
        )
