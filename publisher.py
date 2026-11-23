"""Publish finished clips so they can be watched on a phone.

- GITHUB_RELEASE_TAG set (GitHub Actions): upload each clip to that release and keep the
  release notes as a newest-first list of clips with links.
- NTFY_TOPIC set: push notification via ntfy.sh (iPhone app "ntfy"), tapping opens the clip.
"""
from __future__ import annotations

import logging
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("publish")


class Publisher:
    def __init__(self):
        self.tag = os.environ.get("GITHUB_RELEASE_TAG")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.topic = os.environ.get("NTFY_TOPIC")
        self.rows: list[str] = []

    def asset_url(self, path: Path) -> str | None:
        if not (self.tag and self.repo):
            return None
        name = urllib.parse.quote(path.name)
        return f"https://github.com/{self.repo}/releases/download/{self.tag}/{name}"

    def publish(self, meta: dict):
        video = Path(meta.get("file") or meta["raw_file"])
        url = self.asset_url(video)
        if self.tag:
            r = subprocess.run(["gh", "release", "upload", self.tag, str(video), "--clobber"],
                               capture_output=True, text=True)
            if r.returncode:
                log.error("upload failed: %s", r.stderr.strip())
                url = None
            else:
                self._update_notes(meta, url)
        if self.topic:
            self._notify(meta, url)

    def _update_notes(self, meta: dict, url: str | None):
        tokens = " ".join(t for t, _ in meta.get("top_chat_tokens", [])[:3])
        viewers = f"{meta['viewers']:,}" if meta.get("viewers") else "?"
        camp = f" · [{meta['campaign']}]({meta['campaign_url']})" if meta.get("campaign") else ""
        warn = f"> ⚠️ **{meta['warning']}**\n\n" if meta.get("warning") else ""
        plat = f"Post on: {', '.join(meta['platforms'])}\n" if meta.get("platforms") else ""
        head = (f"### [▶️ {meta['channel']} — ✂️ viewer clip: {meta['viewer_clip_title']}]({url})\n"
                if meta.get("viewer_clip_title") is not None else
                f"### [▶️ {meta['channel']} — chat x{meta['hype_ratio']}]({url})\n")
        self.rows.insert(0, (
            head +
            f"{warn}{viewers} viewers · chat: {tokens}{camp}\n{plat}\n"
            f"Caption (tap copy):\n```\n{meta.get('post_caption', '')}\n```\n"))
        body = ("Clips appear here as they're made — newest first. Tap ▶️ to watch/download, "
                "copy the caption, post, then submit the post link in the campaign.\n\n"
                + "\n---\n".join(self.rows))
        notes = Path("release_notes.md")
        notes.write_text(body, encoding="utf-8")
        subprocess.run(["gh", "release", "edit", self.tag, "--notes-file", str(notes)],
                       capture_output=True)

    def _notify(self, meta: dict, url: str | None):
        title = f"🔥 {meta['channel']} — chat x{meta['hype_ratio']}"
        msg = meta.get("post_caption") or (meta.get("transcript") or "New clip")[:200]
        q = {"title": title, "tags": "movie_camera"}
        if url:
            q["click"] = url
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{self.topic}?" + urllib.parse.urlencode(q),
                data=msg.encode("utf-8"), method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy failed: %s", e)
