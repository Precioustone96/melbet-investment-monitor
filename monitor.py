"""
Melbet live football monitor - single-run version for GitHub Actions.

Each invocation:
  1. Opens a headless browser, establishes a session on the live page
  2. Fetches the games1x2 live-feed endpoint once
  3. Checks every match at minute >= 70 for any Over/Under (Total Goals)
     line with odds <= 1.02
  4. Sends an email for any new qualifying match (via GitHub Secrets)
  5. Records alerted match IDs in alerts.json, keyed by date, so the
     same match isn't alerted twice in one day
  6. Exits. The GitHub Actions workflow commits alerts.json back to the
     repo if it changed, giving persistence across runs without any
     external database.

NOTE: Scraping MelBet's live-feed endpoints this way is very likely
outside their Terms of Service, and running it unattended increases
that exposure. This is for personal/educational use - use at your own
discretion. Odds near 1.01-1.02 late in a match are not risk-free
(stoppage-time goals, red cards, VAR overturns can still happen).
"""

import json
import os
import re
import smtplib
import sys
from datetime import date, datetime, timezone
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# ---------- CONFIG ----------
LIVE_PAGE_URL = "https://melbet.com/en/live/football"
GAMES_API_URL = (
    "https://melbet.com/service-api/main-live-feed/v3/games1x2"
    "?cfView=3&count=40&fcountry=132&gr=62&grMode=4&lng=en&ref=8&selectedMs=1.1,2.1,10.1"
)

MINUTE_THRESHOLD = 70
ODDS_THRESHOLD = 1.02

ALERTS_FILE = "alerts.json"

# --- Email settings, pulled from GitHub Actions secrets (env vars) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_TO = os.environ.get("ALERT_TO", SMTP_USER)


def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return {"date": date.today().isoformat(), "alerted_ids": []}

    with open(ALERTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Reset the list on a new UTC day (GitHub Actions runners are UTC)
    today = date.today().isoformat()
    if data.get("date") != today:
        data = {"date": today, "alerted_ids": []}

    return data


def save_alerts(data):
    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def parse_minute(status_line_str):
    if not status_line_str:
        return None
    m = re.search(r"(\d+)\s*minutes?", status_line_str)
    return int(m.group(1)) if m else None


def find_qualifying_lines(match):
    """Return a list of (side, parameter, cf) for any Over/Under line <= ODDS_THRESHOLD."""
    hits = []
    for group in match.get("centralBlockEventGroups", []):
        if group.get("groupId") != 17:  # 17 = Total Goals
            continue
        for side_events in group.get("events", []):
            for ev in side_events:
                cf = ev.get("cf")
                if cf is None:
                    continue
                if cf <= ODDS_THRESHOLD:
                    side = "Over" if ev.get("type") == 9 else "Under"
                    hits.append((side, ev.get("parameter"), cf))
    return hits


def send_email(subject, body):
    if not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP_USER / SMTP_PASSWORD not set - skipping email, printing alert only.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def check_matches(matches, alerts_data):
    alerted_ids = set(alerts_data["alerted_ids"])
    new_alerts = 0

    for match in matches:
        match_id = match.get("id") or match.get("mainGameId")
        if match_id is None:
            continue

        scores = match.get("scores", {})
        minute = parse_minute(scores.get("statusLineStr"))
        if minute is None or minute < MINUTE_THRESHOLD:
            continue

        hits = find_qualifying_lines(match)
        if not hits:
            continue

        if match_id in alerted_ids:
            continue

        opp1 = match.get("opponent1", {}).get("fullName", "?")
        opp2 = match.get("opponent2", {}).get("fullName", "?")
        score = scores.get("fullScore", "?")
        liga = match.get("liga", {}).get("name", "?")

        lines_str = "\n".join(f"  {side} {param} @ {cf}" for side, param, cf in hits)

        subject = f"Investment Alert: {opp1} vs {opp2} ({minute}')"
        body = (
            f"{liga}\n"
            f"{opp1} vs {opp2}\n"
            f"Minute: {minute}'\n"
            f"Score: {score}\n\n"
            f"Qualifying lines (odds <= {ODDS_THRESHOLD}):\n{lines_str}\n"
        )

        print(f"\n>>> ALERT: {subject}")
        print(body)

        try:
            send_email(subject, body)
            print("Email sent.")
        except Exception as e:
            print(f"Email failed to send: {e}")

        alerted_ids.add(match_id)
        new_alerts += 1

    alerts_data["alerted_ids"] = sorted(alerted_ids)
    return new_alerts


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting single-run poll...")

    alerts_data = load_alerts()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        try:
            page.goto(LIVE_PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            resp = page.request.get(GAMES_API_URL)
            if not resp.ok:
                print(f"Request failed: {resp.status}")
                sys.exit(1)

            matches = resp.json()
            print(f"Got {len(matches)} matches.")

            new_alerts = check_matches(matches, alerts_data)
            print(f"New alerts this run: {new_alerts}")

        finally:
            browser.close()

    save_alerts(alerts_data)
    print("Done.")


if __name__ == "__main__":
    main()
