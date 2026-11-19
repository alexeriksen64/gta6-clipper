"""Find the most-watched live GTA VI streams via the Twitch Helix API."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("discovery")
HELIX = "https://api.twitch.tv/helix"


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        self.cid, self.secret = client_id, client_secret
        self.token, self.token_exp = None, 0.0
        self.game_ids: list[str] | None = None

    def _auth(self):
        if self.token and time.time() < self.token_exp - 60:
            return
        r = requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": self.cid, "client_secret": self.secret,
            "grant_type": "client_credentials"}, timeout=15)
        r.raise_for_status()
        j = r.json()
        self.token, self.token_exp = j["access_token"], time.time() + j["expires_in"]

    def _get(self, path, params):
        self._auth()
        r = requests.get(f"{HELIX}/{path}", params=params, timeout=15, headers={
            "Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        return r.json()

    def resolve_games(self, names: list[str]) -> list[str]:
        if self.game_ids:
            return self.game_ids
        ids = []
        data = self._get("games", [("name", n) for n in names]).get("data", [])
        ids += [g["id"] for g in data]
        if not ids:  # fall back to fuzzy search
            for n in names:
                for g in self._get("search/categories", {"query": n, "first": 5}).get("data", []):
                    if "grand theft auto vi" in g["name"].lower() or "gta vi" in g["name"].lower():
                        ids.append(g["id"])
        ids = list(dict.fromkeys(ids))
        if not ids:
            raise RuntimeError(f"Could not find a Twitch category for {names}")
        log.info("watching Twitch categories: %s", ids)
        self.game_ids = ids
        return ids

    def top_streams(self, names, top_n=8, min_viewers=0, languages=()):
        params = [("game_id", g) for g in self.resolve_games(names)]
        params += [("language", l) for l in languages]
        params += [("first", "100"), ("type", "live")]
        streams = self._get("streams", params).get("data", [])
        streams = [s for s in streams if s["viewer_count"] >= min_viewers]
        streams.sort(key=lambda s: -s["viewer_count"])
        return [{"login": s["user_login"].lower(), "name": s["user_name"],
                 "viewers": s["viewer_count"], "title": s["title"], "game": s.get("game_name", "")}
                for s in streams[:top_n]]

    def channel_status(self, logins: list[str]) -> dict:
        """{login: {...}} for the channels that are live right now."""
        if not logins:
            return {}
        data = self._get("streams", [("user_login", l) for l in logins]).get("data", [])
        return {s["user_login"].lower(): {"login": s["user_login"].lower(), "name": s["user_name"],
                "viewers": s["viewer_count"], "title": s["title"], "game": s.get("game_name", "")}
                for s in data}
