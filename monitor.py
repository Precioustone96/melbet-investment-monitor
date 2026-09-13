"""
MelBet Live Football Monitor

QUALIFICATION RULES:

    - Football match
    - Match minute >= 67
    - Current score is used to calculate the required Total Goals line
    - R goal distance = 4 by default
    - Only UNDER Total Goals markets are considered
    - Required Under line must be at least R goals away from the
      current score
    - Odds <= 1.02

Example:

    Minute: 67'
    Score: 2-0
    Current total: 2 goals
    R: 4

    Required line:
        2 + 4 - 0.5 = Under 5.5

    Therefore:

        Under 5.5 @ 1.02  -> QUALIFIES
        Under 4.5 @ 1.02  -> DOES NOT QUALIFY

When a match qualifies:
    - Sends an email with subject "Investment Alert"
    - Records the match ID in alerts.json
    - Does not alert for that same match again on the same day

alerts.json also records:
    - Current calendar date
    - Exact time of the last completed run
    - Match IDs already alerted today

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

# Start checking from the 67th minute
MINUTE_THRESHOLD = 67

# Maximum acceptable odds
ODDS_THRESHOLD = 1.05

# Required goal distance from current score
#
# Example:
# Current total = 2
# R = 4
#
# Required Under line:
# 2 + 4 - 0.5 = 5.5
#
GOAL_DISTANCE = 4


# ============================================================
# FILE / TIMEZONE SETTINGS
# ============================================================

ALERTS_FILE = "alerts.json"

# Nigeria time
TIMEZONE = ZoneInfo("Africa/Lagos")


# ============================================================
# EMAIL SETTINGS
# ============================================================

SMTP_HOST = os.environ.get(
    "SMTP_HOST",
    "smtp.gmail.com"
)

SMTP_PORT = int(
    os.environ.get(
        "SMTP_PORT",
        "587"
    )
)

SMTP_USER = os.environ.get(
    "SMTP_USER"
)

SMTP_PASSWORD = os.environ.get(
    "SMTP_PASSWORD"
)

ALERT_TO = os.environ.get(
    "ALERT_TO"
)


# ============================================================
# TIME FUNCTIONS
# ============================================================

def current_datetime():
    """
    Return current date/time in Nigeria.
    """
    return datetime.now(TIMEZONE)


def today_string():
    """
    Return today's date.

    Example:
        2026-09-13
    """
    return current_datetime().date().isoformat()


def timestamp_string():
    """
    Return exact current Nigeria time.

    Example:
        2026-09-13 14:35:27 WAT
    """
    return current_datetime().strftime(
        "%Y-%m-%d %H:%M:%S WAT"
    )


# ============================================================
# ALERT STATE
# ============================================================

def load_alerts():
    """
    Load alerts.json.

    If the saved date is different from today,
    the alerted match list is reset.

    This allows the same match to qualify again
    on a different calendar day.
    """

    today = today_string()

    # --------------------------------------------------------
    # File does not exist
    # --------------------------------------------------------

    if not os.path.exists(ALERTS_FILE):

        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    # --------------------------------------------------------
    # Read existing file
    # --------------------------------------------------------

    try:

        with open(
            ALERTS_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

    except (json.JSONDecodeError, OSError):

        print(
            "WARNING: alerts.json could not be read."
        )

        print(
            "Creating a new alert state."
        )

        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    # --------------------------------------------------------
    # New day
    # --------------------------------------------------------

    if data.get("date") != today:

        print(
            "New calendar day detected."
        )

        print(
            "Resetting today's alerted match list."
        )

        return {
            "date": today,
            "last_run": None,
            "alerted_ids": []
        }

    # --------------------------------------------------------
    # Validate alerted_ids
    # --------------------------------------------------------

    if not isinstance(
        data.get("alerted_ids"),
        list
    ):

        data["alerted_ids"] = []

    # --------------------------------------------------------
    # Make sure last_run exists
    # --------------------------------------------------------

    if "last_run" not in data:

        data["last_run"] = None

    return data


def save_alerts(data):
    """
    Save current alert state to alerts.json.
    """

    with open(
        ALERTS_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            indent=2
        )

    print(
        "alerts.json updated successfully."
    )


# ============================================================
# MATCH MINUTE
# ============================================================

def get_match_minute(match):
    """
    Extract match minute from:

        scores.statusLineStr

    Example:

        "67 minutes"

    Returns:
        67
    """

    scores = match.get(
        "scores"
    ) or {}

    status = scores.get(
        "statusLineStr",
        ""
    )

    if not status:

        return None

    minute_match = re.search(
        r"(\d+)\s*minutes?",
        status,
        re.IGNORECASE
    )

    if minute_match:

        return int(
            minute_match.group(1)
        )

    return None


# ============================================================
# SCORE PARSING
# ============================================================

def get_current_total_goals(match):
    """
    Extract the current score and calculate
    the current total number of goals.

    Example:

        fullScore = "2:0"

        Current total = 2

    Another possible format:

        "2-0"

    Returns:
        integer total goals

    Returns None if the score cannot be parsed.
    """

    scores = match.get(
        "scores"
    ) or {}

    full_score = scores.get(
        "fullScore"
    )

    if not full_score:

        return None

    # Convert unusual separators to a standard format
    cleaned_score = str(
        full_score
    ).strip()

    # Find two score numbers.
    #
    # Handles examples such as:
    #   2:0
    #   2-0
    #   2 : 0
    #   2 - 0
    #
    score_match = re.search(
        r"(\d+)\s*[-:]\s*(\d+)",
        cleaned_score
    )

    if not score_match:

        return None

    try:

        home_goals = int(
            score_match.group(1)
        )

        away_goals = int(
            score_match.group(2)
        )

    except ValueError:

        return None

    return home_goals + away_goals


def get_score_string(match):
    """
    Return the score exactly as provided by MelBet.
    """

    scores = match.get(
        "scores"
    ) or {}

    return scores.get(
        "fullScore",
        "?"
    )


# ============================================================
# REQUIRED UNDER LINE
# ============================================================

def calculate_required_under_line(
    current_total_goals
):
    """
    Calculate the minimum qualifying Under line.

    Formula:

        current total + GOAL_DISTANCE - 0.5

    Example:

        Current total = 2
        GOAL_DISTANCE = 4

        2 + 4 - 0.5
        = 5.5

        Required market = Under 5.5
    """

    return (
        current_total_goals
        + GOAL_DISTANCE
        - 0.5
    )


# ============================================================
# EXTRACT NUMERIC TOTAL GOALS PARAMETER
# ============================================================

def parse_parameter(parameter):
    """
    Convert a Total Goals parameter into a number.

    Examples:

        "5.5" -> 5.5
        5.5   -> 5.5
        "5"   -> 5.0

    Returns None if it cannot be converted.
    """

    if parameter is None:

        return None

    try:

        return float(
            str(parameter).strip()
        )

    except (
        TypeError,
        ValueError
    ):

        return None


# ============================================================
# FIND QUALIFYING TOTAL GOALS LINES
# ============================================================

def find_qualifying_lines(match):
    """
    Find qualifying UNDER Total Goals options.

    Conditions:

        1. groupId == 17
           = Total Goals

        2. type == 10
           = Under

        3. Under line >= required line

        4. Odds <= 1.02
    """

    qualifying_lines = []

    # --------------------------------------------------------
    # Current score
    # --------------------------------------------------------

    current_total_goals = (
        get_current_total_goals(
            match
        )
    )

    if current_total_goals is None:

        print(
            "Could not parse current score "
            "for a match."
        )

        return qualifying_lines

    # --------------------------------------------------------
    # Calculate required line
    # --------------------------------------------------------

    required_under_line = (
        calculate_required_under_line(
            current_total_goals
        )
    )

    print(
        f"Current total goals: "
        f"{current_total_goals}"
    )

    print(
        f"Required Under line: "
        f"{required_under_line}"
    )

    # --------------------------------------------------------
    # Get Total Goals group
    # --------------------------------------------------------

    groups = match.get(
        "centralBlockEventGroups",
        []
    )

    if not isinstance(
        groups,
        list
    ):

        return qualifying_lines

    for group in groups:

        if not isinstance(
            group,
            dict
        ):

            continue

        # groupId 17 = Total Goals
        if group.get(
            "groupId"
        ) != 17:

            continue

        events = group.get(
            "events",
            []
        )

        if not isinstance(
            events,
            list
        ):

            continue

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

                # ------------------------------------------------
                # ONLY UNDER
                # type 10 = Under
                # ------------------------------------------------

                if event.get(
                    "type"
                ) != 10:

                    continue

                odd = event.get(
                    "cf"
                )

                if odd is None:

                    continue

                try:

                    odd = float(
                        odd
                    )

                except (
                    TypeError,
                    ValueError
                ):

                    continue

                # ------------------------------------------------
                # ODDS CONDITION
                # ------------------------------------------------

                if odd > ODDS_THRESHOLD:

                    continue

                # ------------------------------------------------
                # TOTAL GOALS PARAMETER
                # ------------------------------------------------

                parameter = event.get(
                    "parameter"
                )

                parameter_number = (
                    parse_parameter(
                        parameter
                    )
                )

                if parameter_number is None:

                    continue

                # ------------------------------------------------
                # GOAL DISTANCE CONDITION
                # ------------------------------------------------

                if (
                    parameter_number
                    < required_under_line
                ):

                    continue

                # ------------------------------------------------
                # QUALIFIES
                # ------------------------------------------------

                qualifying_lines.append({

                    "side": "Under",

                    "parameter": parameter,

                    "odd": odd,

                    "required_line":
                        required_under_line,

                    "current_total":
                        current_total_goals
                })

    return qualifying_lines


# ============================================================
# SEND EMAIL
# ============================================================

def send_email(body):
    """
    Send investment alert email.

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

    # Exact requested subject
    message["Subject"] = (
        "Investment Alert"
    )

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
# CHECK ALL LIVE MATCHES
# ============================================================

