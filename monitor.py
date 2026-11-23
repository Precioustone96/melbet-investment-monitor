"""MelBet live sports monitor.

Alert conditions:
    * A live event in a real sport, at any stage of play.
    * Any available market/selection with decimal odds EXACTLY 1.001.
    * Exclude virtual sports, simulated events and esports, including those
      listed inside a normal sport such as football or basketball.

The scanner discovers the live sport and event pages, loads more results and
reads each eligible event's market feeds. It does not rely on the old 40-game
football feed or its selected markets. An incomplete crawl exits with an error.

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
import json
import os
import re
import shutil
import smtplib
import ssl
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


LIVE_PAGE_URL = "https://melbet.com/en/live"
TARGET_ODDS = Decimal("1.001")
TIMEZONE = ZoneInfo("Africa/Lagos")
ALERTS_FILE = "alerts.json"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_TO = os.environ.get("ALERT_TO")

PAGE_TIMEOUT_MS = 30000
INITIAL_WAIT_MS = 2500
SETTLE_WAIT_MS = 1000
STABLE_ROUNDS = 3
# This is a failure guard, not a limit on the number of games checked.
MAX_DISCOVERY_ROUNDS = 500

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
        if in_markets and "cf" in node and parse_odd(node["cf"]) == TARGET_ODDS:
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
                "params": extra, "odd": str(TARGET_ODDS), "period_id": scope,
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
    """Discover live catalogue and event feeds through the site's own pages.

    This avoids inventing unsupported sport/pagination parameters for the old
    games1x2 API. Every eligible detail page is opened, even if its catalogue
    preview has no 1.001 odds: previews omit many markets.
    """

    def __init__(self, page):
        self.page = page
        self.games = {}
        self.event_urls = {}
        self.sport_urls = set()
        self.visited_sports = set()
        self.feed_seen = False
        self.current_game_id = None
        self.detail_payloads = {}
        self.results = []
        self.errors = []
        page.on("response", self.capture_response)

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
        try:
            data = response.json()
        except Exception:
            return
        self.feed_seen = True
        records = list(game_records(data))
        live_source = bool(re.search(
            r"/LiveFeed/|(?:^|[/_-])live(?:[/_-]|$)", parts.path, re.I
        ))
        for game in records:
            game_id = str(game["id"])
            if (live_source or game_id in self.event_urls
                    or game_id == self.current_game_id or true_flag(game.get("isLive"))):
                self.register_game(game)
        game_id = self.current_game_id
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
        if re.search(rf"(?<!\d){re.escape(game_id)}(?!\d)", response.url):
            scoped.append(data)
        if scoped:
            self.detail_payloads[resource_key(response.url)] = scoped

    def collect_links(self):
        links = self.page.locator("a[href]").evaluate_all(
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
            sport_url = f"{base.scheme}://{base.netloc}/en/live/{sport_slug}"
            if len(segments) == 1:
                self.sport_urls.add(sport_url)
            elif len(segments) >= 3:
                game_number = re.match(r"^(\d+)(?:-|$)", segments[-1])
                league_number = re.match(r"^(\d+)(?:-|$)", segments[-2])
                if not game_number or not league_number:
                    continue
                game_id = game_number.group(1)
                url = urlunsplit((base.scheme, base.netloc, parts.path, "", ""))
                self.sport_urls.add(sport_url)
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

    def collect_embedded_data(self):
        for text in self.page.locator('script[type="application/json"]').all_text_contents():
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

    def click_more(self):
        buttons = self.page.get_by_role("button", name=re.compile(
            r"^(?:(?:show|load|see|view)\s+(?:more|all)(?:\s+(?:events|matches|games|markets|bets|sports))?"
            r"|more\s+(?:events|matches|games|markets|sports))(?:\s*\(\d+\)|\s*\d+)?$", re.I
        ))
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=5000)
                return True
        return False

    def scroll_lists(self):
        # Collect links before each step so virtualised lists are not reduced
        # to just the final screen. Include independently scrolling panels.
        return self.page.evaluate("""() => {
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

    def navigate(self, url):
        self.page.goto(url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)
        self.page.wait_for_timeout(INITIAL_WAIT_MS)
        if urlsplit(self.page.url).hostname != urlsplit(LIVE_PAGE_URL).hostname:
            raise IncompleteScanError("MelBet redirected to another host; the live scan could not be verified.")

    def settle(self):
        stable = 0
        previous = None
        for _ in range(MAX_DISCOVERY_ROUNDS):
            self.collect_links()
            self.collect_embedded_data()
            signature = (len(self.games), len(self.event_urls), len(self.sport_urls),
                         tuple(sorted(self.detail_payloads)))
            more = self.click_more()
            moving = self.scroll_lists()
            stable = stable + 1 if signature == previous and not more and not moving else 0
            if stable >= STABLE_ROUNDS:
                return
            previous = signature
            self.page.wait_for_timeout(SETTLE_WAIT_MS)
        raise IncompleteScanError("A live list did not finish loading; the scan is incomplete.")

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

    def read_market_tabs(self):
        # Some event pages fetch additional markets only when their tab opens.
        root = self.page.locator("main")
        root = root.first if root.count() else self.page
        tabs = root.get_by_role("tab")
        labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
        event_path = urlsplit(self.page.url).path
        for label in dict.fromkeys(labels):
            if not label:
                continue
            tab = root.get_by_role("tab", name=label, exact=True)
            if tab.count() != 1 or not tab.is_visible() or not tab.is_enabled():
                continue
            if tab.evaluate("el => !!el.closest('aside')"):
                continue
            href = tab.get_attribute("href")
            if href and urlsplit(urljoin(self.page.url, href)).path != event_path:
                continue
            tab.click(timeout=5000)
            self.page.wait_for_timeout(SETTLE_WAIT_MS)
            if urlsplit(self.page.url).path != event_path:
                raise IncompleteScanError("An event tab left the live event page.")
            self.settle()

    def scan(self, alerted_ids=()):
        self.navigate(LIVE_PAGE_URL)
        self.settle()
        if not self.sport_urls and not self.games:
            raise IncompleteScanError("No live catalogue was readable. MelBet may be blocking this browser or region.")
        # Discover every sport exposed by the live navigation. There is no
        # hardcoded football list and no limit of 40 events.
        while self.sport_urls - self.visited_sports:
            url = sorted(self.sport_urls - self.visited_sports)[0]
            self.visited_sports.add(url)
            try:
                self.navigate(url)
                self.settle()
            except Exception as error:
                self.errors.append(f"{url}: {error}")
        if not self.games and not self.feed_seen:
            raise IncompleteScanError("No readable live event data; the scan cannot be treated as an empty live schedule.")
        print(f"Discovered {len(self.games)} events across {len(self.visited_sports)} sport pages.")
        checked = set()
        alerted_ids = {str(value) for value in alerted_ids}
        while set(self.games) - checked:
            game_id = sorted(set(self.games) - checked)[0]
            checked.add(game_id)
            game = self.games[game_id]
            alert_id = str(game.get("mainGameId") or game_id)
            if alert_id in alerted_ids or not is_real_live_game(game):
                continue
            url = self.event_url(game_id, game)
            if not url:
                self.errors.append(f"Event {game_id}: no live event URL could be resolved.")
                continue
            self.current_game_id = game_id
            self.detail_payloads = {}
            try:
                self.navigate(url)
                self.settle()
                self.read_market_tabs()
                game = self.games[game_id]
                # Detail metadata may identify a simulation not labelled in
                # the preview. Recheck before inspecting any of its odds.
                if not is_real_live_game(game):
                    continue
                if not any(has_market_data(payload) for payloads in self.detail_payloads.values()
                           for payload in payloads):
                    raise IncompleteScanError("No full event market feed was readable.")
                options = {}
                for payloads in self.detail_payloads.values():
                    for payload in payloads:
                        for option in find_qualifying_options(payload, game_id):
                            options[json.dumps(option, sort_keys=True)] = option
                if options:
                    self.results.append({"game": game, "url": url,
                                         "options": list(options.values())})
            except Exception as error:
                self.errors.append(f"Event {game_id}: {error}")
            finally:
                self.current_game_id = None
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
        "Conditions: live real sport; decimal odds exactly 1.001.",
        "Virtual sports, simulations and esports excluded.",
        f"Alert time: {timestamp_string()}",
    ])


