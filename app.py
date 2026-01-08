import os
import json
import time
import requests
import csv
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
import threading
import random

load_dotenv()

SIMULATION = True
MODE = "BACKTEST"  # BACKTEST, LIVE_SIM, or LIVE
POLL_INTERVAL = 5  # Faster polling for backtest

# BACKTEST CONFIG
BACKTEST_DAYS = 180  # 6 months of data
BACKTEST_SPEED = 100  # Simulate 100x real time

# POSITION SIZING - SMALL RISK
RISK_PCT = 0.03
MAX_POSITION_SIZE_USD = 0.50
MIN_CAPITAL_BUY = 0.10

# ENTRY RANGES
MIN_YES_ENTRY = 0.40
MAX_YES_ENTRY = 0.75
MIN_NO_ENTRY = 0.25
MAX_NO_ENTRY = 0.60

# MOMENTUM
PRICE_MOMENTUM_PCT = 0.03  # 3% momentum needed
MOMENTUM_WINDOW = 5
LIQUIDITY_SURGE_THRESHOLD = 30

# PROFIT TARGETS - WIDER (10-15%)
YES_PROFIT_TARGET = 1.10  # 10% profit
NO_PROFIT_TARGET = 0.90   # 10% profit

# STOPS
HARD_STOP_LOSS_PCT = 0.05  # -5%
SMART_STOP_LOSS_PCT = 0.03  # -3%
MAX_AGE_POSITION = 600  # 10 minutes

MARKETS_PER_PAGE = 100
NUM_PAGES = 50
CLOBB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
TRADING_FEE_PCT = 0.02

capital = 20.0
start_capital = 20.0
positions = {}
closed_trades = []
price_history = {}
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

# BACKTEST VARIABLES
backtest_running = False
backtest_progress = 0
backtest_data = []
backtest_index = 0
backtest_current_time = None

app = Flask(__name__)
CORS(app)

def log(msg: str):
    global log_messages
    ts = datetime.now(timezone.utc).isoformat()
    full_msg = f"[{ts}] {msg}"
    print(full_msg)
    log_messages.append({"time": ts, "message": msg})
    if len(log_messages) > 1000:
        log_messages.pop(0)

def generate_synthetic_backtest_data() -> List[Dict]:
    """Generate synthetic market data for backtesting"""
    data = []
    current_time = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)
    
    for day in range(BACKTEST_DAYS):
        current_time = datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS - day)
        
        # Generate 20-40 markets per day
        num_markets = random.randint(20, 40)
        
        for m_id in range(num_markets):
            market_id = f"market_{day}_{m_id}"
            
            # Random price movement
            base_yes_price = random.uniform(0.40, 0.75)
            base_no_price = 1 - base_yes_price
            
            # Add some momentum
            momentum = random.choice([0.02, -0.02, 0, 0.03, -0.03, 0.01, -0.01])
            
            market = {
                "id": market_id,
                "question": f"Test Market {market_id}",
                "endDate": (current_time + timedelta(hours=random.randint(1, 23))).isoformat(),
                "clobTokenIds": json.dumps([f"token_yes_{market_id}", f"token_no_{market_id}"]),
                "outcomes": json.dumps(["YES", "NO"]),
                "outcomePrices": json.dumps([
                    round(max(0.01, min(0.99, base_yes_price + momentum)), 4),
                    round(max(0.01, min(0.99, base_no_price - momentum)), 4)
                ]),
                "liquidityNum": random.randint(50, 500),
                "timestamp": current_time.isoformat()
            }
            data.append(market)
    
    log(f"Generated {len(data)} synthetic backtest markets across {BACKTEST_DAYS} days")
    return data

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
    global prev_yes_prices, prev_no_prices, prev_liq, last_yes_prices, last_no_prices, last_liq
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
            if mid not in price_history:
                price_history[mid] = {"yes": [], "no": []}
            price_history[mid]["yes"].append(y)
            if len(price_history[mid]["yes"]) > MOMENTUM_WINDOW:
                price_history[mid]["yes"].pop(0)
        
        if n is not None:
            last_no_prices[mid] = n
            if mid not in price_history:
                price_history[mid] = {"yes": [], "no": []}
            price_history[mid]["no"].append(n)
            if len(price_history[mid]["no"]) > MOMENTUM_WINDOW:
                price_history[mid]["no"].pop(0)

