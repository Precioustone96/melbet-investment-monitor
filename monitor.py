"""
MelBet Live Odds Monitor (API version)

Built on the structure of the earlier script that worked on GitHub Actions:
open the MelBet live page with Playwright, then read MelBet's own live-feed
JSON instead of scraping the page's HTML.

QUALIFICATION RULES:

    - Live game in: Football, Tennis, Basketball, Hockey, Volleyball,
      Handball, Baseball or Cricket
    - Any available selection in the feed with decimal odds from 1.001 to
      1.008, inclusive
    - Virtual sports, simulations and esports are excluded
    - At most 400 eligible games are checked per run
    - A game already alerted today is skipped

OUTPUT:

    - One email per qualifying game, subject "Investment Alert"
    - One feedback email per run, subject "MelBet Monitor Feedback"
      (control with FEEDBACK_MODE = always | problems | never)
    - alerts.json   : date, last run, alerted game IDs (committed by workflow)
    - last_scan.json: report of the last run

Usage:
    python monitor.py              normal run
    python monitor.py --dry-run    no emails, no alerts.json changes
    python monitor.py --debug      also print feed URLs and sample fields

Time zone: Africa/Lagos (WAT)
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


# ============================================================
# MELBET SETTINGS
# ============================================================

LIVE_BASE_URL = "https://melbet.com/en/live"

# The football feed URL from the earlier working script. It is used only as
# a fallback if the football page's own feed request cannot be captured.
FOOTBALL_FALLBACK_API_URL = (
    "https://melbet.com/service-api/main-live-feed/v3/games1x2"
    "?cfView=3"
    "&count=40"
    "&fcountry=132"
    "&gr=62"
    "&grMode=4"
    "&lng=en"
    "&ref=8"
    "&selectedMs=1.1,2.1,10.1"
)

# Display name -> live page slug on melbet.com/en/live/<slug>
# If a sport returns nothing, run with --debug and check its slug.
SPORTS = {
    "Football": "football",
    "Tennis": "tennis",
    "Basketball": "basketball",
    "Hockey": "ice-hockey",
    "Volleyball": "volleyball",
    "Handball": "handball",
    "Baseball": "baseball",
    "Cricket": "cricket",
}

# Names MelBet may use inside the feed for each of our sports
SPORT_NAME_ALIASES = {
    "football": "Football", "soccer": "Football",
    "tennis": "Tennis", "lawn tennis": "Tennis",
    "basketball": "Basketball",
    "hockey": "Hockey", "ice hockey": "Hockey", "icehockey": "Hockey",
    "volleyball": "Volleyball",
    "handball": "Handball",
    "baseball": "Baseball",
    "cricket": "Cricket",
}


# ============================================================
# ALERT CONDITIONS
# ============================================================

MIN_ODDS = Decimal("1.001")
MAX_ODDS = Decimal("1.008")

MAX_GAMES = 400
REQUEST_COUNT = 400            # games requested per sport from the feed
SCAN_TIMEOUT_SECONDS = 240     # whole-run budget; feedback is sent after
CAPTURE_WAIT_SECONDS = 12      # wait for the page's own feed request
SETTLE_WAIT_MS = 3000          # session/cookie settle time after navigation

# Virtual sports, simulations and esports (checked against league, team and
# sport names). "FIFA" alone is NOT blocked so the real World Cup stays in.
EXCLUDED_LABELS = re.compile(
    r"\b(?:virtual\w*|e[\s-]?sports?|cyber\w*|simulat\w*|srl|"
    r"computer\s+generated|"
    r"efootball|ebasketball|ehockey|esoccer|etennis|ecricket|"
    r"e[\s-](?:soccer|football|basketball|hockey|tennis|cricket|"
    r"volleyball|handball|baseball)|"
    r"fifa\s*(?:\d{2}(?!\d)|online|volta|eworld|eclub)|"
    r"ea\s*(?:sports\s*)?fc|pes\s*\d*|nba\s*2k\w*|nhl\s*\d{2}(?!\d)|"
    r"madden\w*|dota\s*2?|counter[\s-]*strike\w*|cs\s*(?:2|go)|"
    r"league\s+of\s+legends|valorant|rocket\s+league)\b",
    re.IGNORECASE,
)


# ============================================================
# FILE / TIMEZONE SETTINGS
# ============================================================

ALERTS_FILE = "alerts.json"
REPORT_FILE = "last_scan.json"
TIMEZONE = ZoneInfo("Africa/Lagos")


# ============================================================
# EMAIL SETTINGS
# ============================================================

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_TO = os.environ.get("ALERT_TO")

# always   = feedback email after every run
# problems = only when the run failed or had issues
# never    = no feedback email
FEEDBACK_MODE = os.environ.get("FEEDBACK_MODE", "always").lower()


# ============================================================
# TIME FUNCTIONS
# ============================================================

def current_datetime():
    return datetime.now(TIMEZONE)


def today_string():
    return current_datetime().date().isoformat()


def timestamp_string():
    return current_datetime().strftime("%Y-%m-%d %H:%M:%S WAT")


# ============================================================
# ALERT STATE
# ============================================================

def new_state():
    return {"date": today_string(), "last_run": None, "alerted_ids": []}


def load_alerts():
    """Load alerts.json; reset the alerted list on a new calendar day."""
    if not os.path.exists(ALERTS_FILE):
        return new_state()

    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        print("WARNING: alerts.json could not be read. Creating new state.")
        return new_state()

    if not isinstance(data, dict) or data.get("date") != today_string():
        print("New calendar day: resetting today's alerted game list.")
        return new_state()

    if not isinstance(data.get("alerted_ids"), list):
        data["alerted_ids"] = []
    data.setdefault("last_run", None)
    return data


def save_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


# ============================================================
# EMAIL
# ============================================================

def send_email(subject, body):
    for name, value in (("SMTP_USER", SMTP_USER),
                        ("SMTP_PASSWORD", SMTP_PASSWORD),
                        ("ALERT_TO", ALERT_TO)):
        if not value:
            raise RuntimeError(f"{name} is not configured.")

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = ALERT_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


# ============================================================
# FEED HELPERS
# ============================================================

def clean_text(value):
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip()


def nested_name(value):
    """Return a name from either a string or a dict with a name-like key."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        for key in ("fullName", "name", "title", "nameEng"):
            text = clean_text(value.get(key))
            if text:
                return text
    return ""


