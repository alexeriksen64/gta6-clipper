"""Anonymous, read-only Twitch chat client (no account needed) over IRC/TLS."""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from typing import Callable

log = logging.getLogger("chat")

HOST, PORT = "irc.chat.twitch.tv", 6697


def parse_privmsg(line: str):
    """Return (channel, user, text) for a PRIVMSG line, else None."""
    if " PRIVMSG #" not in line:
        return None
    tags = {}
    if line.startswith("@"):
        raw_tags, line = line[1:].split(" ", 1)
        for kv in raw_tags.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    try:
        prefix, rest = line.split(" PRIVMSG #", 1)
        channel, text = rest.split(" :", 1)
    except ValueError:
        return None
    user = tags.get("display-name") or prefix.lstrip(":").split("!", 1)[0]
    return channel.strip().lower(), user, text.rstrip("\r\n")


class ChatClient:
    def __init__(self, on_message: Callable[[str, str, str, float], None]):
        self.on_message = on_message
        self.channels: set[str] = set()
        self._writer: asyncio.StreamWriter | None = None

    async def set_channels(self, channels: set[str]):
        channels = {c.lower() for c in channels}
        add, remove = channels - self.channels, self.channels - channels
        self.channels = channels
        if self._writer:
            for c in remove:
                await self._send(f"PART #{c}")
            await self._join(add)

    async def _send(self, line: str):
        self._writer.write((line + "\r\n").encode())
        await self._writer.drain()

    async def _join(self, chans):
        for i, c in enumerate(sorted(chans)):
            await self._send(f"JOIN #{c}")
            if i % 15 == 14:
                await asyncio.sleep(10)  # stay under join rate limit

    async def run(self):
        backoff = 1
        while True:
            try:
                reader, self._writer = await asyncio.open_connection(
                    HOST, PORT, ssl=ssl.create_default_context())
                await self._send("CAP REQ :twitch.tv/tags")
                await self._send("PASS SCHMOOPIIE")
                await self._send(f"NICK justinfan{random.randint(10000, 99999)}")
                await self._join(self.channels)
                log.info("chat connected (%d channels)", len(self.channels))
                backoff = 1
                while True:
                    raw = await asyncio.wait_for(reader.readline(), timeout=360)
                    if not raw:
                        raise ConnectionError("chat closed")
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        await self._send("PONG" + line[4:].rstrip())
                        continue
                    if " RECONNECT" in line:
                        raise ConnectionError("server asked to reconnect")
                    msg = parse_privmsg(line)
                    if msg:
                        self.on_message(*msg, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("chat error: %s — reconnecting in %ss", e, backoff)
                self._writer = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
