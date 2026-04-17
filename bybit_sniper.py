import os
import time
from dotenv import load_dotenv
import ccxt
import pandas as pd
import asyncio
from datetime import timezone
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# Load environment variables
load_dotenv()

# =============================
# CONFIG
# =============================
exchange = ccxt.bybit({
    'options': {
        'defaultType': 'spot',
        'adjustForTimeDifference': True,
    },
    'enableRateLimit': True,
    'timeout': 30000,
})

# Second exchange instance for perpetual/linear markets
# Many tokens (RAVE, SIREN, MYX, COAI etc) only have perp pairs
exchange_perp = ccxt.bybit({
    'options': {
        'defaultType': 'linear',
        'adjustForTimeDifference': True,
    },
    'enableRateLimit': True,
    'timeout': 30000,
})

TIMEFRAME = '15m'
LIMIT = 50
SLEEP_TIME = 60  # scan every 1 minute

# Telegram - Load from environment
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# Track state
phase2b_watchlist = set()
alerted_today = {}  # {symbol: last_alert_timestamp}
daily_results = []  # store all alerts for daily summary

# Configurable settings (can be changed via /settings)
settings = {
    'rsi_overbought': 80,        # raised from 70 — runners run hot, don't filter too early
    'rsi_oversold': 30,
    'btc_dump_threshold': -5.0,
    'volume_multiplier': 3.0,
    'wick_ratio': 0.6,
    'min_score': 50,
    'max_alerts_per_scan': 10,
}

# =============================
# FILTER MAJOR COINS
# =============================
EXCLUDED_BASES = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE",
                  "USDT", "USDC", "TUSD", "BUSD", "DAI", "USDE", "USDP", "FDUSD", "PYUSD"}

# =============================
# TELEGRAM COMMANDS
# =============================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>CRIME PUMP SNIPER</b>\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n\n"
        "  <b>Status:</b>  🟢 Active\n"
        "  <b>Market:</b>  Bybit Spot + Perp\n"
        "  <b>Interval:</b>  15m candles\n"
        "  <b>Scan:</b>  Every 60s\n\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n\n"
        "  /scan        Manual scan\n"
        "  /watchlist   Gem watchlist\n"
        "  /runners     Today's alerts\n"
        "  /summary     Daily report\n"
        "  /settings    Configure\n"
        "  /reset       Clear alerts\n\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning...")
    await run_scan(context.bot)
    await update.message.reply_text("✅ Done")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if phase2b_watchlist:
        items = "\n".join(f"  • {s}" for s in sorted(phase2b_watchlist))
        msg = f"<b>Watchlist</b>\n▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n{items}"
    else:
        msg = "<b>Watchlist</b>\n▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n  Empty"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def runners_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if alerted_today:
        items = "\n".join(f"  • {s}" for s in sorted(alerted_today.keys()))
        msg = f"<b>Today's Runners</b>\n▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n{items}"
    else:
        msg = "<b>Today's Runners</b>\n▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n  None yet"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerted_today.clear()
    daily_results.clear()
    await update.message.reply_text("✅ Alerts cleared")

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not daily_results:
        await update.message.reply_text("No signals today yet.")
        return
    msg = build_daily_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        msg = (
            "<b>Settings</b>\n"
            "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n\n"
            f"  <b>rsi_overbought</b>     {settings['rsi_overbought']}\n"
            f"  <b>rsi_oversold</b>       {settings['rsi_oversold']}\n"
            f"  <b>btc_dump_threshold</b> {settings['btc_dump_threshold']}%\n"
            f"  <b>volume_multiplier</b>  {settings['volume_multiplier']}x\n"
            f"  <b>wick_ratio</b>         {settings['wick_ratio']}\n"
            f"  <b>min_score</b>          {settings['min_score']}\n"
            f"  <b>max_alerts_per_scan</b> {settings['max_alerts_per_scan']}\n\n"
            "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            "<code>/settings key value</code>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    if len(args) == 2:
        key, value = args[0], args[1]
        if key in settings:
            try:
                settings[key] = float(value)
                await update.message.reply_text(f"✅ <b>{key}</b> → {settings[key]}", parse_mode=ParseMode.HTML)
            except ValueError:
                await update.message.reply_text("❌ Value must be a number.")
        else:
            await update.message.reply_text(f"❌ Unknown: {key}")
    else:
        await update.message.reply_text("<code>/settings key value</code>", parse_mode=ParseMode.HTML)

# =============================
# FETCH DATA
# =============================
def get_data(symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    return df

def get_btc_data():
    ohlcv = exchange.fetch_ohlcv("BTC/USDT", timeframe=TIMEFRAME, limit=5)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    return df

# =============================
# RSI CALCULATION
# =============================
def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

# =============================
# BTC SENTIMENT CHECK
# =============================
def is_btc_dumping():
    try:
        df = get_btc_data()
        price_now = df['close'].iloc[-1]
        price_yesterday = df['close'].iloc[-2]
        btc_change = (price_now - price_yesterday) / (price_yesterday + 1e-9) * 100
        return btc_change <= settings['btc_dump_threshold'], btc_change
    except Exception:
        return False, 0.0

# =============================
# FAKE PUMP / WICK FILTER
# =============================
def is_fake_pump(df):
    last = df.iloc[-1]
    upper_wick = last['high'] - max(last['close'], last['open'])
    total_range = last['high'] - last['low']

    if total_range == 0:
        return False

    # Fake pump: huge upper wick relative to body
    wick_ratio = upper_wick / (total_range + 1e-9)
    return wick_ratio > settings['wick_ratio']

# =============================
# SUPPORT / RESISTANCE DETECTION
# =============================
def find_support_resistance(df):
    highs = df['high'].values
    lows = df['low'].values
    close = df['close'].iloc[-1]

    # Find resistance: recent swing highs
    resistances = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistances.append(highs[i])

    # Find support: recent swing lows
    supports = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            supports.append(lows[i])

    # Get nearest support below price, nearest resistance above price
    support = max([s for s in supports if s < close], default=df['low'].min())
    resistance = min([r for r in resistances if r > close], default=df['high'].max())

    return support, resistance

# =============================
# ATR CALCULATION
# =============================
def calculate_atr(df, period=14):
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)

    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=1).mean()
    return atr.iloc[-1]