def has_momentum(mid: str, side: str) -> bool:
    if mid not in price_history:
        return False
    
    history = price_history[mid][side.lower()]
    if len(history) < 2:
        return False
    
    oldest = history[0]
    newest = history[-1]
    pct_change = abs((newest - oldest) / oldest) if oldest != 0 else 0
    
    if side == "YES":
        return (newest - oldest) > 0 and pct_change >= PRICE_MOMENTUM_PCT
    else:
        return (oldest - newest) > 0 and pct_change >= PRICE_MOMENTUM_PCT

def expiry_ok(market: Dict) -> bool:
    try:
        end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
        remaining = (end - backtest_current_time).total_seconds() if backtest_current_time else (end - datetime.now(timezone.utc)).total_seconds()
        return remaining >= 60
    except:
        return False

def should_buy_yes(market: Dict) -> bool:
    mid = market.get("id")
    _, price = extract_token(market, "YES")
    
    if price is None or price < MIN_YES_ENTRY or price > MAX_YES_ENTRY:
        return False
    if not expiry_ok(market):
        return False
    if not has_momentum(mid, "YES"):
        return False
    
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    return liq_jump >= LIQUIDITY_SURGE_THRESHOLD or has_momentum(mid, "YES")

def should_buy_no(market: Dict) -> bool:
    mid = market.get("id")
    _, price = extract_token(market, "NO")
    
    if price is None or price < MIN_NO_ENTRY or price > MAX_NO_ENTRY:
        return False
    if not expiry_ok(market):
        return False
    if not has_momentum(mid, "NO"):
        return False
    
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    return liq_jump >= LIQUIDITY_SURGE_THRESHOLD or has_momentum(mid, "NO")

def buy(token: str, price: float, market: Dict, side: str):
    global capital
    
    stake = min(capital * RISK_PCT, MAX_POSITION_SIZE_USD)
    if stake < MIN_CAPITAL_BUY or capital < stake:
        return False
    
    shares = stake / price
    entry_fee = stake * TRADING_FEE_PCT
    total_cost = stake + entry_fee
    
    if total_cost > capital:
        return False
    
    capital -= total_cost
    pos = {
        "entry": price,
        "shares": shares,
        "side": side,
        "market": market.get("question", "Unknown"),
        "market_id": market.get("id"),
        "entry_time": backtest_current_time if backtest_current_time else datetime.now(timezone.utc),
        "entry_cost": stake,
        "entry_fee": entry_fee,
        "high": price,
        "low": price,
    }
    positions[token] = pos
    return True

