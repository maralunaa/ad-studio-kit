# CLAUDE.md — Instructions for Claude Code

## Project: Ad Studio kit

A brand's ad studio: store catalogue → product profiles → checked Meta ads (statics and videos) → results.
Brand specifics live in `brands/<BRAND>/` and are read only through `creative_intelligence/brand.py`.

## Rules

### General
- Read existing files before editing them; prefer editing to creating.
- Keep code minimal and focused. Do not add features that weren't requested.
- Never write a brand's name, products, people or accounts into core code, prompts or knowledge files: they go in
  the brand pack (brand.yaml, or a prompt / shot card / knowledge file inside `brands/<BRAND>/`).

### Paid calls and external APIs
- Every paid model call goes through a provider adapter (`creative_intelligence/providers/`) and is checked
  against a cap (`budget.py`) before it runs. Ask before running anything that spends real money.
- Meta access is read-only. Never call a write or mutate Meta API endpoint (create, update, delete campaigns,
  ad sets, ads or budgets) unless the user has explicitly confirmed it in this session.
- Log every API call with timestamp and endpoint to `outputs/api.log`. Raw API responses are saved timestamped
  and never overwritten.
- Never commit `.env` or any file containing credentials.

### Content
- Copy may only state approved facts; prices only come from the store; the image model never draws final text.
- AI presenters are brand narrators, flagged `ai_person`, never shown as customers or staff.
- Customer quotes come only from the reviews library, copied exactly.

### Prompts
- Prompts are versioned files (`creative_intelligence/prompts/<name>.vN.txt`): copy the newest to the next number
  and edit the copy. Use the `${brand_*}` placeholders instead of brand words.

### UI
- Build every screen from `creative_intelligence/webapp/static/studio.css` (tokens and components). No inline
  styles; the brand accent comes from `/brand.css`.

### Testing
- Tests go in `tests/`. Run them before calling a task done.
- Mock all external API calls; tests never hit a live API.