# =============================
# ENTRY / TP / SL SUGGESTIONS
# =============================
def calculate_trade_levels(df):
    close = df['close'].iloc[-1]
    atr = calculate_atr(df)
    support, resistance = find_support_resistance(df)

    entry = close
    stop_loss = max(support, close - (atr * 1.5))
    tp1 = close + (atr * 2)
    tp2 = min(resistance, close + (atr * 3))
    tp3 = close + (atr * 4.5)

    risk = entry - stop_loss
    reward = tp1 - entry
    rr_ratio = reward / (risk + 1e-9)

    return {
        'entry': entry,
        'sl': stop_loss,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'atr': atr,
        'rr': rr_ratio,
        'support': support,
        'resistance': resistance,
    }

# =============================
# PHASE 2B SCORE
# =============================
def score_phase_2b(df):
    score = 0

    vol_3 = df['volume'].tail(3).mean()
    vol_10 = df['volume'].tail(10).mean()

    if vol_3 > vol_10 * 2:
        score += 30

    if all(x < y for x, y in zip(df['volume'].tail(5), df['volume'].tail(5)[1:])):
        score += 20

    price_now = df['close'].iloc[-1]
    price_prev = df['close'].iloc[-5]
    change = abs(price_now - price_prev) / (price_prev + 1e-9)

    if change < 0.05:
        score += 20

    recent = df.tail(6)
    range_val = (recent['high'].max() - recent['low'].min()) / (recent['low'].min() + 1e-9)

    if range_val < 0.05:
        score += 15

    low_30 = df['low'].min()
    high_30 = df['high'].max()
    position = (price_now - low_30) / (high_30 - low_30 + 1e-9)

    if position < 0.4:
        score += 15

    return score

# =============================
# EARLY VOLUME SURGE
# =============================
def volume_accelerating(df):
    vols = df['volume'].tail(5).values
    return all(x < y for x, y in zip(vols, vols[1:]))

def early_explosion(df):
    vol_now = df['volume'].iloc[-1]
    vol_prev = df['volume'].iloc[-2]

    price_now = df['close'].iloc[-1]
    price_prev = df['close'].iloc[-2]

    volume_jump = vol_now > vol_prev * 1.8
    price_still_low = abs(price_now - price_prev) / (price_prev + 1e-9) < 0.04

    return volume_jump and price_still_low