def game_id(match):
    value = match.get("id") or match.get("mainGameId")
    return None if value is None else str(value)


def team_names(match):
    one = nested_name(match.get("opponent1")) or "Unknown"
    two = nested_name(match.get("opponent2")) or "Unknown"
    return one, two


def league_name(match):
    return nested_name(match.get("liga")) or nested_name(match.get("league")) \
        or "Unknown League"


def feed_sport_name(match):
    """Sport name reported inside the game object, or '' if not present."""
    for key in ("sportName", "sportNameEng", "sport", "sportTitle"):
        name = nested_name(match.get(key))
        if name:
            return name
    return ""


def canonical_sport(name):
    return SPORT_NAME_ALIASES.get(clean_text(name).casefold())


def is_excluded(match, sport_label):
    """True for virtual / simulated / esports games."""
    one, two = team_names(match)
    text = " | ".join((league_name(match), one, two,
                       feed_sport_name(match), sport_label))
    text = re.sub(r"[_/]+", " ", text)
    return bool(EXCLUDED_LABELS.search(text))


def to_decimal(value):
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def iter_events(node, market=""):
    """
    Walk a game object and yield (event, market_name) for every dict that
    carries odds ("cf"). This covers every market the feed contains.
    """
    if isinstance(node, dict):
        name = clean_text(node.get("groupName") or node.get("groupNameEng"))
        if name:
            market = name
        elif node.get("groupId") is not None and "events" in node:
            market = f"Market group {node['groupId']}"

        if "cf" in node and not isinstance(node["cf"], (dict, list)):
            yield node, market

        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from iter_events(value, market)

    elif isinstance(node, list):
        for item in node:
            yield from iter_events(item, market)


def is_blocked(event):
    for key in ("blocked", "isBlocked", "locked", "suspended"):
        if event.get(key) is True:
            return True
    return False


def describe_selection(event):
    name = (clean_text(event.get("name")) or clean_text(event.get("shortName"))
            or f"type {event.get('type', '?')}")
    parameter = event.get("parameter")
    if parameter not in (None, "") and str(parameter) not in name:
        name = f"{name} {parameter}"
    return name


def find_qualifying(match):
    """Every available selection with odds from MIN_ODDS to MAX_ODDS."""
    found, seen = [], set()
    for event, market in iter_events(match):
        odds = to_decimal(event.get("cf"))
        if odds is None or not (MIN_ODDS <= odds <= MAX_ODDS):
            continue
        if is_blocked(event):
            continue
        item = (market or "Market", describe_selection(event), str(odds))
        if item not in seen:
            seen.add(item)
            found.append({"market": item[0], "selection": item[1],
                          "odds": item[2]})
    return found


def extract_matches(payload):
    """Accept a plain list or a dict wrapping the list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "games", "Value", "value", "data", "result"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def with_count(url, count):
    """Return the feed URL with its count parameter raised."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "count"]
    query.append(("count", str(count)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query, safe=","), parts.fragment))


