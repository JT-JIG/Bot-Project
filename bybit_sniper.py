import os
import time
from dotenv import load_dotenv
import ccxt
import pandas as pd
import asyncio
from datetime import timezone
from telegram import Bot, Update
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

TIMEFRAME = '1d'
LIMIT = 50
SLEEP_TIME = 300  # scan every 5 minutes

# Telegram - Load from environment
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# Track state
phase2b_watchlist = set()
alerted_today = {}  # {symbol: last_alert_timestamp}
daily_results = []  # store all alerts for daily summary

# Configurable settings (can be changed via /settings)
settings = {
    'rsi_overbought': 70,
    'rsi_oversold': 30,
    'btc_dump_threshold': -5.0,  # BTC daily % drop to pause scanning
    'volume_multiplier': 3.0,    # volume explosion threshold
    'wick_ratio': 0.6,           # wick-to-body ratio for fake pump filter
}

# =============================
# FILTER MAJOR COINS
# =============================
EXCLUDED = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"]

# =============================
# TELEGRAM COMMANDS
# =============================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔫 CRIME PUMP SNIPER — ACTIVE\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Bybit · Daily · Every 5 min\n\n"
        "💎 Gems  ⚡ Surges  🚀 Breakouts\n"
        "🔥 Explosions  📈 Runners  💥 Wakeups\n\n"
        "Filters: RSI · BTC Sentiment · Wick\n"
        "Extras: S/R · Entry/TP/SL · Score\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "/scan · /watchlist · /runners\n"
        "/summary · /settings · /reset\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Stay sharp. Stay ready. 🫡"
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running manual scan...")
    await run_scan(context.bot)
    await update.message.reply_text("✅ Scan complete.")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if phase2b_watchlist:
        msg = "👀 Phase 2B Watchlist:\n" + "\n".join(phase2b_watchlist)
    else:
        msg = "📭 Watchlist is empty."
    await update.message.reply_text(msg)

async def runners_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if alerted_today:
        msg = "🏃 Today's Runners:\n" + "\n".join(alerted_today.keys())
    else:
        msg = "📭 No runners detected yet today."
    await update.message.reply_text(msg)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerted_today.clear()
    daily_results.clear()
    await update.message.reply_text("🔄 Alerted list cleared.")

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not daily_results:
        await update.message.reply_text("📭 No signals today yet.")
        return
    msg = build_daily_summary()
    await update.message.reply_text(msg)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        msg = (
            "⚙️ CURRENT SETTINGS:\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"RSI Overbought: {settings['rsi_overbought']}\n"
            f"RSI Oversold: {settings['rsi_oversold']}\n"
            f"BTC Dump Threshold: {settings['btc_dump_threshold']}%\n"
            f"Volume Multiplier: {settings['volume_multiplier']}x\n"
            f"Wick Ratio Filter: {settings['wick_ratio']}\n\n"
            "To change: /settings <key> <value>\n"
            "Example: /settings volume_multiplier 4.0"
        )
        await update.message.reply_text(msg)
        return

    if len(args) == 2:
        key, value = args[0], args[1]
        if key in settings:
            try:
                settings[key] = float(value)
                await update.message.reply_text(f"✅ {key} set to {settings[key]}")
            except ValueError:
                await update.message.reply_text("❌ Value must be a number.")
        else:
            await update.message.reply_text(f"❌ Unknown setting: {key}")
    else:
        await update.message.reply_text("Usage: /settings <key> <value>")

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
# GEM FILTER (LOW BASE VOLUME)
# =============================
def is_low_cap_gem(df):
    avg_vol = df['volume'].mean()
    return avg_vol < 200000

# =============================
# PHASE 2B SCORE (ORIGINAL)
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
# EARLY VOLUME SURGE (ORIGINAL)
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
# BREAKOUT DETECTION (ORIGINAL)
# =============================
def is_breakout(df):
    price_now = df['close'].iloc[-1]
    resistance = df['high'].tail(12).max()
    avg_volume = df['volume'][:-1].mean()
    return price_now > resistance and df['volume'].iloc[-1] > avg_volume * 2.5

