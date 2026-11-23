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
    r"pubg|fortnite|rainbow\s+six|call\s+of\s+duty|hearthstone|"
    r"mobile\s+legends|honou?r\s+of\s+kings|arena\s+of\s+valo[u]?r|"
    r"free\s+fire|apex\s+legends|age\s+of\s+empires|quake\w*|"
    r"mortal\s+kombat|tekken\w*|street\s+fighter|smite\w*|"
    r"crossfire|standoff\w*|clash\s+royale|brawl\s+stars|"
    r"vr\s+sports?|marble\w*|casino|slots?|roulette|lottery|bingo|poker|"
    r"politics|elections?|eurovision|tv\s+games?|fast\s+games?)\b",
    re.IGNORECASE,
)
EXCLUDED_FLAGS = {
    "isvirtual", "virtual", "isvirtualsport", "isvirtualsports",
    "isesport", "isesports", "esport", "esports", "iscyber", "cyber",
    "issimulated", "simulated", "issimulation", "simulation",
    "iscomputerGenerated".lower(),
}
UNAVAILABLE_FLAGS = {
    "blocked", "isblocked", "suspended", "issuspended", "closed", "isclosed",
    "finished", "isfinished", "cancelled", "iscancelled", "canceled",
    "iscanceled", "deleted", "isdeleted",
}
TERMINAL_STATUS = re.compile(
    r"\b(?:finished|ended|full\s*time|cancelled|canceled|abandoned|"
    r"not\s+started|scheduled|prematch|pre\s+match|game\s+over|match\s+over)\b", re.IGNORECASE
)
METADATA_KEYS = (
    "sport", "liga", "league", "tournament", "competition", "category",
    "country", "opponent1", "opponent2", "gameTypeName", "typeName",
    "kindName", "name", "title", "url", "_url",
)
MARKET_CONTAINERS = {
    "eventGroups", "centralBlockEventGroups", "marketGroups", "markets",
    "events", "outcomes", "selections", "bets",
}


class IncompleteScanError(RuntimeError):
    pass


def current_datetime():
    return datetime.now(TIMEZONE)


def today_string():
    return current_datetime().date().isoformat()


def timestamp_string():
    return current_datetime().strftime("%Y-%m-%d %H:%M:%S WAT")


