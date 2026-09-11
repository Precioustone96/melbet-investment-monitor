"""
MelBet Live Football Monitor

Checks live football matches for:

    - Match minute >= 70
    - Any Total Goals Over/Under option
    - Odds <= 1.02

When a match qualifies:
    - Sends an email with the subject "Investment Alert"
    - Records the match ID in alerts.json
    - Does not send another alert for that same match on the same day

The alerts.json file also records:
    - The current calendar date
    - The exact time of the last completed run
    - Match IDs that have already triggered an alert today

Time zone:
    Africa/Lagos (WAT)
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


# ============================================================
# MELBET SETTINGS
# ============================================================

LIVE_PAGE_URL = "https://melbet.com/en/live/football"

GAMES_API_URL = (
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


# ============================================================
# ALERT CONDITIONS
# ============================================================

# Match must be at least this minute
MINUTE_THRESHOLD = 70

# Odds must be this value or lower
ODDS_THRESHOLD = 1.02


# ============================================================
# FILE / TIMEZONE SETTINGS
# ============================================================

ALERTS_FILE = "alerts.json"

# Nigeria time
TIMEZONE = ZoneInfo("Africa/Lagos")


# ============================================================
# EMAIL SETTINGS
# ============================================================

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_TO = os.environ.get("ALERT_TO")


# ============================================================
# TIME FUNCTIONS
# ============================================================

def current_datetime():
    """
    Return the current date and time in Nigeria.
    """
    return datetime.now(TIMEZONE)


def today_string():
    """
    Return today's date in YYYY-MM-DD format.
    """
    return current_datetime().date().isoformat()


def timestamp_string():
    """
    Return the exact current time in Nigeria.

    Example:
    2026-09-11 14:35:27 WAT
    """
    return current_datetime().strftime("%Y-%m-%d %H:%M:%S WAT")


# ============================================================
# ALERT STATE FUNCTIONS
# ============================================================

def load_alerts():
    """
    Load alerts.json.

    If the stored date is different from today,
    automatically reset the alerted match IDs.

    This means:

    Match A alerts today
        ↓
    Match A will NOT alert again today

    Tomorrow:
        ↓
    Match A can alert again if it qualifies.
    """

    today = today_string()

    # If alerts.json does not exist
    if not os.path.exists(ALERTS_FILE):
        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

    except (json.JSONDecodeError, OSError):
        print("WARNING: alerts.json could not be read.")
        print("Creating a new alert state.")

        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    # If the date has changed, start a new alert day
    if data.get("date") != today:
        print("New calendar day detected.")
        print("Resetting today's alerted match list.")

        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    # Make sure alerted_ids is always a list
    if not isinstance(data.get("alerted_ids"), list):
        data["alerted_ids"] = []

    # Make sure last_run exists
    if "last_run" not in data:
        data["last_run"] = None

    return data


def save_alerts(data):
    """
    Save alert information to alerts.json.
    """

    with open(ALERTS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    print("alerts.json updated successfully.")


# ============================================================
# MATCH MINUTE
# ============================================================

def get_match_minute(match):
    """
    Extract the current match minute.

    Example statusLineStr:
        "85 minutes"

    Returns:
        85
    """

    scores = match.get("scores") or {}

    status = scores.get("statusLineStr", "")

    if not status:
        return None

    minute_match = re.search(
        r"(\d+)\s*minutes?",
        status,
        re.IGNORECASE
    )

    if minute_match:
        return int(minute_match.group(1))

    return None


# ============================================================
# FIND QUALIFYING TOTAL GOAL MARKETS
# ============================================================

def find_qualifying_lines(match):
    """
    Look for:

        Total Goals
        Over OR Under
        Odds <= 1.02

    MelBet structure:

        groupId 17 = Total Goals

        type 9  = Over
        type 10 = Under

        cf = odds
    """

    qualifying_lines = []

    groups = match.get("centralBlockEventGroups", [])

    if not isinstance(groups, list):
        return qualifying_lines

    for group in groups:

        if not isinstance(group, dict):
            continue

        # Total Goals group
        if group.get("groupId") != 17:
            continue

        events = group.get("events", [])

        if not isinstance(events, list):
            continue

        for event_group in events:

            if not isinstance(event_group, list):
                continue

            for event in event_group:

                if not isinstance(event, dict):
                    continue

                event_type = event.get("type")

                # Over
                if event_type == 9:
                    side = "Over"

                # Under
                elif event_type == 10:
                    side = "Under"

                else:
                    continue

                odd = event.get("cf")

                if odd is None:
                    continue

                try:
                    odd = float(odd)

                except (TypeError, ValueError):
                    continue

                # Our required odds condition
                if odd <= ODDS_THRESHOLD:

                    parameter = event.get(
                        "parameter",
                        "?"
                    )

                    qualifying_lines.append({
                        "side": side,
                        "parameter": parameter,
                        "odd": odd
                    })

    return qualifying_lines


# ============================================================
# SEND EMAIL
# ============================================================

def send_email(body):
    """
    Send the investment alert email.

    Subject is ALWAYS:

        Investment Alert
    """

    if not SMTP_USER:
        raise RuntimeError(
            "SMTP_USER is not configured."
        )

    if not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP_PASSWORD is not configured."
        )

    if not ALERT_TO:
        raise RuntimeError(
            "ALERT_TO is not configured."
        )

    message = MIMEText(
        body,
        "plain",
        "utf-8"
    )

    # EXACT SUBJECT REQUESTED
    message["Subject"] = "Investment Alert"

    message["From"] = SMTP_USER
    message["To"] = ALERT_TO

    with smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30
    ) as server:

        server.ehlo()

        server.starttls()

        server.ehlo()

        server.login(
            SMTP_USER,
            SMTP_PASSWORD
        )

        server.send_message(message)


# ============================================================
# CHECK ALL LIVE MATCHES
# ============================================================

def check_matches(matches, alerts_data):
    """
    Check every live football match.

    A match qualifies when:

        Minute >= 70

        AND

        Total Goals Over/Under odds <= 1.02

    Once an email is successfully sent:

        match ID is added to alerted_ids

    Therefore, the same match cannot send another
    alert during the same calendar day.
    """

    alerted_ids = set(
        str(match_id)
        for match_id in alerts_data.get(
            "alerted_ids",
            []
        )
    )

    new_alerts = 0

    for match in matches:

        if not isinstance(match, dict):
            continue

        # Match ID
        match_id = (
            match.get("id")
            or match.get("mainGameId")
        )

        if match_id is None:
            continue

        match_id = str(match_id)

        # Already alerted today?
        if match_id in alerted_ids:
            continue

        # ----------------------------------------------------
        # CHECK MINUTE
        # ----------------------------------------------------

        minute = get_match_minute(match)

        if minute is None:
            continue

        if minute < MINUTE_THRESHOLD:
            continue

        # ----------------------------------------------------
        # CHECK ODDS
        # ----------------------------------------------------

        qualifying_lines = find_qualifying_lines(
            match
        )

        if not qualifying_lines:
            continue

        # ----------------------------------------------------
        # MATCH INFORMATION
        # ----------------------------------------------------

        opponent1 = (
            match.get("opponent1") or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        opponent2 = (
            match.get("opponent2") or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        league = (
            match.get("liga") or {}
        ).get(
            "name",
            "Unknown League"
        )

        scores = match.get("scores") or {}

        score = scores.get(
            "fullScore",
            "?"
        )

        # ----------------------------------------------------
        # FORMAT QUALIFYING LINES
        # ----------------------------------------------------

        lines = []

        for line in qualifying_lines:

            lines.append(
                f"{line['side']} "
                f"{line['parameter']} "
                f"@ {line['odd']}"
            )

        qualifying_text = "\n".join(lines)

        # ----------------------------------------------------
        # EMAIL BODY
        # ----------------------------------------------------

        email_body = f"""
