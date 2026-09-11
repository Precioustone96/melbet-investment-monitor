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

Designed to run once per GitHub Actions invocation.
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
# CONFIGURATION
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

MINUTE_THRESHOLD = 70
ODDS_THRESHOLD = 1.02

ALERTS_FILE = "alerts.json"

# Nigeria timezone
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
# DATE / ALERT STORAGE
# ============================================================

def today_string():
    """Return today's date using Nigeria/Lagos time."""
    return datetime.now(TIMEZONE).date().isoformat()


def load_alerts():

    today = today_string()

    if not os.path.exists(ALERTS_FILE):

        return {
            "date": today,
            "alerted_ids": []
        }

    try:

        with open(
            ALERTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

    except (
        json.JSONDecodeError,
        OSError
    ):

        print(
            "alerts.json could not be read. "
            "Creating a new alert state."
        )

        return {
            "date": today,
            "alerted_ids": []
        }

    # Reset alerts when a new day begins
    if data.get("date") != today:

        return {
            "date": today,
            "alerted_ids": []
        }

    # Ensure alerted_ids is valid
    if not isinstance(
        data.get("alerted_ids"),
        list
    ):

        data["alerted_ids"] = []

    return data


def save_alerts(data):

    with open(
        ALERTS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2
        )


# ============================================================
# MATCH MINUTE
# ============================================================

def get_match_minute(match):

    scores = match.get("scores") or {}

    status = scores.get(
        "statusLineStr",
        ""
    )

    if not status:
        return None

    # Example:
    # "85 minutes"
    match_minute = re.search(
        r"(\d+)\s*minutes?",
        status,
        re.IGNORECASE
    )

    if match_minute:

        return int(
            match_minute.group(1)
        )

    return None


# ============================================================
# FIND QUALIFYING TOTAL GOALS ODDS
# ============================================================

def find_qualifying_lines(match):

    qualifying_lines = []

    groups = match.get(
        "centralBlockEventGroups",
        []
    )

    for group in groups:

        # groupId 17 = Total Goals
        if group.get("groupId") != 17:
            continue

        events = group.get(
            "events",
            []
        )

        # The API structure is:
        #
        # events = [
        #     [Over events...],
        #     [Under events...]
        # ]
        #
        # type 9 = Over
        # type 10 = Under

        for event_group in events:

            if not isinstance(
                event_group,
                list
            ):
                continue

            for event in event_group:

                if not isinstance(
                    event,
                    dict
                ):
                    continue

                event_type = event.get("type")

                if event_type == 9:
                    side = "Over"

                elif event_type == 10:
                    side = "Under"

                else:
                    continue

                odd = event.get("cf")

                if odd is None:
                    continue

                try:

                    odd = float(odd)

                except (
                    TypeError,
                    ValueError
                ):

                    continue

                # Check the required odds condition
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

    # Exact email title requested
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

        server.send_message(
            message
        )


# ============================================================
# CHECK MATCHES
# ============================================================

def check_matches(
    matches,
    alerts_data
):

    alerted_ids = set(
        str(match_id)
        for match_id
        in alerts_data.get(
            "alerted_ids",
            []
        )
    )

    new_alerts = 0

    for match in matches:

        if not isinstance(
            match,
            dict
        ):
            continue

        # Get unique match ID
        match_id = (
            match.get("id")
            or match.get("mainGameId")
        )

        if match_id is None:
            continue

        match_id = str(match_id)

        # Skip matches already alerted today
        if match_id in alerted_ids:

            continue

        # Get match minute
        minute = get_match_minute(
            match
        )

        if minute is None:

            continue

        if minute < MINUTE_THRESHOLD:

            continue

        # Find Total Goals Over/Under
        # odds <= 1.02
        qualifying_lines = (
            find_qualifying_lines(
                match
            )
        )

        if not qualifying_lines:

            continue

        # Match details
        opponent1 = (
            match.get("opponent1")
            or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        opponent2 = (
            match.get("opponent2")
            or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        league = (
            match.get("liga")
            or {}
        ).get(
            "name",
            "Unknown League"
        )

        scores = (
            match.get("scores")
            or {}
        )

        score = scores.get(
            "fullScore",
            "?"
        )

        # Build qualifying odds list
        lines = []

        for line in qualifying_lines:

            lines.append(
                f"{line['side']} "
                f"{line['parameter']} "
                f"@ {line['odd']}"
            )

        qualifying_text = "\n".join(
            lines
        )

        # Email body
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
""".strip()

        print("\n" + "=" * 60)

        print("QUALIFYING MATCH FOUND")

        print("=" * 60)

        print(email_body)

        print("=" * 60)

        # IMPORTANT:
        #
        # Only add the match to alerted_ids
        # AFTER the email successfully sends.

        try:

            send_email(
                email_body
            )

            print(
                "Email sent successfully."
            )

            # Mark match as alerted ONLY
            # after successful email delivery
            alerted_ids.add(
                match_id
            )

            new_alerts += 1

        except Exception as error:

            print(
                "EMAIL FAILED:"
            )

            print(error)

            # Do NOT save the match ID here.
            # A future run can retry.

    alerts_data[
        "alerted_ids"
    ] = sorted(
        alerted_ids
    )

    return new_alerts


# ============================================================
# MAIN
# ============================================================

def main():

    now = datetime.now(
        TIMEZONE
    )

    print(
        f"[{now.isoformat()}]"
    )

    print(
        "Starting MelBet monitor..."
    )

    alerts_data = load_alerts()

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

            print(
                "Opening MelBet live football page..."
            )

            page.goto(
                LIVE_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            # Give the page time to establish
            # its session/cookies
            page.wait_for_timeout(
                5000
            )

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

            matches = response.json()

            # Confirm API returned a list
            if not isinstance(
                matches,
                list
            ):

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

            new_alerts = (
                check_matches(
                    matches,
                    alerts_data
                )
            )

            print(
                f"New alerts sent: "
                f"{new_alerts}"
            )

        finally:

            browser.close()

    # Save alert state
    save_alerts(
        alerts_data
    )

    print(
        "Monitor finished successfully."
    )


if __name__ == "__main__":

    main()