# ============================================================
# FETCHING (Playwright: open page, then read the page's own feed)
# ============================================================

def capture_feed_url(page, slug, debug):
    """
    Open the sport's live page and capture the live-feed request that the
    page itself makes. This avoids guessing sport IDs or parameters.
    """
    captured = []

    def on_response(response):
        url = response.url
        if "main-live-feed" in url and "/games" in url:
            captured.append(url)

    page.on("response", on_response)
    try:
        try:
            page.goto(f"{LIVE_BASE_URL}/{slug}", wait_until="commit",
                      timeout=30000)
        except Exception as error:
            print(f"  Navigation note for '{slug}': {error}")

        deadline = time.time() + CAPTURE_WAIT_SECONDS
        while time.time() < deadline and not captured:
            page.wait_for_timeout(500)
        page.wait_for_timeout(SETTLE_WAIT_MS if captured else 0)
    finally:
        page.remove_listener("response", on_response)

    if debug:
        print(f"  Captured feed URLs for '{slug}': {captured}")

    preferred = [url for url in captured if "games1x2" in url]
    if preferred:
        return preferred[-1]
    return captured[-1] if captured else None


def fetch_sport(page, sport, slug, debug):
    """Return (matches, feed_url, error_message)."""
    feed_url = capture_feed_url(page, slug, debug)
    source = "page feed"

    if not feed_url and sport == "Football":
        feed_url = FOOTBALL_FALLBACK_API_URL
        source = "fallback URL"

    if not feed_url:
        return [], None, f"{sport}: live feed was not found on the page."

    url = with_count(feed_url, REQUEST_COUNT)
    try:
        response = page.request.get(url, timeout=60000)
    except Exception as error:
        return [], url, f"{sport}: feed request failed ({error})."

    if not response.ok:
        return [], url, f"{sport}: feed returned HTTP {response.status} ({source})."

    try:
        payload = response.json()
    except Exception:
        return [], url, f"{sport}: feed did not return JSON."

    matches = extract_matches(payload)
    if matches is None:
        return [], url, f"{sport}: unexpected feed format ({type(payload).__name__})."

    if debug and matches and isinstance(matches[0], dict):
        print(f"  {sport} sample keys: {sorted(matches[0].keys())[:40]}")
    return matches, url, None


# ============================================================
# ALERT EMAIL BODY
# ============================================================

def alert_body(sport, match, lines):
    one, two = team_names(match)
    scores = match.get("scores") or {}
    score = scores.get("fullScore", "?")
    status = scores.get("statusLineStr", "")
    selections = "\n".join(
        f"- {item['market']}: {item['selection']} @ {item['odds']}"
        for item in lines
    )
    return f"""
INVESTMENT ALERT

Sport: {sport}
League: {league_name(match)}

Match:
{one} vs {two}

Score: {score}
Status: {status or 'live'}

Selection(s) with odds {MIN_ODDS} to {MAX_ODDS}:
{selections}

Game ID: {game_id(match)}
Alert time: {timestamp_string()}
""".strip()


# ============================================================
# REPORT
# ============================================================

def format_report(report):
    status = report["status"].upper()
    lines = [
        "MELBET MONITOR FEEDBACK",
        f"Status: {status}",
        f"Time: {report['finished_at']}",
        f"Sports monitored: {', '.join(SPORTS)}",
        f"Odds range: {MIN_ODDS} to {MAX_ODDS} inclusive",
        f"Scan duration: {report['duration']} seconds",
        f"Live games selected: {report['selected']} / {MAX_GAMES}",
        f"Games checked: {report['checked']}",
        f"Skipped (already alerted today): {report['skipped_alerted']}",
        f"Skipped (virtual / esports / simulated): {report['skipped_excluded']}",
        f"Skipped (duplicate or other sport): {report['skipped_other']}",
        f"Games with qualifying odds: {report['matches']}",
        f"Game alerts delivered: {report['alerts_sent']}",
        "",
        "Per sport (received / selected):",
    ]
    for sport, counts in report["per_sport"].items():
        lines.append(f"- {sport}: {counts['received']} / {counts['selected']}")

    if report["budget_hit"]:
        lines.append("")
        lines.append("Time budget reached: partial scan; remaining sports skipped.")
    if report["matches"] == 0 and report["status"] != "failed":
        lines.append("")
        lines.append("No qualifying odds found in the games checked.")
    if report["errors"]:
        lines.append("")
        lines.append("Issues:")
        lines.extend(f"- {error}" for error in report["errors"][:15])
    return "\n".join(lines)


