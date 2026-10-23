#!/usr/bin/env python3
"""Daily NYC Housing Connect lottery alerts.

Once a day this makes ONE request to Housing Connect: the same search the
website runs when you set a household size and income. The site returns the
open lotteries your household can apply to. We then:
  - drop anything outside your boroughs or bedroom sizes,
  - enrich each match with the city's Open Data feed (share of units in each
    income band) when that lottery is in the feed, and
  - send a digest by push notification (ntfy.sh) and/or email.

If Housing Connect can't be reached, it falls back to the city feed alone.
It never logs in and never submits anything: you apply yourself from the link.

Matches are tagged:
  LIKELY FIT       the city feed shows units in your higher income bands
  CHECK INCOME     the feed shows only units in a mixed band; some may cap below you
  NOT IN CITY DATA the lottery isn't in the feed yet, so we have no band details
Housing Connect's list only gives one income range across ALL household sizes
and no asset limits. Always confirm both on the listing page.

Usage:
    python lottery_alerts.py                  # live run, sends alerts, updates seen.json
    python lottery_alerts.py --dry-run        # print the digest, send nothing, save nothing
    python lottery_alerts.py --hc-fixture sample_hc.json --fixture sample_data.json --dry-run
"""
import argparse
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import date
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).parent
HC_API = "https://a806-housingconnectapi.nyc.gov/HPDPublicAPI/api/Lottery/SearchLotteries"
FEED_API = "https://data.cityofnewyork.us/resource/vy5i-a666.json"
SEARCH_URL = "https://housingconnect.nyc.gov/PublicWeb/search-lotteries"
DETAIL_URL = "https://housingconnect.nyc.gov/PublicWeb/details/{id}"
USER_AGENT = "nyc-lottery-alerts/1.0 (personal use, one request per day)"

BOROUGH_NAMES = {
    "BK": "Brooklyn",
    "BX": "Bronx",
    "QN": "Queens",
    "MN": "Manhattan",
    "SI": "Staten Island",
    "Multiple": "Multiple boroughs",
}
BOROUGH_CODES = {v: k for k, v in BOROUGH_NAMES.items()}
INCOME_FIELDS = {
    "extremely_low": "applied_income_ami_ext_low",    # up to 30% AMI
    "very_low": "applied_income_ami_very_low",        # 31-50%
    "low": "applied_income_ami_low",                  # 51-80%
    "moderate": "applied_income_ami_moderate",        # 81-120%
    "middle": "applied_income_ami_middle",            # 121-165%
    "above": "applied_income_ami_above",              # over 165%
}
BAND_LABELS = {
    "extremely_low": "up to 30% AMI",
    "very_low": "31-50% AMI",
    "low": "51-80% AMI",
    "moderate": "81-120% AMI",
    "middle": "121-165% AMI",
    "above": "over 165% AMI",
}
FEED_BEDROOM_FIELDS = {
    "studio": "unit_distribution_studio",
    "1br": "unit_distribution_1bed",
    "2br": "unit_distribution_2bed",
    "3br": "unit_distribution_3bed",
    "4br": "unit_distribution_4bed",
}
HC_BEDROOM_FIELDS = {
    "studio": "studios",
    "1br": "oneBR",
    "2br": "twoBR",
    "3br": "threeBR",
    "4br": "fourBR",
}
HC_UNIT_CODES = {"1": "studio", "2": "1br", "3": "2br", "4": "3br"}
TIER_ORDER = {"likely": 0, "unverified": 1, "check": 2}
TIER_TAGS = {
    "likely": "[LIKELY FIT]",
    "unverified": "[NOT IN CITY DATA]",
    "check": "[CHECK INCOME]",
}


