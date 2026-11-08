# Setting up the PIXELHELM channel bot

## The easy way (2026-11-03)

1. Claude sets up Google Cloud in the browser panel after you sign in, and you press **Download JSON** on the OAuth client.
2. Double-click **connect channel bot (once)** on the Desktop. Sign in to GitHub when asked, then choose the
   PIXELHELM channel on Google's page and press Allow. It does steps 1-13 below by itself, including a test video.
3. Use **START channel bot** (green) and **STOP channel bot** (red) on the Desktop. That's all.

Still yours, once: verify the channel by phone at https://www.youtube.com/verify (needed for custom thumbnails).

The manual steps below are kept for reference.

About an hour, once. Almost everything happens in your web browser. Nothing gets scheduled on your PC: the bot
runs on GitHub's computers: three Shorts every day (13:07, 17:37 and 21:13 UTC), up to two extra Shorts (10:19 and
23:29 UTC) only while the month's free runner minutes allow, and a long 16:9 video at 15:23 UTC on the weekdays in the
repo variable `LONG_DAYS` (default `1,3,5` = Mon/Wed/Fri). The code refuses to post from anywhere else.

**What the bot can do on YouTube:** upload a video to the PIXELHELM channel and set its thumbnail. That's all. It
can't comment, like, watch, subscribe or read other channels. Google enforces this because the key you give it
only has the "upload" permission (`youtube.upload`). The code checks it too: the bot refuses to run if the key
can do anything more, or if Google doesn't say which permissions the key has.

**Where the YouTube key lives:** in a protected GitHub **environment** called `youtube`. Only the `main` branch can
use it. The job that uses it installs no extra packages and can't change your repository.

**Is it public?** You chose (2026-11-03): **fully automatic**. Set `YT_PRIVACY = public` in step 9 and do **not**
add a required reviewer in step 8: the bot then posts on its own, no click needed. Google still keeps every
video from a new, unverified API project private until it passes Google's audit (step 14); after that they go
public by themselves.

> This is a deliberate exception to the studio rule "never post publicly without asking per post": it covers this
> bot only. To pause it, set `BOT_ENABLED = false` (or disable the `episode` workflow in the Actions tab).

---

## 1. Put the code on GitHub (private)

The code is in `C:\Users\User\Desktop\pixelhelm-channel-bot` (already a git repository).

