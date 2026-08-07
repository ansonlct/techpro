# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import news_core as core  # noqa: E402

WEB_DATA = ROOT / "web" / "data"


def safe_http_url(value: str) -> str:
    return core.sanitize_http_url(value)


def article_payload(row) -> dict:
    description = (row["description"] or "").strip()
    resolution_status = row["url_resolution_status"] or ""
    url = row["url"]
    # Legacy builds may already have written a wrong direct URL into the rolling cache.
    # Until that row is seen again and re-resolved, suppress the
    # unverified legacy URL instead of exporting a possibly unrelated article.
    if resolution_status in {"legacy_unverified", "legacy_search_unresolved"}:
        url = row["original_url"] if core.is_google_news_url(row["original_url"] or "") else ""
    # The website must never present a Google News wrapper as a publisher direct
    # link. Unresolved wrappers stay visible as articles but are non-clickable.
    if core.is_google_news_url(url or ""):
        url = ""
    return {
        "id": row["fingerprint"][:16],
        "published_at": row["published_at"],
        "source": row["source"],
        "category": row["category"] or "其他",
        "title": row["title"],
        "description": description[:600],
        "url": safe_http_url(url),
        "url_resolution_status": resolution_status,
        "feed_name": row["feed_name"],
    }


def build_outputs(collection: dict | None = None) -> dict:
    core.initialize_database_from_seed()
    core.normalize_existing_database()
    override_stats = core.apply_curated_url_overrides(hours=336)
    rows = core.query_articles(hours=48, limit=10000)
    feeds = core.query_feed_states()
    generated = core.to_iso(core.now_hk())
    articles = [article_payload(row) for row in rows]
    categories = Counter(item["category"] for item in articles)
    sources = Counter(item["source"] for item in articles)

    feed_payload = [{
        "id": row["feed_id"],
        "name": row["feed_name"],
        "last_success_at": row["last_success_at"],
        "last_attempt_at": row["last_attempt_at"],
        "ok": not bool(row["last_error"]),
        "last_item_count": row["last_item_count"],
        "error": (row["last_error"] or "")[:240],
    } for row in feeds]

    if collection is None:
        ok_count = sum(1 for feed in feed_payload if feed["ok"] and feed["last_success_at"])
        failed_count = sum(1 for feed in feed_payload if not feed["ok"])
        collection = {
            "feeds_total": len(feed_payload), "feeds_ok": ok_count,
            "feeds_failed": failed_count, "new": 0, "updated": 0,
            "fetched": sum(feed["last_item_count"] or 0 for feed in feed_payload),
            "source_filtered": 0, "irrelevant": 0, "errors": [],
            "mode": "existing-database",
        }
    collection["url_overrides"] = override_stats

    news = {
        "schema_version": 1,
        "app": core.APP_NAME,
        "version": core.APP_VERSION,
        "generated_at": generated,
        "window_hours": 48,
        "total": len(articles),
        "summary": {
            "categories": dict(categories.most_common()),
            "sources": dict(sources.most_common()),
        },
        "articles": articles,
    }
    custom_keywords = core.load_custom_keywords()
    status = {
        "schema_version": 1,
        "generated_at": generated,
        "version": core.APP_VERSION,
        "collection": collection,
        "feed_summary": {
            "total": len(feed_payload),
            "healthy": sum(1 for feed in feed_payload if feed["ok"]),
            "failed": sum(1 for feed in feed_payload if not feed["ok"]),
        },
        "deployment": {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "default_branch": os.environ.get("GITHUB_REF_NAME", "main") or "main",
            "workflow_file": "update-and-deploy.yml",
            "schedule_minutes": [0, 15, 30, 45],
            "timezone": "Asia/Hong_Kong",
            "keywords_file": "keywords.txt",
        },
        "custom_keywords": custom_keywords,
        "target_sources": [
            {"id": item.get("id", ""), "name": item.get("name", "")}
            for item in core.load_config().get("target_sources", [])
            if item.get("name")
        ],
        "capture_policy": {
            "mode": "capture-first",
            "query_results_require_category_match": False,
            "sitewide_results_require_category_match": False,
        },
        "feeds": feed_payload,
    }

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    (WEB_DATA / "news.json").write_text(json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8")
    (WEB_DATA / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    core.export_csv(WEB_DATA / "latest_48h.csv", rows)
    return {"articles": len(articles), "feeds": len(feed_payload), "generated_at": generated}


def progress(event: dict) -> None:
    mark = "✓" if event.get("ok") else "✗"
    print(f"[{event.get('position', 0):>3}/{event.get('total', 0)}] {mark} {event.get('feed', '')}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="收集新聞並產生 GitHub Pages 靜態資料")
    parser.add_argument("--skip-collect", action="store_true", help="只從現有 SQLite 產生網站資料")
    parser.add_argument("--workers", type=int, default=10, help="並行 RSS 工作數（預設 10）")
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args()

    core.initialize_database_from_seed()
    collection = None
    if not args.skip_collect:
        collection = core.run_collection(progress_callback=progress, max_workers=args.workers)
        legacy_repair = core.repair_legacy_unverified_urls(
            hours=72, max_workers=min(args.workers, 3), timeout=15, limit=80,
        )
        resolution = core.resolve_stored_google_news_urls(
            hours=48, max_workers=min(args.workers, 4), timeout=15, limit=500,
        )
        overrides = core.apply_curated_url_overrides(hours=336)
        collection["legacy_url_repair"] = legacy_repair
        collection["url_resolution"] = resolution
        collection["url_overrides"] = overrides
        print(json.dumps(collection, ensure_ascii=False, indent=2))
    pruned = core.prune_database(args.retention_days)
    result = build_outputs(collection)
    core.checkpoint_database()
    print(f"網站資料完成：{result['articles']} 篇、{result['feeds']} 個 feed；清理 {pruned} 筆舊資料")

    if not args.skip_collect and collection and collection.get("feeds_ok", 0) == 0 and result["articles"] == 0:
        print("所有來源均失敗，而且沒有可沿用資料。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
