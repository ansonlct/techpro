# -*- coding: utf-8 -*-
"""Collection and classification core for the web edition.

The module intentionally uses only Python's standard library so it can run on
GitHub Actions without installing third-party packages.
"""
from __future__ import annotations

import base64
import copy
import difflib
import csv
import hashlib
import html
import json
import re
import shutil
import sqlite3
import ssl
import time
import traceback
import urllib.parse
import urllib.request
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

APP_NAME = "香港網騙及數碼風險新聞監察器"
APP_VERSION = "VERSION 1"
# URL states that already contain a trustworthy publisher URL.  These states
# are monotonic: a later Google News wrapper refresh or transient resolver
# failure must never downgrade them back to pending/unresolved.
STABLE_DIRECT_URL_STATUSES = frozenset({
    "direct", "verified_title", "verified_source", "curated_verified",
})
HK_TZ = timezone(timedelta(hours=8))
ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.json"
KEYWORDS_PATH = ROOT_DIR / "keywords.txt"
URL_OVERRIDES_PATH = ROOT_DIR / "url_overrides.json"
RUNTIME_DIR = ROOT_DIR / "runtime"
DATA_DIR = RUNTIME_DIR
DB_PATH = RUNTIME_DIR / "news_monitor.db"
LOG_PATH = RUNTIME_DIR / "monitor.log"
SEED_DB_PATH = ROOT_DIR / "seed" / "news_monitor_seed.db"


def _load_embedded_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "overlap_minutes": 90,
            "search_window_hours": 48,
            "request_timeout_seconds": 25,
            "google_news": {"enabled": True, "search_groups": []},
            "target_sources": [],
            "official_feeds": [],
            "category_keywords": {},
            "context_rules": {},
        }


DEFAULT_CONFIG = _load_embedded_config()


@dataclass(slots=True)
class Article:
    source: str
    title: str
    published_at: datetime
    url: str
    description: str
    feed_id: str
    feed_name: str
    category: str = ""
    fingerprint: str = ""


def now_hk() -> datetime:
    return datetime.now(HK_TZ)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(HK_TZ).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HK_TZ)
        return dt.astimezone(HK_TZ)
    except (ValueError, TypeError):
        return None


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        user_config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"config.json 格式錯誤：{exc}") from exc

    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in user_config.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def load_url_overrides(path: Path | None = None) -> list[dict[str, object]]:
    """Load curated title-to-publisher URL corrections.

    These overrides are deliberately data-driven so a known bad legacy URL can
    be repaired without changing the collector code again. Invalid entries are
    ignored instead of breaking the scheduled build.
    """
    path = path or URL_OVERRIDES_PATH
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = payload.get("overrides", []) if isinstance(payload, dict) else []
    output: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        try:
            parsed = urllib.parse.urlsplit(url)
        except Exception:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        titles = item.get("titles", [])
        if isinstance(titles, str):
            titles = [titles]
        cleaned_titles = [clean_html(str(title)).strip() for title in titles if str(title).strip()]
        if not cleaned_titles:
            continue
        output.append({
            "source": clean_html(str(item.get("source", ""))).strip(),
            "titles": cleaned_titles,
            "url": url,
        })
    return output


def load_custom_keywords(path: Path | None = None, limit: int = 100) -> list[str]:
    """Load all Google News monitoring queries from ``keywords.txt``.

    ``keywords.txt`` is the single source of truth for monitoring queries. Each
    non-empty, non-comment line is one query. Duplicate lines are removed
    case-insensitively, while a safety limit prevents accidental runaway jobs.
    """
    path = path or KEYWORDS_PATH
    if not path.exists():
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        keyword = re.sub(r"\s+", " ", raw_line.strip())
        if not keyword or keyword.startswith("#"):
            continue
        if len(keyword) > 240:
            keyword = keyword[:240].rstrip()
        folded = keyword.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        output.append(keyword)
        if len(output) >= max(1, int(limit)):
            break
    return output


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def initialize_database_from_seed() -> bool:
    """Copy the bundled seed DB only when no runtime database exists."""
    ensure_runtime_dir()
    if DB_PATH.exists() or not SEED_DB_PATH.exists():
        return False
    shutil.copy2(SEED_DB_PATH, DB_PATH)
    return True


