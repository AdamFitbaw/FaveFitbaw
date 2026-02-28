import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime

API_KEY = os.getenv('API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
HEADERS = {'x-apisports-key': API_KEY}

POLL_INTERVAL = 250
HEAVY_FAVE_THRESHOLD = 2.00   # ← lowered for testing (will catch almost everything)

favorite_cache = {}
last_scores = {}

app = Flask(__name__)

@app.route('/')
def home():
    return f"✅ Bot alive | Threshold: {HEAVY_FAVE_THRESHOLD}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"✅ Sent: {msg[:80]}...")
    except Exception as e:
        print("Discord error:", e)

def get_heavy_favorite(fid, home_team, away_team):
    try:
        r = requests.get(f"https://v3.football.api-sports.io/odds?fixture={fid}&bet=1", headers=HEADERS, timeout=15)
        data = r.json()
        for entry in data.get("response", []):
            for bookie in entry.get("bookmakers", [])[:3]:
                for bet in bookie.get("bets", []):
                    if bet.get("id") == 1 or "winner" in str(bet.get("name","")).lower():
                        vals = {}
                        for v in bet.get("values", []):
                            try:
                                odd = float(v.get("odd", 0))
                                val = v.get("value")
                                if val in ["Home", "1"]:
                                    vals["Home"] = odd
                                elif val in ["Away", "2"]:
                                    vals["Away"] = odd
                            except:
                                pass
                        if len(vals) >= 2:
                            min_odd = min(vals.values())
                            if min_odd <= HEAVY_FAVE_THRESHOLD:
                                fave_side = min(vals, key=vals.get)
                                if fave_side == "Home":
                                    return {"team": home_team, "is_home": True, "odd": min_odd}
                                else:
                                    return {"team": away_team, "is_home": False, "odd": min_odd}
        return None
    except Exception as e:
        print("Odds fetch error:", e)
        return None

print("🚀 DEBUG VERSION STARTED — threshold lowered to 2.00")
threading.Thread(target=run_flask, daemon=True).start()
send_discord("🛠️ **DEBUG MODE ACTIVE**\nThreshold lowered to 2.00 for testing\nYou will now get a status update every ~4 minutes")

while True:
    try:
        r = requests.get("https://v3.football.api-sports.io/fixtures/live", headers=HEADERS, timeout=15)
        matches = r.json().get("response", [])
        print(f"📊 {datetime.now().strftime('%H:%M')} — {len(matches)} live matches")

        current_faves = []

        for m in matches:
            fid = m["fixture"]["id"]
            home = m["teams"]["home"]["name"]
            away = m["teams"]["away"]["name"]
            hg = m["goals"]["home"] or 0
            ag = m["goals"]["away"] or 0
            status = m["fixture"]["status"].get("short", "")

            if fid not in favorite_cache and status in ["1H", "HT", "2H", "LIVE", "ET", "P"]:
                fave = get_heavy_favorite(fid, home, away)
                if fave:
                    favorite_cache[fid] = fave
                    print(f"⭐ CACHED: {fave['team']} @ {fave['odd']}")

            if fid in favorite_cache:
                fave = favorite_cache[fid]
                current_faves.append(f"• **{fave['team']}** ({fave['odd']}) | {home} {hg}-{ag} {away}")

        # ALWAYS send status (even if zero)
        status_msg = f"**📊 Live Status** ({datetime.now().strftime('%H:%M')})\n"
        status_msg += f"Live matches: {len(matches)}\n"
        status_msg += f"Heavy faves (≤{HEAVY_FAVE_THRESHOLD}): {len(current_faves)}\n\n"
        if current_faves:
            status_msg += "\n".join(current_faves[:8])
        else:
            status_msg += "No heavy favorites live right now"
        send_discord(status_msg)

    except Exception as e:
        print("Loop error:", e)

    time.sleep(POLL_INTERVAL)
