# Ad Studio — architecture

A brand's ad-making app: product photos → hooks → images → finished ads → Meta results.
Feature guide: `docs/ad-studio.md`. Runbook: `docs/runbook.md`.

## Where we are (2026-10-16)

Phase 1 (T0–T6), the layout work and Foundations F1–F8 are built. Brand specifics live in
`brands/<BRAND>/` (creative_intelligence/brand.py).

| Area | Now |
|---|---|
| Runtime | One Railway service, two processes via honcho (`Procfile`): **web** = gunicorn, 2 workers × 8 threads; **worker** = `python -m creative_intelligence.worker` |
| Code | `webapp/app.py` (75 lines) + 10 blueprints in `webapp/routes/`; shared helpers `webapp/common.py` |
| Background work | SQLite `jobs` table → the worker (image runs, QA, text checks, Meta sync, backups). The web starts no threads. Scheduler runs in the worker |
| Database | SQLite on the Railway volume, WAL, `busy_timeout` 30 s, numbered migrations (`creative_intelligence/migrations/`, `schema_version`) |
| Files | On the volume behind `creative_intelligence/storage.py` (one interface; areas photos, images, ads, composites, core, textads, videos, presenters) |
| Backups | Nightly database → Drive "Backups" (keep 14); weekly new images → Drive; Railway volume backups daily (6 days) + weekly (27 days); restore command tested |
| Models | Provider adapters (`providers/anthropic.py`, `providers/replicate.py`), registry of roles (copy, check, tag, image, video) shown on Admin |
| Spending | Budget check before every paid call: images+video $10/run, $50/day; Claude text $15/day; tagging never blocked, alert above $5/day. the brand's timezone days. Editable on Admin |
| Prompts | Versioned files in `creative_intelligence/prompts/`; every picture records `prompt_version` |
| Deploys | GitHub → Railway on `main` with "Wait for CI"; Railway healthcheck `/healthz` (database + volume); post-deploy smoke (`deploy-smoke.yml`: `/healthz` commit + `/admin/smoke` pages + worker); rollback and Admin alert on failure |
| Observability | Admin overview (spend vs caps, cost per delivered image, volume, failed jobs, worker, last sync/backup); alerts strip on every page, optional Slack; JSON logs |
| Tests | 509 in `tests/` on every push, plus a mocked end-to-end smoke as its own CI step; no live API reachable from tests |

## Deferred by design

These stay out until the lean setup stops being enough; each has a clear trigger.

- **Postgres** — SQLite on one volume serves one service with one writer process well. Revisit
  when we need a second service writing to the same data, more than one replica, or the
  database passes a few GB / write contention shows in the logs.
- **Object storage** (S3, R2) — files stay on the volume behind `storage.py`, so moving is one new
  backend class. Revisit when the volume passes ~70% of a size we're willing to pay for, or files
  must be served to another system directly.
- **Redis / a separate worker service** — the SQLite jobs table and a worker process in the same
  service are enough for a few jobs at a time. Revisit with Postgres, or if jobs need to run on a
  second machine.

## Shape of the code

```
creative_intelligence/
  webapp/        app.py, routes/ (blueprints), common.py, startup.py, auth.py, templates/, static/studio.css
  worker.py      the worker process          jobs.py      jobs table: enqueue / claim / retry / recover
  budget.py      caps and checks             alerts.py    alerts (+ Slack)
  backup.py      Drive backups and restore   storage.py   files on the volume
  providers/     Claude and Replicate adapters, model registry, Kling proof clip
  prompts/       versioned prompt files      migrations/  numbered schema changes
  adstudio/      the product logic (concepts, runs, QA, text check, templates, facts, assets…)
  ingest/        Meta sync, scheduler, daily insights      tagging/  ad tagging
  legacy/        code kept for later (Video for Phase 2, the old static renderer)
```

## Video Studio (V1–V7, 2026-10-20)

Video is built on one **timeline engine** in ffmpeg (`adstudio/timeline.py`); Remotion was not needed.
Shots are generated once, kept in a library and reused; finished videos are cut from them.

| Piece | Where | What it does |
|---|---|---|
| Shots | `adstudio/animate.py`, style `shot` | Veo 3.1 Fast (or Kling) clips from approved images, QA-scored, reused by every later video (`reuse_count`) |
| Voices | `adstudio/voices.py` | House voices from the brand pack (Kokoro, ElevenLabs v3 or Gemini TTS); WhisperX word timings for captions |
| Music | `music/` + `tracks.json` | 18 Meta Sound Collection tracks, **Facebook and Instagram ads only**; ducked under the voice |
| Story | `adstudio/story.py` | Script from library shots only → plan → render (9:16 and 4:5) with captions, hook, end card |
| Winners | `adstudio/winners.py` | Read-only Meta: top ads decoded into templates (presets only; a share of every batch stays "test") |
| Presenters | `adstudio/presenters.py`, `talking.py` | AI brand narrators (never customers or a the brand farmer), approved faces, talking clips; every video `ai_person` |
| Styles and variants | `adstudio/styles.py` | Fruit story, talking head, sensory, kinetic, POV, from a winner; 3 hooks × full/15 s × 2 sizes = 12 versions from pieces made once |
| In the app | Gallery, Results, Admin | Gallery filters by style and length (each version keeps its own `duration_s`); Results groups by style; Admin → Video shows cost per video by style and shot reuse |

**Cost per video type** (model prices on Admin; shot cost measured on prod, the rest estimated):