1. Open **GitHub Desktop** (it's already installed) → **File → Add local repository** → choose that folder.
2. Click **Publish repository**. Name: `pixelhelm-channel-bot`. Keep **"Keep this code private"** ticked. Publish.
3. Open the repository on github.com. The **Actions** tab lists `ci`, `episode`, `lock-deps` and `pin-fonts`.

## 2. Protect the main branch

Repository → **Settings → Rules → Rulesets → New ruleset → New branch ruleset**. Name `protect main`,
Enforcement **Active**, Target branches → **Add target → Include default branch**. Tick **Restrict deletions** and
**Block force pushes**. Leave "Require a pull request" **off** (the bot saves its notes to `main`). **Create**.
Don't add other people as collaborators: anyone who can push to `main` can change what the bot does.

## 3. Make a Google Cloud project

1. Go to https://console.cloud.google.com/ and sign in with the Google account that **owns the PIXELHELM channel**.
2. Use the project picker at the top → **New project** → name it `pixelhelm-channel-bot` → **Create** → select it.

## 4. Turn on the YouTube Data API v3

**APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.

## 5. OAuth consent screen (External, then In production)

1. **APIs & Services → OAuth consent screen** (Google may call it **Google Auth Platform**) → **Get started**.
2. App name `PIXELHELM channel bot`, support email = yours → Audience **External** → contact email → agree → **Create**.
3. **Data access → Add or remove scopes** → under "Manually add scopes" paste
   `https://www.googleapis.com/auth/youtube.upload` → **Add to table** → **Update** → **Save**. Add **only** this one.
4. **Audience → Publish app → Confirm**, so the status shows **In production**.
   Why: in "Testing" mode Google expires the login after 7 days.

## 6. Make the OAuth client (Web application)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type **Web application**, name `channel-bot`.
3. **Authorized redirect URIs → Add URI** → `https://developers.google.com/oauthplayground`
4. **Create**. Copy the **Client ID** and **Client secret** into a temporary note (they are secrets).

## 7. Get the refresh token (choose the PIXELHELM brand channel)

1. Open https://developers.google.com/oauthplayground
2. Click the **gear icon** (top right) → tick **Use your own OAuth credentials** → paste the Client ID and secret →
   close.
3. In **"Input your own scopes"** paste exactly `https://www.googleapis.com/auth/youtube.upload` → **Authorize APIs**.
4. Sign in → **choose the PIXELHELM channel** when Google asks which account or channel → "Google hasn't verified
   this app" → **Advanced → Go to PIXELHELM channel bot (unsafe)** → **Continue / Allow**.
5. **Step 2 → Exchange authorization code for tokens**. Copy the **Refresh token**.

If you picked the wrong channel: https://myaccount.google.com/permissions → remove the app → repeat this step.

## 8. Make the `youtube` environment and put the secrets in it

Repository → **Settings → Environments → New environment** → name `youtube` → **Configure environment**:

1. **Deployment branches and tags** → **Selected branches and tags** → **Add deployment branch or tag rule** → `main`.
2. **Required reviewers**: leave this **off** (you chose fully automatic). Turning it on would make GitHub wait for
   your "Approve" click before every upload.
3. **Environment secrets → Add environment secret**, three times (**not** repository secrets):

| Name | Value |
|---|---|
| `YT_CLIENT_ID` | the Client ID from step 6 |
| `YT_CLIENT_SECRET` | the Client secret from step 6 |
| `YT_REFRESH_TOKEN` | the refresh token from step 7 |

Then delete your temporary note.

Optional **repository** secrets (Settings → Secrets and variables → Actions). Leave them out to keep the bot free:

| Name | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Claude writes each episode's captions and title (checked against the style rules; the built-in line bank is used if anything is off). About $0.05-0.10 per episode. |
| `AI_VIDEO_API_KEY` + `AI_VIDEO_PROVIDER` | Paid AI video for one shot. **Off by default.** See "Optional: AI video". |

## 9. Add the variables

Settings → Secrets and variables → Actions → **Variables** tab → **New repository variable**:

| Name | Value | Meaning |
|---|---|---|
| `YT_PRIVACY` | `public` | your choice: posts go public by themselves (`private` / `unlisted` also work; without the variable the code falls back to `private`) |
| `BOT_ENABLED` | `true` | the master switch. Until it is `true`, every run is a dry run. Checked by the workflow **and** by the Python code. |
| `LONG_DAYS` | `1,3,5` | (optional) weekdays for the long video: numbers 0 (Sunday) to 6 (Saturday), commas, no spaces; `daily` for every day. Unset = Mon/Wed/Fri. See README "Costs" before raising it. |
| `BOT_OVERLAY` | (leave unset) | optional: `off` turns the Remotion text overlay off and burns captions in with Pillow (the old look). Unset = `auto`. |

Add `BOT_ENABLED` **last**, after step 13 shows you a good dry run.

## 10. Lock the fonts and the Python packages (once, about 5 minutes)

1. **Actions → pin-fonts → Run workflow** (branch `main`). It records the EB Garamond font files' fingerprints
   (sha256) in `assets/fonts/fonts.lock.json` and commits that file. From then on every run checks the font bytes
   against it. Until this has run, titles use the Merriweather font that ships in the repo.
2. **Actions → lock-deps → Run workflow**. When it finishes, open the run → **Artifacts → requirements-lock** →
   download → unzip → you get `requirements.lock.txt`. On the repository's main page → **Add file → Upload files** →
   drop it in → **Commit changes**. From then on every package is installed only if its sha256 matches
   (`pip --require-hashes`). Re-run this step when someone changes `requirements*.txt`.

## 11. Pin Blender (optional: needed for fresh character drawings)

1. Open https://download.blender.org/release/Blender5.1/ → click `blender-5.1.0.sha256`.
2. Copy the long code on the line ending in `blender-5.1.0-linux-x64.tar.xz`.
3. In the repository, open `.github/workflows/episode.yml` → pencil icon → find `BLENDER_SHA256: ""` → paste the
   code between the quotes → **Commit changes**.

Until you do this, animated shorts use the 12 character drawings that ship in the repo (fine, just fewer poses).
The workflow refuses any Blender download whose sha256 isn't the one you pasted.

## 12. Upload the gameplay footage (once; the only step on your PC)

The clips are too big for git, so they go in a GitHub **Release**, which the bot downloads on each run.

1. In PowerShell:
   `python C:\Users\User\Desktop\pixelhelm-channel-bot\tools\make_footage_release.py C:\Users\Public\liminality_models\footage C:\Users\User\Desktop\footage-v1`
   This shrinks each clip (school.mp4: 14 MB to 0.4 MB) and writes `manifest.json`.
2. On github.com: repository → **Releases → Draft a new release** → **Choose a tag** → type `footage-v1` →
   **Create new tag** → title `footage v1` → drag **every file** from `Desktop\footage-v1` (all `.mp4` files and
   `manifest.json`) → **Publish release**. The release is private, like the repository.

## 13. Do a dry run and watch it

**Actions → episode → Run workflow** → format `auto`, **dry run ticked** → **Run workflow**.
After about 10-20 minutes, open the run → **Artifacts → episode-…** → download → watch `video.mp4`, look at
`thumb.jpg` and `video_sheet.jpg`. A dry run only has the `test` and `make` jobs; the publish jobs are skipped.

## 14. Verify the channel by phone, then go live

1. While signed in as PIXELHELM, go to https://www.youtube.com/verify. Without this, custom thumbnails fail:
   the video still uploads, but YouTube picks a frame for the thumbnail.
2. Set the variable `BOT_ENABLED` = `true` (step 9).
3. Optional: **Run workflow** with **dry run unticked**, to post one now. With a required reviewer, approve the
   `upload` job when GitHub asks.
4. From now on it posts Mon / Wed / Fri by itself. Each run adds lines to `state/episodes.jsonl`.
5. **Public videos need Google's audit.** New API projects can only upload private videos. To lift that, fill in
   the "YouTube API Services - Audit and Quota Extension Form" (search for that name in Google's YouTube API docs)
   for project `pixelhelm-channel-bot`, saying it only uploads the studio's own videos to its own channel. Until
   Google approves it, `YT_PRIVACY = public` still gives private videos.

To stop the bot: set `BOT_ENABLED` to `false`, or **Actions → episode → ⋯ → Disable workflow**.
To cut off its YouTube access: https://myaccount.google.com/permissions → remove the app.

---

## How a live run is split up (why it's safe)

| Job | Can use | Can't use |
|---|---|---|
| `make` (renders the video with Pillow, numpy, Blender) | read-only repo, optional Claude / AI-video keys | YouTube secrets, git write |
| `claim` (writes "claimed" and pushes it **before** uploading) | git write | any secret; it runs plain Python with no packages installed |
| `upload` (`youtube` environment) | the YouTube secrets | git write; no packages are installed |
| `record` (saves the result) | git write | any secret; plain Python only |

## When something goes wrong

The run turns red and the summary says why. The ledger (`state/episodes.jsonl`) shows each episode's status:

| Status | Meaning | What to do |
|---|---|---|
| `uploaded` | on YouTube (`video_id` is in the row) | nothing |
| `failed` | definitely not on YouTube (quota, expired upload, bad key) | fix the cause if it says one; the next run makes a new episode |
| `unclear` | the connection dropped at the end of the upload, so it might be on YouTube | look in YouTube Studio. The bot never retries this episode, so you can't get a double post. |
| `claimed` (no result row after it) | the upload job died or you rejected it | same as `unclear` |

**"token scopes are …; this bot only accepts exactly youtube.upload"**: the key has the wrong permissions. Redo
step 7 with only the one scope and update the secret in the `youtube` environment.
**"invalid_grant"**: the key stopped working (your password changed, the app was removed, or it was still in
Testing mode). Redo steps 5.4 and 7, then update `YT_REFRESH_TOKEN` in the `youtube` environment.
**Run fails at "Set up job" with "unable to resolve action"**: one of the pinned action SHAs is wrong. Open that
action's releases page (for example github.com/actions/checkout/releases), copy the full commit SHA of the
release named in the comment, and paste it into the workflow file.
**"Branch main is not allowed to deploy to youtube"**: the run came from another branch; only `main` may post.
**Thumbnail not set**: do step 14.1.
**Captions look like the old burned ones**: the Remotion overlay failed and the bot fell back (the episode still
ships). `qa.txt` in the run's artifact has an `INFO  text:` line with the reason; the "Make the episode" log has
the Node error. Nothing to do unless it keeps happening.
**No footage clips**: step 12 isn't done, or the release isn't named `footage-v1`. Animated shorts keep working
without footage.

## Optional: AI video (paid, off by default)

You need the secrets `AI_VIDEO_API_KEY` and `AI_VIDEO_PROVIDER`, the repository variable `AI_VIDEO_ENDPOINT` (the
provider's API URL), **and** that provider's API and download hosts listed in `bot/data/ai_video_hosts.json`
(change it on github.com in a commit to `main`). The bot calls only the hosts in that list, over https, never follows
redirects, and checks the download is an MP4 before using it.
With all of that, the first shot of each animated short is replaced by the AI clip, and the video gets YouTube's
"altered or synthetic content" flag plus a line in the description.
**The Higgsfield adapter was never tested against their real API.** Check their docs against the request shape at
the top of `bot/ai_video.py` before you pay for anything. If anything fails, the free drawn shot is used.

## Updating the characters

After the 3D artist changes the cast: `python tools/sync_assets.py`, then commit and push (GitHub Desktop). The next
run re-renders the pose sprites by itself (if Blender is pinned, step 11).
