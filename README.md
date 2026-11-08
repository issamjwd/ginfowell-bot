# ginfowell bot

Posts Shorts to the ginfowell YouTube channel on its own: real NASA clips and photos, on-this-day history, animals, places, the ancient world, football legends, inventions, the ocean, wild weather and food. Every picture and clip is public domain or under a free licence and credited; music is Kevin MacLeod (CC BY 4.0, credited); the narrator is the open Kokoro voice. It never reposts other creators' videos.

Runs on GitHub Actions (public repo). YouTube keys live only in the `youtube` environment, which only the main branch can use. Switches (repo variables): BOT_ENABLED, BOT_WORLD=only, BOT_VOICE, BOT_BRAND, BOT_TAGLINE, BOT_TAGLINE2, BOT_FOOTER.

The rest of this README describes the shared code, which started as the PIXELHELM channel bot.

---

# pixelhelm-channel-bot

Makes and posts videos for **liminality** (PIXELHELM's dreamcore horror game on Roblox) on GitHub Actions: three
Shorts every day, up to two extra Shorts when the month's free runner minutes allow (see "COSTS / LIMITS"), and a 2-3
minute 16:9 "dream tour" on the `LONG_DAYS` weekdays (default Mon/Wed/Fri; the owner runs it daily). **It only makes and posts videos on its own channel**: it holds the `youtube.upload` permission
and nothing else, and calls only `videos.insert` and `thumbnails.set` (checked by `tests/test_endpoint_guard.py`).

Owner setup: **[SETUP.md](SETUP.md)** (browser steps, plus one footage upload).

## What a run does

```
plan (template x dream x monster x caption lines; never repeats; optional Claude-written words)
 -> storyboard (validated JSON: lowercase, "dreams" never "rooms", no gore, safe glyphs, <= 58 s, flash spacing)
 -> sprites (headless Blender 5.1, Cycles CPU, the rigged cast + pose library; cached by content hash)
 -> render + edit (1080x1920 @ 24 fps; animation on twos; drawn backgrounds; impact frames, shake, speed lines,
                   zoom punch, whip/fade/beat cuts; synthesized score with a drop + hit on the impact)
 -> text overlay (Remotion, render/: hook title, captions, outro line, end card over a CLEAN render; the long
                  video renders its text as transparent frames FIRST and is graded + overlaid + encoded in one
                  pass; if anything about it fails, the episode is re-rendered with burned Pillow captions)
 -> QA gates (ffprobe: 1080x1920 H.264, 8-60 s, audio present and audible, no black > 0.8 s, no freeze > 2.5 s)
 -> thumbnail (1280x720) -> claim row pushed to git -> resumable upload -> result row pushed to git
```

Shorts are footage only (owner feedback 2026-11-04), from the `footage-v4` release (all 13 levels re-shot 2026-11-06 with the
photoreal look at graphics tier 4 / ultra, clips tagged monster / scare / chase with peak times; re-grab the dream
stills from a new recording with `python tools/sync_assets.py --stills-from <folder>`). Six edit styles in
`bot/edits.py`: `chase_cut`, `slow_dread`, `found_footage` (VHS look), `rules_list`, `monster_reveal`, `pov_walk`.
Each opens on a scare clip at its peak with a hit, cuts every 1-2.5 s, never shows a clip twice, keeps text at reading
speed (`bot/captions.py`) and ends on a 1.3 s tag over the last shot. The older templates (incl. the animated, still-
drawing ones) are retired in `bot/data/templates.json`; their code stays for the long video's cards and the tests.
The long video (`bot/longform.py`): 1920x1080, ~2 min, a chapter per current-game dream, full-frame 16:9 footage
(`footage-v4-wide`), chapter titles riding footage, the same cuts / captions / end tag as the Shorts, YouTube chapter
stamps.

## Layout

| Path | What |
|---|---|
| `bot/data/*.json` | **content bank** you can edit: dreams (13, counted from data), caption lines, templates |
| `bot/planner.py`, `templates.py` | picking and staging; `storyboard.py` the validator |
| `bot/variety.py` | the variety guard (same-day Shorts differ; hooks, tempos, music seeds and template x dream pairs rotate) |
| `bot/longform.py` | the long video: chapters per dream (title card, footage panels, drawn scenes), YouTube chapter timestamps |
| `bot/render.py`, `look.py`, `fx.py`, `music.py` | the editor (frames, drawn look, effects, score) |
| `bot/overlay.py`, `render/` | the Remotion text overlay (props from the storyboard, `node render/render.mjs`, audio mux, FFmpeg stills) and its burned-caption fallback in `pipeline.render_video` |
| `assets/style.json` | **the one place** for on-screen fonts, colours (one accent, #F2A7C3) and caption layout; read by Python and the overlay |
| `bot/qa.py` | the gates |
| `bot/publish.py`, `youtube.py`, `state.py` | claim / upload / record, the upload-only client and the ledger. **Stdlib only**: the jobs that hold YouTube secrets or git write access install no packages |
| `bot/writer_claude.py`, `bot/ai_video.py` | optional plug-ins (off without their keys) |
| `blender/` | `liminality_cast.py` (the cast builder + pose API) and `render_sprites.py` |
| `assets/` | cast `.blend`, palette, seed sprites, dream stills, shipped OFL fonts (Montserrat, Merriweather), font lock |
| `state/episodes.jsonl` | the ledger (claimed / uploaded / failed / unclear) |
| `tools/` | `make_footage_release.py`, `sync_assets.py`, `run_tests_offline.py` |

## Local use (optional; nothing is scheduled locally)

```
pip install -r requirements-dev.txt
python -m pytest                                   # or: python tools/run_tests_offline.py (no pytest needed)
# publishing (python -m bot.publish claim|upload|record) only runs on GitHub Actions
set BOT_FOOTAGE_DIR=C:\Users\Public\liminality_models\footage
set BOT_FORMAT=footage & python -m bot make        # DRY_RUN defaults to true: renders into out/, never uploads
```

`BOT_LOW_RES=1` renders 540x960 for quick looks. The text overlay needs Node + `npm ci --ignore-scripts` in `render/`
and a local Chrome (found automatically, or `REMOTION_BROWSER_EXECUTABLE`); it never downloads a browser. Without
them, or with `BOT_OVERLAY=off`, captions are burned in by Pillow as before. `qa.txt` says which path made the video. On Windows, `LOCAL_SERIF_FONT` / `LOCAL_SERIF_FONT_ITALIC` can point
at a local serif to preview titles without the CI-fetched EB Garamond.

Motion v3: one cue sheet (`bot/rig.py`) times the impact frame, the caption slam, the particle bursts and drifting
dust (`render/src/particles.jsx`) and the synthesised sound effects (`bot/sfx.py`: whooshes, impact thumps, end-card
ticks, the score ducked under them). No samples and no new packages. `BOT_SFX=off` renders the score alone.

Edit v4 (2026-11-05): every style has its own look, cut vocabulary, camera and opening title, so two Shorts in a row
never feel like the same template.
- **Grade** (`bot/grade.py`): one ffmpeg filter chain inside the clean video's encode: a filmic S-curve with split-toning
  folded into per-channel curves, halation (highlights only, quarter size, screen-blended in RGB), an optical vignette,
  fine temporal luma grain. chase_cut warm and hard, slow_dread cold and milky, found_footage green tape, rules_list
  rose, monster_reveal crushed and sickly, pov_walk natural; the long video has its own filmic look.
- **Motion** (`bot/motion.py`): eased push-ins, handheld drift, a walking bob (pov), snap-zoom punch-ins on the beat
  (zoom-blurred while they snap), slow motion into the reveal that snaps back on the impact cut, a freeze frame on the
  monster, and decaying (not random) shake. Cuts are edited on both sides: whip with real motion blur, flash cut,
  light-leak burn, dip to black, zoom-through, tape glitch (found_footage only). Flash cuts keep >= 1.6 s from any
  other flash or impact.
- **Text** (`render/src/Short.jsx`): kinetic hooks per style (slam / stretch / typewriter / per-word rise), caption
  words that pop in on a spring, chapter titles that stretch in on a spring.
- **Sound** (`bot/sfx.py`, cues from `bot/rig.py` edit_cues, each on the frame the picture does it): layered impacts
  with harmonics that phone speakers can play, kicks on punch-ins and flash cuts, risers that stop dead on the reveal,
  reversed swells into light leaks, sub drops on dips, glitch bursts, a quiet room tone; the score ducks under all of
  them. Mastered to -14 LUFS (two-pass, linear) with true peaks under -1 dBTP after AAC.

## World Shorts (not only Roblox)

Repo variable `BOT_WORLD` picks what the Shorts are about (the long video is always liminality):
`most` (default, owner 2026-11-07: 3 of every 4 Shorts are world Shorts, the 4th liminality), `mix` (alternate),
`only` (world Shorts only) or `off` (liminality only). A manual run's `topic` input overrides it for that run.
From 2026-12-01 there are 4 base Shorts a day (10:19, 13:07, 17:37, 21:13 UTC) + one budget-checked extra at
23:29 (bot/slots.py FOUR_FROM); before that, 3 base + 2 extras, because November's minutes were mostly used.

World Shorts (`bot/world.py`, switch in `bot/topic.py`) take turns between four kinds:
- `world_clips`: real NASA video clips (launches, spacewalks, Earth from orbit, Mars, the Sun...), public domain;
  the busiest few seconds of each video play in the frame.
- `world_space`: 3-5 real NASA photos of one subject (Saturn, Mars, nebulas, auroras...). NASA media is not
  copyrighted in the US; anything credited to someone else, and maps, charts and drawings, are skipped.
- `world_history`: "what happened on <today>", events from Wikipedia's "On this day" feed (CC BY-SA, credited),
  each shown with its article's picture ONLY when that picture is on Wikimedia Commons under public domain / CC0 /
  CC BY / CC BY-SA. Author and licence appear on screen and in the description. Grim events are left out.
- `world_nature`: topic lists in `bot/data/world_nature.json` (animals, places, ancient world, football legends,
  inventions, the deep ocean, wild weather, food; add titles or whole lists freely), each
  with its Wikipedia summary sentence and picture, on the same Commons licence terms.

The bot never reposts other creators' clips. A world Short skips footage, Blender and Remotion, so it bills fewer
minutes than a liminality Short. If NASA or Wikipedia can't be reached, the run makes a liminality Short instead.
Dry-run one with `template` = `world_space`, `world_history` or `world_nature`.

## Safety guarantees

- Scope is exactly `youtube.upload`: the token refresh refuses a token with any other scope **or no scope field**.
  A runtime URL allow-list blocks every other request, including a hijacked resumable-session URL, and a test
  scans the code for any other endpoint.
- The workflow is split by privilege. `make` runs the third-party packages with no YouTube secrets and no git write.
  `claim` and `record` have git write but no secrets and run plain stdlib Python. `upload` runs in the `youtube`
  environment (main branch only, with an optional required reviewer), has a read-only checkout, and installs no
  packages.
- Never double-posts: the claim row is committed and pushed **before** the upload. An episode that was ever
  claimed is never uploaded again. An ambiguous result, including any unexpected error after the claim, is
  recorded as `unclear` and never retried. A thumbnail failure never loses the video id. The 401, 308 and Range
  handling can't loop forever and never sends an empty chunk.
- Live work needs `BOT_ENABLED=true` (checked in the workflow **and** in Python), `GITHUB_ACTIONS=true` (the YouTube
  client refuses to run anywhere else), the main branch, and the environment secrets. Privacy defaults to private.
- Supply chain: actions pinned by SHA. Python packages are installed with `--require-hashes` from a reviewed
  `requirements.lock.txt` (exact pins until the owner adds it). Blender is checked against a sha256 written into
  the workflow, not one fetched from the same server. EB Garamond is pinned to a commit and sha256, and `pin` writes
  nothing unless every file verifies. The shipped fonts are hash-checked in tests.
- Untrusted inputs: LLM output is schema-constrained, then run through the storyboard validator, else the bank is
  used. AI-video hosts come from a committed allow-list: https only, no redirects, MP4 magic check, and ffmpeg
  opens the file with `-f mp4 -protocol_whitelist file`. Footage manifest file names must be bare `name.mp4`.
  Ledger notes are scrubbed of URLs' query strings and tokens, and capped at 200 characters.
- Quota is counted per Pacific day (YouTube's reset), with no tzdata dependency.
- A failed Blender render deletes its cache folder, and the cache is saved only when a `DONE` marker exists.

## COSTS / LIMITS

| Thing | Limit | This bot |
|---|---|---|
| GitHub Actions (private repo, Free plan) | 2,000 minutes / month, 500 MB artifacts, 10 GB cache | base schedule (3 Shorts + the long video every day) about 1,290 minutes in a 30-day month; the 2 extra Shorts a day only while the month projects to <= 1,850 (maths below); artifacts: live 1 day, dry runs 3 days; Shorts capped at 12 Mbit/s (~15-20 MB), the long video at 10 Mbit/s (~170 MB) |
| YouTube Data API quota | 10,000 units / day per project | `videos.insert` = 1,600 + `thumbnails.set` = 50 = 1,650 per video; 5 Shorts + 1 long = 9,900 a day (99%); an extra Short is skipped unless the Pacific quota day keeps room for the base videos still to come, and the ledger refuses a claim that would pass the daily budget |
| Public visibility | unverified API projects upload **private only** | until Google's API audit passes (SETUP.md step 12) |
| Custom thumbnails | need a phone-verified channel | SETUP.md step 11; the upload still succeeds without it |
| Claude (optional) | pay per use | ~$0.05-0.10 per episode with `ANTHROPIC_API_KEY` |
| AI video (optional, off) | provider pricing | only with `AI_VIDEO_*` set; flags the video as synthetic |
| Scheduled workflows | GitHub pauses schedules after 60 days without repo activity | the bot's own ledger commits keep it active; re-enable in the Actions tab if paused |

**Runner minutes (measured 2026-11-06, ubuntu-24.04, 2 cores; GitHub bills each job rounded UP to a whole minute).**

| Run | Measured | Billed |
|---|---|---|
| Short, make job (gate, setup ~1 min, render ~1 min, overlay ~2.3 min, QA) | 4 min 31 s (dry run 42238067522; 4:00 - 4:55 on earlier runs) | 5 (sometimes 4) |
| Long video, make job, ONE encode (setup 1 min, plan 0.7 min, text frames 1.7 min, render + grade + text + encode 4.3 min, QA + sheet 0.3 min) | 8 min 22 s (dry run 42236456241, a cold clip-profile cache; ~0.6 min less once warm). Was 17-19 min with footage-v4 (dry run 42224771254) | 9 |
| claim + upload + record (live runs only; 10-20 s each) | 3 x ~15 s | 3 |
| a cron whose slot is already filled, or an extra slot without budget (gate step only) | ~15 s | 1 |
| a long-video slot on a day not in LONG_DAYS (job-level `if`) | skipped | 0 |
| ci.yml (pull requests and code pushes to main only, since 2026-11-06) | 2-3 min a run | ~2 a day on average |

Base day (`bot/budget.py` `base_day`, LONG_DAYS=daily): long 9 + 3 + its backup 1 = 13; Shorts 3 x (5 + 3) = 24;
Short backups (13:49, 18:19) 2; extra slots that skip 2; CI 2. **43 a day, 1,290 in a 30-day month.** An extra Short
adds 5 + 3 = 8 (minus its 1-minute skip). Five Shorts every day: 43 + 2 x 7 = 57 a day, **1,710 a month**: inside the
1,850 line, so in a normal month the extras post every day. The gate checks for real before each extra: it sums this
month's billed minutes through the API (all workflows, ceil per job) and adds 8 + 43 a day for the rest of the month;
over 1,850, or if the API can't be read, the extra is skipped. The base Shorts and the long video never wait on it;
past 1,950 they log a warning in the run summary.

November 2026: 994 minutes were already used by 2026-11-06 15:00 UTC (664 of them dry runs, 267 CI on every push).
The base schedule alone projects to ~2,040, so **no extra Shorts this month** (the check will say "would not fit"), and
the base itself sits at the line. To stay under 2,000 in November: no more dry runs, and consider `LONG_DAYS` without 1-2
days for the rest of the month (each skipped long video saves 13). From December the full month fits 5 Shorts a day.

Where the long job's minutes went (2026-11-06): the clean intermediate (crf 15 with film grain, 3,168 frames) took 7-9
minutes and the compose pass decoded it and encoded it again (3.5 min). Now Remotion renders only the text ranges as
transparent PNGs first (the graded end tag's light film texture is plain alpha too, so no range needs the picture),
then the renderer's frames are graded, the text is laid over them and they are encoded once, straight to `video.mp4`
(x264 superfast, crf 19, 10 Mbit/s cap: at that cap the grain is what costs quality, and superfast measured 30.35 dB
PSNR / SSIM 0.914 against veryfast's 30.38 / 0.917 for half the x264 CPU). QA and the contact sheet share one decode.
The plan's clip measurements are kept between runs (actions/cache `.profile-cache`). ffmpeg's .deb files come from
the actions cache (a slow apt mirror cost a Short dry run 6 minutes, 42236495899). Same storyboard rendered both ways
locally: 43 dB PSNR between them, -14.4 LUFS, identical text. Step timings are logged as `timing: ...` lines.

Not merged (would save ~1 more minute a post, ~150 a month): claim, upload and record are three 15-second jobs on
purpose (git write and the YouTube secrets never share a job); merging claim + record around a parallel upload is
possible but changes the live path, which no dry run can test.

**Inauthentic / repetitive content risk.** YouTube's monetization rules exclude "inauthentic" (mass-produced or
repetitive) content, and its spam policy covers repetitive uploads. How this bot reduces that risk: 7 templates in two formats, alternated;
13 dreams x 4 characters x ~47 caption lines; every (template, dreams, monster, lines) combination is used at most
once (fingerprint in the ledger); no template twice in a row, no dream again within 3 episodes, no line again within 6;
each score is synthesized fresh from its own seed; all footage and characters are the studio's own game. Since
2026-11-04 (three to five Shorts a day): six footage edit styles that never repeat back to back or twice a day; same-day
Shorts differ in lead dream, hook line and music; hooks, tempos and template x dream pairs rotate over days
(bot/variety.py). It's still automated content, so watch a few, add your own videos now and then, and retire
templates that feel stale (`enabled: false` in `bot/data/templates.json`). Add lines and templates over time so it
doesn't run out of combinations.
