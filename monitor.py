"""MelBet live sports monitor.

Alert conditions:
    * A live event in football, tennis, basketball, hockey, volleyball,
      handball, baseball or cricket, at any stage of play.
    * Any available market/selection with decimal odds from 1.001 to 1.008,
      including both endpoints. Alert messages retain the actual quoted odds.
    * Exclude virtual sports, simulated events and esports, including those
      listed inside a normal sport such as football or basketball.

Each run selects the first 400 eligible live games in discovery order, across
these eight sports (or fewer when fewer are available). Six event pages are checked
concurrently. Discovery and event pages have short time limits, and the scan
has a five-minute budget. Readable partial results are retained on timeouts.
Matches are emailed during the scan. A final feedback email and last_scan.json
report include checked counts, matches, skipped games and any page failures,
even when no qualifying odds are found. Page failures do not discard results.

Email subject, SMTP environment variables, daily duplicate prevention in
alerts.json and Africa/Lagos timestamps are retained. The old minute, score,
goal-distance and Under-only conditions have been removed.

Install: pip install playwright && python -m playwright install chromium
Run once: python melbet_live_monitor.py
Inspect without sending emails or changing alerts.json:
    python melbet_live_monitor.py --dry-run
Keep the existing scheduler to run the script repeatedly.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import smtplib
import ssl
import sys
import time
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


LIVE_PAGE_URL = "https://melbet.com/en/live"
MIN_ODDS = Decimal("1.001")
MAX_ODDS = Decimal("1.008")
ALLOWED_SPORTS = (
    "Football", "Tennis", "Basketball", "Hockey", "Volleyball",
    "Handball", "Baseball", "Cricket",
)
SPORT_ALIASES = {name.casefold(): name for name in ALLOWED_SPORTS}
SPORT_ALIASES.update({
    "soccer": "Football", "lawn tennis": "Tennis",
    "ice hockey": "Hockey", "icehockey": "Hockey",
    "field hockey": "Hockey", "fieldhockey": "Hockey",
})
TIMEZONE = ZoneInfo("Africa/Lagos")
ALERTS_FILE = "alerts.json"
REPORT_FILE = "last_scan.json"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_TO = os.environ.get("ALERT_TO")

MAX_GAMES = 400
CONCURRENT_PAGES = 6
SCAN_TIMEOUT_SECONDS = 300
DISCOVERY_TIMEOUT_SECONDS = 45
EVENT_TIMEOUT_SECONDS = 15
PAGE_TIMEOUT_MS = 10000
INITIAL_WAIT_MS = 750
SETTLE_WAIT_MS = 250
STABLE_ROUNDS = 2
CATALOGUE_SETTLE_SECONDS = 5
DETAIL_SETTLE_SECONDS = 2
MAX_DISCOVERY_ROUNDS = 20

# Inspect metadata AND league/URL labels. A normal "Football" sport label alone
# does not prove that a FIFA/eFootball competition is a real football match.
# Do not block "FIFA" alone: the real FIFA World Cup must remain eligible.
EXCLUDED_LABELS = re.compile(
    r"\b(?:virtual(?:s|\s+sports?)?|esports?|e\s+sports?|cyber\w*|"
    r"simulat\w*|srl|computer\s+generated|electronic\s+(?:sports?|leagues?)|"
    r"e\s+(?:soccer|football|basketball|hockey|tennis|cricket)|"
    r"efootball|ebasketball|ehockey|esoccer|etennis|ecricket|"
    r"fifa\s*(?:\d{2}(?!\d)|online|volta|e(?:world|club)|esports?)|"
    r"ea\s*(?:sports\s*)?fc|pro\s+evolution\s+soccer|pes(?:\s*\d+)?|"
    r"nba\s*2k\w*|nhl\s*\d{2}(?!\d)|madden\w*|"
    r"dota(?:\s*2)?|counter\s*strike\w*|cs\s*(?:2|go)|"
    r"league\s+of\s+legends|lol|valorant|rocket\s+league|"
    r"starcraft\w*|warcraft\w*|world\s+of\s+warcraft|overwatch\w*|"
