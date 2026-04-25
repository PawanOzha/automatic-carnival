"""
kucoin-bot v4 — micro-grid mean reversion
Goal : grow spot wallet via many small repeatable wins
Strategy: RSI + Bollinger entry with staggered grid buys
Exits  : fee-aware TP limits, RSI profit exits, trailing stop, hard SL
"""

import os, time, json, uuid, hmac, base64, hashlib, math
from decimal import Decimal, ROUND_DOWN
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL           = "https://api.kucoin.com"
API_KEY            = os.getenv("KUCOIN_API_KEY", "")
API_SECRET         = os.getenv("KUCOIN_API_SECRET", "")
API_PASSPHRASE     = os.getenv("KUCOIN_API_PASSPHRASE", "")

SYMBOL             = os.getenv("SYMBOL", "KCS-USDT").upper()
BASE_COIN, QUOTE   = SYMBOL.split("-")

TARGET_USDT        = float(os.getenv("TARGET_USDT",       "5.0"))
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL",      "20"))
MIN_USDT_TO_TRADE  = float(os.getenv("MIN_USDT_TO_TRADE", "1.0"))
MAX_USDT_PER_TRADE = float(os.getenv("MAX_USDT_PER_TRADE","3.5"))
POSITION_PCT       = float(os.getenv("POSITION_PCT",      "0.20"))

TAKE_PROFIT_PCT    = float(os.getenv("TAKE_PROFIT_PCT",   "0.008"))   # +0.8 %
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT",     "0.015"))   # -1.5 %
TRAIL_ACTIVATE     = float(os.getenv("TRAIL_ACTIVATE",    "0.015"))   # start at +1.5 %
TRAIL_DISTANCE     = float(os.getenv("TRAIL_DISTANCE",    "0.008"))   # 0.8 % below peak