# =============================
# EARLY ACCUMULATION SETUP
# =============================
def detect_early_accumulation(symbol, df):
    if len(df) < 10:
        return None

    vols = df['volume'].values
    vol_trending_up = (
        vols[-1] > vols[-2] > vols[-3]
    )

    vol_7d_avg = df['volume'].iloc[-8:-1].mean()
    vol_ratio_7d = vols[-1] / (vol_7d_avg + 1e-9)

    price_today = df['close'].iloc[-1]
    price_yesterday = df['close'].iloc[-2]
    price_change = (price_today - price_yesterday) / (price_yesterday + 1e-9) * 100

    last = df.iloc[-1]
    hl_range_pct = (last['high'] - last['low']) / (last['close'] + 1e-9) * 100

    if (
        vol_trending_up
        and vol_ratio_7d >= 1.8
        and -2.0 <= price_change <= 3.0
        and hl_range_pct < 5.0
    ):
        return {
            'type': '🌀 EARLY ACCUMULATION SETUP',
            'vol_ratio': vol_ratio_7d,
            'price_change': price_change,
            'hl_range_pct': hl_range_pct,
        }

    return None

# =============================
# MOMENTUM DETECTOR (pattern from RAVE/SIREN/MYX/COAI/ORDI analysis)
# 3+ consecutive green candles with rising volume = smart money loading
# =============================
def detect_momentum(symbol, df):
    if len(df) < 10:
        return None

    # Check last 3 candles: all green + volume rising
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    three_green = (c1['close'] > c1['open'] and
                   c2['close'] > c2['open'] and
                   c3['close'] > c3['open'])

    vol_rising = (c3['volume'] > c2['volume'] > c1['volume'])

    if not (three_green and vol_rising):
        return None

    # Volume should be at least 1.3x the 7-candle average
    vol_7_avg = df['volume'].iloc[-10:-3].mean()
    vol_ratio = c3['volume'] / (vol_7_avg + 1e-9)

    if vol_ratio < 1.3:
        return None

    # Price change over the 3 candles
    price_change = (c3['close'] - c1['open']) / (c1['open'] + 1e-9) * 100

    # Reject if price already moved too much (late entry)
    if price_change > 15:
        return None

    return {
        'type': '🟢 MOMENTUM',
        'vol_ratio': vol_ratio,
        'price_change': price_change,
    }

# =============================
# BREAKOUT DETECTION
# =============================
def is_breakout(df):
    price_now = df['close'].iloc[-1]
    resistance = df['high'].tail(12).max()
    avg_volume = df['volume'][:-1].mean()
    return price_now > resistance and df['volume'].iloc[-1] > avg_volume * 2.5

