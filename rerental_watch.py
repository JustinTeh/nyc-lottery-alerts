#!/usr/bin/env python3
"""Daily watcher for the affordable re-rental pages listed on HPD's re-rentals page.

Each company publishes its vacancies on its own website in its own layout, so this
does not try to understand every layout. Once a day it:
  1. checks the site's robots.txt and skips pages that ask bots to stay away,
  2. downloads each page once and pulls out the readable text,
  3. compares it with yesterday's text and shows only the NEW lines that look like
     listings (unit sizes, rents, income limits, "available", and so on),
  4. tells you which pages it could not read (blocked, down, or built with
     JavaScript so there is no text to read).

Yesterday's text is kept in a private GitHub Actions cache, not committed to the repo.
Re-rentals are usually first-come, first-served, so check income and asset limits on the
page and apply quickly. This never logs in and never applies for you.

Usage:
    python rerental_watch.py                 # live run: sends a digest when something changed
    python rerental_watch.py --dry-run       # print the digest, send nothing, save nothing
    python rerental_watch.py --fixtures sample_rerentals_day1.json --cache-dir /tmp/rr --dry-run
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).parent
USER_AGENT = "nyc-lottery-alerts/1.0 (personal use; reads each page once a day)"
ROBOTS_TOKEN = "nyc-lottery-alerts"
MAX_BYTES = 2_000_000
MAX_LINES = 2000
MIN_TEXT_CHARS = 200          # less than this and the page is probably built with JavaScript
MAX_SHOWN = 8                 # new lines shown per page
BIG_CHANGE_SHARE = 0.6        # if this much of the page is new, call it a redesign
PAUSE_SECONDS = 2             # between sites

KEYWORDS = re.compile(
    r"(?i)(studio|\bbr\b|bed|bath|rent|\$\s?\d|income|\bunit|\bapt\b|apartment|available|"
    r"vacan|lottery|deadline|apply|application|sq\.? ?ft)"
)
NOISE = re.compile(
    r"(?i)^\W*(©.*|copyright.*|\d{1,2}[:/.\-]\d{1,2}([:/.\-]\d{2,4})?|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(, \d{4})?)\W*$"
)


# ------------------------------------------------------------ reading a page

class _Text(HTMLParser):
    """Turns HTML into plain lines of text. Table cells on one row stay on one line."""
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "template"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "ul", "ol", "table", "dt", "dd", "header", "main", "aside"}
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.buf, self.skip = [], [], 0

    def _flush(self):
        text = " ".join("".join(self.buf).split()).strip(" |")
        if text:
            self.lines.append(text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in self.CELL:
            if "".join(self.buf).strip():
                self.buf.append(" | ")
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def extract_lines(html):
    parser = _Text()
    parser.feed(html)
    parser.close()
    seen, lines = set(), []
    for line in parser.lines:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines[:MAX_LINES]


def meaningful(line):
    return 4 <= len(line) <= 400 and not NOISE.match(line)


def looks_like_listing(line):
    return bool(KEYWORDS.search(line))


# ------------------------------------------------------------ checking one source

def check_source(src, fetch, allowed, prev):
    """Return (result, new_state_entry). `prev` is yesterday's entry or None."""
    result = {"name": src["name"], "url": src["url"], "status": "unchanged", "added": [],
              "extra": 0, "note": ""}
    try:
        if not allowed(src["url"]):
            result.update(status="blocked", note="its robots.txt asks bots not to read this page")
            return result, _carry(prev, "blocked")
        lines = extract_lines(fetch(src["url"]))
    except Exception as e:  # network errors, 403s, timeouts, odd content types
        result.update(status="failed", note=_short_error(e))
        return result, _carry(prev, "failed")

    chars = sum(len(line) for line in lines)
    if chars < MIN_TEXT_CHARS:
        result.update(status="low", note="almost no readable text (probably loads with JavaScript)")
        return result, _carry(prev, "low")

    entry = {"status": "ok", "lines": lines}
    if prev is None or not prev.get("lines"):
        result["status"] = "baseline"
        return result, entry

    old = set(prev["lines"])
    added = [line for line in lines if line not in old and meaningful(line)]
    if len(lines) >= 20 and len(added) / len(lines) > BIG_CHANGE_SHARE:
        result.update(status="changed", note="most of the page is new, so it was probably redesigned; open it and look")
        return result, entry
    listing = [line for line in added if looks_like_listing(line)]
    if listing:
        result.update(status="changed", added=listing[:MAX_SHOWN], extra=max(0, len(listing) - MAX_SHOWN))
    elif len(added) >= 3:
        result.update(status="changed", added=added[:3], extra=0,
                      note="changed, but nothing in it clearly looks like a listing")
    return result, entry


def _carry(prev, status):
    """A failed read must not wipe yesterday's text, or tomorrow looks 'all new'."""
    kept = dict(prev) if prev else {}
    kept["status"] = status
    return kept


def _short_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"website answered HTTP {e.code}"
    text = str(getattr(e, "reason", e)) or e.__class__.__name__
    return text[:120]


# ------------------------------------------------------------ the whole list

def run(sources, fetch, allowed, state, pause=0.0):
    results, new_state = [], {}
    for i, src in enumerate(s for s in sources if s.get("enabled", True)):
        if pause and i:
            time.sleep(pause)
        result, entry = check_source(src, fetch, allowed, state.get(src["url"]))
        result["was"] = (state.get(src["url"]) or {}).get("status")
        results.append(result)
        new_state[src["url"]] = entry
    return results, new_state