MAX_DAILY_LOSSES   = int(os.getenv("MAX_DAILY_LOSSES",    "2"))
MIN_WALLET_USDT    = float(os.getenv("MIN_WALLET_USDT",   "2.0"))
LIVE_TRADING       = os.getenv("LIVE_TRADING", "false").lower() == "true"
BUY_TIMEOUT_SEC    = int(os.getenv("BUY_TIMEOUT_SEC",     "120"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC",        "300"))

RSI_PERIOD         = int(os.getenv("RSI_PERIOD",          "14"))
RSI_OVERSOLD       = float(os.getenv("RSI_OVERSOLD",      "40"))
RSI_TAKE_PROFIT    = float(os.getenv("RSI_TAKE_PROFIT",   "60"))
BB_PERIOD          = int(os.getenv("BB_PERIOD",           "20"))
BB_STD             = float(os.getenv("BB_STD",            "2.0"))
CANDLE_TYPE        = os.getenv("CANDLE_TYPE",             "5min")
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT",    "0.004"))

GRID_LEVELS        = int(os.getenv("GRID_LEVELS",         "4"))
GRID_STEP_PCT      = float(os.getenv("GRID_STEP_PCT",     "0.002"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS",  "4"))
MAX_PENDING_BUYS   = int(os.getenv("MAX_PENDING_BUYS",    "4"))
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.02"))
MAX_ORDERS_PER_MIN = int(os.getenv("MAX_ORDERS_PER_MIN", "20"))
MIN_SECONDS_BETWEEN_ORDERS = float(os.getenv("MIN_SECONDS_BETWEEN_ORDERS", "0.35"))

MAKER_FEE          = 0.001
TAKER_FEE          = 0.001

STATE_FILE = "state.json"
TRADE_HISTORY_FILE = "trade_history.jsonl"
SESSION    = requests.Session()
SESSION.headers.update({"User-Agent": "kucoin-bot/3.0"})

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

# ── Decimal helpers ───────────────────────────────────────────────────────────
def q4(v): return str(Decimal(str(v)).quantize(Decimal("0.0001"),    rounding=ROUND_DOWN))
def q6(v): return str(Decimal(str(v)).quantize(Decimal("0.000001"),  rounding=ROUND_DOWN))
def q8(v): return str(Decimal(str(v)).quantize(Decimal("0.00000001"),rounding=ROUND_DOWN))

# ── State persistence ────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "in_position": False, "entry_price": None, "entry_size": None,
    "buy_order_id": None,
    "pending_buy_order_id": None, "pending_buy_price": None,
    "pending_buy_size": None, "pending_buy_ts": None,
    "tp_order_id": None, "sl_order_id": None,
    "peak_price": None, "trailing_active": False,
    "daily_losses": 0, "daily_loss_date": str(date.today()),
    "last_loss_ts": 0.0,
    "positions": [],
    "pending_buys": [],
    "next_position_id": 1,
    "equity_date": str(date.today()),
    "day_start_equity": None,
    "realized_pnl_today": 0.0,
    "fees_paid_today": 0.0,
    "wins_today": 0,
    "losses_today": 0,
    "killswitch": False,
    "killswitch_reason": None,
    "last_order_ts": 0.0,
    "order_timestamps": [],
}

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    if saved.get("daily_loss_date") != str(date.today()):
        saved["daily_losses"] = 0
        saved["daily_loss_date"] = str(date.today())
    if saved.get("equity_date") != str(date.today()):
        saved["equity_date"] = str(date.today())
        saved["day_start_equity"] = None
        saved["realized_pnl_today"] = 0.0
        saved["fees_paid_today"] = 0.0
        saved["wins_today"] = 0
        saved["losses_today"] = 0
        saved["killswitch"] = False
        saved["killswitch_reason"] = None

    state = {**DEFAULT_STATE, **saved}

    # migration: v3 single-position/pending fields -> v4 lists
    if not isinstance(state.get("positions"), list):
        state["positions"] = []
    if not isinstance(state.get("pending_buys"), list):
        state["pending_buys"] = []

    if state.get("in_position") and not state["positions"]:
        ep = float(state.get("entry_price") or 0)
        es = float(state.get("entry_size") or 0)
        if ep > 0 and es > 0:
            state["positions"].append({
                "id": int(state.get("next_position_id") or 1),
                "entry_price": ep,
                "entry_size": es,
                "buy_order_id": state.get("buy_order_id"),
                "tp_order_id": state.get("tp_order_id"),
                "peak_price": float(state.get("peak_price") or ep),
                "trailing_active": bool(state.get("trailing_active")),
            })
            state["next_position_id"] = int(state.get("next_position_id") or 1) + 1

    if state.get("pending_buy_order_id") and not state["pending_buys"]:
        state["pending_buys"].append({
            "order_id": state.get("pending_buy_order_id"),
            "price": float(state.get("pending_buy_price") or 0),
            "size": float(state.get("pending_buy_size") or 0),
            "ts": float(state.get("pending_buy_ts") or time.time()),
        })

    # keep legacy mirror fields consistent with first active position/pending
    mirror_legacy_fields(state)
    return state

def save_state(state: dict):
    mirror_legacy_fields(state)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_trade_event(event: dict):
    row = {"ts": int(time.time()), **event}
    with open(TRADE_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")

def reset_daily_risk_counters(state: dict, total_equity: float):
    state["daily_losses"] = 0
    state["daily_loss_date"] = str(date.today())
    state["equity_date"] = str(date.today())
    state["day_start_equity"] = float(total_equity)
    state["realized_pnl_today"] = 0.0
    state["fees_paid_today"] = 0.0
    state["wins_today"] = 0
    state["losses_today"] = 0
    state["killswitch"] = False
    state["killswitch_reason"] = None

def calc_estimated_pnl_quote(entry_price: float, exit_price: float, size: float, exit_fee: float):
    entry_notional = entry_price * size
    exit_notional = exit_price * size
    fees = (entry_notional * MAKER_FEE) + (exit_notional * exit_fee)
    pnl_quote = exit_notional - entry_notional - fees
    return pnl_quote, fees

def can_place_order(state: dict):
    now = time.time()
    last_order_ts = float(state.get("last_order_ts") or 0.0)
    if now - last_order_ts < MIN_SECONDS_BETWEEN_ORDERS:
        return False, f"order throttle {MIN_SECONDS_BETWEEN_ORDERS:.2f}s"

    stamps = [float(x) for x in (state.get("order_timestamps") or []) if now - float(x) <= 60]
    if len(stamps) >= MAX_ORDERS_PER_MIN:
        return False, f"order cap {MAX_ORDERS_PER_MIN}/min"
    return True, ""

def mark_order_placed(state: dict):
    now = time.time()
    stamps = [float(x) for x in (state.get("order_timestamps") or []) if now - float(x) <= 60]
    stamps.append(now)
    state["order_timestamps"] = stamps
    state["last_order_ts"] = now

# ── KuCoin auth ───────────────────────────────────────────────────────────────
def _sign(method: str, endpoint: str, body: str = "") -> dict:
    ts      = str(int(time.time() * 1000))
    payload = ts + method.upper() + endpoint + body
    sig = base64.b64encode(
        hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    pp = base64.b64encode(
        hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": API_KEY, "KC-API-SIGN": sig, "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": pp, "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }

def _private(method: str, endpoint: str, body_dict=None):
    body    = json.dumps(body_dict, separators=(",", ":")) if body_dict else ""
    headers = _sign(method, endpoint, body)
    r = SESSION.request(method.upper(), BASE_URL + endpoint,
                        headers=headers, data=body or None, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {data}")
    return data.get("data")

def _public(endpoint: str, params=None):
    r = SESSION.get(BASE_URL + endpoint, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin public error: {data}")
    return data.get("data")

# ── Market data ───────────────────────────────────────────────────────────────
def get_level1(symbol: str) -> dict:
    d = _public("/api/v1/market/orderbook/level1", {"symbol": symbol})
    return {"price": float(d["price"]),
            "bestBid": float(d["bestBid"]),
            "bestAsk": float(d["bestAsk"])}

def get_candles(symbol: str, ktype: str = "5min", limit: int = 100):
    """Returns (closes, volumes) oldest → newest."""
    rows = _public("/api/v1/market/candles", {"type": ktype, "symbol": symbol})
    rows = rows[:limit]
    closes  = [float(r[2]) for r in rows]
    volumes = [float(r[5]) for r in rows]
    closes.reverse()
    volumes.reverse()
    return closes, volumes

# ── Indicators ────────────────────────────────────────────────────────────────
def calc_rsi(closes: list, period: int = 14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(max(d, 0) for d in deltas[:period]) / period
    avg_l = sum(max(-d, 0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)

def calc_bb(closes: list, period: int = 20, nstd: float = 2.0):
    if len(closes) < period:
        return None, None, None
    w = closes[-period:]
    m = sum(w) / period
    s = math.sqrt(sum((x - m) ** 2 for x in w) / period)
    return m, m - nstd * s, m + nstd * s        # mid, lower, upper

def calc_ema(values: list, period: int):
    if len(values) < period * 2:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e

# ── Balances ──────────────────────────────────────────────────────────────────
def get_both_balances() -> tuple:
    accounts = _private("GET", "/api/v1/accounts?type=trade")
    usdt = base = 0.0
    for acc in accounts:
        if acc["currency"] == "USDT":
            usdt += float(acc["available"])
        elif acc["currency"] == BASE_COIN:
            base += float(acc["available"])
    return usdt, base

# ── Symbol info ───────────────────────────────────────────────────────────────
_sym_cache = {}

def get_symbol_info(symbol: str) -> dict:
    if symbol in _sym_cache:
        return _sym_cache[symbol]
    for s in _public("/api/v2/symbols"):
        if s["symbol"] == symbol:
            info = {"baseMinSize": float(s["baseMinSize"]),
                    "baseIncrement": float(s["baseIncrement"]),
                    "baseIncrementStr": s.get("baseIncrement", "0.000001"),
                    "quoteMinSize": float(s.get("quoteMinSize", "1")),
                    "priceIncrement": s.get("priceIncrement", "0.000001")}
            _sym_cache[symbol] = info
            return info
    raise RuntimeError(f"Symbol {symbol} not found")

def round_size(raw: float, inc: float) -> float:
    if inc <= 0:
        return raw
    return int(raw / inc) * inc

def round_size_decimal(raw: float, inc_s: str) -> float:
    inc = Decimal(str(inc_s))
    return float(Decimal(str(raw)).quantize(inc, rounding=ROUND_DOWN))

def size_to_str(size: float, inc_s: str) -> str:
    inc = Decimal(str(inc_s))
    return str(Decimal(str(size)).quantize(inc, rounding=ROUND_DOWN))

def round_price(raw: float, inc_s: str) -> float:
    inc = Decimal(str(inc_s))
    return float(Decimal(str(raw)).quantize(inc, rounding=ROUND_DOWN))

def price_to_str(price: float, inc_s: str) -> str:
    inc = Decimal(str(inc_s))
    return str(Decimal(str(price)).quantize(inc, rounding=ROUND_DOWN))

# ── Orders ────────────────────────────────────────────────────────────────────
def limit_buy(symbol, price_s, size_s) -> dict:
    body = {"clientOid": str(uuid.uuid4()), "symbol": symbol, "side": "buy",
            "type": "limit", "price": price_s, "size": size_s,
            "timeInForce": "GTC", "postOnly": True}
    if not LIVE_TRADING:
        log(f"[DRY] LIMIT BUY {symbol} p={price_s} s={size_s}")
        return {"orderId": "dry-buy"}
    return _private("POST", "/api/v1/orders", body)

def limit_sell(symbol, price_s, size_s) -> dict:
    body = {"clientOid": str(uuid.uuid4()), "symbol": symbol, "side": "sell",
            "type": "limit", "price": price_s, "size": size_s,
            "timeInForce": "GTC", "postOnly": True}
    if not LIVE_TRADING:
        log(f"[DRY] LIMIT SELL {symbol} p={price_s} s={size_s}")
        return {"orderId": "dry-sell"}
    return _private("POST", "/api/v1/orders", body)

def market_sell(symbol, size_s) -> dict:
    body = {"clientOid": str(uuid.uuid4()), "symbol": symbol, "side": "sell",
            "type": "market", "size": size_s}
    if not LIVE_TRADING:
        log(f"[DRY] MKT SELL {symbol} s={size_s}")
        return {"orderId": "dry-mkt"}
    return _private("POST", "/api/v1/orders", body)

def cancel_order(oid: str):
    if not LIVE_TRADING or oid.startswith("dry"):
        return
    try:
        _private("DELETE", f"/api/v1/orders/{oid}")
    except Exception as e:
        log(f"cancel warning: {e}")

def get_order(oid: str) -> dict:
    if oid.startswith("dry"):
        return {"isActive": False, "dealSize": "0", "side": "buy"}
    return _private("GET", f"/api/v1/orders/{oid}")

# ── State helpers ─────────────────────────────────────────────────────────────
def clear_position(st):
    st.update({"in_position": False, "entry_price": None, "entry_size": None,
               "buy_order_id": None, "tp_order_id": None, "sl_order_id": None,
               "peak_price": None, "trailing_active": False})

def clear_pending(st):
    st.update({"pending_buy_order_id": None, "pending_buy_price": None,
               "pending_buy_size": None, "pending_buy_ts": None})

def mirror_legacy_fields(st: dict):
    """Maintain backward-compatible single-position keys in state.json."""
    pos = st.get("positions") or []
    pend = st.get("pending_buys") or []

    if pos:
        p0 = pos[0]
        st["in_position"] = True
        st["entry_price"] = p0.get("entry_price")
        st["entry_size"] = p0.get("entry_size")
        st["buy_order_id"] = p0.get("buy_order_id")
        st["tp_order_id"] = p0.get("tp_order_id")
        st["peak_price"] = p0.get("peak_price")
        st["trailing_active"] = p0.get("trailing_active", False)
    else:
        clear_position(st)

    if pend:
        b0 = pend[0]
        st["pending_buy_order_id"] = b0.get("order_id")
        st["pending_buy_price"] = b0.get("price")
        st["pending_buy_size"] = b0.get("size")
        st["pending_buy_ts"] = b0.get("ts")
    else:
        clear_pending(st)

def add_position(st: dict, entry_price: float, entry_size: float, buy_order_id=None):
    pid = int(st.get("next_position_id") or 1)
    st["positions"].append({
        "id": pid,
        "entry_price": entry_price,
        "entry_size": entry_size,
        "buy_order_id": buy_order_id,
        "tp_order_id": None,
        "peak_price": entry_price,
        "trailing_active": False,
    })
    st["next_position_id"] = pid + 1
    return pid

def remove_position(st: dict, pos_id: int):
    st["positions"] = [p for p in st.get("positions", []) if int(p.get("id", -1)) != int(pos_id)]

def total_active_slots(st: dict) -> int:
    return len(st.get("positions", [])) + len(st.get("pending_buys", []))

# ── Entry evaluation ─────────────────────────────────────────────────────────
def evaluate_entry(price, closes, volumes, best_bid, best_ask):
    """Returns (should_buy, reason_str)."""
    if best_ask <= 0 or best_bid <= 0:
        return False, "no bid/ask"
    spread = (best_ask - best_bid) / best_ask
    if spread > MAX_SPREAD_PCT:
        return False, f"spread {spread:.4%}"

    rsi = calc_rsi(closes, RSI_PERIOD)
    if rsi is None:
        return False, "no RSI data"
    if rsi >= RSI_OVERSOLD:
        return False, f"RSI {rsi:.1f}>={RSI_OVERSOLD}"

    _, bb_lo, _ = calc_bb(closes, BB_PERIOD, BB_STD)
    if bb_lo is None:
        return False, "no BB data"
    if price > bb_lo:
        return False, f"above BB-lo {bb_lo:.6f}"

    # volume: last candle >= 80 % of 20-period avg
    if len(volumes) >= BB_PERIOD:
        avg_v = sum(volumes[-BB_PERIOD:]) / BB_PERIOD
        if avg_v > 0 and volumes[-1] < avg_v * 0.8:
            return False, "low volume"

    # loose trend guard: not in severe freefall
    ema26 = calc_ema(closes, 26)
    if ema26 and price < ema26 * 0.97:
        return False, "severe downtrend"

    return True, f"RSI={rsi:.1f} BB-lo={bb_lo:.6f}"

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        raise RuntimeError("Missing KuCoin API credentials in .env")

    state = load_state()
    log(f"Bot v4 started | {SYMBOL} | live={LIVE_TRADING} | target={TARGET_USDT}")
    log(
        "Config | "
        f"min_wallet={MIN_WALLET_USDT} "
        f"min_trade={MIN_USDT_TO_TRADE} "
        f"max_trade={MAX_USDT_PER_TRADE} "
        f"position_pct={POSITION_PCT} "
        f"grid_levels={GRID_LEVELS} "
        f"grid_step={GRID_STEP_PCT:.3%} "
        f"max_open={MAX_OPEN_POSITIONS} "
        f"max_pending={MAX_PENDING_BUYS} "
        f"max_dd={MAX_DAILY_DRAWDOWN_PCT:.2%} "
        f"max_orders_min={MAX_ORDERS_PER_MIN} "
        f"rsi_buy<{RSI_OVERSOLD} "
        f"rsi_sell>={RSI_TAKE_PROFIT} "
        f"tp={TAKE_PROFIT_PCT:.3%}"
    )

    while True:
        try:
            # 1 — market snapshot
            lv        = get_level1(SYMBOL)
            price     = lv["price"]
            best_bid  = lv["bestBid"]
            best_ask  = lv["bestAsk"]

            closes, volumes = get_candles(SYMBOL, CANDLE_TYPE, 100)

            usdt_avail, base_avail = get_both_balances()
            total_est = usdt_avail + base_avail * price

            if state.get("equity_date") != str(date.today()) or state.get("day_start_equity") is None:
                reset_daily_risk_counters(state, total_est)
                save_state(state)

            info     = get_symbol_info(SYMBOL)
            min_base = info["baseMinSize"]
            base_inc_s = info.get("baseIncrementStr", "0.000001")
            price_inc = info.get("priceIncrement", "0.000001")
            log(f"DEBUG: {SYMBOL} increments price={price_inc} size={base_inc_s} minBase={min_base}")

            rsi_now  = calc_rsi(closes, RSI_PERIOD)
            _, bb_lo, bb_up = calc_bb(closes, BB_PERIOD, BB_STD)
            open_pos = len(state.get("positions", []))
            pend_pos = len(state.get("pending_buys", []))

            log(f"p={price:.6f} rsi={rsi_now or 0:.1f} "
                f"bb_lo={bb_lo or 0:.6f} total≈{total_est:.4f} "
                f"usdt={usdt_avail:.4f} {BASE_COIN}={base_avail:.6f} "
                f"losses={state['daily_losses']}/{MAX_DAILY_LOSSES} "
                f"slots={open_pos} open/{pend_pos} pending "
                f"realized={float(state.get('realized_pnl_today') or 0):+.4f}")

            # 2 — target reached
            if total_est >= TARGET_USDT:
                log(f"🎯 TARGET {total_est:.4f} >= {TARGET_USDT}. Idle.")
                time.sleep(CHECK_INTERVAL)
                continue

            # 2.1 — drawdown circuit breaker
            day_start_equity = float(state.get("day_start_equity") or total_est)
            drawdown = 0.0 if day_start_equity <= 0 else (day_start_equity - total_est) / day_start_equity
            if drawdown >= MAX_DAILY_DRAWDOWN_PCT:
                state["killswitch"] = True
                state["killswitch_reason"] = f"daily drawdown {drawdown:.2%} >= {MAX_DAILY_DRAWDOWN_PCT:.2%}"
                save_state(state)
                log(f"🛑 Circuit breaker tripped: {state['killswitch_reason']}")
                time.sleep(CHECK_INTERVAL * 3)
                continue
            if state.get("killswitch"):
                log(f"🛑 Killswitch active: {state.get('killswitch_reason')}")
                time.sleep(CHECK_INTERVAL * 3)
                continue

            # 3 — wallet too small
            if total_est < MIN_WALLET_USDT:
                log(f"⛔ Wallet {total_est:.4f} < {MIN_WALLET_USDT}. Halt.")
                time.sleep(CHECK_INTERVAL * 10)
                continue

            # 4 — daily loss cap
            if state["daily_losses"] >= MAX_DAILY_LOSSES:
                log("🛑 Daily loss cap. Resting.")
                time.sleep(CHECK_INTERVAL * 2)
                continue

            # 5 — cooldown after last loss
            if state.get("last_loss_ts", 0) > 0:
                elapsed = time.time() - state["last_loss_ts"]
                if elapsed < COOLDOWN_SEC:
                    log(f"⏸ Cooldown {int(COOLDOWN_SEC - elapsed)}s left")
                    time.sleep(CHECK_INTERVAL)
                    continue

            state_changed = False

            # 6 — process pending buys (many)
            pending_next = []
            for pb in list(state.get("pending_buys", [])):
                oid   = str(pb.get("order_id") or "")
                pprice = float(pb.get("price") or price)
                psz    = float(pb.get("size") or 0)
                pts    = float(pb.get("ts") or time.time())

                if psz < min_base:
                    log(f"⚠️ Drop tiny pending size={psz:.6f}")
                    state_changed = True
                    continue

                if (not LIVE_TRADING) or oid.startswith("dry"):
                    pid = add_position(state, pprice, psz, oid)
                    log(f"✅ Buy filled (dry) pos#{pid} entry={pprice:.6f} size={psz:.6f}")
                    append_trade_event({
                        "symbol": SYMBOL,
                        "event": "buy_filled",
                        "mode": "dry",
                        "position_id": pid,
                        "price": pprice,
                        "size": psz,
                    })
                    state_changed = True
                    continue

                try:
                    order  = get_order(oid)
                    active = bool(order.get("isActive", False))
                    dsz    = float(order.get("dealSize") or 0)
                    dfn    = float(order.get("dealFunds") or 0)
                    age    = int(time.time() - pts)
                except Exception as e:
                    log(f"pending check failed oid={oid}: {e}")
                    pending_next.append(pb)
                    continue

                if dsz >= min_base:
                    if active:
                        cancel_order(oid)
                    entry = (dfn / dsz) if dfn > 0 else pprice
                    pid = add_position(state, entry, dsz, oid)
                    log(f"✅ Buy filled pos#{pid} entry={entry:.6f} size={dsz:.6f}")
                    append_trade_event({
                        "symbol": SYMBOL,
                        "event": "buy_filled",
                        "mode": "live" if LIVE_TRADING else "dry",
                        "position_id": pid,
                        "price": entry,
                        "size": dsz,
                        "order_id": oid,
                    })
                    state_changed = True
                elif not active:
                    log(f"⚠️ Pending closed without fill oid={oid}")
                    state_changed = True
                elif age >= BUY_TIMEOUT_SEC:
                    cancel_order(oid)
                    log(f"⌛ Pending timed out {age}s oid={oid}, canceled")
                    state_changed = True
                else:
                    pending_next.append(pb)
                    log(f"⏳ Waiting fill oid={oid} age={age}s")

            state["pending_buys"] = pending_next

            # 7 — manage active positions (many)
            positions_next = []
            sellable_base = base_avail
            for pos in list(state.get("positions", [])):
                pos_id = int(pos.get("id", 0))
                entry  = float(pos.get("entry_price") or 0)
                size   = float(pos.get("entry_size") or 0)
                if entry <= 0 or size < min_base:
                    log(f"⚠️ Drop invalid pos#{pos_id} entry={entry} size={size}")
                    state_changed = True
                    continue

                pnl = (price - entry) / entry
                peak = max(float(pos.get("peak_price") or entry), price)
                trailing_active = bool(pos.get("trailing_active", False))
                if pnl >= TRAIL_ACTIVATE:
                    trailing_active = True

                tp_id = pos.get("tp_order_id")
                tp_filled = False
                tp_active = False

                if tp_id and (not str(tp_id).startswith("dry")):
                    try:
                        tp_ord = get_order(tp_id)
                        tp_active = bool(tp_ord.get("isActive", False))
                        tp_deal = float(tp_ord.get("dealSize") or 0)
                        tp_filled = (not tp_active) and tp_deal >= max(min_base, size * 0.95)
                    except Exception as e:
                        log(f"tp check failed pos#{pos_id}: {e}")

                if not LIVE_TRADING:
                    tp_filled = pnl >= TAKE_PROFIT_PCT and ((rsi_now or 0) >= RSI_TAKE_PROFIT)

                if tp_filled:
                    exit_price = price
                    pnl_quote, fees_quote = calc_estimated_pnl_quote(entry, exit_price, size, MAKER_FEE)
                    net = pnl - 2 * MAKER_FEE
                    log(f"✅ TP filled pos#{pos_id} | gross={pnl:.4%} net≈{net:.4%} pnl≈{pnl_quote:+.4f} USDT")
                    state["realized_pnl_today"] = float(state.get("realized_pnl_today") or 0) + pnl_quote
                    state["fees_paid_today"] = float(state.get("fees_paid_today") or 0) + fees_quote
                    if pnl_quote >= 0:
                        state["wins_today"] = int(state.get("wins_today") or 0) + 1
                    else:
                        state["losses_today"] = int(state.get("losses_today") or 0) + 1
                    append_trade_event({
                        "symbol": SYMBOL,
                        "event": "position_exit",
                        "reason": "tp_fill",
                        "position_id": pos_id,
                        "entry_price": entry,
                        "exit_price": exit_price,
                        "size": size,
                        "pnl_quote": pnl_quote,
                        "fees_quote": fees_quote,
                    })
                    state_changed = True
                    continue

                # hard stop-loss
                if pnl <= -STOP_LOSS_PCT:
                    if tp_id:
                        cancel_order(str(tp_id))
                    sell_sz = min(size, sellable_base)
                    if sell_sz >= min_base:
                        market_sell(SYMBOL, size_to_str(sell_sz, base_inc_s))
                        mark_order_placed(state)
                        sellable_base = max(0.0, sellable_base - sell_sz)
                    exit_price = price
                    pnl_quote, fees_quote = calc_estimated_pnl_quote(entry, exit_price, size, TAKER_FEE)
                    net = pnl - MAKER_FEE - TAKER_FEE
                    log(f"🔴 SL exit pos#{pos_id} | gross={pnl:.4%} net≈{net:.4%} pnl≈{pnl_quote:+.4f} USDT")
                    state["daily_losses"] += 1
                    state["last_loss_ts"] = time.time()
                    state["realized_pnl_today"] = float(state.get("realized_pnl_today") or 0) + pnl_quote
                    state["fees_paid_today"] = float(state.get("fees_paid_today") or 0) + fees_quote
                    state["losses_today"] = int(state.get("losses_today") or 0) + 1
                    append_trade_event({
                        "symbol": SYMBOL,
                        "event": "position_exit",
                        "reason": "stop_loss",
                        "position_id": pos_id,
                        "entry_price": entry,
                        "exit_price": exit_price,
                        "size": size,
                        "pnl_quote": pnl_quote,
                        "fees_quote": fees_quote,
                    })
                    log(f"Losses today: {state['daily_losses']}/{MAX_DAILY_LOSSES}")
                    state_changed = True
                    continue

                # fast RSI take-profit exit
                min_net_edge = (2 * MAKER_FEE) + 0.0004
                if (rsi_now or 0) >= RSI_TAKE_PROFIT and pnl > min_net_edge:
                    if tp_id:
                        cancel_order(str(tp_id))
                    sell_sz = min(size, sellable_base)
                    if sell_sz >= min_base:
                        market_sell(SYMBOL, size_to_str(sell_sz, base_inc_s))
                        mark_order_placed(state)
                        sellable_base = max(0.0, sellable_base - sell_sz)
                    exit_price = price
                    pnl_quote, fees_quote = calc_estimated_pnl_quote(entry, exit_price, size, TAKER_FEE)
                    net = pnl - MAKER_FEE - TAKER_FEE
                    log(f"💸 RSI exit pos#{pos_id} | gross={pnl:.4%} net≈{net:.4%} pnl≈{pnl_quote:+.4f} USDT rsi={rsi_now:.1f}")
                    state["realized_pnl_today"] = float(state.get("realized_pnl_today") or 0) + pnl_quote
                    state["fees_paid_today"] = float(state.get("fees_paid_today") or 0) + fees_quote
                    if pnl_quote >= 0:
                        state["wins_today"] = int(state.get("wins_today") or 0) + 1
                    else:
                        state["losses_today"] = int(state.get("losses_today") or 0) + 1
                    append_trade_event({
                        "symbol": SYMBOL,
                        "event": "position_exit",
                        "reason": "rsi_take_profit",
                        "position_id": pos_id,
                        "entry_price": entry,
                        "exit_price": exit_price,
                        "size": size,
                        "pnl_quote": pnl_quote,
                        "fees_quote": fees_quote,
                    })
                    state_changed = True
                    continue

                # trailing stop (profit protect)
                trail_exit = False
                if trailing_active and peak > entry:
                    trail_stop = peak * (1 - TRAIL_DISTANCE)
                    if price <= trail_stop:
                        trail_exit = True
                        if tp_id:
                            cancel_order(str(tp_id))
                        sell_sz = min(size, sellable_base)
                        if sell_sz >= min_base:
                            market_sell(SYMBOL, size_to_str(sell_sz, base_inc_s))
                            mark_order_placed(state)
                            sellable_base = max(0.0, sellable_base - sell_sz)
                        exit_price = price
                        pnl_quote, fees_quote = calc_estimated_pnl_quote(entry, exit_price, size, TAKER_FEE)
                        net = pnl - MAKER_FEE - TAKER_FEE
                        log(f"📈 Trail exit pos#{pos_id} | gross={pnl:.4%} net≈{net:.4%} pnl≈{pnl_quote:+.4f} USDT peak={peak:.6f}")
                        state["realized_pnl_today"] = float(state.get("realized_pnl_today") or 0) + pnl_quote
                        state["fees_paid_today"] = float(state.get("fees_paid_today") or 0) + fees_quote
                        if pnl_quote >= 0:
                            state["wins_today"] = int(state.get("wins_today") or 0) + 1
                        else:
                            state["losses_today"] = int(state.get("losses_today") or 0) + 1
                        append_trade_event({
                            "symbol": SYMBOL,
                            "event": "position_exit",
                            "reason": "trailing_stop",
                            "position_id": pos_id,
                            "entry_price": entry,
                            "exit_price": exit_price,
                            "size": size,
                            "pnl_quote": pnl_quote,
                            "fees_quote": fees_quote,
                        })
                        state_changed = True

                if trail_exit:
                    continue

                # place or refresh TP
                if (not tp_id) or (tp_id and not tp_active):
                    tp_gross = TAKE_PROFIT_PCT + 2 * MAKER_FEE
                    tp_price_raw = max(entry * (1 + tp_gross), best_ask * 1.0002)
                    tp_price = round_price(tp_price_raw, price_inc)
                    tp_size = round_size_decimal(size, base_inc_s)
                    if tp_size >= min_base:
                        ok_order, why = can_place_order(state)
                        if ok_order:
                            tp_res = limit_sell(
                                SYMBOL,
                                price_to_str(tp_price, price_inc),
                                size_to_str(tp_size, base_inc_s),
                            )
                            mark_order_placed(state)
                            pos["tp_order_id"] = tp_res.get("orderId", "dry-tp")
                            state_changed = True
                            log(
                                f"🧩 TP placed pos#{pos_id} "
                                f"p={price_to_str(tp_price, price_inc)} "
                                f"s={size_to_str(tp_size, base_inc_s)}"
                            )
                        else:
                            log(f"⏱ TP skip pos#{pos_id}: {why}")

                pos["peak_price"] = peak
                pos["trailing_active"] = trailing_active
                positions_next.append(pos)
                trail_s = "ON" if trailing_active else "off"
                log(f"📊 Hold pos#{pos_id} pnl={pnl:.4%} peak={peak:.6f} trail={trail_s}")

            state["positions"] = positions_next

            # 8 — orphan recovery and fresh entries
            if base_avail > min_base and not state.get("positions") and not state.get("pending_buys"):
                rec_size = round_size_decimal(base_avail, base_inc_s)
                if rec_size >= min_base:
                    pid = add_position(state, price, rec_size, None)
                    state_changed = True
                    log(f"♻️ Recovered orphan inventory -> pos#{pid} size={rec_size:.6f}")

            open_slots = max(0, MAX_OPEN_POSITIONS - len(state.get("positions", [])))
            pending_slots = max(0, MAX_PENDING_BUYS - len(state.get("pending_buys", [])))
            grid_slots = max(0, GRID_LEVELS - total_active_slots(state))
            can_add_orders = min(open_slots, pending_slots, grid_slots)

            if can_add_orders > 0 and usdt_avail >= MIN_USDT_TO_TRADE:
                ok, reason = evaluate_entry(price, closes, volumes, best_bid, best_ask)
                if ok:
                    per_order_spend = min(usdt_avail * POSITION_PCT, MAX_USDT_PER_TRADE)
                    if per_order_spend < MIN_USDT_TO_TRADE:
                        log("Spend too small for grid order, skip.")
                    else:
                        affordable = max(1, int(usdt_avail / per_order_spend))
                        orders_to_place = min(can_add_orders, affordable)
                        for idx in range(orders_to_place):
                            lvl_price_raw = best_bid * (1 - GRID_STEP_PCT * idx) * 0.9998
                            buy_p = round_price(lvl_price_raw, price_inc)
                            if buy_p <= 0:
                                continue
                            raw = per_order_spend / buy_p
                            size = round_size_decimal(raw, base_inc_s)
                            if size < min_base:
                                log(f"Grid level {idx+1}: size {size} < min {min_base}, skip")
                                continue

                            bp_s = price_to_str(buy_p, price_inc)
                            sz_s = size_to_str(size, base_inc_s)
                            ok_order, why = can_place_order(state)
                            if not ok_order:
                                log(f"⏱ Grid buy skip L{idx+1}: {why}")
                                continue
                            res = limit_buy(SYMBOL, bp_s, sz_s)
                            mark_order_placed(state)
                            oid = res.get("orderId", "dry-buy")
                            state.setdefault("pending_buys", []).append({
                                "order_id": oid,
                                "price": buy_p,
                                "size": size,
                                "ts": time.time(),
                            })
                            state_changed = True
                            log(f"📥 Grid buy L{idx+1}/{orders_to_place} p={bp_s} s={sz_s} | {reason}")
                            append_trade_event({
                                "symbol": SYMBOL,
                                "event": "buy_submitted",
                                "level": idx + 1,
                                "order_id": oid,
                                "price": buy_p,
                                "size": size,
                                "mode": "live" if LIVE_TRADING else "dry",
                            })
                else:
                    log(f"⏳ No signal — {reason}")
            elif usdt_avail < MIN_USDT_TO_TRADE and not state.get("positions"):
                log("⏳ Insufficient USDT")

            if state_changed:
                save_state(state)

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()