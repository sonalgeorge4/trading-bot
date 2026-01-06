import os
import json
import time
import requests
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from flask import Flask, render_template_string, jsonify
from flask_cors import CORS
import threading

load_dotenv()

# =====================
# CONFIG
# =====================
SIMULATION = True
POLL_INTERVAL = 10
MAX_OPEN_TRADES = 5
START_CAPITAL = 20.0
RISK_PCT = 0.15
MAX_POSITION_SIZE_USD = 3.0
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

# =====================
# STATE
# =====================
capital = START_CAPITAL
initial_capital = START_CAPITAL
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
closed_trades = []

app = Flask(__name__)
CORS(app)

# =====================
# DATABASE
# =====================
DB_FILE = "trading_bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY, token TEXT, side TEXT, entry_price REAL, 
                  shares REAL, entry_time TEXT, exit_price REAL, exit_time TEXT, 
                  pnl REAL, market TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS capital_history
                 (id INTEGER PRIMARY KEY, timestamp TEXT, capital REAL, positions_count INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs
                 (id INTEGER PRIMARY KEY, timestamp TEXT, message TEXT)''')
    conn.commit()
    conn.close()

def save_trade(token, side, entry_price, shares, entry_time, exit_price=None, exit_time=None, pnl=0, market=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO trades VALUES (NULL,?,?,?,?,?,?,?,?,?)',
              (token, side, entry_price, shares, entry_time, exit_price, exit_time, pnl, market))
    conn.commit()
    conn.close()

def save_capital_history(capital_val, pos_count):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    ts = datetime.now(timezone.utc).isoformat()
    c.execute('INSERT INTO capital_history VALUES (NULL,?,?,?)', (ts, capital_val, pos_count))
    conn.commit()
    conn.close()

def save_log(msg):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    ts = datetime.now(timezone.utc).isoformat()
    c.execute('INSERT INTO logs VALUES (NULL,?,?)', (ts, msg))
    conn.commit()
    conn.close()

def get_trades():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM trades ORDER BY entry_time DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()
    return rows

def get_capital_history():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT timestamp, capital FROM capital_history ORDER BY timestamp DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

# =====================
# LOGGING
# =====================
def log(msg: str):
    global log_messages
    ts = datetime.now(timezone.utc).isoformat()
    full_msg = f"[{ts}] {msg}"
    print(full_msg)
    log_messages.append({"time": ts, "message": msg})
    if len(log_messages) > 500:
        log_messages.pop(0)
    save_log(msg)

def send_telegram(message: str):
    pass

# =====================
# MARKET FETCH
# =====================
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
            log(f"Error fetching markets: {e}")
            break
    return out

# =====================
# TOKEN HELPERS
# =====================
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

# =====================
# PRICE CACHE
# =====================
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

# =====================
# ENTRY LOGIC
# =====================
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

# =====================
# EXECUTION
# =====================
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
    save_trade(token, side, price, shares, pos["entry_time"], market=pos["market"])
    msg = f"BUY {side} {market.get('question', 'Unknown')[:45]} @ {price:.3f}"
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
        closed_trades.append({"side": side, "pnl": pnl, "market": pos["market"]})
        del positions[token]
        log(f"EXIT YES TP @ {price:.3f} | PnL: ${pnl:.2f}")
        return
   
    if side == "NO" and price <= 0.02:
        proceeds = shares * price
        pnl = proceeds - (shares * entry)
        capital += proceeds
        closed_trades.append({"side": side, "pnl": pnl, "market": pos["market"]})
        del positions[token]
        log(f"EXIT NO TP @ {price:.3f} | PnL: ${pnl:.2f}")
        return
    
    if side == "YES":
        pos["high"] = max(pos["high"], price)
        if price < pos["high"] * (1 - TRAILING_SL_PCT):
            proceeds = shares * price
            pnl = proceeds - (shares * entry)
            capital += proceeds
            closed_trades.append({"side": side, "pnl": pnl, "market": pos["market"]})
            del positions[token]
            log(f"EXIT YES SL @ {price:.3f} | PnL: ${pnl:.2f}")
    else:
        pos["low"] = min(pos["low"], price)
        if price > pos["low"] * (1 + TRAILING_SL_PCT):
            proceeds = shares * price
            pnl = proceeds - (shares * entry)
            capital += proceeds
            closed_trades.append({"side": side, "pnl": pnl, "market": pos["market"]})
            del positions[token]
            log(f"EXIT NO SL @ {price:.3f} | PnL: ${pnl:.2f}")

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

# =====================
# BOT LOOP
# =====================
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
            
            save_capital_history(capital, len(positions))
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

# =====================
# FLASK ROUTES
# =====================
@app.route('/')
def dashboard():
    html = '''<!DOCTYPE html><html><head><title>Professional Trading Bot Dashboard</title><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',Roboto,sans-serif;background:#0f1419;color:#fff;padding:20px}html{scroll-behavior:smooth}.container{max-width:1400px;margin:0 auto}.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:40px;border-radius:15px;margin-bottom:30px;box-shadow:0 20px 60px rgba(0,0,0,0.3)}.header h1{font-size:32px;margin-bottom:20px;font-weight:700}.controls{display:flex;gap:10px;margin-bottom:20px}.btn{padding:12px 24px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:all 0.3s}.btn-start{background:#4CAF50;color:white}.btn-start:hover{background:#45a049;transform:translateY(-2px)}.btn-stop{background:#f44336;color:white}.btn-stop:hover{background:#da190b}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-top:20px}.stat-card{background:rgba(255,255,255,0.1);padding:25px;border-radius:12px;border-left:4px solid #667eea;backdrop-filter:blur(10px)}.stat-label{font-size:12px;opacity:0.8;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}.stat-value{font-size:36px;font-weight:700;margin-bottom:5px}.stat-subtext{font-size:12px;opacity:0.6}.status-badge{display:inline-block;padding:6px 12px;border-radius:20px;font-size:12px;font-weight:600}.status-running{background:#4CAF50;color:white}.status-stopped{background:#f44336;color:white}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(500px,1fr));gap:20px;margin-bottom:30px}.chart-card{background:rgba(255,255,255,0.05);padding:25px;border-radius:12px;border:1px solid rgba(255,255,255,0.1)}.chart-card h3{margin-bottom:20px;font-size:18px}.positions-table,.trades-table{background:rgba(255,255,255,0.05);border-radius:12px;padding:25px;margin-bottom:30px;border:1px solid rgba(255,255,255,0.1)}.positions-table h2,.trades-table h2{margin-bottom:20px;font-size:20px}table{width:100%;border-collapse:collapse}th{background:rgba(102,126,234,0.2);padding:15px;text-align:left;font-weight:600;border-bottom:2px solid #667eea}td{padding:15px;border-bottom:1px solid rgba(255,255,255,0.1)}tr:hover{background:rgba(102,126,234,0.1)}tbody tr:nth-child(even){background:rgba(255,255,255,0.02)}.pnl-positive{color:#4CAF50}.pnl-negative{color:#f44336}.logs{background:rgba(255,255,255,0.05);border-radius:12px;padding:25px;max-height:400px;overflow-y:auto;border:1px solid rgba(255,255,255,0.1)}.log-entry{padding:10px;margin:5px 0;background:rgba(102,126,234,0.1);border-left:3px solid #667eea;border-radius:4px;font-family:monospace;font-size:12px}.no-data{text-align:center;color:#888;padding:30px}</style></head><body><div class="container"><div class="header"><div style="display:flex;justify-content:space-between;align-items:center"><div><h1>📊 Professional Trading Bot</h1><p id="status" style="margin-top:10px"><span class="status-badge status-stopped">STOPPED</span></p></div><div class="controls"><button class="btn btn-start" onclick="startBot()">▶ Start Bot</button><button class="btn btn-stop" onclick="stopBot()">⏹ Stop Bot</button></div></div><div class="stats"><div class="stat-card"><div class="stat-label">Current Capital</div><div class="stat-value" id="capital">$0.00</div><div class="stat-subtext">Initial: <span id="initial">$20.00</span></div></div><div class="stat-card"><div class="stat-label">Total P&L</div><div class="stat-value" id="pnl">$0.00</div><div class="stat-subtext">ROI: <span id="roi">0.00%</span></div></div><div class="stat-card"><div class="stat-label">Open Positions</div><div class="stat-value" id="open-pos">0</div><div class="stat-subtext" id="max-pos">/ 5 Maximum</div></div><div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value" id="win-rate">0%</div><div class="stat-subtext" id="win-loss">0W / 0L</div></div></div></div><div class="charts"><div class="chart-card"><h3>📈 Capital Growth</h3><canvas id="capitalChart"></canvas></div><div class="chart-card"><h3>💰 P&L Distribution</h3><canvas id="pnlChart"></canvas></div></div><div class="positions-table"><h2>🔓 Open Positions</h2><table><thead><tr><th>Side</th><th>Market</th><th>Entry Price</th><th>Shares</th><th>Entry Time</th></tr></thead><tbody id="pos-tbody"><tr><td colspan="5" class="no-data">No open positions</td></tr></tbody></table></div><div class="trades-table"><h2>✅ Recent Closed Trades</h2><table><thead><tr><th>Side</th><th>Market</th><th>P&L</th><th>Status</th></tr></thead><tbody id="trades-tbody"><tr><td colspan="4" class="no-data">No closed trades yet</td></tr></tbody></table></div><div class="logs"><h3 style="margin-bottom:15px">📋 Live Logs</h3><div id="logs-container"></div></div></div><script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script><script>let capitalChart,pnlChart;function updateDashboard(){fetch('/api/status').then(r=>r.json()).then(data=>{document.getElementById('capital').textContent='$'+data.capital.toFixed(2);document.getElementById('initial').textContent='$'+data.initial_capital.toFixed(2);const pnl=data.capital-data.initial_capital;document.getElementById('pnl').textContent=(pnl>=0?'+':'')+'$'+pnl.toFixed(2);const roi=(pnl/data.initial_capital)*100;document.getElementById('roi').textContent=roi.toFixed(2)+'%';document.getElementById('open-pos').textContent=data.positions_count;let statusText=data.bot_running?'<span class="status-badge status-running">RUNNING</span>':'<span class="status-badge status-stopped">STOPPED</span>';document.getElementById('status').innerHTML=statusText;let tbody=document.getElementById('pos-tbody');if(data.positions.length===0){tbody.innerHTML='<tr><td colspan="5" class="no-data">No open positions</td></tr>'}else{tbody.innerHTML=data.positions.map(p=>`<tr><td><strong>${p.side}</strong></td><td>${p.market.substring(0,50)}...</td><td>${p.entry.toFixed(4)}</td><td>${p.shares.toFixed(2)}</td><td>${new Date(p.entry_time).toLocaleString()}</td></tr>`).join('')}let tradesBody=document.getElementById('trades-tbody');if(data.closed_trades.length===0){tradesBody.innerHTML='<tr><td colspan="4" class="no-data">No closed trades yet</td></tr>'}else{tradesBody.innerHTML=data.closed_trades.slice(-20).reverse().map(t=>`<tr><td><strong>${t.side}</strong></td><td>${t.market.substring(0,50)}...</td><td class="${t.pnl>=0?'pnl-positive':'pnl-negative'}">${t.pnl>=0?'+':''}$${t.pnl.toFixed(2)}</td><td>${t.pnl>=0?'✅ Win':'❌ Loss'}</td></tr>`).join('')}updateCharts(data.capital_history);let logContainer=document.getElementById('logs-container');logContainer.innerHTML=data.logs.slice(-15).reverse().map(l=>`<div class="log-entry"><strong>${l.time.substring(11,19)}</strong> ${l.message}</div>`).join('')})}.function updateCharts(capitalHistory){const labels=capitalHistory.map(d=>new Date(d[0]).toLocaleTimeString());const data=capitalHistory.map(d=>d[1]);const ctx1=document.getElementById('capitalChart').getContext('2d');if(capitalChart)capitalChart.destroy();capitalChart=new Chart(ctx1,{type:'line',data:{labels:labels,datasets:[{label:'Capital ($)',data:data,borderColor:'#667eea',backgroundColor:'rgba(102,126,234,0.1)',tension:0.4,fill:true}]},options:{responsive:true,plugins:{legend:{display:true,labels:{color:'#fff'}}},scales:{y:{ticks:{color:'#888'},grid:{color:'rgba(255,255,255,0.1)'}},x:{ticks:{color:'#888'},grid:{color:'rgba(255,255,255,0.1)'}}}}});const ctx2=document.getElementById('pnlChart').getContext('2d');if(pnlChart)pnlChart.destroy();pnlChart=new Chart(ctx2,{type:'doughnut',data:{labels:['Wins','Losses'],datasets:[{data:[50,50],backgroundColor:['#4CAF50','#f44336']}]},options:{responsive:true,plugins:{legend:{display:true,labels:{color:'#fff'}}}}})}function startBot(){fetch('/api/start',{method:'POST'}).then(r=>r.json()).then(d=>{updateDashboard()})}function stopBot(){fetch('/api/stop',{method:'POST'}).then(r=>r.json()).then(d=>{updateDashboard()})}updateDashboard();setInterval(updateDashboard,2000);</script></body></html>'''
    return render_template_string(html)

@app.route('/api/status')
def get_status():
    return jsonify({
        "capital": capital,
        "initial_capital": initial_capital,
        "positions_count": len(positions),
        "positions": list(positions.values()),
        "closed_trades": closed_trades[-50:],
        "error_count": error_count,
        "bot_running": bot_running,
        "logs": log_messages,
        "capital_history": get_capital_history()
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
    init_db()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
