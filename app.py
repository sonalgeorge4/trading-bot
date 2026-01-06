import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from flask import Flask, render_template_string, jsonify
from flask_cors import CORS
import threading

load_dotenv()

SIMULATION = True
POLL_INTERVAL = 10
MAX_OPEN_TRADES = 5
START_CAPITAL = float(os.getenv("START_CAPITAL", 20.0))
RISK_PCT = 0.15
MAX_POSITION_SIZE_USD = 3.0
MIN_CAPITAL_BUY = 2.0
MIN_ENTRY_PRICE = 0.05
MIN_YES_PRICE = 0.70
MAX_NO_PRICE = 0.30
X_ABS_MOVE = 0.01
EARLY_ABS_MOVE = 0.005
TRAILING_SL_PCT = 0.05
LIQUIDITY_MIN = 50
LIQUIDITY_SURGE_THRESHOLD = 40
MIN_EXPIRY_BUFFER_HIGH_CONF = 120
MIN_EXPIRY_BUFFER_LOW_CONF = 300
MARKETS_PER_PAGE = 100
NUM_PAGES = 50
CLOBB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

capital = START_CAPITAL
positions = {}
last_yes_prices = {}
last_no_prices = {}
prev_yes_prices = {}
prev_no_prices = {}
last_liq = {}
prev_liq = {}
error_count = 0
start_time = datetime.now(timezone.utc)
log_messages = []
bot_running = False

app = Flask(__name__)
CORS(app)

def log(msg: str):
    global log_messages
    ts = datetime.now(timezone.utc).isoformat()
    full_msg = f"[{ts}] {msg}"
    print(full_msg)
    log_messages.append({"time": ts, "message": msg})
    if len(log_messages) > 500:
        log_messages.pop(0)

def send_telegram(message: str):
    pass

def fetch_markets() -> List[Dict]:
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    out = []
    for page in range(NUM_PAGES):
        params = {
            "limit": MARKETS_PER_PAGE,
            "offset": page * MARKETS_PER_PAGE,
            "active": "true",
            "closed": "false",
            "end_date_min": now.isoformat(),
            "end_date_max": tomorrow.isoformat(),
            "liquidity_num_min": LIQUIDITY_MIN,
        }
        try:
            r = requests.get(f"{GAMMA_HOST}/markets", params=params, timeout=10)
            response = r.json()
            if isinstance(response, list):
                data = response
            elif isinstance(response, dict):
                data = response.get("data", [])
            else:
                data = []
            if not data:
                break
            out.extend(data)
        except Exception as e:
            log(f"Error fetching markets page {page}: {e}")
            break
    return out

def extract_token(market: Dict, side: str) -> Tuple[Optional[str], Optional[float]]:
    try:
        clob_ids_raw = market.get("clobTokenIds", "")
        outcomes_raw = market.get("outcomes", "")
        prices_raw = market.get("outcomePrices", "")
        
        if not all([clob_ids_raw, outcomes_raw, prices_raw]):
            return None, None
        
        if isinstance(clob_ids_raw, str):
            ids = json.loads(clob_ids_raw)
        else:
            ids = clob_ids_raw
            
        if isinstance(outcomes_raw, str):
            outcomes = [o.upper() for o in json.loads(outcomes_raw)]
        else:
            outcomes = [o.upper() for o in outcomes_raw]
            
        if isinstance(prices_raw, str):
            prices = list(map(float, json.loads(prices_raw)))
        else:
            prices = list(map(float, prices_raw))
        
        idx = outcomes.index(side)
        return ids[idx], prices[idx]
    except:
        return None, None

def update_price_cache(markets: List[Dict]):
    global prev_yes_prices, prev_no_prices, prev_liq
    prev_yes_prices = last_yes_prices.copy()
    prev_no_prices = last_no_prices.copy()
    prev_liq = last_liq.copy()
    last_yes_prices.clear()
    last_no_prices.clear()
    last_liq.clear()
    for m in markets:
        mid = m.get("id")
        if not mid:
            continue
        last_liq[mid] = m.get("liquidityNum", 0)
        _, y = extract_token(m, "YES")
        _, n = extract_token(m, "NO")
        if y is not None:
            last_yes_prices[mid] = y
        if n is not None:
            last_no_prices[mid] = n

def expiry_ok(market: Dict, price: float) -> bool:
    try:
        end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
        remaining = (end - datetime.now(timezone.utc)).total_seconds()
        buf = MIN_EXPIRY_BUFFER_HIGH_CONF if price > 0.9 else MIN_EXPIRY_BUFFER_LOW_CONF
        return remaining >= buf
    except:
        return False