# ============================================================
# MAIN MONITOR
# ============================================================

def run(args):
    from playwright.sync_api import sync_playwright

    started = time.time()
    alerts_data = load_alerts()
    alerted_ids = {str(item) for item in alerts_data.get("alerted_ids", [])}

    print("=" * 60)
    print("MELBET LIVE ODDS MONITOR")
    print("=" * 60)
    print(f"Run started: {timestamp_string()}")
    print(f"Odds range: {MIN_ODDS} to {MAX_ODDS}")
    print(f"Previously alerted games today: {len(alerted_ids)}")
    if args.dry_run:
        print("DRY RUN: no emails and no state changes.")

    report = {
        "selected": 0, "checked": 0, "matches": 0, "alerts_sent": 0,
        "skipped_alerted": 0, "skipped_excluded": 0, "skipped_other": 0,
        "per_sport": {}, "errors": [], "budget_hit": False,
    }
    seen_ids = set()
    sports_read = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        try:
            for sport, slug in SPORTS.items():
                if report["selected"] >= MAX_GAMES:
                    break
                if time.time() - started > SCAN_TIMEOUT_SECONDS:
                    report["budget_hit"] = True
                    break

                print(f"\nReading {sport} ...")
                matches, feed_url, error = fetch_sport(page, sport, slug, args.debug)
                counts = {"received": len(matches), "selected": 0}
                report["per_sport"][sport] = counts

                if error:
                    print(f"  {error}")
                    report["errors"].append(error)
                    continue

                sports_read += 1
                print(f"  Games received: {len(matches)}")

                for match in matches:
                    if report["selected"] >= MAX_GAMES:
                        break
                    if not isinstance(match, dict):
                        continue

                    gid = game_id(match)
                    if gid is None or gid in seen_ids:
                        report["skipped_other"] += 1
                        continue

                    # If the game states its sport and it is a different one,
                    # the feed ignored our sport filter: skip it here.
                    reported = canonical_sport(feed_sport_name(match))
                    if reported and reported != sport:
                        report["skipped_other"] += 1
                        continue

                    seen_ids.add(gid)

                    if is_excluded(match, sport):
                        report["skipped_excluded"] += 1
                        continue
                    if gid in alerted_ids:
                        report["skipped_alerted"] += 1
                        continue

                    report["selected"] += 1
                    counts["selected"] += 1
                    report["checked"] += 1

                    lines = find_qualifying(match)
                    if not lines:
                        continue

                    report["matches"] += 1
                    body = alert_body(sport, match, lines)
                    print("\n" + "=" * 60)
                    print("QUALIFYING GAME FOUND")
                    print("=" * 60)
                    print(body)

                    if args.dry_run:
                        continue
                    try:
                        send_email("Investment Alert", body)
                        alerted_ids.add(gid)
                        report["alerts_sent"] += 1
                        print("Email sent successfully.")
                    except Exception as email_error:
                        message = f"Alert email failed for game {gid}: {email_error}"
                        print(message)
                        report["errors"].append(message)
        finally:
            browser.close()

    # --------------------------------------------------------
    # Final status
    # --------------------------------------------------------
    if sports_read == 0:
        report["status"] = "failed"
    elif report["errors"] or report["budget_hit"]:
        report["status"] = "partial"
    else:
        report["status"] = "complete"

    report["duration"] = round(time.time() - started, 1)
    report["finished_at"] = timestamp_string()
    text = format_report(report)
    print("\n" + text)

    # --------------------------------------------------------
    # Feedback email
    # --------------------------------------------------------
    feedback_failed = False
    wanted = (FEEDBACK_MODE == "always"
              or (FEEDBACK_MODE == "problems" and report["status"] != "complete"))
    if wanted and not args.dry_run:
        try:
            send_email("MelBet Monitor Feedback", text)
            print("Feedback email sent.")
        except Exception as error:
            feedback_failed = True
            print(f"Feedback email failed: {error}")

    # --------------------------------------------------------
    # Save state
    # --------------------------------------------------------
    if not args.dry_run:
        alerts_data["date"] = today_string()
        alerts_data["alerted_ids"] = sorted(alerted_ids)
        alerts_data["last_run"] = report["finished_at"]
        alerts_data["last_status"] = report["status"]
        save_json(ALERTS_FILE, alerts_data)
        save_json(REPORT_FILE, report)
        print("alerts.json updated.")

    return 1 if report["status"] == "failed" or feedback_failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print matches; no emails, no file writes.")
    parser.add_argument("--debug", action="store_true",
                        help="Print captured feed URLs and sample fields.")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"MONITOR FAILED: {error}", file=sys.stderr)
        sys.exit(1)