def log_to_file(message: str) -> None:
    ensure_runtime_dir()
    stamp = now_hk().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def db_connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT,
            published_at TEXT NOT NULL,
            url TEXT NOT NULL,
            original_url TEXT,
            url_resolution_status TEXT,
            url_resolution_checked_at TEXT,
            description TEXT,
            feed_id TEXT NOT NULL,
            feed_name TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
        CREATE TABLE IF NOT EXISTS feed_state (
            feed_id TEXT PRIMARY KEY,
            feed_name TEXT NOT NULL,
            last_success_at TEXT,
            last_attempt_at TEXT,
            last_error TEXT,
            last_item_count INTEGER NOT NULL DEFAULT 0
        );
    """)
    # Idempotent schema upgrade for databases created before direct-URL tracking.
    article_columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    for column, declaration in (
        ("original_url", "TEXT"),
        ("url_resolution_status", "TEXT"),
        ("url_resolution_checked_at", "TEXT"),
    ):
        if column not in article_columns:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column} {declaration}")
    conn.execute("""
        UPDATE articles
        SET url_resolution_status = CASE
            WHEN url LIKE 'https://news.google.com/%' THEN 'pending'
            WHEN feed_id LIKE 'google_%' THEN 'legacy_unverified'
            ELSE 'direct'
        END
        WHERE url_resolution_status IS NULL OR url_resolution_status = ''
    """)
    conn.execute("""
        UPDATE articles SET original_url = url
        WHERE (original_url IS NULL OR original_url = '')
          AND url LIKE 'https://news.google.com/%'
    """)
    conn.commit()
    return conn


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def article_category_scores(title: str, description: str, config: dict) -> dict[str, float]:
    haystack = normalize_text(f"{title} {description}")
    scores: dict[str, float] = {}
    for category, words in config.get("category_keywords", {}).items():
        score = 0.0
        for word in words:
            normalized = normalize_text(word)
            if normalized and normalized in haystack:
                score += 1.0 + min(len(normalized), 24) / 16.0
        if score > 0:
            scores[category] = score

    for category, rule in config.get("context_rules", {}).items():
        groups = rule.get("required_groups", [])
        matched_terms: list[str] = []
        all_groups_matched = True
        for group in groups:
            group_matches = [term for term in group if normalize_text(term) in haystack]
            if not group_matches:
                all_groups_matched = False
                break
            matched_terms.extend(group_matches)
        if all_groups_matched and groups:
            score = float(rule.get("base_score", 20))
            score += sum(min(len(normalize_text(term)), 20) / 20.0 for term in matched_terms)
            scores[category] = max(scores.get(category, 0.0), score)
    return scores


def classify_article(title: str, description: str, config: dict) -> str:
    scores = article_category_scores(title, description, config)
    return max(scores.items(), key=lambda item: item[1])[0] if scores else ""


def is_relevant(title: str, description: str, config: dict) -> bool:
    return bool(article_category_scores(title, description, config))


def sanitize_http_url(value: str | None) -> str:
    """Return one safe HTTP(S) URL, stripping accidental HTML attributes.

    Some publisher RSS feeds place text such as ``target="blank`` after the
    URL inside a link element.  Keeping that suffix makes the browser open an
    invalid address.  This helper extracts the actual URL and rejects schemes,
    credentials, control characters and backslashes that should never appear
    in a public article link.
    """
    raw = html.unescape(str(value or "")).strip()
    if not raw:
        return ""
    match = re.match(r"https?://[^\"'<>]+", raw, flags=re.I)
    candidate = (match.group(0) if match else raw).strip()
    candidate = re.split(
        r"\s+(?=(?:target|rel|class|style|onclick)\s*=)", candidate,
        maxsplit=1, flags=re.I,
    )[0].strip()
    candidate = "".join(
        urllib.parse.quote(char, safe="") if char.isspace() else char
        for char in candidate
    )
    if any(ord(char) < 32 for char in candidate) or "\\" in candidate:
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except Exception:
        return ""
    if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return ""
    return candidate


def canonical_source(source: str, config: dict) -> str:
    source_n = normalize_text(source)
    if not source_n:
        return ""
    for target in config.get("target_sources", []):
        candidates = [target.get("name", ""), *target.get("aliases", [])]
        for candidate in candidates:
            candidate_n = normalize_text(candidate)
            if candidate_n and (candidate_n == source_n or candidate_n in source_n or source_n in candidate_n):
                return target["name"]
    return ""


def source_from_url(url: str, config: dict) -> str:
    url = sanitize_http_url(url)
    if not url:
        return ""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")
    except Exception:
        return ""
    for target in config.get("target_sources", []):
        for alias in target.get("aliases", []):
            alias_n = normalize_text(alias)
            if "." in alias_n and (host == alias_n or host.endswith("." + alias_n)):
                return target["name"]
    return ""


def split_publisher_suffix(title: str, config: dict) -> tuple[str, str]:
    cleaned = clean_html(title)
    for separator in (" - ", " – ", " — "):
        if separator in cleaned:
            headline, suffix = cleaned.rsplit(separator, 1)
            canonical = canonical_source(suffix, config)
            if canonical:
                return headline.strip(), canonical
    return cleaned, ""


def detect_article_source(source: str, title: str, url: str, config: dict) -> tuple[str, str]:
    clean_title, suffix_source = split_publisher_suffix(title, config)
    return clean_title, suffix_source or source_from_url(url, config) or canonical_source(source, config)


def make_fingerprint(source: str, title: str, published_at: datetime) -> str:
    key = "|".join([
        normalize_text(source), normalize_text(title),
        published_at.astimezone(HK_TZ).strftime("%Y-%m-%d"),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def request_bytes(url: str, timeout: int, attempts: int = 2) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/150 Safari/537.36 HKDigitalRiskNewsMonitor/1.0.0"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-HK,zh-TW;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
    })
    context = ssl.create_default_context()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.6 * (attempt + 1))
    assert last_exc is not None
    raise last_exc




def is_google_news_url(url: str) -> bool:
    """Return True for Google News wrapper URLs used by RSS and /read links."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    return host == "news.google.com" and bool(parts) and (
        "articles" in parts or (len(parts) >= 2 and parts[-2] == "read")
    )


def _google_news_article_id(url: str) -> str:
    if not is_google_news_url(url):
        return ""
    try:
        path_parts = [part for part in urllib.parse.urlsplit(url).path.split("/") if part]
    except Exception:
        return ""
    if not path_parts:
        return ""
    token = path_parts[-1].strip()
    return token if re.fullmatch(r"[A-Za-z0-9_-]{16,}", token) else ""