# =============================
# RUNNER DETECTION (tuned for 15m candles)
# =============================
def detect_runner(symbol, df):
    if len(df) < 15:
        return None

    vol_today = df['volume'].iloc[-1]
    vol_7_avg = df['volume'].iloc[-8:-1].mean()
    vol_14_avg = df['volume'].iloc[-15:-1].mean()

    price_now = df['close'].iloc[-1]
    price_prev = df['close'].iloc[-2]
    price_change = (price_now - price_prev) / (price_prev + 1e-9) * 100

    price_open = df['open'].iloc[-1]
    intraday_change = (price_now - price_open) / (price_open + 1e-9) * 100

    vol_ratio_7 = vol_today / (vol_7_avg + 1e-9)
    vol_ratio_14 = vol_today / (vol_14_avg + 1e-9)

    vol_trending = (df['volume'].iloc[-1] > df['volume'].iloc[-2] > df['volume'].iloc[-3])

    # Volume explosion: big spike vs recent average
    if vol_ratio_7 >= settings['volume_multiplier'] and price_change > 0:
        return {
            'type': '🔥 VOLUME EXPLOSION',
            'vol_ratio': vol_ratio_7,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    # Runner: solid volume + strong price move
    if vol_ratio_7 >= 2.0 and price_change > 2:
        return {
            'type': '📈 RUNNER',
            'vol_ratio': vol_ratio_7,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    # Accumulation: volume building, price flat
    if vol_ratio_7 >= 1.5 and vol_trending and abs(price_change) < 3:
        return {
            'type': '⚡ ACCUMULATION',
            'vol_ratio': vol_ratio_7,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    # Volume breakout on longer timeframe
    if vol_ratio_14 >= 2.5 and price_change > 0:
        return {
            'type': '💥 VOLUME BREAKOUT',
            'vol_ratio': vol_ratio_14,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    return None

# =============================
# COMPOSITE SCORE
# =============================
def calculate_composite_score(df, signal_type, vol_ratio=0):
    score = 0
    rsi = calculate_rsi(df)

    # Volume component (0-30)
    if vol_ratio >= 5:
        score += 30
    elif vol_ratio >= 3:
        score += 25
    elif vol_ratio >= 2:
        score += 20
    elif vol_ratio >= 1.5:
        score += 10

    # RSI component (0-25)
    if rsi < 30:
        score += 25  # oversold = great buy zone
    elif rsi < 40:
        score += 20
    elif rsi < 50:
        score += 15
    elif rsi > 70:
        score -= 10  # overbought penalty

    # Signal type component (0-25)
    type_scores = {
        'phase2b': 25,
        'breakout': 20,
        'volume_explosion': 20,
        'daily_runner': 15,
        'runner': 15,
        'accumulation': 15,
        'volume_breakout': 15,
        'early_accumulation': 20,
        'momentum': 18,
        'early_surge': 10,
    }
    score += type_scores.get(signal_type, 10)

    # Trend component (0-20)
    price_now = df['close'].iloc[-1]
    sma_10 = df['close'].tail(10).mean()
    sma_20 = df['close'].tail(20).mean()

    if price_now > sma_10 > sma_20:
        score += 20  # strong uptrend
    elif price_now > sma_10:
        score += 10  # above short MA
    elif price_now < sma_10 < sma_20:
        score -= 5   # downtrend penalty

    return min(max(score, 0), 100)

# =============================
# FORMAT ALERT MESSAGE
# =============================
def format_alert(symbol, signal_type, df, extra_info=""):
    rsi = calculate_rsi(df)
    levels = calculate_trade_levels(df)
    composite = calculate_composite_score(
        df, signal_type,
        vol_ratio=extra_info.get('vol_ratio', 0) if isinstance(extra_info, dict) else 0
    )

    # Grade icon
    if composite >= 80:
        grade = "🟢"
    elif composite >= 65:
        grade = "🔵"
    elif composite >= 50:
        grade = "🟡"
    else:
        grade = "⚪"

    # Signal type label (short)
    type_labels = {
        'volume_explosion': '🔥 Explosion',
        'runner': '📈 Runner',
        'accumulation': '⚡ Accumulation',
        'volume_breakout': '💥 Breakout',
        'early_surge': '⚡ Surge',
        'breakout': '🚀 Breakout',
        'early_accumulation': '🌀 Early Accum',
        'momentum': '🟢 Momentum',
    }
    label = type_labels.get(signal_type, signal_type)

    # Extract base token name
    base = symbol.split("/")[0]

    # Build clean card
    vol_line = ""
    chg_line = ""
    if isinstance(extra_info, dict):
        if 'vol_ratio' in extra_info:
            vol_line = f"  <b>Volume:</b>  {extra_info['vol_ratio']:.1f}x avg\n"
        if 'price_change' in extra_info:
            chg_pct = extra_info['price_change']
            arrow = "▲" if chg_pct >= 0 else "▼"
            chg_line = f"  <b>Change:</b>  {arrow} {abs(chg_pct):.2f}%\n"

    msg = (
        f"<b>{base}</b> ({symbol})\n"
        f"  {label}\n"
        f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
        f"  <b>Score:</b>   {grade} {composite}/100\n"
        f"  <b>RSI:</b>     {rsi:.0f}\n"
        f"{vol_line}"
        f"{chg_line}"
        f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
        f"  <b>Entry:</b>   <code>${levels['entry']:.6g}</code>\n"
        f"  <b>TP1:</b>     <code>${levels['tp1']:.6g}</code>\n"
        f"  <b>TP2:</b>     <code>${levels['tp2']:.6g}</code>\n"
        f"  <b>SL:</b>      <code>${levels['sl']:.6g}</code>\n"
        f"  <b>R:R:</b>     {levels['rr']:.1f}"
    )

    return msg, composite

# =============================
# DAILY SUMMARY
# =============================
def build_daily_summary():
    if not daily_results:
        return "No signals today."

    total = len(daily_results)
    avg_score = sum(r['score'] for r in daily_results) / total
    top_signals = sorted(daily_results, key=lambda x: x['score'], reverse=True)[:5]

    by_type = {}
    for r in daily_results:
        t = r['signal_type']
        by_type[t] = by_type.get(t, 0) + 1

    msg = (
        "<b>Daily Summary</b>\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n\n"
        f"  <b>Signals:</b>  {total}\n"
        f"  <b>Avg Score:</b>  {avg_score:.0f}/100\n\n"
    )

    for t, count in sorted(by_type.items(), key=lambda x: x[1], reverse=True):
        msg += f"  {t}  ×{count}\n"

    msg += "\n▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n<b>Top Signals</b>\n\n"
    for i, r in enumerate(top_signals, 1):
        msg += f"  {i}. <b>{r['symbol']}</b>  {r['score']}/100\n"

    return msg

# =============================
# SCAN MARKET (COMBINED)
# =============================
def scan_symbol(symbol, ex, now, phase2b_best, alerts):
    """Scan a single symbol for all signal types. Returns True if alerted."""
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    except Exception:
        return

    if len(df) < 15:
        return

    rsi = calculate_rsi(df)

    if rsi > settings['rsi_overbought']:
        return

    if is_fake_pump(df):
        return

    # Per-symbol cooldown
    last_alerted = alerted_today.get(symbol, 0)
    if now - last_alerted < 3600:
        return

    # --- Phase 2B + Surge + Breakout ---
    score = score_phase_2b(df)
    if score >= 75:
        phase2b_best.append((symbol, score))
        phase2b_watchlist.add(symbol)

    if volume_accelerating(df) and early_explosion(df):
        full_msg, composite = format_alert(symbol, 'early_surge', df, {
            'vol_ratio': df['volume'].iloc[-1] / (df['volume'].iloc[-2] + 1e-9),
            'price_change': (df['close'].iloc[-1] - df['close'].iloc[-2]) / (df['close'].iloc[-2] + 1e-9) * 100,
        })
        if composite >= settings['min_score']:
            print(f"early_surge: {symbol} ({composite})")
            alerts.append((full_msg, composite))
            alerted_today[symbol] = now
        daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'early_surge'})
        return

    if symbol in phase2b_watchlist and is_breakout(df):
        full_msg, composite = format_alert(symbol, 'breakout', df, {
            'vol_ratio': df['volume'].iloc[-1] / (df['volume'][:-1].mean() + 1e-9),
        })
        if composite >= settings['min_score']:
            print(f"breakout: {symbol} ({composite})")
            alerts.append((full_msg, composite))
            alerted_today[symbol] = now
        daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'breakout'})
        phase2b_watchlist.discard(symbol)
        return

    # --- Runner detection ---
    result = detect_runner(symbol, df)
    if result:
        signal_map = {
            '🔥 VOLUME EXPLOSION': 'volume_explosion',
            '📈 RUNNER': 'runner',
            '⚡ ACCUMULATION': 'accumulation',
            '💥 VOLUME BREAKOUT': 'volume_breakout',
        }
        sig_type = signal_map.get(result['type'], 'runner')
        full_msg, composite = format_alert(symbol, sig_type, df, {
            'vol_ratio': result['vol_ratio'],
            'price_change': result['price_change'],
        })
        if composite >= settings['min_score']:
            print(f"{sig_type}: {symbol} ({composite})")
            alerts.append((full_msg, composite))
            alerted_today[symbol] = now
        daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': sig_type})
        return

    # --- Momentum detection (3 green candles + rising volume) ---
    mom = detect_momentum(symbol, df)
    if mom:
        full_msg, composite = format_alert(symbol, 'momentum', df, {
            'vol_ratio': mom['vol_ratio'],
            'price_change': mom['price_change'],
        })
        if composite >= settings['min_score']:
            print(f"momentum: {symbol} ({composite})")
            alerts.append((full_msg, composite))
            alerted_today[symbol] = now
        daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'momentum'})
        return

    # --- Early accumulation detection ---
    accum = detect_early_accumulation(symbol, df)
    if accum:
        full_msg, composite = format_alert(symbol, 'early_accumulation', df, {
            'vol_ratio': accum['vol_ratio'],
            'price_change': accum['price_change'],
        })
        if composite >= settings['min_score']:
            print(f"early_accumulation: {symbol} ({composite})")
            alerts.append((full_msg, composite))
            alerted_today[symbol] = now
        daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'early_accumulation'})


