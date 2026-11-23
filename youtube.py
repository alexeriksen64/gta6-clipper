"""YouTube live support: checks whether a channel is live and reads its live chat (no API key needed)."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from typing import Callable

log = logging.getLogger("youtube")


def live_status(url: str) -> dict | None:
    """{'id', 'title', 'author'} if the channel/URL is live right now, else None."""
    try:
        r = subprocess.run([sys.executable, "-m", "streamlink", "--json", url],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        log.warning("youtube status check failed: %s", e)
        return None
    if data.get("error") or not data.get("streams"):
        if data.get("error"):
            log.info("youtube %s not live (%s)", url, str(data["error"])[:120])
        return None
    meta = data.get("metadata") or {}
    return {"id": meta.get("id"), "title": meta.get("title") or "", "author": meta.get("author") or ""}


def recent_stream_ids(channel_url: str, n: int = 6) -> list[str]:
    """Video IDs from the channel's Live tab (newest first), using yt-dlp's fast flat listing."""
    base = channel_url.split("/live")[0].rstrip("/")
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(n),
                            "-J", "--no-warnings", base + "/streams"],
                           capture_output=True, text=True, timeout=90)
        data = json.loads(r.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        log.warning("youtube stream list failed: %s", e)
        return []
    entries = data.get("entries") or []
    live_first = sorted(entries, key=lambda e: 0 if e.get("live_status") == "is_live" else 1)
    return [e["id"] for e in live_first if e.get("id")]


def find_live(channel_url: str) -> dict | None:
    """The channel's stream that is live right now. YouTube's /live link can point at a
    scheduled (not yet started) stream, so fall back to checking the newest streams one by one."""
    st = live_status(channel_url)
    if st and st.get("id"):
        return st
    for vid in recent_stream_ids(channel_url):
        st = live_status(f"https://www.youtube.com/watch?v={vid}")  # fails for upcoming/finished streams
        if st:
            st["id"] = st.get("id") or vid
            return st
    return None


class YouTubeChat:
    """Follows one YouTube channel: waits until it is live, then streams its chat messages
    into on_message(key, user, text, ts) — the same callback the Twitch chat uses."""

    def __init__(self, key: str, url: str, on_message: Callable[[str, str, str, float], None],
                 on_live: Callable[[str, str], None] | None = None):
        self.key, self.url, self.on_message, self.on_live = key, url, on_message, on_live
        self.status: dict | None = None
        self._stop = threading.Event()

    def _read_chat(self, video_id: str, loop: asyncio.AbstractEventLoop):
        from chat_downloader import ChatDownloader
        chat = ChatDownloader().get_chat(f"https://www.youtube.com/watch?v={video_id}",
                                         message_types=["text_message"])
        for m in chat:
            if self._stop.is_set():
                break
            text = m.get("message") or ""
            user = (m.get("author") or {}).get("name") or "?"
            if text:
                loop.call_soon_threadsafe(self.on_message, self.key, user, text, time.time())

    async def run(self):
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self.status = await asyncio.to_thread(find_live, self.url)
            if not self.status or not self.status.get("id"):
                await asyncio.sleep(60)
                continue
            log.info("youtube %s live: %s", self.key, self.status.get("title", "")[:80])
            if self.on_live:  # point the recorder at the actual live video
                self.on_live(self.key, f"https://www.youtube.com/watch?v={self.status['id']}")
            try:
                await asyncio.to_thread(self._read_chat, self.status["id"], loop)
                log.info("youtube %s chat ended", self.key)
            except Exception as e:  # noqa: BLE001
                log.warning("youtube %s chat error: %s", self.key, e)
            await asyncio.sleep(30)

    def stop(self):
        self._stop.set()
