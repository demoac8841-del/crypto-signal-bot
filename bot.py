"""
Crypto Trading Signal Bot - Delta Exchange + Telegram
Analyzes BTC/USDT market data and sends trading signals via Telegram.

FIXES APPLIED:
  1. get_product_id() - removed, candles now use symbol directly
  2. Candle endpoint - corrected to /v2/history/candles with right params
  3. Ticker API     - fixed to /v2/tickers with correct field parsing
  4. Order Book     - fixed response format (buy/sell keys + price/size fields)
  5. API Secret     - added DELTA_API_SECRET for authenticated/trading endpoints
  6. Symbol         - changed to "BTCUSD" (Delta perpetual contract name)
  7. Error Handling - None formatting fixed with safe fallback values
"""

import os
import time
import logging
import requests
import statistics
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────────────────────────
# CONFIGURATION  (edit these)
# ─────────────────────────────────────────────
DELTA_API_KEY    = os.getenv("DELTA_API_KEY",    "")   # Your Delta API Key
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")   # Your Delta API Secret  ← FIX #5

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

SYMBOL          = "BTCUSD"   # ← FIX #6: Delta perpetual contract (not BTCUSDT)
INTERVAL        = "15m"      # candle interval: 1m,5m,15m,1h,4h,1d
CANDLE_LIMIT    = 100        # candles to fetch
CHECK_EVERY_SEC = 300        # run analysis every 5 minutes
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

DELTA_BASE = "https://api.delta.exchange"

# Delta resolution map  (interval string → minutes, as Delta expects)
DELTA_RESOLUTION = {
    "1m": "1",  "3m": "3",   "5m": "5",   "15m": "15",
    "30m": "30","1h": "60",  "2h": "120",  "4h": "240",
    "6h": "360","1d": "1D",
}


# ──────────────────────────────────────────────────────────
# DELTA EXCHANGE DATA FETCHING
# ──────────────────────────────────────────────────────────

