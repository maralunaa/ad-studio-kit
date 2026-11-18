# Ad Studio kit

An ad studio for one brand's products: it turns a store catalogue into finished, checked Meta ads (static ads and
short videos) and links each ad to its own Meta results, so the next batch builds on what worked. Everything
brand-specific lives in a **brand pack** (`brands/<name>/`); this repository ships a neutral sample brand.

**Flow:** product → profile (look, sourced facts, reference photos, hooks) → images (planned, generated, scored,
approved) → Compose (text drawn by code) and video (shot library, voices, presenters, timeline) → Gallery and
launch kit (each file named by its Asset ID, the Meta ad name) → results from a daily read-only Meta sync.

**Built in:** spending caps checked before every paid call, a cost log, only approved facts in copy, prices only
from the store, no AI-drawn text, read-only Meta access, real customer quotes only, versioned prompt files.

## Run it locally

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY and CI_REPLICATE_API_KEY at least
pip install -r requirements.txt   # Python 3.11; ffmpeg for video
PYTHONPATH=. python creative_intelligence/webapp/app.py      # http://localhost:5555/products
python -m pytest tests -q         # all live API calls are blocked in tests
```

The local app runs the web and a background worker together. With `BRAND=sample` it shows the sample brand; point
`store.public_url` in the pack (or `SHOPIFY_PUBLIC_URL`) at a Shopify store to load real products.

## Make it yours

Copy `brands/sample/` to `brands/<your-brand>/`, edit `brand.yaml`, replace the logo, and set `BRAND`. What else
to change for a new category (prompts, shot cards, quality rubric, product types) is in
[brands/README.md](brands/README.md).

## Docs

| File | What's in it |
|---|---|
| [brands/README.md](brands/README.md) | The brand pack and how to start a new brand |
| [docs/ad-studio.md](docs/ad-studio.md) | Every feature, configuration variable, cap and data rule |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the parts fit and what is deferred |
| [docs/runbook.md](docs/runbook.md) | Everyday tasks and what to do when something breaks |
| [THIRD_PARTY.md](THIRD_PARTY.md) | Open-source material adapted here, with licences |

## Deploying

One service, two processes (`Procfile`: gunicorn web + a worker), one SQLite database and a file volume. The repo
is set up for Railway (`railway.toml`, `nixpacks.toml`) with a GitHub workflow that checks each deploy and rolls
back on failure; any host that runs a Procfile and gives a persistent volume works.

Music tracks and fonts: put licensed tracks in `music/` (none are shipped); fonts in
`creative_intelligence/adstudio/fonts/` are under the SIL Open Font Licence.