def should_buy_yes(market: Dict) -> bool:
    mid = market.get("id")
    _, price = extract_token(market, "YES")
    if price is None or price < MIN_YES_PRICE:
        return False
    if not expiry_ok(market, price):
        return False
    prev = prev_yes_prices.get(mid)
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    if prev is None:
        return liq_jump >= LIQUIDITY_SURGE_THRESHOLD
    return (
        price - prev >= X_ABS_MOVE or
        (prev > 0.85 and price - prev >= EARLY_ABS_MOVE) or
        liq_jump >= LIQUIDITY_SURGE_THRESHOLD
    )

def should_buy_no(market: Dict) -> bool:
    mid = market.get("id")
    _, price = extract_token(market, "NO")
    if price is None or price > MAX_NO_PRICE:
        return False
    if not expiry_ok(market, price):
        return False
    prev = prev_no_prices.get(mid)
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    if prev is None:
        return liq_jump >= LIQUIDITY_SURGE_THRESHOLD
    return (
        prev - price >= X_ABS_MOVE or
        (prev < 0.15 and prev - price >= EARLY_ABS_MOVE) or
        liq_jump >= LIQUIDITY_SURGE_THRESHOLD
    )

def buy(token: str, price: float, market: Dict, side: str):
    global capital
    stake = min(capital * RISK_PCT, MAX_POSITION_SIZE_USD)
    if stake < 1 or capital < stake:
        return
    shares = stake / price
    capital -= stake
    pos = {
        "entry": price,
        "shares": shares,
        "side": side,
        "market": market.get("question", "Unknown"),
        "market_id": market.get("id"),
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "high": price,
        "low": price,
    }
    positions[token] = pos
    msg = f"BUY {side} {market.get('question', 'Unknown')[:45]} @ {price:.3f} | Shares: {shares:.2f} | Capital: ${capital:.2f}"
    log(msg)

def check_exit(token: str, pos: Dict):
    global capital
    price = get_price(token)
    if price is None:
        return
    entry = pos["entry"]
    shares = pos["shares"]
    side = pos.get("side", "YES")
    
    if side == "YES" and price >= 0.98:
        proceeds = shares * price
        pnl = proceeds - (shares * entry)
        capital += proceeds
        del positions[token]
        log(f"EXIT YES TP @ {price:.3f} | PnL: ${pnl:.2f} | Capital: ${capital:.2f}")
        return
   
    if side == "NO" and price <= 0.02:
        proceeds = shares * price
        pnl = proceeds - (shares * entry)
        capital += proceeds
        del positions[token]
        log(f"EXIT NO TP @ {price:.3f} | PnL: ${pnl:.2f} | Capital: ${capital:.2f}")
        return
    
    if side == "YES":
        pos["high"] = max(pos["high"], price)
        if price < pos["high"] * (1 - TRAILING_SL_PCT):
            proceeds = shares * price
            pnl = proceeds - (shares * entry)
            capital += proceeds
            del positions[token]
            log(f"EXIT YES SL @ {price:.3f} | PnL: ${pnl:.2f} | Capital: ${capital:.2f}")
    else:
        pos["low"] = min(pos["low"], price)
        if price > pos["low"] * (1 + TRAILING_SL_PCT):
            proceeds = shares * price
            pnl = proceeds - (shares * entry)
            capital += proceeds
            del positions[token]
            log(f"EXIT NO SL @ {price:.3f} | PnL: ${pnl:.2f} | Capital: ${capital:.2f}")

def get_price(token_id: str) -> Optional[float]:
    try:
        r = requests.get(f"{CLOBB_HOST}/price?token_id={token_id}", timeout=5).json()
        bid = float(r.get("bid", 0))
        ask = float(r.get("ask", 0))
        if bid and ask:
            return (bid + ask) / 2
        return None
    except:
        return None

def bot_loop():
    global capital, error_count, bot_running
    log(f"BOT STARTED | Capital ${capital:.2f}")
    bot_running = True
    while bot_running:
        try:
            markets = fetch_markets()
            log(f"Fetched {len(markets)} markets")
            
            update_price_cache(markets)
            for m in markets:
                if len(positions) >= MAX_OPEN_TRADES:
                    break
                mid = m.get("id")
                if not mid:
                    continue
                ytok, yprice = extract_token(m, "YES")
                ntok, nprice = extract_token(m, "NO")
                if len(positions) < MAX_OPEN_TRADES and ytok and ytok not in positions and should_buy_yes(m):
                    buy(ytok, yprice, m, "YES")
                    continue
                if len(positions) < MAX_OPEN_TRADES and ntok and ntok not in positions and should_buy_no(m):
                    buy(ntok, nprice, m, "NO")
            
            for t, p in list(positions.items()):
                check_exit(t, p)
            
            log(f"SCAN DONE | Open {len(positions)}/{MAX_OPEN_TRADES} | Capital ${capital:.2f}")
            error_count = 0
            time.sleep(POLL_INTERVAL)
           
        except Exception as e:
            error_count += 1
            log(f"ERROR ({error_count}/5): {e}")
            if error_count >= 5:
                log("CRITICAL: Too many errors. Shutting down.")
                bot_running = False
                break
            time.sleep(60)

