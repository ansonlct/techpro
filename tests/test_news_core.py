# -*- coding: utf-8 -*-
from __future__ import annotations
import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src import news_core as monitor

SAMPLES = [
    ('騎劫WhatsApp帳戶騙案｜男子遭假父親呃千萬元存款', '文匯報', '平台罪案'),
    ('滬六旬漢墮投資騙局被呃594萬　贓款穿越千里變金條', '香港01', '網騙'),
    ('Anthropic AI僅60小時即破解頂尖加密系統　網絡安全秩序遭顛覆', '橙新聞', 'AI安全'),
    ('6馬來西亞人來港電騙判囚　管理貓池10日騙587萬', '香港01', '電騙'),
    ('涉Telegram發布逾萬色情影像 無業男被控發布淫褻物品', '星島日報', '平台罪案'),
    ('證監行動｜六福證券網絡保安缺失遭罰款', '香港經濟日報', '網安'),
]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp.name)
        self.patches = [
            patch.object(monitor, "RUNTIME_DIR", temp_path / "runtime"),
            patch.object(monitor, "DATA_DIR", temp_path / "runtime"),
            patch.object(monitor, "DB_PATH", temp_path / "runtime" / "news_monitor.db"),
            patch.object(monitor, "LOG_PATH", temp_path / "runtime" / "monitor.log"),
            patch.object(monitor, "SEED_DB_PATH", temp_path / "missing-seed.db"),
        ]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_config_matches_disk(self):
        disk = json.loads((Path(__file__).parents[1] / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(monitor.DEFAULT_CONFIG, disk)

    def test_user_samples_are_relevant_and_classified(self):
        for title, source, category in SAMPLES:
            with self.subTest(title=title):
                self.assertTrue(monitor.is_relevant(title, "", monitor.DEFAULT_CONFIG))
                self.assertEqual(monitor.classify_article(title, "", monitor.DEFAULT_CONFIG), category)
                self.assertEqual(monitor.canonical_source(source, monitor.DEFAULT_CONFIG), source)

    def test_context_rules_reject_generic_platform_and_ai_news(self):
        cfg = monitor.DEFAULT_CONFIG
        self.assertFalse(monitor.is_relevant("Telegram推出全新貼圖功能", "", cfg))
        self.assertFalse(monitor.is_relevant("AI公司發布新模型", "", cfg))
        self.assertTrue(monitor.is_relevant("Telegram群組涉發布淫褻影像被捕", "", cfg))
        self.assertTrue(monitor.is_relevant("Claude對話外洩可由搜尋引擎存取", "", cfg))

    def test_publisher_suffix_is_removed(self):
        title, source = monitor.detect_article_source(
            "香港01", "兩男遭WhatsApp騙款 - 香港01",
            "https://news.google.com/rss/articles/example", monitor.DEFAULT_CONFIG,
        )
        self.assertEqual((title, source), ("兩男遭WhatsApp騙款", "香港01"))

    def test_wrong_site_result_is_rejected(self):
        conn = monitor.db_connect()
        article = monitor.Article("香港01", "兩男遭WhatsApp騙款 - 香港01",
            monitor.now_hk()-timedelta(hours=1), "https://news.google.com/rss/articles/x",
            "WhatsApp帳戶騎劫騙案", "google_mingpao_platform", "明報－平台罪案")
        stats = monitor.process_feed_articles(conn, [article], monitor.now_hk()-timedelta(hours=48),
            monitor.now_hk(), monitor.DEFAULT_CONFIG, expected_source="明報")
        self.assertEqual(stats["source_filtered"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 0)
        conn.close()

    def test_duplicate_search_results_become_one_row(self):
        conn = monitor.db_connect(); published = monitor.now_hk()-timedelta(hours=1)
        for feed in ("fraud", "platform"):
            article = monitor.Article("香港01", "兩男遭WhatsApp騙款 - 香港01", published,
                "https://news.google.com/rss/articles/x", "WhatsApp帳戶騎劫騙案", feed, feed)
            monitor.process_feed_articles(conn, [article], monitor.now_hk()-timedelta(hours=48),
                monitor.now_hk(), monitor.DEFAULT_CONFIG, expected_source="香港01")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
        conn.close()

    def test_direct_rss_parsing_forces_source(self):
        xml = '''<rss version="2.0"><channel><item><title>AlipayHK推短訊防偽提醒功能 助用戶辨別騙案</title><link>https://example.com/a</link><description>短訊及網絡騙案資訊</description><pubDate>Thu, 30 Jul 2026 18:34:00 +0800</pubDate></item></channel></rss>'''.encode('utf-8')
        articles = monitor.parse_feed(xml, "direct", "直接 RSS", default_source="文匯報")
        self.assertEqual(len(articles), 1); self.assertEqual(articles[0].source, "文匯報")

    def test_feed_jobs_have_direct_feeds_first_and_site_fallbacks(self):
        jobs = monitor.build_feed_jobs(monitor.DEFAULT_CONFIG)
        first_google = next(i for i,j in enumerate(jobs) if j["kind"] == "google")
        self.assertTrue(all(j["kind"] == "rss" for j in jobs[:first_google]))
        ids = {j["id"] for j in jobs}
        self.assertIn("hket_technology", ids); self.assertIn("google_am730_sitewide", ids)

    def test_keywords_file_is_single_source_for_google_monitoring_jobs(self):
        path = Path(self.temp.name) / "keywords.txt"
        path.write_text("# comment\n深偽詐騙\n  假冒客服  \n深偽詐騙\n", encoding="utf-8")
        with patch.object(monitor, "KEYWORDS_PATH", path):
            self.assertEqual(monitor.load_custom_keywords(), ["深偽詐騙", "假冒客服"])
            jobs = monitor.build_feed_jobs(monitor.DEFAULT_CONFIG)
        keyword_jobs = [job for job in jobs if "_keyword_" in job["id"]]
        targets = monitor.DEFAULT_CONFIG["target_sources"]
        self.assertEqual(len(keyword_jobs), 2 * len(targets))
        self.assertTrue(all(job.get("accept_query_matches") for job in keyword_jobs))
        self.assertTrue(all(job.get("expected_source") for job in keyword_jobs))
        self.assertTrue(any("site:hk01.com" in job["query"] for job in keyword_jobs))
        self.assertEqual(monitor.DEFAULT_CONFIG["google_news"]["search_groups"], [])

    def test_custom_keyword_job_can_accept_direct_query_match(self):
        conn = monitor.db_connect()
        article = monitor.Article(
            "香港01", "某機構發布年度報告 - 香港01",
            monitor.now_hk() - timedelta(hours=1),
            "https://news.google.com/rss/articles/custom",
            "自訂關鍵字搜尋結果", "custom", "自訂關鍵字",
        )
        stats = monitor.process_feed_articles(
            conn, [article], monitor.now_hk() - timedelta(hours=48), monitor.now_hk(),
            monitor.DEFAULT_CONFIG, accept_query_matches=True,
        )
        self.assertEqual(stats["new"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
        conn.close()

    def test_capture_first_keeps_generic_sitewide_article_as_other(self):
        conn = monitor.db_connect()
        article = monitor.Article(
            "香港電台", "多瑙河水位受高溫及乾旱影響下降 匈牙利核電廠將停運",
            monitor.now_hk() - timedelta(hours=1),
            "https://news.rthk.hk/rthk/ch/component/k2/example.htm",
            "國際新聞", "google_rthk_sitewide", "香港電台－全站 RSS 後備",
        )
        stats = monitor.process_feed_articles(
            conn, [article], monitor.now_hk() - timedelta(hours=48), monitor.now_hk(),
            monitor.DEFAULT_CONFIG, expected_source="香港電台", accept_query_matches=True,
        )
        self.assertEqual(stats["new"], 1)
        row = conn.execute("SELECT category FROM articles").fetchone()
        self.assertEqual(row["category"], "")
        conn.close()

    def test_window_and_query_are_bounded(self):
        end = datetime(2026,7,31,8,21,tzinfo=monitor.HK_TZ)
        self.assertEqual(end-monitor.get_window_start(None,"x",end,{"search_window_hours":48}), timedelta(hours=48))
        conn = monitor.db_connect(); current = monitor.now_hk()
        for label, dt in (("within",current-timedelta(hours=47)),("old",current-timedelta(hours=49)),("future",current+timedelta(hours=1))):
            monitor.save_article(conn, monitor.Article("香港01",f"詐騙測試 {label}",dt,f"https://example.com/{label}","詐騙","t","t"))
        conn.commit(); conn.close()
        self.assertEqual([r["title"] for r in monitor.query_articles(48)], ["詐騙測試 within"])



    def test_curated_override_matches_feed_suffix_and_repairs_database(self):
        overrides = [{
            "source": "明報",
            "titles": ["中疾控：新冠病毒檢測陽性率20.3% 屬常規周期波動"],
            "url": "https://news.mingpao.com/ins/test",
        }]
        title = "中疾控：新冠病毒檢測陽性率20.3% 屬常規周期波動 (13:35) - 20260802 - 兩岸"
        self.assertEqual(
            monitor.find_url_override(title, "明報", overrides, monitor.DEFAULT_CONFIG),
            "https://news.mingpao.com/ins/test",
        )
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiOverrideToken1234567890?oc=5"
        monitor.save_article(conn, monitor.Article(
            "明報", title, monitor.now_hk() - timedelta(hours=1), wrapped,
            "測試", "google_mingpao_sitewide", "明報－全站 RSS 後備",
        ))
        conn.commit(); conn.close()
        with patch.object(monitor, "load_url_overrides", return_value=overrides):
            stats = monitor.apply_curated_url_overrides(hours=48)
        self.assertEqual(stats["updated"], 1)
        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], "https://news.mingpao.com/ins/test")
        self.assertEqual(row["original_url"], wrapped)
        self.assertEqual(row["url_resolution_status"], "curated_verified")

    def test_legacy_url_repair_rediscovery_updates_wrapper(self):
        conn = monitor.db_connect()
        article = monitor.Article(
            "香港01", "美國7州供水系統遭網絡攻擊",
            monitor.now_hk() - timedelta(hours=1),
            "https://www.hk01.com/wrong", "測試",
            "google_hk01_sitewide", "香港01－全站 RSS 後備",
        )
        monitor.save_article(conn, article)
        conn.execute(
            "UPDATE articles SET original_url=NULL, url_resolution_status='legacy_unverified'"
        )
        conn.commit(); conn.close()
        wrapper = "https://news.google.com/rss/articles/CBMiRediscoveredToken123456789?oc=5"
        with patch.object(monitor, "rediscover_google_news_wrapper", return_value=wrapper):
            stats = monitor.repair_legacy_unverified_urls(hours=48, max_workers=1)
        self.assertEqual(stats["rediscovered"], 1)
        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], wrapper)
        self.assertEqual(row["original_url"], wrapper)
        self.assertEqual(row["url_resolution_status"], "pending")


    def test_publisher_title_parser_accepts_reversed_meta_attribute_order(self):
        page = '<html><head><meta content="正確文章標題" property="og:title"><title>Fallback</title></head></html>'
        self.assertEqual(monitor._extract_publisher_page_title(page), "正確文章標題")

    def test_redirect_fallback_without_readable_title_is_rejected(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiRedirectToken123456789012?oc=5"
        monitor.save_article(conn, monitor.Article(
            "香港電台", "正確文章標題", monitor.now_hk() - timedelta(hours=1),
            wrapped, "測試", "google_rthk_sitewide", "香港電台－全站 RSS 後備",
        ))
        conn.commit(); conn.close()
        with patch.object(monitor, "decode_google_news_url_offline", return_value=""), \
             patch.object(monitor, "_get_google_decoding_params", return_value=None), \
             patch.object(monitor, "_follow_google_news_redirect", return_value="https://news.rthk.hk/"), \
             patch.object(monitor, "_fetch_publisher_page_title", return_value=""):
            stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=1)
        self.assertEqual(stats["resolved"], 0)
        self.assertEqual(stats["rejected_title_unavailable"], 1)
        self.assertEqual(monitor.query_articles(hours=48)[0]["url"], wrapped)

    def test_curated_rows_are_skipped_by_google_resolver(self):
        conn = monitor.db_connect()
        wrapper = "https://news.google.com/rss/articles/CBMiCuratedToken123456789012?oc=5"
        monitor.save_article(conn, monitor.Article(
            "信報", "OpenAI丨新模型Astra一口氣破解10條數學難題",
            monitor.now_hk() - timedelta(hours=1), wrapper, "測試",
            "google_hkej_sitewide", "信報－全站 RSS 後備",
        ))
        conn.execute(
            "UPDATE articles SET url=?, original_url=?, url_resolution_status='curated_verified'",
            ("https://www.hkej.com/instantnews/test", wrapper),
        )
        conn.commit(); conn.close()
        stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=1)
        self.assertEqual(stats["attempted"], 0)
        self.assertEqual(monitor.query_articles(hours=48)[0]["url"], "https://www.hkej.com/instantnews/test")

    def test_google_refresh_cannot_downgrade_verified_direct_url(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiStableVerifiedToken123456789?oc=5"
        article = monitor.Article(
            "香港01", "已驗證連結不可消失", monitor.now_hk() - timedelta(hours=7),
            wrapped, "第一次摘要", "google_hk01", "香港01－監察關鍵字", category="網騙",
        )
        monitor.save_article(conn, article)
        direct = "https://www.hk01.com/社會新聞/stable-link"
        conn.execute(
            """UPDATE articles SET url=?, original_url=?, url_resolution_status='verified_title',
               url_resolution_checked_at=?""",
            (direct, wrapped, monitor.to_iso(monitor.now_hk() - timedelta(hours=6))),
        )
        conn.commit()

        # A later scheduled Google RSS collection sees the wrapper again.
        refreshed = monitor.Article(
            "香港01", "已驗證連結不可消失", article.published_at, wrapped,
            "更新後摘要", "google_hk01", "香港01－監察關鍵字", category="網騙",
        )
        monitor.save_article(conn, refreshed)
        conn.commit(); conn.close()

        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], direct)
        self.assertEqual(row["original_url"], wrapped)
        self.assertEqual(row["url_resolution_status"], "verified_title")
        self.assertEqual(row["description"], "更新後摘要")

    def test_google_refresh_cannot_replace_authoritative_direct_feed_url(self):
        conn = monitor.db_connect()
        published = monitor.now_hk() - timedelta(hours=7)
        direct = "https://news.rthk.hk/rthk/ch/component/k2/stable.htm"
        monitor.save_article(conn, monitor.Article(
            "香港電台", "官方 RSS 直連不可被 Google wrapper 覆蓋", published, direct,
            "官方摘要", "rthk_direct", "香港電台 RSS", category="網安",
        ))
        wrapped = "https://news.google.com/rss/articles/CBMiStableDirectToken123456789?oc=5"
        monitor.save_article(conn, monitor.Article(
            "香港電台", "官方 RSS 直連不可被 Google wrapper 覆蓋", published, wrapped,
            "Google 摘要", "google_rthk", "Google News", category="網安",
        ))
        conn.commit(); conn.close()

        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], direct)
        self.assertEqual(row["url_resolution_status"], "direct")
        self.assertEqual(row["description"], "Google 摘要")

    def test_resolver_skips_already_verified_google_origin_rows(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiResolverStableToken123456789?oc=5"
        monitor.save_article(conn, monitor.Article(
            "香港經濟日報", "resolver 不可重驗已成功 direct URL",
            monitor.now_hk() - timedelta(hours=7), wrapped, "測試",
            "google_hket", "Google News", category="AI安全",
        ))
        direct = "https://news.hket.com/article/9999999/stable"
        conn.execute(
            "UPDATE articles SET url=?, original_url=?, url_resolution_status='verified_source'",
            (direct, wrapped),
        )
        conn.commit(); conn.close()

        with patch.object(monitor, "_follow_google_news_redirect") as redirect:
            stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=1)
        self.assertEqual(stats["attempted"], 0)
        redirect.assert_not_called()
        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], direct)
        self.assertEqual(row["url_resolution_status"], "verified_source")

    def test_legacy_google_news_url_decodes_offline(self):
        target = "https://example.com/新聞/測試?x=1"
        payload = target.encode("utf-8")
        length = len(payload)
        encoded_length = bytearray()
        while True:
            byte = length & 0x7F
            length >>= 7
            encoded_length.append(byte | (0x80 if length else 0))
            if not length:
                break
        token = base64.urlsafe_b64encode(
            b"\x08\x13\x22" + bytes(encoded_length) + payload + b"\xd2\x01\x00"
        ).decode("ascii").rstrip("=")
        wrapped = f"https://news.google.com/rss/articles/{token}?oc=5"
        self.assertEqual(monitor.decode_google_news_url_offline(wrapped), target)

    def test_modern_google_news_url_uses_single_decoder(self):
        wrapped = "https://news.google.com/rss/articles/CBMiVkFVX3lxTE5ExampleOpaqueToken123456789?oc=5"
        final = "https://example.com/最終新聞"
        with patch.object(monitor, "_batch_decode_google_news_id", return_value=final) as decoder:
            self.assertEqual(monitor.resolve_google_news_url(wrapped), final)
        decoder.assert_called_once()

    def test_resolve_stored_google_urls_updates_database(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiVkFVX3lxTE5ExampleOpaqueToken123456789?oc=5"
        article = monitor.Article(
            "香港01", "Google News 最終網址測試", monitor.now_hk() - timedelta(hours=1),
            wrapped, "詐騙新聞", "google", "Google News", category="網騙",
        )
        monitor.save_article(conn, article); conn.commit(); conn.close()
        final = "https://www.hk01.com/社會新聞/測試"
        params = {"gn_art_id": monitor._google_news_article_id(wrapped), "timestamp": "1725000000", "signature": "sig"}
        with patch.object(monitor, "_get_google_decoding_params", return_value=params), \
             patch.object(monitor, "_batch_decode_google_params", return_value={params["gn_art_id"]: final}):
            stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=2)
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["signed_params"], 1)
        rows = monitor.query_articles(hours=48)
        self.assertEqual(rows[0]["url"], final)

    def test_signed_google_params_and_batch_response_parsing(self):
        article_id = "CBMiiwFBVV95cUxOSDQtdHhibjR1YXZWWmdzM2ZsZmJlQW5OLTRNdEVLa2ExZ2hSNWxVc0Y5LXFZcWRpTFVEbG1JVzRQbTBFcWpZTEhzX3Fjc0FKMWVWNElYZ08zd0pUSmNTWXN3TmdvNWx2SzBqYXlRb1NXdi1zZ1VENkpVWU5HakNfREJENnRfREZ5Wldj"
        page = '<html><c-wiz><div data-n-a-sg="ATR1-test&amp;sig" data-n-a-ts="1725016444"></div></c-wiz></html>'
        params = monitor._extract_google_decoding_params(page, article_id)
        self.assertEqual(params, {"gn_art_id": article_id, "timestamp": "1725016444", "signature": "ATR1-test&sig"})
        nested = json.dumps(["garturlres", "https://example.com/最終新聞", 1])
        envelope = json.dumps([["wrb.fr", "Fbv4je", nested], ["di", 1], ["af.httprm", 1]])
        response = ")]}'\n\n" + envelope
        self.assertEqual(monitor._extract_batchexecute_urls(response), ["https://example.com/最終新聞"])


    def test_multi_item_google_decode_uses_one_http_request_per_item(self):
        params = [
            {"gn_art_id": "opaque-id-one-123456", "timestamp": "1725000001", "signature": "sig1"},
            {"gn_art_id": "opaque-id-two-123456", "timestamp": "1725000002", "signature": "sig2"},
        ]
        payloads = [
            json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://www.hk01.com/a", 1])]]),
            json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://news.rthk.hk/b", 1])]]),
        ]

        class FakeResponse:
            def __init__(self, text): self.text = text
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit=None): return self.text.encode("utf-8")

        with patch.object(monitor.urllib.request, "urlopen", side_effect=[FakeResponse(x) for x in payloads]) as opener:
            decoded = monitor._batch_decode_google_params(params, timeout=3)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(decoded[params[0]["gn_art_id"]], "https://www.hk01.com/a")
        self.assertEqual(decoded[params[1]["gn_art_id"]], "https://news.rthk.hk/b")

    def test_wrong_same_publisher_title_is_rejected_and_wrapper_kept(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiVkFVX3lxTE5AnotherOpaqueToken123456789?oc=5"
        article = monitor.Article(
            "香港經濟日報", "OpenAI調查發現更多AI越獄及模型失控",
            monitor.now_hk() - timedelta(hours=1), wrapped, "AI安全新聞",
            "google_hket", "香港經濟日報－監察關鍵字", category="AI安全",
        )
        monitor.save_article(conn, article); conn.commit(); conn.close()
        params = {"gn_art_id": monitor._google_news_article_id(wrapped), "timestamp": "1725000000", "signature": "sig"}
        wrong = "https://news.hket.com/article/4169887/unrelated"
        with patch.object(monitor, "_get_google_decoding_params", return_value=params), \
             patch.object(monitor, "_batch_decode_google_params", return_value={params["gn_art_id"]: wrong}), \
             patch.object(monitor, "_fetch_publisher_page_title", return_value="全民運動日 李家超與官員落場運動"):
            stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=1)
        self.assertEqual(stats["resolved"], 0)
        self.assertEqual(stats["rejected_title_mismatch"], 1)
        row = monitor.query_articles(hours=48)[0]
        self.assertEqual(row["url"], wrapped)
        self.assertEqual(row["url_resolution_status"], "unresolved")

    def test_resolver_rejects_wrong_publisher_domain(self):
        conn = monitor.db_connect()
        wrapped = "https://news.google.com/rss/articles/CBMiVkFVX3lxTE5WrongDomainOpaqueToken123456?oc=5"
        article = monitor.Article(
            "香港01", "網絡安全測試新聞", monitor.now_hk() - timedelta(hours=1),
            wrapped, "網安", "google_hk01", "香港01－監察關鍵字", category="網安",
        )
        monitor.save_article(conn, article); conn.commit(); conn.close()
        params = {"gn_art_id": monitor._google_news_article_id(wrapped), "timestamp": "1725000000", "signature": "sig"}
        wrong = "https://news.rthk.hk/rthk/ch/component/k2/123.htm"
        with patch.object(monitor, "_get_google_decoding_params", return_value=params), \
             patch.object(monitor, "_batch_decode_google_params", return_value={params["gn_art_id"]: wrong}):
            stats = monitor.resolve_stored_google_news_urls(hours=48, max_workers=1)
        self.assertEqual(stats["rejected_source_mismatch"], 1)
        self.assertEqual(monitor.query_articles(hours=48)[0]["url"], wrapped)

    def test_all_collection_jobs_use_capture_first_policy(self):
        jobs = monitor.build_feed_jobs(monitor.DEFAULT_CONFIG)
        self.assertTrue(jobs)
        self.assertTrue(all(job.get("accept_query_matches") for job in jobs))

    def test_request_fallback_uses_second_url(self):
        calls=[]
        def fake(url, timeout, attempts=2):
            calls.append(url)
            if url.endswith("one"): raise OSError("failed")
            return b"<rss/>"
        with patch.object(monitor, "request_bytes", side_effect=fake):
            content, used = monitor.request_first_available(["https://x/one","https://x/two"], 5)
        self.assertEqual(content,b"<rss/>"); self.assertEqual(used,"https://x/two"); self.assertEqual(len(calls),2)


if __name__ == "__main__": unittest.main()
