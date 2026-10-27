"""Offline tests for giveaway_finder.py.  Run: python -m unittest discover -s tests -v"""

import contextlib
import enum
import inspect
import io
import json
import os
import sys
import tempfile
import types
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import giveaway_finder as gf  # noqa: E402

CHANNEL_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "SMTP_PORT",
                "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO", "APIFY_TOKEN")

REGION_CASES = [
    ("Gewinnspiel! Teilnahme nur mit Wohnsitz in Deutschland. Folgt uns!", True, "open to DE ✓"),
    ("Verlosung 🎁 Teilnahme ab 18, nur DE/AT/CH. Kommentiert!", True, "open to DE ✓"),
    ("Gewinnspiel nur für 🇩🇪🇦🇹 – kommentiert ein Herz", True, "open to DE ✓"),
    ("Verlosung: Versand nur innerhalb der EU. Kommentiert", True, "open to DE ✓"),
    ("Gewinnspiel – Versand nur nach Österreich und Deutschland", True, "open to DE ✓"),
    ("Giveaway! Worldwide shipping 🌍 comment below", True, "worldwide ✓"),
    ("Gewinnspiel weltweit! Kommentiert", True, "worldwide ✓"),
    ("Gewinnspiel – nur für Österreich! Kommentiere und folge", False, "only AT"),
    ("Verlosung! Teilnahme nur in der Schweiz möglich. Kommentiert", False, "only CH"),
    ("GIVEAWAY 🎁 US only! Follow + comment", False, "only US"),
    ("Gewinnspiel 🇺🇸 only – comment", False, "only US"),
    ("Giveaway for USA residents! Follow us", False, "only US"),
    ("Giveaway, ships to US and Canada only, follow us", False, "only CA/US"),
    ("Giveaway! Open to UK residents only. Tag a friend", False, "only UK"),
    ("Giveaway for my Turkish followers - only Türkiye", False, "only TR"),
    ("Gewinnspiel: Wir verlosen eine Reise nach Italien! Nur noch 3 Tage. Kommentiert", True, "region not stated"),
    ("Nur für kurze Zeit: Reise nach Spanien gewinnen! Folgt uns", True, "region not stated"),
    ("GEWINNSPIEL! NUR HEUTE! FOLGT UNS UND KOMMENTIERT", True, "region not stated"),
    ("LOOK AT THIS GIVEAWAY, ONLY AT OUR SHOP, FOLLOW US", True, "region not stated"),
    ("GIVEAWAY TIME! ONLY FOLLOWERS CAN WIN, COMMENT AND FOLLOW US", True, "region not stated"),
    ("Gewinnspiel ca. 50€ Wert, nur ca. 2 Tage. Kommentiert", True, "region not stated"),
    ("Gewinnspiel 🇩🇪 Kommentiert und folgt!", True, "region not stated"),
    ("Kommentiere nur ein Emoji und folge uns! Gewinnspiel", True, "region not stated"),
    ("Gewinnspiel, Versand kostenlos, kommentiert", True, "region not stated"),
]


class RegionTests(unittest.TestCase):
    def test_spec_cases(self):
        for text, eligible, label in REGION_CASES:
            with self.subTest(text=text):
                self.assertEqual(gf.region_check(text, {"DE"}), (eligible, label))

    def test_uk_allowed(self):
        text = "Giveaway! Open to UK residents only"
        self.assertEqual(gf.region_check(text, {"UK"}), (True, "open to UK ✓"))
        self.assertEqual(gf.region_check(text, ["GB"]), (True, "open to UK ✓"))

    def test_empty_allowed_turns_check_off(self):
        self.assertEqual(gf.region_check("GIVEAWAY 🎁 US only! Follow + comment", []), (True, ""))

    def test_dotted_us(self):
        self.assertEqual(gf.region_check("Giveaway! Ships to the U.S. only. Comment", {"DE"}), (False, "only US"))