def check_exit(token: str, pos: Dict, markets_dict: Dict):
    global capital
    
    market_id = pos.get("market_id")
    price = None
    
    if market_id and market_id in markets_dict:
        market = markets_dict[market_id]
        _, price = extract_token(market, pos["side"])
    
    if price is None:
        return
    
    entry = pos["entry"]
    shares = pos["shares"]
    side = pos.get("side", "YES")
    entry_cost = pos.get("entry_cost", shares * entry)
    entry_fee = pos.get("entry_fee", 0)
    total_invested = entry_cost + entry_fee
    
    entry_time = pos["entry_time"]
    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    
    position_age = (backtest_current_time - entry_time).total_seconds() if backtest_current_time else (datetime.now(timezone.utc) - entry_time).total_seconds()
    
    exit_price = None
    exit_reason = None
    
    # PROFIT TARGET
    if side == "YES" and price >= entry * YES_PROFIT_TARGET:
        exit_price = price
        exit_reason = "PROFIT_TARGET"
    elif side == "NO" and price <= entry * NO_PROFIT_TARGET:
        exit_price = price
        exit_reason = "PROFIT_TARGET"
    
    # HARD STOP LOSS
    elif side == "YES" and price <= entry * (1 - HARD_STOP_LOSS_PCT):
        exit_price = price
        exit_reason = "HARD_SL"
    elif side == "NO" and price >= entry * (1 + HARD_STOP_LOSS_PCT):
        exit_price = price
        exit_reason = "HARD_SL"
    
    # SMART STOP LOSS
    elif side == "YES" and price <= entry * (1 - SMART_STOP_LOSS_PCT):
        if not has_momentum(market_id, "YES"):
            exit_price = price
            exit_reason = "SMART_SL"
    elif side == "NO" and price >= entry * (1 + SMART_STOP_LOSS_PCT):
        if not has_momentum(market_id, "NO"):
            exit_price = price
            exit_reason = "SMART_SL"
    
    # TIME DECAY
    elif position_age > MAX_AGE_POSITION:
        exit_price = price
        exit_reason = "TIME_DECAY"
    
    if exit_price is not None:
        proceeds = shares * exit_price
        exit_fee = proceeds * TRADING_FEE_PCT
        net_proceeds = proceeds - exit_fee
        
        gross_pnl = proceeds - entry_cost
        net_pnl = net_proceeds - total_invested
        pnl_pct = (net_pnl / total_invested) * 100 if total_invested > 0 else 0
        
        capital += net_proceeds
        
        closed_trades.append({
            "market": pos["market"],
            "side": side,
            "entry_price": entry,
            "exit_price": exit_price,
            "shares": shares,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "position_age": position_age,
        })
        
        del positions[token]

def backtest_loop():
    global backtest_running, backtest_progress, backtest_index, backtest_current_time, capital, positions, closed_trades, price_history
    
    log(f"BACKTEST START | Days: {BACKTEST_DAYS} | Start Capital: ${start_capital:.2f}")
    backtest_running = True
    capital = start_capital
    positions = {}
    closed_trades = []
    price_history = {}
    
    backtest_data = generate_synthetic_backtest_data()
    backtest_data.sort(key=lambda x: x.get("timestamp", ""))
    
    for idx, market_batch in enumerate(backtest_data):
        if not backtest_running:
            break
        
        # Group by timestamp for realistic scanning
        current_batch = [m for m in backtest_data if m.get("timestamp") == market_batch.get("timestamp")]
        
        backtest_current_time = datetime.fromisoformat(market_batch.get("timestamp"))
        backtest_index = idx
        backtest_progress = int((idx / len(backtest_data)) * 100)
        
        markets_dict = {m.get("id"): m for m in current_batch if m.get("id")}
        update_price_cache(current_batch)
        
        # Exit first
        for t, p in list(positions.items()):
            check_exit(t, p, markets_dict)
        
        # New entries
        for m in current_batch:
            if len(positions) >= 20:
                break
            
            mid = m.get("id")
            if not mid:
                continue
            
            ytok, yprice = extract_token(m, "YES")
            ntok, nprice = extract_token(m, "NO")
            
            if ytok and ytok not in positions and should_buy_yes(m):
                buy(ytok, yprice, m, "YES")
            
            if ntok and ntok not in positions and should_buy_no(m):
                buy(ntok, nprice, m, "NO")
        
        # Log progress every 100 batches
        if idx % 100 == 0:
            if closed_trades:
                total_pnl = sum(t.get("net_pnl", 0) for t in closed_trades)
                win_rate = len([t for t in closed_trades if t.get("net_pnl", 0) > 0]) / len(closed_trades) * 100
                gains = sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) > 0)
                losses = abs(sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) < 0))
                pf = gains / losses if losses > 0 else 0
                log(f"Progress {backtest_progress}% | Trades: {len(closed_trades)} | PnL: ${total_pnl:.2f} | WR: {win_rate:.1f}% | PF: {pf:.2f}x | Capital: ${capital:.2f}")
        
        time.sleep(0.01)  # Prevent blocking
    
    # Final summary
    if closed_trades:
        total_pnl = sum(t.get("net_pnl", 0) for t in closed_trades)
        win_rate = len([t for t in closed_trades if t.get("net_pnl", 0) > 0]) / len(closed_trades) * 100
        gains = sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) > 0)
        losses = abs(sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) < 0))
        pf = gains / losses if losses > 0 else 0
        roi = ((capital - start_capital) / start_capital) * 100
        
        log(f"BACKTEST COMPLETE")
        log(f"Total Trades: {len(closed_trades)} | Total PnL: ${total_pnl:.2f} | ROI: {roi:.2f}%")
        log(f"Win Rate: {win_rate:.1f}% | Profit Factor: {pf:.2f}x")
        log(f"Final Capital: ${capital:.2f} | Avg Trade: ${total_pnl/len(closed_trades):.3f}")
        
        if pf >= 1.5:
            log("✓ PASSED - Profit Factor >= 1.5x. Ready to paper trade!")
        else:
            log(f"✗ FAILED - Profit Factor {pf:.2f}x < 1.5x. Need strategy improvements.")
    
    backtest_running = False