def num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def load_config(path=None):
    """Personal filters (income, assets) never live in the repo.

    Order: --config path, then the CONFIG_JSON environment variable (a GitHub
    Actions secret), then config.local.json (git-ignored, for local runs).
    """
    if path:
        return json.loads(Path(path).read_text())
    if os.environ.get("CONFIG_JSON"):
        return json.loads(os.environ["CONFIG_JSON"])
    local = HERE / "config.local.json"
    if local.exists():
        return json.loads(local.read_text())
    sys.exit("No config found. Copy config.example.json to config.local.json, "
             "or set the CONFIG_JSON secret for GitHub Actions.")


def clean(text):
    return " ".join(str(text or "").split())


# ---------------------------------------------------------------- fetching

def fetch_hc(cfg):
    """ONE request: the site's own search for this household size and income."""
    body = {
        "UnitTypes": [], "NearbyPlaces": [], "NearbySubways": [], "Amenities": [],
        "Applied": None, "HPDUserId": None, "Boroughs": [], "Neighborhoods": [],
        "HouseholdSize": str(cfg["household_size"]),
        "Income": str(cfg["annual_income"]),
        "HouseholdType": 1, "OwnerTypes": [], "PreferanceTypes": [], "LotteryTypes": [],
        "Min": None, "Max": None, "RentalSubsidy": None,
    }
    req = urllib.request.Request(
        HC_API,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("rentals", [])


def fetch_feed():
    params = {"$where": "lottery_status='Active'", "$limit": "5000"}
    url = f"{FEED_API}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.load(resp)


# ---------------------------------------------------------------- matching

def band_counts(rec, bands):
    counts = {b: num(rec.get(INCOME_FIELDS[b])) for b in bands}
    return {k: v for k, v in counts.items() if v > 0}


def feed_tier(feed_rec, cfg):
    """Return (tier, bands) from a city-feed record. tier is None if no unit is in range."""
    likely = band_counts(feed_rec, cfg["likely_bands"])
    if likely:
        return "likely", likely
    maybe = band_counts(feed_rec, cfg["maybe_bands"])
    if maybe:
        return "check", maybe
    return None, {}


def parse_end(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def from_hc(rec, cfg, today, feed_by_id):
    end_date = parse_end(rec.get("lotteryEndDate"))
    if end_date is None or end_date < today:
        return None
    code = BOROUGH_CODES.get(clean(rec.get("borough")), "?")
    if code in cfg["exclude_boroughs"]:
        return None
    # Double-check the income window (the site already filtered, this is cheap insurance).
    lo, hi = num(rec.get("minIncome")), num(rec.get("maxIncome"))
    if (lo and cfg["annual_income"] < lo) or (hi and cfg["annual_income"] > hi):
        return None

    beds = {b: num(rec.get(HC_BEDROOM_FIELDS[b])) for b in HC_BEDROOM_FIELDS}
    if not any(beds.values()):  # fall back to the per-unit type codes
        for c in str(rec.get("unitTypeIds") or "").split(","):
            name = HC_UNIT_CODES.get(c.strip())
            if name:
                beds[name] += 1
    wanted = {k: v for k, v in beds.items() if k in cfg["bedrooms"] and v > 0}
    if not wanted:
        return None

    lottery_id = str(rec.get("lotteryId"))
    feed_rec = feed_by_id.get(lottery_id)
    if feed_rec is None:
        tier, bands = "unverified", {}
    else:
        tier, bands = feed_tier(feed_rec, cfg)
        if tier is None:  # feed says every unit is in a lower band than you
            return None

    rents = sorted(int(x) for x in str(rec.get("rents") or "").split(",") if x.strip().isdigit())
    return {
        "id": lottery_id,
        "name": clean(rec.get("lotteryName", "Unnamed lottery")),
        "borough": clean(rec.get("borough")) or "?",
        "place": clean(rec.get("neighborhood")),
        "deadline": end_date,
        "days_left": (end_date - today).days,
        "tier": tier,
        "bands": bands,
        "band_units": sum(bands.values()),
        "total_units": num(rec.get("units")),
        "beds": wanted,
        "income_range": (lo, hi) if lo and hi else None,
        "rent_range": (rents[0], rents[-1]) if rents else None,
    }


def from_feed(rec, cfg, today):
    """Fallback when Housing Connect can't be reached: city feed only."""
    if rec.get("lottery_status") != "Active":
        return None
    end_date = parse_end(rec.get("lottery_end_date"))
    if end_date is None or end_date < today:
        return None
    if rec.get("borough") in cfg["exclude_boroughs"]:
        return None
    if rec.get("development_type") != "Rental":
        return None
    beds = {b: num(rec.get(FEED_BEDROOM_FIELDS[b])) for b in cfg["bedrooms"]}
    beds = {k: v for k, v in beds.items() if v > 0}
    if not beds:
        return None
    tier, bands = feed_tier(rec, cfg)
    if tier is None:
        return None
    return {
        "id": str(rec.get("lottery_id")),
        "name": clean(rec.get("lottery_name", "Unnamed lottery")),
        "borough": BOROUGH_NAMES.get(rec.get("borough"), rec.get("borough", "?")),
        "place": clean(rec.get("postcode")),
        "deadline": end_date,
        "days_left": (end_date - today).days,
        "tier": tier,
        "bands": bands,
        "band_units": sum(bands.values()),
        "total_units": num(rec.get("unit_count")),
        "beds": beds,
        "income_range": None,
        "rent_range": None,
    }


# ---------------------------------------------------------------- digest

def fmt_counts(d, labels=None):
    return ", ".join(f"{v} {(labels or {}).get(k, k)}" for k, v in d.items())


def fmt_item(m):
    when = "today" if m["days_left"] == 0 else f"in {m['days_left']}d"
    of_total = f" of {m['total_units']}" if m["total_units"] else ""
    if m["tier"] == "likely":
        note = f"{m['band_units']}{of_total} units in your range ({fmt_counts(m['bands'], BAND_LABELS)})"
    elif m["tier"] == "check":
        note = (f"only {m['band_units']}{of_total} units in a mixed band "
                f"({fmt_counts(m['bands'], BAND_LABELS)}), some may cap below your income")
    else:
        note = "no income-band details yet, check the income table"
    lines = [
        f"- {TIER_TAGS[m['tier']]} {m['name']} ({m['borough']}"
        f"{', ' + m['place'] if m['place'] else ''})",
        f"  Deadline {m['deadline']:%b %d} ({when}). Bedrooms: {fmt_counts(m['beds'])}.",
    ]
    extra = []
    if m["rent_range"]:
        extra.append(f"Rents ${m['rent_range'][0]:,}-${m['rent_range'][1]:,}")
    if m["income_range"]:
        extra.append(f"Income ${m['income_range'][0]:,}-${m['income_range'][1]:,} across all household sizes")
    lines.append("  " + (". ".join(extra) + ". " if extra else "") + note[0].upper() + note[1:] + ".")
    lines.append(f"  {DETAIL_URL.format(id=m['id'])}")
    return "\n".join(lines)


def build_digest(new, closing_soon, others, total, cfg, source_note):
    lines = []
    if source_note:
        lines += [source_note, ""]
    if new:
        lines.append(f"NEW ({len(new)})")
        lines += [fmt_item(m) for m in new]
    if closing_soon:
        lines.append("")
        lines.append(f"CLOSING SOON ({len(closing_soon)})")
        lines += [fmt_item(m) for m in closing_soon]
    if others:
        lines.append("")
        lines.append(f"ALSO OPEN ({len(others)})")
        lines += [fmt_item(m) for m in others]
    lines.append("")
    lines.append(f"{total} open lotteries match your filters.")
    lines.append(
        f"Your numbers: household of {cfg['household_size']}, income ${cfg['annual_income']:,}, "
        f"assets ${cfg['assets']:,}."
    )
    lines.append(
        "This list has no asset limits and only a lottery-wide income range. On each listing, open "
        "the Available Units table and confirm your income fits your household size and your assets "
        "are under the asset limit."
    )
    lines.append(f"Browse all: {SEARCH_URL}")
    return "\n".join(lines)


def trim_for_push(body, limit=3900):
    """Cut at a line boundary so a link is never chopped in half."""
    if len(body.encode("utf-8")) <= limit:
        return body
    note = "\n...more in your email, or browse: " + SEARCH_URL
    kept = []
    size = len(note.encode("utf-8"))
    for line in body.split("\n"):
        size += len(line.encode("utf-8")) + 1
        if size > limit:
            break
        kept.append(line)
    return "\n".join(kept) + note


def send_ntfy(topic, title, body):
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=trim_for_push(body).encode("utf-8"),
        headers={"Title": title, "Click": SEARCH_URL, "Tags": "house"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).read()


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("EMAIL_TO")
    if not (host and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    return True


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture", help="city-feed records from a JSON file instead of the live feed")
    ap.add_argument("--hc-fixture", help="Housing Connect response from a JSON file instead of the live site")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    ap.add_argument("--config", help="read filters from this JSON file (e.g. config.example.json)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    today = date.fromisoformat(args.today) if args.today else date.today()

    feed_records, feed_ok = [], True
    try:
        feed_records = json.loads(Path(args.fixture).read_text()) if args.fixture else fetch_feed()
    except Exception as e:  # the feed only enriches, so keep going without it
        feed_ok = False
        print(f"City feed unavailable ({e}); matches will have no income-band details.", file=sys.stderr)
    feed_by_id = {str(r.get("lottery_id")): r for r in feed_records}

    hc_records, source_note = None, ""
    try:
        if args.hc_fixture:
            hc_records = json.loads(Path(args.hc_fixture).read_text()).get("rentals", [])
        else:
            hc_records = fetch_hc(cfg)
    except Exception as e:
        print(f"Housing Connect unreachable ({e}); falling back to the city feed.", file=sys.stderr)

    if hc_records is not None:
        found = [m for r in hc_records if (m := from_hc(r, cfg, today, feed_by_id))]
    elif feed_ok:
        found = [m for r in feed_records if (m := from_feed(r, cfg, today))]
        source_note = ("NOTE: Housing Connect was unreachable today, so this list comes from the "
                       "city feed only and may be missing recent lotteries.")
    else:
        print("Both Housing Connect and the city feed failed. Nothing to report.", file=sys.stderr)
        return 1

    found.sort(key=lambda m: (TIER_ORDER[m["tier"]], m["deadline"]))

    state_path = HERE / "seen.json"
    seen = set(json.loads(state_path.read_text())) if state_path.exists() else set()
    new = [m for m in found if m["id"] not in seen]
    closing = [m for m in found if m["id"] in seen and m["days_left"] <= cfg["reminder_days"]]

    if not new and not closing:
        print(f"{len(found)} matching open lotteries, nothing new. No alert sent.")
        return 0

    listed = {m["id"] for m in new} | {m["id"] for m in closing}
    others = [m for m in found if m["id"] not in listed]
    digest = build_digest(new, closing, others, len(found), cfg, source_note)
    title = f"{len(new)} new NYC housing lotteries" if new else "NYC housing lottery closing soon"
    print(title)
    if args.dry_run:
        print(digest)
        print("\n[dry run] nothing sent, state not saved.")
        return 0
    # Live runs log only counts: Actions logs are public on a public repo,
    # and the digest includes your income and assets.
    print(f"{len(new)} new, {len(closing)} closing soon, {len(found)} total matches.")

    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        send_ntfy(topic, title, digest)
        sent = True
    if send_email(title, digest):
        sent = True
    if not sent:
        print("No NTFY_TOPIC or SMTP settings found, so no alert was delivered.", file=sys.stderr)
        return 1

    state_path.write_text(json.dumps(sorted(seen | {m["id"] for m in found}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
