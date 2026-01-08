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
MAX_OPEN_TRADES = 20
START_CAPITAL = float(os.getenv("START_CAPITAL", 20.0))

# AGGRESSIVE POSITION SIZING - SMALL RISK PER TRADE
RISK_PCT = 0.03  # Only 3% per trade (was 15%)
MAX_POSITION_SIZE_USD = 0.50  # Small positions (was 3.0)
MIN_CAPITAL_BUY = 0.10  # Very small minimum (was 2.0)

# REALISTIC PROFIT TARGETS - 2-8% expected moves
MIN_YES_ENTRY = 0.45  # Catch momentum, not extreme prices
MAX_YES_ENTRY = 0.75  # Don't chase 0.90+
MIN_NO_ENTRY = 0.25
MAX_NO_ENTRY = 0.55

# MOMENTUM DETECTION - catch moves early
PRICE_MOMENTUM_PCT = 0.02  # 2% price move (was 0.01 absolute)
MOMENTUM_WINDOW = 3  # Check last 3 scans for momentum
LIQUIDITY_SURGE_THRESHOLD = 30

# PROFIT TARGETS - SMALL BUT CONSISTENT
YES_PROFIT_TARGET = 1.05  # Just 5% profit (was 0.98 = 28% move)
NO_PROFIT_TARGET = 0.95   # Just 5% profit (was 0.02 = 98% move)

# STOP LOSSES - TIGHT AND IMMEDIATE
HARD_STOP_LOSS_PCT = 0.03  # Exit immediately if -3% (was 5% trailing)
SMART_STOP_LOSS_PCT = 0.02  # Exit if -2% with no momentum

# EXPIRY AND TIMING
MIN_EXPIRY_BUFFER = 60  # At least 1 minute (was 120-300)
MAX_AGE_POSITION = 300  # Exit after 5 mins if no profit (was unlimited)

MARKETS_PER_PAGE = 100
NUM_PAGES = 50
CLOBB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
TRADING_FEE_PCT = 0.02  # 2% fee

capital = START_CAPITAL
positions = {}
closed_trades = []
price_history = {}  # Track price history for momentum
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
            "liquidity_num_min": 20,
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
            # Track price history
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
    """Check if price is moving in favorable direction"""
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
    else:  # NO
        return (oldest - newest) > 0 and pct_change >= PRICE_MOMENTUM_PCT

def expiry_ok(market: Dict) -> bool:
    try:
        end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
        remaining = (end - datetime.now(timezone.utc)).total_seconds()
        return remaining >= MIN_EXPIRY_BUFFER
    except:
        return False

def should_buy_yes(market: Dict) -> bool:
    """Buy YES when it's in middle range with upward momentum"""
    mid = market.get("id")
    _, price = extract_token(market, "YES")
    
    if price is None:
        return False
    if price < MIN_YES_ENTRY or price > MAX_YES_ENTRY:
        return False
    if not expiry_ok(market):
        return False
    
    # Check for upward momentum
    if not has_momentum(mid, "YES"):
        return False
    
    # Also consider liquidity jumps as entry signal
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    return liq_jump >= LIQUIDITY_SURGE_THRESHOLD or has_momentum(mid, "YES")

def should_buy_no(market: Dict) -> bool:
    """Buy NO when it's in middle range with downward momentum"""
    mid = market.get("id")
    _, price = extract_token(market, "NO")
    
    if price is None:
        return False
    if price < MIN_NO_ENTRY or price > MAX_NO_ENTRY:
        return False
    if not expiry_ok(market):
        return False
    
    # Check for downward momentum
    if not has_momentum(mid, "NO"):
        return False
    
    # Also consider liquidity jumps as entry signal
    liq_jump = last_liq.get(mid, 0) - prev_liq.get(mid, 0)
    return liq_jump >= LIQUIDITY_SURGE_THRESHOLD or has_momentum(mid, "NO")