| Type | Paid parts | About |
|---|---|---|
| Library shot | Veo 3.1 Fast clip + QA | $1.55 each (9 made, $13.98) |
| Fruit story from library shots | Script check, voice, word timings | $0.05–0.11 per video (prod videos 14, 15); rendering is free |
| Each extra variant (new hook) | Voice + word timings for the hook | about $0.01; a presenter hook adds its clip |
| Talking-head line (6 s) | OmniHuman $0.14/s, Kling Avatar $0.09/s, Wan S2V $0.10/s, Veo with sound $0.15/s | $0.55–0.90 per line |
| Presenter portrait | Nano Banana Pro | $0.18 each |

Talking-model prices are unverified until the first bill; correct them on Admin → Model prices.

**Render load on the single service.** Rendering runs in the worker process, one job at a time,
ffmpeg capped at 4 threads (`FFMPEG_THREADS`) and every text layer in one overlay input. On prod a
30 s story takes about 60–85 s per size, so a 12-version batch is about 15 minutes of worker time.
Web requests stay responsive because gunicorn runs in its own process; other jobs wait in the
queue meanwhile. Revisit (a second worker service, or a render queue elsewhere) if batches of more
than about 24 versions become routine or memory alerts appear during renders.

## Knowledge base (K1, 2026-10-20)

`knowledge/` holds the Meta Creative Playbook (`meta-creative-playbook.md`, exported from Claude Docs) and six
files derived from it: `styles.yaml`, `hooks.yaml`, `rules.yaml`, `compliance.yaml`, `variations.yaml`,
`metrics.yaml`. Every value group names the playbook section it comes from; `creative_intelligence/knowledge.py`
loads them, validates them, checks each source still exists, and gives a `version()` (`pb<revision>-<sha8>`) to
record on what the app makes. `/healthz` fails if they don't load or trace, so a bad change rolls back.
Prompt files reference knowledge by key (`{{knowledge:hooks.types}}`); `prompts.text()` fills them in and
`prompts.stamp()` adds `knowledge@<version>` to the `prompt_version` of everything made with them (K2a). Hooks
carry a playbook `hook_type` (the opening move) next to their Hook University archetype (the structure).
QA and export follow `rules.yaml` (K2b): Compose, the simple overlay, captions and end cards keep copy inside
the safe zones (`templates.safe_area()`; 9:16 clear of the top 14% and bottom 35%); rubric v4 adds measured
word count, safe zone and allowed-CTA criteria and raises the contrast floor to 4.5:1; finished videos are
encoded at 10 Mbps and checked against the export spec (`videotools.ensure_export`). `adstudio/prelaunch.py`
turns the playbook's pre-launch checklists into a per-ad list (Gallery flag, launch-kit manifest).
The Images (Compose layouts, grouped by playbook style) and Video style pickers are built from `styles.yaml`
(K2c): `styles.catalog()` is the live video styles, each with its `style-*` prompt reading its playbook entry;
planned styles show as coming soon. Fruit Story has a presenter-led / narrated switch (`mode`, kept in the
video's settings and in its Asset style tag, e.g. `fruit_story/narrated`).
Gallery cards, the asset panel and Results groups show the playbook's diagnosis hint (`adstudio/diagnosis.py`,
rules and levels in `metrics.yaml`) once an ad has 7 days and about 5,000 impressions (K2d). The read-only sync
asks Meta for 15-second video views (falling back without them); hold rate is 15-second ÷ 3-second views when
they exist, else ThruPlays ÷ 3-second views, and then hold hints are skipped for videos under 15 s.
**Admin → Knowledge (K3)** shows the playbook in force (revision, date), the knowledge version, every derived
value with its playbook section and what reads it (prompts found by scanning them, code from
`knowledge.CODE_USES`) and the checklists. Uploading a new Markdown export stores it (never live) with a
section diff; a worker job (`knowledge_update`) has Claude propose updates, one item each. Each item is approved
or rejected by hand: playbook and knowledge items go live together through an overlay next to the database
(`overlay.json`, adopted `playbook.md`), refused if they would break validation; rubric and prompt wording is
approved for the PR. "Download what's approved" gives the bundle that PR folds into the repo; Undo removes an
approved value.

## Styles and research (S1–S4, R, 2026-10-21)

- **Styles** live in `knowledge/styles.yaml`; the pickers, script rules and shot cards follow it. Video styles
  added: Us vs supermarket (split screen), How to eat it, Fast-cut montage, Listicle video (no Claude call),
  Claymation (stop-motion clay cards), Ripening time-lapse (facts-driven), Comment reply (a real comment as a
  sticker), Fruit characters (bible + approved look sheet, episodes end on the real fruit). Shot cards can be
  reserved for one style (`styles`, `cast`), skip QA criteria with a reason (`qa_skip`, `qa_note`) and carry facts
  into the still (`facts_category`).
- **Statics** added: notes list, text thread, post-it, handwritten letter (Caveat, OFL), real comments, review
  mashup, offer-first banner, listicle card. Quotes only come from the **real reviews library** (Admin), by id.
- **Real footage** uploads to the shot library tagged `real`, with consent, never mixed with AI.
- **Research** (Gallery › Research, `adstudio/research.py`): competitor ads from the Meta Ad Library API (read-only;
  EU/UK delivery only) or added by hand, tagged with our taxonomy; patterns and gaps; a weekly brief that suggests
  styles; the Page comments import (needs the Page token). Claude calls count against a $10/month research cap.
