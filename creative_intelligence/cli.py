"""
Creative Intelligence CLI.

Entry point for all creative intelligence operations.
All commands are safe by default (read-only or dry-run).

Usage:
  python -m creative_intelligence.cli <command> [options]

Commands:
  init                  Initialise the database
  ingest                Ingest creatives + performance from raw data files
  tag                   Tag all un-tagged creatives (rule-based)
  tag --ai              Also AI-tag (Claude, fruit taxonomy) ads that spent in the window
  extract-patterns      Extract winning creative patterns from tagged data
  status                Show system status

These are the steps the Meta sync (ingest/meta_sync.py) runs.

Run any command with --help for details.
"""
from __future__ import annotations

import argparse
import sys


def _init(args: argparse.Namespace) -> None:
    from creative_intelligence.db import init_db, get_db_path
    init_db()
    print(f"Database initialised at: {get_db_path()}")


def _ingest(args: argparse.Namespace) -> None:
    from creative_intelligence.db import init_db, get_connection
    from creative_intelligence.ingest.ads_reader import build_creative_records
    from creative_intelligence.ingest.insights_reader import parse_insights
    from creative_intelligence.ingest.creative_fetcher import enrich_records_with_copy
    from datetime import datetime

    init_db()
    conn = get_connection()

    print("Loading creative records from data/raw/...")
    records = build_creative_records()
    print(f"  Found {len(records)} ads")

    if not args.dry_run and not args.skip_copy_fetch:
        print("  Fetching creative copy from Meta API...")
        records = enrich_records_with_copy(records, dry_run=False)
    else:
        print("  Skipping copy fetch (dry-run or --skip-copy-fetch)")

    now = datetime.utcnow().isoformat()
    inserted = updated = 0
    with conn:
        for rec in records:
            existing = conn.execute(
                "SELECT id FROM creatives WHERE id = ?", (rec["creative_id"],)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE creatives SET ad_name=?, campaign_name=?, status=?,
                       format=?, hook_text=?, primary_text=?, headline=?,
                       description=?, cta=?, destination_url=?, last_seen_date=?, updated_at=?
                       WHERE id=?""",
                    (rec["ad_name"], rec["campaign_name"], rec["status"],
                     rec["format"], rec["hook_text"], rec["primary_text"],
                     rec["headline"], rec["description"], rec["cta"],
                     rec["destination_url"], now[:10], now, rec["creative_id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO creatives
                       (id, ad_id, ad_name, adset_id, adset_name, campaign_id, campaign_name,
                        status, format, hook_text, primary_text, headline, description,
                        cta, destination_url, first_seen_date, last_seen_date, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rec["creative_id"], rec["ad_id"], rec["ad_name"],
                     rec["adset_id"], rec["adset_name"], rec["campaign_id"],
                     rec["campaign_name"], rec["status"], rec["format"],
                     rec["hook_text"], rec["primary_text"], rec["headline"],
                     rec["description"], rec["cta"], rec["destination_url"],
                     now[:10], now[:10], now, now),
                )
                inserted += 1
    print(f"  Creatives: {inserted} inserted, {updated} updated")

    # Ingest performance data.
    # Build ad_id → creative_id lookup from the creatives table.
    ad_to_creative = {
        r["ad_id"]: r["id"]
        for r in conn.execute("SELECT id, ad_id FROM creatives").fetchall()
        if r["ad_id"]
    }

    from creative_intelligence.ingest.insights_reader import _PREFIX_MAP, _latest_file
    ranges = ([r for r in _PREFIX_MAP if r in ("yesterday", "7d", "30d") or _latest_file(_PREFIX_MAP[r])]
              if not args.date_range else [args.date_range])
    for dr in ranges:
        print(f"  Loading {dr} performance data...")
        perf_rows = parse_insights(dr)
        perf_inserted = perf_skipped = perf_errors = 0
        with conn:
            for p in perf_rows:
                # Resolve actual creative_id from the ad_id.
                ad_id = p["ad_id"]
                creative_id = ad_to_creative.get(ad_id)
                if not creative_id:
                    perf_skipped += 1
                    continue
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO creative_performance
                           (creative_id, ad_id, date_range, snapshot_date,
                            spend, impressions, clicks, ctr, cpc, cpm, frequency,
                            purchases, revenue, roas, cpa,
                            video_views, video_view_rate)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (creative_id, ad_id, p["date_range"],
                         p["snapshot_date"], p.get("spend"), p.get("impressions"),
                         p.get("clicks"), p.get("ctr"), p.get("cpc"), p.get("cpm"),
                         p.get("frequency"), p.get("purchases"), p.get("revenue"),
                         p.get("roas"), p.get("cpa"),
                         p.get("video_views"), p.get("video_view_rate")),
                    )
                    perf_inserted += 1
                except Exception as e:
                    perf_errors += 1
                    if perf_errors <= 3:
                        print(f"    [WARN] perf insert error for ad {ad_id}: {e}")
        msg = f"    {dr}: {perf_inserted} rows inserted"
        if perf_skipped:
            msg += f" | {perf_skipped} skipped (ad not in creatives table)"
        if perf_errors:
            msg += f" | {perf_errors} errors"
        print(msg)

    conn.close()
    print("Ingest complete.")


def _tag(args: argparse.Namespace) -> None:
    from creative_intelligence.db import get_connection
    from creative_intelligence.tagging.rule_tagger import tag_all
    from creative_intelligence import config

    conn = get_connection()
    # legacy rows whose id was lost can't hold tags (creative_tags.creative_id is a FK)
    records = [dict(r) for r in conn.execute("SELECT * FROM creatives WHERE id IS NOT NULL").fetchall()]
    print(f"Tagging {len(records)} creatives (rule-based)...")

    tag_rows = tag_all(records)
    inserted = 0
    with conn:
        for t in tag_rows:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO creative_tags
                       (creative_id, tag_type, tag_value, confidence, source)
                       VALUES (?,?,?,?,?)""",
                    (t["creative_id"], t["tag_type"], t["tag_value"],
                     t["confidence"], t["source"]),
                )
                inserted += 1
            except Exception as e:
                if inserted == 0 and len(tag_rows) > 0:
                    print(f"  [WARN] First tag insert error: {e}")
                    print(f"         creative_id={t['creative_id']!r}")
    print(f"  {inserted} tags written")

    if args.ai:
        # Claude re-tags ads that spent in the window (fruit taxonomy; cached per copy)
        from creative_intelligence.tagging.ai_tagger import tag_with_ai
        rows = [dict(r) for r in conn.execute(
            "SELECT DISTINCT c.id, c.ad_name, c.headline, c.primary_text FROM creatives c"
            " JOIN creative_performance p ON p.creative_id = c.id"
            " WHERE p.date_range = ? AND p.spend > 0", (args.ai_window,)).fetchall()]
        print(f"Running AI tagging (Claude) on {len(rows)} ads with {args.ai_window} spend...")
        stats = tag_with_ai(conn, rows)
        print(f"  {stats}")

    conn.close()


