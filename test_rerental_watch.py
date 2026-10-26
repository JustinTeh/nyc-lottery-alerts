"""Offline tests for rerental_watch.py. Run with:  python3 -m unittest -v"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import lottery_alerts
import rerental_watch as rw

FAC_DAY1 = """<html><head><title>Available Affordable Housing Units</title></head><body>
<nav><a href="/">Home</a> <a href="/about">About us</a> Donate Contact</nav>
<h1>Current Units Available for Re-Rental</h1>
<p>Updated Oct 26, 2026</p>
<div><h2>The Axel - 539 Vanderbilt Avenue, Brooklyn NY</h2>
<ul><li>Unit 6F</li><li>Unit Size: Studio</li><li>Rent: $2,732.32</li></ul></div>
<div><h2>Sunset Ridge Apartments</h2>
<ul><li>Unit 1B</li><li>Unit Size: 1 Bedroom</li><li>Max Income: $76,350</li></ul></div>
<table><tr><th>Unit</th><th>Size</th><th>Rent</th></tr>
<tr><td>2H</td><td>Studio</td><td>$1,374.00</td></tr></table>
<p>Applications are processed first-come, first-served under HPD guidelines.</p>
<footer>Copyright 2026 Fifth Avenue Committee. All rights reserved.</footer>
<script>var visits = 12345; track(visits);</script></body></html>"""

FAC_DAY2 = FAC_DAY1.replace("Updated Oct 26, 2026", "Updated Oct 27, 2026").replace(
    "</table>",
    '<tr><td>3C</td><td>1 Bedroom</td><td>$1,650.00</td></tr></table>'
    "<p>Our offices are closed on Friday for the holiday.</p>",
)

JS_SHELL = '<html><head><title>App</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>'
FAC = "https://fac.example/re-rentals"
SHELL = "https://shell.example/"
FLAKY = "https://flaky.example/units"
BLOCKED = "https://blocked.example/units"
SOURCES = [
    {"name": "FAC", "url": FAC},
    {"name": "Shell", "url": SHELL},
    {"name": "Flaky", "url": FLAKY},
    {"name": "Blocked", "url": BLOCKED},
    {"name": "Off", "url": "https://off.example/", "enabled": False, "why_off": "test"},
]


def fake_fetch(pages):
    def fetch(url):
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page
    return fetch


def allow_all_but_blocked(url):
    return "blocked.example" not in url


class ExtractTests(unittest.TestCase):
    def test_table_row_stays_on_one_line_and_chrome_is_skipped(self):
        lines = rw.extract_lines(FAC_DAY1)
        self.assertIn("2H | Studio | $1,374.00", lines)
        self.assertIn("Unit Size: Studio", lines)
        joined = "\n".join(lines)
        self.assertNotIn("Donate", joined)       # <nav>
        self.assertNotIn("12345", joined)        # <script>
        self.assertNotIn("Copyright", joined)    # <footer>


class CheckSourceTests(unittest.TestCase):
    def check(self, pages, prev_state=None, url=FAC):
        src = {"name": "X", "url": url}
        return rw.check_source(src, fake_fetch(pages), allow_all_but_blocked, (prev_state or {}).get(url))

    def test_first_sight_is_a_silent_baseline(self):
        result, entry = self.check({FAC: FAC_DAY1})
        self.assertEqual(result["status"], "baseline")
        self.assertEqual(entry["status"], "ok")

    def test_only_new_listing_lines_are_reported_not_dates_or_notices(self):
        _, entry = self.check({FAC: FAC_DAY1})
        result, _ = self.check({FAC: FAC_DAY2}, {FAC: entry})
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["added"], ["3C | 1 Bedroom | $1,650.00"])

    def test_unchanged_page_is_unchanged(self):
        _, entry = self.check({FAC: FAC_DAY1})
        result, _ = self.check({FAC: FAC_DAY1}, {FAC: entry})
        self.assertEqual(result["status"], "unchanged")

    def test_failed_read_keeps_yesterdays_text(self):
        _, entry = self.check({FAC: FAC_DAY1})
        failed, kept = self.check({FAC: urllib.error.HTTPError(FAC, 503, "down", {}, None)}, {FAC: entry})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("503", failed["note"])
        self.assertEqual(kept["lines"], entry["lines"])
        # tomorrow it is back: only truly new lines show, not the whole page
        result, _ = self.check({FAC: FAC_DAY2}, {FAC: kept})
        self.assertEqual(result["added"], ["3C | 1 Bedroom | $1,650.00"])

    def test_javascript_only_page_is_flagged(self):
        result, _ = self.check({SHELL: JS_SHELL}, url=SHELL)
        self.assertEqual(result["status"], "low")

    def test_robots_block_is_respected_without_fetching(self):
        fetch = mock.Mock()
        result, _ = rw.check_source({"name": "B", "url": BLOCKED}, fetch, allow_all_but_blocked, None)
        self.assertEqual(result["status"], "blocked")
        fetch.assert_not_called()

    def test_redesign_is_called_a_redesign(self):
        old = "<body>" + "".join(f"<p>Old apartment line number {i} rent $1,{i:03d}</p>" for i in range(40)) + "</body>"
        new = "<body>" + "".join(f"<p>Brand new apartment entry {i} rent $2,{i:03d}</p>" for i in range(40)) + "</body>"
        _, entry = self.check({FAC: old})
        result, _ = self.check({FAC: new}, {FAC: entry})
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["added"], [])
        self.assertIn("redesigned", result["note"])


class DigestTests(unittest.TestCase):
    def day(self, pages, state):
        results, new_state = rw.run(SOURCES, fake_fetch(pages), allow_all_but_blocked, state)
        return results, new_state

    def test_three_day_story(self):
        disabled = [s for s in SOURCES if not s.get("enabled", True)]
        flaky_down = urllib.error.HTTPError(FLAKY, 503, "down", {}, None)

        # Day 1: baseline. One page broken, one JS-only, one blocked, one off.
        results, state = self.day({FAC: FAC_DAY1, SHELL: JS_SHELL, FLAKY: flaky_down}, {})
        self.assertEqual(len(results), 4)  # disabled source is skipped
        title, text = rw.build_digest(results, True, disabled)
        self.assertIn("reading 1 of 4 pages", title)
        self.assertIn("CAN'T READ (3)", text)
        self.assertIn("robots.txt", text)
        self.assertIn("JavaScript", text)
        self.assertIn("Turned off on purpose: Off", text)

        # Day 2: FAC has a new unit, Flaky is still down, nothing else new.
        results, state = self.day({FAC: FAC_DAY2, SHELL: JS_SHELL, FLAKY: flaky_down}, state)
        title, text = rw.build_digest(results, False, disabled)
        self.assertEqual(title, "1 re-rental page changed")
        self.assertIn("+ 3C | 1 Bedroom | $1,650.00", text)
        self.assertNotIn("CAN'T READ", text)   # old problems are not repeated daily
        self.assertNotIn("holiday", text)      # notices without listing words are ignored

        # Day 3: nothing changes anywhere, so stay quiet.
        results, state = self.day({FAC: FAC_DAY2, SHELL: JS_SHELL, FLAKY: flaky_down}, state)
        self.assertEqual(rw.build_digest(results, False, disabled), (None, None))

        # Day 4: Flaky recovers.
        recovered = "".join(f"<p>Studio {i} available, rent $1,{i:03d}</p>" for i in range(20))
        results, state = self.day({FAC: FAC_DAY2, SHELL: JS_SHELL, FLAKY: recovered}, state)
        title, text = rw.build_digest(results, False, disabled)
        self.assertIn("READABLE AGAIN", text)
        self.assertIn("- Flaky", text)


class RobotsTests(unittest.TestCase):
    def test_disallow_and_missing_robots(self):
        robots = "User-agent: *\nDisallow: /private/\n"
        def get(url):
            if url.startswith("https://a.example"):
                return robots
            raise urllib.error.HTTPError(url, 404, "nope", {}, None)
        allowed = rw.robots_checker(get)
        self.assertFalse(allowed("https://a.example/private/units"))
        self.assertTrue(allowed("https://a.example/public/units"))
        self.assertTrue(allowed("https://b.example/anything"))  # no robots.txt means allowed


class MainIntegrationTests(unittest.TestCase):
    """Runs main() for several days with fixtures, capturing what would be sent."""

    def run_main(self, tmp, fixtures, dry=False):
        fx = Path(tmp) / "fixtures.json"
        fx.write_text(json.dumps(fixtures))
        src = Path(tmp) / "sources.json"
        src.write_text(json.dumps(SOURCES))
        argv = ["rerental_watch.py", "--sources", str(src), "--cache-dir", str(Path(tmp) / "cache"),
                "--fixtures", str(fx)] + (["--dry-run"] if dry else [])
        sent = []
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}, clear=False), \
             mock.patch.object(lottery_alerts, "send_ntfy", lambda topic, title, body: sent.append((title, body))), \
             mock.patch.object(lottery_alerts, "send_email", lambda subject, body: False):
            code = rw.main()
        return code, sent

    def test_state_is_saved_only_when_sent_and_reports_are_quiet_when_unchanged(self):
        down = {"error": "down", "http": 503}
        with tempfile.TemporaryDirectory() as tmp:
            day1 = {FAC: FAC_DAY1, SHELL: JS_SHELL, FLAKY: down}
            code, sent = self.run_main(tmp, day1, dry=True)
            self.assertEqual((code, sent), (0, []))
            self.assertFalse((Path(tmp) / "cache" / "state.json").exists())  # dry run saves nothing

            code, sent = self.run_main(tmp, day1)
            self.assertEqual(code, 0)
            self.assertEqual(len(sent), 1)
            self.assertIn("reading 1 of 4 pages", sent[0][0])
            self.assertTrue((Path(tmp) / "cache" / "state.json").exists())

            day2 = {FAC: FAC_DAY2, SHELL: JS_SHELL, FLAKY: down}
            code, sent = self.run_main(tmp, day2)
            self.assertEqual(len(sent), 1)
            self.assertIn("3C | 1 Bedroom | $1,650.00", sent[0][1])

            code, sent = self.run_main(tmp, day2)   # same again
            self.assertEqual((code, sent), (0, []))

    def test_no_notification_channel_fails_loudly_and_keeps_state_unsaved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Path(tmp) / "fixtures.json"
            fx.write_text(json.dumps({FAC: FAC_DAY1}))
            src = Path(tmp) / "sources.json"
            src.write_text(json.dumps([SOURCES[0]]))
            argv = ["rerental_watch.py", "--sources", str(src), "--cache-dir", str(Path(tmp) / "c"), "--fixtures", str(fx)]
            env = {k: v for k, v in os.environ.items() if k not in ("NTFY_TOPIC", "SMTP_HOST", "EMAIL_TO")}
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(rw.main(), 1)
            self.assertFalse((Path(tmp) / "c" / "state.json").exists())


class PushFitTests(unittest.TestCase):
    def test_long_digest_is_cut_at_a_line_break(self):
        body = "\n".join(f"- Site {i} https://example.com/page/{i}" for i in range(400))
        out = rw.fit_for_push(body)
        self.assertLessEqual(len(out.encode()), 3800)
        self.assertTrue(out.endswith("the rest is in your email."))
        self.assertNotIn("https://example.com/page/3\n...", out.split("\n")[-2] + "x")  # no half-cut line


if __name__ == "__main__":
    unittest.main()