def send_email(body):
    for name, value in (("SMTP_USER", SMTP_USER), ("SMTP_PASSWORD", SMTP_PASSWORD),
                        ("ALERT_TO", ALERT_TO)):
        if not value:
            raise RuntimeError(f"{name} is not configured.")
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = "Investment Alert"
    message["From"] = SMTP_USER
    message["To"] = ALERT_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        refused = server.send_message(message)
        if refused:
            raise RuntimeError("The email server rejected an alert recipient.")


def check_matches(results, alerts_data, dry_run=False):
    alerted = {str(value) for value in alerts_data.get("alerted_ids", [])}
    count = 0
    errors = []
    for result in results:
        game = result["game"]
        alert_id = str(game.get("mainGameId") or game["id"])
        if alert_id in alerted:
            continue
        body = format_alert(result)
        print(body, "\n")
        if dry_run:
            alerted.add(alert_id)
            count += 1
            continue
        try:
            send_email(body)
        except Exception as error:
            errors.append(f"Email for event {alert_id} failed: {error}")
            continue
        # Persist immediately after a successful email; a later page or SMTP
        # failure must not cause already-sent alerts to be sent again.
        alerted.add(alert_id)
        alerts_data["alerted_ids"] = sorted(alerted)
        save_alerts(alerts_data)
        count += 1
    return count, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print matches without emails or state-file writes.")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    alerts_data = load_alerts()
    print(f"MELBET LIVE SPORTS MONITOR — {timestamp_string()}")
    print("Target: exactly 1.001, all live real sports and available markets.")
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        # The standard Playwright install is preferred. An explicit local
        # Chromium path is useful on systems that provide their own browser.
        executable = os.environ.get("CHROMIUM_EXECUTABLE")
        if executable:
            launch_options["executable_path"] = executable
        elif not os.path.exists(playwright.chromium.executable_path) and shutil.which("chromium"):
            launch_options["executable_path"] = shutil.which("chromium")
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            scanner = LiveScanner(context.new_page())
            results = scanner.scan(alerts_data.get("alerted_ids", []))
            count, email_errors = check_matches(results, alerts_data, args.dry_run)
            errors = scanner.errors + email_errors
        finally:
            browser.close()
    print(f"{'Matching events' if args.dry_run else 'New alerts sent'}: {count}")
    if errors:
        raise IncompleteScanError("Run incomplete; last_run was not advanced:\n" + "\n".join(errors))
    if not args.dry_run:
        alerts_data["date"] = today_string()
        alerts_data["last_run"] = timestamp_string()
        save_alerts(alerts_data)
    print("MONITOR FINISHED SUCCESSFULLY")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"MONITOR FAILED: {error}", file=sys.stderr)
        sys.exit(1)