def load_alerts():
    empty = {"date": today_string(), "last_run": None, "alerted_ids": []}
    try:
        with open(ALERTS_FILE, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or data.get("date") != today_string():
        return empty
    if not isinstance(data.get("alerted_ids"), list):
        data["alerted_ids"] = []
    data.setdefault("last_run", None)
    return data


def save_alerts(data):
    temporary_path = ALERTS_FILE + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
    os.replace(temporary_path, ALERTS_FILE)


def normalise_label(value):
    return re.sub(r"[-_./]+", " ", str(value)).strip()


def allowed_sport_name(sport):
    """Match whole sport names/slugs, not substrings like 'table tennis'."""
    if isinstance(sport, dict):
        sport = sport.get("name") or sport.get("slug") or ""
    label = " ".join(normalise_label(sport).casefold().split())
    return SPORT_ALIASES.get(label)


def true_flag(value):
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def false_flag(value):
    return value is False or str(value).strip().lower() in {"0", "false", "no"}


def metadata_is_excluded(value):
    if isinstance(value, dict):
        for key, child in value.items():
            flag = key.replace("_", "").lower()
            if flag in EXCLUDED_FLAGS and true_flag(child):
                return True
            # Odds/market descriptions are not used to classify the sport.
            if key not in MARKET_CONTAINERS and metadata_is_excluded(child):
                return True
    elif isinstance(value, list):
        return any(metadata_is_excluded(child) for child in value)
    elif isinstance(value, str):
        return bool(EXCLUDED_LABELS.search(normalise_label(value)))
    return False


def unavailable(value):
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        flag = key.replace("_", "").lower()
        if flag in UNAVAILABLE_FLAGS and true_flag(child):
            return True
        if flag in {"isactive", "active", "enabled", "isenabled"} and false_flag(child):
            return True
    return False


def is_real_live_game(game):
    if not isinstance(game, dict) or unavailable(game):
        return False
    # Records come only from /en/live pages/their feeds. Explicit contrary
    # metadata takes precedence; no football minute or score is required.
    if "isLive" in game and false_flag(game["isLive"]):
        return False
    sport = game.get("sport")
    if not isinstance(sport, dict) or not (sport.get("name") or sport.get("slug")):
        return False
    if allowed_sport_name(sport) is None:
        return False
    if normalise_label(sport.get("name", sport.get("slug", ""))).lower() == "fifa":
        return False
    selected = {key: game[key] for key in METADATA_KEYS if key in game}
    selected.update({key: value for key, value in game.items()
                     if key.replace("_", "").lower() in EXCLUDED_FLAGS})
    if metadata_is_excluded(selected):
        return False
    scores = game.get("scores") or {}
    if not isinstance(scores, dict):
        scores = {}
    status = " ".join(str(value) for value in (
        game.get("status", ""), scores.get("statusLineStr", ""),
        scores.get("status", ""),
    ) if isinstance(value, str))
    status = normalise_label(status)
    return not (TERMINAL_STATUS.search(status) or status.upper() in {"FT", "AET", "FINAL"})


def is_game_record(value):
    return (isinstance(value, dict) and value.get("id") is not None
            and isinstance(value.get("sport"), dict)
            and any(key in value for key in ("opponent1", "scores", "liga", "league")))


def game_records(value):
    if isinstance(value, dict):
        if is_game_record(value):
            yield value
        for key, child in value.items():
            if key not in MARKET_CONTAINERS:
                yield from game_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from game_records(child)


def has_market_data(value):
    if isinstance(value, dict):
        if "cf" in value or any(key in value for key in (
            "eventGroups", "centralBlockEventGroups", "marketGroups", "markets",
        )):
            return True
        return any(has_market_data(child) for child in value.values()
                   if isinstance(child, (dict, list)))
    if isinstance(value, list):
        return any(has_market_data(child) for child in value)
    return False


def parse_odd(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def find_qualifying_options(payload, game_id=None):
    """Search all market groups, using the raw odd rather than rounded cfView."""
    options = {}

    def visit(node, group=None, in_markets=False, scope=None, period=None):
        if isinstance(node, list):
            for child in node:
                visit(child, group, in_markets, scope, period)
            return
        if not isinstance(node, dict) or unavailable(node):
            return
        if is_game_record(node):
            if not is_real_live_game(node):
                return
            if game_id is not None and str(node.get("id")) != str(game_id):
                if str(node.get("mainGameId")) != str(game_id):
                    return
            scope = str(node["id"])
            period = node.get("periodName") or period
        if "groupId" in node or "marketId" in node:
            group = node
        odd = parse_odd(node.get("cf")) if in_markets else None
        if odd is not None and MIN_ODDS <= odd <= MAX_ODDS:
            market = group or {}
            market_id = market.get("groupId", market.get("marketId", "?"))
            market_name = (market.get("groupName") or market.get("name")
                           or market.get("title") or f"Market {market_id}")
            selection = (node.get("name") or node.get("title") or node.get("label")
                         or f"Selection type {node.get('type', node.get('id', '?'))}")
            parameter = node.get("parameter")
            params = node.get("eventParams") or {}
            extra = params.get("params", []) if isinstance(params, dict) else []
            key = (str(market_id), str(node.get("type", node.get("id", selection))),
                   json.dumps(parameter, sort_keys=True), json.dumps(extra, sort_keys=True),
                   str(node.get("gameId", scope or "")))
            options[key] = {
                "market": market_name, "market_id": market_id,
                "selection": selection, "parameter": parameter,
                "params": extra, "odd": str(odd), "period_id": scope,
                "period": node.get("periodName") or market.get("periodName") or period,
            }
        for key, child in node.items():
            if isinstance(child, (dict, list)):
                visit(child, group, in_markets or key in MARKET_CONTAINERS, scope, period)

    visit(payload, scope=str(game_id) if game_id is not None else None)
    return list(options.values())


def slugify(value):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def resource_key(url):
    parts = urlsplit(url)
    volatile = {"_", "ts", "timestamp", "_t", "time", "rnd", "random", "nonce"}
    query = [(key, value) for key, value in parse_qsl(parts.query)
             if key.lower() not in volatile]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))


