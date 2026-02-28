import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime

# ====================== CONFIG ======================
API_KEY = os.getenv('API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
HEADERS = {'x-apisports-key': API_KEY}

POLL_INTERVAL = 250          # you set this — change to 600 if you hit quota too fast
HEAVY_FAVE_THRESHOLD = 1.60

favorite_cache = {}   # fixture_id → fave details
last_scores = {}

# ====================== KEEP-ALIVE SERVER (fixes restarts) ======================
app = Flask(__name__)

@app.route('/')
def home():
    return f"✅ Soccer Alert Bot is ALIVE | Poll: {POLL_INTERVAL}s | Threshold: {HEAVY_FAVE_THRESHOLD}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

# ====================== HELPER FUNCTIONS ======================
def send_discord(msg):
    if not WEBHOOK_URL:
        print("❌ No WEBHOOK_URL set!")
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"✅ Discord sent: {msg[:100]}...")
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
        print(f"Odds error for {fid}:", e)
        return None

# ====================== STARTUP ======================
print("🚀 Soccer Alert Bot STARTING...")
print(f"API_KEY: {'✅ LOADED' if API_KEY and len(API_KEY)>30 else '❌ MISSING'}")
print(f"WEBHOOK_URL: {'✅ LOADED' if WEBHOOK_URL and WEBHOOK_URL.startswith('https') else '❌ MISSING'}")
print(f"Polling every {POLL_INTERVAL}s | Threshold ≤ {HEAVY_FAVE_THRESHOLD}")

if not API_KEY or not WEBHOOK_URL:
    print("⚠️ CRITICAL: Missing variables — fix in Railway Variables tab!")

threading.Thread(target=run_flask, daemon=True).start()
print("✅ Keep-alive web server started (prevents restarts)")

send_discord(f"✅ **Soccer Alert Bot is now ONLINE**\nThreshold: {HEAVY_FAVE_THRESHOLD}\nPolling: {POLL_INTERVAL}s")

print("Bot running — waiting for first poll...")

# ====================== MAIN LOOP ======================
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

            # Cache heavy favorite (once per match)
            if fid not in favorite_cache and status in ["1H", "HT", "2H", "LIVE", "ET", "P"]:
                fave = get_heavy_favorite(fid, home, away)
                if fave:
                    favorite_cache[fid] = fave
                    print(f"⭐ Cached heavy fave: {fave['team']} @ {fave['odd']}")

            # Alert if heavy fave goes 1-0 down or worse
            if fid in favorite_cache:
                fave = favorite_cache[fid]
                fave_g = hg if fave["is_home"] else ag
                opp_g = ag if fave["is_home"] else hg

                current = (hg, ag)
                if fid not in last_scores or last_scores[fid] != current:
                    if fave_g == 0 and opp_g >= 1:
                        msg = f"🚨 **HEAVY FAVE DOWN!** {home} {hg}-{ag} {away}\n**{fave['team']}** ({fave['odd']}) trailing"
                        send_discord(msg)
                    last_scores[fid] = current

                # Add to summary list
                current_faves.append(f"• **{fave['team']}** ({fave['odd']}) | {home} {hg}-{ag} {away}")

        # === YOUR REQUESTED FEATURE ===
        if current_faves:
            summary = f"**🔴 Live Heavy Favorites** (odds ≤ {HEAVY_FAVE_THRESHOLD})\n\n" + "\n".join(current_faves[:10])
            if len(current_faves) > 10:
                summary += f"\n... +{len(current_faves)-10} more"
            send_discord(summary)
        else:
            print("No heavy favorites live right now")

    except Exception as e:
        print("Loop error:", e)

    time.sleep(POLL_INTERVAL)