def _item(r):
    lines = [f"- {r['name']}  {r['url']}"]
    lines += [f"  + {line[:160]}" for line in r["added"]]
    if r["extra"]:
        lines.append(f"  (+{r['extra']} more lines on the page)")
    if r["note"]:
        lines.append(f"  ({r['note']})")
    return "\n".join(lines)


def build_digest(results, first_run, disabled):
    changed = [r for r in results if r["status"] == "changed"]
    unreadable = [r for r in results if r["status"] in ("failed", "blocked", "low")]
    # On later days only mention pages whose problem is new (or that just recovered).
    news = unreadable if first_run else [r for r in unreadable if r["was"] != r["status"]]
    recovered = [] if first_run else [r for r in results
                                      if r["was"] in ("failed", "blocked", "low") and r["status"] in ("baseline", "unchanged", "changed")]
    readable = [r for r in results if r["status"] in ("baseline", "unchanged", "changed")]

    if not (first_run or changed or news or recovered):
        return None, None

    out = []
    if first_run:
        title = f"Re-rental watcher is on: reading {len(readable)} of {len(results)} pages"
        out.append(f"Now tracking {len(readable)} of {len(results)} re-rental pages. From tomorrow, "
                   "you'll hear about new postings here.")
    else:
        title = f"{len(changed)} re-rental page{'s' if len(changed) != 1 else ''} changed" if changed \
            else "Re-rental watcher: page status update"
    if changed:
        out += ["", f"CHANGED ({len(changed)})"] + [_item(r) for r in changed]
    if news:
        out += ["", f"CAN'T READ ({len(news)})"] + [f"- {r['name']}: {r['note']}  {r['url']}" for r in news]
    if recovered:
        out += ["", "READABLE AGAIN"] + [f"- {r['name']}" for r in recovered]
    if first_run and disabled:
        out += ["", "Turned off on purpose: " + ", ".join(s["name"] for s in disabled)]
    out += ["", "Re-rentals are usually first-come, first-served. Open the page, confirm the income range "
                "for your household size and the asset limit, then apply."]
    return title, "\n".join(out)


# ------------------------------------------------------------ network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        kind = resp.headers.get("Content-Type", "")
        if "html" not in kind and "text" not in kind:
            raise ValueError(f"not a web page ({kind or 'unknown type'})")
        raw = resp.read(MAX_BYTES)
        return raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")


def robots_checker(get=http_get):
    cache = {}

    def allowed(url):
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in cache:
            parser = urllib.robotparser.RobotFileParser()
            try:
                parser.parse(get(root + "/robots.txt").splitlines())
            except Exception:  # no robots.txt (or unreadable) means nothing is forbidden
                parser.parse([])
            cache[root] = parser
        return cache[root].can_fetch(ROBOTS_TOKEN, url)

    return allowed


def fit_for_push(body, limit=3800):
    """Phone notifications are size-limited; cut at a line break and point to the email."""
    if len(body.encode("utf-8")) <= limit:
        return body
    note = "\n...the rest is in your email."
    kept, size = [], len(note.encode("utf-8"))
    for line in body.split("\n"):
        size += len(line.encode("utf-8")) + 1
        if size > limit:
            break
        kept.append(line)
    return "\n".join(kept) + note


def fixture_fetcher(path):
    data = json.loads(Path(path).read_text())

    def fetch(url):
        item = data.get(url)
        if item is None:
            raise ValueError("no fixture for this page")
        if isinstance(item, dict):
            raise urllib.error.HTTPError(url, item.get("http", 500), item.get("error", "error"), {}, None)
        return item

    return fetch


# ------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sources", default=str(HERE / "sources.json"))
    ap.add_argument("--cache-dir", default=os.environ.get("RERENTAL_CACHE_DIR") or str(HERE / ".rerental_cache"))
    ap.add_argument("--fixtures", help="JSON of {url: html} to read instead of the network (offline test)")
    args = ap.parse_args()

    sources = json.loads(Path(args.sources).read_text())
    disabled = [s for s in sources if not s.get("enabled", True)]
    cache_dir = Path(args.cache_dir)
    state_path = cache_dir / "state.json"
    first_run = not state_path.exists()
    state = {} if first_run else json.loads(state_path.read_text())

    if args.fixtures:
        fetch, allowed, pause = fixture_fetcher(args.fixtures), (lambda url: "blocked.example" not in url), 0
    else:
        fetch, allowed, pause = http_get, robots_checker(), PAUSE_SECONDS

    results, new_state = run(sources, fetch, allowed, state, pause)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{date.today()}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))

    title, digest = build_digest(results, first_run, disabled)
    if digest is None:
        print("Nothing new on the re-rental pages. No alert sent.")
    else:
        print(title)
        print(digest)
        if args.dry_run:
            print("\n[dry run] nothing sent, state not saved.")
            return 0
        import lottery_alerts as la  # reuse the same ntfy and email senders
        sent = False
        topic = os.environ.get("NTFY_TOPIC")
        if topic:
            la.send_ntfy(topic, title, fit_for_push(digest))
            sent = True
        if la.send_email(title, digest):
            sent = True
        if not sent:
            print("No NTFY_TOPIC or SMTP settings found, so no alert was delivered.", file=sys.stderr)
            return 1

    if not args.dry_run:
        cache_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
