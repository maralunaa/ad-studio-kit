"""
SQLite connection and schema management for the creative intelligence subsystem.
Completely isolated from the Meta ads ops reporting pipeline.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

BUSY_TIMEOUT_S = 30
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# DB lives inside the project tree; never inside data/raw or outputs.
_DEFAULT_DB_PATH = Path(__file__).parent.parent / "creative_intelligence_data" / "creative_intel.db"


def get_db_path() -> Path:
    """Return DB path, respecting CI_DB_PATH env override."""
    import os
    override = os.getenv("CI_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection.

    Enables WAL mode for better concurrent read performance and
    enforces foreign key constraints.
    """
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")  # wait for another writer instead of failing
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Bring the database up to the latest schema. Cheap once a path is up to date in this process."""
    path = str(db_path or get_db_path())
    if path in _UP_TO_DATE and Path(path).exists():
        return
    conn = get_connection(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()
    _UP_TO_DATE.add(path)


_UP_TO_DATE: set[str] = set()


# ── numbered migrations ──────────────────────────────────────────────────────
# 1 is the baseline: schema.sql plus the old additive list below (frozen; don't add to it).
# Every later change is a file migrations/NNNN_name.sql, applied once, in order, and recorded
# in schema_version. Write them so a re-run is harmless (IF NOT EXISTS), since two processes
# can start at the same moment.
def _baseline(conn: sqlite3.Connection) -> None:
    conn.executescript((Path(__file__).parent / "models" / "schema.sql").read_text())
    _migrate(conn)
    conn.commit()


def migrations() -> list[tuple[int, str, Any]]:
    out: list[tuple[int, str, Any]] = [(1, "baseline", _baseline)]
    for f in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        out.append((int(f.name[:4]), f.stem[5:], lambda c, f=f: _apply_sql(c, f.read_text())))
    versions = [v for v, _, _ in out]
    if len(set(versions)) != len(versions) or versions != sorted(versions):
        raise RuntimeError(f"migration numbers must be unique and ascending: {versions}")
    return out


def _apply_sql(conn: sqlite3.Connection, script: str) -> None:
    """Run a migration file statement by statement. Adding a column that is already there counts
    as done, so a migration re-run (two processes, a restored older database) is harmless."""
    stmt = ""
    for line in script.splitlines(keepends=True):
        stmt += line
        if sqlite3.complete_statement(stmt):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
            stmt = ""
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded. Returns the versions applied now.
    A file lock next to the database makes processes that start together (gunicorn master and
    worker) take turns; the second finds the work done."""
    import fcntl
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_file:                                  # in-memory database: nobody to race
        return _migrate_locked(conn)
    with open(str(db_file) + ".migrate.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _migrate_locked(conn)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _migrate_locked(conn: sqlite3.Connection) -> list[int]:
    from datetime import datetime
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY,"
                     " name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    done = []
    for version, name, apply in migrations():
        if version in applied:
            continue
        apply(conn)
        with conn:
            conn.execute("INSERT OR IGNORE INTO schema_version (version, name, applied_at) VALUES (?,?,?)",
                         (version, name, datetime.utcnow().isoformat(timespec="seconds")))
        done.append(version)
    return done


def _migrate(conn: sqlite3.Connection) -> None:
    """Part of the baseline: the additive changes made before numbered migrations. Frozen."""
    migrations = [
        "ALTER TABLE products ADD COLUMN tags TEXT",
        # render_assets: decision/review columns added in v1.4.1
        "ALTER TABLE render_assets ADD COLUMN is_favorite INTEGER DEFAULT 0",
        "ALTER TABLE render_assets ADD COLUMN is_ready_to_test INTEGER DEFAULT 0",
        "ALTER TABLE render_assets ADD COLUMN review_notes TEXT DEFAULT ''",
        "ALTER TABLE render_assets ADD COLUMN reviewed_at TEXT",
        # render_outputs: store generation error for visibility without log access
        "ALTER TABLE render_outputs ADD COLUMN error_message TEXT",
        # ── video module (v1.5) ──────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS video_storyboards (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id                  TEXT,
            concept_text                TEXT NOT NULL,
            scenes_json                 TEXT,
            ugc_script_json             TEXT,
            total_duration_seconds      INTEGER DEFAULT 30,
            source_production_output_id INTEGER,
            source_output_type          TEXT DEFAULT 'concept',
            created_at                  TEXT DEFAULT (datetime('now'))
        )""",
        # Additive columns for storyboards created before lineage tracking
        "ALTER TABLE video_storyboards ADD COLUMN source_production_output_id INTEGER",
        "ALTER TABLE video_storyboards ADD COLUMN source_output_type TEXT DEFAULT 'concept'",
        """CREATE TABLE IF NOT EXISTS video_scenes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            storyboard_id       INTEGER NOT NULL
                                    REFERENCES video_storyboards(id) ON DELETE CASCADE,
            scene_index         INTEGER NOT NULL,
            purpose             TEXT,
            visual_description  TEXT,
            text_overlay        TEXT DEFAULT '',
            duration_seconds    REAL  DEFAULT 5,
            camera_type         TEXT DEFAULT 'handheld',
            framing             TEXT DEFAULT 'medium',
            movement            TEXT DEFAULT 'none',
            product_focus       TEXT DEFAULT '',
            lighting_style      TEXT DEFAULT '',
            render_asset_id     INTEGER
        )""",
        "ALTER TABLE video_storyboards ADD COLUMN is_favorite INTEGER DEFAULT 0",
        "ALTER TABLE video_storyboards ADD COLUMN is_approved INTEGER DEFAULT 0",
        "ALTER TABLE video_storyboards ADD COLUMN review_notes TEXT DEFAULT ''",
        # ── video types + assembly (v1.6) ─────────────────────────────
        "ALTER TABLE video_storyboards ADD COLUMN video_type TEXT DEFAULT 'ugc'",
        "ALTER TABLE video_scenes ADD COLUMN video_type TEXT DEFAULT 'ugc'",
        """CREATE TABLE IF NOT EXISTS video_outputs (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            storyboard_id               INTEGER NOT NULL,
            video_type                  TEXT NOT NULL DEFAULT 'ugc',
            source_output_type          TEXT DEFAULT 'concept',
            source_production_output_id INTEGER,
            output_path                 TEXT,
            metadata_json               TEXT DEFAULT '{}',
            status                      TEXT NOT NULL DEFAULT 'pending',
            is_approved                 INTEGER DEFAULT 0,
            is_favorite                 INTEGER DEFAULT 0,
            is_ready_to_test            INTEGER DEFAULT 0,
            created_at                  TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE INDEX IF NOT EXISTS idx_video_outputs_storyboard
           ON video_outputs(storyboard_id)""",
        # ── scene-level clip generation (v1.7) ──────────────────────────
        """CREATE TABLE IF NOT EXISTS video_scene_clips (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            storyboard_id       INTEGER NOT NULL,
            scene_id            INTEGER NOT NULL,
            scene_index         INTEGER NOT NULL,
            provider            TEXT NOT NULL DEFAULT 'replicate',
            model               TEXT,
            prompt              TEXT,
            clip_local_path     TEXT,
            clip_url            TEXT,
            duration_seconds    REAL,
            aspect_ratio        TEXT DEFAULT '9:16',
            status              TEXT NOT NULL DEFAULT 'pending',
            error_message       TEXT,
            metadata_json       TEXT DEFAULT '{}',
            created_at          TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE INDEX IF NOT EXISTS idx_video_scene_clips_storyboard
           ON video_scene_clips(storyboard_id)""",
        """CREATE INDEX IF NOT EXISTS idx_video_scene_clips_scene
           ON video_scene_clips(scene_id)""",
        # Add prediction_id for async Replicate predictions (survives container restarts)
        "ALTER TABLE video_scene_clips ADD COLUMN prediction_id TEXT",
        # Add product image URL — used as first-frame reference for image-to-video clip generation
        "ALTER TABLE products ADD COLUMN image_url TEXT",
        # ── Hook D+C+PS framework (v1.6) ─────────────────────────────────
        # Store archetype label + D/C/PS breakdown + clarity note per generated hook
        "ALTER TABLE generated_hooks ADD COLUMN archetype TEXT",
        "ALTER TABLE generated_hooks ADD COLUMN dcp_d TEXT",
        "ALTER TABLE generated_hooks ADD COLUMN dcp_c TEXT",
        "ALTER TABLE generated_hooks ADD COLUMN dcp_ps TEXT",
        "ALTER TABLE generated_hooks ADD COLUMN dcp_clarity TEXT",
        # ── Creative scoring + learning system (v1.8) ────────────────────────
        "ALTER TABLE render_assets ADD COLUMN quality_score REAL",
        "ALTER TABLE render_assets ADD COLUMN score_breakdown_json TEXT",
        """CREATE TABLE IF NOT EXISTS asset_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        INTEGER NOT NULL,
    asset_type      TEXT NOT NULL DEFAULT 'render',
    feedback_type   TEXT NOT NULL,
    rejection_tags  TEXT DEFAULT '[]',
    note            TEXT DEFAULT '',
    product_id      TEXT,
    variant_label   TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_asset_feedback_asset
   ON asset_feedback(asset_id)""",
        """CREATE INDEX IF NOT EXISTS idx_asset_feedback_product
   ON asset_feedback(product_id)""",
        """CREATE TABLE IF NOT EXISTS creative_learnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT,
    learning_type   TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'feedback',
    summary         TEXT NOT NULL,
    detail_json     TEXT DEFAULT '{}',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_creative_learnings_product
   ON creative_learnings(product_id)""",
        # candle-label studio — isolated from the ads render pipeline.
        """CREATE TABLE IF NOT EXISTS label_renders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL,
    recipe          TEXT NOT NULL,
    code_s          TEXT NOT NULL,
    code_i          TEXT NOT NULL,
    code_e          TEXT NOT NULL,
    code_h          TEXT NOT NULL,
    code_p          TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    negative_prompt TEXT NOT NULL,
    image_path      TEXT,
    provider        TEXT,
    model           TEXT,
    status          TEXT NOT NULL DEFAULT 'spec_only',
    error_message   TEXT,
    is_favorite     INTEGER NOT NULL DEFAULT 0,
    notes           TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_label_renders_sku
   ON label_renders(sku, created_at DESC)""",
        # ── Ad Studio — reference-photo product ads (isolated) ──────────────
        """CREATE TABLE IF NOT EXISTS adstudio_products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    scent_notes   TEXT DEFAULT '',
    physical_desc TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        # add notes to existing DBs (no-op / silently ignored where it exists)
        "ALTER TABLE adstudio_products ADD COLUMN notes TEXT DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS adstudio_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    path        TEXT NOT NULL,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_photos_product
   ON adstudio_photos(product_id)""",
        """CREATE TABLE IF NOT EXISTS adstudio_hooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    hook_text   TEXT NOT NULL DEFAULT '',
    archetype   TEXT DEFAULT '',
    d           TEXT DEFAULT '',
    c           TEXT DEFAULT '',
    ps          TEXT DEFAULT '',
    headline    TEXT DEFAULT '',
    subhead     TEXT DEFAULT '',
    body        TEXT DEFAULT '',
    cta         TEXT DEFAULT '',
    source      TEXT DEFAULT 'manual',
    created_at  TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_hooks_product
   ON adstudio_hooks(product_id)""",
        """CREATE TABLE IF NOT EXISTS adstudio_ads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    hook_id       INTEGER,
    format_key    TEXT NOT NULL,
    aspect_ratio  TEXT NOT NULL DEFAULT '4:5',
    prompt        TEXT NOT NULL DEFAULT '',
    model         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    image_path    TEXT,
    error_message TEXT,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_ads_product
   ON adstudio_ads(product_id, created_at DESC)""",
        # Ad Studio v2 — text-free image library (diversity seeds) + composites.
        """CREATE TABLE IF NOT EXISTS adstudio_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    archetype     TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    concept       TEXT NOT NULL DEFAULT '',
    prompt        TEXT NOT NULL DEFAULT '',
    aspect_ratio  TEXT NOT NULL DEFAULT '4:5',
    model         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    image_path    TEXT,
    error_message TEXT,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_images_product
   ON adstudio_images(product_id, created_at DESC)""",
        """CREATE TABLE IF NOT EXISTS adstudio_composites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    image_id     INTEGER NOT NULL,
    hook_id      INTEGER,
    headline     TEXT DEFAULT '',
    subhead      TEXT DEFAULT '',
    cta          TEXT DEFAULT '',
    layout_json  TEXT DEFAULT '{}',
    output_path  TEXT,
    is_favorite  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_composites_product
   ON adstudio_composites(product_id, created_at DESC)""",
        # after the CREATEs above, so fresh DBs get the column too
        "ALTER TABLE adstudio_images ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE adstudio_composites ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
        # Core photos (core1-3) + box-size infographics (PDP / Build-a-Box).
        "ALTER TABLE adstudio_products ADD COLUMN varieties TEXT DEFAULT ''",
        "ALTER TABLE adstudio_products ADD COLUMN shopify_url TEXT DEFAULT ''",
        "ALTER TABLE adstudio_products ADD COLUMN info_json TEXT DEFAULT '{}'",
        """CREATE TABLE IF NOT EXISTS adstudio_core (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    variant       INTEGER NOT NULL DEFAULT 0,
    prompt        TEXT NOT NULL DEFAULT '',
    aspect_ratio  TEXT NOT NULL DEFAULT '1:1',
    status        TEXT NOT NULL DEFAULT 'queued',
    image_path    TEXT,
    error_message TEXT,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_core_product
   ON adstudio_core(product_id, kind, created_at DESC)""",
        # photo role: core1|core2|core3|infographic|other ('' = untagged)
        "ALTER TABLE adstudio_photos ADD COLUMN role TEXT DEFAULT ''",
        # Claude's per-shot brief + operator fix note, folded into the prompt at run time
        "ALTER TABLE adstudio_core ADD COLUMN brief TEXT DEFAULT ''",
        "ALTER TABLE adstudio_core ADD COLUMN fix TEXT DEFAULT ''",
        # product type drives the workflow: fruit | candle | other
        "ALTER TABLE adstudio_products ADD COLUMN product_type TEXT DEFAULT ''",
        """UPDATE adstudio_products SET product_type =
   CASE WHEN lower(name) LIKE '%candle%' THEN 'candle' ELSE 'fruit' END
   WHERE product_type IS NULL OR product_type = ''""",
        # which winning pattern (creative_patterns.id) a hook was written from
        "ALTER TABLE adstudio_hooks ADD COLUMN pattern_id INTEGER",
        # AI tagging cache: one Claude classification per distinct ad copy
        """CREATE TABLE IF NOT EXISTS ai_tag_cache (
    key         TEXT PRIMARY KEY,
    tags_json   TEXT NOT NULL,
    model       TEXT,
    taxonomy    TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
)""",
        # spend-weighted winning patterns (analysis.patterns.extract_weighted_patterns)
        "ALTER TABLE creative_patterns ADD COLUMN total_spend REAL",
        "ALTER TABLE creative_patterns ADD COLUMN total_purchases REAL",
        "ALTER TABLE creative_patterns ADD COLUMN total_revenue REAL",
        "ALTER TABLE creative_patterns ADD COLUMN roas_lift REAL",
        "ALTER TABLE creative_patterns ADD COLUMN top_ad_share REAL",
        "ALTER TABLE creative_patterns ADD COLUMN date_range TEXT",
        # which weighted-extraction run wrote the row; only the newest run is "current"
        "ALTER TABLE creative_patterns ADD COLUMN extract_run TEXT",
        # Gallery review: approve / reject / ready-to-launch + notes (as in Render)
        "ALTER TABLE adstudio_composites ADD COLUMN review_status TEXT DEFAULT ''",
        "ALTER TABLE adstudio_composites ADD COLUMN is_ready INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE adstudio_composites ADD COLUMN review_notes TEXT DEFAULT ''",
        "ALTER TABLE adstudio_composites ADD COLUMN reviewed_at TEXT",
        # a Core shot used as a Compose base gets a mirror adstudio_images row
        "ALTER TABLE adstudio_images ADD COLUMN core_id INTEGER",
        # Meta → patterns refresh runs (read-only pull; see ingest/meta_sync.py)
        """CREATE TABLE IF NOT EXISTS meta_sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    step         TEXT DEFAULT '',
    message      TEXT DEFAULT '',
    date_range   TEXT DEFAULT '30d',
    n_ads        INTEGER,
    spend        REAL,
    n_patterns   INTEGER
)""",
        # AI Ads — finished ads with the copy drawn in by the image model.
        # A brief (Claude-drafted from winning patterns, operator-edited) feeds
        # the 12 fixed variants, then Claude-invented scenes with their own copy.
        """CREATE TABLE IF NOT EXISTS adstudio_briefs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    pattern_id    INTEGER,
    pattern_ids   TEXT DEFAULT '[]',
    idea          TEXT DEFAULT '',
    review_text   TEXT DEFAULT '',
    review_author TEXT DEFAULT '',
    brief_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_briefs_product
   ON adstudio_briefs(product_id, created_at DESC)""",
        """CREATE TABLE IF NOT EXISTS adstudio_textads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    brief_id      INTEGER NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'variant',
    variant       TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    concept       TEXT DEFAULT '',
    text_layout   TEXT DEFAULT '',
    headline      TEXT DEFAULT '',
    subhead       TEXT DEFAULT '',
    cta           TEXT DEFAULT '',
    body          TEXT DEFAULT '',
    style_seed    INTEGER NOT NULL DEFAULT 0,
    fix           TEXT DEFAULT '',
    prompt        TEXT DEFAULT '',
    aspect_ratio  TEXT NOT NULL DEFAULT '9:16',
    model         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    image_path    TEXT,
    error_message TEXT,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    review_status TEXT DEFAULT '',
    is_ready      INTEGER NOT NULL DEFAULT 0,
    text_checked  INTEGER NOT NULL DEFAULT 0,
    review_notes  TEXT DEFAULT '',
    reviewed_at   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_textads_product
   ON adstudio_textads(product_id, brief_id, created_at DESC)""",
        # Google Drive exports for outside agencies: one folder per export,
        # one item row per ad uploaded (an ad can be exported more than once).
        """CREATE TABLE IF NOT EXISTS adstudio_drive_auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    email         TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    connected_at  TEXT DEFAULT (datetime('now'))
)""",
        "ALTER TABLE adstudio_products ADD COLUMN drive_folder_id TEXT",
        """CREATE TABLE IF NOT EXISTS adstudio_drive_exports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    folder_id    TEXT NOT NULL,
    folder_name  TEXT NOT NULL,
    folder_url   TEXT NOT NULL,
    n_ads        INTEGER NOT NULL DEFAULT 0,
    ad_ids       TEXT NOT NULL DEFAULT '[]',
    sheet_url    TEXT,
    status       TEXT NOT NULL DEFAULT 'uploading',
    created_at   TEXT DEFAULT (datetime('now')),
    finished_at  TEXT
)""",
        """CREATE TABLE IF NOT EXISTS adstudio_drive_export_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    export_id    INTEGER NOT NULL,
    textad_id    INTEGER NOT NULL,
    file_id      TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    file_url     TEXT,
    uploaded_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (export_id, textad_id)
)""",
        """CREATE INDEX IF NOT EXISTS idx_adstudio_drive_items_ad
   ON adstudio_drive_export_items(textad_id)""",
        # Ad Studio v2 T1 — per-ad daily Meta results (see ingest/daily_insights.py)
        # and how each sync was started (manual button / scheduled / cron endpoint).
        """CREATE TABLE IF NOT EXISTS ad_daily_insights (
    ad_id          TEXT NOT NULL,
    date           TEXT NOT NULL,
    campaign_id    TEXT,
    adset_id       TEXT,
    ad_name        TEXT,
    spend          REAL,
    impressions    INTEGER,
    clicks         INTEGER,
    ctr            REAL,
    purchases      INTEGER,
    revenue        REAL,
    cpa            REAL,
    roas           REAL,
    video_3s_views INTEGER,
    thruplays      INTEGER,
    video_p25      INTEGER,
    video_p50      INTEGER,
    video_p75      INTEGER,
    video_p100     INTEGER,
    updated_at     TEXT,
    PRIMARY KEY (ad_id, date)
)""",
        """CREATE INDEX IF NOT EXISTS idx_ad_daily_insights_date
   ON ad_daily_insights(date)""",
        "ALTER TABLE meta_sync_runs ADD COLUMN trigger TEXT DEFAULT 'manual'",
        # Ad Studio v2 T2 — asset registry (adstudio/assets.py) and generation cost log (adstudio/costs.py)
        """CREATE TABLE IF NOT EXISTS adstudio_assets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       TEXT NOT NULL UNIQUE,
    concept_id     TEXT NOT NULL,
    product_id     INTEGER NOT NULL,
    source         TEXT NOT NULL,
    source_id      INTEGER NOT NULL,
    angle          TEXT DEFAULT '',
    hook_type      TEXT DEFAULT '',
    format         TEXT DEFAULT '',
    style          TEXT DEFAULT '',
    product        TEXT DEFAULT '',
    product_type   TEXT DEFAULT '',
    source_tab     TEXT DEFAULT '',
    image_model    TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    run_id         TEXT,
    cost_usd       REAL DEFAULT 0,
    ai_generated   INTEGER NOT NULL DEFAULT 1,
    meta_ad_id     TEXT,
    linked_by      TEXT,
    linked_at      TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    UNIQUE (source, source_id)
)""",
        "CREATE INDEX IF NOT EXISTS idx_adstudio_assets_product ON adstudio_assets(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_adstudio_assets_meta ON adstudio_assets(meta_ad_id)",
        """CREATE TABLE IF NOT EXISTS model_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    model      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    units      REAL NOT NULL DEFAULT 1,
    cost_usd   REAL NOT NULL DEFAULT 0,
    source     TEXT,
    source_id  INTEGER,
    run_id     TEXT
)""",
        "CREATE INDEX IF NOT EXISTS idx_model_calls_source ON model_calls(source, source_id)",
        "CREATE INDEX IF NOT EXISTS idx_model_calls_at ON model_calls(at)",
        # T3 — text check on AI Ads images (adstudio/textcheck.py)
        "ALTER TABLE adstudio_textads ADD COLUMN textcheck_status TEXT DEFAULT ''",
        "ALTER TABLE adstudio_textads ADD COLUMN textcheck_json TEXT DEFAULT ''",
        "ALTER TABLE adstudio_textads ADD COLUMN textcheck_at TEXT",
        "ALTER TABLE adstudio_textads ADD COLUMN base_path TEXT",
        # T4 — quality scores (adstudio/qa.py), review log for overrules, review state on images
        """CREATE TABLE IF NOT EXISTS qa_scores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    source_id      INTEGER NOT NULL,
    rubric_version INTEGER,
    scores_json    TEXT DEFAULT '{}',
    total          REAL,
    passed         INTEGER NOT NULL DEFAULT 0,
    failures_json  TEXT DEFAULT '[]',
    reasoning      TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    model          TEXT DEFAULT '',
    cost_usd       REAL DEFAULT 0,
    at             TEXT,
    UNIQUE (source, source_id)
)""",
        """CREATE TABLE IF NOT EXISTS qa_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    source_id  INTEGER NOT NULL,
    action     TEXT NOT NULL,
    qa_total   REAL,
    qa_passed  INTEGER,
    overrule   TEXT,
    at         TEXT
)""",
        "ALTER TABLE adstudio_images ADD COLUMN review_status TEXT DEFAULT ''",
        # T5 — one-pass runs (adstudio/runs.py) and fan-out sizes
        """CREATE TABLE IF NOT EXISTS adstudio_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    size         INTEGER NOT NULL,
    aspect_ratio TEXT NOT NULL DEFAULT '4:5',
    status       TEXT NOT NULL DEFAULT 'queued',
    budget_usd   REAL,
    spent_usd    REAL DEFAULT 0,
    delivered    INTEGER DEFAULT 0,
    plan_json    TEXT,
    message      TEXT DEFAULT '',
    created_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT
)""",
        """CREATE TABLE IF NOT EXISTS adstudio_run_slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    plan_json  TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'queued',
    attempts   INTEGER NOT NULL DEFAULT 0,
    image_id   INTEGER,
    qa_total   REAL,
    last_error TEXT DEFAULT ''
)""",
        "CREATE INDEX IF NOT EXISTS idx_run_slots_run ON adstudio_run_slots(run_id)",
        "ALTER TABLE adstudio_images ADD COLUMN run_id INTEGER",
        "ALTER TABLE adstudio_images ADD COLUMN parent_image_id INTEGER",
        # T6 — facts and claims library (adstudio/facts.py)
        """CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER,
    category    TEXT NOT NULL DEFAULT 'other',
    text        TEXT NOT NULL,
    source      TEXT DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS banned_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER,
    term        TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
)""",
        """CREATE TABLE IF NOT EXISTS copy_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    source_id   INTEGER NOT NULL,
    status      TEXT NOT NULL,
    result_json TEXT DEFAULT '{}',
    at          TEXT,
    UNIQUE (source, source_id)
)""",
        """CREATE TABLE IF NOT EXISTS copy_check_cache (
    key         TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    at          TEXT
)""",
        """CREATE TABLE IF NOT EXISTS model_prices (
    model      TEXT PRIMARY KEY,
    unit       TEXT NOT NULL,
    usd        REAL NOT NULL,
    source     TEXT DEFAULT '',
    checked_at TEXT
)""",
    ]
    for stmt in migrations:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # Column/table already exists


def db_exists(db_path: Path | None = None) -> bool:
    path = db_path or get_db_path()
    return path.exists()