class ParsingTests(unittest.TestCase):
    def test_deadline_with_weekday(self):
        created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        self.assertEqual(gf.parse_deadline("Teilnahmeschluss: Sonntag, 02.11.", created),
                         datetime(2026, 11, 2, 23, 59, tzinfo=gf.BERLIN))

    def test_deadline_rolls_into_next_year(self):
        created = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)
        self.assertEqual(gf.parse_deadline("Gewinnspiel bis 05.01.", created),
                         datetime(2027, 1, 5, 23, 59, tzinfo=gf.BERLIN))

    def test_deadline_with_year_and_invalid_date(self):
        created = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.assertEqual(gf.parse_deadline("läuft bis zum 30.10.26", created).date(), date(2026, 10, 30))
        self.assertEqual(gf.parse_deadline("Ende: 15.11.2026", created).date(), date(2026, 11, 15))
        self.assertIsNone(gf.parse_deadline("Gewinnspiel bis 31.02.", created))
        self.assertIsNone(gf.parse_deadline("Gewinnspiel, kommentiert!", created))

    def test_deadline_phrasings_seen_in_real_posts(self):
        created = datetime(2026, 10, 25, tzinfo=timezone.utc)
        cases = {
            "Gewinnspielende: 01.11.2026 um 23:59 Uhr": date(2026, 11, 1),
            "Teilnahmeschluss: [1.11.2026, 23:59 Uhr]": date(2026, 11, 1),
            "Teilnahmeschluss: Sonntag, [01.11.2026] um 23:59 Uhr": date(2026, 11, 1),
            "Das Gewinnspiel endet am Sonntag, den 08. November um 23:59 Uhr": date(2026, 11, 8),
            "Das Gewinnspiel läuft bis Sonntag den 1. November 2026, 23:59 Uhr": date(2026, 11, 1),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(gf.parse_deadline(text, created).date(), expected)

    def test_prize_count_real_phrasings(self):
        self.assertEqual(gf.count_prizes("Ich verlose 11 Produkte aus dem Adventskalender an eine glückliche Person!"), 1)
        self.assertEqual(gf.count_prizes("Wir verlosen 3 Pakete an drei Gewinner"), 3)
        self.assertEqual(gf.count_prizes("habt ihr jetzt die Chance 1 von 6 Probierpaketen zu gewinnen"), 6)

    def test_dm_flag_only_when_you_are_asked_to_write(self):
        for text in ["Der Gewinner wird per DM benachrichtigt.", "Die Gewinnerin wird per Direktnachricht kontaktiert",
                     "und einen 50 € dm-Gutschein", "Teilnahmebedingungen: https://www.dm.de/social/x",
                     "Diesen Beitrag per Privatnachricht an einen Freund weiterleiten"]:
            with self.subTest(text=text):
                self.assertNotIn("asks you to DM", gf.scam_flags(text))
        for text in ["Schreib mir eine DM, um deinen Gewinn zu erhalten", "Melde dich per DM bei mir",
                     "DM me to claim your prize", "Schickt uns eine Nachricht mit eurer Adresse"]:
            with self.subTest(text=text):
                self.assertIn("asks you to DM", gf.scam_flags(text))

    def test_prize_count(self):
        self.assertEqual(gf.count_prizes("Ich verlose 50€ Gutschein"), 1)
        self.assertEqual(gf.count_prizes("3x 100€ Gutschein"), 3)
        self.assertEqual(gf.count_prizes("Verlosung 🎁 10 Personen gewinnen einen Gutschein"), 10)
        self.assertEqual(gf.count_prizes("Wir verlosen 50 Euro"), 1)
        self.assertEqual(gf.count_prizes("verlosen wir 2 Kinokarten"), 2)
        self.assertEqual(gf.count_prizes("Wir verlosen 100 Tickets"), 1)  # above the cap of 20

    def test_winner_announced_is_not_ended(self):
        text = "GIVEAWAY Win a PS5! Follow + comment. Winner announced Friday"
        self.assertTrue(gf.is_giveaway(text))
        self.assertFalse(gf.is_ended(text))

    def test_ended_giveaway_is_dropped(self):
        text = "Gewinnspiel ist beendet! Gewinnerin steht fest"
        self.assertTrue(gf.is_ended(text))
        self.assertFalse(gf.is_giveaway(text) and not gf.is_ended(text))

    def test_hashtag_alone_is_not_a_giveaway(self):
        self.assertFalse(gf.is_giveaway("Mein Outfit heute #ootd #gewinnspiel"))

    def test_scam_flags(self):
        flags = gf.scam_flags("Link in der Bio! Schreib mir per WhatsApp, nur Versandkosten zahlen, Bitcoin")
        self.assertEqual(flags, ["asks you to click a bio link", "asks you to DM",
                                 "moves you to WhatsApp/Telegram", "mentions fees/shipping costs",
                                 "crypto / gift-card wording"])
        self.assertEqual(gf.scam_flags("Gewinnspiel, kommentiert!"), [])


class ScoringTests(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(gf.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(gf.percentile([5, 1, 4, 2, 3], 30), 2.2)
        self.assertEqual(gf.percentile([7], 30), 7)

    def test_projection(self):
        f = lambda t: 1 - 2.718281828459045 ** (-t / 36)  # noqa: E731
        self.assertAlmostEqual(gf.project_comments(10, 5, 168, 36), 10 * f(168) / f(5), places=6)
        self.assertEqual(gf.project_comments(500, 200, 168, 36), 500)
        self.assertEqual(gf.project_comments(0, 2, 168, 36), 1)

    def test_cutoff_methods(self):
        cfg = dict(gf.DEFAULT_CONFIG)
        cutoff, method = gf.compute_cutoff(list(range(1, 11)), cfg)
        self.assertAlmostEqual(cutoff, 3.7)
        self.assertEqual(method, "the cheapest 30% of 10 giveaways seen in the last 30 days")
        cutoff, method = gf.compute_cutoff([10, 30, 20], cfg)
        self.assertEqual(cutoff, 20)
        self.assertIn("median", method)
        self.assertEqual(gf.compute_cutoff([], cfg)[0], None)
        cutoff, method = gf.compute_cutoff(list(range(100, 200)), {**cfg, "hard_max_per_prize": 50})
        self.assertEqual(cutoff, 50)
        self.assertIn("hard_max_per_prize", method)

    def test_history_overwrites_and_prunes(self):
        today = date(2026, 10, 27)
        history = {
            "old": {"date": "2026-09-26", "per_prize": 1.0},
            "edge": {"date": "2026-09-27", "per_prize": 2.0},
            "p1": {"date": "2026-10-20", "per_prize": 99.0},
            "broken": {"date": "yesterday", "per_prize": 3.0},
        }
        out = gf.update_history(history, [{"id": "p1", "per_prize": 12.345}], today, 30)
        self.assertEqual(out, {"edge": {"date": "2026-09-27", "per_prize": 2.0},
                               "p1": {"date": "2026-10-27", "per_prize": 12.35}})

    def test_seen_prune(self):
        seen = {"a": "2026-08-27", "b": "2026-08-28", "c": "garbage"}
        self.assertEqual(gf.prune_seen(seen, date(2026, 10, 27)), {"b": "2026-08-28"})


def make_giveaway(i, **overrides):
    g = {
        "id": str(i), "author": f"author_{i}", "verified": i % 2 == 0, "prizes": 1 + i % 3,
        "region": "open to DE ✓", "per_prize": 50.0 + i, "chance": 1 / (50.0 + i), "comments": 1234 * i,
        "deadline": datetime(2026, 11, 2, 23, 59, tzinfo=gf.BERLIN), "age_hours": 5 + i * 3,
        "text": "GEWINNSPIEL <3 & mehr! " * 10, "flags": ["asks you to DM", "mentions fees/shipping costs"],
        "url": f"https://www.tiktok.com/@author_{i}/video/{7_000_000_000_000_000_000 + i}?a=1&b=2",
    }
    g.update(overrides)
    return g


class TagChecker(HTMLParser):
    VOID = {"br", "meta", "img", "hr", "link", "input"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.counts = [], [], Counter()

    def handle_starttag(self, tag, attrs):
        self.counts[tag] += 1
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


class OutputTests(unittest.TestCase):
    def report(self, n, max_len=30, wrong_region=0):
        cfg = {**gf.DEFAULT_CONFIG, "max_list_length": max_len}
        stats = {**dict.fromkeys(gf.STAT_KEYS, 0), "active": n, "fetched": 3 * n, "wrong_region": wrong_region}
        selected = [make_giveaway(i) for i in range(1, n + 1)]
        return gf.build_report(date(2026, 10, 27), stats, selected, cfg, 81.6, "the median", {"2": "2026-10-20"})

    def assert_html_ok(self, text):
        checker = TagChecker()
        checker.feed(text)
        checker.close()
        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])
        return checker.counts

    def test_entry_format_matches_spec(self):
        g = make_giveaway(
            1, author="kerzenliebe", verified=True, prizes=3, per_prize=82.3, comments=140, age_hours=30,
            text="🎉 GEWINNSPIEL 🎉 Wir verlosen 3x unsere neue Duftkerze!\n Folgt uns, liked & kommentiert "
                 "und gewinnt mit etwas Glück", flags=["asks you to DM"])
        e = gf.make_entry(g, is_new=True)
        self.assertEqual(e.title, "🆕 @kerzenliebe ✔️ · 3 prizes · open to DE ✓")
        self.assertEqual(e.stats, "~82 competitors per prize (≈1 in 82) · 140 comments now · ends 02.11. · posted 1d ago")
        self.assertEqual(e.caption, "🎉 GEWINNSPIEL 🎉 Wir verlosen 3x unsere neue Duftkerze! Folgt uns, liked & kommentiert und…")
        self.assertEqual(len(e.caption), 90)
        self.assertEqual(e.warning, "⚠️ asks you to DM")
        e = gf.make_entry({**g, "prizes": 1, "deadline": None, "age_hours": 5, "flags": [], "comments": 12345,
                           "verified": False}, is_new=False)
        self.assertEqual(e.title, "@kerzenliebe · 1 prize · open to DE ✓")
        self.assertIn("12.345 comments now · no date found · posted 5h ago", e.stats)
        self.assertEqual(e.warning, "")

    def test_summary_and_footer(self):
        r = self.report(35, wrong_region=2)
        self.assertEqual(r.summary, "35 active giveaways found (105 posts checked). 2 skipped because they're "
                                    "limited to other countries. 35 are at or below 82 competitors per prize - the median.")
        self.assertEqual(r.footer, "…and 5 more (raise max_list_length to see them).")
        self.assertNotIn("skipped", self.report(3).summary)
        self.assertEqual(self.report(0).footer, "Nothing below the cutoff today.")
        self.assertTrue(self.report(1).entries[0].title.startswith("🆕"))  # id "1" is new
        self.assertFalse(self.report(2).entries[1].title.startswith("🆕"))  # id "2" was seen before

    def test_telegram_chunks(self):
        chunks = gf.render_telegram(self.report(30))
        self.assertGreater(len(chunks), 1)
        for i, chunk in enumerate(chunks):
            self.assertLess(len(chunk), 3900)
            self.assert_html_ok(chunk)
            if i:
                self.assertTrue(chunk.startswith('<a href="'))  # split only at entry boundaries
        joined = "\n\n".join(chunks)
        self.assertEqual(joined.count("<a href="), 30)
        self.assertIn("&lt;3 &amp; mehr", joined)
        self.assertIn("?a=1&amp;b=2", joined)

    def test_email_html_parses(self):
        report = self.report(30)
        counts = self.assert_html_ok(gf.render_email_html(report))
        self.assertEqual(counts["ol"], 1)
        self.assertEqual(counts["li"], 30)
        self.assertIn("estimates", gf.render_email_html(report))
        self.assertEqual(gf.email_subject(report), "🎁 30 TikTok giveaways - 27.10.2026")

    def test_markdown_report(self):
        md = gf.render_markdown(self.report(2))
        self.assertTrue(md.startswith("# 🎁 TikTok giveaways - 27.10.2026"))
        self.assertIn("[1. 🆕 @author\\_1 · 2 prizes · open to DE ✓](https://www.tiktok.com/@author_1/", md)
        self.assertIn("*GEWINNSPIEL \\<3 & mehr!", md)


class DeliveryTests(unittest.TestCase):
    def test_empty_env_is_unset(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "EMAIL_TO": "a@x.de, b@x.de",
                                          "SMTP_PORT": "", "SMTP_USER": "", "SMTP_PASSWORD": ""}):
            settings = gf.email_settings()
        self.assertEqual(settings["port"], 587)
        self.assertIsNone(settings["user"])
        self.assertEqual(settings["to"], ["a@x.de", "b@x.de"])
        with mock.patch.dict(os.environ, {"SMTP_HOST": "", "EMAIL_TO": ""}):
            self.assertIsNone(gf.email_settings())

    def test_send_email_uses_starttls_and_multipart(self):
        report = OutputTests().report(2)
        settings = {"host": "smtp.example.com", "port": 587, "user": "me@x.de", "password": "pw", "to": ["you@x.de"]}
        with mock.patch("smtplib.SMTP") as smtp_cls:
            gf.send_email(settings, report)
        smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=60)
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("me@x.de", "pw")
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(msg["Subject"], "🎁 2 TikTok giveaways - 27.10.2026")

    def test_send_telegram_request(self):
        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            sent.append(request)
            return Response(b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            gf.send_telegram("123:abc", "42", ["one", "two"])
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].full_url.endswith("/bot123:abc/sendMessage"))
        body = sent[0].data.decode()
        self.assertIn("parse_mode=HTML", body)
        self.assertIn("disable_web_page_preview=true", body)