def scan_market_sync():
    # BTC sentiment check
    btc_dumping, btc_change = is_btc_dumping()
    if btc_dumping:
        msg = (
            f"<b>Scan Paused</b>\n"
            f"▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            f"  BTC  ▼ {abs(btc_change):.2f}%\n"
            f"  Threshold: {settings['btc_dump_threshold']}%"
        )
        print(f"BTC dump: {btc_change:+.2f}%")
        return [msg]

    phase2b_best = []
    alerts = []
    now = time.time()
    scanned = 0
    seen_bases = set()

    # --- Scan SPOT markets ---
    spot_markets = exchange.load_markets()
    for symbol in spot_markets:
        if "/USDT" not in symbol or symbol.count("USDT") > 1:
            continue
        base = symbol.split("/")[0]
        if base in EXCLUDED_BASES:
            continue
        seen_bases.add(base)
        scanned += 1
        scan_symbol(symbol, exchange, now, phase2b_best, alerts)

    # --- Scan PERP markets (catches tokens without spot pairs) ---
    try:
        perp_markets = exchange_perp.load_markets()
        for symbol in perp_markets:
            if "/USDT:USDT" not in symbol:
                continue
            base = symbol.split("/")[0]
            if base in EXCLUDED_BASES or base in seen_bases:
                continue  # skip if already scanned on spot
            scanned += 1
            scan_symbol(symbol, exchange_perp, now, phase2b_best, alerts)
    except Exception as e:
        print(f"Perp scan error: {e}")

    # Sort by score (best first) and cap
    alerts.sort(key=lambda x: x[1], reverse=True)
    max_alerts = int(settings['max_alerts_per_scan'])
    top_alerts = [msg for msg, score in alerts[:max_alerts]]

    # Phase 2B summary
    phase2b_best.sort(key=lambda x: x[1], reverse=True)
    if phase2b_best:
        lines = "\n".join(f"  - <b>{s}</b>  {sc}/100" for s, sc in phase2b_best[:3])
        msg = f"<b>Gem Setups</b>\n{lines}"
        print(f"Gems: {[s for s,_ in phase2b_best[:3]]}")
        top_alerts.append(msg)

    print(f"Scanned {scanned} coins (spot+perp), found {len(alerts)} signals (showing top {len(top_alerts)})")

    if not top_alerts:
        print("No signals detected")

    return top_alerts