def check_matches(
    matches,
    alerts_data
):
    """
    Check all live matches.

    Qualification:

        Minute >= 67

        AND

        Under Total Goals line is at least
        R goals away from the current score

        AND

        Odds <= 1.02

    R is controlled by:

        GOAL_DISTANCE = 4

    Once the email successfully sends,
    the match ID is recorded for the day.
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

        if not isinstance(
            match,
            dict
        ):

            continue

        # ----------------------------------------------------
        # MATCH ID
        # ----------------------------------------------------

        match_id = (
            match.get("id")
            or match.get("mainGameId")
        )

        if match_id is None:

            continue

        match_id = str(
            match_id
        )

        # ----------------------------------------------------
        # ALREADY ALERTED TODAY?
        # ----------------------------------------------------

        if match_id in alerted_ids:

            continue

        # ----------------------------------------------------
        # CHECK MINUTE
        # ----------------------------------------------------

        minute = get_match_minute(
            match
        )

        if minute is None:

            continue

        if minute < MINUTE_THRESHOLD:

            continue

        # ----------------------------------------------------
        # GET SCORE
        # ----------------------------------------------------

        current_total_goals = (
            get_current_total_goals(
                match
            )
        )

        if current_total_goals is None:

            print(
                f"Could not parse score "
                f"for match {match_id}."
            )

            continue

        # ----------------------------------------------------
        # FIND QUALIFYING MARKET
        # ----------------------------------------------------

        qualifying_lines = (
            find_qualifying_lines(
                match
            )
        )

        if not qualifying_lines:

            continue

        # ----------------------------------------------------
        # MATCH INFORMATION
        # ----------------------------------------------------

        opponent1 = (
            match.get(
                "opponent1"
            ) or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        opponent2 = (
            match.get(
                "opponent2"
            ) or {}
        ).get(
            "fullName",
            "Unknown Team"
        )

        league = (
            match.get(
                "liga"
            ) or {}
        ).get(
            "name",
            "Unknown League"
        )

        score = get_score_string(
            match
        )

        required_under_line = (
            calculate_required_under_line(
                current_total_goals
            )
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

        qualifying_text = (
            "\n".join(lines)
        )

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

Current total goals:
{current_total_goals}

Required goal distance:
{GOAL_DISTANCE} goals

Minimum qualifying Under line:
Under {required_under_line}

Qualifying Total Goals Option(s):

{qualifying_text}

Match ID:
{match_id}

Conditions:
Minute >= {MINUTE_THRESHOLD}
Goal distance >= {GOAL_DISTANCE}
Odds <= {ODDS_THRESHOLD}

Alert Time:
{timestamp_string()}
""".strip()

        # ----------------------------------------------------
        # PRINT ALERT
        # ----------------------------------------------------

        print(
            "\n" + "=" * 60
        )

        print(
            "QUALIFYING MATCH FOUND"
        )

        print(
            "=" * 60
        )

        print(
            email_body
        )

        print(
            "=" * 60
        )

        # ----------------------------------------------------
        # SEND EMAIL
        # ----------------------------------------------------

        try:

            send_email(
                email_body
            )

            print(
                "Email sent successfully."
            )

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Only mark the match as alerted AFTER
            # the email successfully sends.
            # ------------------------------------------------

            alerted_ids.add(
                match_id
            )

            new_alerts += 1

        except Exception as error:

            print(
                "EMAIL FAILED:"
            )

            print(
                error
            )

            print(
                "Match was NOT added "
                "to alerted_ids."
            )

            print(
                "The monitor will retry it "
                "on the next run if it qualifies."
            )

    # --------------------------------------------------------
    # Save IDs back into state
    # --------------------------------------------------------

    alerts_data["alerted_ids"] = sorted(
        alerted_ids
    )

    return new_alerts


