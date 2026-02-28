import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime, timedelta, timezone

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')

POLL_INTERVAL = 300           # 5 min
ODDS_REFRESH_INTERVAL = 1800  # 30 min match refresh
HEAVY_THRESHOLD = 1.60

favorite_cache = {}  # fave_team: {'odd': float, 'home': str, 'away': str, 'is_home': bool, 'match_id': str}
match_cache = {}  # match_id: {'sport_key': str, 'commence_time': str, 'home': str, 'away': str}
last_scores = {}
last_refresh = 0

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot alive - pre-match odds with live tied additions"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"Sent: {msg[:120]}...")
    except:
        pass

def refresh_matches():
    global last_refresh
    try:
        now_utc = datetime.now(timezone.utc)
        commence_from = (now_utc - timedelta(hours=24)).isoformat().replace('+00:00', 'Z')  # Include recent started
        commence_to = (now_utc + timedelta(hours=6)).isoformat().replace('+00:00', 'Z')

        url = (
            f"https://api.the-odds-api.com/v4/sports/upcoming/odds/"
            f"?apiKey={ODDS_API_KEY}"
            f"&regions=eu"
            f"&markets=h2h"
            f"&oddsFormat=decimal"
        )

        r = requests.get(url, timeout=15)
        print(f"Matches API status: {r.status_code}")
        if r.status_code != 200:
            print(f"API error response: {r.text[:300]}")
            send_discord(f"⚠️ Matches fetch failed ({r.status_code})")
            return

        odds_data = r.json()

        match_cache.clear()
        new_pre_faves = 0

        for game in odds_data:
            home = game['home_team']
            away = game['away_team']
            match_id = game['id']
            sport_key = game['sport_key']
            commence_time = game['commence_time']

            match_cache[match_id] = {'sport_key': sport_key, 'commence_time': commence_time, 'home': home, 'away': away}

            # Cache pre-match heavy faves for upcoming
            commence_dt = datetime.fromisoformat(commence_time.rstrip('Z')).replace(tzinfo=timezone.utc)
            if commence_dt > now_utc:
                min_odd = 99.0
                fave_team = None
                is_home = False
                for book in game['bookmakers'][:5]:
                    for m in book.get('markets', []):
                        if m.get('key') == 'h2h':
                            for o in m.get('outcomes', []):
                                price = float(o.get('price', 99))
                                if price < min_odd:
                                    min_odd = price
                                    fave_team = o['name']
                                    is_home = fave_team == home
                if min_odd <= HEAVY_THRESHOLD and fave_team:
                    favorite_cache[fave_team] = {
                        'odd': min_odd,
                        'home': home,
                        'away': away,
                        'is_home': is_home,
                        'match_id': match_id
                    }
                    new_pre_faves += 1

        print(f"Matches refreshed: {len(match_cache)} matches, {new_pre_faves} pre-match faves")
        send_discord(f"🔄 **Matches refreshed**\nCached {len(match_cache)} matches, {new_pre_faves} pre-match faves")

        last_refresh = time.time()

    except Exception as e:
        print("Refresh error:", str(e))
        send_discord("⚠️ Refresh failed")

def get_pre_match_fave(match_id, sport_key, commence_time):
    try:
        # Snapshot 1 hour before kickoff
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
        print(f"Historical status for {match_id}: {r.status_code}")
        if r.status_code != 200:
            print(f"Historical error: {r.text[:300]}")
            return None

        data = r.json()
        if 'data' not in data or not data['data']:
            return None

        game_data = data['data'][0]
        min_odd = 99.0
        fave_team = None
        is_home = False
        home = game_data['home_team']
        away = game_data['away_team']
        for book in game_data['bookmakers'][:5]:
            for m in book.get('markets', []):
                if m.get('key') == 'h2h':
                    for o in m.get('outcomes', []):
                        price = float(o.get('price', 99))
                        if price < min_odd:
                            min_odd = price
                            fave_team = o['name']
                            is_home = fave_team == home
        if min_odd <= HEAVY_THRESHOLD and fave_team:
            return {
                'odd': min_odd,
                'home': home,
                'away': away,
                'is_home': is_home,
                'match_id': match_id
            }
        return None
    except Exception as e:
        print("Historical error:", str(e))
        return None

# Startup
print("Starting - initial matches fetch...")
threading.Thread(target=run_flask, daemon=True).start()
refresh_matches()

# Loop
while True:
    try:
        now = time.time()
        if now - last_refresh >= ODDS_REFRESH_INTERVAL:
            refresh_matches()

        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard", timeout=15)
        events = r.json().get("events", [])

        live_faves = []
        alerts = []

        for e in events:
            if e.get("status", {}).get("type", {}).get("state") != "in":
                continue

            comp = e["competitions"][0]
            home = comp["competitors"][0]["team"]["displayName"]
            away = comp["competitors"][1]["team"]["displayName"]
            hg = int(comp["competitors"][0].get("score", 0) or 0)
            ag = int(comp["competitors"][1].get("score", 0) or 0)

            game_time = e.get("status", {}).get("displayClock", "")
            if game_time:
                game_time = f" ({game_time})"

            match_id = e["id"]
            fave_data = None

            # Check if already in cache
            for ft, data in favorite_cache.items():
                if data['match_id'] == match_id:
                    fave_data = data
                    break

            # If not, and tied, fetch pre-match
            if not fave_data and hg == ag:
                # Find sport_key and commence_time from match_cache (by name match)
                for mid, mdata in match_cache.items():
                    if (mdata['home'] == home and mdata['away'] == away) or (mdata['home'] == away and mdata['away'] == home):
                        fave_data = get_pre_match_fave(mid, mdata['sport_key'], mdata['commence_time'])
                        if fave_data:
                            favorite_cache[fave_team] = fave_data
                            break

            if fave_data:
                score_str = f"{home} {hg}-{ag} {away}{game_time}"
                is_home = fave_data['is_home']
                odd = fave_data['odd']
                fave_team = fave_data['fave_team'] if 'fave_team' in fave_data else next(iter(favorite_cache))  # fallback
                is_losing = (is_home and hg < ag and ag >= 1) or (not is_home and ag < hg and hg >= 1)

                line = f"**{fave_team}** ({odd}) | {score_str}"
                live_faves.append({"line": line, "is_losing": is_losing})

                current = (hg, ag)
                if match_id not in last_scores or last_scores[match_id] != current:
                    fave_g = hg if is_home else ag
                    opp_g = ag if is_home else hg
                    if fave_g == 0 and opp_g >= 1:
                        alerts.append(f"🚨 **HEAVY FAVE DOWN!** {score_str}\n**{fave_team}** (pre-match {odd}) trailing")
                    last_scores[match_id] = current

        if live_faves:
            summary = f"```diff
            for fave in live_faves:
                prefix = "- " if fave["is_losing"] else ""
                summary += f"{prefix}{fave['line']}\n"
            summary += "```"
            send_discord(summary)
        else:
            print("No live heavy faves right now")

        for a in alerts:
            send_discord(a)

    except Exception as e:
        print("Loop error:", str(e))

    time.sleep(POLL_INTERVAL)