def make_sample_items(now):
    def ts(hours_ago):
        return int((now - timedelta(hours=hours_ago)).timestamp())

    def ddmm(days):
        return (now + timedelta(days=days)).astimezone(gf.BERLIN).strftime("%d.%m.")

    def item(pid, text, comments, hours_ago, fans, verified=False, iso=False):
        out = {"id": pid, "text": text, "commentCount": comments, "playCount": comments * 40,
               "webVideoUrl": f"https://www.tiktok.com/@user{pid}/video/{pid}",
               "authorMeta": {"name": f"user{pid}", "fans": fans, "verified": verified}}
        if iso:
            out["createTimeISO"] = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        else:
            out["createTime"] = ts(hours_ago)
        return out

    first = item("1001", "🎉 GEWINNSPIEL 🎉 Wir verlosen 3x unsere neue Duftkerze! Folgt uns, liked & kommentiert. "
                         f"Teilnahme nur in Deutschland. Teilnahmeschluss: {ddmm(6)}", 140, 26, 5400, True)
    return [
        first,
        item("1002", "Verlosung 🎁 Wir verlosen 2 Gutscheine! Kommentiert eure Lieblingsfarbe. Link in Bio", 900, 50, 12000),
        item("1003", "GIVEAWAY Win a PS5! Follow + comment. Winner announced Friday", 25000, 30, 800000, True),
        item("1004", "Gewinnspiel 🇩🇪 Kommentiert und folgt! Schreib mir eine DM", 40, 5, 900, iso=True),
        item("1005", f"Gewinnspiel: 5 Gewinner bekommen je ein Buch! Folgt uns und kommentiert. Endet am {ddmm(3)}",
             300, 72, 3000),
        item("1006", "Wir verschenken eine Kaffeemaschine ☕ Folgt & teilt das Video! Versand nur innerhalb der EU",
             2200, 100, 45000),
        item("1007", "Gewinnspiel weltweit! Kommentiert mit 🍀", 60, 10, 1500),
        item("1008", "VERLOSUNG 🎄 10 Personen gewinnen einen 20€ Gutschein. Markiert eine Freundin!", 4000, 150, 70000),
        item("1009", f"Gewinnspiel! Folgt uns, wir verlosen ein Paar Sneaker bis {ddmm(2)}", 1100, 60, 25000),
        item("1010", "Giveaway deutschland 🎁 Comment + follow, Versandkosten trägt der Gewinner", 15, 3, 450),
        dict(first),  # same post from a second seed
        {"error": "Post not available", "url": "https://www.tiktok.com/@gone/video/1"},
        item("1011", "Gewinnspiel ist beendet! Die Gewinnerin steht fest. Danke fürs Mitmachen", 800, 40, 9000),
        item("1012", "Gewinnspiel! Kommentiert und folgt", 50, 20 * 24, 9000),
        item("1013", f"Gewinnspiel bis {ddmm(-1)}! Kommentiert und folgt", 70, 4 * 24, 9000),
        item("1014", "GIVEAWAY 🎁 US only! Follow + comment", 300, 20, 9000),
        item("1015", "Mein Outfit heute #ootd #gewinnspiel", 30, 10, 9000),
        item("1016", "Gewinnspiel! Kommentiert", 5, 10, 50),
    ]