@app.route('/')
def dashboard():
    html = '''<!DOCTYPE html><html><head><title>Trading Bot Dashboard</title><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>* { margin: 0; padding: 0; box-sizing: border-box; } body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; } .container { max-width: 1200px; margin: 0 auto; } .header { background: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } .header h1 { color: #333; margin-bottom: 10px; } .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 20px; } .stat-box { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; } .stat-label { font-size: 12px; opacity: 0.9; margin-bottom: 10px; } .stat-value { font-size: 28px; font-weight: bold; } .positions, .logs { background: white; border-radius: 10px; padding: 30px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } .positions h2, .logs h2 { color: #333; margin-bottom: 20px; border-bottom: 2px solid #667eea; padding-bottom: 10px; } table { width: 100%; border-collapse: collapse; } th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; } th { background: #f5f5f5; font-weight: 600; color: #333; } .log-entry { padding: 10px; margin: 5px 0; background: #f9f9f9; border-left: 4px solid #667eea; font-family: monospace; font-size: 12px; } .control-btn { background: #667eea; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; } .control-btn:hover { background: #764ba2; } .control-btn.stop { background: #e74c3c; } .control-btn.stop:hover { background: #c0392b; } .no-data { text-align: center; color: #999; padding: 20px; } .status { display: inline-block; padding: 5px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; } .status.running { background: #2ecc71; color: white; } .status.stopped { background: #e74c3c; color: white; }</style></head><body><div class="container"><div class="header"><div style="display: flex; justify-content: space-between; align-items: center;"><div><h1>🤖 Trading Bot Dashboard</h1><p id="status" style="color: #666; margin-top: 5px;"><span class="status stopped">STOPPED</span></p></div><div><button class="control-btn" onclick="startBot()">Start Bot</button><button class="control-btn stop" onclick="stopBot()">Stop Bot</button></div></div><div class="stats"><div class="stat-box"><div class="stat-label">CAPITAL</div><div class="stat-value" id="capital">$0.00</div></div><div class="stat-box"><div class="stat-label">OPEN POSITIONS</div><div class="stat-value" id="open-pos">0</div></div><div class="stat-box"><div class="stat-label">ERRORS</div><div class="stat-value" id="errors">0</div></div></div></div><div class="positions"><h2>Open Positions</h2><table><thead><tr><th>Side</th><th>Market</th><th>Entry Price</th><th>Shares</th><th>Entry Time</th></tr></thead><tbody id="pos-tbody"><tr><td colspan="5" class="no-data">No open positions</td></tr></tbody></table></div><div class="logs"><h2>Recent Logs</h2><div id="logs-container" style="max-height: 400px; overflow-y: auto;"></div></div></div><script>function updateDashboard() { fetch('/api/status').then(r => r.json()).then(data => { document.getElementById('capital').textContent = '$' + data.capital.toFixed(2); document.getElementById('open-pos').textContent = data.positions_count; document.getElementById('errors').textContent = data.error_count; let statusText = data.bot_running ? '<span class="status running">RUNNING</span>' : '<span class="status stopped">STOPPED</span>'; document.getElementById('status').innerHTML = statusText; let tbody = document.getElementById('pos-tbody'); if (data.positions.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="no-data">No open positions</td></tr>'; } else { tbody.innerHTML = data.positions.map(p => `<tr><td><strong>${p.side}</strong></td><td>${p.market.substring(0, 40)}...</td><td>${p.entry.toFixed(4)}</td><td>${p.shares.toFixed(2)}</td><td>${new Date(p.entry_time).toLocaleString()}</td></tr>`).join(''); } let logContainer = document.getElementById('logs-container'); logContainer.innerHTML = data.logs.slice(-20).reverse().map(l => `<div class="log-entry"><strong>${l.time.substring(11, 19)}</strong> ${l.message}</div>`).join(''); }); } function startBot() { fetch('/api/start', {method: 'POST'}).then(r => r.json()).then(data => { updateDashboard(); }); } function stopBot() { fetch('/api/stop', {method: 'POST'}).then(r => r.json()).then(data => { updateDashboard(); }); } updateDashboard(); setInterval(updateDashboard, 2000);</script></body></html>'''
    return render_template_string(html)

@app.route('/api/status')
def get_status():
    return jsonify({
        "capital": capital,
        "positions_count": len(positions),
        "positions": list(positions.values()),
        "error_count": error_count,
        "bot_running": bot_running,
        "logs": log_messages
    })

@app.route('/api/start', methods=['POST'])
def start_bot():
    global bot_running
    if not bot_running:
        bot_running = True
        threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"status": "Bot started"})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    global bot_running
    bot_running = False
    return jsonify({"status": "Bot stopped"})

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
