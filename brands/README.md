# Brand packs

Everything that makes the app one brand's ad studio lives in one folder here. The engine (jobs, caps, image runs,
quality scoring, Compose, the video timeline, Gallery, the Meta results loop) reads the pack and never names a brand.

```
brands/<name>/
  brand.yaml          required: name, store, naming, colours, fonts, rules, voices, presenters, reviews, competitors
  logo.png            the mark in the top bar, login page and end cards (square, transparent background)
  prompts/            optional: <prompt-name>.vN.txt replaces the core prompt of that name
  shots/              optional: <shot-type>.vN.yaml adds or replaces a shot recipe card
  knowledge/          optional: your own playbook files (else knowledge/ at the repo root)
  qa_rubric.yaml      optional: your own quality bar (else config/qa_rubric.yaml)
```

Pick a pack with `BRAND=<name>` (or `BRAND_DIR=/any/path`). Code reads it only through
`creative_intelligence/brand.py`.

## Start a new brand

1. `cp -r brands/sample brands/acme` and set `BRAND=acme` in `.env`.
2. Edit `brands/acme/brand.yaml`: name, short name (the prefix on every Meta ad name), website, one-line
   description, what the products ship in, timezone, colours, store feed URL and its tags, product type mapping.
3. Replace `logo.png`.
4. Pick voices and presenters (`voices`, `presenters`); add review sources and competitors to watch.
5. Read the core prompts that matter most for your category and copy any you want to change into
   `brands/acme/prompts/` (same file name, any version number). Every prompt can use `${brand}`,
   `${brand_short}`, `${brand_site}`, `${brand_category}`, `${brand_description}`, `${brand_about}` and
   `${brand_packaging}`.
6. If your products aren't food, write shot cards for your own shot types in `brands/acme/shots/` and set the
   quality bar in `brands/acme/qa_rubric.yaml`.
7. Run the tests, then one small real image run and one Animate with the caps set low.

## Product types

The core ships four product types (`creative_intelligence/adstudio/profiles.py`): **fruit** (the most developed:
cut photos, fruit-count checks in video, a food image menu), **candle**, **gift box** and **other** (a generic
profile for anything). A new category, say skincare, works on `other` from day one; add a profile in
`profiles.py` with its image menu, reference lock and never-include rules when you want it tuned.

## What stays in the core

House rules that protect any brand are in the core and should stay there: copy may only state approved facts;
prices only come from the store; the image model never draws final text; AI presenters are flagged and never posed
as customers or staff; customer quotes are only real ones, copied exactly; Meta access is read-only.
