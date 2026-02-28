import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime, timedelta, timezone

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')

POLL_INTERVAL = 300           # 5 min live score checks
ODDS_REFRESH_INTERVAL = 1800  # 30 min for match info refresh
HEAVY_THRESHOLD = 1.60

favorite_cache = {}  # fave_team → {'odd': float, 'home': str, 'away': str, 'is_home': bool, 'match_id': str}
match_cache = {}  # (home.lower(), away.lower()) → {'sport_key': str, 'match_id': str, 'commence_time': str}
last_scores = {}
last_refresh = 0

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot alive - pre-match odds only with live tied additions"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"Sent: {msg[:120]}...")
    except Exception as e:
        print("Discord send failed:", e)

def refresh_match_info():
    global last_refresh
    try:
        now_utc = datetime.now(timezone.utc)
        commence_from = (now_utc - timedelta(hours=24)).isoformat().replace('+00:00', 'Z')
        commence_to = (now_utc + timedelta(hours=6)).isoformat().replace('+00:00', 'Z')

        sports = "soccer_epl,soccer_spain_la_liga,soccer_germany_bundesliga,soccer_italy_serie_a,soccer_france_ligue_one,soccer_uefa_champs_league,soccer_uefa_europa_league,soccer_uefa_champs_league_qualification"

        url = (
            f"https://api.the-odds-api.com/v4/sports/odds/"
            f"?apiKey={ODDS_API_KEY}"
            f"&regions=eu"
            f"&markets=h2h"
            f"&oddsFormat=decimal"
            f"&sports={sports}"
            f"&commenceTimeFrom={commence_from}"
            f"&commenceTimeTo={commence_to}"
        )

        r = requests.get(url, timeout=15)
        print(f"Match info API status: {r.status_code}")
        if r.status_code != 200:
            print(f"Match info error response: {r.text[:200]}")
            send_discord(f"⚠️ Match info fetch returned {r.status_code}")
            return

        odds_data = r.json()

        match_cache.clear()
        new_pre_faves = 0

        for game in odds_data:
            home = game['home_team'].lower()
            away = game['away_team'].lower()
            key = (home, away)

            match_cache[key] = {
                'sport_key': game['sport_key'],
                'match_id': game['id'],
                'commence_time': game['commence_time']
            }

            # Cache pre-match favorites
            commence_time = datetime.fromisoformat(game['commence_time'].rstrip('Z')).replace(tzinfo=timezone.utc)
            if commence_time > now_utc:
                min_odd = 99.0
                fave_team = None
                is_home = False
                for book in game['bookmakers'][:5]:
                    for market in book.get('markets', []):
                        if market.get('key') == 'h2h':
                            for outcome in market['outcomes']:
                                price = float(outcome.get('price', 99))
                                if price < min_odd:
                                    min_odd = price
                                    fave_team = outcome['name']
                                    is_home = fave_team.lower() == home
                if min_odd <= HEAVY_THRESHOLD and fave_team:
                    favorite_cache[fave_team] = {
                        'odd': min_odd,
                        'home': game['home_team'],
                        'away': game['away_team'],
                        'is_home': is_home,
                        'match_id': game['id']
                    }
                    new_pre_faves += 1

        print(f"Match info refreshed: {len(match_cache)} matches cached, {new_pre_faves} pre-match heavy faves")
        send_discord(f"🔄 **Match info refreshed**\nCached {len(match_cache)} matches, {new_pre_faves} pre-match heavy faves")

        last_refresh = time.time()

    except Exception as e:
        print("Match refresh error:", str(e))
        send_discord("⚠️ Match refresh failed")