@app.route('/')
def dashboard():
    html = '''<!DOCTYPE html><html><head><title>Trading Bot Backtest</title><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>* { margin: 0; padding: 0; box-sizing: border-box; } body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; } .container { max-width: 1400px; margin: 0 auto; } .header { background: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } h1 { color: #333; margin-bottom: 10px; } .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-top: 20px; } .stat-box { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 15px; border-radius: 8px; text-align: center; } .stat-label { font-size: 11px; opacity: 0.9; margin-bottom: 8px; text-transform: uppercase; } .stat-value { font-size: 24px; font-weight: bold; } .section { background: white; border-radius: 10px; padding: 25px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } h2 { color: #333; margin-bottom: 15px; border-bottom: 2px solid #667eea; padding-bottom: 10px; } table { width: 100%; border-collapse: collapse; } th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; font-size: 12px; } th { background: #f5f5f5; font-weight: 600; } .log-entry { padding: 8px; margin: 4px 0; background: #f9f9f9; border-left: 3px solid #667eea; font-family: monospace; font-size: 11px; } .btn { background: #667eea; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; } .btn:hover { background: #764ba2; } .btn:disabled { background: #ccc; cursor: not-allowed; } .progress-bar { width: 100%; height: 30px; background: #eee; border-radius: 5px; overflow: hidden; margin: 15px 0; } .progress-fill { height: 100%; background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); width: 0%; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 12px; } .pnl-positive { color: #2ecc71; font-weight: bold; } .pnl-negative { color: #e74c3c; font-weight: bold; }</style></head><body><div class="container"><div class="header"><div><h1>📊 Trading Bot Backtest Engine</h1><p style="color: #666; margin-top: 10px;">6-Month Backtest with 10-15% Profit Targets</p></div><div style="margin-top: 15px;"><button class="btn" id="startBtn" onclick="startBacktest()">Start Backtest</button><button class="btn" id="stopBtn" onclick="stopBacktest()" style="display:none;">Stop Backtest</button></div><div class="progress-bar"><div class="progress-fill" id="progress" style="width: 0%;">0%</div></div><div class="stats"><div class="stat-box"><div class="stat-label">Progress</div><div class="stat-value" id="progress-text">0%</div></div><div class="stat-box"><div class="stat-label">Closed Trades</div><div class="stat-value" id="closed-count">0</div></div><div class="stat-box"><div class="stat-label">Total PnL</div><div class="stat-value" id="total-pnl">$0.00</div></div><div class="stat-box"><div class="stat-label">Win Rate</div><div class="stat-value" id="win-rate">0%</div></div><div class="stat-box"><div class="stat-label">Profit Factor</div><div class="stat-value" id="profit-factor">0x</div></div><div class="stat-box"><div class="stat-label">Current Capital</div><div class="stat-value" id="capital">$20.00</div></div></div></div><div class="section"><h2>Closed Trades (Last 20)</h2><table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>Age</th></tr></thead><tbody id="trades-tbody"><tr><td colspan="8" style="text-align: center; color: #999;">No trades yet</td></tr></tbody></table></div><div class="section"><h2>Backtest Logs</h2><div id="logs-container" style="max-height: 600px; overflow-y: auto;"></div></div></div><script>let isRunning = false; function startBacktest() { if (isRunning) return; isRunning = true; document.getElementById('startBtn').disabled = true; document.getElementById('startBtn').style.display = 'none'; document.getElementById('stopBtn').style.display = 'inline-block'; fetch('/api/backtest/start', {method: 'POST'}).then(() => updateBacktest()); } function stopBacktest() { isRunning = false; fetch('/api/backtest/stop', {method: 'POST'}); document.getElementById('startBtn').disabled = false; document.getElementById('startBtn').style.display = 'inline-block'; document.getElementById('stopBtn').style.display = 'none'; } function updateBacktest() { fetch('/api/backtest/status').then(r => r.json()).then(data => { document.getElementById('progress').style.width = data.progress + '%'; document.getElementById('progress-text').textContent = data.progress + '%'; document.getElementById('closed-count').textContent = data.closed_trades_count; document.getElementById('capital').textContent = '$' + data.capital.toFixed(2); let totalPnl = data.total_pnl || 0; document.getElementById('total-pnl').textContent = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2); document.getElementById('win-rate').textContent = data.win_rate.toFixed(1) + '%'; document.getElementById('profit-factor').textContent = (data.profit_factor || 0).toFixed(2) + 'x'; let tradesHtml = ''; if (data.closed_trades && data.closed_trades.length > 0) { tradesHtml = data.closed_trades.slice(-20).reverse().map(t => { let pnlClass = t.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'; let ageMin = Math.round(t.position_age / 60); return `<tr><td>${t.market.substring(0, 25)}</td><td><strong>${t.side}</strong></td><td>${t.entry_price.toFixed(4)}</td><td>${t.exit_price.toFixed(4)}</td><td class="${pnlClass}">${t.net_pnl >= 0 ? '+' : ''}$${t.net_pnl.toFixed(3)}</td><td class="${pnlClass}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%</td><td>${t.exit_reason}</td><td>${ageMin}m</td></tr>`; }).join(''); } document.getElementById('trades-tbody').innerHTML = tradesHtml || '<tr><td colspan="8" style="text-align: center; color: #999;">No trades yet</td></tr>'; let logContainer = document.getElementById('logs-container'); logContainer.innerHTML = data.logs.slice(-50).reverse().map(l => `<div class="log-entry"><strong>${l.time.substring(11, 19)}</strong> ${l.message}</div>`).join(''); logContainer.scrollTop = 0; if (isRunning) setTimeout(updateBacktest, 500); }); } setInterval(() => { if (isRunning) updateBacktest(); }, 500);</script></body></html>'''
    return render_template_string(html)

@app.route('/api/backtest/start', methods=['POST'])
def start_backtest():
    global backtest_running
    if not backtest_running:
        threading.Thread(target=backtest_loop, daemon=True).start()
    return jsonify({"status": "Backtest started"})

@app.route('/api/backtest/stop', methods=['POST'])
def stop_backtest():
    global backtest_running
    backtest_running = False
    return jsonify({"status": "Backtest stopped"})

@app.route('/api/backtest/status')
def backtest_status():
    total_pnl = sum(t.get("net_pnl", 0) for t in closed_trades)
    
    if closed_trades:
        win_rate = len([t for t in closed_trades if t.get("net_pnl", 0) > 0]) / len(closed_trades) * 100
        gains = sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) > 0)
        losses = abs(sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) < 0))
        profit_factor = gains / losses if losses > 0 else gains
    else:
        win_rate = 0
        profit_factor = 0
    
    return jsonify({
        "progress": backtest_progress,
        "closed_trades_count": len(closed_trades),
        "closed_trades": closed_trades,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "capital": capital,
        "backtest_running": backtest_running,
        "logs": log_messages
    })

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
