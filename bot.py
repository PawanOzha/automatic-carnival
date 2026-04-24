"""
kucoin-bot v3 — mean-reversion grinder
Goal : grow spot wallet from 4.08 USDT → 5.00 USDT
Strategy: RSI-oversold + Bollinger-Band lower-band touch on 5-min candles
Exits  : fee-aware TP limit, trailing stop, hard SL
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
POSITION_PCT       = float(os.getenv("POSITION_PCT",      "0.85"))

TAKE_PROFIT_PCT    = float(os.getenv("TAKE_PROFIT_PCT",   "0.030"))   # +3.0 %
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT",     "0.015"))   # -1.5 %
TRAIL_ACTIVATE     = float(os.getenv("TRAIL_ACTIVATE",    "0.015"))   # start at +1.5 %
TRAIL_DISTANCE     = float(os.getenv("TRAIL_DISTANCE",    "0.008"))   # 0.8 % below peak

MAX_DAILY_LOSSES   = int(os.getenv("MAX_DAILY_LOSSES",    "2"))
MIN_WALLET_USDT    = float(os.getenv("MIN_WALLET_USDT",   "2.0"))
LIVE_TRADING       = os.getenv("LIVE_TRADING", "false").lower() == "true"
BUY_TIMEOUT_SEC    = int(os.getenv("BUY_TIMEOUT_SEC",     "120"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC",        "300"))

RSI_PERIOD         = int(os.getenv("RSI_PERIOD",          "14"))
RSI_OVERSOLD       = float(os.getenv("RSI_OVERSOLD",      "50"))
BB_PERIOD          = int(os.getenv("BB_PERIOD",           "20"))
BB_STD             = float(os.getenv("BB_STD",            "2.0"))
CANDLE_TYPE        = os.getenv("CANDLE_TYPE",             "5min")
MAX_SPREAD_PCT     = float(os.getenv("MAX_SPREAD_PCT",    "0.004"))

MAKER_FEE          = 0.001
TAKER_FEE          = 0.001

STATE_FILE = "state.json"
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
}

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    if saved.get("daily_loss_date") != str(date.today()):
        saved["daily_losses"]    = 0
        saved["daily_loss_date"] = str(date.today())
    return {**DEFAULT_STATE, **saved}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

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
                    "quoteMinSize": float(s.get("quoteMinSize", "1"))}
            _sym_cache[symbol] = info
            return info
    raise RuntimeError(f"Symbol {symbol} not found")

def round_size(raw: float, inc: float) -> float:
    if inc <= 0:
        return raw
    return int(raw / inc) * inc

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
    log(f"Bot v3 started | {SYMBOL} | live={LIVE_TRADING} | target={TARGET_USDT}")

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

            info     = get_symbol_info(SYMBOL)
            min_base = info["baseMinSize"]
            base_inc = info["baseIncrement"]

            rsi_now  = calc_rsi(closes, RSI_PERIOD)
            _, bb_lo, bb_up = calc_bb(closes, BB_PERIOD, BB_STD)

            log(f"p={price:.6f} rsi={rsi_now or 0:.1f} "
                f"bb_lo={bb_lo or 0:.6f} total≈{total_est:.4f} "
                f"usdt={usdt_avail:.4f} {BASE_COIN}={base_avail:.6f} "
                f"losses={state['daily_losses']}/{MAX_DAILY_LOSSES}")

            # 2 — target reached
            if total_est >= TARGET_USDT:
                log(f"🎯 TARGET {total_est:.4f} >= {TARGET_USDT}. Idle.")
                time.sleep(CHECK_INTERVAL)
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

            # 6 — pending buy: wait for fill or timeout
            pend_id = state.get("pending_buy_order_id")
            if pend_id:
                if not LIVE_TRADING:
                    fp = float(state.get("pending_buy_price") or price)
                    fs = float(state.get("pending_buy_size") or 0)
                    state.update({"in_position": True, "entry_price": fp,
                                  "entry_size": fs, "buy_order_id": pend_id,
                                  "peak_price": fp, "trailing_active": False})
                    clear_pending(state); save_state(state)
                    log(f"✅ Buy filled (dry) entry={fp:.6f} size={fs:.6f}")
                else:
                    order   = get_order(pend_id)
                    active  = bool(order.get("isActive", False))
                    dsz     = float(order.get("dealSize") or 0)
                    dfn     = float(order.get("dealFunds") or 0)
                    age     = int(time.time() - float(state.get("pending_buy_ts") or time.time()))

                    if dsz >= min_base:
                        if active:
                            cancel_order(pend_id)
                        entry = (dfn / dsz) if dfn > 0 else float(state.get("pending_buy_price") or price)
                        state.update({"in_position": True, "entry_price": entry,
                                      "entry_size": dsz, "buy_order_id": pend_id,
                                      "peak_price": entry, "trailing_active": False})
                        clear_pending(state); save_state(state)
                        log(f"✅ Buy filled entry={entry:.6f} size={dsz:.6f}")
                    elif not active:
                        clear_pending(state); save_state(state)
                        log("⚠️ Buy closed without fill.")
                    elif age >= BUY_TIMEOUT_SEC:
                        cancel_order(pend_id)
                        clear_pending(state); save_state(state)
                        log(f"⌛ Buy timed out {age}s, canceled.")
                    else:
                        log(f"⏳ Waiting fill deal={dsz:.6f} age={age}s")

            # 7 — in position: manage trade with trailing stop
            if state["in_position"]:
                entry = float(state["entry_price"])
                size  = float(state.get("entry_size") or 0)
                pnl   = (price - entry) / entry

                # update peak price
                peak = max(float(state.get("peak_price") or entry), price)
                state["peak_price"] = peak

                # check TP limit order fill
                tp_id = state.get("tp_order_id")
                tp_filled = False
                tp_active = False

                if tp_id and not tp_id.startswith("dry"):
                    tp_ord = get_order(tp_id)
                    tp_active = bool(tp_ord.get("isActive", False))
                    tp_deal   = float(tp_ord.get("dealSize") or 0)
                    tp_filled = (not tp_active) and tp_deal >= max(min_base, size * 0.95)

                # dry-run simulation
                sl_filled = False
                if not LIVE_TRADING:
                    tp_filled = pnl >= TAKE_PROFIT_PCT
                    sl_filled = pnl <= -STOP_LOSS_PCT

                # ── trailing stop check (live & dry) ──
                trail_exit = False
                if pnl >= TRAIL_ACTIVATE:
                    state["trailing_active"] = True
                if state.get("trailing_active") and peak > entry:
                    trail_stop = peak * (1 - TRAIL_DISTANCE)
                    if price <= trail_stop and not tp_filled:
                        trail_exit = True
                        if not LIVE_TRADING:
                            tp_filled = False
                            sl_filled = False

                if tp_filled:
                    net = pnl - 2 * MAKER_FEE
                    log(f"✅ TP filled | gross={pnl:.4%} net≈{net:.4%}")
                    clear_position(state); clear_pending(state); save_state(state)

                elif trail_exit:
                    # trailing stop triggered — profitable exit
                    if tp_id:
                        cancel_order(tp_id)
                    sell_sz = min(size, base_avail)
                    if sell_sz >= min_base:
                        market_sell(SYMBOL, q6(sell_sz))
                    net = pnl - MAKER_FEE - TAKER_FEE
                    log(f"📈 Trail exit | gross={pnl:.4%} net≈{net:.4%} peak={peak:.6f}")
                    clear_position(state); clear_pending(state); save_state(state)

                else:
                    # place/re-place TP if needed
                    if not tp_id or (tp_id and not tp_active and not tp_filled):
                        # fee-aware TP: gross target must cover both legs of fees
                        tp_gross = TAKE_PROFIT_PCT + 2 * MAKER_FEE
                        tp_price = max(entry * (1 + tp_gross), best_ask * 1.0002)
                        tp_res = limit_sell(SYMBOL, q6(tp_price), q6(size))
                        state["tp_order_id"] = tp_res.get("orderId", "dry-tp")
                        save_state(state)
                        log(f"🧩 TP placed p={q6(tp_price)} s={q6(size)}")

                    # hard stop-loss — emergency market exit
                    if pnl <= -STOP_LOSS_PCT:
                        if tp_id:
                            cancel_order(tp_id)
                        sell_sz = min(size, base_avail)
                        if sell_sz >= min_base:
                            market_sell(SYMBOL, q6(sell_sz))
                            sl_filled = True
                        else:
                            log(f"⚠️ SL triggered but size too small ({sell_sz:.6f})")

                    if sl_filled:
                        net = pnl - MAKER_FEE - TAKER_FEE
                        log(f"🔴 SL exit | gross={pnl:.4%} net≈{net:.4%}")
                        state["daily_losses"] += 1
                        state["last_loss_ts"] = time.time()
                        clear_position(state); clear_pending(state); save_state(state)
                        log(f"Losses today: {state['daily_losses']}/{MAX_DAILY_LOSSES}")
                    elif not tp_filled and not trail_exit:
                        trail_s = "ON" if state.get("trailing_active") else "off"
                        log(f"📊 Hold pnl={pnl:.4%} peak={peak:.6f} trail={trail_s}")

            # 8 — no position: look for entry
            else:
                # recover orphaned base coin — use ACTUAL balance for TP calc
                if base_avail > min_base and not state["in_position"]:
                    log(f"♻️ Recovered {base_avail:.6f} {BASE_COIN}")
                    # use current price as entry — TP still fee-aware
                    state.update({"in_position": True, "entry_price": price,
                                  "entry_size": base_avail, "buy_order_id": None,
                                  "peak_price": price, "trailing_active": False})
                    tp_gross = TAKE_PROFIT_PCT + 2 * MAKER_FEE
                    tp_p = max(price * (1 + tp_gross), best_ask * 1.0002)
                    tp_res = limit_sell(SYMBOL, q6(tp_p), q6(base_avail))
                    state["tp_order_id"] = tp_res.get("orderId", "dry-tp")
                    save_state(state)

                elif usdt_avail >= MIN_USDT_TO_TRADE:
                    ok, reason = evaluate_entry(price, closes, volumes, best_bid, best_ask)
                    if ok:
                        spend = min(usdt_avail * POSITION_PCT, MAX_USDT_PER_TRADE)
                        if spend < MIN_USDT_TO_TRADE:
                            log("Spend too small, skip.")
                        else:
                            buy_p = round(best_bid * 0.9998, 6)  # just below bid
                            raw   = spend / buy_p
                            size  = round_size(raw, base_inc)
                            if size < min_base:
                                log(f"Size {size} < min {min_base}, skip.")
                            else:
                                bp_s = q6(buy_p)
                                sz_s = q6(size)
                                res  = limit_buy(SYMBOL, bp_s, sz_s)
                                oid  = res.get("orderId", "dry-buy")
                                state.update({
                                    "in_position": False, "entry_price": None,
                                    "entry_size": None, "buy_order_id": None,
                                    "pending_buy_order_id": oid,
                                    "pending_buy_price": buy_p,
                                    "pending_buy_size": size,
                                    "pending_buy_ts": time.time(),
                                    "tp_order_id": None, "sl_order_id": None,
                                })
                                save_state(state)
                                log(f"📥 Buy placed p={bp_s} s={sz_s} | {reason}")
                    else:
                        log(f"⏳ No signal — {reason}")
                else:
                    log("⏳ Insufficient USDT")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()