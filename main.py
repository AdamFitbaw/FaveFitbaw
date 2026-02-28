import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime, timezone

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')

POLL_INTERVAL = 240  # 5 minutes
HEAVY_THRESHOLD = 1.60
ODDS_FETCH_EVERY = 6  # fetch odds every 6 polls (~30 min) to save quota

favorite_cache = {}  # team_name: {'odd': float, 'match_id': str}
last_scores = {}
poll_count = 0

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot alive - real odds testing"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"✅ Sent: {msg[:120]}...")
    except:
        pass

print("🚀 Real-odds testing bot started (The Odds API free + ESPN scores)")
threading.Thread(target=run_flask, daemon=True).start()
send_discord("✅ **Real-Odds Testing Bot ONLINE**\nThreshold ≤1.60\nPolling: 5 min\nOdds fetched every ~30 min\n(You can delete this message)")

while True:
    try:
        poll_count += 1

        # === FETCH PRE-MATCH ODDS (real bookie odds) ===
        if poll_count % ODDS_FETCH_EVERY == 1 or poll_count == 1:
            try:
                r = requests.get(f"https://api.the-odds-api.com/v4/sports/soccer/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal", timeout=15)
                odds_data = r.json()
                new_faves = 0
                for game in odds_data:
                    if not game.get('bookmakers'):
                        continue
                    home = game['home_team']
                    away = game['away_team']
                    # Get lowest odds (heaviest fave)
                    min_odd = 99
                    fave_team = None
                    for b in game['bookmakers'][:5]:  # first 5 bookies
                        for m in b.get('markets', []):
                            if m.get('key') == 'h2h':
                                for o in m['outcomes']:
                                    if o['price'] < min_odd:
                                        min_odd = o['price']
                                        fave_team = o['name']
                    if min_odd <= HEAVY_THRESHOLD and fave_team:
                        favorite_cache[fave_team] = {'odd': min_odd, 'match_id': game['id']}
                        new_faves += 1
                print(f"⭐ Fetched odds — {new_faves} new heavy faves cached")
            except Exception as e:
                print("Odds fetch error:", e)

        # === LIVE SCORES (ALL soccer worldwide - free) ===
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard", timeout=15)
        events = r.json().get("events", [])

        live_faves = []
        alerts = []

        for e in events:
            if e.get("status", {}).get("type", {}).get("state") != "in":
                continue  # only live

            comp = e["competitions"][0]
            home = comp["competitors"][0]["team"]["displayName"]
            away = comp["competitors"][1]["team"]["displayName"]
            hg = int(comp["competitors"][0].get("score", 0) or 0)
            ag = int(comp["competitors"][1].get("score", 0) or 0)

            for fave_team, data in list(favorite_cache.items()):
                if fave_team in home or fave_team in away:
                    score_str = f"{home} {hg}-{ag} {away}"
                    live_faves.append(f"• **{fave_team}** ({data['odd']}) | {score_str}")

                    # Alert if fave is losing
                    if (fave_team in home and hg < ag and ag >= 1) or (fave_team in away and ag < hg and hg >= 1):
                        alerts.append(f"🚨 **HEAVY FAVE DOWN!** {score_str}\n**{fave_team}** ({data['odd']}) trailing")

        # Summary every poll
        if live_faves:
            summary = f"**🔴 Live Heavy Faves (real odds ≤{HEAVY_THRESHOLD})**\n" + "\n".join(live_faves[:20])
            if len(live_faves) > 20:
                summary += f"\n... +{len(live_faves)-20} more"
            send_discord(summary)
        else:
            print("No live heavy faves right now")

        for a in alerts:
            send_discord(a)

    except Exception as e:
        print("Loop error:", e)

    time.sleep(POLL_INTERVAL)