class LiveScanner:
    """Select a bounded catalogue, then inspect independent event pages."""

    def __init__(self, page):
        self.page = page
        self.games = {}
        self.event_urls = {}
        self.sport_urls = {}  # Preserve the site's discovery order.
        self.visited_sports = set()
        self.feed_seen = False
        self.current_game_id = None
        self.detail_payloads = {}
        self.results = []
        self.errors = []
        self.pending_responses = set()
        self.selected_ids = []
        self.attempted_ids = set()
        self.checked_ids = set()
        self.readable_ids = set()
        self.skipped_alerted = 0
        self.skipped_inactive = 0
        self.budget_reached = False
        self.discovered_count = 0
        page.on("response", self.capture_response)

    def eligible_games(self):
        # A half/set/period record must not consume another game's slot.
        return [game_id for game_id, game in self.games.items()
                if is_real_live_game(game)
                and str(game.get("mainGameId") or game_id) == game_id]

    def add_error(self, error):
        message = str(error).strip().splitlines()[0] or type(error).__name__
        if message not in self.errors:
            self.errors.append(message)

    def register_game(self, game):
        game_id = str(game["id"])
        existing = self.games.get(game_id, {})
        merged = dict(existing)
        for key, value in game.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        self.games[game_id] = merged

    def capture_response(self, response):
        parts = urlsplit(response.url)
        if (parts.hostname != urlsplit(LIVE_PAGE_URL).hostname
                or response.status != 200
                or not re.search(r"/(?:service-api|LiveFeed)/", parts.path, re.I)
                or not re.search(r"feed|game|event|market|sport|live", parts.path, re.I)
                or "json" not in response.headers.get("content-type", "").lower()):
            return
        # Freeze the event scope before awaiting the response body.
        task = asyncio.create_task(self.read_response(response, self.current_game_id))
        self.pending_responses.add(task)
        task.add_done_callback(self.pending_responses.discard)

    async def read_response(self, response, event_id):
        try:
            data = await response.json()
            self.process_response(response.url, data, event_id)
        except Exception:
            # An unreadable response is detected by the event coverage check.
            return

    def process_response(self, url, data, event_id):
        parts = urlsplit(url)
        self.feed_seen = True
        records = list(game_records(data))
        live_source = bool(re.search(
            r"/LiveFeed/|(?:^|[/_-])live(?:[/_-]|$)", parts.path, re.I
        ))
        for game in records:
            game_id = str(game["id"])
            if (live_source or game_id in self.event_urls
                    or game_id == event_id or true_flag(game.get("isLive"))):
                self.register_game(game)
        game_id = event_id
        if game_id is None:
            return
        # Catalogue responses contain previews, not the complete event markets.
        if re.search(r"/main-live-feed/|/Get1x2", parts.path, re.I):
            return
        scoped = []
        for game in records:
            if str(game["id"]) == game_id:
                scoped.append(game)
        # Market-only endpoints commonly carry the game ID in the URL. When
        # they include a wrapper with game metadata plus sibling markets, keep
        # the wrapper as well. The market walker rejects other game records.
        if re.search(rf"(?<!\d){re.escape(game_id)}(?!\d)", url):
            scoped.append(data)
        if scoped:
            self.detail_payloads[resource_key(url)] = scoped

    async def finish_responses(self):
        tasks = list(self.pending_responses)
        if tasks:
            await asyncio.wait(tasks, timeout=0.5)

    async def collect_links(self):
        links = await self.page.locator("a[href]").evaluate_all(
            "nodes => nodes.map(a => ({href: a.href, text: a.textContent || ''}))"
        )
        base = urlsplit(LIVE_PAGE_URL)
        for link in links:
            parts = urlsplit(urljoin(LIVE_PAGE_URL, link["href"]))
            if parts.hostname != base.hostname or not parts.path.startswith("/en/live/"):
                continue
            if metadata_is_excluded(parts.path + " " + link["text"]):
                continue
            segments = parts.path.rstrip("/").split("/")[3:]
            if not segments:
                continue
            sport_slug = segments[0]
            if allowed_sport_name(sport_slug) is None:
                continue
            sport_url = f"{base.scheme}://{base.netloc}/en/live/{sport_slug}"
            if len(segments) == 1:
                self.sport_urls.setdefault(sport_url, None)
            elif len(segments) >= 3:
                game_number = re.match(r"^(\d+)(?:-|$)", segments[-1])
                league_number = re.match(r"^(\d+)(?:-|$)", segments[-2])
                if not game_number or not league_number:
                    continue
                game_id = game_number.group(1)
                url = urlunsplit((base.scheme, base.netloc, parts.path, "", ""))
                self.sport_urls.setdefault(sport_url, None)
                self.event_urls[game_id] = url
                # URL metadata provides classification when a server-rendered
                # list has not requested a separate JSON catalogue.
                fallback = {
                    "id": game_id,
                    "sport": {"name": sport_slug.replace("-", " "), "slug": sport_slug},
                    "liga": {"id": league_number.group(1),
                             "name": re.sub(r"^\d+-?", "", segments[-2]).replace("-", " ")},
                    "_url": url,
                }
                existing = self.games.get(game_id)
                self.register_game(fallback)
                if existing:
                    self.register_game(existing)

    async def collect_embedded_data(self):
        texts = await self.page.locator('script[type="application/json"]').all_text_contents()
        for text in texts:
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                continue
            records = list(game_records(data))
            for game in records:
                game_id = str(game["id"])
                if (game_id in self.event_urls or game_id == self.current_game_id
                        or true_flag(game.get("isLive"))):
                    self.register_game(game)
                if str(game["id"]) == self.current_game_id:
                    self.detail_payloads["embedded:" + str(game["id"])] = [game]

    async def click_more(self):
        buttons = self.page.get_by_role("button", name=re.compile(
            r"^(?:(?:show|load|see|view)\s+(?:more|all)(?:\s+(?:events|matches|games|markets|bets|sports))?"
            r"|more\s+(?:events|matches|games|markets|sports))(?:\s*\(\d+\)|\s*\d+)?$", re.I
        ))
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible() and await button.is_enabled():
                await button.click(timeout=1500)
                return True
        return False

    async def scroll_lists(self):
        # Collect links before each step so virtualised lists are not reduced
        # to just the final screen. Include independently scrolling panels.
        return await self.page.evaluate("""() => {
            let moved = false;
            const roots = [document.scrollingElement,
                ...document.querySelectorAll('main, section, div, ul')];
            for (const el of new Set(roots)) {
                if (!el || el.clientHeight < 100 || el.scrollHeight <= el.clientHeight + 2) continue;
                if (el !== document.scrollingElement &&
                    !/(auto|scroll)/.test(getComputedStyle(el).overflowY)) continue;
                const before = el.scrollTop;
                el.scrollTop = Math.min(el.scrollTop + Math.max(100, el.clientHeight * .8),
                                        el.scrollHeight - el.clientHeight);
                if (el.scrollTop > before + 1) moved = true;
            }
            return moved;
        }""")

    async def navigate(self, url):
        await self.page.goto(url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)
        await self.page.wait_for_timeout(INITIAL_WAIT_MS)
        if urlsplit(self.page.url).hostname != urlsplit(LIVE_PAGE_URL).hostname:
            raise IncompleteScanError("MelBet redirected to another host; the live scan could not be verified.")

    async def settle(self, catalogue=False):
        seconds = CATALOGUE_SETTLE_SECONDS if catalogue else DETAIL_SETTLE_SECONDS
        try:
            return await asyncio.wait_for(self.settle_page(catalogue), timeout=seconds)
        except asyncio.TimeoutError:
            return False

    async def settle_page(self, catalogue):
        stable = 0
        previous = None
        for _ in range(MAX_DISCOVERY_ROUNDS):
            if catalogue:
                await self.collect_links()
            await self.collect_embedded_data()
            await self.finish_responses()
            if catalogue and len(self.eligible_games()) >= MAX_GAMES:
                return True
            signature = (len(self.games), len(self.event_urls), len(self.sport_urls),
                         tuple(sorted(self.detail_payloads)))
            more = await self.click_more()
            moving = await self.scroll_lists()
            stable = stable + 1 if signature == previous and not more and not moving else 0
            if stable >= STABLE_ROUNDS:
                return True
            previous = signature
            await self.page.wait_for_timeout(SETTLE_WAIT_MS)
        return False

    def event_url(self, game_id, game):
        if game_id in self.event_urls:
            return self.event_urls[game_id]
        sport = game.get("sport") or {}
        league = game.get("liga") or game.get("league") or {}
        sport_slug = sport.get("slug") or slugify(sport.get("name", ""))
        if not sport_slug or not league.get("id"):
            return None
        # The public event route uses the sport, league ID and game ID. Prefer
        # actual page links; this covers records not rendered in a preview.
        opponent1 = (game.get("opponent1") or {}).get("fullName", "")
        opponent2 = (game.get("opponent2") or {}).get("fullName", "")
        league_segment = f"{league['id']}-{slugify(league.get('name', 'league'))}"
        event_segment = f"{game_id}-{slugify(opponent1 + ' ' + opponent2) or 'event'}"
        return f"{LIVE_PAGE_URL}/{sport_slug}/{league_segment}/{event_segment}"

    async def read_market_tabs(self):
        # Some event pages fetch additional markets only when their tab opens.
        root = self.page.locator("main")
        root = root.first if await root.count() else self.page
        tabs = root.get_by_role("tab")
        labels = [(await tabs.nth(i).inner_text()).strip() for i in range(await tabs.count())]
        event_path = urlsplit(self.page.url).path
        for label in dict.fromkeys(labels):
            if not label:
                continue
            tab = root.get_by_role("tab", name=label, exact=True)
            if await tab.count() != 1 or not await tab.is_visible() or not await tab.is_enabled():
                continue
            if await tab.evaluate("el => !!el.closest('aside')"):
                continue
            href = await tab.get_attribute("href")
            if href and urlsplit(urljoin(self.page.url, href)).path != event_path:
                continue
            await tab.click(timeout=1500)
            await self.page.wait_for_timeout(SETTLE_WAIT_MS)
            if urlsplit(self.page.url).path != event_path:
                raise IncompleteScanError("An event tab left the live event page.")
            if not await self.settle():
                self.add_error(f"Market tab '{label}' exceeded its loading budget.")

    async def discover(self):
        url = LIVE_PAGE_URL
        while url:
            try:
                await self.navigate(url)
                settled = await self.settle(catalogue=True)
                if not settled and len(self.eligible_games()) < MAX_GAMES:
                    self.add_error(f"Catalogue {url}: loading budget reached; using discovered games.")
            except Exception as error:
                self.add_error(f"Catalogue {url}: {error}")
            if len(self.eligible_games()) >= MAX_GAMES:
                break
            url = next((candidate for candidate in self.sport_urls
                        if candidate not in self.visited_sports), None)
            if url:
                self.visited_sports.add(url)

    def available_result(self, game_id, url):
        game = self.games[game_id]
        if not is_real_live_game(game):
            return None
        options = {}
        for payloads in self.detail_payloads.values():
            for payload in payloads:
                for option in find_qualifying_options(payload, game_id):
                    options[json.dumps(option, sort_keys=True)] = option
        if options:
            return {"game": game, "url": url, "options": list(options.values())}
        return None

    async def inspect_event(self, context, game_id):
        game = self.games[game_id]
        url = self.event_url(game_id, game)
        if not url:
            self.add_error(f"Event {game_id}: no live event URL could be resolved.")
            return None
        page = await context.new_page()
        page.set_default_timeout(2000)
        detail = LiveScanner(page)
        detail.register_game(game)
        detail.current_game_id = game_id
        try:
            async def load():
                await detail.navigate(url)
                if not await detail.settle():
                    detail.add_error("Some markets exceeded their loading budget.")
                await detail.read_market_tabs()
                await detail.finish_responses()

            try:
                await asyncio.wait_for(load(), timeout=EVENT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                detail.add_error("Page time limit reached; retained markets already read.")
            except Exception as error:
                detail.add_error(error)
            if detail.pending_responses:
                detail.add_error("Some market responses did not finish reading.")
            # Inspect data even if navigation, a tab or a later feed failed.
            result = detail.available_result(game_id, url)
            readable = any(has_market_data(payload) for payloads in detail.detail_payloads.values()
                           for payload in payloads)
            if not is_real_live_game(detail.games[game_id]):
                self.skipped_inactive += 1
            elif not readable:
                detail.add_error("No event market feed was readable.")
            else:
                self.readable_ids.add(game_id)
                if not detail.errors:
                    self.checked_ids.add(game_id)
            for error in detail.errors:
                self.add_error(f"Event {game_id}: {error}")
            return result
        except asyncio.CancelledError:
            # The overall deadline may interrupt a page after it found odds.
            result = detail.available_result(game_id, url)
            if result or any(has_market_data(payload) for payloads in detail.detail_payloads.values()
                             for payload in payloads):
                self.readable_ids.add(game_id)
            self.add_error(f"Event {game_id}: run budget reached; retained markets already read.")
            return result
        finally:
            page.remove_listener("response", detail.capture_response)
            pending = list(detail.pending_responses)
            for task in pending:
                task.cancel()
            if pending:
                try:
                    await asyncio.gather(*pending, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            try:
                await asyncio.wait_for(page.close(), timeout=2)
            except (Exception, asyncio.CancelledError):
                pass

    async def scan(self, context, alerted_ids=(), on_result=None,
                   max_seconds=SCAN_TIMEOUT_SECONDS, workers=CONCURRENT_PAGES):
        deadline = time.monotonic() + max_seconds
        try:
            await asyncio.wait_for(self.discover(), timeout=min(
                DISCOVERY_TIMEOUT_SECONDS, max_seconds * 0.25))
        except asyncio.TimeoutError:
            self.add_error("Catalogue time limit reached; checking games already discovered.")
        await self.finish_responses()
        self.selected_ids = self.eligible_games()[:MAX_GAMES]
        self.discovered_count = len(self.eligible_games())
        if not self.games and not self.feed_seen:
            self.add_error("No live catalogue was readable. The website may be blocking this browser or region.")
        print(f"Selected {len(self.selected_ids)} live games (limit {MAX_GAMES}).", flush=True)
        alerted_ids = {str(value) for value in alerted_ids}
        jobs = asyncio.Queue()
        for game_id in self.selected_ids:
            if game_id in alerted_ids:
                self.skipped_alerted += 1
            else:
                jobs.put_nowait(game_id)

        async def worker():
            while not jobs.empty():
                if time.monotonic() >= deadline:
                    self.budget_reached = True
                    return
                game_id = jobs.get_nowait()
                self.attempted_ids.add(game_id)
                try:
                    result = await self.inspect_event(context, game_id)
                    if result:
                        self.results.append(result)
                        if on_result:
                            await on_result(result)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.add_error(f"Event {game_id}: {error}")
                finally:
                    jobs.task_done()
                    processed = jobs.qsize()
                    if len(self.attempted_ids) % 25 == 0 or processed == 0:
                        print(f"Pages attempted: {len(self.attempted_ids)}; "
                              f"fully checked: {len(self.checked_ids)}; "
                              f"matching games: {len(self.results)}.", flush=True)

        tasks = [asyncio.create_task(worker()) for _ in range(min(workers, jobs.qsize()))]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=max(0.01, deadline - time.monotonic()))
            if pending:
                self.budget_reached = True
                for task in pending:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.budget_reached:
            self.add_error("Run time limit reached; returning available results.")
        return self.results


def format_alert(result):
    game = result["game"]
    sport = game.get("sport") or {}
    league = game.get("liga") or game.get("league") or {}
    opponent1 = (game.get("opponent1") or {}).get("fullName", "")
    opponent2 = (game.get("opponent2") or {}).get("fullName", "")
    event_name = " vs ".join(name for name in (opponent1, opponent2) if name)
    event_name = event_name or game.get("name") or f"Event {game['id']}"
    scores = game.get("scores") or {}
    lines = []
    for option in result["options"]:
        detail = option["selection"]
        if option["parameter"] is not None:
            detail += f" ({option['parameter']})"
        elif option["params"]:
            detail += " " + " ".join(str(item) for item in option["params"])
        if option.get("period"):
            detail += f" [{option['period']}]"
        lines.append(f"- {option['market']}: {detail} @ {option['odd']}")
    return "\n".join([
        "LIVE SPORTS ODDS ALERT", "", f"Sport: {sport.get('name', '?')}",
        f"League: {league.get('name', '?')}", f"Event: {event_name}",
        f"Status: {scores.get('statusLineStr', 'Live')}",
        f"Score: {scores.get('fullScore', 'Not provided')}", "",
        "Qualifying available selections:", *lines, "",
        f"Event ID: {game['id']}", f"Live event: {result['url']}", "",
        f"Conditions: selected live real sport; decimal odds {MIN_ODDS} to {MAX_ODDS} inclusive.",
        "Virtual sports, simulations and esports excluded.",
        f"Alert time: {timestamp_string()}",
    ])


def send_email(body, subject="Investment Alert"):
    for name, value in (("SMTP_USER", SMTP_USER), ("SMTP_PASSWORD", SMTP_PASSWORD),
                        ("ALERT_TO", ALERT_TO)):
        if not value:
            raise RuntimeError(f"{name} is not configured.")
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = ALERT_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        refused = server.send_message(message)
        if refused:
            raise RuntimeError("The email server rejected an alert recipient.")


def check_matches(results, alerts_data, dry_run=False):
    alerted = {str(value) for value in alerts_data.get("alerted_ids", [])}
    pending = {}
    for result in results:
        game = result["game"]
        alert_id = str(game.get("mainGameId") or game["id"])
        if alert_id not in alerted:
            pending[alert_id] = result
    if not pending:
        return 0, []
    body = "\n\n".join(format_alert(result) for result in pending.values())
    print(body, "\n", flush=True)
    if not dry_run:
        try:
            send_email(body)
        except Exception as error:
            return 0, [f"Match email failed: {error}"]
    alerted.update(pending)
    alerts_data["alerted_ids"] = sorted(alerted)
    if not dry_run:
        # The sender is serial: no simultaneous writes to duplicate state.
        save_alerts(alerts_data)
    return len(pending), []


async def send_queued_matches(queue, alerts_data, dry_run):
    """Deliver available results without blocking browser workers on SMTP."""
    count, errors = 0, []
    stopping = False
    while not stopping:
        item = await queue.get()
        if item is None:
            break
        batch = [item]
        while not queue.empty():
            item = queue.get_nowait()
            if item is None:
                stopping = True
                break
            batch.append(item)
        try:
            sent, failures = await asyncio.to_thread(check_matches, batch, alerts_data, dry_run)
            count += sent
            errors.extend(failures)
        except Exception as error:
            errors.append(f"Could not deliver or record match alerts: {error}")
    return count, errors


def make_report(scanner, count, errors, elapsed):
    errors = list(scanner.errors if scanner else []) + list(errors)
    selected = len(scanner.selected_ids) if scanner else 0
    attempted = len(scanner.attempted_ids) if scanner else 0
    checked = len(scanner.checked_ids) if scanner else 0
    readable = len(scanner.readable_ids) if scanner else 0
    skipped_alerted = scanner.skipped_alerted if scanner else 0
    skipped_inactive = scanner.skipped_inactive if scanner else 0
    unattempted = max(0, selected - attempted - skipped_alerted)
    status = "partial" if errors or unattempted else "complete"
    if errors and not readable and not skipped_alerted and not skipped_inactive:
        status = "failed"
    by_sport = {}
    if scanner:
        for game_id in scanner.selected_ids:
            sport = scanner.games[game_id].get("sport") or {}
            name = allowed_sport_name(sport) or "Unknown"
            by_sport[name] = by_sport.get(name, 0) + 1
    return {
        "status": status, "finished_at": timestamp_string(),
        "elapsed_seconds": round(elapsed, 1), "game_limit": MAX_GAMES,
        "allowed_sports": list(ALLOWED_SPORTS),
        "odds_min": str(MIN_ODDS), "odds_max": str(MAX_ODDS),
        "discovered_eligible": scanner.discovered_count if scanner else 0,
        "selected": selected, "attempted": attempted, "fully_checked": checked,
        "with_readable_markets": readable,
        "partial_or_failed_pages": max(0, attempted - checked - skipped_inactive),
        "skipped_already_alerted": skipped_alerted,
        "skipped_no_longer_live": skipped_inactive, "not_attempted": unattempted,
        "selected_by_sport": by_sport,
        "matching_games": len(scanner.results) if scanner else 0,
        "alerts_sent": count, "errors": errors,
        "matches": scanner.results if scanner else [],
    }


def format_report(report, dry_run=False):
    lines = [
        "MELBET MONITOR FEEDBACK", f"Status: {report['status'].upper()}",
        f"Time: {report['finished_at']}",
        "Sports monitored: " + ", ".join(report["allowed_sports"]),
        f"Scan duration: {report['elapsed_seconds']} seconds",
        f"Live games selected: {report['selected']} / {MAX_GAMES}",
        f"Event pages attempted: {report['attempted']}",
        f"Games with readable market data: {report['with_readable_markets']}",
        f"Games fully checked: {report['fully_checked']}",
        f"Pages partially read or failed: {report['partial_or_failed_pages']}",
        f"Skipped (already alerted today): {report['skipped_already_alerted']}",
        f"Skipped (no longer eligible/live): {report['skipped_no_longer_live']}",
        f"Selected games not reached: {report['not_attempted']}",
        f"Games with available odds {report['odds_min']} to {report['odds_max']} inclusive: {report['matching_games']}",
        f"{'Matches printed (dry run)' if dry_run else 'Game alerts delivered'}: {report['alerts_sent']}",
    ]
    if report["selected_by_sport"]:
        lines.append("Selected sports: " + ", ".join(
            f"{sport}: {number}" for sport, number in report["selected_by_sport"].items()))
    if not report["matching_games"]:
        if report["fully_checked"]:
            lines.append("No qualifying odds found in the markets read during this scan.")
        else:
            lines.append("No new qualifying odds verified; see coverage counts and issues.")
    if report["status"] != "complete":
        lines.append("Partial results retained; this is not a complete check of all selected games.")
    if report["errors"]:
        lines.extend(["", "Issues:"] + ["- " + message for message in report["errors"][:10]])
        if len(report["errors"]) > 10:
            lines.append(f"Additional issues in {REPORT_FILE}: {len(report['errors']) - 10}")
    return "\n".join(lines)


def save_report(report):
    temporary_path = REPORT_FILE + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
    os.replace(temporary_path, REPORT_FILE)


async def run_monitor(args):
    started = time.monotonic()
    alerts_data = load_alerts()
    queue = asyncio.Queue()
    sender = asyncio.create_task(send_queued_matches(queue, alerts_data, args.dry_run))
    scanner = None
    errors = []
    print(f"MELBET LIVE SPORTS MONITOR — {timestamp_string()}", flush=True)
    print(f"Target: {MIN_ODDS} to {MAX_ODDS} inclusive; first {MAX_GAMES} eligible live games; "
          f"scan budget {args.max_seconds:g}s; {args.workers} concurrent pages.", flush=True)
    print("Sports: " + ", ".join(ALLOWED_SPORTS), flush=True)
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            launch_options = {"headless": True}
            executable = os.environ.get("CHROMIUM_EXECUTABLE")
            if executable:
                launch_options["executable_path"] = executable
            elif not os.path.exists(playwright.chromium.executable_path) and shutil.which("chromium"):
                launch_options["executable_path"] = shutil.which("chromium")
            browser = await asyncio.wait_for(playwright.chromium.launch(**launch_options), timeout=20)
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()
                page.set_default_timeout(2000)
                scanner = LiveScanner(page)
                await scanner.scan(context, alerts_data.get("alerted_ids", []), queue.put,
                                   args.max_seconds, args.workers)
            finally:
                await asyncio.wait_for(browser.close(), timeout=5)
    except Exception as error:
        message = str(error).strip().splitlines()[0] or type(error).__name__
        errors.append(f"Browser scan stopped: {message}")
    finally:
        queue.put_nowait(None)
        count, email_errors = await sender
        errors.extend(email_errors)

    report = make_report(scanner, count, errors, time.monotonic() - started)
    if args.dry_run:
        print(format_report(report, dry_run=True), flush=True)
        return 1 if report["status"] == "failed" else 0

    # Keep a local report even when SMTP or a later state-file write fails.
    save_report(report)
    delivered_ids = set(alerts_data.get("alerted_ids", []))
    unsent = [result for result in report["matches"]
              if str(result["game"].get("mainGameId") or result["game"]["id"]) not in delivered_ids]
    feedback = format_report(report)
    if unsent:
        feedback += "\n\nAvailable match results:\n" + "\n\n".join(map(format_alert, unsent))
    feedback_failed = False
    try:
        await asyncio.to_thread(send_email, feedback, "Monitor Feedback")
        report["feedback_sent"] = True
        # Feedback includes any results whose earlier match email failed.
        delivered_ids.update(str(result["game"].get("mainGameId") or result["game"]["id"])
                             for result in unsent)
        report["alerts_sent"] += len(unsent)
    except Exception as error:
        feedback_failed = True
        report["feedback_sent"] = False
        report["errors"].append(f"Feedback email failed: {error}")
        if report["status"] == "complete":
            report["status"] = "partial"
    alerts_data["date"] = today_string()
    alerts_data["alerted_ids"] = sorted(delivered_ids)
    alerts_data["last_run"] = report["finished_at"]
    alerts_data["last_status"] = report["status"]
    if report["status"] == "complete":
        alerts_data["last_successful_run"] = report["finished_at"]
    save_report(report)
    save_alerts(alerts_data)
    print(format_report(report), flush=True)
    return 1 if report["status"] == "failed" or feedback_failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print matches without emails or state-file writes.")
    parser.add_argument("--max-seconds", type=float, default=SCAN_TIMEOUT_SECONDS,
                        help="Scan time budget in seconds (default: 300; feedback follows).")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=CONCURRENT_PAGES,
                        help="Concurrent event pages (default: 6).")
    args = parser.parse_args()
    if args.max_seconds <= 0:
        parser.error("--max-seconds must be positive.")
    return asyncio.run(run_monitor(args))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"MONITOR FAILED: {error}", file=sys.stderr)
        sys.exit(1)
