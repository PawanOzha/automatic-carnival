"""
tiny-kucoin-bot  —  fixed & improved
Goal: grow spot wallet from 4.08 USDT → 5.00 USDT
Strategy: EMA pullback, limit orders (maker), hard stops, daily loss cap
"""

import os
import time
import json
import uuid
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, date

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL            = "https://api.kucoin.com"
API_KEY             = os.getenv("KUCOIN_API_KEY", "")
API_SECRET          = os.getenv("KUCOIN_API_SECRET", "")
API_PASSPHRASE      = os.getenv("KUCOIN_API_PASSPHRASE", "")

SYMBOL              = os.getenv("SYMBOL", "KCS-USDT").upper()
BASE_COIN, QUOTE   = SYMBOL.split("-")

TARGET_USDT         = float(os.getenv("TARGET_USDT",        "5.0"))
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL",       "30"))
MIN_USDT_TO_TRADE   = float(os.getenv("MIN_USDT_TO_TRADE",  "1.0"))
MAX_USDT_PER_TRADE  = float(os.getenv("MAX_USDT_PER_TRADE", "3.5"))
TAKE_PROFIT_PCT     = float(os.getenv("TAKE_PROFIT_PCT",    "0.025"))   # +2.5 %
STOP_LOSS_PCT       = float(os.getenv("STOP_LOSS_PCT",      "0.012"))   # -1.2 %
MAX_DAILY_LOSSES    = int(os.getenv("MAX_DAILY_LOSSES",     "2"))
MIN_WALLET_USDT     = float(os.getenv("MIN_WALLET_USDT",    "2.0"))     # halt below this
LIVE_TRADING        = os.getenv("LIVE_TRADING", "false").lower() == "true"

STATE_FILE = "state.json"
SESSION    = requests.Session()
SESSION.headers.update({"User-Agent": "tiny-kucoin-bot/2.0"})

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

# ── Decimal helpers ───────────────────────────────────────────────────────────
def q4(v: float) -> str:
    return str(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))

def q6(v: float) -> str:
    return str(Decimal(str(v)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))

