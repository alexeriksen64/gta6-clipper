"""Add a stacked logo (e.g. ClipUp) to clips that were already made, and publish them in one new release.

Runs in GitHub Actions (needs `gh` and ffmpeg). Finds every finished clip of the chosen channel in
the existing releases, overlays the logo, and uploads the new versions with ready captions.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from main import find_logo, merge_tags  # noqa: E402


def gh(*args, capture=True) -> str:
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def captions_by_file(body: str) -> dict:
    """Map clip file name -> caption, parsed from the release notes the clipper wrote."""
    out = {}
    for sec in body.split("\n---\n"):
        m = re.search(r"\]\((https://[^)]+/([^/)]+\.mp4))\)", sec)
        c = re.search(r"```\n(.*?)\n```", sec, re.S)
        if m:
            out[m.group(2)] = c.group(1).strip() if c else ""
    return out


def add_tags(caption: str, tags: list[str]) -> str:
    """Append only the hashtags the caption doesn't already have (case-insensitive)."""
    have = {t.lower() for t in re.findall(r"#\w+", caption)}
    missing = [t for t in tags if t.lower() not in have]
    if not missing:
        return caption.strip()
    return (caption.rstrip() + " " + " ".join(missing)).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="kaicenat")
    ap.add_argument("--stack", default="GTA VI Clipup Logo", help="name of the stack campaign in config.yaml")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    st = next(s for s in cfg.get("stack_campaigns") or [] if s["name"] == a.stack)
    logo = find_logo(st["logo"])
    if not logo:
        sys.exit(f"Logo {st['logo']} not found — upload it to the logos folder first.")
    width = int(st.get("logo_width") or 900)
    # old clips already have subtitles just under the gameplay (y≈1340–1450), so the logo goes below
    # them, slightly smaller so it stays clear of the platform's caption area at the bottom
    old_width = min(width, 800)

    releases = json.loads(gh("release", "list", "--limit", "100", "--json", "tagName"))
    work = Path("old_clips"); work.mkdir(exist_ok=True)
    done, rows = set(), []
    for rel in releases:
        tag = rel["tagName"]
        if not tag.startswith("clips-"):
            continue
        info = json.loads(gh("release", "view", tag, "--json", "assets,body"))
        caps = captions_by_file(info.get("body") or "")
        for asset in info.get("assets") or []:
            name = asset["name"]
            if f"_{a.channel}_" not in name or not name.endswith(".mp4") or "_logo" in name or name in done:
                continue
            done.add(name)
            src = work / name
            dst = work / name.replace(".mp4", "_logo.mp4")
            gh("release", "download", tag, "--pattern", name, "--dir", str(work), "--clobber")
            r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                                "-i", str(logo.resolve()), "-filter_complex",
                                f"[1:v]scale={old_width}:-1,format=rgba[lg];[0:v][lg]overlay=(W-w)/2:1475:format=auto[v]",
                                "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(dst)])
            if r.returncode:
                print(f"skip {name}: ffmpeg failed")
                continue
            rows.append((dst, add_tags(caps.get(name, ""), st.get("hashtags") or [])))
            print(f"done {name}")
            src.unlink(missing_ok=True)

    if not rows:
        print("No old clips found for", a.channel)
        return
    new_tag = f"logo-{a.channel}-{time.strftime('%Y%m%d-%H%M')}"
    gh("release", "create", new_tag, "--prerelease",
       "--title", f"🏷️ {a.channel} clips with {st['name']} logo ({len(rows)})",
       "--notes", "Uploading…")
    repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                          capture_output=True, text=True).stdout.strip()
    parts = [f"Older {a.channel} clips with the {st['name']} logo added. Only post clips you haven't "
             "posted yet — re-posting one you already posted counts as a duplicate.\n"]
    for dst, cap in rows:
        gh("release", "upload", new_tag, str(dst), "--clobber")
        url = f"https://github.com/{repo}/releases/download/{new_tag}/{dst.name}"
        parts.append(f"### [▶️ {dst.name}]({url})\n➕ Submit to: {st['name']}\n\n"
                     f"Caption (tap copy):\n```\n{cap}\n```\n")
    notes = Path("logo_notes.md")
    notes.write_text("\n---\n".join(parts), encoding="utf-8")
    gh("release", "edit", new_tag, "--notes-file", str(notes))
    print(f"Published {len(rows)} clips to release {new_tag}")


if __name__ == "__main__":
    main()