# ============================================================
# MAIN MONITOR
# ============================================================

def main():

    print(
        "=" * 60
    )

    print(
        "MELBET INVESTMENT MONITOR"
    )

    print(
        "=" * 60
    )

    print(
        f"Run started: "
        f"{timestamp_string()}"
    )

    print(
        f"Minimum minute: "
        f"{MINUTE_THRESHOLD}"
    )

    print(
        f"Goal distance (R): "
        f"{GOAL_DISTANCE}"
    )

    print(
        f"Maximum odds: "
        f"{ODDS_THRESHOLD}"
    )

    # --------------------------------------------------------
    # LOAD ALERT STATE
    # --------------------------------------------------------

    alerts_data = load_alerts()

    print(
        f"Alert date: "
        f"{alerts_data['date']}"
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

            # =================================================
            # OPEN MELBET
            # =================================================

            print(
                "Opening MelBet live football page..."
            )

            # -------------------------------------------------
            # IMPORTANT FIX:
            #
            # Do NOT wait for "domcontentloaded".
            #
            # MelBet can keep the page/network activity
            # running long enough to cause a 60-second
            # Playwright timeout on GitHub Actions.
            #
            # "commit" lets navigation establish without
            # waiting for the entire page to finish loading.
            # -------------------------------------------------

            try:

                page.goto(
                    LIVE_PAGE_URL,
                    wait_until="commit",
                    timeout=30000
                )

                print(
                    "MelBet page connection established."
                )

            except Exception as error:

                print(
                    "WARNING: MelBet page navigation "
                    "did not complete normally."
                )

                print(
                    f"Navigation message: {error}"
                )

                print(
                    "Continuing to the live-feed API..."
                )

            # Give MelBet a few seconds to establish
            # the browser session/cookies.
            page.wait_for_timeout(
                5000
            )

            # =================================================
            # FETCH LIVE DATA
            # =================================================

            print(
                "Fetching live match data..."
            )

            response = page.request.get(
                GAMES_API_URL,
                timeout=60000
            )

            # ------------------------------------------------
            # CHECK API RESPONSE
            # ------------------------------------------------

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

            # =================================================
            # CHECK MATCHES
            # =================================================

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
    # RECORD LAST COMPLETED RUN
    # ========================================================

    alerts_data["last_run"] = (
        timestamp_string()
    )

    alerts_data["date"] = (
        today_string()
    )

    # ========================================================
    # SAVE ALERT STATE
    # ========================================================

    save_alerts(
        alerts_data
    )

    # ========================================================
    # FINAL STATUS
    # ========================================================

    print(
        f"Last run recorded as: "
        f"{alerts_data['last_run']}"
    )

    print(
        f"Total alerted matches today: "
        f"{len(alerts_data['alerted_ids'])}"
    )

    print(
        "=" * 60
    )

    print(
        "MONITOR FINISHED SUCCESSFULLY"
    )

    print(
        "=" * 60
    )


# ============================================================
# START PROGRAM
# ============================================================

if __name__ == "__main__":
    main()