def buy(token: str, price: float, market: Dict, side: str):
    global capital
    
    stake = min(capital * RISK_PCT, MAX_POSITION_SIZE_USD)
    if stake < MIN_CAPITAL_BUY or capital < stake:
        return
    
    shares = stake / price
    entry_fee = stake * TRADING_FEE_PCT
    total_cost = stake + entry_fee
    
    if total_cost > capital:
        return
    
    capital -= total_cost
    pos = {
        "entry": price,
        "shares": shares,
        "side": side,
        "market": market.get("question", "Unknown"),
        "market_id": market.get("id"),
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "entry_cost": stake,
        "entry_fee": entry_fee,
        "high": price,
        "low": price,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    positions[token] = pos
    msg = f"BUY {side} {market.get('question', 'Unknown')[:40]} @ {price:.4f} | Stake: ${stake:.2f} | Capital: ${capital:.2f}"
    log(msg)

def check_exit(token: str, pos: Dict, markets_dict: Dict):
    """Check and execute exit with smart stop losses"""
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
    
    entry_time = datetime.fromisoformat(pos["entry_time"])
    position_age = (datetime.now(timezone.utc) - entry_time).total_seconds()
    
    exit_price = None
    exit_reason = None
    
    # PROFIT TARGET - Small but consistent
    if side == "YES" and price >= entry * YES_PROFIT_TARGET:
        exit_price = price
        exit_reason = "PROFIT_TARGET"
    elif side == "NO" and price <= entry * NO_PROFIT_TARGET:
        exit_price = price
        exit_reason = "PROFIT_TARGET"
    
    # HARD STOP LOSS - Immediate exit if -3%
    elif side == "YES" and price <= entry * (1 - HARD_STOP_LOSS_PCT):
        exit_price = price
        exit_reason = "HARD_SL"
    elif side == "NO" and price >= entry * (1 + HARD_STOP_LOSS_PCT):
        exit_price = price
        exit_reason = "HARD_SL"
    
    # SMART STOP LOSS - Exit if -2% AND no momentum
    elif side == "YES" and price <= entry * (1 - SMART_STOP_LOSS_PCT):
        if not has_momentum(market_id, "YES"):
            exit_price = price
            exit_reason = "SMART_SL_NO_MOMENTUM"
    elif side == "NO" and price >= entry * (1 + SMART_STOP_LOSS_PCT):
        if not has_momentum(market_id, "NO"):
            exit_price = price
            exit_reason = "SMART_SL_NO_MOMENTUM"
    
    # TIME DECAY - Exit after 5 mins regardless if underwater
    elif position_age > MAX_AGE_POSITION:
        exit_price = price
        exit_reason = "TIME_DECAY"
    
    # EXECUTE EXIT
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
            "exit_time": datetime.now(timezone.utc).isoformat(),
        })
        
        del positions[token]
        
        msg = f"EXIT {side} ({exit_reason}) @ {exit_price:.4f} | Net PnL: ${net_pnl:.3f} ({pnl_pct:.2f}%) | Capital: ${capital:.2f}"
        log(msg)