def _read_varint(data: bytes, start: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    position = start
    while position < len(data) and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid varint")


def decode_google_news_url_offline(url: str) -> str:
    """Decode the older Google News RSS format without a network request.

    Newer tokens contain an ``AU_yqL`` opaque identifier and need the Google
    batchexecute resolver; in that case this function returns an empty string.
    """
    article_id = _google_news_article_id(url)
    if not article_id:
        return ""
    try:
        padded = article_id + "=" * ((4 - len(article_id) % 4) % 4)
        data = base64.urlsafe_b64decode(padded)
        prefix = b"\x08\x13\x22"
        suffix = b"\xd2\x01\x00"
        if data.startswith(prefix):
            data = data[len(prefix):]
        if data.endswith(suffix):
            data = data[:-len(suffix)]
        length, position = _read_varint(data)
        if length <= 0 or position + length > len(data):
            return ""
        candidate = data[position:position + length].decode("utf-8", errors="strict").strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    except Exception:
        return ""
    return ""


def _extract_google_decoding_params(page: str, article_id: str) -> dict[str, str] | None:
    """Extract the timestamp/signature required by Google's current resolver.

    Modern ``AU_yqL`` tokens cannot be decoded offline.  Google embeds a short-
    lived timestamp and signature in the article landing page.  Attribute order
    is not stable, so both values are located independently.
    """
    timestamp_match = re.search(r'''data-n-a-ts\s*=\s*["'](\d+)["']''', page, flags=re.I)
    signature_match = re.search(r'''data-n-a-sg\s*=\s*["']([^"']+)["']''', page, flags=re.I)
    if not timestamp_match or not signature_match:
        return None
    return {
        "gn_art_id": article_id,
        "timestamp": timestamp_match.group(1),
        "signature": html.unescape(signature_match.group(1)).strip(),
    }


def _get_google_decoding_params(article_id: str, timeout: int = 15) -> dict[str, str] | None:
    """Fetch the signed decoding parameters for one opaque article id.

    ``/articles`` is tried first and ``/rss/articles`` is retained as a fallback
    because Google has alternated between both landing pages.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", article_id or ""):
        return None
    queries = (
        "?hl=zh-TW&gl=HK&ceid=HK%3Azh-Hant",
        "?hl=en-US&gl=US&ceid=US%3Aen",
        "",
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/150 Safari/537.36 HKDigitalRiskNewsMonitor/1.0.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.7",
        "Cache-Control": "no-cache",
    }
    context = ssl.create_default_context()
    for path in ("articles", "rss/articles"):
        for query in queries:
            request = urllib.request.Request(
                f"https://news.google.com/{path}/{article_id}{query}", headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=max(3, timeout), context=context) as response:
                    raw = response.read(768_000)
                    charset = response.headers.get_content_charset() or "utf-8"
                params = _extract_google_decoding_params(raw.decode(charset, errors="replace"), article_id)
                if params:
                    return params
            except Exception:
                continue
    return None


def _google_rpc_payload(article_id: str, timestamp: str, signature: str) -> list:
    context = [
        ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
         None, None, None, None, None, 0, 1],
        "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
    ]
    return ["garturlreq", context, article_id, int(timestamp), signature]


def _extract_batchexecute_results(text: str) -> list[str | None]:
    """Parse ordered ``Fbv4je`` results, preserving failed-item placeholders."""
    envelopes: list = []
    for chunk in re.split(r"\n\n+", text):
        candidate = chunk.strip()
        if not candidate or candidate.startswith(")]}'"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # Some envelopes prefix the JSON with a byte-count line.
            bracket = candidate.find("[")
            if bracket < 0:
                continue
            try:
                parsed = json.loads(candidate[bracket:])
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, list):
            envelopes.extend(parsed)

    results: list[str | None] = []
    for entry in envelopes:
        if not (isinstance(entry, list) and len(entry) >= 3):
            continue
        if entry[0] != "wrb.fr" and entry[1] != "Fbv4je":
            continue
        candidate: str | None = None
        if isinstance(entry[2], str):
            try:
                nested = json.loads(entry[2])
            except json.JSONDecodeError:
                nested = None
            if (isinstance(nested, list) and len(nested) >= 2
                    and nested[0] == "garturlres" and isinstance(nested[1], str)
                    and nested[1].startswith(("http://", "https://"))):
                candidate = nested[1]
        results.append(candidate)

    if results:
        return results

    # Defensive single-result fallback for minor envelope changes.
    match = re.search(r'\[\\"garturlres\\",\\"((?:\\.|[^"\\])*)\\"', text)
    if not match:
        return []
    try:
        candidate = json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        candidate = match.group(1).replace(r"\/", "/")
    return [candidate if candidate.startswith(("http://", "https://")) else None]


def _extract_batchexecute_urls(text: str) -> list[str]:
    """Compatibility helper returning only successful URLs."""
    return [url for url in _extract_batchexecute_results(text) if url]


def _batch_decode_google_params(params: list[dict[str, str]], timeout: int = 20) -> dict[str, str]:
    """Resolve signed opaque ids one request at a time.

    The Google ``batchexecute`` response does not reliably preserve the input
    order for a multi-item request.  Returning a mapping by zipping the response
    to the request can therefore attach a valid publisher URL to the wrong news
    headline.  VERSION 1 deliberately sends one RPC item per HTTP request.
    """
    result: dict[str, str] = {}
    for item in params:
        try:
            article_id = item["gn_art_id"]
            rpc = _google_rpc_payload(article_id, item["timestamp"], item["signature"])
        except (KeyError, TypeError, ValueError):
            continue
        request_payload = [[[
            "Fbv4je",
            json.dumps(rpc, ensure_ascii=False, separators=(",", ":")),
            None,
            "generic",
        ]]]
        encoded = urllib.parse.quote(
            json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")), safe="",
        )
        request = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
            data=("f.req=" + encoded).encode("ascii"),
            method="POST",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/150 Safari/537.36 HKDigitalRiskNewsMonitor/1.0.0"
                ),
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://news.google.com/",
                "Origin": "https://news.google.com",
            },
        )
        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=max(4, timeout), context=context) as response:
                text = response.read(1_000_000).decode("utf-8", errors="replace")
        except Exception:
            continue
        candidates = _extract_batchexecute_results(text)
        candidate = next((value for value in candidates if value), "")
        if candidate and not is_google_news_url(candidate):
            result[article_id] = candidate
    return result


def _batch_decode_google_news_id(article_id: str, timeout: int = 15) -> str:
    """Resolve one modern opaque id using signed landing-page parameters."""
    params = _get_google_decoding_params(article_id, timeout=timeout)
    if not params:
        return ""
    return _batch_decode_google_params([params], timeout=max(timeout, 20)).get(article_id, "")


def _follow_google_news_redirect(url: str, timeout: int = 15) -> str:
    """Fallback resolver using ordinary redirects and common HTML URL hints."""
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/150 Safari/537.36 HKDigitalRiskNewsMonitor/1.0.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-HK,zh-TW;q=0.9,en;q=0.7",
    })
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=max(3, timeout), context=context) as response:
        final_url = response.geturl()
        if final_url and not is_google_news_url(final_url):
            return final_url
        raw = response.read(256_000)
        charset = response.headers.get_content_charset() or "utf-8"
    page = raw.decode(charset, errors="replace")
    patterns = (
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\';]+)',
    )
    for pattern in patterns:
        found = re.search(pattern, page, flags=re.I)
        if not found:
            continue
        candidate = html.unescape(found.group(1).strip())
        candidate = urllib.parse.urljoin(url, candidate)
        if candidate.startswith(("http://", "https://")) and not is_google_news_url(candidate):
            return candidate
    return ""


def _normalize_headline_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", clean_html(value or "")).casefold()
    value = re.sub(r"\s*[-–—|｜]\s*(香港01|明報|星島|東方|橙新聞|am730|hket|信報|香港電台|now|有線|無綫|文匯|大公).*$", "", value)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)


def _headline_similarity(expected: str, observed: str) -> float:
    left = _normalize_headline_for_match(expected)
    right = _normalize_headline_for_match(observed)
    if not left or not right:
        return 0.0
    if min(len(left), len(right)) >= 8 and (left in right or right in left):
        return 1.0
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    left_pairs = {left[i:i + 2] for i in range(max(0, len(left) - 1))}
    right_pairs = {right[i:i + 2] for i in range(max(0, len(right) - 1))}
    containment = 0.0
    if left_pairs and right_pairs:
        containment = len(left_pairs & right_pairs) / max(1, min(len(left_pairs), len(right_pairs)))
    longest = difflib.SequenceMatcher(None, left, right).find_longest_match().size
    longest_score = longest / max(1, min(len(left), len(right)))
    return max(sequence, containment, longest_score)


def _normalize_override_title(value: str) -> str:
    """Normalize feed-added time/date/section suffixes before exact matching."""
    value = clean_html(value or "")
    value = re.sub(r"\s*[-–—]\s*\d{8}\s*[-–—]\s*[^-–—]+$", "", value)
    value = re.sub(r"\s*\(\d{1,2}:\d{2}\)\s*$", "", value)
    return _normalize_headline_for_match(value)


def find_url_override(title: str, source: str = "",
                      overrides: list[dict[str, object]] | None = None,
                      config: dict | None = None) -> str:
    """Return a curated direct URL for an exact normalized title/source pair."""
    target_title = _normalize_override_title(title)
    if not target_title:
        return ""
    overrides = overrides if overrides is not None else load_url_overrides()
    config = config or load_config()
    target_source = canonical_source(source, config) or clean_html(source or "").strip()
    for item in overrides:
        expected_source = canonical_source(str(item.get("source", "")), config) or str(item.get("source", "")).strip()
        if expected_source and target_source and expected_source != target_source:
            continue
        for alias in item.get("titles", []):
            if target_title == _normalize_override_title(str(alias)):
                return str(item.get("url", "")).strip()
    return ""


def apply_curated_url_overrides(hours: int = 336, db_path: Path | None = None) -> dict[str, int]:
    """Repair matching database rows using the curated URL correction file."""
    overrides = load_url_overrides()
    stats = {"checked": 0, "matched": 0, "updated": 0}
    if not overrides:
        return stats
    config = load_config()
    conn = db_connect(db_path)
    try:
        cutoff = to_iso(now_hk() - timedelta(hours=max(1, int(hours))))
        rows = conn.execute(
            """SELECT id, source, title, url, original_url, url_resolution_status
               FROM articles WHERE published_at >= ?""",
            (cutoff,),
        ).fetchall()
        stats["checked"] = len(rows)
        checked_at = to_iso(now_hk())
        for row in rows:
            direct = find_url_override(row["title"], row["source"], overrides, config)
            if not direct:
                continue
            stats["matched"] += 1
            original = row["original_url"] or ""
            if not original and is_google_news_url(row["url"] or ""):
                original = row["url"]
            if row["url"] == direct and row["url_resolution_status"] == "curated_verified":
                continue
            cursor = conn.execute(
                """UPDATE articles SET url=?, original_url=?,
                   url_resolution_status='curated_verified',
                   url_resolution_checked_at=? WHERE id=?""",
                (direct, original or None, checked_at, row["id"]),
            )
            stats["updated"] += max(cursor.rowcount, 0)
        conn.commit()
        return stats
    finally:
        conn.close()


class _PublisherTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_titles: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {str(key).casefold(): (value or "") for key, value in attrs}
        if tag == "meta":
            marker = (attributes.get("property") or attributes.get("name")).casefold()
            content = attributes.get("content", "").strip()
            if marker in {"og:title", "twitter:title"} and content:
                self.meta_titles.append(content)
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and data.strip():
            self.title_parts.append(data.strip())

    def best_title(self) -> str:
        candidates = [*self.meta_titles, " ".join(self.title_parts)]
        for value in candidates:
            title = clean_html(value)
            if title:
                return title
        return ""


def _extract_publisher_page_title(page: str) -> str:
    parser = _PublisherTitleParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        pass
    title = parser.best_title()
    if title:
        return title
    match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    return clean_html(match.group(1)) if match else ""


def _fetch_publisher_page_title(url: str, timeout: int = 10) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/150 Safari/537.36 HKDigitalRiskNewsMonitor/1.0.0"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.7",
        "Accept-Language": "zh-HK,zh-TW;q=0.9,en;q=0.6",
    })
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=max(3, timeout), context=context) as response:
        raw = response.read(512_000)
        charset = response.headers.get_content_charset() or "utf-8"
    return _extract_publisher_page_title(raw.decode(charset, errors="replace"))


def _validate_resolved_publisher_url(candidate: str, expected_source: str,
                                     expected_title: str, config: dict,
                                     timeout: int = 10) -> tuple[bool, str]:
    if not candidate or is_google_news_url(candidate):
        return False, "google_or_empty"
    detected_source = source_from_url(candidate, config)
    if expected_source and detected_source != expected_source:
        return False, "source_mismatch"
    try:
        remote_title = _fetch_publisher_page_title(candidate, timeout=timeout)
    except Exception:
        remote_title = ""
    if remote_title and _headline_similarity(expected_title, remote_title) < 0.34:
        return False, "title_mismatch"
    return True, "title_verified" if remote_title else "source_verified"


def resolve_google_news_url(url: str, timeout: int = 15) -> str:
    """Return the publisher URL for a Google News wrapper when possible.

    This compatibility helper has no article source/title to validate against,
    so production database resolution uses ``resolve_stored_google_news_urls``.
    """
    if not is_google_news_url(url):
        return url
    offline = decode_google_news_url_offline(url)
    if offline:
        return offline
    article_id = _google_news_article_id(url)
    if article_id:
        try:
            decoded = _batch_decode_google_news_id(article_id, timeout=timeout)
            if decoded and not is_google_news_url(decoded):
                return decoded
        except Exception:
            pass
    try:
        redirected = _follow_google_news_redirect(url, timeout=timeout)
        if redirected:
            return redirected
    except Exception:
        pass
    return url


def resolve_stored_google_news_urls(hours: int = 48, max_workers: int = 4,
                                    timeout: int = 15, limit: int = 500,
                                    db_path: Path | None = None) -> dict[str, int]:
    """Resolve and strictly validate recent Google News wrapper URLs.

    A candidate is written only when it belongs to the expected publisher.  If
    the publisher page title is available it must also resemble the RSS title.
    Any uncertain candidate is rejected and the original Google News URL is kept.
    """
    conn = db_connect(db_path)
    try:
        config = load_config()
        cutoff = to_iso(now_hk() - timedelta(hours=max(1, hours)))
        rows = conn.execute(
            """SELECT id, source, title, url, original_url, url_resolution_status FROM articles
               WHERE published_at >= ?
                 AND COALESCE(url_resolution_status, 'pending') IN ('pending', 'unresolved')
                 AND (url LIKE 'https://news.google.com/%'
                      OR original_url LIKE 'https://news.google.com/%')
               ORDER BY published_at DESC LIMIT ?""",
            (cutoff, max(1, int(limit))),
        ).fetchall()
        if not rows:
            return {
                "attempted": 0, "resolved": 0, "unresolved": 0, "updated_rows": 0,
                "signed_params": 0, "redirect_fallbacks": 0, "single_requests": 0,
                "rejected_source_mismatch": 0, "rejected_title_mismatch": 0,
                "rejected_title_unavailable": 0,
                "verified_by_title": 0, "verified_by_source": 0,
            }

        items: dict[str, dict] = {}
        for row in rows:
            wrapper = row["original_url"] if is_google_news_url(row["original_url"] or "") else row["url"]
            if not is_google_news_url(wrapper or ""):
                continue
            items.setdefault(wrapper, {
                "wrapper": wrapper,
                "source": row["source"],
                "title": row["title"],
                "row_ids": [],
            })["row_ids"].append(row["id"])

        stats = {
            "attempted": len(items), "resolved": 0, "unresolved": 0, "updated_rows": 0,
            "signed_params": 0, "redirect_fallbacks": 0, "single_requests": 0,
            "rejected_source_mismatch": 0, "rejected_title_mismatch": 0,
            "rejected_title_unavailable": 0,
            "verified_by_title": 0, "verified_by_source": 0,
        }
        if not items:
            return stats

        def resolve_one(item: dict) -> tuple[dict, str, str, bool, bool]:
            wrapper = item["wrapper"]
            candidate = decode_google_news_url_offline(wrapper)
            used_redirect = False
            used_signed = False
            if not candidate:
                article_id = _google_news_article_id(wrapper)
                if article_id:
                    params = _get_google_decoding_params(article_id, timeout=timeout)
                    if params:
                        used_signed = True
                        candidate = _batch_decode_google_params([params], timeout=max(timeout, 20)).get(article_id, "")
                if not candidate:
                    try:
                        candidate = _follow_google_news_redirect(wrapper, timeout=timeout)
                        used_redirect = bool(candidate)
                    except Exception:
                        candidate = ""
            if not candidate:
                return item, "", "unresolved", used_redirect, used_signed
            valid, reason = _validate_resolved_publisher_url(
                candidate, item["source"], item["title"], config, timeout=min(timeout, 10),
            )
            # Redirect-page fallbacks can occasionally expose only a publisher
            # homepage/canonical listing. Without a readable matching title, do
            # not promote that fallback to a direct article URL.
            if valid and used_redirect and reason == "source_verified":
                return item, "", "title_unavailable", used_redirect, used_signed
            return item, candidate if valid else "", reason, used_redirect, used_signed

        workers = max(1, min(int(max_workers), 3, len(items)))
        results = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="url-single") as pool:
            futures = [pool.submit(resolve_one, item) for item in items.values()]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception:
                    continue

        checked_at = to_iso(now_hk())
        processed_wrappers: set[str] = set()
        for item, candidate, reason, used_redirect, used_signed in results:
            wrapper = item["wrapper"]
            processed_wrappers.add(wrapper)
            if not decode_google_news_url_offline(wrapper):
                stats["single_requests"] += 1
            if used_signed:
                stats["signed_params"] += 1
            if used_redirect:
                stats["redirect_fallbacks"] += 1
            if reason == "source_mismatch":
                stats["rejected_source_mismatch"] += 1
            elif reason == "title_mismatch":
                stats["rejected_title_mismatch"] += 1
            elif reason == "title_unavailable":
                stats["rejected_title_unavailable"] += 1
            if candidate:
                status = "verified_title" if reason == "title_verified" else "verified_source"
                if reason == "title_verified":
                    stats["verified_by_title"] += 1
                else:
                    stats["verified_by_source"] += 1
                for row_id in item["row_ids"]:
                    cursor = conn.execute(
                        """UPDATE articles SET url=?, original_url=?,
                           url_resolution_status=?, url_resolution_checked_at=? WHERE id=?""",
                        (candidate, wrapper, status, checked_at, row_id),
                    )
                    stats["updated_rows"] += max(cursor.rowcount, 0)
                stats["resolved"] += 1
            else:
                for row_id in item["row_ids"]:
                    conn.execute(
                        """UPDATE articles SET url=?, original_url=?,
                           url_resolution_status='unresolved', url_resolution_checked_at=? WHERE id=?""",
                        (wrapper, wrapper, checked_at, row_id),
                    )

        # Futures that failed before returning are also kept as original wrappers.
        for wrapper, item in items.items():
            if wrapper in processed_wrappers:
                continue
            for row_id in item["row_ids"]:
                conn.execute(
                    """UPDATE articles SET url=?, original_url=?,
                       url_resolution_status='unresolved', url_resolution_checked_at=? WHERE id=?""",
                    (wrapper, wrapper, checked_at, row_id),
                )

        stats["unresolved"] = stats["attempted"] - stats["resolved"]
        conn.commit()
        return stats
    finally:
        conn.close()

def parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(HK_TZ)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(HK_TZ)
    except Exception:
        return None


def child_text(item: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(item):
        if child.tag.split("}")[-1].lower() in names:
            return "".join(child.itertext()).strip()
    return ""


def find_items(root: ET.Element) -> list[ET.Element]:
    items = [node for node in root.iter() if node.tag.split("}")[-1].lower() == "item"]
    return items or [node for node in root.iter() if node.tag.split("}")[-1].lower() == "entry"]


def parse_feed(xml_bytes: bytes, feed_id: str, feed_name: str, default_source: str = "") -> list[Article]:
    root = ET.fromstring(xml_bytes)
    output: list[Article] = []
    for item in find_items(root):
        title = clean_html(child_text(item, ("title",)))
        description = clean_html(child_text(item, ("description", "summary", "content")))
        pub_raw = child_text(item, ("pubdate", "published", "updated", "date"))
        source = clean_html(child_text(item, ("source",))) or default_source
        link = child_text(item, ("link",))
        if not link:
            for child in list(item):
                if child.tag.split("}")[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        link = sanitize_http_url(link)
        published = parse_rss_date(pub_raw)
        if title and link and published:
            output.append(Article(source, title, published, link, description, feed_id, feed_name))
    return output


def google_feed_url(query: str, config: dict, start_time: datetime) -> str:
    google = config["google_news"]
    query_with_time = f"({query}) after:{start_time.strftime('%Y-%m-%d')}"
    params = urllib.parse.urlencode({
        "q": query_with_time,
        "hl": google.get("language", "zh-TW"),
        "gl": google.get("country", "HK"),
        "ceid": google.get("ceid", "HK:zh-Hant"),
    })
    return f"https://news.google.com/rss/search?{params}"


def _site_query_for_source(source: str, config: dict) -> str:
    canonical = canonical_source(source, config) or source
    for target in config.get("target_sources", []):
        if target.get("name") == canonical:
            return str(target.get("site_query", "")).strip()
    return ""


def rediscover_google_news_wrapper(title: str, source: str, config: dict | None = None,
                                   timeout: int = 15) -> str:
    """Find a fresh Google News wrapper for a legacy row whose original wrapper was lost."""
    config = config or load_config()
    clean_title = clean_html(title or "").strip()
    if not clean_title:
        return ""
    site_query = _site_query_for_source(source, config)
    query_title = clean_title[:180].replace('"', " ")
    query = f'"{query_title}" {site_query}'.strip()
    feed_url = google_feed_url(query, config, now_hk() - timedelta(days=4))
    try:
        xml_bytes = request_bytes(feed_url, timeout=max(4, timeout), attempts=1)
        candidates = parse_feed(xml_bytes, "legacy_url_repair", "Legacy URL repair")
    except Exception:
        return ""
    best_url = ""
    best_score = 0.0
    expected_source = canonical_source(source, config) or source
    for article in candidates:
        candidate_title, detected_source = detect_article_source(
            article.source, article.title, article.url, config,
        )
        if expected_source and detected_source != expected_source:
            continue
        score = _headline_similarity(clean_title, candidate_title)
        if score > best_score and score >= 0.72 and is_google_news_url(article.url):
            best_url = article.url
            best_score = score
    return best_url


def repair_legacy_unverified_urls(hours: int = 72, max_workers: int = 3,
                                  timeout: int = 15, limit: int = 80,
                                  db_path: Path | None = None) -> dict[str, int]:
    """Recover Google wrappers lost by legacy builds through exact-title search."""
    stats = {"attempted": 0, "rediscovered": 0, "updated_rows": 0, "unresolved": 0}
    conn = db_connect(db_path)
    try:
        cutoff = to_iso(now_hk() - timedelta(hours=max(1, int(hours))))
        retry_before = to_iso(now_hk() - timedelta(hours=6))
        rows = conn.execute(
            """SELECT id, source, title, url, original_url FROM articles
               WHERE published_at >= ?
                 AND url_resolution_status IN ('legacy_unverified', 'legacy_search_unresolved')
                 AND (url_resolution_checked_at IS NULL OR url_resolution_checked_at <= ?)
                 AND (original_url IS NULL OR original_url=''
                      OR original_url NOT LIKE 'https://news.google.com/%')
               ORDER BY published_at DESC LIMIT ?""",
            (cutoff, retry_before, max(1, int(limit))),
        ).fetchall()
        stats["attempted"] = len(rows)
        if not rows:
            return stats
        config = load_config()
        workers = max(1, min(int(max_workers), 3, len(rows)))

        def lookup(row):
            return row, rediscover_google_news_wrapper(
                row["title"], row["source"], config=config, timeout=timeout,
            )

        results = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="legacy-url") as pool:
            futures = [pool.submit(lookup, row) for row in rows]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception:
                    continue
        checked_at = to_iso(now_hk())
        returned_ids: set[int] = set()
        for row, wrapper in results:
            returned_ids.add(row["id"])
            if not wrapper:
                conn.execute(
                    """UPDATE articles SET url_resolution_status='legacy_search_unresolved',
                       url_resolution_checked_at=? WHERE id=?""",
                    (checked_at, row["id"]),
                )
                continue
            cursor = conn.execute(
                """UPDATE articles SET url=?, original_url=?,
                   url_resolution_status='pending', url_resolution_checked_at=? WHERE id=?""",
                (wrapper, wrapper, checked_at, row["id"]),
            )
            stats["rediscovered"] += 1
            stats["updated_rows"] += max(cursor.rowcount, 0)
        for row in rows:
            if row["id"] not in returned_ids:
                conn.execute(
                    """UPDATE articles SET url_resolution_status='legacy_search_unresolved',
                       url_resolution_checked_at=? WHERE id=?""",
                    (checked_at, row["id"]),
                )
        stats["unresolved"] = stats["attempted"] - stats["rediscovered"]
        conn.commit()
        return stats
    finally:
        conn.close()


def get_window_start(conn: sqlite3.Connection | None, feed_id: str, run_end: datetime, config: dict) -> datetime:
    del conn, feed_id
    return run_end - timedelta(hours=max(1, int(config.get("search_window_hours", 48))))


def mark_feed_attempt(conn: sqlite3.Connection, feed_id: str, feed_name: str, attempted_at: datetime) -> None:
    conn.execute("""
        INSERT INTO feed_state (feed_id, feed_name, last_attempt_at, last_item_count)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(feed_id) DO UPDATE SET feed_name=excluded.feed_name,
            last_attempt_at=excluded.last_attempt_at
    """, (feed_id, feed_name, to_iso(attempted_at)))
    conn.commit()


def mark_feed_success(conn: sqlite3.Connection, feed_id: str, feed_name: str, success_at: datetime, item_count: int) -> None:
    conn.execute("""
        INSERT INTO feed_state (feed_id, feed_name, last_success_at, last_attempt_at, last_error, last_item_count)
        VALUES (?, ?, ?, ?, NULL, ?)
        ON CONFLICT(feed_id) DO UPDATE SET feed_name=excluded.feed_name,
            last_success_at=excluded.last_success_at, last_attempt_at=excluded.last_attempt_at,
            last_error=NULL, last_item_count=excluded.last_item_count
    """, (feed_id, feed_name, to_iso(success_at), to_iso(success_at), item_count))
    conn.commit()


def mark_feed_error(conn: sqlite3.Connection, feed_id: str, feed_name: str, attempted_at: datetime, error: str) -> None:
    conn.execute("""
        INSERT INTO feed_state (feed_id, feed_name, last_attempt_at, last_error, last_item_count)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(feed_id) DO UPDATE SET feed_name=excluded.feed_name,
            last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error,
            last_item_count=0
    """, (feed_id, feed_name, to_iso(attempted_at), error[:1000]))
    conn.commit()


def save_article(conn: sqlite3.Connection, article: Article) -> str:
    stamp = to_iso(now_hk())
    article.url = sanitize_http_url(article.url)
    if not article.url:
        raise ValueError("article URL must be an absolute HTTP(S) URL")
    article.fingerprint = make_fingerprint(article.source, article.title, article.published_at)
    original_url = article.url
    resolution_status = "pending" if is_google_news_url(original_url) else "direct"
    existing = conn.execute(
        """SELECT id, url, original_url, url_resolution_status, url_resolution_checked_at
           FROM articles WHERE fingerprint=?""",
        (article.fingerprint,),
    ).fetchone()
    if existing:
        existing_url = sanitize_http_url(existing["url"] or "")
        existing_status = existing["url_resolution_status"] or ""

        # Google News frequently re-emits the same story with its wrapper URL.
        # Do not let that less-trustworthy observation overwrite a publisher
        # URL that was already obtained from a direct feed or verified resolver.
        preserve_verified = (
            is_google_news_url(original_url)
            and existing_status in STABLE_DIRECT_URL_STATUSES
            and bool(existing_url)
            and not is_google_news_url(existing_url)
        )
        if preserve_verified:
            # Keep the provenance of the stronger observation as well.  A
            # Google wrapper refresh may extend last_seen_at and content, but it
            # must not replace the direct-feed/resolver URL or its source state.
            conn.execute("""
                UPDATE articles SET description=?, category=?, last_seen_at=?
                WHERE fingerprint=?
            """, (article.description, article.category, stamp, article.fingerprint))
        else:
            conn.execute("""
                UPDATE articles SET url=?, original_url=?, url_resolution_status=?,
                    url_resolution_checked_at=NULL, description=?, category=?, last_seen_at=?,
                    feed_id=?, feed_name=? WHERE fingerprint=?
            """, (article.url, original_url, resolution_status, article.description, article.category,
                  stamp, article.feed_id, article.feed_name, article.fingerprint))
        return "updated"
    conn.execute("""
        INSERT INTO articles (fingerprint, source, title, category, published_at, url,
            original_url, url_resolution_status, url_resolution_checked_at, description,
            feed_id, feed_name, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
    """, (article.fingerprint, article.source, article.title, article.category,
          to_iso(article.published_at), article.url, original_url, resolution_status,
          article.description, article.feed_id, article.feed_name, stamp, stamp))
    return "new"

def process_feed_articles(conn: sqlite3.Connection, articles: list[Article], start_time: datetime,
                          run_end: datetime, config: dict, expected_source: str = "",
                          force_source: str = "", accept_query_matches: bool = False) -> dict[str, int]:
    stats = {"fetched": len(articles), "new": 0, "updated": 0, "too_old": 0,
             "future": 0, "irrelevant": 0, "source_filtered": 0}
    for article in articles:
        article.url = sanitize_http_url(article.url)
        if not article.url:
            stats["source_filtered"] += 1
            continue
        article.title, detected_source = detect_article_source(article.source, article.title, article.url, config)
        actual_source = force_source or detected_source
        if article.published_at < start_time:
            stats["too_old"] += 1
            continue
        if article.published_at > run_end + timedelta(minutes=10):
            stats["future"] += 1
            continue
        if not accept_query_matches and not is_relevant(article.title, article.description, config):
            stats["irrelevant"] += 1
            continue
        if not actual_source or (expected_source and actual_source != expected_source):
            stats["source_filtered"] += 1
            continue
        article.source = actual_source
        article.category = classify_article(article.title, article.description, config)
        stats[save_article(conn, article)] += 1
    conn.commit()
    return stats


def build_feed_jobs(config: dict) -> list[dict]:
    jobs: list[dict] = []
    for feed in config.get("official_feeds", []):
        if not feed.get("enabled", True):
            continue
        urls = [url for url in (feed.get("urls") or [feed.get("url", "")]) if url]
        if urls:
            jobs.append({"id": feed["id"], "name": feed["name"], "kind": "rss",
                         "urls": urls, "force_source": feed["source"],
                         "accept_query_matches": True})

    google = config.get("google_news", {})
    if google.get("enabled", True):
        monitoring_queries = load_custom_keywords()
        for target in config.get("target_sources", []):
            for query_no, query in enumerate(monitoring_queries, start=1):
                digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
                jobs.append({
                    "id": f"google_{target['id']}_keyword_{digest}",
                    "name": f"{target['name']}－監察關鍵字 {query_no}",
                    "kind": "google",
                    "query": f"({query}) {target['site_query']}",
                    "expected_source": target["name"],
                    "accept_query_matches": True,
                })
            if google.get("site_wide_fallback", True):
                jobs.append({
                    "id": f"google_{target['id']}_sitewide",
                    "name": f"{target['name']}－全站 RSS 後備",
                    "kind": "google", "query": target["site_query"],
                    "expected_source": target["name"],
                    "accept_query_matches": True,
                })
    return jobs


def request_first_available(urls: list[str], timeout: int) -> tuple[bytes, str]:
    errors: list[str] = []
    for url in urls:
        try:
            return request_bytes(url, timeout), url
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("所有 RSS 網址均失敗；" + " | ".join(errors))


def _fetch_job(job: dict, config: dict, run_end: datetime, timeout: int) -> dict:
    start_time = get_window_start(None, job["id"], run_end, config)
    if job["kind"] == "google":
        urls = [google_feed_url(job["query"], config, start_time)]
    else:
        urls = job["urls"]
    xml_bytes, used_url = request_first_available(urls, timeout)
    articles = parse_feed(xml_bytes, job["id"], job["name"],
                          default_source=job.get("force_source", "") if job["kind"] == "rss" else "")
    return {"job": job, "articles": articles, "used_url": used_url, "start_time": start_time}


def run_collection(progress_callback: Callable[[dict], None] | None = None,
                   max_workers: int = 10) -> dict:
    callback = progress_callback or (lambda event: None)
    config = load_config()
    timeout = int(config.get("request_timeout_seconds", 25))
    run_end = now_hk()
    initialize_database_from_seed()
    conn = db_connect()
    jobs = build_feed_jobs(config)
    totals: dict = {"feeds_total": len(jobs), "feeds_ok": 0, "feeds_failed": 0,
                    "new": 0, "updated": 0, "fetched": 0, "source_filtered": 0,
                    "irrelevant": 0, "errors": [], "started_at": to_iso(run_end)}
    for job in jobs:
        mark_feed_attempt(conn, job["id"], job["name"], run_end)

    workers = max(1, min(int(max_workers), 20, len(jobs) or 1))
    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feed") as pool:
            futures = {pool.submit(_fetch_job, job, config, run_end, timeout): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                completed += 1
                try:
                    result = future.result()
                    stats = process_feed_articles(
                        conn, result["articles"], result["start_time"], run_end, config,
                        expected_source=job.get("expected_source", ""),
                        force_source=job.get("force_source", ""),
                        accept_query_matches=bool(job.get("accept_query_matches", False)),
                    )
                    mark_feed_success(conn, job["id"], job["name"], run_end, stats["fetched"])
                    totals["feeds_ok"] += 1
                    for key in ("new", "updated", "fetched", "source_filtered", "irrelevant"):
                        totals[key] += stats[key]
                    callback({"type": "progress", "position": completed, "total": len(jobs),
                              "feed": job["name"], "ok": True, "stats": stats})
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    mark_feed_error(conn, job["id"], job["name"], run_end, error)
                    totals["feeds_failed"] += 1
                    totals["errors"].append({"feed": job["name"], "error": error[:500]})
                    log_to_file(f"{job['name']} 更新失敗：{error}\n{traceback.format_exc()}")
                    callback({"type": "progress", "position": completed, "total": len(jobs),
                              "feed": job["name"], "ok": False, "error": error})
        totals["finished_at"] = to_iso(now_hk())
        return totals
    finally:
        conn.close()


def query_articles(hours: int | None = 48, search_text: str = "", limit: int = 5000,
                   db_path: Path | None = None) -> list[sqlite3.Row]:
    conn = db_connect(db_path)
    try:
        sql = """SELECT id, fingerprint, source, title, category, published_at, url,
                 original_url, url_resolution_status, url_resolution_checked_at,
                 description, feed_name, first_seen_at, last_seen_at FROM articles WHERE 1=1"""
        params: list[object] = []
        if hours is not None:
            end = now_hk()
            cutoff = end - timedelta(hours=hours)
            sql += " AND published_at >= ? AND published_at <= ?"
            params.extend([to_iso(cutoff), to_iso(end)])
        if search_text.strip():
            pattern = f"%{search_text.strip()}%"
            sql += " AND (title LIKE ? OR source LIKE ? OR category LIKE ? OR description LIKE ?)"
            params.extend([pattern] * 4)
        sql += " ORDER BY published_at DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def query_feed_states(db_path: Path | None = None) -> list[sqlite3.Row]:
    conn = db_connect(db_path)
    try:
        return conn.execute("""SELECT feed_id, feed_name, last_success_at, last_attempt_at,
            last_error, last_item_count FROM feed_state ORDER BY feed_name""").fetchall()
    finally:
        conn.close()


def export_csv(destination: Path, rows: Iterable[sqlite3.Row]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = ["出版時間（香港）", "來源", "分類", "標題", "新聞網址", "摘要",
               "收集來源", "首次發現時間", "最後看見時間"]
    with destination.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            public_url = row["url"] or ""
            if is_google_news_url(public_url) or row["url_resolution_status"] in {
                "legacy_unverified", "legacy_search_unresolved", "unresolved", "pending",
            }:
                public_url = ""
            writer.writerow([row["published_at"], row["source"], row["category"], row["title"],
                             public_url, row["description"], row["feed_name"],
                             row["first_seen_at"], row["last_seen_at"]])


def normalize_existing_database(config: dict | None = None, db_path: Path | None = None) -> None:
    config = config or load_config()
    if db_path is None:
        initialize_database_from_seed()
    conn = db_connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM articles ORDER BY id").fetchall()
        if not rows:
            return
        conn.execute("BEGIN")
        for row in rows:
            conn.execute("UPDATE articles SET fingerprint=? WHERE id=?", (f"v15-migrate-{row['id']}", row["id"]))
        keepers: dict[str, int] = {}
        for row in rows:
            published = parse_iso(row["published_at"])
            clean_url = sanitize_http_url(row["url"] or "")
            clean_original_url = sanitize_http_url(row["original_url"] or "")
            clean_title, actual_source = detect_article_source(row["source"], row["title"], clean_url, config)
            if not published or not actual_source or not clean_url:
                conn.execute("DELETE FROM articles WHERE id=?", (row["id"],))
                continue
            fingerprint = make_fingerprint(actual_source, clean_title, published)
            category = classify_article(clean_title, row["description"] or "", config) or row["category"] or ""
            if fingerprint in keepers:
                keeper_id = keepers[fingerprint]
                keeper = conn.execute("SELECT description, first_seen_at, last_seen_at FROM articles WHERE id=?", (keeper_id,)).fetchone()
                description = row["description"] or ""
                if keeper and len(description) > len(keeper["description"] or ""):
                    conn.execute("UPDATE articles SET description=? WHERE id=?", (description, keeper_id))
                if keeper:
                    conn.execute("UPDATE articles SET first_seen_at=?, last_seen_at=? WHERE id=?",
                                 (min(keeper["first_seen_at"], row["first_seen_at"]),
                                  max(keeper["last_seen_at"], row["last_seen_at"]), keeper_id))
                conn.execute("DELETE FROM articles WHERE id=?", (row["id"],))
                continue
            keepers[fingerprint] = row["id"]
            conn.execute("""UPDATE articles SET fingerprint=?, source=?, title=?, category=?,
                         url=?, original_url=? WHERE id=?""",
                         (fingerprint, actual_source, clean_title, category, clean_url,
                          clean_original_url or None, row["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def checkpoint_database() -> None:
    """Flush WAL content into the main DB before GitHub Actions caches it."""
    conn = db_connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()

def prune_database(retention_days: int = 14) -> int:
    cutoff = now_hk() - timedelta(days=max(2, retention_days))
    conn = db_connect()
    try:
        cursor = conn.execute("DELETE FROM articles WHERE published_at < ?", (to_iso(cutoff),))
        conn.commit()
        return max(cursor.rowcount, 0)
    finally:
        conn.close()
