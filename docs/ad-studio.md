# Ad Studio

Architecture and what's deferred: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). When something goes wrong: [`docs/runbook.md`](runbook.md).

Makes Meta ad images, finished static ads and short videos for a brand's products, checks them, and
links each ad to its own Meta results. Everything brand-specific lives in a brand pack
(`brands/<BRAND>/`, see `brands/README.md`); this file describes the engine. It deploys from the `main`
branch (Railway is the documented host).

## Running it

```bash
PYTHONPATH=. python creative_intelligence/webapp/app.py      # http://localhost:5555/products
python -m pytest tests -q                                      # main suite (tests/legacy is excluded)
```

On Railway one service runs two processes (`Procfile`, started by honcho):

- **web**: gunicorn (`gunicorn.conf.py`, 2 workers × 8 threads). It answers requests and starts no
  background threads; the gunicorn master brings the schema up to date once per boot.
- **worker**: `python -m creative_intelligence.worker`. It works through the `jobs` table (image
  runs, QA scoring, text checks, the Meta sync, backups; `JOB_CONCURRENCY` at a time) and runs the
  scheduler (daily sync, nightly and weekly backups, volume check). It holds a lock so only one
  worker uses the data volume; on start it closes runs a restart interrupted and puts other
  unfinished jobs back in the queue. It checks in every 30 s; Admin → Jobs shows it, every recent
  job and Retry for failed ones, and the deploy smoke fails if it hasn't checked in.

The web queues work with `jobs.enqueue` (one queued or running job per dedupe key, so two
clicks or two processes can't start two syncs). Making a single image or AI ad by hand still
happens inside its request. Locally, `python creative_intelligence/webapp/app.py` runs the web
and a worker thread (`LOCAL_WORKER=0` to turn that off); `honcho start -e /dev/null` runs both
processes the way Railway does.

Code layout: `webapp/app.py` creates the app and `/healthz`; routes are blueprints in
`webapp/routes/` (pages, products, images, compose, aiads, gallery, drive, core, results, ops);
shared helpers in `webapp/common.py` (tests replace `common.db`, `qa_later`, `textcheck_later`).

### Deploying

Push (or merge a PR) to `main`. Railway is GitHub-linked to that branch only, with "Wait for
CI": it deploys after the tests pass, and only switches over once `/healthz` answers (database
reachable, volume writable). Then `.github/workflows/deploy-smoke.yml` checks the live app:
`/healthz` must report the new commit and `/admin/smoke` must load every main page. If the deploy
or the check fails, it rolls back to the last good deployment and posts an alert to the Admin
page (and the failed workflow emails you). `railway up` is no longer used.

GitHub secrets it needs: `RAILWAY_TOKEN` (a Railway **project** token for the app's project and
environment) and `ADMIN_TOKEN` (the app's `ADMIN_TOKEN`); repository variables `APP_URL`,
`RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_SERVICE_ID` and `RAILWAY_DEPLOY_ENVIRONMENT`.

## Flow

Four sections in the top bar, each a stage strip underneath (routes/pages.py, templates/_top.html);
Admin and Sign out sit in the Account menu. The product in the top bar travels with every link.

| Section | Stages | What happens |
|---|---|---|
| **Products** `/products` | — | The catalogue and the product profile (Product Hub, below). |
| **Images** `/images/<stage>` | Create · Review · Compose · Product shots | Create: start a run and follow it. Review: the image wall, QA scores, approve / reject. Compose: text on approved images, or AI ads (scene + copy drawn by code). Product shots: the fruit product-page set (the old Core photos page). |
| **Video** `/video/<stage>` | Create · Shots · Edit · Review | Create: pick a style (Animate now; the others say "coming soon"). Shots: the shot library. Edit: coming. Review: this product's clips. |
| **Gallery** `/gallery/<stage>` | Assets · Performance | Assets: every final image and video, all products by default, one click to this product. Performance: results on Meta (the old Results tab). |

Every asset shows one status: Draft (being made or not yet scored) → In review → Approved → Exported →
Live (linked to a Meta ad), or Rejected. Each stage ends with a "next step" link. The strip counts what
is waiting (images and clips to review). Old pages redirect: `/adstudio` → `/products` (or the stage an
old link meant), `/core` → `/images/shots`.

Hover or tab to a picture in Review or Gallery for its actions and the A/R/T/F/C keys; select several for
bulk actions (each kind only takes the actions it supports). The launch kit (one product) holds every
ready Compose ad, AI ad and video, named by Asset ID, with `manifest.csv`. Drive export sends AI ads.

## Features
### Product Hub — `adstudio/catalog.py`, `adstudio/hub.py`, migration 0011
The Products tab: every product on the left (search; filter by type, in stock, in season, has ads,
profile ready, not in store), the profile on the right in tabs.

- **Catalogue.** A daily job (after `SHOPIFY_SYNC_HOUR_UTC`, default 6) and "Sync now" read the
  store's public feed (`SHOPIFY_PUBLIC_URL/products.json`, no token). Build-a-box twins (tags
  hidden / exclude-cart / buildabox) and gift cards are left out. Types: "Fruit: …" → fruit,
  "Mix Box: …" → gift box, "Candle: …" → candle, else other. In stock = any variant available.
  In season = tag `c.abundant`; out of season = `c.seasonal` or `preorder`. Products made before the
  hub are linked by Shopify URL or exact name. A product that leaves the store is archived, never
  deleted (and nothing is archived if the feed comes back under half its usual size).