def bot_loop():
    global capital, error_count, bot_running
    log(f"BOT STARTED | Capital ${capital:.2f} | Risk per trade: {RISK_PCT*100}% | Target: {(YES_PROFIT_TARGET-1)*100:.1f}%")
    bot_running = True
    
    while bot_running:
        try:
            markets = fetch_markets()
            
            markets_dict = {m.get("id"): m for m in markets if m.get("id")}
            update_price_cache(markets)
            
            # EXIT FIRST
            for t, p in list(positions.items()):
                check_exit(t, p, markets_dict)
            
            # NEW ENTRIES
            for m in markets:
                if len(positions) >= MAX_OPEN_TRADES:
                    break
                
                mid = m.get("id")
                if not mid:
                    continue
                
                ytok, yprice = extract_token(m, "YES")
                ntok, nprice = extract_token(m, "NO")
                
                if ytok and ytok not in positions and should_buy_yes(m):
                    buy(ytok, yprice, m, "YES")
                    continue
                
                if ntok and ntok not in positions and should_buy_no(m):
                    buy(ntok, nprice, m, "NO")
            
            # STATS
            if closed_trades:
                total_pnl = sum(t.get("net_pnl", 0) for t in closed_trades)
                winners = len([t for t in closed_trades if t.get("net_pnl", 0) > 0])
                win_rate = (winners / len(closed_trades)) * 100
                avg_win = sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) > 0) / winners if winners > 0 else 0
                avg_loss = sum(t.get("net_pnl", 0) for t in closed_trades if t.get("net_pnl", 0) <= 0) / (len(closed_trades) - winners) if (len(closed_trades) - winners) > 0 else 0
                
                log(f"SCAN | Open: {len(positions)}/{MAX_OPEN_TRADES} | Closed: {len(closed_trades)} | PnL: ${total_pnl:.2f} | WR: {win_rate:.1f}% | Avg W: ${avg_win:.3f} | Avg L: ${avg_loss:.3f} | Capital: ${capital:.2f}")
            else:
                log(f"SCAN | Open: {len(positions)}/{MAX_OPEN_TRADES} | Capital: ${capital:.2f}")
            
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
    html = '''<!DOCTYPE html><html><head><title>Trading Bot Dashboard</title><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>* { margin: 0; padding: 0; box-sizing: border-box; } body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; } .container { max-width: 1400px; margin: 0 auto; } .header { background: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } .header h1 { color: #333; margin-bottom: 10px; } .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-top: 20px; } .stat-box { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 15px; border-radius: 8px; text-align: center; } .stat-label { font-size: 11px; opacity: 0.9; margin-bottom: 8px; text-transform: uppercase; } .stat-value { font-size: 24px; font-weight: bold; } .positions, .logs, .trades { background: white; border-radius: 10px; padding: 25px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); } h2 { color: #333; margin-bottom: 15px; border-bottom: 2px solid #667eea; padding-bottom: 10px; } table { width: 100%; border-collapse: collapse; } th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; font-size: 12px; } th { background: #f5f5f5; font-weight: 600; } .log-entry { padding: 8px; margin: 4px 0; background: #f9f9f9; border-left: 3px solid #667eea; font-family: monospace; font-size: 11px; } .control-btn { background: #667eea; color: white; padding: 8px 16px; border: none; border-radius: 5px; cursor: pointer; font-size: 13px; margin-right: 8px; } .control-btn:hover { background: #764ba2; } .control-btn.stop { background: #e74c3c; } .control-btn.stop:hover { background: #c0392b; } .status { display: inline-block; padding: 4px 8px; border-radius: 20px; font-size: 11px; font-weight: bold; } .status.running { background: #2ecc71; color: white; } .status.stopped { background: #e74c3c; color: white; } .pnl-positive { color: #2ecc71; font-weight: bold; } .pnl-negative { color: #e74c3c; font-weight: bold; }</style></head><body><div class="container"><div class="header"><div style="display: flex; justify-content: space-between; align-items: center;"><div><h1>🤖 Trading Bot v2 (Low Risk Strategy)</h1><p id="status" style="color: #666; margin-top: 5px;"><span class="status stopped">STOPPED</span></p></div><div><button class="control-btn" onclick="startBot()">Start</button><button class="control-btn stop" onclick="stopBot()">Stop</button></div></div><div class="stats"><div class="stat-box"><div class="stat-label">Capital</div><div class="stat-value" id="capital">$0.00</div></div><div class="stat-box"><div class="stat-label">Open</div><div class="stat-value" id="open-pos">0</div></div><div class="stat-box"><div class="stat-label">Closed</div><div class="stat-value" id="closed-trades">0</div></div><div class="stat-box"><div class="stat-label">Total PnL</div><div class="stat-value" id="total-pnl">$0.00</div></div><div class="stat-box"><div class="stat-label">Win Rate</div><div class="stat-value" id="win-rate">0%</div></div><div class="stat-box"><div class="stat-label">Profit Factor</div><div class="stat-value" id="profit-factor">0x</div></div></div></div><div class="positions"><h2>Open Positions</h2><table><thead><tr><th>Side</th><th>Market</th><th>Entry</th><th>Shares</th><th>Entry Time</th></tr></thead><tbody id="pos-tbody"><tr><td colspan="5" style="text-align: center; color: #999;">No open positions</td></tr></tbody></table></div><div class="trades"><h2>Recent Closed Trades</h2><table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>Age</th></tr></thead><tbody id="trades-tbody"><tr><td colspan="8" style="text-align: center; color: #999;">No closed trades</td></tr></tbody></table></div><div class="logs"><h2>Logs</h2><div id="logs-container" style="max-height: 500px; overflow-y: auto;"></div></div></div><script>function updateDashboard() { fetch('/api/status').then(r => r.json()).then(data => { document.getElementById('capital').textContent = '$' + data.capital.toFixed(2); document.getElementById('open-pos').textContent = data.positions_count; document.getElementById('closed-trades').textContent = data.closed_trades_count; let totalPnl = data.total_pnl || 0; document.getElementById('total-pnl').textContent = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2); document.getElementById('win-rate').textContent = data.win_rate.toFixed(1) + '%'; document.getElementById('profit-factor').textContent = (data.profit_factor || 0).toFixed(2) + 'x'; let statusText = data.bot_running ? '<span class="status running">RUNNING</span>' : '<span class="status stopped">STOPPED</span>'; document.getElementById('status').innerHTML = statusText; let tbody = document.getElementById('pos-tbody'); if (!data.positions || data.positions.length === 0) { tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #999;">No open positions</td></tr>'; } else { tbody.innerHTML = data.positions.map(p => `<tr><td><strong>${p.side}</strong></td><td>${p.market.substring(0, 35)}</td><td>${p.entry.toFixed(4)}</td><td>${p.shares.toFixed(1)}</td><td>${new Date(p.entry_time).toLocaleTimeString()}</td></tr>`).join(''); } let tradesHtml = ''; if (data.closed_trades && data.closed_trades.length > 0) { tradesHtml = data.closed_trades.slice(-15).reverse().map(t => { let pnlClass = t.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'; let ageMin = Math.round(t.position_age / 60); return `<tr><td>${t.market.substring(0, 25)}</td><td><strong>${t.side}</strong></td><td>${t.entry_price.toFixed(4)}</td><td>${t.exit_price.toFixed(4)}</td><td class="${pnlClass}">${t.net_pnl >= 0 ? '+' : ''}$${t.net_pnl.toFixed(3)}</td><td class="${pnlClass}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%</td><td>${t.exit_reason}</td><td>${ageMin}m</td></tr>`; }).join(''); } let tradesBody = document.getElementById('trades-tbody'); tradesBody.innerHTML = tradesHtml || '<tr><td colspan="8" style="text-align: center; color: #999;">No closed trades</td></tr>'; let logContainer = document.getElementById('logs-container'); logContainer.innerHTML = data.logs.slice(-30).reverse().map(l => `<div class="log-entry"><strong>${l.time.substring(11, 19)}</strong> ${l.message}</div>`).join(''); }); } function startBot() { fetch('/api/start', {method: 'POST'}).then(() => updateDashboard()); } function stopBot() { fetch('/api/stop', {method: 'POST'}).then(() => updateDashboard()); } updateDashboard(); setInterval(updateDashboard, 2000);</script></body></html>'''
    return render_template_string(html)

@app.route('/api/status')
def get_status():
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
        "capital": capital,
        "positions_count": len(positions),
        "positions": list(positions.values()),
        "closed_trades_count": len(closed_trades),
        "closed_trades": closed_trades,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
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