EXPECTED_STATS = {"fetched": 16, "not_giveaway": 1, "ended": 2, "wrong_region": 1,
                  "too_old": 1, "small_account": 1, "active": 10}


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.patches = [
            mock.patch.object(gf, "STATE_DIR", self.state),
            mock.patch.object(gf, "REPORT_PATH", self.tmp / "latest_report.md"),
            mock.patch.object(gf, "CONFIG_PATH", self.tmp / "missing-config.json"),  # use the defaults
            mock.patch.dict(os.environ, {k: "" for k in CHANNEL_VARS}),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.items = make_sample_items(datetime.now(timezone.utc))

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gf.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_pipeline_stats(self):
        posts = gf.normalize_items(self.items)
        active, stats = gf.process_posts(posts, gf.DEFAULT_CONFIG, datetime.now(timezone.utc))
        self.assertEqual(stats, EXPECTED_STATS)
        by_id = {g["id"]: g for g in active}
        self.assertEqual(by_id["1001"]["prizes"], 3)
        self.assertEqual(by_id["1005"]["prizes"], 5)
        self.assertEqual(by_id["1008"]["prizes"], 10)
        self.assertEqual(by_id["1006"]["region"], "open to DE ✓")
        self.assertEqual(by_id["1007"]["region"], "worldwide ✓")
        self.assertEqual(by_id["1004"]["flags"], ["asks you to DM"])
        self.assertIsNone(by_id["1003"]["deadline"])

    def test_sample_run_prints_ranked_list_and_writes_no_state(self):
        sample = self.tmp / "sample.json"
        sample.write_text(json.dumps(self.items), encoding="utf-8")
        code, out, err = self.run_main(["--sample", str(sample)])
        self.assertEqual(code, 0)
        self.assertIn("10 active giveaways found (16 posts checked). 1 skipped because they're limited to "
                      "other countries. 3 are at or below", out)
        self.assertIn("the cheapest 30% of 10 giveaways seen in the last 30 days.", out)
        self.assertIn("fetched=16, not_giveaway=1, ended=2, wrong_region=1, too_old=1, small_account=1, active=10", err)
        odds = [int(part.split("≈1 in ")[1].split(")")[0].replace(".", "")) for part in out.split("\n")
                if "≈1 in " in part]
        self.assertEqual(len(odds), 3)
        self.assertEqual(odds, sorted(odds))
        self.assertFalse(self.state.exists())
        self.assertTrue((self.tmp / "latest_report.md").exists())

    def test_dry_run_writes_no_state(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}), mock.patch.object(gf, "fetch_items",
                                                                                  return_value=self.items):
            code, out, _ = self.run_main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("10 active giveaways found", out)
        self.assertFalse(self.state.exists())

    def test_missing_token(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main([])
        self.assertIn("console.apify.com", str(ctx.exception.code))

    def test_mocked_apify_v3_run(self):
        calls = {}
        items = self.items

        class Status(enum.Enum):
            SUCCEEDED = "SUCCEEDED"

        class Run:  # mimics the pydantic Run model of apify-client 3.x
            status = Status.SUCCEEDED
            default_dataset_id = "dataset-1"
            usage_total_usd = 0.42

        class Actor:
            def call(self, **kwargs):
                calls.update(kwargs)
                return Run()

        class Dataset:
            def iterate_items(self):
                return iter(items)

        class User:
            def limits(self):
                return fake_account(1.2)

        class Client:
            def __init__(self, token):
                calls["token"] = token

            def user(self):
                return User()

            def actor(self, actor_id):
                calls["actor"] = actor_id
                return Actor()

            def dataset(self, dataset_id):
                calls["dataset"] = dataset_id
                return Dataset()

        fake_module = types.ModuleType("apify_client")
        fake_module.ApifyClient = Client
        raw = self.tmp / "raw.json"
        with mock.patch.dict(sys.modules, {"apify_client": fake_module}), \
                mock.patch.dict(os.environ, {"APIFY_TOKEN": "apify_api_test"}):
            code, out, err = self.run_main(["--save-raw", str(raw)])

        self.assertEqual(code, 0)
        self.assertEqual(calls["token"], "apify_api_test")
        self.assertEqual(calls["actor"], "clockworks/tiktok-scraper")
        self.assertEqual(calls["dataset"], "dataset-1")
        run_input = calls["run_input"]
        self.assertEqual(run_input["proxyCountryCode"], "DE")
        self.assertEqual(run_input["searchSection"], "/video")
        self.assertNotIn("videoSearchSorting", run_input)  # returns no posts as of October 2026
        self.assertEqual(run_input["resultsPerPage"], 20)
        self.assertFalse(run_input["shouldDownloadVideos"])
        self.assertEqual(calls["max_items"], 120)
        self.assertEqual(calls["max_total_charge_usd"], Decimal("0.5"))
        self.assertEqual(calls["run_timeout"], timedelta(minutes=20))
        self.assertIn("Apify usage this billing cycle: $1.20 of $5.00.", err)
        self.assertIn("SUCCEEDED, cost $0.42", err)
        self.assertIn("3 are at or below", out)  # no channel configured -> printed

        seen = json.loads((self.state / "seen.json").read_text(encoding="utf-8"))
        history = json.loads((self.state / "history.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(history), 10)
        self.assertEqual(len(json.loads(raw.read_text(encoding="utf-8"))), len(items))

        # Second run: the same posts are no longer new.
        with mock.patch.dict(sys.modules, {"apify_client": fake_module}), \
                mock.patch.dict(os.environ, {"APIFY_TOKEN": "apify_api_test"}):
            _, out, _ = self.run_main([])
        self.assertNotIn("🆕", out)

    def test_run_without_posts_is_an_error(self):
        client = mock.Mock()
        client.user.return_value.limits.return_value = fake_account(0.0)
        client.actor.return_value.call.return_value = {
            "status": "SUCCEEDED", "statusMessage": "Scraped 0/3 search queries", "defaultDatasetId": "d"}
        client.dataset.return_value.iterate_items.return_value = iter([])
        fake_module = types.ModuleType("apify_client")
        fake_module.ApifyClient = lambda token: client
        with mock.patch.dict(sys.modules, {"apify_client": fake_module}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError) as ctx:
            gf.fetch_items(gf.DEFAULT_CONFIG, "token")
        self.assertIn("returned no posts (Scraped 0/3 search queries)", str(ctx.exception))

    def test_dict_run_with_camel_case_keys(self):
        run = {"status": "SUCCEEDED", "defaultDatasetId": "d", "usageTotalUsd": 0.1}
        self.assertEqual(gf._field(run, "default_dataset_id", "defaultDatasetId"), "d")
        self.assertEqual(gf._field(run, "usage_total_usd", "usageTotalUsd"), 0.1)
        self.assertIsNone(gf._field(None, "a", "a"))


def fake_account(used, limit=5.0):
    """Shape of apify-client 3.x AccountLimits (client.user().limits())."""
    return types.SimpleNamespace(
        current=types.SimpleNamespace(monthly_usage_usd=used),
        limits=types.SimpleNamespace(max_monthly_usage_usd=limit),
        monthly_usage_cycle=types.SimpleNamespace(end_at=datetime(2026, 11, 26, 23, 59, tzinfo=timezone.utc)),
    )


class BudgetTests(unittest.TestCase):
    def client(self, account=None, error=None):
        user = mock.Mock()
        user.limits.side_effect = error
        user.limits.return_value = account
        client = mock.Mock()
        client.user.return_value = user
        return client

    def check(self, client, **cfg):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = gf.within_budget(client, {**gf.DEFAULT_CONFIG, **cfg})
        return ok, err.getvalue()

    def test_runs_while_usage_plus_cap_fits(self):
        ok, log = self.check(self.client(fake_account(4.5)))
        self.assertTrue(ok)
        self.assertIn("$4.50 of $5.00", log)

    def test_skips_when_a_run_could_exceed_the_plan(self):
        ok, log = self.check(self.client(fake_account(4.51)))
        self.assertFalse(ok)
        self.assertIn("resets on 27.11.2026", log)  # 23:59 UTC is already the next day in Berlin

    def test_own_budget_below_plan_limit(self):
        self.assertFalse(self.check(self.client(fake_account(2.6)), monthly_budget_usd=3)[0])
        self.assertTrue(self.check(self.client(fake_account(2.5)), monthly_budget_usd=3)[0])

    def test_unreadable_usage_falls_back_to_cap(self):
        ok, log = self.check(self.client(error=RuntimeError("insufficient permissions")))
        self.assertTrue(ok)
        self.assertIn("could not read the Apify usage", log)


class ProjectFileTests(unittest.TestCase):
    # Free-plan prices of clockworks/tiktok-scraper (October 2026): result and German proxy
    # are charged per result, plus a fee per run.
    PER_RESULT_USD = 0.0037 + 0.0013
    PER_RUN_USD = 0.001

    def test_config_json_keys_are_known(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual({k for k in cfg if not k.startswith("_")}, set(gf.DEFAULT_CONFIG))

    def test_config_json_fits_the_free_plan(self):
        cfg = gf.load_config(ROOT / "config.json")
        posts = (len(cfg["hashtags"]) + len(cfg["search_queries"])) * cfg["results_per_seed"]
        per_run = posts * self.PER_RESULT_USD + self.PER_RUN_USD
        # 31 daily runs leave room for a few manual runs within the $5 free credit.
        self.assertLess(31 * per_run, 4.0)
        self.assertEqual(cfg["max_cost_per_run_usd"], 0.5)  # the actor rejects lower caps

    @unittest.skipUnless(__import__("importlib.util").util.find_spec("apify_client"), "apify-client not installed")
    def test_real_client_accepts_call_options(self):
        from apify_client import ApifyClient
        call = type(ApifyClient("token").actor("clockworks/tiktok-scraper")).call
        inspect.signature(call).bind(None, run_input={}, max_items=1,
                                     max_total_charge_usd=Decimal("0.5"), run_timeout=timedelta(minutes=20))

    @unittest.skipUnless(__import__("importlib.util").util.find_spec("yaml"), "PyYAML not installed")
    def test_workflow_yaml(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8"))
        triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
        self.assertEqual(triggers["schedule"], [{"cron": "37 6 * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(wf["permissions"], {"contents": "write"})
        job = wf["jobs"]["run"]
        self.assertEqual(job["timeout-minutes"], 30)
        steps = job["steps"]
        self.assertEqual(steps[0]["uses"], "actions/checkout@v4")
        self.assertEqual(steps[1]["uses"], "actions/setup-python@v5")
        self.assertEqual(steps[1]["with"]["python-version"], "3.12")
        run_step = next(s for s in steps if s.get("run") == "python giveaway_finder.py")
        self.assertEqual(set(run_step["env"]), set(CHANNEL_VARS))
        self.assertIn("git diff --cached --quiet ||", steps[-1]["run"])


if __name__ == "__main__":
    unittest.main()