def _extract_patterns(args: argparse.Namespace) -> None:
    from creative_intelligence.analysis.patterns import extract_patterns, extract_weighted_patterns
    print(f"Extracting {'spend-weighted ' if args.weighted else ''}patterns from {args.date_range} data...")
    pats = (extract_weighted_patterns(date_range=args.date_range) if args.weighted
            else extract_patterns(date_range=args.date_range))
    print(f"  {len(pats)} patterns written")
    for p in pats[:5]:
        print(f"  - {p['pattern_name']} ({p['winner_count']} winners)")


def _status(args: argparse.Namespace) -> None:
    from creative_intelligence.db import db_exists, get_db_path, get_connection

    print("=== Creative Intelligence System Status ===\n")
    db_path = get_db_path()
    print(f"DB path:   {db_path}")
    print(f"DB exists: {db_exists()}")

    if not db_exists():
        print("\nRun `init` first.")
        return

    conn = get_connection()
    for table in [
        "creatives", "creative_performance", "creative_tags",
        "creative_patterns", "products", "product_benefits",
        "generated_hooks", "generated_scripts", "creative_visual_attributes",
    ]:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<40} {n:>6} rows")
        except Exception as e:
            print(f"  {table:<40} ERROR: {e}")
    conn.close()


# ─────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m creative_intelligence.cli",
        description="Creative Intelligence CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialise the database")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest creatives and performance data")
    p_ingest.add_argument("--dry-run", action="store_true", default=False)
    p_ingest.add_argument("--skip-copy-fetch", action="store_true", default=False)
    p_ingest.add_argument("--date-range", default=None, choices=["yesterday", "7d", "30d", "90d"])

    # tag
    p_tag = sub.add_parser("tag", help="Tag creatives")
    p_tag.add_argument("--ai", action="store_true", default=False,
                       help="Also AI-tag (Claude, fruit taxonomy) ads that spent in --ai-window")
    p_tag.add_argument("--ai-window", default="90d", choices=["7d", "30d", "90d"])

    # extract-patterns
    p_pat = sub.add_parser("extract-patterns", help="Extract winning patterns")
    p_pat.add_argument("--date-range", default="7d")
    p_pat.add_argument("--weighted", action="store_true", default=False,
                       help="Spend-weighted patterns (pooled ROAS vs account average)")

    # status
    sub.add_parser("status", help="Show system status")

    args = parser.parse_args()

    dispatch = {
        "init":                _init,
        "ingest":              _ingest,
        "tag":                 _tag,
        "extract-patterns":    _extract_patterns,
        "status":              _status,
    }

    handler = dispatch.get(args.command)
    if handler:
        try:
            handler(args)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(1)
        except Exception as e:
            print(f"Error: {e}")
            raise
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
