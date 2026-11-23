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


class YouTubeChat:
    """Follows one YouTube channel: waits until it is live, then streams its chat messages
    into on_message(key, user, text, ts) — the same callback the Twitch chat uses."""

    def __init__(self, key: str, url: str, on_message: Callable[[str, str, str, float], None]):
        self.key, self.url, self.on_message = key, url, on_message
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
            self.status = await asyncio.to_thread(live_status, self.url)
            if not self.status or not self.status.get("id"):
                await asyncio.sleep(60)
                continue
            log.info("youtube %s live: %s", self.key, self.status.get("title", "")[:80])
            try:
                await asyncio.to_thread(self._read_chat, self.status["id"], loop)
                log.info("youtube %s chat ended", self.key)
            except Exception as e:  # noqa: BLE001
                log.warning("youtube %s chat error: %s", self.key, e)
            await asyncio.sleep(30)

    def stop(self):
        self._stop.set()