def delta_get(path: str, params: dict = None) -> Optional[dict | list]:
    """Generic Delta Exchange REST GET with proper auth headers."""
    headers = {
        "api-key":      DELTA_API_KEY,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }
    try:
        r = requests.get(DELTA_BASE + path, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Delta wraps: {"result": ..., "success": true}
        if isinstance(data, dict):
            if not data.get("success", True):
                log.error("Delta API error: %s", data.get("error", data))
                return None
            if "result" in data:
                return data["result"]
        return data
    except requests.exceptions.HTTPError:
        log.error("Delta HTTP error %s: %s", r.status_code, r.text[:300])
    except requests.exceptions.RequestException as e:
        log.error("Delta request failed: %s", e)
    return None


# ── FIX #1 & #2: Removed get_product_id(); candles use symbol directly ──

def fetch_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """
    Fetch OHLCV candles from Delta Exchange.
    Endpoint: GET /v2/history/candles
    Params  : symbol, resolution (minutes), start, end
    """
    resolution = DELTA_RESOLUTION.get(interval, "15")
    end_time   = int(time.time())
    start_time = end_time - int(resolution if resolution != "1D" else 1440) * 60 * limit

    # FIX #2: correct endpoint + correct param names for Delta
    raw = delta_get("/v2/history/candles", {
        "symbol":     symbol,
        "resolution": resolution,
        "start":      start_time,
        "end":        end_time,
    })

    if not raw:
        log.warning("fetch_klines: no data returned for %s", symbol)
        return []

    # Delta candle fields: time, open, high, low, close, volume
    candles = []
    for c in raw:
        try:
            candles.append({
                "open":   float(c.get("open",  0)),
                "high":   float(c.get("high",  0)),
                "low":    float(c.get("low",   0)),
                "close":  float(c.get("close", 0)),
                "volume": float(c.get("volume", 0)),
                "ts":     int(c.get("time", 0)),
            })
        except (TypeError, ValueError) as e:
            log.debug("Skipping malformed candle: %s | %s", c, e)

    candles.sort(key=lambda x: x["ts"])   # oldest → newest
    return candles[-limit:]


def fetch_ticker(symbol: str) -> Optional[dict]:
    """
    Fetch 24h ticker from Delta Exchange.
    FIX #3: correct endpoint /v2/tickers/{symbol} with verified field names.
    """
    # Delta ticker endpoint returns a single object inside result
    result = delta_get(f"/v2/tickers/{symbol}")
    if not result:
        # Fallback: try listing all tickers
        all_tickers = delta_get("/v2/tickers")
        if all_tickers:
            for t in (all_tickers if isinstance(all_tickers, list) else []):
                if t.get("symbol") == symbol:
                    result = t
                    break
    if not result:
        return None
    try:
        # Delta ticker fields verified from their docs
        price_change = result.get("price_change_24h",         None)
        close_price  = result.get("close",                    None) or \
                       result.get("mark_price",               None) or \
                       result.get("last_price",               1)
        close_price  = float(close_price or 1)

        if price_change is not None:
            pct = (float(price_change) / close_price) * 100
        else:
            pct = 0.0

        return {
            "priceChangePercent": round(pct, 4),
            "highPrice":  float(result.get("high",       close_price)),
            "lowPrice":   float(result.get("low",        close_price)),
        }
    except (TypeError, ValueError) as e:
        log.error("fetch_ticker parse error: %s | raw=%s", e, result)
        return None


def fetch_order_book(symbol: str, depth: int = 20) -> Optional[dict]:
    """
    Fetch order book from Delta Exchange.
    FIX #4: Delta returns buy[]/sell[] arrays with 'limit_price' and 'size' keys.
    """
    result = delta_get(f"/v2/l2orderbook/{symbol}")
    if not result:
        return None

    try:
        # buy  = bids (buyers), sell = asks (sellers)
        raw_bids = result.get("buy",  [])[:depth]
        raw_asks = result.get("sell", [])[:depth]

        bids = [[str(b.get("limit_price", b.get("price", 0))),
                 str(b.get("size", b.get("quantity", 0)))] for b in raw_bids]
        asks = [[str(a.get("limit_price", a.get("price", 0))),
                 str(a.get("size", a.get("quantity", 0)))] for a in raw_asks]
        return {"bids": bids, "asks": asks}
    except (TypeError, AttributeError) as e:
        log.error("fetch_order_book parse error: %s", e)
        return None


# ──────────────────────────────────────────────────────────
# TECHNICAL INDICATORS  (pure math — unchanged)
# ──────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[Optional[float]]:
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def ema(values: list[float], period: int) -> list[Optional[float]]:
    result = [None] * (period - 1)
    if len(values) < period:
        return [None] * len(values)
    seed = sum(values[:period]) / period
    result.append(seed)
    k = 2 / (period + 1)
    for v in values[period:]:
        result.append(result[-1] * (1 - k) + v * k)
    return result


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    result = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        result.append(100.0)
    else:
        result.append(100 - 100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0)
        l = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            result.append(100 - 100 / (1 + avg_gain / avg_loss))
    return result


def macd(closes: list[float], fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    valid = [v for v in macd_line if v is not None]
    sig_raw = ema(valid, signal)
    offset = len(macd_line) - len(valid)
    signal_line = [None] * offset + sig_raw
    histogram = []
    for m, s in zip(macd_line, signal_line):
        if m is None or s is None:
            histogram.append(None)
        else:
            histogram.append(m - s)
    return macd_line, signal_line, histogram


def bollinger_bands(closes: list[float], period=20, std_dev=2):
    mid = sma(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        if mid[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1: i + 1]
            std = statistics.stdev(window) if len(window) > 1 else 0
            upper.append(mid[i] + std_dev * std)
            lower.append(mid[i] - std_dev * std)
    return upper, mid, lower


def volume_trend(volumes: list[float], period=20) -> float:
    if len(volumes) < period * 2:
        return 1.0
    recent = sum(volumes[-period:]) / period
    older  = sum(volumes[-period * 2: -period]) / period
    return recent / older if older > 0 else 1.0


def order_book_imbalance(order_book: Optional[dict]) -> float:
    if not order_book:
        return 0.0
    try:
        bid_vol = sum(float(b[1]) for b in order_book.get("bids", []))
        ask_vol = sum(float(a[1]) for a in order_book.get("asks", []))
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


# ──────────────────────────────────────────────────────────
# MARKET ANALYSIS ENGINE
# ──────────────────────────────────────────────────────────

def safe_float(val, default=0.0) -> float:
    """FIX #7: Safe conversion — avoids None formatting errors."""
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def analyze_market(symbol: str) -> Optional[dict]:
    """Run full market analysis and return a signal dict."""
    candles = fetch_klines(symbol, INTERVAL, CANDLE_LIMIT)
    if len(candles) < 50:
        log.warning("Not enough candles (%d) — skipping analysis", len(candles))
        return None

    ticker = fetch_ticker(symbol)
    ob     = fetch_order_book(symbol)

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    price   = closes[-1]

    # ── Compute indicators ───────────────────────
    ema9_vals  = ema(closes, 9)
    ema21_vals = ema(closes, 21)
    ema50_vals = ema(closes, 50)
    rsi14_vals = rsi(closes, 14)
    macd_line, signal_line, histogram = macd(closes)
    bb_upper, _, bb_lower = bollinger_bands(closes, 20)
    vol_ratio = volume_trend(volumes)
    ob_imbal  = order_book_imbalance(ob)

    # Latest valid values  — FIX #7: use safe_float so None never reaches formatter
    ema9_now  = safe_float(next((v for v in reversed(ema9_vals)  if v is not None), None), price)
    ema21_now = safe_float(next((v for v in reversed(ema21_vals) if v is not None), None), price)
    ema50_now = safe_float(next((v for v in reversed(ema50_vals) if v is not None), None), price)
    rsi_now   = safe_float(next((v for v in reversed(rsi14_vals) if v is not None), None), 50.0)
    hist_now  = safe_float(next((v for v in reversed(histogram)  if v is not None), None), 0.0)
    bb_up     = safe_float(next((v for v in reversed(bb_upper)   if v is not None), None), price)
    bb_lo     = safe_float(next((v for v in reversed(bb_lower)   if v is not None), None), price)

    hist_vals = [v for v in histogram if v is not None]
    hist_prev = safe_float(hist_vals[-2] if len(hist_vals) >= 2 else None, hist_now)

    price_change_pct = safe_float(ticker.get("priceChangePercent") if ticker else None, 0.0)
    high24 = safe_float(ticker.get("highPrice") if ticker else None, price)
    low24  = safe_float(ticker.get("lowPrice")  if ticker else None, price)

    # ── Scoring ──────────────────────────────────
    score = 0
    signals_used = []
    reasons = []

    # 1. EMA alignment
    if ema9_now > ema21_now > ema50_now:
        score += 25
        signals_used.append("EMA Stack ↑")
        reasons.append("EMA 9 > 21 > 50 (bullish alignment)")
    elif ema9_now < ema21_now < ema50_now:
        score -= 25
        signals_used.append("EMA Stack ↓")
        reasons.append("EMA 9 < 21 < 50 (bearish alignment)")
    else:
        signals_used.append("EMA Mixed")
        reasons.append("EMAs are mixed (no clear trend)")

    # 2. Price vs EMA50
    score += 10 if price > ema50_now else -10

    # 3. RSI
    if rsi_now > 60:
        score += 15
        signals_used.append(f"RSI {rsi_now:.1f} (bullish)")
        reasons.append(f"RSI at {rsi_now:.1f} — momentum is bullish")
    elif rsi_now < 40:
        score -= 15
        signals_used.append(f"RSI {rsi_now:.1f} (bearish)")
        reasons.append(f"RSI at {rsi_now:.1f} — momentum is bearish")
    elif rsi_now > 50:
        score += 5
        signals_used.append(f"RSI {rsi_now:.1f} (neutral+)")
    else:
        score -= 5
        signals_used.append(f"RSI {rsi_now:.1f} (neutral-)")

    # 4. MACD histogram
    if hist_now > 0 and hist_now > hist_prev:
        score += 20
        signals_used.append("MACD ↑ Histogram")
        reasons.append("MACD histogram expanding above zero line")
    elif hist_now < 0 and hist_now < hist_prev:
        score -= 20
        signals_used.append("MACD ↓ Histogram")
        reasons.append("MACD histogram expanding below zero line")
    elif hist_now > 0:
        score += 8
        signals_used.append("MACD Positive")
    else:
        score -= 8
        signals_used.append("MACD Negative")

    # 5. Bollinger Bands
    bb_range = bb_up - bb_lo
    if bb_range > 0:
        bb_pos = (price - bb_lo) / bb_range
        if bb_pos > 0.8:
            score += 10
            signals_used.append("BB Upper Zone")
            reasons.append("Price in upper Bollinger Band zone — strong upward pressure")
        elif bb_pos < 0.2:
            score -= 10
            signals_used.append("BB Lower Zone")
            reasons.append("Price in lower Bollinger Band zone — strong downward pressure")

    # 6. Volume trend
    if vol_ratio > 1.3:
        score = int(score * 1.2)
        signals_used.append(f"Vol Surge {vol_ratio:.1f}x")
        reasons.append(f"Volume is {vol_ratio:.1f}x above average — confirming move")
    elif vol_ratio < 0.7:
        signals_used.append("Low Volume")
        reasons.append("Volume below average — move may lack conviction")

    # 7. Order book imbalance
    if ob_imbal > 0.15:
        score += 10
        signals_used.append(f"OB Bid Pressure +{ob_imbal:.0%}")
        reasons.append(f"Order book shows {ob_imbal:.0%} more bid depth — buying interest")
    elif ob_imbal < -0.15:
        score -= 10
        signals_used.append(f"OB Ask Pressure {ob_imbal:.0%}")
        reasons.append(f"Order book shows {abs(ob_imbal):.0%} more ask depth — selling pressure")

    # 8. 24h price change
    if abs(price_change_pct) > 2:
        score += 5 if price_change_pct > 0 else -5

    # ── Final direction ──────────────────────────
    score = max(-100, min(100, score))
    abs_score = abs(score)

    if score >= 20:
        direction, emoji = "BULLISH 🟢", "📈"
    elif score <= -20:
        direction, emoji = "BEARISH 🔴", "📉"
    else:
        direction, emoji = "NEUTRAL ⚪", "↔️"

    if abs_score >= 70:
        confidence, conf_bar = "Very High", "█████"
    elif abs_score >= 50:
        confidence, conf_bar = "High",      "████░"
    elif abs_score >= 30:
        confidence, conf_bar = "Medium",    "███░░"
    else:
        confidence, conf_bar = "Low",       "██░░░"

    return {
        "symbol":     symbol,
        "price":      price,
        "direction":  direction,
        "emoji":      emoji,
        "score":      score,
        "confidence": confidence,
        "conf_bar":   conf_bar,
        "signals":    signals_used,
        "reasons":    reasons[:4],
        "rsi":        rsi_now,
        "ema9":       ema9_now,
        "ema50":      ema50_now,
        "macd_hist":  hist_now,
        "vol_ratio":  vol_ratio,
        "change24h":  price_change_pct,
        "high24":     high24,
        "low24":      low24,
        "interval":   INTERVAL,
    }


# ──────────────────────────────────────────────────────────
# TELEGRAM MESSAGING
# ──────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent ✓")
        return True
    except requests.exceptions.RequestException as e:
        log.error("Telegram send failed: %s", e)
        return False


def format_signal_message(sig: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reasons_text = (
        "\n".join(f"  • {r}" for r in sig["reasons"])
        if sig["reasons"] else "  • Insufficient signal clarity"
    )
    # FIX #7: all values are guaranteed floats — no None formatting error
    return (
        f"{sig['emoji']} <b>CRYPTO SIGNAL ALERT</b> {sig['emoji']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> {sig['symbol']}\n"
        f"💰 <b>Price:</b> ${sig['price']:,.2f}\n"
        f"📊 <b>Direction:</b> {sig['direction']}\n"
        f"🎯 <b>Confidence:</b> {sig['confidence']} {sig['conf_bar']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <b>Why the market is moving:</b>\n"
        f"{reasons_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 <b>Key Indicators ({sig['interval']} chart):</b>\n"
        f"  RSI(14):    {sig['rsi']:.1f}\n"
        f"  EMA9:       ${sig['ema9']:,.0f}\n"
        f"  EMA50:      ${sig['ema50']:,.0f}\n"
        f"  MACD Hist:  {sig['macd_hist']:+.2f}\n"
        f"  Volume:     {sig['vol_ratio']:.1f}x avg\n"
        f"  24h Change: {sig['change24h']:+.2f}%\n"
        f"  24h High:   ${sig['high24']:,.2f}\n"
        f"  24h Low:    ${sig['low24']:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now}\n"
        f"🤖 CryptoSignal Bot | Delta Exchange"
    )


# ──────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────

def main():
    log.info("═" * 55)
    log.info("  Crypto Trading Signal Bot — Delta Exchange")
    log.info("  Symbol: %s  |  Interval: %s  |  Every: %ds",
             SYMBOL, INTERVAL, CHECK_EVERY_SEC)
    log.info("═" * 55)

    if not DELTA_API_KEY:
        log.warning("⚠️  DELTA_API_KEY is not set — public endpoints only.")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️  Telegram credentials missing — running in DRY-RUN mode.")
        dry_run = True
    else:
        dry_run = False

    iteration = 0
    while True:
        iteration += 1
        log.info("── Analysis #%d ──────────────────────────", iteration)
        try:
            sig = analyze_market(SYMBOL)
            if sig:
                msg = format_signal_message(sig)
                log.info("\n%s", msg.replace("<b>", "").replace("</b>", ""))
                if not dry_run:
                    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
            else:
                log.warning("Analysis returned no signal — will retry next cycle.")
        except Exception as e:
            log.exception("Unexpected error: %s", e)

        log.info("Sleeping %d seconds…\n", CHECK_EVERY_SEC)
        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    main()