async def run_scan(bot: Bot):
    loop = asyncio.get_event_loop()
    alerts = await loop.run_in_executor(None, scan_market_sync)
    if not alerts:
        return
    # Batch alerts into messages (Telegram limit 4096 chars)
    separator = "\n\n"
    batch = separator.join(alerts)
    while batch:
        chunk = batch[:4000]
        if len(batch) > 4000:
            # Split at last double-newline to keep cards intact
            last_break = chunk.rfind("\n\n")
            if last_break > 0:
                chunk = batch[:last_break]
        await bot.send_message(chat_id=CHAT_ID, text=chunk, parse_mode=ParseMode.HTML)
        batch = batch[len(chunk):].lstrip("\n ")

# =============================
# BACKGROUND SCANNER
# =============================
async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    print("🔍 Scanning...")
    try:
        await run_scan(context.bot)
    except Exception as e:
        import traceback
        print("Error:", e)
        traceback.print_exc()

# =============================
# DAILY SUMMARY (auto at midnight UTC)
# =============================
async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if daily_results:
        msg = build_daily_summary()
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
    # Reset for new day
    alerted_today.clear()
    daily_results.clear()
    print("📋 Daily summary sent. Lists cleared for new day.")

# =============================
# START
# =============================
if __name__ == "__main__":
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CommandHandler("runners", runners_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Recurring scan every 1 minute
    app.job_queue.run_repeating(scheduled_scan, interval=SLEEP_TIME, first=5)

    # Daily summary at 23:55 UTC
    from datetime import time as dtime
    app.job_queue.run_daily(send_daily_summary, time=dtime(hour=23, minute=55, tzinfo=timezone.utc))

    print("🤖 Crime Pump Sniper Bot started!")
    app.run_polling()
