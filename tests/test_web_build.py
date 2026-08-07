# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import build_web_data
from src import news_core as core


class WebBuildTests(unittest.TestCase):
    def test_payload_rejects_non_http_url(self):
        self.assertEqual(build_web_data.safe_http_url("javascript:alert(1)"), "")
        self.assertEqual(build_web_data.safe_http_url(""), "")
        self.assertEqual(build_web_data.safe_http_url("/relative/article"), "")
        self.assertEqual(build_web_data.safe_http_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(
            build_web_data.safe_http_url('https://example.com/a" target="blank'),
            "https://example.com/a",
        )
        self.assertEqual(
            build_web_data.safe_http_url("https://example.com/a　b"),
            "https://example.com/a%E3%80%80b",
        )

    def test_rss_parser_strips_accidental_html_attributes_from_link(self):
        xml = b"""<?xml version='1.0' encoding='UTF-8'?>
        <rss><channel><item>
          <title>Test article</title>
          <link>https://example.com/article&amp;quot; target=&amp;quot;blank</link>
          <pubDate>Sun, 02 Aug 2026 10:00:00 +0800</pubDate>
        </item></channel></rss>"""
        articles = core.parse_feed(xml, "test", "Test feed", default_source="香港01")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://example.com/article")

    def test_legacy_unverified_google_result_does_not_export_wrong_direct_url(self):
        row = {
            "fingerprint": "abc123456789012345", "published_at": core.to_iso(core.now_hk()),
            "source": "香港經濟日報", "category": "AI安全", "title": "OpenAI測試",
            "description": "", "url": "https://news.hket.com/article/wrong",
            "original_url": None, "url_resolution_status": "legacy_unverified",
            "feed_name": "香港經濟日報－監察關鍵字",
        }
        self.assertEqual(build_web_data.article_payload(row)["url"], "")


    def test_unresolved_google_wrapper_is_not_exported_as_direct_link(self):
        row = {
            "fingerprint": "abc123456789012345", "published_at": core.to_iso(core.now_hk()),
            "source": "信報", "category": "AI安全", "title": "OpenAI測試",
            "description": "",
            "url": "https://news.google.com/rss/articles/CBMiExampleToken123456789?oc=5",
            "original_url": "https://news.google.com/rss/articles/CBMiExampleToken123456789?oc=5",
            "url_resolution_status": "unresolved",
            "feed_name": "信報－全站 RSS 後備",
        }
        self.assertEqual(build_web_data.article_payload(row)["url"], "")

    def test_static_files_use_relative_github_pages_paths(self):
        root = Path(__file__).parents[1] / "web"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "assets/app.js").read_text(encoding="utf-8")
        self.assertIn('./assets/styles.css', html)
        self.assertIn('fetchJson("./data/news.json")', js)
        self.assertNotIn('fetch("/data/', js)
        self.assertNotIn('id="viewToggleButton"', html)
        self.assertIn('id="nextUpdate"', html)
        self.assertNotIn('id="runUpdateButton"', html)
        self.assertIn('id="approveAllCheckbox"', html)
        self.assertIn('id="downloadApprovedButton"', html)
        self.assertIn('class="article-approve"', html)
        self.assertIn('formatArticleTime', js)
        self.assertIn('downloadApprovedTxt', js)
        self.assertIn('readableUrlForExport', js)
        self.assertIn('decodeURI(value)', js)
        self.assertIn('new URL(value)', js)
        self.assertNotIn('new URL(url, location.href)', js)
        self.assertIn('titleLink.removeAttribute("href")', js)
        self.assertIn('id="menuButton"', html)
        self.assertIn('id="riskView"', html)
        self.assertIn('id="systemView"', html)
        self.assertIn('id="keywordsView"', html)
        self.assertIn('id="keywordList"', html)
        self.assertIn('id="monitoredSourceList"', html)
        self.assertIn('只顯示數碼風險相關', js)
        self.assertIn('class="category-checklist"', html)
        self.assertNotIn('<select id="categoryFilter"', html)
        self.assertIn('defaultCategorySelection', js)
        self.assertIn('name !== "其他"', js)
        self.assertNotIn('加入方法', html)
        self.assertNotIn('keyword-help', html)
        self.assertIn('編輯 keywords.txt', html)
        self.assertIn('<option value="24" selected>', html)
        self.assertIn('articleTimeText', js)
        self.assertNotIn('initViewMode', js)
        self.assertIn('class="global-search"', html)
        css = (root / "assets/styles.css").read_text(encoding="utf-8")
        self.assertNotIn('news-list.compact', css)
        self.assertIn('.news-row', css)
        self.assertIn('.sidebar', css)

    def test_build_outputs_creates_valid_json_and_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp); runtime = temp / "runtime"; web_data = temp / "webdata"
            with patch.object(core,"RUNTIME_DIR",runtime), patch.object(core,"DATA_DIR",runtime), \
                 patch.object(core,"DB_PATH",runtime/"news_monitor.db"), patch.object(core,"LOG_PATH",runtime/"monitor.log"), \
                 patch.object(core,"SEED_DB_PATH",temp/"missing.db"), patch.object(build_web_data,"WEB_DATA",web_data):
                conn=core.db_connect(); article=core.Article("香港01","投資騙局測試",core.now_hk()-timedelta(hours=1),"https://example.com/a","虛假投資騙案","test","測試來源",category="網騙")
                core.save_article(conn,article); core.mark_feed_success(conn,"test","測試來源",core.now_hk(),1); conn.close()
                with patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/risk-monitor", "GITHUB_REF_NAME": "main"}, clear=False):
                    result=build_web_data.build_outputs({"feeds_total":1,"feeds_ok":1,"feeds_failed":0,"new":1,"updated":0,"fetched":1,"errors":[]})
                self.assertEqual(result["articles"],1)
                news=json.loads((web_data/"news.json").read_text(encoding="utf-8")); status=json.loads((web_data/"status.json").read_text(encoding="utf-8"))
                self.assertEqual(news["total"],1); self.assertEqual(status["feed_summary"]["healthy"],1)
                self.assertEqual(status["deployment"]["repository"], "example/risk-monitor")
                self.assertEqual(status["deployment"]["schedule_minutes"], [0, 15, 30, 45])
                self.assertEqual(status["custom_keywords"], core.load_custom_keywords())
                self.assertEqual(len(status["target_sources"]), len(core.DEFAULT_CONFIG["target_sources"]))
                self.assertEqual(status["capture_policy"]["mode"], "capture-first")
                self.assertTrue((web_data/"latest_48h.csv").exists())


    def test_version_1_release_markers_are_consistent(self):
        root = Path(__file__).parents[1]
        self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "VERSION 1")
        self.assertEqual(core.APP_VERSION, "VERSION 1")
        self.assertIn("VERSION 1", (root / "web/index.html").read_text(encoding="utf-8"))
        self.assertIn("VERSION 1", (root / "web/assets/app.js").read_text(encoding="utf-8"))
        self.assertIn("hk-risk-monitor-version-1", (root / "web/service-worker.js").read_text(encoding="utf-8"))

    def test_manifest_and_security_metadata_are_release_ready(self):
        root = Path(__file__).parents[1]
        manifest = json.loads((root / "web/manifest.json").read_text(encoding="utf-8"))
        html = (root / "web/index.html").read_text(encoding="utf-8")
        self.assertEqual(manifest["id"], "./")
        self.assertEqual(manifest["start_url"], "./")
        self.assertEqual(manifest["display"], "standalone")
        self.assertIs(manifest["prefer_related_applications"], False)
        self.assertIn('name="referrer" content="no-referrer"', html)
        self.assertIn('http-equiv="Content-Security-Policy"', html)
        self.assertIn("object-src 'none'", html)

    def test_curated_overrides_match_their_configured_publishers(self):
        config = core.load_config()
        overrides = core.load_url_overrides()
        self.assertGreaterEqual(len(overrides), 18)
        seen = set()
        for item in overrides:
            expected = core.canonical_source(str(item["source"]), config) or str(item["source"])
            self.assertEqual(core.source_from_url(str(item["url"]), config), expected)
            for title in item["titles"]:
                key = (expected, core._normalize_override_title(str(title)))
                self.assertNotIn(key, seen)
                seen.add(key)
                self.assertEqual(core.find_url_override(str(title), expected, overrides, config), item["url"])

    def test_packaged_public_data_contains_no_wrapper_or_homepage_links(self):
        root = Path(__file__).parents[1]
        payload = json.loads((root / "web/data/news.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["total"], len(payload["articles"]))
        for article in payload["articles"]:
            url = article.get("url", "")
            self.assertFalse(url.startswith("https://news.google.com/"))
            self.assertNotEqual(url.rstrip("/"), "https://ansonlct.github.io/technews")

    def test_generic_seed_database_name_is_used(self):
        root = Path(__file__).parents[1]
        self.assertEqual(core.SEED_DB_PATH.name, "news_monitor_seed.db")
        self.assertTrue(core.SEED_DB_PATH.is_file())
        self.assertFalse((root / "seed/news_monitor_v1.4.1.db").exists())


if __name__ == "__main__": unittest.main()
