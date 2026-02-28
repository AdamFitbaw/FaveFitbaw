import requests
import time
import os
from flask import Flask
import threading
from datetime import datetime, timedelta, timezone

WEBHOOK_URL = os.getenv('WEBHOOK_URL')
ODDS_API_KEY = os.getenv('ODDS_API_KEY')

POLL_INTERVAL = 240           # 5 minutes - live score checks
ODDS_REFRESH_INTERVAL = 1800  # 30 minutes - odds cache refresh (pre-kickoff only)
HEAVY_THRESHOLD = 1.60

favorite_cache = {}  # fave_team: {'odd': float, 'home': str, 'away': str, 'is_home': bool, 'match_id': str, 'commence_time': str}
last_scores = {}
last_odds_refresh = 0

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot alive - pre-match odds only"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        print(f"✅ Sent: {msg[:120]}...")
    except:
        pass

def refresh_odds():
    global last_odds_refresh
    try:
        commence_from = datetime.utcnow().isoformat() + 'Z'
        commence_to = (datetime.utcnow() + timedelta(hours=6)).isoformat() + 'Z'

        # You can shorten this list if you want faster requests / fewer quota use
        sports = (
            "soccer_africa_cup_of_nations,soccer_argentina_primera_division,soccer_australia_aleague,"
            "soccer_austria_bundesliga,soccer_belgium_first_div,soccer_brazil_campeonato,soccer_brazil_serie_b,"
            "soccer_chile_campeonato,soccer_china_superleague,soccer_denmark_superliga,soccer_efl_champ,"
            "soccer_england_efl_cup,soccer_england_league1,soccer_england_league2,soccer_epl,soccer_fa_cup,"
            "soccer_fifa_world_cup,soccer_fifa_world_cup_qualifiers_europe,soccer_fifa_world_cup_qualifiers_south_america,"
            "soccer_fifa_world_cup_womens,soccer_fifa_world_cup_winner,soccer_fifa_club_world_cup,"
            "soccer_finland_veikkausliiga,soccer_france_ligue_one,soccer_france_ligue_two,soccer_germany_bundesliga,"
            "soccer_germany_bundesliga2,soccer_germany_liga3,soccer_greece_super_league,soccer_italy_serie_a,"
            "soccer_italy_serie_b,soccer_japan_j_league,soccer_korea_kleague1,soccer_league_of_ireland,"
            "soccer_mexico_ligamx,soccer_netherlands_eredivisie,soccer_norway_eliteserien,soccer_poland_ekstraklasa,"
            "soccer_portugal_primeira_liga,soccer_russia_premier_league,soccer_spain_la_liga,soccer_spain_segunda_division,"
            "soccer_spl,soccer_sweden_allsvenskan,soccer_sweden_superettan,soccer_switzerland_superleague,"
            "soccer_turkey_super_league,soccer_uefa_europa_conference_league,soccer_uefa_champs_league,"
            "soccer_uefa_champs_league_qualification,soccer_uefa_champs_league_women,soccer_uefa_europa_league,"
            "soccer_uefa_european_championship,soccer_uefa_euro_qualification,soccer_uefa_nations_league,"
            "soccer_concacaf_gold_cup,soccer_concacaf_leagues_cup,soccer_conmebol_copa_america,"
            "soccer_conmebol_copa_libertadores,soccer_conmebol_copa_sudamericana,soccer_usa_mls"
        )

        url = (
            f"https://api.the-odds-api.com/v4/sports/odds/?apiKey={ODDS_API_KEY}"
            f"&regions=eu&markets=h2h&oddsFormat=decimal"
            f"&sports={sports}"
            f"&commenceTimeFrom={commence_from}&commenceTimeTo={commence_to}"
        )

        r = requests.get(url, timeout=15)
        odds_data = r.json()

        new_faves = 0
        updated = 0

        for game in odds_data:
            # Only consider pre-kickoff games
            commence_time = datetime.fromisoformat(game['commence_time'].rstrip('Z')).replace(tzinfo=timezone.utc)
            if commence_time <= datetime.now(timezone.utc):
                continue  # Skip live or started games

            match_id = game['id']
            if match_id in [data['match_id'] for data in favorite_cache.values()]:
                # Update existing if pre-kickoff (e.g., odds changed)
                updated += 1
            else:
                new_faves += 1

            if not game.get('bookmakers'):
                continue
            home = game['home_team']
            away = game['away_team']
            min_odd = 99
            fave_team = None
            is_home = False
            for b in game['bookmakers'][:5]:
                for m in b.get('markets', []):
                    if m.get('key') == 'h2h':
                        for o in m['outcomes']:
                            if o['price'] < min_odd:
                                min_odd = o['price']
                                fave_team = o['name']
                                is_home = fave_team == home
            if min_odd <= HEAVY_THRESHOLD and fave_team:
                favorite_cache[fave_team] = {
                    'odd': min_odd,
                    'home': home,
                    'away': away,
                    'is_home': is_home,
                    'match_id': match_id,
                    'commence_time': game['commence_time']
                }

        print(f"⭐ Pre-match odds refreshed: {new_faves} new, {updated} updated heavy faves cached")
        send_discord(
            f"🔄 **Pre-match odds cache refreshed**\n"
            f"Added {new_faves} new / updated {updated} heavy faves (≤{HEAVY_THRESHOLD}) for next 6 hours (upcoming only)"
        )
        last_odds_refresh = time.time()

    except Exception as e:
        print("Odds refresh error:", e)
        send_discord("⚠️ Pre-match odds refresh failed — check Railway logs")

# ==================== START ====================
print("🚀 Bot starting - first pre-match odds scan...")
threading.Thread(target=run_flask, daemon=True).start()

# Do first odds fetch immediately
refresh_odds()

# ==================== MAIN LOOP ====================
while True:
    try:
        current_time = time.time()
        # Refresh pre-match odds every 30 minutes
        if current_time - last_odds_refresh >= ODDS_REFRESH_INTERVAL:
            refresh_odds()

        # Free live scores worldwide
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
            match_str = f"{home} vs {away}"

            # Game time (if available from ESPN)
            game_time = e.get("status", {}).get("displayClock", "")
            if game_time:
                game_time = f" ({game_time})"

            for fave_team, data in list(favorite_cache.items()):
                if fave_team in home or fave_team in away or match_str in f"{data['home']} vs {data['away']}":
                    score_str = f"{home} {hg}-{ag} {away}{game_time}"
                    is_losing = (data['is_home'] and hg < ag and ag >= 1) or \
                                (not data['is_home'] and ag < hg and hg >= 1)

                    line = f"**{fave_team}** ({data['odd']}) | {score_str}"
                    live_faves.append({"line": line, "is_losing": is_losing})

                    current = (hg, ag)
                    match_id = e["id"]
                    if match_id not in last_scores or last_scores[match_id] != current:
                        fave_g = hg if data['is_home'] else ag
                        opp_g = ag if data['is_home'] else hg
                        if fave_g == 0 and opp_g >= 1:
                            alerts.append(f"🚨 **HEAVY FAVE DOWN!** {score_str}\n**{fave_team}** (pre-match {data['odd']}) trailing")
                        last_scores[match_id] = current

        if live_faves:
            summary = f"```diff\n**🔴 Live Heavy Faves (pre-match ≤{HEAVY_THRESHOLD})**\n"
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
        print("Loop error:", e)

    time.sleep(POLL_INTERVAL)