def fetch_pre_match_fave(home, away, sport_key, match_id, commence_time):
    try:
        # Fetch historical pre-match odds (1 hour before kickoff)
        pre_time = (datetime.fromisoformat(commence_time.rstrip('Z')).replace(tzinfo=timezone.utc) - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')

        url = (
            f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/events/{match_id}/odds/"
            f"?apiKey={ODDS_API_KEY}"
            f"&regions=eu"
            f"&markets=h2h"
            f"&oddsFormat=decimal"
            f"&date={pre_time}"
        )

        r = requests.get(url, timeout=15)
        print(f"Historical odds status for {home} vs {away}: {r.status_code}")

        if r.status_code != 200:
            print(f"Historical error response: {r.text[:200]}")
            return None

        historical_data = r.json()
        # Assume first timestamp snapshot
        if 'data' in historical_data and historical_data['data']:
            game_data = historical_data['data'][0]
            min_odd = 99.0
            fave_team = None
            is_home = False
            for book in game_data['bookmakers'][:5]:
                for market in book.get('markets', []):
                    if market.get('key') == 'h2h':
                        for outcome in market['outcomes']:
                            price = float(outcome.get('price', 99))
                            if price < min_odd:
                                min_odd = price
                                fave_team = outcome['name']
                                is_home = fave_team.lower() == home.lower()
            if min_odd <= HEAVY_THRESHOLD and fave_team:
                return {
                    'odd': min_odd,
                    'home': game_data['home_team'],
                    'away': game_data['away_team'],
                    'is_home': is_home,
                    'match_id': match_id
                }
        return None
    except Exception as e:
        print("Historical odds error:", str(e))
        return None

# ==================== STARTUP ====================
print("Bot starting - initial match fetch...")
threading.Thread(target=run_flask, daemon=True).start()
refresh_match_info()

# ==================== MAIN LOOP ====================
while True:
    try:
        now = time.time()
        if now - last_refresh >= ODDS_REFRESH_INTERVAL:
            refresh_match_info()

        # Live scores
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard", timeout=15)
        events = r.json().get("events", [])

        live_faves = []
        alerts = []

        for e in events:
            if e.get("status", {}).get("type", {}).get("state") != "in":
                continue

            comp = e["competitions"][0]
            home = comp["competitors"][0]["team"]["displayName"].lower()
            away = comp["competitors"][1]["team"]["displayName"].lower()
            hg = int(comp["competitors"][0].get("score", "0") or "0")
            ag = int(comp["competitors"][1].get("score", "0") or "0")

            game_time = e.get("status", {}).get("displayClock", "")
            if game_time:
                game_time = f" ({game_time})"

            key = (home, away)
            key_reverse = (away, home)  # in case order swapped

            cached_match = match_cache.get(key) or match_cache.get(key_reverse)
            if not cached_match:
                continue

            # Adjust for reverse key
            if key_reverse in match_cache:
                hg, ag = ag, hg
                home, away = away, home

            is_tied = hg == ag

            # Get or fetch pre-match fave
            fave_team = None
            for ft, data in favorite_cache.items():
                if data['match_id'] == cached_match['match_id']:
                    fave_team = ft
                    odd = data['odd']
                    is_home = data['is_home']

                    # Adjust is_home if reverse
                    if key_reverse in match_cache:
                        is_home = not is_home

                    break

            if not fave_team and is_tied:
                # Fetch pre-match for tied live
                fave_data = fetch_pre_match_fave(home, away, cached_match['sport_key'], cached_match['match_id'], cached_match['commence_time'])
                if fave_data:
                    favorite_cache[fave_data['fave_team']] = fave_data
                    fave_team = fave_data['fave_team']
                    odd = fave_data['odd']
                    is_home = fave_data['is_home']

            if fave_team:
                score_str = f"{home} {hg}-{ag} {away}{game_time}"
                is_losing = (is_home and hg < ag and ag >= 1) or (not is_home and ag < hg and hg >= 1)

                line = f"**{fave_team}** ({odd}) | {score_str}"
                live_faves.append({"line": line, "is_losing": is_losing})

                match_id = e["id"]
                current = (hg, ag)
                if match_id not in last_scores or last_scores[match_id] != current:
                    fave_g = hg if is_home else ag
                    opp_g = ag if is_home else hg
                    if fave_g == 0 and opp_g >= 1:
                        alerts.append(f"🚨 **HEAVY FAVE DOWN!** {score_str}\n**{fave_team}** (pre {odd}) trailing")
                    last_scores[match_id] = current

        if live_faves:
            summary = "```diff\n**🔴 Live Heavy Faves (pre-match only)**\n"
            for item in live_faves:
                prefix = "- " if item["is_losing"] else ""
                summary += f"{prefix}{item['line']}\n"
            summary += "```"
            send_discord(summary)
        else:
            print("No live heavy faves")

        for alert in alerts:
            send_discord(alert)

    except Exception as e:
        print("Main loop error:", str(e))

    time.sleep(POLL_INTERVAL)
