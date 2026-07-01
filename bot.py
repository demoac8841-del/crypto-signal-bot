"""
Crypto Trading Signal Bot - Binance + Telegram
Analyzes BTC/USDT market data and sends trading signals via Telegram.
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
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")   # Set via env var
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")     # Set via env var

SYMBOL          = "BTCUSDT"
INTERVAL        = "15m"          # candle interval
CANDLE_LIMIT    = 100            # candles to fetch
CHECK_EVERY_SEC = 300            # run analysis every 5 minutes
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"


# ──────────────────────────────────────────────────────────
# BINANCE DATA FETCHING
# ──────────────────────────────────────────────────────────

def binance_get(path: str, params: dict = None) -> Optional[dict | list]:
    """Generic Binance REST GET with error handling."""
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        r = requests.get(BINANCE_BASE + path, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        log.error("Binance HTTP error %s: %s", r.status_code, r.text[:200])
    except requests.exceptions.RequestException as e:
        log.error("Binance request failed: %s", e)
    return None


def fetch_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    """Fetch OHLCV candles and return list of dicts."""
    raw = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not raw:
        return []
    candles = []
    for c in raw:
        candles.append({
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
            "ts":     c[0],
        })
    return candles


def fetch_ticker(symbol: str) -> Optional[dict]:
    """Fetch 24-hour ticker stats."""
    return binance_get("/api/v3/ticker/24hr", {"symbol": symbol})


def fetch_order_book(symbol: str, depth: int = 20) -> Optional[dict]:
    """Fetch order book."""
    return binance_get("/api/v3/depth", {"symbol": symbol, "limit": depth})


# ──────────────────────────────────────────────────────────
# TECHNICAL INDICATORS
# ──────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> list[float]:
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def ema(values: list[float], period: int) -> list[float]:
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
        rs = avg_gain / avg_loss
        result.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0)
        l = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - 100 / (1 + rs))
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
    # re-align signal to full length
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
            std = statistics.stdev(window)
            upper.append(mid[i] + std_dev * std)
            lower.append(mid[i] - std_dev * std)
    return upper, mid, lower


def volume_trend(volumes: list[float], period=20) -> float:
    """Return ratio of recent avg volume vs older avg volume."""
    if len(volumes) < period * 2:
        return 1.0
    recent = sum(volumes[-period:]) / period
    older  = sum(volumes[-period * 2: -period]) / period
    return recent / older if older > 0 else 1.0


def order_book_imbalance(order_book: dict) -> float:
    """Positive = more bids (buying pressure), negative = more asks."""
    if not order_book:
        return 0.0
    bid_vol = sum(float(b[1]) for b in order_book.get("bids", []))
    ask_vol = sum(float(a[1]) for a in order_book.get("asks", []))
    total = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


# ──────────────────────────────────────────────────────────
# MARKET ANALYSIS ENGINE
# ──────────────────────────────────────────────────────────

def analyze_market(symbol: str) -> Optional[dict]:
    """Run full market analysis and return a signal dict."""
    candles = fetch_klines(symbol, INTERVAL, CANDLE_LIMIT)
    if len(candles) < 50:
        log.warning("Not enough candles (%d)", len(candles))
        return None

    ticker    = fetch_ticker(symbol)
    ob        = fetch_order_book(symbol)

    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    price = closes[-1]

    # ── Indicators ──────────────────────────────
    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    macd_line, signal_line, histogram = macd(closes)
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20)
    vol_ratio   = volume_trend(volumes)
    ob_imbal    = order_book_imbalance(ob)

    # Latest valid values
    ema9_now  = next((v for v in reversed(ema9)  if v is not None), None)
    ema21_now = next((v for v in reversed(ema21) if v is not None), None)
    ema50_now = next((v for v in reversed(ema50) if v is not None), None)
    rsi_now   = next((v for v in reversed(rsi14) if v is not None), None)
    macd_now  = next((v for v in reversed(macd_line) if v is not None), None)
    sig_now   = next((v for v in reversed(signal_line) if v is not None), None)
    hist_now  = next((v for v in reversed(histogram) if v is not None), None)
    bb_up     = next((v for v in reversed(bb_upper) if v is not None), None)
    bb_lo     = next((v for v in reversed(bb_lower) if v is not None), None)

    # Prev MACD histogram for crossover detection
    hist_vals   = [v for v in histogram if v is not None]
    hist_prev   = hist_vals[-2] if len(hist_vals) >= 2 else hist_now

    # 24h change
    price_change_pct = float(ticker["priceChangePercent"]) if ticker else 0.0
    high24   = float(ticker["highPrice"])  if ticker else price
    low24    = float(ticker["lowPrice"])   if ticker else price

    # ── Scoring system (each signal adds ± points) ──
    score = 0          # -100 … +100 → negative = bearish, positive = bullish
    signals_used = []
    reasons = []

    # 1. EMA alignment
    if ema9_now and ema21_now and ema50_now:
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
    if ema50_now:
        if price > ema50_now:
            score += 10
        else:
            score -= 10

    # 3. RSI
    if rsi_now is not None:
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

    # 4. MACD crossover / histogram
    if hist_now is not None and hist_prev is not None:
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

    # 5. Bollinger Bands position
    if bb_up and bb_lo:
        bb_range = bb_up - bb_lo
        bb_pos   = (price - bb_lo) / bb_range if bb_range > 0 else 0.5
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
        # amplify existing signal direction
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
        if price_change_pct > 0:
            score += 5
        else:
            score -= 5

    # ── Final direction ──────────────────────────
    score = max(-100, min(100, score))
    abs_score = abs(score)

    if score >= 20:
        direction = "BULLISH 🟢"
        emoji = "📈"
    elif score <= -20:
        direction = "BEARISH 🔴"
        emoji = "📉"
    else:
        direction = "NEUTRAL ⚪"
        emoji = "↔️"

    if abs_score >= 70:
        confidence = "Very High"
        conf_bar   = "█████"
    elif abs_score >= 50:
        confidence = "High"
        conf_bar   = "████░"
    elif abs_score >= 30:
        confidence = "Medium"
        conf_bar   = "███░░"
    else:
        confidence = "Low"
        conf_bar   = "██░░░"

    return {
        "symbol":       symbol,
        "price":        price,
        "direction":    direction,
        "emoji":        emoji,
        "score":        score,
        "confidence":   confidence,
        "conf_bar":     conf_bar,
        "signals":      signals_used,
        "reasons":      reasons[:4],  # top 4 reasons
        "rsi":          rsi_now,
        "ema9":         ema9_now,
        "ema50":        ema50_now,
        "macd_hist":    hist_now,
        "vol_ratio":    vol_ratio,
        "change24h":    price_change_pct,
        "high24":       high24,
        "low24":        low24,
        "interval":     INTERVAL,
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
    reasons_text = "\n".join(f"  • {r}" for r in sig["reasons"]) if sig["reasons"] else "  • Insufficient signal clarity"

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
        f"🤖 CryptoSignal Bot | Auto-analysis"
    )


# ──────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────

def main():
    log.info("═" * 50)
    log.info("  Crypto Trading Signal Bot starting…")
    log.info("  Symbol:   %s | Interval: %s", SYMBOL, INTERVAL)
    log.info("  Checking every %d seconds", CHECK_EVERY_SEC)
    log.info("═" * 50)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        log.warning("    Set them as environment variables and restart.")
        log.warning("    Running in DRY-RUN mode (no messages sent).")
        dry_run = True
    else:
        dry_run = False

    iteration = 0
    while True:
        iteration += 1
        log.info("── Analysis #%d ──────────────────", iteration)
        try:
            sig = analyze_market(SYMBOL)
            if sig:
                msg = format_signal_message(sig)
                log.info("\n%s", msg.replace("<b>", "").replace("</b>", ""))
                if not dry_run:
                    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
            else:
                log.warning("Analysis returned no signal.")
        except Exception as e:
            log.exception("Unexpected error in analysis loop: %s", e)

        log.info("Sleeping %d seconds until next check…\n", CHECK_EVERY_SEC)
        time.sleep(CHECK_EVERY_SEC)

if __name__ == "__main__":
    main()
