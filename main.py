import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ──────────────────────────────────────────────
# CONFIG — set these as Railway environment vars
# ──────────────────────────────────────────────
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FAVOURITE_THRESHOLD = float(os.environ.get("FAVOURITE_THRESHOLD", "1.6"))
LOOKAHEAD_HOURS     = int(os.environ.get("LOOKAHEAD_HOURS", "6"))
SCAN_INTERVAL       = int(os.environ.get("SCAN_INTERVAL_SECONDS", "240"))  # 5 mins

BASE_URL = "https://api.the-odds-api.com/v4"

# ──────────────────────────────────────────────
# ANSI colour codes (Discord renders these inside ```ansi blocks)
# ──────────────────────────────────────────────
RED   = "\u001b[31m"
GREEN = "\u001b[32m"
WHITE = "\u001b[37m"
RESET = "\u001b[0m"
BOLD  = "\u001b[1m"

# ──────────────────────────────────────────────
# State
# ──────────────────────────────────────────────
favourites_cache: dict = {}
SOCCER_SPORTS: list = []


# ──────────────────────────────────────────────
# League discovery
# ──────────────────────────────────────────────

def discover_soccer_leagues() -> list:
    """
    Fetch all sports from The Odds API and return every active soccer league key.
    Covers every league that currently has betting markets — no hardcoding.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/",
            params={"apiKey": ODDS_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        all_sports = resp.json()

        soccer_leagues = [
            s["key"]
            for s in all_sports
            if s.get("group", "").lower() == "soccer"
            and s.get("active", False)
        ]

        print(f"[DISCOVERY] Found {len(soccer_leagues)} active soccer leagues:")
        for key in soccer_leagues:
            title = next((s["title"] for s in all_sports if s["key"] == key), key)
            print(f"   • {key}  ({title})")

        return soccer_leagues

    except Exception as e:
        print(f"[ERROR] League discovery failed: {e}")
        fallback = [
            "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
            "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_uefa_champs_league",
        ]
        print(f"[DISCOVERY] Falling back to {len(fallback)} hardcoded leagues.")
        return fallback


# ──────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────

def get_upcoming_odds(sport: str) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{sport}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=10,
        )
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining", "?")
        print(f"  [API] {sport} | quota remaining: {remaining}")
        return resp.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            print(f"  [SKIP] {sport} — no markets available.")
        else:
            print(f"  [ERROR] get_upcoming_odds({sport}): {e}")
        return []
    except Exception as e:
        print(f"  [ERROR] get_upcoming_odds({sport}): {e}")
        return []


def get_scores(sport: str) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{sport}/scores/",
            params={"apiKey": ODDS_API_KEY, "daysFrom": 1},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            return []
        print(f"  [ERROR] get_scores({sport}): {e}")
        return []
    except Exception as e:
        print(f"  [ERROR] get_scores({sport}): {e}")
        return []


# ──────────────────────────────────────────────
# Logic helpers
# ──────────────────────────────────────────────

def parse_commence(commence_time_str: str) -> datetime:
    return datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))


def extract_favourite(game: dict) -> tuple | None:
    """
    Returns (fav_team_name, fav_odds) if a h2h favourite is below threshold.
    Averages odds across all available bookmakers.
    """
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        return None

    home = game["home_team"]
    away = game["away_team"]
    home_odds_list, away_odds_list = [], []

    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name  = outcome.get("name", "")
                price = outcome.get("price", 0)
                if name == home:
                    home_odds_list.append(price)
                elif name == away:
                    away_odds_list.append(price)

    if not home_odds_list or not away_odds_list:
        return None

    avg_home = sum(home_odds_list) / len(home_odds_list)
    avg_away = sum(away_odds_list) / len(away_odds_list)

    if avg_home < avg_away and avg_home < FAVOURITE_THRESHOLD:
        return (home, round(avg_home, 2))
    if avg_away < avg_home and avg_away < FAVOURITE_THRESHOLD:
        return (away, round(avg_away, 2))

    return None


def get_score_for_team(score_entry: dict, team_name: str) -> int | None:
    scores = score_entry.get("scores") or []
    for s in scores:
        if s.get("name") == team_name:
            try:
                return int(s.get("score", 0))
            except (ValueError, TypeError):
                return 0
    return None


def determine_status(fav_team: str, home: str, away: str, score_entry: dict) -> str:
    home_score = get_score_for_team(score_entry, home)
    away_score = get_score_for_team(score_entry, away)
    if home_score is None or away_score is None:
        return "drawing"
    fav_is_home = fav_team == home
    fav_score = home_score if fav_is_home else away_score
    opp_score = away_score if fav_is_home else home_score
    if fav_score > opp_score:
        return "winning"
    elif fav_score < opp_score:
        return "losing"
    return "drawing"


def format_score(home: str, away: str, score_entry: dict) -> str:
    hs  = get_score_for_team(score_entry, home)
    a_s = get_score_for_team(score_entry, away)
    return f"? - ?" if hs is None else f"{hs} - {a_s}"


def get_match_minute(score_entry: dict) -> str:
    try:
        kickoff = parse_commence(score_entry.get("commence_time", ""))
        now     = datetime.now(timezone.utc)
        elapsed = int((now - kickoff).total_seconds() / 60)
        if 0 <= elapsed <= 130:
            return f"{elapsed}'"
    except Exception:
        pass
    return "LIVE"


# ──────────────────────────────────────────────
# Core scan functions
# ──────────────────────────────────────────────

def scan_upcoming():
    """Scan ALL soccer leagues for upcoming favourites and cache them."""
    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(hours=LOOKAHEAD_HOURS)
    new_found = 0

    print(f"[UPCOMING] Scanning {len(SOCCER_SPORTS)} leagues...")

    for sport in SOCCER_SPORTS:
        games = get_upcoming_odds(sport)
        for game in games:
            game_id = game.get("id")
            if not game_id or game_id in favourites_cache:
                continue
            try:
                commence = parse_commence(game.get("commence_time", ""))
            except Exception:
                continue
            if commence <= now or commence > cutoff:
                continue

            result = extract_favourite(game)
            if result:
                fav_team, fav_odds = result
                favourites_cache[game_id] = {
                    "home":     game["home_team"],
                    "away":     game["away_team"],
                    "sport":    sport,
                    "commence": game["commence_time"],
                    "fav_team": fav_team,
                    "fav_odds": fav_odds,
                }
                new_found += 1
                print(f"  [+] {game['home_team']} vs {game['away_team']} → ⭐ {fav_team} @ {fav_odds}")

    print(f"[UPCOMING] Done. +{new_found} new favourites | {len(favourites_cache)} total cached.")


def scan_live() -> list:
    """Check all live matches across every league for active favourites."""
    alerts = []
    now    = datetime.now(timezone.utc)

    print(f"[LIVE] Scanning live games across {len(SOCCER_SPORTS)} leagues...")

    for sport in SOCCER_SPORTS:
        scores = get_scores(sport)
        if not scores:
            continue

        live_odds_list = get_upcoming_odds(sport)
        live_odds_map  = {g.get("id"): g for g in live_odds_list}

        for score_entry in scores:
            game_id   = score_entry.get("id")
            completed = score_entry.get("completed", False)
            if completed:
                continue

            try:
                commence = parse_commence(score_entry.get("commence_time", ""))
                if commence > now:
                    continue
            except Exception:
                continue

            home = score_entry.get("home_team", "")
            away = score_entry.get("away_team", "")

            # ── Cached favourite now playing ──
            if game_id in favourites_cache:
                cached   = favourites_cache[game_id]
                fav_team = cached["fav_team"]
                fav_odds = cached["fav_odds"]
                if game_id in live_odds_map:
                    result = extract_favourite(live_odds_map[game_id])
                    if result:
                        fav_team, fav_odds = result
                alerts.append({
                    "home": home, "away": away, "sport": sport,
                    "fav_team": fav_team, "fav_odds": fav_odds,
                    "status":   determine_status(fav_team, home, away, score_entry),
                    "score":    format_score(home, away, score_entry),
                    "minute":   get_match_minute(score_entry),
                })

            # ── Not cached — check live odds ──
            elif game_id in live_odds_map:
                result = extract_favourite(live_odds_map[game_id])
                if result:
                    fav_team, fav_odds = result
                    alerts.append({
                        "home": home, "away": away, "sport": sport,
                        "fav_team": fav_team, "fav_odds": fav_odds,
                        "status":   determine_status(fav_team, home, away, score_entry),
                        "score":    format_score(home, away, score_entry),
                        "minute":   get_match_minute(score_entry),
                    })

    # Sort: losing first (most urgent), drawing, winning
    order = {"losing": 0, "drawing": 1, "winning": 2}
    alerts.sort(key=lambda a: order.get(a["status"], 1))
    return alerts


# ──────────────────────────────────────────────
# Discord output
# ──────────────────────────────────────────────

STATUS_ICON   = {"winning": "▲", "drawing": "■", "losing": "▼"}
STATUS_COLOUR = {"winning": GREEN, "drawing": WHITE, "losing": RED}
STATUS_LABEL  = {"winning": "WINNING", "drawing": "LEVEL  ", "losing": "LOSING "}


def build_discord_message(alerts: list) -> str:
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    sep     = "─" * 62

    header_lines = [
        f"{WHITE}{BOLD}⚽  SOCCER FAVOURITES MONITOR  —  {now_str}{RESET}",
        f"{WHITE}{sep}{RESET}",
    ]

    if not alerts:
        body_lines = [f"{WHITE}No live favourites right now. {len(favourites_cache)} upcoming cached.{RESET}"]
    else:
        body_lines = []
        for a in alerts:
            status  = a["status"]
            colour  = STATUS_COLOUR.get(status, WHITE)
            icon    = STATUS_ICON.get(status, "■")
            label   = STATUS_LABEL.get(status, "LEVEL  ")

            matchup = f"{a['home']} vs {a['away']}"
            if len(matchup) > 36:
                matchup = matchup[:33] + "..."

            line = (
                f"{colour}{BOLD}{icon} {matchup:<37}{RESET}"
                f"{colour}  {a['score']}  {a['minute']:<5}  ⭐{a['fav_team']} @{a['fav_odds']}  [{label}]{RESET}"
            )
            body_lines.append(line)

    footer_lines = [
        f"{WHITE}{sep}{RESET}",
        f"{WHITE}Threshold <{FAVOURITE_THRESHOLD}  •  {len(favourites_cache)} cached  •  {len(SOCCER_SPORTS)} leagues{RESET}",
    ]

    all_lines = header_lines + body_lines + footer_lines
    body      = "\n".join(all_lines)
    return f"**⚽ Soccer Favourites** — `{now_str}`\n```ansi\n{body}\n```"


def send_to_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print("[DISCORD] No webhook URL — printing to stdout:")
        print(content)
        return
    try:
        max_len = 1990
        chunks  = [content[i:i+max_len] for i in range(0, len(content), max_len)]
        for chunk in chunks:
            resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=10)
            if resp.status_code in (200, 204):
                print("[DISCORD] ✓ Sent")
            else:
                print(f"[DISCORD] Status {resp.status_code}: {resp.text[:200]}")
            if len(chunks) > 1:
                time.sleep(0.5)
    except Exception as e:
        print(f"[DISCORD] Error: {e}")


# ──────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────

def main():
    global SOCCER_SPORTS

    print("=" * 60)
    print("  ⚽ Soccer Favourites Bot")
    print(f"  Threshold : odds < {FAVOURITE_THRESHOLD}")
    print(f"  Lookahead : {LOOKAHEAD_HOURS} hours")
    print(f"  Interval  : every {SCAN_INTERVAL}s")
    print("=" * 60)

    if not ODDS_API_KEY:
        print("[FATAL] ODDS_API_KEY is not set. Exiting.")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL not set — output to stdout only.")

    # Discover every available soccer league at startup
    print("\n[BOOT] Discovering all available soccer leagues...")
    SOCCER_SPORTS = discover_soccer_leagues()
    print(f"[BOOT] {len(SOCCER_SPORTS)} leagues loaded.\n")

    last_discovery   = time.time()
    REDISCOVER_EVERY = 6 * 3600  # refresh league list every 6 hours

    while True:
        cycle_start = datetime.now(timezone.utc)
        print(f"\n{'='*60}")
        print(f"[CYCLE] {cycle_start.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"{'='*60}")

        # Refresh league list every 6 hours (new competitions may start)
        if time.time() - last_discovery > REDISCOVER_EVERY:
            print("[BOOT] Refreshing league list...")
            SOCCER_SPORTS  = discover_soccer_leagues()
            last_discovery = time.time()

        scan_upcoming()
        alerts  = scan_live()
        message = build_discord_message(alerts)
        send_to_discord(message)

        print(f"[CYCLE] {len(alerts)} alert(s) posted. Sleeping {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