def q8(v: float) -> str:
    return str(Decimal(str(v)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    default = {
        "in_position":         False,
        "entry_price":         None,
        "entry_size":          None,
        "buy_order_id":        None,
        "tp_order_id":         None,
        "sl_order_id":         None,
        "daily_losses":        0,
        "daily_loss_date":     str(date.today()),
    }
    if not os.path.exists(STATE_FILE):
        return default
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    # reset daily loss counter if it's a new day
    if saved.get("daily_loss_date") != str(date.today()):
        saved["daily_losses"]    = 0
        saved["daily_loss_date"] = str(date.today())
    return {**default, **saved}

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
        "KC-API-KEY":        API_KEY,
        "KC-API-SIGN":       sig,
        "KC-API-TIMESTAMP":  ts,
        "KC-API-PASSPHRASE": pp,
        "KC-API-KEY-VERSION":"2",
        "Content-Type":      "application/json",
    }

def _private(method: str, endpoint: str, body_dict=None):
    body    = json.dumps(body_dict, separators=(",", ":")) if body_dict else ""
    headers = _sign(method, endpoint, body)
    url     = BASE_URL + endpoint

    r = SESSION.request(method.upper(), url, headers=headers,
                        data=body or None, timeout=20)
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
    return {
        "price":   float(d["price"]),
        "bestBid": float(d["bestBid"]),
        "bestAsk": float(d["bestAsk"]),
    }

def get_recent_closes(symbol: str, ktype="1min", limit=60) -> list:
    rows   = _public("/api/v1/market/candles", {"type": ktype, "symbol": symbol})
    rows   = rows[:limit]
    closes = [float(r[2]) for r in rows]   # close price is index 2
    closes.reverse()                        # oldest → newest
    return closes

# ── EMA (proper warmup) ───────────────────────────────────────────────────────
def ema(values: list, period: int):
    if len(values) < period * 2:            # need 2× period for reliable warmup
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period       # SMA seed — much more accurate
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

# ── Balances ──────────────────────────────────────────────────────────────────
def get_trade_balance(currency: str) -> float:
    """Returns available balance in the SPOT/TRADE account only."""
    accounts = _private("GET", "/api/v1/accounts?type=trade")
    total = 0.0
    for acc in accounts:
        if acc["currency"] == currency:
            total += float(acc["available"])
    return total

def get_both_balances() -> tuple:
    """Returns (usdt_available, base_available) from trade account."""
    accounts = _private("GET", "/api/v1/accounts?type=trade")
    usdt = base = 0.0
    for acc in accounts:
        if acc["currency"] == "USDT":
            usdt += float(acc["available"])
        elif acc["currency"] == BASE_COIN:
            base += float(acc["available"])
    return usdt, base

def estimate_total(usdt: float, base: float, price: float) -> float:
    return usdt + base * price

# ── Symbol info (min size / increment) ───────────────────────────────────────
_symbol_info_cache = {}

def get_symbol_info(symbol: str) -> dict:
    if symbol in _symbol_info_cache:
        return _symbol_info_cache[symbol]
    symbols = _public("/api/v2/symbols")
    for s in symbols:
        if s["symbol"] == symbol:
            info = {
                "baseMinSize":  float(s["baseMinSize"]),
                "baseIncrement":float(s["baseIncrement"]),
                "quoteMinSize": float(s.get("quoteMinSize", "1")),
            }
            _symbol_info_cache[symbol] = info
            return info
    raise RuntimeError(f"Symbol {symbol} not found")

# ── Order placement (LIMIT — maker) ──────────────────────────────────────────
def limit_buy(symbol: str, price_str: str, size_str: str) -> dict:
    body = {
        "clientOid":   str(uuid.uuid4()),
        "symbol":      symbol,
        "side":        "buy",
        "type":        "limit",
        "price":       price_str,
        "size":        size_str,
        "timeInForce": "GTC",
        "postOnly":    True,         # ← maker only, never taker
    }
    if not LIVE_TRADING:
        log(f"[DRY RUN] LIMIT BUY  {symbol}  price={price_str}  size={size_str}")
        return {"orderId": "dry-buy"}
    return _private("POST", "/api/v1/orders", body)

def limit_sell(symbol: str, price_str: str, size_str: str) -> dict:
    body = {
        "clientOid":   str(uuid.uuid4()),
        "symbol":      symbol,
        "side":        "sell",
        "type":        "limit",
        "price":       price_str,
        "size":        size_str,
        "timeInForce": "GTC",
        "postOnly":    True,         # ← maker only, never taker
    }
    if not LIVE_TRADING:
        log(f"[DRY RUN] LIMIT SELL {symbol}  price={price_str}  size={size_str}")
        return {"orderId": "dry-sell"}
    return _private("POST", "/api/v1/orders", body)

def cancel_order(order_id: str):
    if not LIVE_TRADING or order_id.startswith("dry"):
        return
    try:
        _private("DELETE", f"/api/v1/orders/{order_id}")
    except Exception as e:
        log(f"cancel_order warning: {e}")

def get_order(order_id: str) -> dict:
    if order_id.startswith("dry"):
        return {"isActive": False, "dealSize": "0", "side": "buy"}
    return _private("GET", f"/api/v1/orders/{order_id}")

# ── Entry signal ──────────────────────────────────────────────────────────────
def should_buy(price: float, fast_ema, slow_ema) -> bool:
    if fast_ema is None or slow_ema is None:
        return False
    trend_up = fast_ema > slow_ema          # short EMA above long EMA
    pullback = price < fast_ema * 0.997     # price dipped 0.3 % below fast EMA
    return trend_up and pullback

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    if not API_KEY or not API_SECRET or not API_PASSPHRASE:
        raise RuntimeError("Missing KuCoin API credentials in .env")

    state = load_state()
    log(f"Bot started | symbol={SYMBOL} | live={LIVE_TRADING} | "
        f"target={TARGET_USDT} USDT | daily_losses={state['daily_losses']}")

    while True:
        try:
            # ── 1. Market snapshot ─────────────────────────────────────────
            level1      = get_level1(SYMBOL)
            price       = level1["price"]
            best_bid    = level1["bestBid"]
            best_ask    = level1["bestAsk"]

            closes      = get_recent_closes(SYMBOL, "1min", 80)
            fast_ema    = ema(closes, 9)
            slow_ema    = ema(closes, 21)

            usdt_avail, base_avail = get_both_balances()
            total_est   = estimate_total(usdt_avail, base_avail, price)

            info        = get_symbol_info(SYMBOL)
            min_base    = info["baseMinSize"]
            base_inc    = info["baseIncrement"]

            log(f"price={price:.6f}  fast_ema={fast_ema or 0:.6f}  "
                f"slow_ema={slow_ema or 0:.6f}  "
                f"total≈{total_est:.4f}  usdt={usdt_avail:.4f}  "
                f"{BASE_COIN}={base_avail:.6f}  "
                f"losses_today={state['daily_losses']}/{MAX_DAILY_LOSSES}")

            # ── 2. Target reached — just hold, no more trades ──────────────
            if total_est >= TARGET_USDT:
                log(f"🎯 TARGET REACHED: {total_est:.4f} >= {TARGET_USDT:.2f}. "
                    f"Bot idle. Take profit manually!")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── 3. Safety: wallet too small ────────────────────────────────
            if total_est < MIN_WALLET_USDT:
                log(f"⛔ Wallet {total_est:.4f} < min {MIN_WALLET_USDT}. "
                    f"Halting to protect funds.")
                time.sleep(CHECK_INTERVAL * 10)
                continue

            # ── 4. Daily loss cap ──────────────────────────────────────────
            if state["daily_losses"] >= MAX_DAILY_LOSSES:
                log("🛑 Daily loss limit hit. Resting until tomorrow.")
                time.sleep(CHECK_INTERVAL * 2)
                continue

            # ── 5. In position — manage open trade ─────────────────────────
            if state["in_position"]:
                entry  = float(state["entry_price"])
                pnl    = (price - entry) / entry

                # Check if TP limit order filled
                tp_id  = state.get("tp_order_id")
                sl_id  = state.get("sl_order_id")

                tp_filled = sl_filled = False

                if tp_id and not tp_id.startswith("dry"):
                    tp_order = get_order(tp_id)
                    tp_filled = not tp_order.get("isActive", True)

                if sl_id and not sl_id.startswith("dry"):
                    sl_order  = get_order(sl_id)
                    sl_filled = not sl_order.get("isActive", True)

                # Dry run: simulate fill by PnL thresholds
                if LIVE_TRADING is False:
                    tp_filled = pnl >= TAKE_PROFIT_PCT
                    sl_filled = pnl <= -STOP_LOSS_PCT

                if tp_filled:
                    log(f"✅ Take-profit filled | pnl≈{pnl:.4%}")
                    if sl_id:
                        cancel_order(sl_id)
                    state.update({"in_position": False, "entry_price": None,
                                  "entry_size": None, "tp_order_id": None,
                                  "sl_order_id": None})
                    save_state(state)

                elif sl_filled:
                    log(f"🔴 Stop-loss filled | pnl≈{pnl:.4%}")
                    if tp_id:
                        cancel_order(tp_id)
                    state["daily_losses"] += 1
                    state.update({"in_position": False, "entry_price": None,
                                  "entry_size": None, "tp_order_id": None,
                                  "sl_order_id": None})
                    save_state(state)
                    log(f"Daily losses now: {state['daily_losses']}/{MAX_DAILY_LOSSES}")

                else:
                    log(f"📊 Holding | pnl≈{pnl:.4%} | "
                        f"TP={entry * (1 + TAKE_PROFIT_PCT):.6f}  "
                        f"SL={entry * (1 - STOP_LOSS_PCT):.6f}")

            # ── 6. No position — look for entry ────────────────────────────
            else:
                # Recover orphaned base coin position
                if base_avail > min_base and not state["in_position"]:
                    log(f"♻️  Recovered {base_avail:.6f} {BASE_COIN} from wallet.")
                    state["in_position"] = True
                    state["entry_price"] = price
                    state["entry_size"]  = base_avail
                    save_state(state)

                elif usdt_avail >= MIN_USDT_TO_TRADE and should_buy(price, fast_ema, slow_ema):
                    spend = min(usdt_avail * 0.98, MAX_USDT_PER_TRADE)

                    if spend < MIN_USDT_TO_TRADE:
                        log("Spend too small — skipping.")
                    else:
                        # Place limit buy just below best bid (maker)
                        buy_price  = round(best_bid * 0.9995, 6)   # 0.05 % below bid
                        raw_size   = spend / buy_price

                        # Round down to base increment
                        decimals   = len(base_inc.rstrip("0").split(".")[-1]) \
                                     if "." in base_inc else 0
                        size       = round(raw_size - (raw_size % float(base_inc)),
                                           decimals)

                        if size < min_base:
                            log(f"Size {size} below min {min_base} — skipping.")
                        else:
                            buy_price_str = q6(buy_price)
                            size_str      = q6(size)

                            res = limit_buy(SYMBOL, buy_price_str, size_str)
                            buy_oid = res.get("orderId", "dry-buy")

                            # Place TP and SL limit sells immediately
                            tp_price = round(buy_price * (1 + TAKE_PROFIT_PCT), 6)
                            sl_price = round(buy_price * (1 - STOP_LOSS_PCT),   6)

                            tp_res = limit_sell(SYMBOL, q6(tp_price), size_str)
                            sl_res = limit_sell(SYMBOL, q6(sl_price), size_str)

                            state.update({
                                "in_position":  True,
                                "entry_price":  buy_price,
                                "entry_size":   size,
                                "buy_order_id": buy_oid,
                                "tp_order_id":  tp_res.get("orderId", "dry-tp"),
                                "sl_order_id":  sl_res.get("orderId", "dry-sl"),
                            })
                            save_state(state)

                            log(f"📥 Limit buy placed | "
                                f"price={buy_price_str}  size={size_str}  "
                                f"TP={q6(tp_price)}  SL={q6(sl_price)}")
                else:
                    log("⏳ No entry signal")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()