# Ad Studio — runbook

Where things are and what to do when something goes wrong. Architecture: `docs/ARCHITECTURE.md`.

The app deploys from `main`: work goes through a branch and a pull request into `main`.

## Everyday

| Task | How |
|---|---|
| Deploy | Merge a PR into `main`. CI runs; Railway deploys after it passes; the deploy smoke checks the live app |
| See what's happening | Admin → Overview, Jobs, Storage and backups. Railway → your project → Logs (JSON lines; filter on `"level": "error"`) |
| Change a spending cap | Admin → Spending → Caps → Save (takes effect on the next paid call) |
| Check a model price | Admin → Spending → Model prices: correct it against the bill and Save (marks it checked) |
| Back up now | Admin → Storage and backups → Back up database now / new images now |
| Make static ads in bulk | Images → Compose → Bulk: pick approved images, ad types, hooks, ads per image and sizes, then Make. Runs in the background (Admin → Jobs: "Bulk static ads"); ads land in Gallery to review. Code-drawn ads are free; AI scenes are optional and priced on the button |
| Change a prompt | Copy `creative_intelligence/prompts/<name>.vN.txt` to `vN+1`, edit, PR |

## When something goes wrong

**A deploy failed or the smoke failed.** The deploy-smoke workflow rolls back to the last good
deployment and posts an alert (strip on every page; GitHub emails the failed run). Read the
workflow log (Actions → "Ad Studio deploy smoke") for which page or check failed; fix on a
branch; merge again. Needs the `RAILWAY_TOKEN` secret (a Railway *project* token) to roll back;
without it the workflow only alerts. Manual rollback: Railway → your project → Deployments → the
last good one → Redeploy.

**"Daily cap reached — resets at 00:00 the brand's timezone."** Nothing was spent by the blocked call. Raise the
cap on Admin if the spend is expected, or wait for midnight the brand's timezone.

**The worker is down** (Admin shows "Down", smoke fails on `worker`). honcho stops the whole service
if either process dies and Railway restarts it; if it keeps dying, read the `worker.1` lines in the
logs. Jobs wait in the queue meanwhile; interrupted image runs are closed as "Interrupted by an
app restart" and can simply be started again.

**A job failed** (Admin → Jobs). Read its error; Retry (not for image runs: start a new run).
Failed runs, syncs and backups also raise an alert.

**Meta sync failed.** Admin → Meta sync shows the step and message. Usual causes: expired
`META_ACCESS_TOKEN` (renew the system-user token in Meta Business settings, update the Railway
variable), or Meta rate limits (wait and press Sync). It is read-only; nothing in Meta changes.

**Backup failed.** Usually Drive: the account was disconnected (Gallery → Connect Google Drive), the
Backups folder was moved to the trash (`BACKUP_DRIVE_FOLDER_ID`), or the OAuth app is in "Testing"
mode (sign-ins expire after 7 days; set it to In production or Internal). Railway volume backups
are independent of Drive.

**Volume over 80%.** Grow it in Railway (service → Volume → size), or archive and delete old images.

## Video

| Task | How |
|---|---|
| Make shots for a product | Video → Shots: pick approved images, Make. Each passing shot joins the library |
| Make a video | Video → Create: pick a style, write the script (claims-checked), check the estimate, Start. Versions appear in Gallery (filter Video → style, length) |
| Approve a presenter face | Video → Create → Talking head: Make looks, approve one. Talking styles need an approved face |
| Add a music track | `scripts/add_music.py <file>` (Meta Sound Collection only; FB and IG ads only), PR |
| See video spend | Admin → Video (by style, per version, shot reuse) and Admin → Spending |

**A render failed.** Read the message on the video (Gallery or Admin → Jobs). "Error reinitializing
filters" or exit 137 means ffmpeg ran out of memory: check `FFMPEG_THREADS` is 4 or less, then
Retry. Shots and voice are kept, so a retry re-renders without paying again.

**A script is refused** ("isn't an approved fact"). The claims check found a claim that is not in
the product's facts. Edit the line, or add the fact on the product page. Origin claims (country,
region, farm location) are never allowed.

**A presenter clip looks wrong.** QA scores it against the approved face; reject it and make the
line again, or try another method (Veo, OmniHuman, Kling Avatar).

## Restore

Ask before each use; both replace live data.

- **Database from a Drive backup** (inside the container: `railway ssh`):
  `python -m creative_intelligence.backup restore <Drive file id>` (dry run: lists what would change),
  then the same with `--no-dry-run`, then restart the service.
- **Whole volume** (database and images): Railway → your project → Volume → Backups → Restore.
- **Seed v2 from a snapshot of another copy**: `scripts/restore_to_v2.sh` with `ALLOW_RESTORE=1`
  (dry run unless `--no-dry-run`), then set `ALLOW_RESTORE=0`.

## Secrets and settings (names only)

Railway (your project): `ADSTUDIO_PASSWORD`, `FLASK_SECRET_KEY`, `ADMIN_TOKEN`, `ANTHROPIC_API_KEY`,
`CI_REPLICATE_API_KEY`, `META_*`, `SHOPIFY_*`, `SCHEDULER_ENABLED`, `CI_RAW_DATA_DIR`,
`GOOGLE_OAUTH_CLIENT_ID/SECRET`, `ADSTUDIO_DRIVE_FOLDER_ID`, `BACKUP_DRIVE_FOLDER_ID`, optional
`SLACK_WEBHOOK_URL`. GitHub secrets: `RAILWAY_TOKEN`, `ADMIN_TOKEN`. Full list with defaults:
`docs/ad-studio.md` → Configuration.
