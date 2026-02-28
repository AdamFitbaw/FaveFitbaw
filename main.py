import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime, timedelta, timezone

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')

POLL_INTERVAL = 240           # 5 min
ODDS_REFRESH_INTERVAL = 1800  # 30 min
HEAVY_THRESHOLD = 1.60

favorite_cache = {}  # fave_team: {'odd': float, 'home': str, 'away': str, 'is_home': bool, 'match_id': str, 'commence_time': str}
last_scores = {}
last_odds_refresh = 0

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot alive - using 'upcoming' for reliable odds"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"Sent: {msg[:120]}...")
    except:
        pass

def refresh_odds():
    global last_odds_refresh
    try:
        now_utc = datetime.now(timezone.utc)
        commence_from = now_utc.isoformat().replace('+00:00', 'Z')
        commence_to = (now_utc + timedelta(hours=6)).isoformat().replace('+00:00', 'Z')

        # Use 'upcoming' - returns upcoming + live across all sports (no sport list needed)
        url = (
            f"https://api.the-odds-api.com/v4/sports/upcoming/odds/"
            f"?apiKey={ODDS_API_KEY}"
            f"&regions=eu"
            f"&markets=h2h"
            f"&oddsFormat=decimal"
            f"&commenceTimeFrom={commence_from}"
            f"&commenceTimeTo={commence_to}"
        )

        r = requests.get(url, timeout=15)
        print(f"Odds API status: {r.status_code}")
        if r.status_code != 200:
            print(f"API error response: {r.text[:300]}")
            send_discord(f"⚠️ Odds fetch failed ({r.status_code}) - check logs")
            return

        odds_data = r.json()

        favorite_cache.clear()
        new_faves = 0

        for game in odds_data:
            commence_str = game.get('commence_time')
            if not commence_str:
                continue
            commence_time = datetime.fromisoformat(commence_str.rstrip('Z')).replace(tzinfo=timezone.utc)
            if commence_time <= now_utc:
                continue  # Strict pre-kickoff only

            if not game.get('bookmakers'):
                continue

            home = game['home_team']
            away = game['away_team']
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
                    'match_id': game['id'],
                    'commence_time': commence_str
                }
                new_faves += 1

        print(f"Pre-match odds refreshed: {new_faves} heavy faves cached")
        send_discord(f"🔄 **Pre-match odds refreshed**\nCached {new_faves} heavy faves (≤{HEAVY_THRESHOLD}) for upcoming games")

        last_odds_refresh = time.time()

    except Exception as e:
        print("Refresh error:", str(e))
        send_discord("⚠️ Refresh failed - check logs")

# Startup
print("Starting - initial odds fetch...")
threading.Thread(target=run_flask, daemon=True).start()
refresh_odds()

# Main loop
while True:
    try:
        now = time.time()
        if now - last_odds_refresh >= ODDS_REFRESH_INTERVAL:
            refresh_odds()

        # Live scores
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

            for fave_team, data in list(favorite_cache.items()):
                if fave_team in home or fave_team in away:
                    score_str = f"{home} {hg}-{ag} {away}{game_time}"
                    is_losing = (data['is_home'] and hg < ag and ag >= 1) or \
                                (not data['is_home'] and ag < hg and hg >= 1)

                    line = f"**{fave_team}** ({data['odd']}) | {score_str}"
                    live_faves.append({"line": line, "is_losing": is_losing})

                    match_id = e["id"]
                    current = (hg, ag)
                    if match_id not in last_scores or last_scores[match_id] != current:
                        fave_g = hg if data['is_home'] else ag
                        opp_g = ag if data['is_home'] else hg
                        if fave_g == 0 and opp_g >= 1:
                            alerts.append(f"🚨 **HEAVY FAVE DOWN!** {score_str}\n**{fave_team}** (pre-match {data['odd']}) trailing")
                        last_scores[match_id] = current

        if live_faves:
            summary = f"```diff\n**🔴 Live Heavy Faves (pre-match odds only)**\n"
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