# =============================
# DAILY RUNNER DETECTION
# =============================
def detect_daily_runner(symbol, df):
    if len(df) < 15:
        return None

    vol_today = df['volume'].iloc[-1]
    vol_7d_avg = df['volume'].iloc[-8:-1].mean()
    vol_14d_avg = df['volume'].iloc[-15:-1].mean()

    price_today = df['close'].iloc[-1]
    price_yesterday = df['close'].iloc[-2]
    price_change = (price_today - price_yesterday) / (price_yesterday + 1e-9) * 100

    price_open = df['open'].iloc[-1]
    intraday_change = (price_today - price_open) / (price_open + 1e-9) * 100

    vol_ratio_7d = vol_today / (vol_7d_avg + 1e-9)
    vol_ratio_14d = vol_today / (vol_14d_avg + 1e-9)

    vol_trending = (df['volume'].iloc[-1] > df['volume'].iloc[-2] > df['volume'].iloc[-3])

    if vol_ratio_7d >= settings['volume_multiplier'] and price_change > 0:
        return {
            'type': '🔥 VOLUME EXPLOSION',
            'vol_ratio': vol_ratio_7d,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    if vol_ratio_7d >= 2 and price_change > 5:
        return {
            'type': '📈 DAILY RUNNER',
            'vol_ratio': vol_ratio_7d,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    if vol_ratio_7d >= 1.8 and vol_trending and abs(price_change) < 3:
        return {
            'type': '⚡ ACCUMULATION',
            'vol_ratio': vol_ratio_7d,
            'price_change': price_change,
            'intraday': intraday_change,
        }

    if vol_ratio_14d >= 2.5 and price_change > 0:
        return {
            'type': '💥 VOLUME BREAKOUT',
            'vol_ratio': vol_ratio_14d,
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
        'accumulation': 15,
        'volume_breakout': 15,
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

    # RSI label
    if rsi > settings['rsi_overbought']:
        rsi_label = "⚠️ OVERBOUGHT"
    elif rsi < settings['rsi_oversold']:
        rsi_label = "🟢 OVERSOLD"
    else:
        rsi_label = "NEUTRAL"

    # Score label
    if composite >= 80:
        grade = "🔥 A+"
    elif composite >= 65:
        grade = "✅ A"
    elif composite >= 50:
        grade = "🟡 B"
    else:
        grade = "⚪ C"

    msg = ""
    if isinstance(extra_info, dict) and 'header' in extra_info:
        msg += f"{extra_info['header']}\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RSI: {rsi:.1f} ({rsi_label})\n"
        f"🏆 Score: {composite}/100 ({grade})\n"
    )

    if isinstance(extra_info, dict):
        if 'vol_ratio' in extra_info:
            msg += f"📦 Volume: {extra_info['vol_ratio']:.1f}x avg\n"
        if 'price_change' in extra_info:
            msg += f"📈 Change: {extra_info['price_change']:+.2f}%\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TRADE SETUP:\n"
        f"Entry: ${levels['entry']:.6g}\n"
        f"SL: ${levels['sl']:.6g}\n"
        f"TP1: ${levels['tp1']:.6g}\n"
        f"TP2: ${levels['tp2']:.6g}\n"
        f"TP3: ${levels['tp3']:.6g}\n"
        f"R:R — {levels['rr']:.1f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 Support: ${levels['support']:.6g}\n"
        f"📐 Resistance: ${levels['resistance']:.6g}\n"
        f"📏 ATR: ${levels['atr']:.6g}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    return msg, composite

# =============================
# DAILY SUMMARY
# =============================
def build_daily_summary():
    if not daily_results:
        return "📭 No signals today."

    total = len(daily_results)
    avg_score = sum(r['score'] for r in daily_results) / total
    top_signals = sorted(daily_results, key=lambda x: x['score'], reverse=True)[:5]

    by_type = {}
    for r in daily_results:
        t = r['signal_type']
        by_type[t] = by_type.get(t, 0) + 1

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 DAILY SUMMARY\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Total Signals: {total}\n"
        f"🏆 Avg Score: {avg_score:.0f}/100\n\n"
        "📈 Signal Breakdown:\n"
    )

    for t, count in sorted(by_type.items(), key=lambda x: x[1], reverse=True):
        msg += f"  {t}: {count}\n"

    msg += "\n🏅 Top 5 Signals:\n"
    for i, r in enumerate(top_signals, 1):
        msg += f"  {i}. {r['symbol']} — {r['score']}/100 ({r['signal_type']})\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━"
    return msg

# =============================
# SCAN MARKET (COMBINED)
# =============================
def scan_market_sync():
    # BTC sentiment check
    btc_dumping, btc_change = is_btc_dumping()
    if btc_dumping:
        msg = (
            f"🐻 BTC DUMP DETECTED: {btc_change:+.2f}%\n"
            f"Scanning paused — market conditions unsafe.\n"
            f"Threshold: {settings['btc_dump_threshold']}%"
        )
        print(msg)
        return [msg]

    markets = exchange.load_markets()
    phase2b_best = []
    alerts = []

    for symbol in markets:
        if "/USDT" not in symbol:
            continue

        if any(x in symbol for x in EXCLUDED):
            continue

        try:
            df = get_data(symbol)

            if len(df) < 15:
                continue

            rsi = calculate_rsi(df)

            # Skip overbought coins
            if rsi > settings['rsi_overbought']:
                continue

            # Skip fake pumps
            if is_fake_pump(df):
                continue

            # --- ORIGINAL: Phase 2B + Surge + Breakout (low-cap gems) ---
            if is_low_cap_gem(df):
                score = score_phase_2b(df)
                if score >= 75:
                    phase2b_best.append((symbol, score))
                    phase2b_watchlist.add(symbol)

                if volume_accelerating(df) and early_explosion(df):
                    header = f"⚡ EARLY VOLUME SURGE: {symbol}"
                    full_msg, composite = format_alert(symbol, 'early_surge', df, {
                        'header': header,
                        'vol_ratio': df['volume'].iloc[-1] / (df['volume'].iloc[-2] + 1e-9),
                        'price_change': (df['close'].iloc[-1] - df['close'].iloc[-2]) / (df['close'].iloc[-2] + 1e-9) * 100,
                    })
                    print(header)
                    alerts.append(full_msg)
                    daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'early_surge'})

                if symbol in phase2b_watchlist and is_breakout(df):
                    header = f"🚀 GEM BREAKOUT: {symbol}"
                    full_msg, composite = format_alert(symbol, 'breakout', df, {
                        'header': header,
                        'vol_ratio': df['volume'].iloc[-1] / (df['volume'][:-1].mean() + 1e-9),
                    })
                    print(header)
                    alerts.append(full_msg)
                    daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': 'breakout'})
                    phase2b_watchlist.discard(symbol)

            # --- Daily runner detection (all coins) ---
            now = time.time()
            last_alerted = alerted_today.get(symbol, 0)
            if now - last_alerted >= 3600:
                result = detect_daily_runner(symbol, df)
                if result:
                    header = f"{result['type']}: {symbol}"
                    signal_map = {
                        '🔥 VOLUME EXPLOSION': 'volume_explosion',
                        '📈 DAILY RUNNER': 'daily_runner',
                        '⚡ ACCUMULATION': 'accumulation',
                        '💥 VOLUME BREAKOUT': 'volume_breakout',
                    }
                    sig_type = signal_map.get(result['type'], 'daily_runner')

                    full_msg, composite = format_alert(symbol, sig_type, df, {
                        'header': header,
                        'vol_ratio': result['vol_ratio'],
                        'price_change': result['price_change'],
                    })
                    print(header)
                    alerts.append(full_msg)
                    alerted_today[symbol] = now
                    daily_results.append({'symbol': symbol, 'score': composite, 'signal_type': sig_type})

        except Exception:
            continue

    # Phase 2B summary
    phase2b_best.sort(key=lambda x: x[1], reverse=True)
    if phase2b_best:
        msg = "💎 BYBIT GEM SETUPS:\n"
        for s, sc in phase2b_best[:3]:
            msg += f"{s} → {sc}/100\n"
        print(msg)
        alerts.append(msg)

    if not alerts:
        print("No signals detected")

    return alerts

async def run_scan(bot: Bot):
    loop = asyncio.get_event_loop()
    alerts = await loop.run_in_executor(None, scan_market_sync)
    for msg in alerts:
        await bot.send_message(chat_id=CHAT_ID, text=msg)

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
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)
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

    # Recurring scan every 5 minutes
    app.job_queue.run_repeating(scheduled_scan, interval=SLEEP_TIME, first=5)

    # Daily summary at 23:55 UTC
    from datetime import time as dtime
    app.job_queue.run_daily(send_daily_summary, time=dtime(hour=23, minute=55, tzinfo=timezone.utc))

    print("🤖 Crime Pump Sniper Bot started!")
    app.run_polling()
