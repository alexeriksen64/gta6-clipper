# GTA VI Auto-Clipper — run it from your iPhone

Watches the biggest live GTA VI streams on Twitch. When a chat suddenly goes crazy, it clips that
moment and auto-edits it into a vertical short with word-by-word captions and a streamer credit.
You start it from the **GitHub app** and watch the clips in **Safari**. Optionally, you get a **push
notification** for every new clip.

---

## One-time setup (≈15 min, all on iPhone)

### 1. Put the project on GitHub
1. Tap the zip in the **Files** app to unzip it.
2. In Safari, go to github.com and tap **+ → New repository**. Name it `gta6-clipper` and tap **Create**.
   * **Public** repo: GitHub's servers are free with no minute limit, but anyone can see your clips.
   * **Private** repo: only you see the clips, but free accounts get 2,000 minutes a month (≈6 runs of 5.5 h).
3. Tap **Add file → Upload files** and select every file from the unzipped folder
   (the `.py` files, `config.yaml`, `requirements.txt`, `README.md`). Then tap **Commit changes**.
4. Tap **Add file → Create new file**. For the name, type `.github/workflows/clip.yml`. Open the
   separate `clip.yml` file, copy everything in it and paste it in. Tap **Commit changes**.

### 2. Get Twitch keys (lets it find the top GTA VI streams)
1. In Safari, go to **dev.twitch.tv/console/apps** and log in. Twitch requires 2-factor authentication for this.
2. Tap **Register Your Application**. Name: anything. OAuth Redirect URL: `http://localhost`.
   Category: Other. Client type: **Confidential**.
3. Open the app, copy the **Client ID**, then tap **New Secret** and copy the secret.

### 3. Add the keys to GitHub
In your repo (in Safari): **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `TWITCH_CLIENT_ID` | your Client ID |
| `TWITCH_CLIENT_SECRET` | your secret |
| `NTFY_TOPIC` *(optional)* | a hard-to-guess name, e.g. `gta6-clips-k39xq7` |

### 4. (Optional) Push notifications
Install the free **ntfy** app from the App Store, tap **+**, and subscribe to the same topic name you used
for `NTFY_TOPIC`. Every new clip sends a notification, and tapping it opens the video.

---

## Using it

**Start:** GitHub app → your repo → **Actions** → **GTA VI clipper** → **Run workflow**.

| Option | Meaning |
|---|---|
| hours | how long to watch (max 5.5 per run) |
| channels | leave blank for the top GTA VI streams, or e.g. `xqc,kaicenat` |
| top_n | how many streams to watch at once (5 is right for GitHub's servers) |
| sensitivity | `more clips` / `normal` / `fewer clips` |
| keep_going | start a new run automatically when this one ends |
| runner | `ubuntu-latest` = GitHub's servers, `self-hosted` = your own computer (see below) |

**Watch clips:** open the run in the app. The summary has a link to the release page for that run.
You can also go to the repo's **Releases**. The release page lists every clip, newest first, with
chat hype, viewer count, what chat was spamming and the transcript. Tap ▶️ to play.
Clips are added during the run, a minute or two after each moment.

**Stop:** open the running run → **Cancel**. Cancelling also stops `keep_going`.

---

## Paid campaigns (default mode)

`config.yaml → campaigns` lists the clipping campaigns you joined. In `campaigns` mode only those streamers are
watched, and clips are made only while their Twitch category is GTA VI (rule: Kai playing GTA 6 must be the main focus).
Every clip on the release page comes with a ready caption containing the required hashtags.

**Per clip:** tap ▶️ → Share → Save Video → post to TikTok/Shorts/Reels with the copied caption →
paste the post link into the campaign on Content Rewards.

**Kai Cenat pool:** a clip needs 1K views to count, and you need **750K total views** across your clips to get a share.
Post every good clip on all allowed platforms (TikTok, Shorts, Reels, X) to reach that.

To add a campaign, copy the Kai Cenat block in `config.yaml` and change the name, link, channels and hashtags.

## ⚠️ GitHub's servers vs. your own computer

GitHub's terms say Actions is for building and testing software, and they rule out using it as
general-purpose "serverless computing". Hours of stream recording can count as misuse, which can get
jobs stopped or Actions disabled on the repo. Using GitHub's servers for a test run or launch-day
sessions is a judgement call. Running nonstop with `keep_going` is the riskiest option.

The safer setup is a **self-hosted runner**. Your own PC or Mac does the work, and you still start runs
and watch clips from your iPhone exactly as above. It's also faster (it can use your GPU and better caption models).
1. On the computer, install Python 3.10+, ffmpeg and the GitHub CLI (`gh`). On Windows, also install Git for Windows.
2. In the repo (in Safari): **Settings → Actions → Runners → New self-hosted runner**. Follow the commands
   it shows on the computer, then start the runner with `./run.sh` (or `run.cmd` on Windows).
3. When you start a run, choose `runner: self-hosted`. Keep the computer on and awake.

---

## Running it directly on a computer (no GitHub)
```bash
pip install -r requirements.txt     # plus ffmpeg
python main.py                      # top streams (put Twitch keys in config.yaml)
python main.py --channels xqc       # specific channels, no keys needed
```
Clips are saved in `clips/`. All the tuning settings are in `config.yaml`. You can override any of them with `--set key=value`.

## Notes
* Twitch may show ads to recording servers. Ad breaks show up as gaps, and clips that land on one are skipped.
* These are other creators' broadcasts. Each clip credits the channel, but reposting or monetising clips
  without permission can break Twitch, TikTok or YouTube rules.
