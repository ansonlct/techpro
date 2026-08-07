# -*- coding: utf-8 -*-
"""Strict release validation for the static website package."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import struct
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import news_core as core  # noqa: E402

WEB = ROOT / "web"
EXPECTED_VERSION = "VERSION 1"
EXPECTED_CACHE = "hk-risk-monitor-version-1"
REQUIRED_WEB_FILES = (
    "index.html",
    "manifest.json",
    "service-worker.js",
    "assets/app.js",
    "assets/styles.css",
    "icons/icon.svg",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "data/news.json",
    "data/status.json",
    "data/latest_48h.csv",
)
REQUIRED_DB_COLUMNS = {
    "fingerprint",
    "source",
    "title",
    "category",
    "published_at",
    "url",
    "original_url",
    "url_resolution_status",
    "url_resolution_checked_at",
    "description",
    "feed_id",
    "feed_name",
    "first_seen_at",
    "last_seen_at",
}
CSV_URL_HEADER = "新聞網址"


class ValidationError(RuntimeError):
    pass


class _HTMLAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.local_refs: list[str] = []
        self.meta: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): (value or "") for key, value in attrs}
        element_id = values.get("id", "").strip()
        if element_id:
            self.ids.append(element_id)
        if tag.casefold() == "meta":
            self.meta.append(values)
        for key in ("href", "src"):
            value = values.get(key, "").strip()
            if value.startswith(("./", "../")):
                self.local_refs.append(value.split("?", 1)[0].split("#", 1)[0])


def fail(message: str) -> None:
    raise ValidationError(message)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"JSON 無法讀取：{path.relative_to(ROOT)}：{exc}")
    if not isinstance(value, dict):
        fail(f"JSON 根節點必須是 object：{path.relative_to(ROOT)}")
    return value


def is_safe_public_url(value: str) -> bool:
    if not value:
        return True
    return core.sanitize_http_url(value) == value


def is_forbidden_public_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host == "news.google.com":
        return True
    if host == "ansonlct.github.io" and path.casefold() == "/technews":
        return True
    return False


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        header = fh.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        fail(f"不是有效 PNG：{path.relative_to(ROOT)}")
    return struct.unpack(">II", header[16:24])


def validate_files() -> None:
    missing = [name for name in REQUIRED_WEB_FILES if not (WEB / name).is_file()]
    if missing:
        fail("缺少網站檔案：" + ", ".join(missing))
    if not (ROOT / "seed/news_monitor_seed.db").is_file():
        fail("缺少正式版種子資料庫：seed/news_monitor_seed.db")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version != EXPECTED_VERSION or core.APP_VERSION != EXPECTED_VERSION:
        fail(f"版本不一致：VERSION={version!r}, APP_VERSION={core.APP_VERSION!r}")


def validate_data() -> tuple[dict, dict]:
    news = read_json(WEB / "data/news.json")
    status = read_json(WEB / "data/status.json")
    if news.get("schema_version") != 1 or not isinstance(news.get("articles"), list):
        fail("news.json schema 無效")
    if status.get("schema_version") != 1 or not isinstance(status.get("feeds"), list):
        fail("status.json schema 無效")
    if news.get("version") != EXPECTED_VERSION or status.get("version") != EXPECTED_VERSION:
        fail("news.json／status.json 未標示 VERSION 1")
    articles = news["articles"]
    if news.get("total") != len(articles):
        fail("news.json total 與 articles 數量不符")

    seen_ids: set[str] = set()
    for index, article in enumerate(articles, start=1):
        if not isinstance(article, dict):
            fail(f"news.json 第 {index} 篇文章格式無效")
        missing = [key for key in ("id", "title", "published_at", "source", "category", "url") if key not in article]
        if missing:
            fail(f"news.json 第 {index} 篇文章缺少欄位：{', '.join(missing)}")
        if not str(article.get("title", "")).strip() or not str(article.get("published_at", "")).strip():
            fail(f"news.json 第 {index} 篇文章標題或時間為空")
        article_id = str(article.get("id", "")).strip()
        if not article_id or article_id in seen_ids:
            fail(f"news.json 文章 ID 為空或重複：{article_id!r}")
        seen_ids.add(article_id)
        url = str(article.get("url", "")).strip()
        if not is_safe_public_url(url):
            fail(f"news.json 含非 HTTP(S) URL：{url}")
        if is_forbidden_public_url(url):
            fail(f"news.json 含 wrapper／網站首頁錯誤連結：{url}")

    deployment = status.get("deployment")
    if not isinstance(deployment, dict):
        fail("status.json 缺少 deployment 設定")
    if deployment.get("workflow_file") != "update-and-deploy.yml":
        fail("status.json workflow_file 不正確")
    if deployment.get("timezone") != "Asia/Hong_Kong":
        fail("status.json 時區不是 Asia/Hong_Kong")
    if deployment.get("schedule_minutes") != [0, 15, 30, 45]:
        fail("status.json 排程分鐘設定不正確")
    return news, status


def validate_csv(news: dict) -> None:
    path = WEB / "data/latest_48h.csv"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as exc:
        fail(f"CSV 無法讀取：{exc}")
    if len(rows) != len(news["articles"]):
        fail(f"CSV 行數 {len(rows)} 與 news.json {len(news['articles'])} 不符")
    if rows and CSV_URL_HEADER not in rows[0]:
        fail(f"CSV 缺少欄位：{CSV_URL_HEADER}")
    for row in rows:
        url = (row.get(CSV_URL_HEADER) or "").strip()
        if not is_safe_public_url(url) or is_forbidden_public_url(url):
            fail(f"CSV 含不安全／錯誤 URL：{url}")


def validate_html_and_assets() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    parser = _HTMLAuditParser()
    parser.feed(html)
    duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
    if duplicates:
        fail("index.html 含重複 id：" + ", ".join(duplicates))
    for required_ref in ("./assets/styles.css", "./assets/app.js", "./manifest.json"):
        if required_ref not in html:
            fail(f"index.html 缺少相對路徑：{required_ref}")
    for ref in parser.local_refs:
        target = (WEB / ref).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            fail(f"index.html 本地路徑越界：{ref}")
        if not target.is_file() and not (ref in {"./", "../"}):
            fail(f"index.html 引用不存在檔案：{ref}")

    referrer_ok = any(item.get("name", "").casefold() == "referrer" and item.get("content", "") == "no-referrer" for item in parser.meta)
    if not referrer_ok:
        fail("index.html 缺少 no-referrer 設定")
    csp_values = [item.get("content", "") for item in parser.meta if item.get("http-equiv", "").casefold() == "content-security-policy"]
    if not csp_values or "default-src 'self'" not in csp_values[0] or "object-src 'none'" not in csp_values[0]:
        fail("index.html Content-Security-Policy 不完整")
    if EXPECTED_VERSION not in html:
        fail("index.html 未顯示 VERSION 1")

    manifest = read_json(WEB / "manifest.json")
    if manifest.get("id") != "./" or manifest.get("start_url") != "./":
        fail("manifest id／start_url 必須為 ./")
    if manifest.get("display") != "standalone":
        fail("manifest display 必須為 standalone")
    if manifest.get("prefer_related_applications") is not False:
        fail("manifest prefer_related_applications 必須為 false")
    icon_entries = manifest.get("icons")
    if not isinstance(icon_entries, list) or len(icon_entries) < 2:
        fail("manifest icons 不完整")
    expected_icons = {"./icons/icon-192.png": (192, 192), "./icons/icon-512.png": (512, 512)}
    for icon_path, dimensions in expected_icons.items():
        if not any(item.get("src") == icon_path and item.get("sizes") == f"{dimensions[0]}x{dimensions[1]}" for item in icon_entries if isinstance(item, dict)):
            fail(f"manifest 缺少正確圖示設定：{icon_path}")
        if png_dimensions(WEB / icon_path.removeprefix("./")) != dimensions:
            fail(f"圖示尺寸錯誤：{icon_path}")


def validate_service_worker() -> None:
    worker = (WEB / "service-worker.js").read_text(encoding="utf-8")
    app_js = (WEB / "assets/app.js").read_text(encoding="utf-8")
    if EXPECTED_CACHE not in worker:
        fail("Service Worker cache 名稱不是正式版")
    if "request.mode === \"navigate\"" not in worker:
        fail("Service Worker 缺少 navigation 專用 fallback")
    if worker.count('caches.match("./index.html")') != 1:
        fail("Service Worker 的 index.html fallback 必須只出現在 navigation 流程")
    if "VERSION 1" not in app_js:
        fail("app.js fallback 版本不是 VERSION 1")
    if "new URL(url, location.href)" in app_js:
        fail("app.js 仍可能把空 URL 解析成目前網站")
    if 'titleLink.removeAttribute("href")' not in app_js:
        fail("app.js 未移除不可驗證文章的 href")


def validate_overrides() -> None:
    config = core.load_config()
    overrides = core.load_url_overrides()
    if not overrides:
        fail("url_overrides.json 沒有有效項目")
    seen: set[tuple[str, str]] = set()
    for item in overrides:
        expected = core.canonical_source(str(item.get("source", "")), config) or str(item.get("source", "")).strip()
        actual = core.source_from_url(str(item.get("url", "")), config)
        if not expected or actual != expected:
            fail(f"override URL 來源不符：{expected or item.get('source')} -> {item.get('url')}")
        url = str(item.get("url", "")).strip()
        if not is_safe_public_url(url) or is_forbidden_public_url(url):
            fail(f"override URL 不安全：{url}")
        for title in item.get("titles", []):
            normalized = core._normalize_override_title(str(title))
            key = (expected, normalized)
            if not normalized or key in seen:
                fail(f"override 標題為空或重複：{expected} / {title}")
            seen.add(key)
            if core.find_url_override(str(title), expected, overrides, config) != url:
                fail(f"override 無法反向匹配：{title}")


def validate_seed_database() -> None:
    path = ROOT / "seed/news_monitor_seed.db"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fail(f"種子資料庫 integrity_check 失敗：{integrity}")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
        missing = sorted(REQUIRED_DB_COLUMNS - columns)
        if missing:
            fail("種子資料庫缺少欄位：" + ", ".join(missing))
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT fingerprint FROM articles GROUP BY fingerprint HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if duplicates:
            fail(f"種子資料庫含 {duplicates} 組重複 fingerprint")
    except sqlite3.Error as exc:
        fail(f"種子資料庫無法讀取：{exc}")
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass


def main() -> int:
    try:
        validate_files()
        news, status = validate_data()
        validate_csv(news)
        validate_html_and_assets()
        validate_service_worker()
        validate_overrides()
        validate_seed_database()
    except ValidationError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    clickable = sum(1 for item in news["articles"] if item.get("url"))
    print(
        "Validation passed: "
        f"{news['total']} articles ({clickable} clickable), "
        f"{len(status['feeds'])} feed states, VERSION 1 release package"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