INVESTMENT ALERT

League: {league}

Match:
{opponent1} vs {opponent2}

Minute: {minute}'
Score: {score}

Qualifying Total Goals Option(s):

{qualifying_text}

Match ID: {match_id}

Condition:
Minute >= {MINUTE_THRESHOLD}
Odds <= {ODDS_THRESHOLD}

Alert Time:
{timestamp_string()}
""".strip()

        # ----------------------------------------------------
        # PRINT ALERT TO GITHUB ACTIONS LOG
        # ----------------------------------------------------

        print("\n" + "=" * 60)
        print("QUALIFYING MATCH FOUND")
        print("=" * 60)

        print(email_body)

        print("=" * 60)

        # ----------------------------------------------------
        # SEND EMAIL
        # ----------------------------------------------------

        try:

            send_email(email_body)

            print(
                "Email sent successfully."
            )

            # IMPORTANT:
            # Only record the match AFTER
            # the email was successfully sent.
            alerted_ids.add(match_id)

            new_alerts += 1

        except Exception as error:

            print(
                "EMAIL FAILED:"
            )

            print(error)

            print(
                "Match was NOT added to alerted_ids."
            )

            print(
                "The monitor will retry this match "
                "on the next run if it still qualifies."
            )

    # Save updated IDs
    alerts_data["alerted_ids"] = sorted(
        alerted_ids
    )

    return new_alerts


# ============================================================
# MAIN MONITOR
# ============================================================

def main():

    print("=" * 60)
    print("MELBET INVESTMENT MONITOR")
    print("=" * 60)

    print(
        f"Run started: {timestamp_string()}"
    )

    # Load today's alert state
    alerts_data = load_alerts()

    print(
        f"Alert date: {alerts_data['date']}"
    )

    print(
        f"Previously alerted matches today: "
        f"{len(alerts_data['alerted_ids'])}"
    )

    # --------------------------------------------------------
    # START PLAYWRIGHT
    # --------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000
            }
        )

        try:

            # ------------------------------------------------
            # OPEN MELBET
            # ------------------------------------------------

            print(
                "Opening MelBet live football page..."
            )

            page.goto(
                LIVE_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            # Give MelBet time to establish the session
            page.wait_for_timeout(5000)

            # ------------------------------------------------
            # FETCH LIVE DATA
            # ------------------------------------------------

            print(
                "Fetching live match data..."
            )

            response = page.request.get(
                GAMES_API_URL,
                timeout=60000
            )

            if not response.ok:

                print(
                    f"API request failed: "
                    f"{response.status}"
                )

                sys.exit(1)

            # ------------------------------------------------
            # READ JSON
            # ------------------------------------------------

            matches = response.json()

            if not isinstance(matches, list):

                print(
                    "Unexpected API response format."
                )

                print(
                    f"Response type: "
                    f"{type(matches)}"
                )

                sys.exit(1)

            print(
                f"Live matches received: "
                f"{len(matches)}"
            )

            # ------------------------------------------------
            # CHECK MATCHES
            # ------------------------------------------------

            new_alerts = check_matches(
                matches,
                alerts_data
            )

            print(
                f"New alerts sent: "
                f"{new_alerts}"
            )

        finally:

            browser.close()

    # ========================================================
    # RECORD EXACT LAST RUN TIME
    # ========================================================

    # This is recorded AFTER the monitoring process has
    # completed successfully.
    alerts_data["last_run"] = timestamp_string()

    # Make sure the date is also current
    alerts_data["date"] = today_string()

    # Save everything
    save_alerts(alerts_data)

    # --------------------------------------------------------
    # FINAL STATUS
    # --------------------------------------------------------

    print(
        f"Last run recorded as: "
        f"{alerts_data['last_run']}"
    )

    print(
        f"Total alerted matches today: "
        f"{len(alerts_data['alerted_ids'])}"
    )

    print("=" * 60)
    print("MONITOR FINISHED SUCCESSFULLY")
    print("=" * 60)


# ============================================================
# RUN SCRIPT
# ============================================================

if __name__ == "__main__":
    main()