- **Not in store.** "+ Add product" makes one. Its prompts say it is not sold yet, and the copy
  check flags any price, stock or "order now".
- **Profile.** Overview (Shopify description, live prices, what's worked, hooks), Look & taste,
  Benefits & facts, Customer language (a slot for real customer words), References (tag each photo
  whole / cut / in hand / in box / lifestyle; 1–3 heroes go to the image model first), Hooks & ideas
  (the old Hooks tab).
- **Research.** Claude with web search drafts the taste, facts and customer language. Each fact carries its source URL
  and a confidence and arrives pending; health claims without a strong source, and claims about
  the brand not taken from our own product page, arrive as "do not use". Only approved facts
  and an approved taste reach prompts. The estimate is shown first; it counts on the Claude text cap.
- **Fill profile (on demand).** A "Fill profile" button on each product in the list and on its
  page (`fill` job); nothing fills by itself. It fills only what is empty: Shopify photos, the look
  and photo tags, research (taste, facts and customer language: real quotes, word for word, with
  URLs, in one web-search pass of up to 5 searches), and 5 Hook University hooks. Facts true of a
  whole fruit family (catalog.family: "Fruit: Mango, …" and "Mix Box: Mango, …" → mango) are
  researched once, shared, and listed for later research to skip. A failed part doesn't stop the
  rest; the text cap stops it. Once done the list shows "Researched".
- **Hooks (Hook University).** `adstudio/hook_university.py` + `prompts/hook-university-system`: the
  original method (D+C+PS, archetypes A1–A12, an angle mix with one random angle per batch, banned
  templates, the original hook QA, saved hooks treated as exhausted, one retry) merged with the
  winning Meta patterns, the operator's idea, approved facts and on-image headline/subhead/body/CTA.
- **Readiness.** Ready for images = 2+ tagged references (one cut, for fruit), the look, 1+ hook.
  Ready for video = that plus 5+ approved facts and the taste approved. Generate images (an 8-image
  run), Animate (pick an approved image) and Make video (coming) sit at the top and say what's missing.


### Login (T0)
Shared password on every page and API (`ADSTUDIO_PASSWORD`, session cookie signed with
`FLASK_SECRET_KEY`). On Railway the app is locked if the password is not set. `/admin/*` endpoints
keep their `ADMIN_TOKEN` header check. Retired pages and endpoints answer "This page was retired" (410).

### Automatic Meta sync (T1) — `ingest/meta_sync.py`, `ingest/scheduler.py`, `ingest/daily_insights.py`
Read-only. The worker's scheduler queues the sync once a day after `SYNC_HOUR_UTC`; the Sync button and `POST /admin/meta-sync` (cron, `X-Admin-Token`) still
work. Each sync re-fetches the last 3 days per ad per day into `ad_daily_insights` (upserted, no
duplicates; includes 3-second views and thruplays for hook and hold rate). Claude tagging is cached
per ad copy. The Hooks tab shows "Last synced" and warns after 7 days or a failed run.

### Asset IDs and results (T2) — `adstudio/assets.py`
Every finished ad gets an Asset ID (`ASSET_ID_TEMPLATE`) and a Concept ID shared by variants of one
idea, plus tags (angle, hook type, format, style, product, product type, source tab, image model,
prompt version, run, generation cost). The ID is the file name in launch kits and Drive, and the
"Meta ad name" column in the Drive sheet. Each sync links Meta ads whose name contains an Asset ID;
cards can also be linked by hand. Linked cards show ROAS, CPA, CTR, spend (and hook/hold rate for
video); the Results tab groups them by angle, hook, format or product.

### Text on ads (T3) — `adstudio/textcheck.py`, `adstudio/templates.py`, `adstudio/render_template.py`
AI Ads makes **text-free** images (`TEXTFREE_AI_ADS=1`) and draws the brief's copy with code, so it
is spelled exactly; copy edits re-draw instantly with no new image. The 12 formats are Compose
templates (layout data in `templates.py`, renderer separate) that adapt to 9:16 (Stories-safe),
4:5 and 1:1. Price is optional, off by default, and read live from Shopify (import-time price as a
marked fallback). Ads whose text was drawn by the image model are read back by Claude and
auto-rejected if anything is misspelled, missing, repeated or invented.

### Quality scoring (T4) — `adstudio/qa.py`, `config/qa_rubric.yaml`
Claude scores each new image 1–5 per criterion against the product's reference photos; legibility
of drawn text is measured by code (contrast at phone size). Edit the YAML to change criteria,
weights, minimums or the pass mark — no code change. Galleries hide QA failures (see "Rejected by
QA"), can sort by score, and show the reasons on hover. Approving a QA failure or rejecting a high
scorer is logged; the rate is on the Admin page.

### Runs and fan-out (T5) — `adstudio/runs.py`, `adstudio/sizes.py`
Ask for 8, 12, 16 or 20 images. Claude plans the run across scene, camera, light, surface and fruit
state; a server worker makes 3 at a time, scores each as it lands and quietly regenerates failures
with QA's reasons (max 3 tries per slot). The run stops before going over `BUDGET_RUN_USD` or the
day's `BUDGET_DAY_USD` and says "N of M delivered" with reasons. "Fan out" turns one image into
3 headlines × 3 layouts × 3 sizes; sizes are fitted without cropping.

### Facts and claims (T6) — `adstudio/facts.py`
Approved facts for every product and banned words live in the Admin page; a product's own facts
are reviewed on its profile (Product Hub). Hook, headline and brief prompts may only
state those facts (and the operator's product setup). Every brief, saved hook and Compose ad is
checked: banned words by code, factual claims by Claude; anything unapproved is flagged on the card.
Every asset is marked AI-generated in manifests and the Drive sheet.

### Admin overview, alerts and logs (F8)
The top of Admin is the operating picture: images-and-video and Claude spend today against their
caps, cost per delivered image (each run, and the 7-day average), volume used, failed jobs in the
last 24 hours, whether the worker is running, the last Meta sync and backups, spend by provider
for today / 7 / 30 days, and recent runs.

Alerts (failed deploy, backup or job; volume over 80%; tagging over its level) show as a strip
under the top bar on every page and as cards on Admin until dismissed. Set `SLACK_WEBHOOK_URL`
(a Slack incoming webhook) to also post them to a channel. Logs are one JSON object per line on
Railway (`LOG_FORMAT=json`, the default there; gunicorn access lines too), readable text locally.

### Admin (`/admin`)
Alerts (failed deploys, rollbacks) at the top until dismissed, Meta sync history, spend today and by kind, caps, model prices (mark them checked against your
bills), recent runs, QA overrule rate and rubric, and the facts library.

### Costs, caps and models (F5) — `budget.py`, `providers/`
Every image, video and Claude call is logged in `model_calls` (and a line in `outputs/api.log`) with
its estimated cost from `model_prices`; prices marked unverified should be checked on Admin against
the bills.

Every paid call is checked against a cap first, through the provider adapters
(`providers/anthropic.py` for Claude, `providers/replicate.py` for images and video):

| Cap | Default | Covers |
|---|---|---|
| Images, per run | $10 | an image run (`BUDGET_RUN_USD`) |
| Images, per day | $50 | all image generation (`BUDGET_DAY_USD`) |
| Video, per job / per day | $25 / $60 | Video Studio clips and their stills (`BUDGET_VIDEO_JOB_USD`, `BUDGET_VIDEO_DAY_USD`) |
| Claude text calls, per day | $15 | hooks, briefs, chat, QA, text and claims checks, run plans (`BUDGET_TEXT_DAY_USD`) |
| Meta sync tagging | never blocked | logged; an Admin alert when a day passes $5 (`BUDGET_TAGGING_ALERT_USD`) |

A day runs from 00:00 the brand's timezone. Over a cap the call stops before anything is spent with "Daily cap
reached — resets at 00:00 the brand's timezone. Admin can raise it." (the API answers 402). The caps are
edited on Admin; the settings above are only the starting values.

The model registry (`providers/__init__.py`, shown on Admin) lists each role (copy, check, tag,
image, video), its model setting, budget and price. Video: Kling 3.0 on Replicate
(`kwaivgi/kling-v3-video`, `CI_VIDEO_MODEL`). Its price is an **unverified estimate of $0.168 per
second** (standard 720p, no audio): Replicate's page shows none; Kling's own API is $0.084/s at 720p
and $0.112/s at 1080p, and a third-party comparison says Replicate charges about twice that. A 5 s
clip is about $0.84. Proof clip (dry run unless `--no-dry-run`):

```bash
python -m creative_intelligence.providers.video_proof --product 1
```

## Prompts (F6) — `creative_intelligence/prompts/`

Every fixed prompt is a text file, `<name>.v<N>.txt`, read exactly as written (17 today: the
reference locks per product type, concept and scene rules, the copy chat, run planning, QA, text
and claims checks, Core shots, Meta tagging). Code loads the newest version with
`prompts.text(name)`; `${box}`-style placeholders are filled in by the code.

To change a prompt, copy the newest file to the next number and edit the copy; the old version
stays. Each generated image, AI ad and Core shot records which files and versions made it in
`prompt_version` (e.g. `run-plan-system@v1+fruit-concept-rules@v1+fruit-reference-lock@v1`), and
its Asset tags carry the same, so results can be compared before and after a prompt change. Rows
made before this keep a short hash of their prompt instead.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `ADSTUDIO_PASSWORD` | — | Team password (required on Railway) |
| `FLASK_SECRET_KEY` | random | Signs the login cookie; set it so logins survive restarts |
| `ADMIN_TOKEN` | — | Header token for `/admin/meta-sync`, `/admin/smoke`, `/admin/alert` |
| `WEB_CONCURRENCY`, `WEB_THREADS` | `2`, `8` | gunicorn workers and threads per worker |
| `JOB_CONCURRENCY` | `3` | Jobs the worker runs at once |
| `LOCAL_WORKER` | `1` | Local `python app.py` also runs a worker thread |
| `ANTHROPIC_API_KEY`, `CI_REPLICATE_API_KEY` | — | Claude and Replicate |
| `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `META_API_VERSION` | — | Read-only Meta access |
| `SHOPIFY_STORE_URL`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_API_VERSION` | — | Shopify |
| `SCHEDULER_ENABLED`, `SYNC_HOUR_UTC` | `0`, `5` | Daily automatic sync |
| `BUDGET_RUN_USD`, `BUDGET_DAY_USD`, `BUDGET_TEXT_DAY_USD`, `BUDGET_TAGGING_ALERT_USD` | `10`, `50`, `15`, `5` | Starting caps (then edited on Admin) |
| `SLACK_WEBHOOK_URL` | — | Optional: post alerts to Slack |
| `LOG_FORMAT` | `json` on Railway, else `text` | Log line format |
| `BUDGET_VIDEO_JOB_USD`, `BUDGET_VIDEO_DAY_USD` | `25`, `60` | Starting video caps (then edited on Admin) |
| `ANIMATE_STILL_STRATEGY` | `outpaint` | How a still becomes 9:16 for Animate: `outpaint` or `native` |
| `VIDEO_X264_PRESET` | `medium` | Encoder speed for finished videos (tests use `ultrafast`) |
| `CI_VIDEO_MODEL_AMBIENT`, `CI_VIDEO_MODEL_MULTIREF` | `google/veo-3.1-fast`, `bytedance/seedance-2.0` | Shot-library routes for ambient and multi-reference shots |
| `VIDEO_FALLBACKS` | `veo:kling,seedance:kling` | Route to try once when a route's model fails |
| `CI_VIDEO_MODEL` | `kwaivgi/kling-v3-video` | Replicate video model |
| `TEXTFREE_AI_ADS`, `FACTS_CHECK` | `1`, `1` | T3 text-free AI Ads; T6 claims checks |
| `ASSET_ID_TEMPLATE` | `GHF_{concept}_{angle}_{hook}_{format}_{product}_v{version:02d}` | Meta ad name format |
| `CI_CLAUDE_COPY_MODEL` | `claude-sonnet-4-5` | Hooks, briefs, chat, scenes |
| `CI_CLAUDE_CHECK_MODEL` | `claude-opus-5-5` | QA, text check, claims, run plans |
| `CI_CLAUDE_TAG_MODEL` | `claude-opus-5` | Tagging Meta ads in the sync |
| `CI_IMAGE_MODEL` | `google/nano-banana-pro` | Replicate image model |
| `ADSTUDIO_DRIVE_FOLDER_ID`, `GOOGLE_OAUTH_CLIENT_ID/SECRET` | — | Drive export (no default folder) |
| `BACKUP_DRIVE_FOLDER_ID`, `BACKUP_HOUR_UTC` | —, `3` | Drive "Backups" folder for the nightly database backup, and the earliest hour (UTC) |
| `BACKUP_IMAGES_WEEKDAY` | `6` | Day of the weekly image backup (0 = Monday) |
| `CI_RAW_DATA_DIR` | `data/raw` | Where raw Meta API responses are saved (v2: on the volume) |
| `STORAGE_BACKEND` | `volume` | File storage; only `volume` exists (object storage deferred by design) |
| `ALLOW_RESTORE` | `0` | `1` opens the copy route `/admin/restore-snapshot` (with `ADMIN_TOKEN`); turn off after |
| `QA_RUBRIC_PATH` | `config/qa_rubric.yaml` | Use a different rubric file |

## Video Studio (Phase 2b)

### V1 — Animate an approved still (`adstudio/animate.py`, `video_qa.py`, `videotools.py`)
**Animate** appears on approved images (Images tab and Gallery) and on Core shots. Pick a motion
preset (camera-only moves first: they keep the fruit still and drift least), or describe one; 5–8 s;
optionally a Compose layout and a hook for the text. The dialog shows the estimate before anything
is spent. Then, as a job in the worker:

1. **Start still, never cropped.** Kling follows the start image's shape, so the still must be 9:16.
   A 9:16 image is used as is; otherwise it is **extended** ("outpaint": Nano Banana Pro continues the
   photo above and below, the photo and reference photos attached) or **regenerated natively** at
   9:16 from the image's own prompt (`ANIMATE_STILL_STRATEGY`, default `outpaint`). Anything that
   still comes back in another shape is padded with a blurred fill.
2. **Clip** from the still (`CI_VIDEO_MODEL`, Kling 3.0 standard), with the motion rules prompt
   (`prompts/animate-motion-rules`): same fruit count, shapes and positions; no text.
3. **Video QA**: one frame per second on a contact sheet plus the first and last frames, scored
   with the rubric's video criteria (fruit accuracy, warping/morphing, hands, consistency first to
   last). A different fruit count between first and last frame always fails. A failing clip is
   made once more with QA's reasons; the second is kept either way (under "Rejected by QA" if it fails).
4. **Finished versions**: 9:16 and 4:5 (padded, not cropped), each with the copy drawn by the
   Compose renderer as a transparent layer and laid over the clip by ffmpeg. Each gets an Asset ID
   (all versions of one clip share concept `V<id>`), style `animate`, and the job's cost.

In the Gallery (type **Videos**) cards play on hover; **More headlines** makes the same clip with
other hooks in both sizes at no cost; approve / ready / launch kit work as for images (the manifest
gains style, length, size and `ai_person`). Caps: **$25 per video job** and **$60 per day for video**,
separate from the image caps, editable on Admin; a restart closes a half-made clip rather than
paying for it twice.

### V2 — Shot library (`adstudio/shots.py`, `prompts/shots/`)
The **Shots** tab lists one card per shot type for the product: orchard wide, fruit on the tree, hand
picking, packing, whole-fruit hero, knife cut, cross-section macro, juice/texture macro, box opening,
plated/sharing. Each type has a versioned recipe card (`prompts/shots/<type>.vN.yaml`: purpose,
length, energy, the still's locks/freedom/composition/lighting, the motion, model route, sound,
parameters and known pitfalls).

- **Made once, reused.** Generating a type that already has a passing shot returns that shot; nothing
  is spent. Retire a shot to make a new one. Timelines (V3) take shots from here and count each reuse.
- **From an approved still where possible**: the best approved image of a matching kind (e.g. a
  cross-section image for the cross-section macro), extended to 9:16 like Animate; otherwise a still
  generated from the card plus the product type's reference lock.
- **Model routing**: product, cut and macro shots → Kling 3.0; orchard and lifestyle shots with
  ambient sound → Veo 3.1 Fast (`CI_VIDEO_MODEL_AMBIENT`); multi-reference shots → Seedance 2.0
  (`CI_VIDEO_MODEL_MULTIREF`). If a model fails, the route's fallback (`VIDEO_FALLBACKS`) is tried once.
- **QA**: as Animate; busy cards (`strict_qa`: hand picking, packing, knife cut, box opening,
  plated/sharing) need the same fruit first to last exactly and very little warping. A retake gets
  the fix for what failed.

### V3 — Timeline engine + storytelling (`adstudio/timeline.py`, `story.py`, `voices.py`)
- **A video is a timeline** (JSON): picture (library shots), voice-over with word timings, captions,
  on-screen text, sound effects, music, end card. `timeline.render` draws it with ffmpeg: each shot
  framed to the size (background-only crop around the product, else padded), stretched up to 1.35x or
  held on its last frame to fill its line, joined with 0.3 s crossfades; captions burned in a few words
  at a time from the word timings (PIL); the hook headline from the Compose templates; music ducked
  under the voice (sidechain compression); a code-drawn end card (product photo, name, CTA, the brand mark).
  9:16 and 4:5. Remotion wasn't needed.
- **House voices** (`config.HOUSE_VOICES`, from the brand pack's `voices`): Kokoro on Replicate (well under
  a cent a voice-over), ElevenLabs v3 or Gemini TTS on Replicate, picked per voice. Word timings: WhisperX on Replicate; if it fails,
  captions are timed roughly and the video says so.
- **Storytelling** is now Fruit Story's **narrated** version (S1: one style with a presenter-led / narrated
  switch, Video › Create › Fruit Story); the card below is gone, and videos made with it stay in Gallery.
  How it worked: Claude writes a 20, 30 or 45 s script from approved facts only
  (no origin, prices or presenter-as-farmer), each line on a shot type, plus an on-screen hook and an
  end-card CTA; the copy is claims-checked. The plan shows which shots are in the library; missing
  ones are generated only if ticked, otherwise lines reuse library shots. The `story` job waits for
  shots being made (up to 45 min), then voice → timings → timeline → both sizes → video QA (no
  fruit-count check across cuts). Music: tracks in `music/` only.

### V4 — Winner decoder (`adstudio/winners.py`, Video › Create › Proven style)
- **Which ads:** the top video ads by 90-day spend (latest snapshot, at least $300, one per concept),
  best cost per purchase first, with 7-day hook rate (3-second views / impressions) and hold rate
  (ThruPlays / 3-second views).
- **Fetch (read-only):** GET the ad's creative → its video → the mp4. Raw responses are saved to the raw
  data folder with a timestamp (never overwritten); every call is logged to `outputs/api.log` (no token).
  If the token can't read the video (Page-owned videos need Page access), the thumbnail and ad copy are
  used and the template is marked "Thumbnail only".
- **Decode:** cuts by ffmpeg scene detection, one frame per cut, speech with word times (WhisperX), then
  Claude writes the template: hook type, each shot's type and timing, presenter share, when product,
  price and CTA appear, on-screen text, pacing, why it works, how to rebuild it. About $0.19 an ad,
  on the Claude text cap. Templates are presets for V6, not the only way: keep testing new AI styles.

### V5 — AI presenters (`adstudio/presenters.py`, `talking.py`, Video › Create › Talking head)
- **Presenters:** from the brand pack's `presenters` (name, role, voice, look, framing), editable in the
  app. Brand narrators only: never shown as customers or the brand's real staff (the look prompt rules out uniforms and farm-worker cues). Approved facts only.
- **Looks:** Nano Banana Pro portraits (9:16, chest-up, facing the camera, mouth closed: what lip-sync
  needs), two at a time; "Use this face" approves the presenter. The character sheet (5 angles and
  expressions) is made from the approved face as the reference, so the face stays the same.
- **Talking-head clips** (style `talking`, `ai_person` = 1, QA'd for lip-sync, face identity and hands):
  `veo` = Veo 3.1 Fast with its own speech (4/6/8 s lines, not the house voice); `omnihuman`,
  `kling_avatar`, `wan_s2v` = the house voice reads the line, then the model makes the presenter say it.
  Lines are claims-checked first. The comparison sample makes the same line three ways. Styled videos default to
  `kling_avatar` (cheapest; 2026-11-02); fruit characters lip-sync with `omnihuman`, which animates any picture.

### V6 — Styles and variants (`adstudio/styles.py`, Video › Create)
- **Styles:** Fruit Story (presenter hook → orchard → tree → three cuts → presenter on why it's different
  → offer card), Talking head, Sensory / ASMR (no voice), Kinetic type, POV / gift reveal, and From a
  winning ad (a decoded V4 template). Each is a prompt file `prompts/style-<name>`.
- **Lines** are narrator (house voice over a library shot), presenter (a talking-head clip, ai_person) or
  text (big kinetic words over a shot, silent). The voice track is built line by line; word timings run
  once per hook × length for the captions (which step aside for on-screen text).
- **Variants** without new shots: up to 3 hooks, 30 s and 15 s (lines marked short; the closing line
  always stays), 9:16 and 4:5, bold or boxed captions. Each piece is made once, so an extra variant
  costs only what is new (another hook's voice-over or presenter clip). Each variant is a render of the
  same video: its own Asset ID under one concept. The editor shows the total, the first, and the cost
  per extra variant before anything runs.
- Proven styles and templates sit next to new styles: keep a share of every batch for tests.

## Data: migrations, backups, restore

- **SQLite** in WAL mode with a 30 s `busy_timeout`, so the gunicorn workers wait for each other
  instead of failing on a write.
- **Migrations** are numbered: `creative_intelligence/migrations/NNNN_name.sql`, applied once, in
  order, recorded in `schema_version` (1 = the baseline: `schema.sql` plus the older additive list,
  now frozen). Add a schema change as the next file; write it so a re-run is harmless.
- **Nightly database backup to Google Drive** (`creative_intelligence/backup.py`): after
  `BACKUP_HOUR_UTC` the scheduler takes an online SQLite backup, checks it, gzips it and uploads it
  to the "Backups" folder (`BACKUP_DRIVE_FOLDER_ID`) with the Drive account connected in the
  Gallery. The newest 14 stay; older ones go to the Drive trash. Results and a "Back up now"
  button are on Admin; a failed backup posts an Admin alert.
- **Railway volume backups** (database and images) are on: daily, kept 6 days; weekly, kept 27 days.
  Restore those from the Railway dashboard (service → Volume → Backups).
- **Files** (reference photos, images, ads) live on the Railway volume behind one interface,
  `creative_intelligence/storage.py`: areas `photos`, `images`, `ads`, `composites`, `core`,
  `textads` under `<data>/adstudio/`, next to the database. The database stores each file's ref
  (today its path). Object storage is deferred by design: only `STORAGE_BACKEND=volume` exists.
- **Weekly image backup**: every `BACKUP_IMAGES_WEEKDAY` (default Sunday) the scheduler copies
  image files Drive doesn't have yet into `Backups/images/<area>/` (`ops_backup_files` lists what's
  sent). Nothing is deleted from Drive.
- **Volume usage** is on Admin (used, free, by area); at 80% full an Admin alert appears.
- **Raw Meta responses** go to `CI_RAW_DATA_DIR`; on v2 that's `/app/creative_intelligence_data/raw`
  on the volume, so they survive deploys (they used to land in the container's `data/raw`).
- **Restore a Drive backup** (ask before each use; run in the app's container, `railway ssh`):

  ```bash
  python -m creative_intelligence.backup restore <Drive file id or path.db.gz>               # dry run: checks it, lists what would change
  python -m creative_intelligence.backup restore <Drive file id or path.db.gz> --no-dry-run  # copies it over the live database
  ```

  Then restart the service so every worker reopens the database.
- **Copy route** (`webapp/restore.py`, `scripts/restore_to_v2.sh`, `scripts/snapshot_remote.py`):
  loads a whole data snapshot (database + images) into v2. Off unless `ALLOW_RESTORE=1`, needs
  `ADMIN_TOKEN`; the script is a dry run unless given `--no-dry-run`. Ask before each use.

### Setting up Google Drive (once)

Signed in to Google as the account that will own the files:

1. [Google Cloud console](https://console.cloud.google.com/) → create a project (e.g. "Ad Studio")
   → APIs & Services → Library → enable **Google Drive API**.
2. OAuth consent screen → user type **Internal** (if the account is in your Google Workspace;
   no review, and sign-ins don't expire). If you must use External, set it to **In production**:
   in "Testing" Google expires the sign-in after 7 days and backups stop.
   Scopes: `openid`, `email`, `https://www.googleapis.com/auth/drive` (the app writes into
   folders it didn't create, so the narrower `drive.file` won't do).
3. Credentials → Create credentials → OAuth client ID → **Web application**. Authorized redirect
   URIs: `https://<your-app>.up.railway.app/drive/callback` (and
   `http://localhost:5555/drive/callback` for local use).
4. In Railway add `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`.
5. In that account's Drive create the folders **Backups** and **Test exports**. Open each; its ID
   is the part of the address after `/folders/`. Set `BACKUP_DRIVE_FOLDER_ID` (Backups) and
   `ADSTUDIO_DRIVE_FOLDER_ID` (Test exports) in Railway.
6. In the app: Gallery → **Connect Google Drive** → sign in as that account. Then Admin →
   **Back up database now** and **Back up new images now** to check it end to end.

## Design system

One stylesheet, `creative_intelligence/webapp/static/studio.css`: tokens and every component.
No inline styles. See CLAUDE.md for the rule.

## Tests and CI

`tests/` runs on Python 3.11 in GitHub Actions on every push to `main` and every PR into it
(`.github/workflows/ad-studio-tests.yml`). A root fixture blocks live Claude and Replicate calls.
First comes the mocked end-to-end smoke, `tests/test_e2e_smoke.py`: product → photo → hook → 8-image
run through the jobs table and worker → gallery → Compose → approve and ready → launch kit, with
Admin, Jobs and costs checked. `tests/legacy/` covers code kept for later (Video for Phase 2, the old
static rendering layer in `creative_intelligence/legacy/`) and is not part of the suite.

## Which sync writes where

| Job | Runs on | Writes |
|---|---|---|
| Ad Studio Meta sync | inside the app | its SQLite database on the Railway volume |
