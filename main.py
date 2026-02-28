import requests
import time
import os

API_KEY = os.getenv('API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
HEADERS = {'x-apisports-key': API_KEY}

POLL_INTERVAL = 300  # 5 min (change to 600 for 10 min if you want fewer requests)
HEAVY_FAVE_THRESHOLD = 1.4  # lower = stricter (e.g. 1.50 for massive faves only)

favorite_cache = {}  # fixture_id: {'team': str, 'is_home': bool}
last_scores = {}

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
    except:
        pass

def get_heavy_favorite(fid, home_team, away_team):
    try:
        r = requests.get(f"https://v3.football.api-sports.io/odds?fixture={fid}&bet=1", headers=HEADERS, timeout=15)
        data = r.json()
        for entry in data.get("response", []):
            for bookie in entry.get("bookmakers", [])[:3]:  # first 3 bookies
                for bet in bookie.get("bets", []):
                    if bet.get("id") == 1 or "winner" in bet.get("name", "").lower():
                        vals = {}
                        for v in bet.get("values", []):
                            try:
                                if v.get("value") in ["Home", "Away", "1", "2"]:
                                    vals[v["value"]] = float(v["odd"])
                            except:
                                pass
                        if len(vals) < 2:
                            continue
                        min_odd = min(vals.values())
                        if min_odd > HEAVY_FAVE_THRESHOLD:
                            continue
                        fave_side = min(vals, key=vals.get)
                        if fave_side in ["Home", "1"]:
                            return {"team": home_team, "is_home": True, "odd": min_odd}
                        elif fave_side in ["Away", "2"]:
                            return {"team": away_team, "is_home": False, "odd": min_odd}
        return None
    except:
        return None

print("Bot running - odds + live scores active")

while True:
    try:
        r = requests.get("https://v3.football.api-sports.io/fixtures/live", headers=HEADERS, timeout=15)
        matches = r.json().get("response", [])
        
        for m in matches:
            fid = m["fixture"]["id"]
            home = m["teams"]["home"]["name"]
            away = m["teams"]["away"]["name"]
            hg = m["goals"]["home"] or 0
            ag = m["goals"]["away"] or 0
            status = m["fixture"]["status"].get("short", "")
            
            # Cache fave once when match starts
            if fid not in favorite_cache and status in ["1H", "HT", "2H", "LIVE", "ET", "P"]:
                fave = get_heavy_favorite(fid, home, away)
                if fave:
                    favorite_cache[fid] = fave
                    print(f"HEAVY FAVE CACHED: {fave['team']} @ {fave['odd']}")
            
            # Alert on score change
            current = (hg, ag)
            if fid in favorite_cache and (fid not in last_scores or last_scores[fid] != current):
                fave = favorite_cache[fid]
                fave_g = hg if fave["is_home"] else ag
                opp_g = ag if fave["is_home"] else hg
                
                if fave_g == 0 and opp_g >= 1:  # 1-0 down or worse
                    msg = f"🚨 **HEAVY FAVE DOWN!** {home} {hg}-{ag} {away}\n"
                    msg += f"**{fave['team']}** (pre-match {fave['odd']}) trailing"
                    send_discord(msg)
                    print(f"ALERT SENT for fixture {fid}")
                
                last_scores[fid] = current
    except Exception as e:
        print("Loop error:", e)
    
    time.sleep(POLL_INTERVAL)
