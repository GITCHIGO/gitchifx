"""
GAMAN-X — Multi-Strategy Trading Dashboard
Aparte engine naast GAMAN. Port 5001. Geen MT5 integratie.

Strategieën:
  1. Silver Bullet      — Killzone FVG reversal (VOLLEDIG GEÏMPLEMENTEERD)
  2. CHoCH Reversal     — Structure-based trend reversal (placeholder)
  3. BOS Continuation   — Structure-based trend continuation (placeholder)
  4. Asian Range Breakout + Retest — Session breakout (placeholder)

Datafetching: TradingView WebSocket + yfinance fallback (hergebruik patroon GAMAN)
"""
import os, sys, json, time, threading, traceback, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import pytz
from flask import Flask, request, jsonify, render_template_string

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "gamanx_state.json"
PRESETS_FILE = BASE_DIR / "gamanx_presets.json"

PORT = 5001
SCAN_INTERVAL = 30      # seconden — conservatief, GAMAN-X loopt naast GAMAN
DASHBOARD_TITLE = "GAMAN-X"

# Brussels timezone (zelfde als GAMAN voor consistency)
TZ = pytz.timezone("Europe/Brussels")
TZ_NY = pytz.timezone("America/New_York")

# Pip definities (zelfde als GAMAN)
PIP     = {"EURUSD": 0.0001, "XAUUSD": 0.10}
PIP_EUR = {"EURUSD": 0.10,   "XAUUSD": 0.92}

# TradingView symbols
TV_SYMBOLS = {"EURUSD": "FX:EURUSD", "XAUUSD": "OANDA:XAUUSD"}
YF_SYMBOLS = {"EURUSD": "EURUSD=X",  "XAUUSD": "GC=F"}

# Timeframe mapping for yfinance
TF_YF     = {"15M":"15m",  "1H":"1h",   "4H":"1h"}
TF_PERIOD = {"15M":"7d",   "1H":"60d",  "4H":"60d"}

# ════════════════════════════════════════════════════════════
# UTILITIES — time, formatting, logging
# ════════════════════════════════════════════════════════════

def now_brussels():
    return datetime.now(TZ)

def now_ny():
    return datetime.now(TZ_NY)

def fmt_brussels(dt=None):
    if dt is None: dt = now_brussels()
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def log(level, msg):
    """Console + memory log."""
    ts = fmt_brussels()
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    engine_log.append({"ts": ts, "level": level, "msg": msg})
    # Cap log to last 500 entries
    if len(engine_log) > 500:
        del engine_log[:len(engine_log)-500]

# In-memory log buffer
engine_log = []

# ════════════════════════════════════════════════════════════
# DATA FETCHING — TradingView WebSocket + yfinance fallback
# ════════════════════════════════════════════════════════════

def fetch_candles_yf(pair, tf, start=None, end=None):
    """yfinance candle fetcher — voor backtest of als fallback."""
    try:
        import yfinance as yf
    except ImportError:
        log("ERROR", "yfinance not installed")
        return None

    symbol = YF_SYMBOLS.get(pair)
    yf_tf  = TF_YF.get(tf, "1h")
    period = TF_PERIOD.get(tf, "60d")

    try:
        ticker = yf.Ticker(symbol)
        if start and end:
            df = ticker.history(start=start, end=end, interval=yf_tf, auto_adjust=False)
        else:
            df = ticker.history(period=period, interval=yf_tf, auto_adjust=False)

        if df is None or len(df) < 5:
            return None

        # Resample 1h -> 4h if needed
        if tf == "4H" and yf_tf == "1h":
            df = df.resample("4H").agg({
                "Open":"first", "High":"max", "Low":"min",
                "Close":"last", "Volume":"sum"
            }).dropna()

        df.columns = [c.lower() for c in df.columns]
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(TZ)
        return df
    except Exception as e:
        log("WARN", f"yfinance fetch failed: {e}")
        return None


def fetch_candles_tv(pair, tf):
    """TradingView WebSocket candle fetcher — primair voor live data."""
    try:
        import websocket
    except ImportError:
        log("WARN", "websocket-client not installed, using yfinance")
        return fetch_candles_yf(pair, tf)

    tv_tf_map = {"15M": "15", "1H": "60", "4H": "240"}
    tv_tf = tv_tf_map.get(tf, "60")
    symbol = TV_SYMBOLS.get(pair, "FX:EURUSD")

    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket",
            headers={"Origin": "https://data.tradingview.com"},
            timeout=10
        )

        def send_msg(msg):
            m = "~m~" + str(len(msg)) + "~m~" + msg
            ws.send(m)

        def gen_token(length=12):
            import random, string
            chars = string.ascii_lowercase + string.digits
            return "qs_" + "".join(random.choices(chars, k=length))

        cs_token = gen_token()
        send_msg(json.dumps({"m":"set_auth_token","p":["unauthorized_user_token"]}))
        send_msg(json.dumps({"m":"chart_create_session","p":[cs_token,""]}))
        send_msg(json.dumps({"m":"resolve_symbol","p":[cs_token,"symbol_1",f"={{\"symbol\":\"{symbol}\",\"adjustment\":\"splits\"}}"]}))
        send_msg(json.dumps({"m":"create_series","p":[cs_token,"s1","s1","symbol_1",tv_tf,500]}))

        candles = []
        start = time.time()
        while time.time() - start < 15:
            try:
                raw = ws.recv()
                if not raw: continue
                # Strip ~m~N~m~ prefix
                while "~m~" in raw:
                    idx = raw.find("~m~", raw.find("~m~")+3)
                    if idx == -1: break
                    chunk = raw[raw.find("~m~")+3:]
                    if "~m~" in chunk:
                        size_end = chunk.find("~m~")
                        try:
                            size = int(chunk[:size_end])
                            payload = chunk[size_end+3:size_end+3+size]
                            raw = chunk[size_end+3+size:]
                            if payload.startswith("{"):
                                msg = json.loads(payload)
                                if msg.get("m") == "timescale_update":
                                    series_data = msg["p"][1].get("s1", {}).get("s", [])
                                    for c in series_data:
                                        v = c.get("v", [])
                                        if len(v) >= 5:
                                            candles.append({
                                                "ts": v[0], "open": v[1], "high": v[2],
                                                "low": v[3], "close": v[4],
                                                "volume": v[5] if len(v) > 5 else 0
                                            })
                                if msg.get("m") == "series_completed":
                                    break
                        except: pass
                    else:
                        break
                if candles and len(candles) >= 100:
                    break
            except Exception: break

        try: ws.close()
        except: pass

        if not candles:
            log("WARN", f"TV WebSocket no data for {pair} {tf}, fallback to yfinance")
            return fetch_candles_yf(pair, tf)

        df = pd.DataFrame(candles)
        df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(TZ)
        df = df.set_index("dt").sort_index()
        df = df[["open","high","low","close","volume"]]
        return df

    except Exception as e:
        log("WARN", f"TV WebSocket failed: {e}, fallback to yfinance")
        return fetch_candles_yf(pair, tf)


def fetch_candles(pair, tf, start=None, end=None):
    """Master fetcher — TV voor live, yfinance voor historisch."""
    if start and end:
        return fetch_candles_yf(pair, tf, start, end)
    return fetch_candles_tv(pair, tf)


def fetch_price(pair):
    """Snelle live price fetch — laatste close van laatste candle."""
    try:
        df = fetch_candles_tv(pair, "15M")
        if df is not None and len(df) > 0:
            return float(df["close"].iloc[-1])
    except: pass
    return None


# ════════════════════════════════════════════════════════════
# SHARED HELPERS — pivot detection, FVG, etc.
# ════════════════════════════════════════════════════════════

def detect_swing_points(df, length=5):
    """Fractal-based swing high/low detector.
    Bar i is swing high als high[i] > high[i-length:i] en high[i] > high[i+1:i+length+1].
    Returns: list of {idx, ts, type: 'HIGH'|'LOW', price}
    """
    swings = []
    if df is None or len(df) < 2*length+1:
        return swings
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    for i in range(length, n-length):
        # Swing high
        if highs[i] == max(highs[i-length:i+length+1]) and highs[i] > highs[i-1]:
            swings.append({"idx": i, "ts": df.index[i], "type": "HIGH", "price": float(highs[i])})
        # Swing low
        elif lows[i] == min(lows[i-length:i+length+1]) and lows[i] < lows[i-1]:
            swings.append({"idx": i, "ts": df.index[i], "type": "LOW", "price": float(lows[i])})
    return swings


def detect_fvg(df, idx, direction):
    """Detect FVG (Fair Value Gap) op of net voor index idx.
    direction: 'BULL' (bullish FVG, prijs in uptrend gap) of 'BEAR'
    3-candle pattern: candle[i-1], candle[i] (displacement), candle[i+1]
    Bullish FVG: low van candle[i+1] > high van candle[i-1]  → gap tussen die levels
    Bearish FVG: high van candle[i+1] < low van candle[i-1]
    Returns: dict {top, bottom, ts} of None
    """
    if idx < 1 or idx >= len(df) - 1:
        return None
    c0_high = df["high"].iloc[idx-1]
    c0_low  = df["low"].iloc[idx-1]
    c2_high = df["high"].iloc[idx+1]
    c2_low  = df["low"].iloc[idx+1]

    if direction == "BULL" and c2_low > c0_high:
        return {"top": float(c2_low), "bottom": float(c0_high), "ts": df.index[idx]}
    if direction == "BEAR" and c2_high < c0_low:
        return {"top": float(c0_low), "bottom": float(c2_high), "ts": df.index[idx]}
    return None


def atr(df, period=14):
    """Average True Range — gebruikt voor displacement detection."""
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    tr_list = []
    for i in range(1, len(df)):
        tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr_list.append(tr)
    if len(tr_list) < period: return 0.0
    return float(np.mean(tr_list[-period:]))


# ════════════════════════════════════════════════════════════
# STRATEGIE 1: SILVER BULLET — VOLLEDIG GEÏMPLEMENTEERD
# ════════════════════════════════════════════════════════════

class SilverBulletDetector:
    """ICT Silver Bullet strategy.
    Killzones (NY tijd):
      - London KZ:  03:00–04:00 AM EST
      - NY AM KZ:   10:00–11:00 AM EST
      - NY PM KZ:   02:00–03:00 PM EST
    Setup: liquidity sweep → displacement → FVG → entry op FVG retest
    SL: voorbij swept high/low
    TP: RR-based (default 1:2)
    """

    KILLZONES_NY = [
        ("london",  3,  4),   # 03:00 - 04:00 NY EST
        ("ny_am",   10, 11),  # 10:00 - 11:00 NY EST
        ("ny_pm",   14, 15),  # 14:00 - 15:00 NY EST
    ]

    def __init__(self, enabled_killzones=None, displacement_atr_mult=1.5,
                 rr=2.0, sweep_lookback=30):
        self.enabled_killzones = enabled_killzones or ["london","ny_am","ny_pm"]
        self.displacement_atr_mult = displacement_atr_mult
        self.rr = rr
        self.sweep_lookback = sweep_lookback

    def in_killzone(self, dt_ny):
        """Check of huidige NY tijd binnen een active killzone valt."""
        hour = dt_ny.hour
        for name, start_h, end_h in self.KILLZONES_NY:
            if name not in self.enabled_killzones: continue
            if start_h <= hour < end_h:
                return name
        return None

    def detect_signal(self, df, pair, diagnostics=None):
        """Scan laatste candle voor Silver Bullet setup.

        Args:
            diagnostics: optional dict — als gezet, increment counters voor filter rejection
        Returns: signal dict of None.
        """
        if df is None or len(df) < self.sweep_lookback + 5:
            if diagnostics is not None: diagnostics["insufficient_data"] += 1
            return None

        # Check current bar is in killzone (NY time)
        latest_ny = df.index[-1].astimezone(TZ_NY)
        kz_name = self.in_killzone(latest_ny)
        if kz_name is None:
            if diagnostics is not None: diagnostics["not_in_killzone"] += 1
            return None

        # Get ATR for displacement check
        cur_atr = atr(df, 14)
        if cur_atr <= 0:
            if diagnostics is not None: diagnostics["no_atr"] += 1
            return None

        # Look at last few bars for displacement + sweep + FVG
        n = len(df)
        in_kz_but_no_setup = False
        for i in range(n-4, n-1):
            # Displacement check: this bar's range > 1.5x ATR
            bar_range = df["high"].iloc[i] - df["low"].iloc[i]
            if bar_range < self.displacement_atr_mult * cur_atr:
                in_kz_but_no_setup = True
                continue

            # Determine direction of displacement
            close = df["close"].iloc[i]
            open_p = df["open"].iloc[i]
            if close > open_p:
                direction = "LONG"
                disp_dir = "BULL"
            else:
                direction = "SHORT"
                disp_dir = "BEAR"

            # Sweep check: did this bar's wick break recent swing high/low?
            lookback_high = df["high"].iloc[max(0, i-self.sweep_lookback):i].max()
            lookback_low = df["low"].iloc[max(0, i-self.sweep_lookback):i].min()

            swept = False
            sl_level = None
            if direction == "LONG":
                if df["low"].iloc[i] < lookback_low and close > lookback_low:
                    swept = True
                    sl_level = df["low"].iloc[i]
            else:
                if df["high"].iloc[i] > lookback_high and close < lookback_high:
                    swept = True
                    sl_level = df["high"].iloc[i]

            if not swept:
                if diagnostics is not None: diagnostics["no_sweep_after_displacement"] += 1
                continue

            # FVG check: bar i+1 should have created an FVG
            fvg = detect_fvg(df, i, disp_dir)
            if fvg is None:
                if diagnostics is not None: diagnostics["no_fvg_after_sweep"] += 1
                continue

            # Build signal
            entry_price = (fvg["top"] + fvg["bottom"]) / 2
            pip_size = PIP.get(pair, 0.0001)

            if direction == "LONG":
                sl = sl_level - 2 * pip_size
                risk = entry_price - sl
                tp = entry_price + self.rr * risk
            else:
                sl = sl_level + 2 * pip_size
                risk = sl - entry_price
                tp = entry_price - self.rr * risk

            if risk <= 0:
                if diagnostics is not None: diagnostics["invalid_risk"] += 1
                continue

            if diagnostics is not None: diagnostics["signals_generated"] += 1
            return {
                "strategy": "SB",
                "pair": pair,
                "direction": direction,
                "entry": float(entry_price),
                "sl": float(sl),
                "tp": float(tp),
                "killzone": kz_name,
                "fvg_top": fvg["top"],
                "fvg_bottom": fvg["bottom"],
                "sweep_level": float(lookback_high if direction == "SHORT" else lookback_low),
                "atr": cur_atr,
                "rr": self.rr,
                "ts": str(df.index[-1]),
            }

        if in_kz_but_no_setup and diagnostics is not None:
            diagnostics["killzone_no_displacement"] += 1
        return None


# ════════════════════════════════════════════════════════════
# PLACEHOLDER DETECTORS — CHoCH, BOS, Asian Breakout
# ════════════════════════════════════════════════════════════

class CHoCHDetector:
    """Change of Character — structuur-gebaseerde trend reversal.

    Concept:
      - Track swings (HH/HL/LH/LL) en bepaal trend state (BULL/BEAR/NEUTRAL)
      - Bullish CHoCH: in BEAR state, close > laatste Lower High
      - Bearish CHoCH: in BULL state, close < laatste Higher Low
      - Entry: na pullback naar de gebroken level (confirmation variant)

    Realistische backtest WR: 45-55% (NIET de 70%+ die guru's claimen).
    """

    def __init__(self, swing_length=5, rr=2.0, use_pullback=True, max_pullback_bars=15):
        self.swing_length = swing_length
        self.rr = rr
        self.use_pullback = use_pullback
        self.max_pullback_bars = max_pullback_bars

    def _classify_trend(self, swings):
        """Bepaal trend state op basis van laatste 2 HH/LL paren.

        Returns: 'BULL', 'BEAR', of 'NEUTRAL'
        """
        if len(swings) < 4:
            return "NEUTRAL", None, None

        # Pak laatste 2 highs en 2 lows
        highs = [s for s in swings if s["type"] == "HIGH"][-2:]
        lows = [s for s in swings if s["type"] == "LOW"][-2:]

        if len(highs) < 2 or len(lows) < 2:
            return "NEUTRAL", None, None

        last_high = highs[-1]
        prev_high = highs[-2]
        last_low = lows[-1]
        prev_low = lows[-2]

        # BULL: HH + HL
        if last_high["price"] > prev_high["price"] and last_low["price"] > prev_low["price"]:
            return "BULL", last_high, last_low
        # BEAR: LH + LL
        if last_high["price"] < prev_high["price"] and last_low["price"] < prev_low["price"]:
            return "BEAR", last_high, last_low
        return "NEUTRAL", last_high, last_low

    def detect_signal(self, df, pair, diagnostics=None):
        """Scan for CHoCH event op laatste bar.

        Returns: signal dict of None.
        """
        if df is None or len(df) < self.swing_length * 4 + 5:
            if diagnostics is not None: diagnostics["insufficient_data"] += 1
            return None

        swings = detect_swing_points(df, length=self.swing_length)
        if len(swings) < 4:
            if diagnostics is not None: diagnostics["not_enough_swings"] += 1
            return None

        trend, last_high, last_low = self._classify_trend(swings)
        if trend == "NEUTRAL":
            if diagnostics is not None: diagnostics["trend_neutral"] += 1
            return None

        latest_close = float(df["close"].iloc[-1])
        signal_dir = None
        broken_level = None
        sl_level = None

        if trend == "BEAR":
            if latest_close > last_high["price"]:
                signal_dir = "LONG"
                broken_level = last_high["price"]
                sl_level = last_low["price"]
        elif trend == "BULL":
            if latest_close < last_low["price"]:
                signal_dir = "SHORT"
                broken_level = last_low["price"]
                sl_level = last_high["price"]

        if signal_dir is None:
            if diagnostics is not None: diagnostics["no_choch_break"] += 1
            return None

        pip_size = PIP.get(pair, 0.0001)
        if self.use_pullback:
            entry_price = broken_level
        else:
            entry_price = latest_close

        if signal_dir == "LONG":
            sl = sl_level - 2 * pip_size
            risk = entry_price - sl
            tp = entry_price + self.rr * risk
        else:
            sl = sl_level + 2 * pip_size
            risk = sl - entry_price
            tp = entry_price - self.rr * risk

        if risk <= 0:
            if diagnostics is not None: diagnostics["invalid_risk"] += 1
            return None

        if diagnostics is not None: diagnostics["signals_generated"] += 1
        return {
            "strategy":     "CH",
            "pair":         pair,
            "direction":    signal_dir,
            "entry":        float(entry_price),
            "sl":           float(sl),
            "tp":           float(tp),
            "broken_level": float(broken_level),
            "prior_trend":  trend,
            "swing_length": self.swing_length,
            "rr":           self.rr,
            "ts":           str(df.index[-1]),
        }


class BOSDetector:
    """Break of Structure — trend continuation strategy.

    Concept:
      - Track swings + trend state (zelfde als CHoCH detector)
      - Bullish BOS: in BULL state, close > last Higher High
      - Bearish BOS: in BEAR state, close < last Lower Low
      - Entry: pullback naar gebroken level (klassieke retest van old resistance → support)

    Verschil met CHoCH:
      - CHoCH = break TEGEN trend (reversal)
      - BOS   = break MET trend (continuation)

    Realistische backtest WR: 55-65% — beste van de 4 strategieën in trending markten.
    """

    def __init__(self, swing_length=5, rr=2.0, max_pullback_pct=0.786):
        self.swing_length = swing_length
        self.rr = rr
        self.max_pullback_pct = max_pullback_pct  # invalidation als pullback >78.6%

    def _classify_trend(self, swings):
        """Identiek aan CHoCH — bepaal trend state via laatste 2 HH/HL of LH/LL."""
        if len(swings) < 4:
            return "NEUTRAL", None, None

        highs = [s for s in swings if s["type"] == "HIGH"][-2:]
        lows = [s for s in swings if s["type"] == "LOW"][-2:]

        if len(highs) < 2 or len(lows) < 2:
            return "NEUTRAL", None, None

        last_high = highs[-1]
        prev_high = highs[-2]
        last_low = lows[-1]
        prev_low = lows[-2]

        if last_high["price"] > prev_high["price"] and last_low["price"] > prev_low["price"]:
            return "BULL", last_high, last_low
        if last_high["price"] < prev_high["price"] and last_low["price"] < prev_low["price"]:
            return "BEAR", last_high, last_low
        return "NEUTRAL", last_high, last_low

    def detect_signal(self, df, pair, diagnostics=None):
        """Scan for BOS event op laatste bar."""
        if df is None or len(df) < self.swing_length * 4 + 5:
            if diagnostics is not None: diagnostics["insufficient_data"] += 1
            return None

        swings = detect_swing_points(df, length=self.swing_length)
        if len(swings) < 4:
            if diagnostics is not None: diagnostics["not_enough_swings"] += 1
            return None

        trend, last_high, last_low = self._classify_trend(swings)
        if trend == "NEUTRAL":
            if diagnostics is not None: diagnostics["trend_neutral"] += 1
            return None

        latest_close = float(df["close"].iloc[-1])
        signal_dir = None
        broken_level = None
        sl_level = None

        if trend == "BULL":
            if latest_close > last_high["price"]:
                signal_dir = "LONG"
                broken_level = last_high["price"]
                sl_level = last_low["price"]
        elif trend == "BEAR":
            if latest_close < last_low["price"]:
                signal_dir = "SHORT"
                broken_level = last_low["price"]
                sl_level = last_high["price"]

        if signal_dir is None:
            if diagnostics is not None: diagnostics["no_bos_break"] += 1
            return None

        entry_price = broken_level
        pip_size = PIP.get(pair, 0.0001)

        if signal_dir == "LONG":
            sl = sl_level - 2 * pip_size
            risk = entry_price - sl
            tp = entry_price + self.rr * risk
        else:
            sl = sl_level + 2 * pip_size
            risk = sl - entry_price
            tp = entry_price - self.rr * risk

        if risk <= 0:
            if diagnostics is not None: diagnostics["invalid_risk"] += 1
            return None

        if diagnostics is not None: diagnostics["signals_generated"] += 1
        return {
            "strategy":     "BOS",
            "pair":         pair,
            "direction":    signal_dir,
            "entry":        float(entry_price),
            "sl":           float(sl),
            "tp":           float(tp),
            "broken_level": float(broken_level),
            "trend":        trend,
            "swing_length": self.swing_length,
            "rr":           self.rr,
            "ts":           str(df.index[-1]),
        }


class AsianBreakoutDetector:
    """Asian Range Breakout + Retest strategy.

    Concept:
      - Markeer Asian session high/low (23:00-07:00 Brussels tijd)
      - Wacht op London/NY open (07:00-12:00 Brussels)
      - Bij breakout (close boven/onder range) + retest van gebroken level → entry
      - SL voorbij tegenovergestelde range level
      - TP = range size × tp_range_mult (default 1.5)

    State machine per dag:
      1. WAITING_RANGE       — Asian session loopt nog
      2. RANGE_COMPLETE      — high/low gevonden, wacht op breakout
      3. BROKEN_UP/DOWN      — breakout gedetecteerd, wacht op retest
      4. ENTRY               — retest geraakt, signaal geven

    Realistic backtest WR: 50-60% pure setup, hoger met HTF bias filter.
    """

    # Brussels tijdgrenzen
    ASIAN_START_HOUR = 23   # 23:00 prior day
    ASIAN_END_HOUR   = 7    # 07:00 same day
    BREAKOUT_END_HOUR = 12  # 12:00 same day (London + early NY)

    # Range size filters per pair (in pips)
    DEFAULT_RANGE_FILTERS = {
        "EURUSD": {"min": 20,  "max": 80},
        "XAUUSD": {"min": 100, "max": 400},
    }

    def __init__(self, min_range_pips=None, max_range_pips=None,
                 tp_range_mult=1.5, rr=None):
        # Per-pair filters worden runtime gepakt; deze attrs zijn overrides
        self.min_range_pips_override = min_range_pips
        self.max_range_pips_override = max_range_pips
        self.tp_range_mult = tp_range_mult
        self.rr = rr  # alternatief voor tp_range_mult (als gezet, gebruik RR)

    def _get_range_filters(self, pair):
        f = self.DEFAULT_RANGE_FILTERS.get(pair, {"min": 20, "max": 80})
        min_p = self.min_range_pips_override if self.min_range_pips_override else f["min"]
        max_p = self.max_range_pips_override if self.max_range_pips_override else f["max"]
        return min_p, max_p

    def _find_asian_range_for_day(self, df, target_date):
        """Find Asian session high/low for de Asian session die EINDIGT op target_date.

        Asian session = prior day 23:00 Brussels → target_date 07:00 Brussels.
        Returns: (high, low, end_idx) of (None, None, None) als incomplete.
        """
        # target_date is a date object — Asian session is van (target_date - 1d) 23:00 tot target_date 07:00
        prev_day = target_date - timedelta(days=1)
        session_start = TZ.localize(datetime.combine(prev_day, datetime.min.time())).replace(hour=self.ASIAN_START_HOUR)
        session_end = TZ.localize(datetime.combine(target_date, datetime.min.time())).replace(hour=self.ASIAN_END_HOUR)

        try:
            # Slice df voor deze sessie
            mask = (df.index >= session_start) & (df.index < session_end)
            session_df = df[mask]
            if len(session_df) < 5:
                return None, None, None
            high = float(session_df["high"].max())
            low = float(session_df["low"].min())
            end_idx = df.index.get_indexer([session_end], method="nearest")[0]
            return high, low, end_idx
        except Exception:
            return None, None, None

    def _scan_breakout_and_retest(self, df, range_high, range_low, start_idx, end_idx, pair):
        """Scan candles tussen start_idx (07:00) en end_idx (12:00) voor breakout + retest.

        Returns: signal dict of None.
        """
        if start_idx >= end_idx or start_idx >= len(df):
            return None

        pip_size = PIP.get(pair, 0.0001)
        broken_dir = None  # "UP" of "DOWN"
        break_idx = None
        retest_idx = None

        # Phase 1: vind breakout
        for i in range(start_idx, min(end_idx, len(df))):
            close = float(df["close"].iloc[i])
            if close > range_high:
                broken_dir = "UP"
                break_idx = i
                break
            if close < range_low:
                broken_dir = "DOWN"
                break_idx = i
                break

        if broken_dir is None:
            return None

        # Phase 2: vind retest (na breakout, maar voor end_idx)
        retest_level = range_high if broken_dir == "UP" else range_low
        for i in range(break_idx + 1, min(end_idx, len(df))):
            low = float(df["low"].iloc[i])
            high = float(df["high"].iloc[i])
            close = float(df["close"].iloc[i])

            if broken_dir == "UP":
                # Retest = low van bar raakt of dipt onder range_high
                # Confirmation: close blijft boven range_high
                if low <= retest_level and close > retest_level:
                    retest_idx = i
                    break
            else:
                # Retest van range_low van bovenaf
                if high >= retest_level and close < retest_level:
                    retest_idx = i
                    break

        if retest_idx is None:
            return None

        # Build signal
        range_size = range_high - range_low
        if broken_dir == "UP":
            direction = "LONG"
            entry_price = retest_level  # break level as retest entry
            sl = range_low - 2 * pip_size
        else:
            direction = "SHORT"
            entry_price = retest_level
            sl = range_high + 2 * pip_size

        if self.rr is not None and self.rr > 0:
            # RR-based TP
            if direction == "LONG":
                risk = entry_price - sl
                tp = entry_price + self.rr * risk
            else:
                risk = sl - entry_price
                tp = entry_price - self.rr * risk
        else:
            # Range projection TP
            if direction == "LONG":
                tp = entry_price + range_size * self.tp_range_mult
            else:
                tp = entry_price - range_size * self.tp_range_mult

        # Validatie
        if direction == "LONG" and (entry_price - sl) <= 0:
            return None
        if direction == "SHORT" and (sl - entry_price) <= 0:
            return None

        return {
            "strategy":     "AB",
            "pair":         pair,
            "direction":    direction,
            "entry":        float(entry_price),
            "sl":           float(sl),
            "tp":           float(tp),
            "range_high":   range_high,
            "range_low":    range_low,
            "range_pips":   round(range_size / pip_size, 1),
            "broken_dir":   broken_dir,
            "break_idx":    break_idx,
            "retest_idx":   retest_idx,
            "ts":           str(df.index[retest_idx]),
        }

    def detect_signal(self, df, pair):
        """Live detection — kijk of vandaag een Asian Range Breakout setup heeft.

        Returns: signal dict of None.
        """
        if df is None or len(df) < 20:
            return None

        now = now_brussels()
        # Alleen tijdens breakout window (07:00-12:00 Brussels) genereren we signalen
        if not (self.ASIAN_END_HOUR <= now.hour < self.BREAKOUT_END_HOUR):
            return None

        # Find Asian range voor vandaag
        high, low, end_idx = self._find_asian_range_for_day(df, now.date())
        if high is None or low is None:
            return None

        # Range size filter
        pip_size = PIP.get(pair, 0.0001)
        range_pips = (high - low) / pip_size
        min_p, max_p = self._get_range_filters(pair)
        if range_pips < min_p or range_pips > max_p:
            return None

        # Scan voor breakout + retest na 07:00 (end_idx)
        breakout_end_dt = TZ.localize(datetime.combine(now.date(), datetime.min.time())).replace(hour=self.BREAKOUT_END_HOUR)
        try:
            breakout_end_idx = df.index.get_indexer([breakout_end_dt], method="nearest")[0]
        except Exception:
            breakout_end_idx = len(df) - 1

        signal = self._scan_breakout_and_retest(
            df, high, low, end_idx, breakout_end_idx, pair
        )
        if signal is None:
            return None

        # Live mode: alleen retest die zojuist gebeurd is (laatste bar of 2)
        if signal["retest_idx"] < len(df) - 3:
            return None  # te oude retest

        return signal


# ════════════════════════════════════════════════════════════
# LIVE ENGINE
# ════════════════════════════════════════════════════════════

class GamanXEngine:
    """Multi-strategy engine. Holds detectors, scans, manages open trades."""

    def __init__(self):
        self.running = False
        self.paused = False
        self.lock = threading.Lock()
        self.open_trades = []
        self.closed_trades = []
        self.scan_count = 0
        self.last_scan_ts = None
        self.config = self._default_config()
        # Detectors
        self.detectors = {
            "SB":  SilverBulletDetector(),
            "CH":  CHoCHDetector(),
            "BOS": BOSDetector(),
            "AB":  AsianBreakoutDetector(),
        }
        self._load_state()
        self._scan_thread = None

    def _default_config(self):
        return {
            "pair": "BOTH",                  # EURUSD / XAUUSD / BOTH
            "tfs": ["15M"],                  # list — kan ["15M"], ["1H"], ["15M","1H"], etc.
            "capital": 10000,
            "lot_eur": 10,                   # micro lots
            "lot_xau": 1,
            "rr": 2.0,
            "discord_webhook": "",
            "strategies": {
                "SB":  {"enabled": True,  "killzones": ["london","ny_am","ny_pm"], "disp_atr": 1.5},
                "CH":  {"enabled": False, "swing_length": 5},
                "BOS": {"enabled": False, "swing_length": 5},
                "AB":  {"enabled": False, "min_range_pips": None, "max_range_pips": None, "tp_range_mult": 1.5, "use_rr": False},
            },
            "max_open_trades": 4,
        }

    def _load_state(self):
        if not STATE_FILE.exists(): return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            self.open_trades   = data.get("open_trades", [])
            self.closed_trades = data.get("closed_trades", [])
            self.config        = {**self._default_config(), **data.get("config", {})}
            # Backward compat: oude config heeft "tf" string, nieuwe heeft "tfs" list
            if "tf" in self.config and "tfs" not in data.get("config", {}):
                self.config["tfs"] = [self.config.pop("tf")]
            elif "tf" in self.config:
                # tfs is set, drop legacy tf
                self.config.pop("tf", None)
            # Safety: if tfs is empty or invalid, default
            if not self.config.get("tfs") or not isinstance(self.config.get("tfs"), list):
                self.config["tfs"] = ["15M"]
            log("INFO", f"State loaded: {len(self.open_trades)} open, {len(self.closed_trades)} closed")
        except Exception as e:
            log("WARN", f"State load failed: {e}")

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "open_trades":   self.open_trades,
                    "closed_trades": self.closed_trades,
                    "config":        self.config,
                }, f, indent=2, default=str)
        except Exception as e:
            log("WARN", f"State save failed: {e}")

    def start(self):
        if self.running:
            return False
        self.running = True
        self.paused = False
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()
        log("INFO", "GAMAN-X engine STARTED")
        return True

    def stop(self):
        self.running = False
        log("INFO", "GAMAN-X engine STOPPED")
        self._save_state()

    def pause(self, paused=True):
        self.paused = paused
        log("INFO", f"GAMAN-X engine {'PAUSED' if paused else 'RESUMED'}")

    def _scan_loop(self):
        while self.running:
            try:
                if not self.paused:
                    self._do_scan()
            except Exception as e:
                log("ERROR", f"Scan loop error: {e}")
                log("ERROR", traceback.format_exc())
            time.sleep(SCAN_INTERVAL)

    def _do_scan(self):
        """One full scan cycle. Iterate over pairs * timeframes * strategies."""
        self.scan_count += 1
        self.last_scan_ts = fmt_brussels()

        # Determine pairs to scan
        pairs = ["EURUSD","XAUUSD"] if self.config["pair"] == "BOTH" else [self.config["pair"]]
        tfs = self.config.get("tfs", ["15M"])
        if not tfs or not isinstance(tfs, list):
            tfs = ["15M"]

        # Monitor open trades first (close at SL/TP)
        self._monitor_open_trades()

        # Check open trades cap
        if len(self.open_trades) >= self.config.get("max_open_trades", 4):
            return

        # Iterate strategies × tfs × pairs
        for sid, sconfig in self.config["strategies"].items():
            if not sconfig.get("enabled", False):
                continue
            detector = self.detectors.get(sid)
            if detector is None:
                continue

            # Update detector config (re-init if needed)
            if sid == "SB":
                detector.enabled_killzones = sconfig.get("killzones", ["london","ny_am","ny_pm"])
                detector.displacement_atr_mult = sconfig.get("disp_atr", 1.5)
                detector.rr = self.config.get("rr", 2.0)
            elif sid == "CH":
                detector.swing_length = sconfig.get("swing_length", 5)
                detector.rr = self.config.get("rr", 2.0)
            elif sid == "BOS":
                detector.swing_length = sconfig.get("swing_length", 5)
                detector.rr = self.config.get("rr", 2.0)
            elif sid == "AB":
                detector.min_range_pips_override = sconfig.get("min_range_pips")
                detector.max_range_pips_override = sconfig.get("max_range_pips")
                detector.tp_range_mult = sconfig.get("tp_range_mult", 1.5)
                use_rr = sconfig.get("use_rr", False)
                detector.rr = self.config.get("rr", 2.0) if use_rr else None

            # Scan each TF × pair combo
            for tf in tfs:
                for pair in pairs:
                    try:
                        df = fetch_candles(pair, tf)
                        if df is None or len(df) < 30:
                            continue
                        signal = detector.detect_signal(df, pair)
                        if signal:
                            # Dedup per strategy+pair+tf — voorkomt 15M en 1H gelijktijdig dezelfde setup
                            if self._has_recent_trade(sid, pair, tf=tf, minutes=30):
                                continue
                            # Tag signal met TF voor tracking
                            signal["tf"] = tf
                            self._open_trade(signal)
                    except Exception as e:
                        log("WARN", f"Scan {sid} {pair} {tf}: {e}")

    def _has_recent_trade(self, sid, pair, tf=None, minutes=30):
        """Check if we've opened a similar trade recently.
        If tf is given, dedup is per strategy+pair+tf (15M en 1H mogen los).
        If tf is None, dedup per strategy+pair only (legacy behavior).
        """
        cutoff = (now_brussels() - timedelta(minutes=minutes)).timestamp()
        for t in self.open_trades + self.closed_trades:
            if t.get("strategy") != sid or t.get("pair") != pair:
                continue
            if tf is not None and t.get("tf") and t.get("tf") != tf:
                continue
            ts = t.get("opened_ts", 0)
            if ts >= cutoff:
                return True
        return False

    def _open_trade(self, signal):
        sid = signal["strategy"]
        # Generate trade ID with strategy prefix
        existing = [t for t in self.open_trades + self.closed_trades if t.get("strategy") == sid]
        trade_id = f"{sid}-{len(existing)+1:03d}"

        lot = self.config["lot_eur"] if signal["pair"] == "EURUSD" else self.config["lot_xau"]

        trade = {
            "id":          trade_id,
            "strategy":    sid,
            "pair":        signal["pair"],
            "tf":          signal.get("tf", ""),
            "direction":   signal["direction"],
            "entry":       signal["entry"],
            "sl":          signal["sl"],
            "tp":          signal["tp"],
            "lotsize":     lot,
            "opened_at":   fmt_brussels(),
            "opened_ts":   int(now_brussels().timestamp()),
            "meta":        {k: v for k, v in signal.items() if k not in ("strategy","pair","direction","entry","sl","tp","tf")},
            "pnl_eur":     0.0,
            "status":      "OPEN",
        }

        with self.lock:
            self.open_trades.append(trade)

        log("TRADE", f"OPEN {trade_id} {signal['pair']} {signal['direction']} @ {signal['entry']:.5f} SL={signal['sl']:.5f} TP={signal['tp']:.5f}")

        # Discord notify
        self._discord(f"📈 **{sid} OPEN** — {signal['pair']} {signal['direction']}\nEntry: `{signal['entry']:.5f}`\nSL: `{signal['sl']:.5f}` | TP: `{signal['tp']:.5f}`\nMeta: {signal.get('killzone', '-')}")
        self._save_state()

    def _monitor_open_trades(self):
        """Check elke open trade voor SL/TP hit."""
        to_close = []
        for trade in self.open_trades:
            try:
                price = fetch_price(trade["pair"])
                if price is None:
                    continue
                hit = None
                if trade["direction"] == "LONG":
                    if price <= trade["sl"]:  hit = "SL"
                    elif price >= trade["tp"]: hit = "TP"
                else:
                    if price >= trade["sl"]:  hit = "SL"
                    elif price <= trade["tp"]: hit = "TP"
                # Update live P&L
                pip_size = PIP.get(trade["pair"], 0.0001)
                pip_value = PIP_EUR.get(trade["pair"], 0.10)
                if trade["direction"] == "LONG":
                    pips = (price - trade["entry"]) / pip_size
                else:
                    pips = (trade["entry"] - price) / pip_size
                trade["pnl_eur"] = pips * pip_value * trade["lotsize"]
                trade["live_price"] = price

                if hit:
                    to_close.append((trade, hit, price))
            except Exception as e:
                log("WARN", f"Monitor {trade['id']}: {e}")

        for trade, hit, exit_price in to_close:
            self._close_trade(trade, hit, exit_price)

    def _close_trade(self, trade, hit, exit_price):
        with self.lock:
            self.open_trades = [t for t in self.open_trades if t["id"] != trade["id"]]

        # Compute final P&L
        pip_size = PIP.get(trade["pair"], 0.0001)
        pip_value = PIP_EUR.get(trade["pair"], 0.10)
        if trade["direction"] == "LONG":
            pips = (exit_price - trade["entry"]) / pip_size
        else:
            pips = (trade["entry"] - exit_price) / pip_size
        pnl_eur = pips * pip_value * trade["lotsize"]

        closed = {
            **trade,
            "exit":        exit_price,
            "closed_at":   fmt_brussels(),
            "hit":         hit,
            "pips":        round(pips, 1),
            "pnl_eur":     round(pnl_eur, 2),
            "status":      "CLOSED",
        }
        self.closed_trades.append(closed)

        icon = "✅" if hit == "TP" else "❌"
        log("TRADE", f"CLOSE {trade['id']} @ {exit_price:.5f} [{hit}] {pips:+.1f}p €{pnl_eur:+.2f}")
        self._discord(f"{icon} **{trade['strategy']} CLOSE** — {trade['pair']} {trade['direction']}\nExit: `{exit_price:.5f}` [{hit}]\n{pips:+.1f} pips | €{pnl_eur:+.2f}")
        self._save_state()

    def _discord(self, msg):
        url = self.config.get("discord_webhook", "")
        if not url:
            return
        try:
            requests.post(url, json={"content": msg, "username": "GAMAN-X"}, timeout=5)
        except Exception as e:
            log("WARN", f"Discord send failed: {e}")

    def get_status(self):
        return {
            "running":      self.running,
            "paused":       self.paused,
            "scan_count":   self.scan_count,
            "last_scan":    self.last_scan_ts,
            "open_trades":  self.open_trades,
            "closed_trades": self.closed_trades[-50:],  # last 50
            "config":       self.config,
            "stats":        self._compute_stats(),
        }

    def _compute_stats(self):
        """Per-strategy stats from closed_trades."""
        stats = {}
        for sid in ["SB","CH","BOS","AB"]:
            trades = [t for t in self.closed_trades if t.get("strategy") == sid]
            wins = [t for t in trades if t.get("pnl_eur", 0) > 0]
            pnl = sum(t.get("pnl_eur", 0) for t in trades)
            open_count = len([t for t in self.open_trades if t.get("strategy") == sid])
            stats[sid] = {
                "trades": len(trades),
                "wr":     round(100 * len(wins) / len(trades), 1) if trades else 0,
                "pnl":    round(pnl, 2),
                "open":   open_count,
            }
        return stats


# ════════════════════════════════════════════════════════════
# BACKTESTER — Generic + Silver Bullet + CHoCH
# ════════════════════════════════════════════════════════════

def _simulate_forward(df, i, signal, max_bars=50):
    """Simulate forward bars to find SL/TP exit.
    Returns: (exit_idx, exit_price, hit) — hit is 'SL'|'TP'|'TIMEOUT'.
    """
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    direction = signal["direction"]
    n = len(df)

    for j in range(i+1, min(i + max_bars, n)):
        h = df["high"].iloc[j]
        l = df["low"].iloc[j]
        if direction == "LONG":
            if l <= sl:
                return j, float(sl), "SL"
            if h >= tp:
                return j, float(tp), "TP"
        else:
            if h >= sl:
                return j, float(sl), "SL"
            if l <= tp:
                return j, float(tp), "TP"

    # Timeout
    exit_idx = min(i + max_bars, n - 1)
    return exit_idx, float(df["close"].iloc[exit_idx]), "TIMEOUT"


def _compute_backtest_stats(trades, capital):
    """Standard stats berekening uit trades lijst."""
    if not trades:
        return {"total": 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0
    avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0
    pf = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses and sum(t["pnl"] for t in losses) != 0 else float("inf")

    equity = capital
    peak = capital
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        if equity > peak: peak = equity
        dd = equity - peak
        if dd < max_dd: max_dd = dd

    return {
        "total":    len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "wr":       round(100 * len(wins) / len(trades), 1),
        "pnl":      round(total_pnl, 2),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pf":       round(pf, 2) if pf != float("inf") else 999,
        "max_dd":   round(max_dd, 2),
    }


def backtest_silver_bullet(pair, tf, start_date, end_date, capital=10000,
                           lot=10, rr=2.0, killzones=None, disp_atr=1.5):
    """Run Silver Bullet backtest over historical period."""
    log("INFO", f"Backtest SB {pair} {tf} {start_date} -> {end_date}")
    df = fetch_candles(pair, tf, start=start_date, end=end_date)
    if df is None or len(df) < 50:
        return {"error": "No data for this period"}

    detector = SilverBulletDetector(
        enabled_killzones=killzones,
        displacement_atr_mult=disp_atr,
        rr=rr,
    )
    diagnostics = {
        "candles_scanned":              0,
        "insufficient_data":            0,
        "not_in_killzone":              0,
        "no_atr":                       0,
        "killzone_no_displacement":     0,
        "no_sweep_after_displacement":  0,
        "no_fvg_after_sweep":           0,
        "invalid_risk":                 0,
        "signals_generated":            0,
    }
    pip_size = PIP.get(pair, 0.0001)
    pip_value = PIP_EUR.get(pair, 0.10)
    trades = []
    n = len(df)

    last_exit_idx = -1
    for i in range(50, n - 1):
        diagnostics["candles_scanned"] += 1
        if last_exit_idx >= i - 2:
            continue
        window = df.iloc[:i+1]
        signal = detector.detect_signal(window, pair, diagnostics=diagnostics)
        if signal is None:
            continue

        exit_idx, exit_price, hit = _simulate_forward(df, i, signal, max_bars=50)
        if signal["direction"] == "LONG":
            pips = (exit_price - signal["entry"]) / pip_size
        else:
            pips = (signal["entry"] - exit_price) / pip_size
        pnl = pips * pip_value * lot

        trades.append({
            "ts":         str(df.index[i]),
            "exit_ts":    str(df.index[exit_idx]),
            "pair":       pair,
            "direction":  signal["direction"],
            "entry":      round(signal["entry"], 5),
            "exit":       round(exit_price, 5),
            "sl":         round(signal["sl"], 5),
            "tp":         round(signal["tp"], 5),
            "pips":       round(pips, 1),
            "pnl":        round(pnl, 2),
            "hit":        hit,
            "killzone":   signal.get("killzone"),
            "strategy":   "SB",
        })
        last_exit_idx = exit_idx

    result = {"diagnostics": diagnostics}
    if not trades:
        result.update({"trades": [], "stats": {"total": 0}, "msg": "No setups found in this period"})
    else:
        result.update({"trades": trades, "stats": _compute_backtest_stats(trades, capital)})
    return result


def backtest_choch(pair, tf, start_date, end_date, capital=10000,
                   lot=10, rr=2.0, swing_length=5):
    """Run CHoCH backtest over historical period."""
    log("INFO", f"Backtest CHoCH {pair} {tf} {start_date} -> {end_date}")
    df = fetch_candles(pair, tf, start=start_date, end=end_date)
    if df is None or len(df) < swing_length * 4 + 20:
        return {"error": "No data for this period"}

    detector = CHoCHDetector(swing_length=swing_length, rr=rr)
    diagnostics = {
        "candles_scanned":   0,
        "insufficient_data": 0,
        "not_enough_swings": 0,
        "trend_neutral":     0,
        "no_choch_break":    0,
        "invalid_risk":      0,
        "signals_generated": 0,
    }
    pip_size = PIP.get(pair, 0.0001)
    pip_value = PIP_EUR.get(pair, 0.10)
    trades = []
    n = len(df)

    last_exit_idx = -1
    min_bars = swing_length * 4 + 5
    for i in range(min_bars, n - 1):
        diagnostics["candles_scanned"] += 1
        if last_exit_idx >= i - 2:
            continue
        window = df.iloc[:i+1]
        signal = detector.detect_signal(window, pair, diagnostics=diagnostics)
        if signal is None:
            continue

        exit_idx, exit_price, hit = _simulate_forward(df, i, signal, max_bars=80)
        if signal["direction"] == "LONG":
            pips = (exit_price - signal["entry"]) / pip_size
        else:
            pips = (signal["entry"] - exit_price) / pip_size
        pnl = pips * pip_value * lot

        trades.append({
            "ts":          str(df.index[i]),
            "exit_ts":     str(df.index[exit_idx]),
            "pair":        pair,
            "direction":   signal["direction"],
            "entry":       round(signal["entry"], 5),
            "exit":        round(exit_price, 5),
            "sl":          round(signal["sl"], 5),
            "tp":          round(signal["tp"], 5),
            "pips":        round(pips, 1),
            "pnl":         round(pnl, 2),
            "hit":         hit,
            "prior_trend": signal.get("prior_trend"),
            "strategy":    "CH",
        })
        last_exit_idx = exit_idx

    result = {"diagnostics": diagnostics}
    if not trades:
        result.update({"trades": [], "stats": {"total": 0}, "msg": "No CHoCH setups found in this period"})
    else:
        result.update({"trades": trades, "stats": _compute_backtest_stats(trades, capital)})
    return result


def backtest_bos(pair, tf, start_date, end_date, capital=10000,
                 lot=10, rr=2.0, swing_length=5):
    """Run BOS Continuation backtest over historical period."""
    log("INFO", f"Backtest BOS {pair} {tf} {start_date} -> {end_date}")
    df = fetch_candles(pair, tf, start=start_date, end=end_date)
    if df is None or len(df) < swing_length * 4 + 20:
        return {"error": "No data for this period"}

    detector = BOSDetector(swing_length=swing_length, rr=rr)
    diagnostics = {
        "candles_scanned":   0,
        "insufficient_data": 0,
        "not_enough_swings": 0,
        "trend_neutral":     0,
        "no_bos_break":      0,
        "invalid_risk":      0,
        "signals_generated": 0,
    }
    pip_size = PIP.get(pair, 0.0001)
    pip_value = PIP_EUR.get(pair, 0.10)
    trades = []
    n = len(df)

    last_exit_idx = -1
    min_bars = swing_length * 4 + 5
    for i in range(min_bars, n - 1):
        diagnostics["candles_scanned"] += 1
        if last_exit_idx >= i - 2:
            continue
        window = df.iloc[:i+1]
        signal = detector.detect_signal(window, pair, diagnostics=diagnostics)
        if signal is None:
            continue

        exit_idx, exit_price, hit = _simulate_forward(df, i, signal, max_bars=80)
        if signal["direction"] == "LONG":
            pips = (exit_price - signal["entry"]) / pip_size
        else:
            pips = (signal["entry"] - exit_price) / pip_size
        pnl = pips * pip_value * lot

        trades.append({
            "ts":          str(df.index[i]),
            "exit_ts":     str(df.index[exit_idx]),
            "pair":        pair,
            "direction":   signal["direction"],
            "entry":       round(signal["entry"], 5),
            "exit":        round(exit_price, 5),
            "sl":          round(signal["sl"], 5),
            "tp":          round(signal["tp"], 5),
            "pips":        round(pips, 1),
            "pnl":         round(pnl, 2),
            "hit":         hit,
            "trend":       signal.get("trend"),
            "strategy":    "BOS",
        })
        last_exit_idx = exit_idx

    result = {"diagnostics": diagnostics}
    if not trades:
        result.update({"trades": [], "stats": {"total": 0}, "msg": "No BOS setups found in this period"})
    else:
        result.update({"trades": trades, "stats": _compute_backtest_stats(trades, capital)})
    return result


def backtest_all_strategies(pair, tf, start_date, end_date, capital=10000,
                            lot=10, rr=2.0, swing_length=5, disp_atr=1.5,
                            killzones=None, ab_tp_mult=1.5, strategies=None):
    """Run geselecteerde strategieën apart en combineer resultaten in 1 view.

    Args:
        strategies: list van strategie IDs to run. None = alle 4.
                    Geldige waarden: "SB", "CH", "BOS", "AB"
    """
    if not strategies:
        strategies = ["SB", "CH", "BOS", "AB"]
    # Filter naar valide
    strategies = [s for s in strategies if s in ("SB","CH","BOS","AB")]
    if not strategies:
        return {"error": "No valid strategies selected"}

    log("INFO", f"Backtest MIX {strategies} {pair} {tf} {start_date} -> {end_date}")

    per_strategy = {}

    if "SB" in strategies:
        per_strategy["SB"] = backtest_silver_bullet(
            pair=pair, tf=tf, start_date=start_date, end_date=end_date,
            capital=capital, lot=lot, rr=rr,
            killzones=killzones or ["london","ny_am","ny_pm"], disp_atr=disp_atr,
        )

    if "CH" in strategies:
        per_strategy["CH"] = backtest_choch(
            pair=pair, tf=tf, start_date=start_date, end_date=end_date,
            capital=capital, lot=lot, rr=rr, swing_length=swing_length,
        )

    if "BOS" in strategies:
        per_strategy["BOS"] = backtest_bos(
            pair=pair, tf=tf, start_date=start_date, end_date=end_date,
            capital=capital, lot=lot, rr=rr, swing_length=swing_length,
        )

    if "AB" in strategies:
        per_strategy["AB"] = backtest_asian_breakout(
            pair=pair, tf=tf, start_date=start_date, end_date=end_date,
            capital=capital, lot=lot, rr=None, tp_range_mult=ab_tp_mult,
        )

    # Combineer alle trades, sort by ts
    all_trades = []
    for sid, res in per_strategy.items():
        if isinstance(res, dict) and "trades" in res:
            all_trades.extend(res.get("trades", []))
    all_trades.sort(key=lambda t: t.get("ts", ""))

    # Per-strategy summary
    summary = {}
    for sid, res in per_strategy.items():
        if isinstance(res, dict) and "stats" in res:
            summary[sid] = res["stats"]
        else:
            summary[sid] = {"total": 0, "error": res.get("error") if isinstance(res, dict) else None}

    return {
        "mode":           "mix",
        "strategies_run": strategies,
        "trades":         all_trades,
        "per_strategy":   summary,
        "stats":          _compute_backtest_stats(all_trades, capital) if all_trades else {"total": 0},
        "diagnostics_per_strategy": {sid: res.get("diagnostics", {}) for sid, res in per_strategy.items() if isinstance(res, dict)},
    }


def backtest_asian_breakout(pair, tf, start_date, end_date, capital=10000,
                            lot=10, rr=None, tp_range_mult=1.5,
                            min_range_pips=None, max_range_pips=None):
    """Run Asian Range Breakout backtest. Per-dag scan."""
    log("INFO", f"Backtest Asian {pair} {tf} {start_date} -> {end_date}")
    df = fetch_candles(pair, tf, start=start_date, end=end_date)
    if df is None or len(df) < 50:
        return {"error": "No data for this period"}

    detector = AsianBreakoutDetector(
        min_range_pips=min_range_pips,
        max_range_pips=max_range_pips,
        tp_range_mult=tp_range_mult,
        rr=rr,
    )
    pip_size = PIP.get(pair, 0.0001)
    pip_value = PIP_EUR.get(pair, 0.10)
    trades = []
    diagnostics = {
        "days_checked":          0,
        "weekend_skipped":       0,
        "no_asian_range":        0,
        "range_too_small":       0,
        "range_too_big":         0,
        "no_breakout":           0,
        "no_retest":             0,
        "signals_generated":     0,
    }

    start_dt = pd.to_datetime(start_date).date()
    end_dt = pd.to_datetime(end_date).date()
    current = start_dt + timedelta(days=1)

    while current <= end_dt:
        if current.weekday() >= 5:
            diagnostics["weekend_skipped"] += 1
            current += timedelta(days=1)
            continue
        diagnostics["days_checked"] += 1

        high, low, end_idx = detector._find_asian_range_for_day(df, current)
        if high is None or low is None:
            diagnostics["no_asian_range"] += 1
            current += timedelta(days=1)
            continue

        range_pips = (high - low) / pip_size
        min_p, max_p = detector._get_range_filters(pair)
        if range_pips < min_p:
            diagnostics["range_too_small"] += 1
            current += timedelta(days=1)
            continue
        if range_pips > max_p:
            diagnostics["range_too_big"] += 1
            current += timedelta(days=1)
            continue

        breakout_end_dt = TZ.localize(datetime.combine(current, datetime.min.time())).replace(hour=detector.BREAKOUT_END_HOUR)
        try:
            breakout_end_idx = df.index.get_indexer([breakout_end_dt], method="nearest")[0]
        except Exception:
            breakout_end_idx = min(end_idx + 30, len(df) - 1)

        signal = detector._scan_breakout_and_retest(df, high, low, end_idx, breakout_end_idx, pair)
        if signal is None:
            # Check if there was a breakout but no retest (logged inside scanner)
            diagnostics["no_breakout"] += 1  # simplified
            current += timedelta(days=1)
            continue

        diagnostics["signals_generated"] += 1
        entry_idx = signal["retest_idx"]
        eod_dt = TZ.localize(datetime.combine(current, datetime.min.time())).replace(hour=22)
        try:
            eod_idx = df.index.get_indexer([eod_dt], method="nearest")[0]
        except Exception:
            eod_idx = entry_idx + 50
        max_bars = min(50, eod_idx - entry_idx)

        exit_idx, exit_price, hit = _simulate_forward(df, entry_idx, signal, max_bars=max_bars)
        if signal["direction"] == "LONG":
            pips = (exit_price - signal["entry"]) / pip_size
        else:
            pips = (signal["entry"] - exit_price) / pip_size
        pnl = pips * pip_value * lot

        trades.append({
            "ts":          str(df.index[entry_idx]),
            "exit_ts":     str(df.index[exit_idx]),
            "pair":        pair,
            "direction":   signal["direction"],
            "entry":       round(signal["entry"], 5),
            "exit":        round(exit_price, 5),
            "sl":          round(signal["sl"], 5),
            "tp":          round(signal["tp"], 5),
            "pips":        round(pips, 1),
            "pnl":         round(pnl, 2),
            "hit":         hit,
            "range_pips":  signal["range_pips"],
            "broken_dir":  signal["broken_dir"],
            "strategy":    "AB",
        })

        current += timedelta(days=1)

    result = {"diagnostics": diagnostics}
    if not trades:
        result.update({"trades": [], "stats": {"total": 0}, "msg": "No Asian Breakout setups found in this period"})
    else:
        result.update({"trades": trades, "stats": _compute_backtest_stats(trades, capital)})
    return result


# ════════════════════════════════════════════════════════════
# FLASK APP
# ════════════════════════════════════════════════════════════

app = Flask(__name__)
engine = GamanXEngine()

@app.route("/")
def index():
    ua = request.headers.get("User-Agent", "").lower()
    is_mobile = any(k in ua for k in ["iphone", "android", "mobile", "ipad"])
    # Allow forced override via query: ?view=mobile or ?view=desktop
    force = request.args.get("view")
    if force == "mobile": is_mobile = True
    elif force == "desktop": is_mobile = False
    return render_template_string(MOBILE_HTML if is_mobile else HTML_TEMPLATE)

@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, **engine.get_status()})

@app.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    body = request.json or {}
    cfg = engine.config
    cfg["pair"] = body.get("pair", cfg["pair"])
    # Accept either "tfs" (list) or "tf" (legacy single) — normalize to list
    if "tfs" in body:
        tfs = body["tfs"]
        if isinstance(tfs, str): tfs = [tfs]
        cfg["tfs"] = tfs if tfs else ["15M"]
    elif "tf" in body:
        cfg["tfs"] = [body["tf"]]
    cfg["lot_eur"] = body.get("lot_eur", cfg["lot_eur"])
    cfg["lot_xau"] = body.get("lot_xau", cfg["lot_xau"])
    cfg["rr"]      = body.get("rr",      cfg["rr"])
    cfg["discord_webhook"] = body.get("discord_webhook", cfg["discord_webhook"])
    if "strategies" in body:
        for sid, sc in body["strategies"].items():
            if sid in cfg["strategies"]:
                cfg["strategies"][sid].update(sc)
    started = engine.start()
    return jsonify({"ok": True, "started": started, "config": cfg})

@app.route("/api/engine/stop", methods=["POST"])
def api_engine_stop():
    engine.stop()
    return jsonify({"ok": True})

@app.route("/api/engine/pause", methods=["POST"])
def api_engine_pause():
    engine.pause(True)
    return jsonify({"ok": True})

@app.route("/api/engine/resume", methods=["POST"])
def api_engine_resume():
    engine.pause(False)
    return jsonify({"ok": True})

@app.route("/api/engine/config", methods=["POST"])
def api_engine_config():
    body = request.json or {}
    cfg = engine.config
    # Normalize: if "tf" string given, convert to ["tf"] list
    if "tf" in body and "tfs" not in body:
        body["tfs"] = [body.pop("tf")]
    # Allow partial updates
    for k, v in body.items():
        if k == "strategies" and isinstance(v, dict):
            for sid, sc in v.items():
                if sid in cfg["strategies"]:
                    cfg["strategies"][sid].update(sc)
        elif k == "tfs":
            tfs = v if isinstance(v, list) else [v]
            cfg["tfs"] = tfs if tfs else ["15M"]
            cfg.pop("tf", None)  # drop legacy
        else:
            cfg[k] = v
    engine._save_state()
    return jsonify({"ok": True, "config": cfg})

@app.route("/api/trade/<tid>/close", methods=["POST"])
def api_trade_close(tid):
    trade = next((t for t in engine.open_trades if t["id"] == tid), None)
    if not trade:
        return jsonify({"ok": False, "error": "Trade not found"}), 404
    price = fetch_price(trade["pair"])
    if price is None:
        return jsonify({"ok": False, "error": "Could not fetch price"}), 500
    engine._close_trade(trade, "MANUAL", price)
    return jsonify({"ok": True})


# ── Presets ──
def _load_presets():
    if not PRESETS_FILE.exists(): return {}
    try:
        with open(PRESETS_FILE) as f:
            return json.load(f)
    except Exception as e:
        log("WARN", f"Presets load failed: {e}")
        return {}

def _save_presets(presets):
    try:
        with open(PRESETS_FILE, "w") as f:
            json.dump(presets, f, indent=2, default=str)
        return True
    except Exception as e:
        log("WARN", f"Presets save failed: {e}")
        return False

def _preset_summary(cfg):
    """Compact summary of preset config."""
    strats = cfg.get("strategies", {})
    enabled = [sid for sid, sc in strats.items() if sc.get("enabled")]
    tfs = cfg.get("tfs", [cfg.get("tf", "15M")])
    return {
        "pair":   cfg.get("pair"),
        "tfs":    tfs,
        "strats": enabled,
        "rr":     cfg.get("rr"),
    }

@app.route("/api/presets", methods=["GET"])
def api_presets_list():
    presets = _load_presets()
    light = {name: {"saved_at": p.get("saved_at"), "summary": _preset_summary(p.get("config", {}))} for name, p in presets.items()}
    return jsonify({"ok": True, "presets": light})

@app.route("/api/presets/<name>", methods=["POST"])
def api_presets_save(name):
    body = request.json or {}
    config = body.get("config") or engine.config
    presets = _load_presets()
    presets[name] = {
        "saved_at": fmt_brussels(),
        "config": config,
    }
    if _save_presets(presets):
        return jsonify({"ok": True, "preset": presets[name]})
    return jsonify({"ok": False, "error": "Save failed"}), 500

@app.route("/api/presets/<name>", methods=["DELETE"])
def api_presets_delete(name):
    presets = _load_presets()
    if name not in presets:
        return jsonify({"ok": False, "error": "Preset not found"}), 404
    del presets[name]
    if _save_presets(presets):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Delete failed"}), 500

@app.route("/api/presets/<name>/apply", methods=["POST"])
def api_presets_apply(name):
    presets = _load_presets()
    if name not in presets:
        return jsonify({"ok": False, "error": "Preset not found"}), 404
    preset_cfg = presets[name].get("config", {})
    for k, v in preset_cfg.items():
        if k == "strategies" and isinstance(v, dict):
            for sid, sc in v.items():
                if sid in engine.config["strategies"]:
                    engine.config["strategies"][sid].update(sc)
        else:
            engine.config[k] = v
    engine._save_state()
    return jsonify({"ok": True, "config": engine.config})


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    body = request.json or {}
    strategy = body.get("strategy", "SB")
    pair = body.get("pair", "EURUSD")
    tf = body.get("tf", "1H")
    start = body.get("start")
    end = body.get("end")
    capital = float(body.get("capital", 10000))
    lot = int(body.get("lot", 10))
    rr = float(body.get("rr", 2.0))

    if not start or not end:
        return jsonify({"error": "start and end dates required"}), 400

    if strategy not in ("SB", "CH", "BOS", "AB", "ALL", "MIX"):
        return jsonify({"error": f"Strategy {strategy} not yet implemented"}), 400

    if strategy in ("ALL", "MIX"):
        swing_length = int(body.get("swing_length", 5))
        disp_atr = float(body.get("disp_atr", 1.5))
        killzones = body.get("killzones", ["london","ny_am","ny_pm"])
        tp_mult = float(body.get("tp_range_mult", 1.5))
        # MIX: explicit list. ALL: all 4.
        strats_to_run = body.get("strategies") if strategy == "MIX" else None
        result = backtest_all_strategies(
            pair=pair, tf=tf,
            start_date=start, end_date=end,
            capital=capital, lot=lot, rr=rr,
            swing_length=swing_length, disp_atr=disp_atr,
            killzones=killzones, ab_tp_mult=tp_mult,
            strategies=strats_to_run,
        )
    elif strategy == "SB":
        killzones = body.get("killzones", ["london","ny_am","ny_pm"])
        disp_atr = float(body.get("disp_atr", 1.5))
        result = backtest_silver_bullet(
            pair=pair, tf=tf,
            start_date=start, end_date=end,
            capital=capital, lot=lot, rr=rr,
            killzones=killzones, disp_atr=disp_atr,
        )
    elif strategy == "CH":
        swing_length = int(body.get("swing_length", 5))
        result = backtest_choch(
            pair=pair, tf=tf,
            start_date=start, end_date=end,
            capital=capital, lot=lot, rr=rr,
            swing_length=swing_length,
        )
    elif strategy == "BOS":
        swing_length = int(body.get("swing_length", 5))
        result = backtest_bos(
            pair=pair, tf=tf,
            start_date=start, end_date=end,
            capital=capital, lot=lot, rr=rr,
            swing_length=swing_length,
        )
    else:  # AB
        min_range = body.get("min_range_pips")
        max_range = body.get("max_range_pips")
        tp_mult = float(body.get("tp_range_mult", 1.5))
        use_rr = body.get("use_rr", False)
        result = backtest_asian_breakout(
            pair=pair, tf=tf,
            start_date=start, end_date=end,
            capital=capital, lot=lot,
            rr=rr if use_rr else None,
            tp_range_mult=tp_mult,
            min_range_pips=int(min_range) if min_range else None,
            max_range_pips=int(max_range) if max_range else None,
        )
    return jsonify(result)

@app.route("/api/candles_around")
def api_candles_around():
    """Return candles in een window rond een specifieke timestamp — voor trade detail mini-chart.
    Query params: pair, tf, ts (ISO datetime), bars_before (default 30), bars_after (default 20)
    """
    pair = request.args.get("pair", "EURUSD")
    tf = request.args.get("tf", "15M")
    ts_str = request.args.get("ts", "")
    bars_before = int(request.args.get("bars_before", 30))
    bars_after = int(request.args.get("bars_after", 20))
    try:
        if not ts_str:
            return jsonify({"ok": False, "error": "Missing ts param"}), 400
        # Parse target timestamp
        target_dt = pd.to_datetime(ts_str)
        if target_dt.tzinfo is None:
            target_dt = target_dt.tz_localize(TZ)
        # Fetch a window — use yfinance for historic data
        # Compute approximate date range (extra padding for safety)
        tf_minutes = {"15M": 15, "1H": 60, "4H": 240}.get(tf, 15)
        before_minutes = (bars_before + 5) * tf_minutes
        after_minutes = (bars_after + 5) * tf_minutes
        start_dt = (target_dt - timedelta(minutes=before_minutes)).strftime("%Y-%m-%d")
        end_dt = (target_dt + timedelta(minutes=after_minutes + 1440)).strftime("%Y-%m-%d")  # +1 day padding
        df = fetch_candles_yf(pair, tf, start=start_dt, end=end_dt)
        if df is None or len(df) == 0:
            return jsonify({"ok": False, "error": "No data for this period"}), 500
        # Slice rond target_dt
        # Find index of bar at or nearest to target
        try:
            idx = df.index.get_indexer([target_dt], method="nearest")[0]
        except Exception:
            return jsonify({"ok": False, "error": "Could not locate timestamp"}), 500
        start_idx = max(0, idx - bars_before)
        end_idx = min(len(df), idx + bars_after + 1)
        sliced = df.iloc[start_idx:end_idx]
        candles = []
        target_idx_in_slice = idx - start_idx
        for i, (ts, row) in enumerate(sliced.iterrows()):
            candles.append({
                "t": int(ts.timestamp()),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "is_target": i == target_idx_in_slice,
            })
        return jsonify({"ok": True, "pair": pair, "tf": tf, "candles": candles, "target_idx": target_idx_in_slice})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/candles")
def api_candles():
    """Return last N candles for chart rendering."""
    pair = request.args.get("pair", "EURUSD")
    tf = request.args.get("tf", "15M")
    limit = int(request.args.get("limit", 200))
    try:
        df = fetch_candles(pair, tf)
        if df is None or len(df) == 0:
            return jsonify({"ok": False, "error": "No data"}), 500
        df = df.tail(limit)
        candles = []
        for ts, row in df.iterrows():
            candles.append({
                "t": int(ts.timestamp()),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            })
        return jsonify({"ok": True, "pair": pair, "tf": tf, "candles": candles})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": engine_log[-100:]})


# ════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ════════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GAMAN-X — Multi-Strategy</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Noto+Serif+JP:wght@900&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#04020f; --bg2:#0a0a18; --card:rgba(15,15,25,.85);
  --border:#2a2a3f; --border2:#3d3d5a;
  --glow:#ffffff; --glow2:#e5e7eb; --glow3:#f3f4f6;
  --text:#fafafa; --text2:#a8a8b8; --text3:#6b6b7a;
  --green:#34d399; --red:#f87171; --amber:#fbbf24;
  --r:10px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;overflow-x:hidden;min-height:100vh}
body::before{
  content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse 90% 60% at 50% -10%,rgba(255,255,255,.15) 0%,transparent 65%),
             radial-gradient(ellipse 50% 40% at 85% 90%,rgba(229,231,235,.10) 0%,transparent 55%);
  pointer-events:none;z-index:0;
}
#app{position:relative;z-index:2;padding:20px;max-width:1600px;margin:0 auto}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);margin-bottom:16px;backdrop-filter:blur(8px)}
.logo{font-family:'Noto Serif JP',serif;font-size:24px;font-weight:900;color:var(--glow);text-shadow:0 0 12px rgba(255,255,255,.5);letter-spacing:1px}
.logo span{font-family:'Inter',sans-serif;font-size:11px;font-weight:600;color:var(--text2);letter-spacing:2px;margin-left:8px;opacity:.7}
.engine-status{display:flex;align-items:center;gap:8px;font-family:'JetBrains Mono',monospace;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--text3)}
.dot.live{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.paused{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.grid{display:grid;grid-template-columns:1fr 420px;gap:16px}
@media(max-width:1100px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px;margin-bottom:16px;backdrop-filter:blur(8px)}
.card-title{font-size:11px;font-weight:700;color:var(--glow2);letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between}
.strategies{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.strat-card{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:8px;padding:12px;transition:.2s}
.strat-card.enabled{border-color:var(--glow);box-shadow:0 0 12px rgba(255,255,255,.1)}
.strat-card.placeholder{opacity:.5}
.strat-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.strat-name{font-weight:700;font-size:13px;color:var(--text)}
.strat-id{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--text3);letter-spacing:1px}
.strat-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;font-size:10px;color:var(--text2);margin-top:6px}
.strat-stats div{background:rgba(0,0,0,.3);padding:4px 6px;border-radius:4px;text-align:center}
.strat-stats b{color:var(--text);display:block;font-size:12px}
.switch{position:relative;width:34px;height:18px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#1a1a2e;border-radius:18px;cursor:pointer;transition:.3s;border:1px solid var(--border2)}
.slider::before{content:'';position:absolute;width:12px;height:12px;left:2px;top:2px;background:var(--text3);border-radius:50%;transition:.3s}
.switch input:checked + .slider{background:rgba(255,255,255,.2);border-color:var(--glow)}
.switch input:checked + .slider::before{transform:translateX(16px);background:var(--glow);box-shadow:0 0 8px var(--glow)}
.btn{background:rgba(255,255,255,.05);border:1px solid var(--border2);color:var(--text);padding:8px 14px;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;font-weight:600;transition:.2s}
.btn:hover{background:rgba(255,255,255,.1);border-color:var(--glow)}
.btn.primary{background:rgba(52,211,153,.15);border-color:var(--green);color:var(--green)}
.btn.danger{background:rgba(248,113,113,.15);border-color:var(--red);color:var(--red)}
.btn.warn{background:rgba(251,191,36,.15);border-color:var(--amber);color:var(--amber)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.form-group{display:flex;flex-direction:column}
.form-group label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.form-group input,.form-group select{background:rgba(0,0,0,.3);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-family:inherit;font-size:13px}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--glow)}
table{width:100%;border-collapse:collapse;font-size:12px;font-family:'JetBrains Mono',monospace}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600}
.dir-LONG{color:var(--green)}
.dir-SHORT{color:var(--red)}
.pnl-pos{color:var(--green)}
.pnl-neg{color:var(--red)}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{padding:8px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;background:rgba(255,255,255,.03);border:1px solid var(--border);color:var(--text2);transition:.2s}
.tab.active{background:rgba(255,255,255,.1);border-color:var(--glow);color:var(--text)}
.log-area{background:rgba(0,0,0,.4);border:1px solid var(--border);border-radius:6px;padding:10px;font-family:'JetBrains Mono',monospace;font-size:11px;max-height:300px;overflow:auto}
.log-line{padding:2px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.log-INFO{color:var(--text2)}
.log-WARN{color:var(--amber)}
.log-ERROR{color:var(--red)}
.log-TRADE{color:var(--green)}
.killzone-chip{display:inline-block;padding:3px 8px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:12px;font-size:10px;margin-right:4px;cursor:pointer;color:var(--text2)}
.killzone-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}
.placeholder-msg{padding:24px;text-align:center;color:var(--text3);font-size:12px;font-style:italic}
.chart-btn{background:rgba(255,255,255,.03);border:1px solid var(--border);color:var(--text2);padding:4px 10px;border-radius:4px;font-size:10px;cursor:pointer;font-family:inherit;transition:.2s}
.chart-btn:hover{background:rgba(255,255,255,.08)}
.chart-btn.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}
.strat-chip{display:inline-flex;align-items:center;padding:6px 12px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--text2);transition:.2s;user-select:none}
.strat-chip:hover{background:rgba(255,255,255,.08)}
.strat-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text);box-shadow:0 0 8px rgba(255,255,255,.1)}
.strat-chip::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text3);margin-right:6px;transition:.2s}
.strat-chip.active::before{background:var(--green);box-shadow:0 0 6px var(--green)}
.tf-chip{display:inline-flex;align-items:center;padding:6px 14px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--text2);transition:.2s;user-select:none;font-weight:600}
.tf-chip:hover{background:rgba(255,255,255,.08)}
.tf-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text);box-shadow:0 0 6px rgba(255,255,255,.1)}
</style>
</head>
<body>
<div id="app">

<div class="topbar">
  <div class="logo">無限<span>GAMAN-X · MULTI-STRATEGY</span></div>
  <div class="engine-status">
    <span class="dot" id="engineDot"></span>
    <span id="engineLabel">OFFLINE</span>
    <span style="color:var(--text3);margin:0 8px">|</span>
    <span id="scanCounter">Scans: 0</span>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="live" onclick="switchTab('live')">Live Trading</div>
  <div class="tab" data-tab="backtest" onclick="switchTab('backtest')">Backtester</div>
  <div class="tab" data-tab="logs" onclick="switchTab('logs')">System Logs</div>
</div>

<!-- LIVE TRADING TAB -->
<div id="tab-live">
<div class="grid">
<div>

<div class="card" id="chartCard">
  <div class="card-title">
    Market Chart
    <div style="display:flex;gap:6px">
      <button class="chart-btn active" data-pair="EURUSD" onclick="switchChartPair('EURUSD')">EURUSD</button>
      <button class="chart-btn" data-pair="XAUUSD" onclick="switchChartPair('XAUUSD')">XAUUSD</button>
      <span style="color:var(--text3);margin:0 6px">|</span>
      <button class="chart-btn active" data-tf="15M" onclick="switchChartTF('15M')">15M</button>
      <button class="chart-btn" data-tf="1H" onclick="switchChartTF('1H')">1H</button>
      <button class="chart-btn" data-tf="4H" onclick="switchChartTF('4H')">4H</button>
    </div>
  </div>
  <canvas id="priceChart" style="width:100%;height:300px;display:block;background:rgba(0,0,0,.3);border-radius:6px"></canvas>
  <div id="chartInfo" style="margin-top:8px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text3);display:flex;gap:16px">
    <span>Pair: <span id="chartPairLabel" style="color:var(--text)">EURUSD</span></span>
    <span>TF: <span id="chartTFLabel" style="color:var(--text)">15M</span></span>
    <span>Live: <span id="chartLivePrice" style="color:var(--glow)">-</span></span>
    <span>Candles: <span id="chartCandles" style="color:var(--text2)">-</span></span>
  </div>
</div>

<div class="card">
  <div class="card-title">Strategies <span id="lastScan" style="font-size:10px;color:var(--text3)"></span></div>
  <div class="strategies" id="stratGrid"></div>
</div>

<div class="card">
  <div class="card-title">Open Positions <span id="openCount" style="color:var(--text3);font-weight:400">0 open</span></div>
  <div id="openPos"></div>
</div>

<div class="card">
  <div class="card-title">Recent Closed Trades</div>
  <div id="closedTrades"></div>
</div>

</div>

<div>
<div class="card">
  <div class="card-title">Engine Control</div>
  <div style="display:flex;gap:8px;margin-bottom:16px">
    <button class="btn primary" onclick="engineStart()">▶ START</button>
    <button class="btn warn" onclick="engineTogglePause()" id="pauseBtn">⏸ PAUSE</button>
    <button class="btn danger" onclick="engineStop()">⏹ STOP</button>
  </div>
  
  <div class="form-row">
    <div class="form-group">
      <label>Pair</label>
      <select id="cfgPair"><option value="EURUSD">EURUSD</option><option value="XAUUSD">XAUUSD</option><option value="BOTH" selected>BOTH</option></select>
    </div>
    <div class="form-group">
      <label>Timeframes (1 of meer)</label>
      <div id="cfgTFPicker" style="display:flex;gap:6px;padding-top:4px">
        <span class="tf-chip active" data-tf="15M" onclick="toggleTFChip(this)">15M</span>
        <span class="tf-chip" data-tf="1H" onclick="toggleTFChip(this)">1H</span>
        <span class="tf-chip" data-tf="4H" onclick="toggleTFChip(this)">4H</span>
      </div>
    </div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Lot EUR (micro)</label><input type="number" id="cfgLotEur" value="10" min="1"></div>
    <div class="form-group"><label>Lot XAU (micro)</label><input type="number" id="cfgLotXau" value="1" min="1"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Risk:Reward</label><input type="number" id="cfgRR" value="2" min="0.5" step="0.5"></div>
    <div class="form-group"><label>Capital</label><input type="number" id="cfgCap" value="10000" min="100"></div>
  </div>
  <div class="form-group" style="margin-bottom:10px"><label>Discord webhook</label><input type="text" id="cfgDiscord" placeholder="https://discord.com/api/webhooks/..."></div>
</div>

<div class="card">
  <div class="card-title">Config Presets</div>
  <div style="display:flex;gap:6px;margin-bottom:10px">
    <input type="text" id="presetName" placeholder="Preset naam (bv. EUR-Aggressive)" style="flex:1;background:rgba(0,0,0,.3);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-family:inherit;font-size:13px">
    <button class="btn primary" onclick="savePreset()" style="white-space:nowrap;font-size:11px;padding:8px 14px">💾 Save</button>
  </div>
  <div id="presetList"></div>
</div>

<div class="card">
  <div class="card-title">Silver Bullet Config</div>
  <div style="margin-bottom:10px">
    <label style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;display:block">Killzones</label>
    <div>
      <span class="killzone-chip active" data-kz="london" onclick="toggleKZ(this)">London 03-04 NY</span>
      <span class="killzone-chip active" data-kz="ny_am" onclick="toggleKZ(this)">NY AM 10-11</span>
      <span class="killzone-chip active" data-kz="ny_pm" onclick="toggleKZ(this)">NY PM 14-15</span>
    </div>
  </div>
  <div class="form-group"><label>Displacement ATR multiplier</label><input type="number" id="sbDispAtr" value="1.5" step="0.1" min="1"></div>
</div>

<div class="card">
  <div class="card-title">CHoCH Config</div>
  <div class="form-group"><label>Swing length (pivot bars)</label><input type="number" id="chSwingLen" value="5" min="3" max="20"></div>
  <div style="font-size:10px;color:var(--text3);line-height:1.5;margin-top:8px;padding:8px;background:rgba(255,255,255,.02);border-radius:6px">
    Hogere swing length = minder signalen, major structure focus. Lager = meer signalen, meer noise.
  </div>
</div>

<div class="card">
  <div class="card-title">BOS Config</div>
  <div class="form-group"><label>Swing length (pivot bars)</label><input type="number" id="bosSwingLen" value="5" min="3" max="20"></div>
  <div style="font-size:10px;color:var(--text3);line-height:1.5;margin-top:8px;padding:8px;background:rgba(255,255,255,.02);border-radius:6px">
    Trend continuation — werkt in trending markten. In choppy markten geeft veel false breakouts.
  </div>
</div>

<div class="card">
  <div class="card-title">Asian Breakout Config</div>
  <div class="form-row">
    <div class="form-group"><label>Min range (pips)</label><input type="number" id="abMinRange" placeholder="auto" min="0" step="5"></div>
    <div class="form-group"><label>Max range (pips)</label><input type="number" id="abMaxRange" placeholder="auto" min="0" step="10"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>TP = range × mult</label><input type="number" id="abTpMult" value="1.5" min="0.5" step="0.1"></div>
    <div class="form-group"><label>Use RR instead</label>
      <select id="abUseRR"><option value="false" selected>Range projection</option><option value="true">RR-based</option></select>
    </div>
  </div>
  <div style="font-size:10px;color:var(--text3);line-height:1.5;margin-top:8px;padding:8px;background:rgba(255,255,255,.02);border-radius:6px">
    Asian session: 23:00-07:00 Brussels. Breakout window: 07:00-12:00 Brussels.<br>
    Default range filters per pair: EURUSD 20-80 pips, XAUUSD 100-400 pips. Laat leeg voor auto.
  </div>
</div>

</div>
</div>
</div>

<!-- BACKTEST TAB -->
<div id="tab-backtest" style="display:none">
<div class="card">
  <div class="card-title">Backtester</div>
    <div style="margin-bottom:14px">
      <label style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;display:block">Strategies to test (1 of meer)</label>
      <div id="btStratPicker" style="display:flex;flex-wrap:wrap;gap:6px">
        <span class="strat-chip active" data-sid="SB" onclick="toggleStratChip(this)">Silver Bullet</span>
        <span class="strat-chip" data-sid="CH" onclick="toggleStratChip(this)">CHoCH</span>
        <span class="strat-chip" data-sid="BOS" onclick="toggleStratChip(this)">BOS</span>
        <span class="strat-chip" data-sid="AB" onclick="toggleStratChip(this)">Asian Breakout</span>
        <span style="margin-left:8px;display:flex;gap:4px">
          <button class="btn" style="padding:4px 10px;font-size:10px" onclick="selectAllStrats()">All</button>
          <button class="btn" style="padding:4px 10px;font-size:10px" onclick="selectNoStrats()">None</button>
        </span>
      </div>
      <div style="font-size:10px;color:var(--text3);margin-top:6px">
        Eén = single-strategy backtest. Meerdere = parallel runs (elke strategie apart, gecombineerde view).
      </div>
    </div>
  <div class="form-row">
    <div class="form-group"><label>Pair</label><select id="btPair"><option>EURUSD</option><option>XAUUSD</option></select></div>
    <div class="form-group"><label>Timeframe</label><select id="btTF"><option value="15M" selected>15M</option><option value="1H">1H</option><option value="4H">4H</option><option value="15M+1H">15M + 1H (combined)</option></select></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Lot (micro)</label><input type="number" id="btLot" value="10" min="1"></div>
    <div class="form-group"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Start date</label><input type="date" id="btStart"></div>
    <div class="form-group"><label>End date</label><input type="date" id="btEnd"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>RR</label><input type="number" id="btRR" value="2" step="0.5"></div>
    <div class="form-group"><label>Displacement ATR (SB only)</label><input type="number" id="btDispAtr" value="1.5" step="0.1"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Swing length (CHoCH/BOS)</label><input type="number" id="btSwingLen" value="5" min="3" max="20"></div>
    <div class="form-group"></div>
  </div>
  <button class="btn primary" onclick="runBacktest()" id="btRunBtn">▶ RUN BACKTEST</button>
  <div id="btResult" style="margin-top:20px"></div>
</div>
</div>

<!-- LOGS TAB -->
<div id="tab-logs" style="display:none">
<div class="card">
  <div class="card-title">System Logs <button class="btn" onclick="refreshLogs()" style="font-size:10px;padding:4px 10px">↻ Refresh</button></div>
  <div class="log-area" id="logArea"></div>
</div>
</div>

</div>

<script>
const STRATEGIES = [
  {id:"SB",  name:"Silver Bullet",     sub:"Killzone FVG reversal", placeholder:false},
  {id:"CH",  name:"CHoCH Reversal",    sub:"Structure trend reversal", placeholder:false},
  {id:"BOS", name:"BOS Continuation",  sub:"Trend continuation", placeholder:false},
  {id:"AB",  name:"Asian Breakout",    sub:"Session range breakout", placeholder:false},
];

let S = { status: null, currentTab: "live", config: null };

async function refreshStatus(){
  try{
    const r = await fetch("/api/status");
    const d = await r.json();
    S.status = d;
    if(!S.config){ S.config = d.config; renderConfig(); }
    renderTopbar();
    renderStrategies();
    renderOpenTrades();
    renderClosed();
  }catch(e){ console.error("status fetch:", e); }
}

function renderTopbar(){
  const d = S.status;
  const dot = document.getElementById("engineDot");
  const lbl = document.getElementById("engineLabel");
  if(d.running && d.paused){ dot.className = "dot paused"; lbl.textContent = "PAUSED"; }
  else if(d.running){ dot.className = "dot live"; lbl.textContent = "RUNNING"; }
  else { dot.className = "dot"; lbl.textContent = "OFFLINE"; }
  document.getElementById("scanCounter").textContent = `Scans: ${d.scan_count} | Last: ${d.last_scan || "-"}`;
  document.getElementById("lastScan").textContent = d.last_scan ? `Last scan: ${d.last_scan}` : "";
}

function renderStrategies(){
  const grid = document.getElementById("stratGrid");
  const stats = (S.status && S.status.stats) || {};
  const cfg = (S.status && S.status.config && S.status.config.strategies) || {};
  grid.innerHTML = STRATEGIES.map(s => {
    const st = stats[s.id] || {trades:0,wr:0,pnl:0,open:0};
    const enabled = cfg[s.id] && cfg[s.id].enabled;
    return `
      <div class="strat-card ${enabled?'enabled':''} ${s.placeholder?'placeholder':''}">
        <div class="strat-head">
          <div>
            <div class="strat-name">${s.name}</div>
            <div class="strat-id">${s.id} · ${s.sub}</div>
          </div>
          ${s.placeholder ? '<span class="strat-id">SOON</span>' : `<label class="switch"><input type="checkbox" ${enabled?'checked':''} onchange="toggleStrategy('${s.id}',this.checked)"><span class="slider"></span></label>`}
        </div>
        <div class="strat-stats">
          <div><b>${st.trades}</b>trades</div>
          <div><b>${st.wr}%</b>wr</div>
          <div><b>€${st.pnl.toFixed(2)}</b>pnl</div>
        </div>
      </div>
    `;
  }).join("");
}

function renderOpenTrades(){
  const trades = (S.status && S.status.open_trades) || [];
  document.getElementById("openCount").textContent = `${trades.length} open`;
  if(!trades.length){
    document.getElementById("openPos").innerHTML = '<div class="placeholder-msg">No open positions</div>';
    return;
  }
  window._liveOpenTrades = trades;
  document.getElementById("openPos").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>Strat</th><th>TF</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Live</th><th>SL</th><th>TP</th><th>P&L</th><th></th></tr></thead>
      <tbody>${trades.map((t, idx) => `
        <tr class="live-tr" data-tidx="${idx}" style="cursor:pointer" onclick="toggleLiveTradeDetail('open', ${idx}, this, event)">
          <td>${t.id}</td>
          <td>${t.strategy}</td>
          <td>${t.tf||"-"}</td>
          <td>${t.pair}</td>
          <td class="dir-${t.direction}">${t.direction}</td>
          <td>${t.entry.toFixed(5)}</td>
          <td>${(t.live_price||0).toFixed(5)}</td>
          <td>${t.sl.toFixed(5)}</td>
          <td>${t.tp.toFixed(5)}</td>
          <td class="${(t.pnl_eur||0)>=0?'pnl-pos':'pnl-neg'}">€${(t.pnl_eur||0).toFixed(2)}</td>
          <td><button class="btn danger" style="padding:3px 8px;font-size:10px" onclick="event.stopPropagation();closeTrade('${t.id}')">✕</button></td>
        </tr>
      `).join("")}</tbody>
    </table>
    <div id="liveOpenDetail" style="margin-top:12px;display:none"></div>
  `;
}

function renderClosed(){
  const trades = ((S.status && S.status.closed_trades) || []).slice(-20).reverse();
  if(!trades.length){
    document.getElementById("closedTrades").innerHTML = '<div class="placeholder-msg">No closed trades yet</div>';
    return;
  }
  window._liveClosedTrades = trades;
  document.getElementById("closedTrades").innerHTML = `
    <table>
      <thead><tr><th>Time</th><th>ID</th><th>Strat</th><th>TF</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Hit</th><th>Pips</th><th>P&L</th></tr></thead>
      <tbody>${trades.map((t, idx) => `
        <tr class="live-tr-closed" data-tidx="${idx}" style="cursor:pointer" onclick="toggleLiveTradeDetail('closed', ${idx}, this, event)">
          <td>${(t.closed_at||"").slice(11,19)}</td>
          <td>${t.id}</td>
          <td>${t.strategy}</td>
          <td>${t.tf||"-"}</td>
          <td>${t.pair}</td>
          <td class="dir-${t.direction}">${t.direction}</td>
          <td>${t.entry.toFixed(5)}</td>
          <td>${(t.exit||0).toFixed(5)}</td>
          <td>${t.hit}</td>
          <td>${(t.pips||0).toFixed(1)}</td>
          <td class="${(t.pnl_eur||0)>=0?'pnl-pos':'pnl-neg'}">€${(t.pnl_eur||0).toFixed(2)}</td>
        </tr>
      `).join("")}</tbody>
    </table>
    <div id="liveClosedDetail" style="margin-top:12px;display:none"></div>
  `;
}

// Desktop: live trade detail expand (open of closed)
async function toggleLiveTradeDetail(kind, tradeIdx, rowEl, ev){
  if(ev) ev.stopPropagation();
  const list = kind === "open" ? (window._liveOpenTrades || []) : (window._liveClosedTrades || []);
  const trade = list[tradeIdx];
  if(!trade) return;
  const panelId = kind === "open" ? "liveOpenDetail" : "liveClosedDetail";
  const panel = document.getElementById(panelId);
  if(!panel) return;
  // Toggle close
  if(panel.dataset.tidx === String(tradeIdx) && panel.style.display !== "none"){
    panel.style.display = "none";
    panel.dataset.tidx = "";
    document.querySelectorAll(`.${kind==="open"?"live-tr":"live-tr-closed"}`).forEach(tr => tr.style.background = "");
    return;
  }
  panel.dataset.tidx = String(tradeIdx);
  document.querySelectorAll(`.${kind==="open"?"live-tr":"live-tr-closed"}`).forEach(tr => tr.style.background = "");
  if(rowEl) rowEl.style.background = "rgba(255,255,255,.05)";

  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  const tf = trade.tf || "15M";
  // Duration
  let durationStr = "-";
  const startTs = trade.opened_at || trade.ts;
  const endTs = trade.closed_at;
  if(startTs && endTs){
    try{
      const t1 = new Date(startTs.replace(" ", "T"));
      const t2 = new Date(endTs.replace(" ", "T"));
      const diff = Math.abs(t2-t1);
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = hours > 0 ? `${hours}u ${mins}m` : `${mins}m`;
    }catch(e){}
  } else if(startTs && kind === "open"){
    try{
      const t1 = new Date(startTs.replace(" ", "T"));
      const diff = Math.abs(Date.now() - t1.getTime());
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = (hours > 0 ? `${hours}u ${mins}m` : `${mins}m`) + " (open)";
    }catch(e){}
  }
  const rr = Math.abs(trade.tp - trade.entry) / Math.max(0.000001, Math.abs(trade.entry - trade.sl));
  const meta = trade.meta || {};
  const exitVal = trade.exit !== undefined ? trade.exit : (trade.live_price || 0);
  const pnl = trade.pnl_eur !== undefined ? trade.pnl_eur : 0;

  panel.style.display = "block";
  panel.innerHTML = `
    <div style="padding:14px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="font-size:12px;color:var(--glow2);font-weight:700;letter-spacing:1px;text-transform:uppercase">
          ${kind==="open"?"OPEN":"CLOSED"} · ${trade.id} · ${trade.pair} ${trade.direction} · ${trade.strategy} ${trade.tf?'· '+trade.tf:''}
        </div>
        <button class="btn" style="padding:4px 10px;font-size:10px" onclick="toggleLiveTradeDetail('${kind}', ${tradeIdx})">✕ Close</button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;font-size:11px;margin-bottom:12px">
        <div><span style="color:var(--text3)">Opened</span><br><b>${(startTs||"").slice(0,16)}</b></div>
        ${kind==="closed" ? `<div><span style="color:var(--text3)">Closed</span><br><b>${(endTs||"").slice(0,16)}</b></div>` : `<div><span style="color:var(--text3)">Live price</span><br><b>${(trade.live_price||0).toFixed(dec)}</b></div>`}
        <div><span style="color:var(--text3)">Entry</span><br><b>${trade.entry.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">${kind==="closed"?"Exit":"Current"}</span><br><b>${exitVal.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">SL</span><br><b style="color:var(--red)">${trade.sl.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">TP</span><br><b style="color:var(--green)">${trade.tp.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">RR</span><br><b>${rr.toFixed(2)}</b></div>
        <div><span style="color:var(--text3)">Lot</span><br><b>${trade.lotsize||"-"}</b></div>
        <div><span style="color:var(--text3)">Duration</span><br><b>${durationStr}</b></div>
        ${kind==="closed" ? `<div><span style="color:var(--text3)">Hit</span><br><b>${trade.hit}</b></div><div><span style="color:var(--text3)">Pips</span><br><b>${trade.pips}</b></div>` : ""}
        <div><span style="color:var(--text3)">P&L</span><br><b class="${pnl>=0?'pnl-pos':'pnl-neg'}">€${pnl.toFixed(2)}</b></div>
        ${meta.killzone ? `<div><span style="color:var(--text3)">Killzone</span><br><b>${meta.killzone}</b></div>` : ""}
        ${meta.prior_trend ? `<div><span style="color:var(--text3)">Prior</span><br><b>${meta.prior_trend}</b></div>` : ""}
        ${meta.trend ? `<div><span style="color:var(--text3)">Trend</span><br><b>${meta.trend}</b></div>` : ""}
        ${meta.broken_dir ? `<div><span style="color:var(--text3)">Break</span><br><b>${meta.broken_dir}</b></div>` : ""}
        ${meta.range_pips ? `<div><span style="color:var(--text3)">Asian range</span><br><b>${meta.range_pips}p</b></div>` : ""}
      </div>
      <canvas class="live-mini-chart" style="width:100%;height:280px;background:rgba(0,0,0,.4);border-radius:6px;display:block"></canvas>
      <div style="font-size:10px;color:var(--text3);text-align:center;margin-top:6px">Entry-bar gemarkeerd · SL (red) / TP (green) / Entry (white dashed) levels</div>
    </div>
  `;

  const canvas = panel.querySelector(".live-mini-chart");
  const targetTs = startTs || trade.ts;
  try{
    const r = await fetch(`/api/candles_around?pair=${trade.pair}&tf=${tf}&ts=${encodeURIComponent(targetTs)}&bars_before=40&bars_after=30`);
    const d = await r.json();
    if(d.ok && d.candles && d.candles.length > 0){
      renderDtMiniChart(canvas, d.candles, {
        entry: trade.entry, sl: trade.sl, tp: trade.tp,
        direction: trade.direction, pair: trade.pair
      }, d.target_idx);
    } else {
      canvas.style.display = "none";
      const msg = document.createElement("div");
      msg.style.cssText = "padding:12px;text-align:center;color:var(--text3);font-size:11px;font-style:italic";
      msg.textContent = `Chart unavailable: ${d.error || "no data"}`;
      canvas.parentNode.insertBefore(msg, canvas);
    }
  }catch(e){
    canvas.style.display = "none";
  }
  panel.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function renderConfig(){
  if(!S.config) return;
  document.getElementById("cfgPair").value = S.config.pair;
  // Multi-TF chips
  const activeTFs = S.config.tfs || (S.config.tf ? [S.config.tf] : ["15M"]);
  document.querySelectorAll("#cfgTFPicker .tf-chip").forEach(c => {
    c.classList.toggle("active", activeTFs.includes(c.dataset.tf));
  });
  document.getElementById("cfgLotEur").value = S.config.lot_eur;
  document.getElementById("cfgLotXau").value = S.config.lot_xau;
  document.getElementById("cfgRR").value = S.config.rr;
  document.getElementById("cfgCap").value = S.config.capital;
  document.getElementById("cfgDiscord").value = S.config.discord_webhook;
  // Silver Bullet
  const sb = S.config.strategies.SB || {};
  document.getElementById("sbDispAtr").value = sb.disp_atr || 1.5;
  document.querySelectorAll(".killzone-chip").forEach(c => {
    if((sb.killzones||[]).includes(c.dataset.kz)) c.classList.add("active");
    else c.classList.remove("active");
  });
  // CHoCH
  const ch = S.config.strategies.CH || {};
  const chEl = document.getElementById("chSwingLen");
  if(chEl) chEl.value = ch.swing_length || 5;
  // BOS
  const bos = S.config.strategies.BOS || {};
  const bosEl = document.getElementById("bosSwingLen");
  if(bosEl) bosEl.value = bos.swing_length || 5;
  // AB
  const ab = S.config.strategies.AB || {};
  const abMin = document.getElementById("abMinRange");
  const abMax = document.getElementById("abMaxRange");
  const abTp = document.getElementById("abTpMult");
  const abRR = document.getElementById("abUseRR");
  if(abMin) abMin.value = ab.min_range_pips || "";
  if(abMax) abMax.value = ab.max_range_pips || "";
  if(abTp) abTp.value = ab.tp_range_mult || 1.5;
  if(abRR) abRR.value = ab.use_rr ? "true" : "false";
}

function toggleKZ(el){ el.classList.toggle("active"); }

async function toggleStrategy(sid, enabled){
  const strategies = {};
  strategies[sid] = {enabled};
  await fetch("/api/engine/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({strategies})});
  refreshStatus();
}

function getBodyConfig(){
  const killzones = [...document.querySelectorAll(".killzone-chip.active")].map(c => c.dataset.kz);
  return {
    pair: document.getElementById("cfgPair").value,
    tfs: [...document.querySelectorAll("#cfgTFPicker .tf-chip.active")].map(c => c.dataset.tf),
    lot_eur: parseInt(document.getElementById("cfgLotEur").value)||10,
    lot_xau: parseInt(document.getElementById("cfgLotXau").value)||1,
    rr: parseFloat(document.getElementById("cfgRR").value)||2.0,
    capital: parseFloat(document.getElementById("cfgCap").value)||10000,
    discord_webhook: document.getElementById("cfgDiscord").value.trim(),
    strategies: {
      SB: {killzones, disp_atr: parseFloat(document.getElementById("sbDispAtr").value)||1.5},
      CH: {swing_length: parseInt(document.getElementById("chSwingLen").value)||5},
      BOS: {swing_length: parseInt(document.getElementById("bosSwingLen").value)||5},
      AB: {
        min_range_pips: parseInt(document.getElementById("abMinRange").value) || null,
        max_range_pips: parseInt(document.getElementById("abMaxRange").value) || null,
        tp_range_mult: parseFloat(document.getElementById("abTpMult").value)||1.5,
        use_rr: document.getElementById("abUseRR").value === "true",
      },
    },
  };
}

// ── Preset management ──
async function loadPresets(){
  try{
    const r = await fetch("/api/presets");
    const d = await r.json();
    const list = document.getElementById("presetList");
    if(!list) return;
    const presets = d.presets || {};
    const names = Object.keys(presets);
    if(names.length === 0){
      list.innerHTML = '<div style="color:var(--text3);font-size:11px;text-align:center;padding:10px;font-style:italic">Geen presets — sla huidige config op met een naam</div>';
      return;
    }
    list.innerHTML = names.map(name => {
      const p = presets[name];
      const s = p.summary || {};
      const tfsStr = (s.tfs||[]).join("+") || "?";
      const stratsStr = (s.strats||[]).join(",") || "none";
      return `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;margin-bottom:6px">
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;color:var(--text);font-size:12px">${name}</div>
            <div style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace">${s.pair||"?"} · ${tfsStr} · ${stratsStr} · RR ${s.rr||"?"}</div>
            <div style="font-size:9px;color:var(--text3);margin-top:2px">${p.saved_at||""}</div>
          </div>
          <div style="display:flex;gap:4px">
            <button class="btn" style="padding:4px 10px;font-size:10px" onclick="applyPreset('${name}')">↓ Apply</button>
            <button class="btn danger" style="padding:4px 8px;font-size:10px" onclick="deletePreset('${name}')">✕</button>
          </div>
        </div>
      `;
    }).join("");
  }catch(e){ console.error("loadPresets:", e); }
}

async function savePreset(){
  const nameInput = document.getElementById("presetName");
  const name = (nameInput.value || "").trim();
  if(!name){ alert("Geef een preset naam op"); return; }
  if(!/^[A-Za-z0-9_\-\.\s]+$/.test(name)){ alert("Alleen letters, cijfers, spatie, _ - ."); return; }
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({config: getBodyConfig()})
    });
    const d = await r.json();
    if(d.ok){
      nameInput.value = "";
      loadPresets();
    } else {
      alert("Save failed: " + (d.error || "unknown"));
    }
  }catch(e){ alert("Error: " + e.message); }
}

async function applyPreset(name){
  if(!confirm(`Preset "${name}" toepassen? Huidige config wordt overschreven.`)) return;
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}/apply`, {method: "POST"});
    const d = await r.json();
    if(d.ok){
      S.config = d.config;
      renderConfig();
    } else {
      alert("Apply failed: " + (d.error || "unknown"));
    }
  }catch(e){ alert("Error: " + e.message); }
}

async function deletePreset(name){
  if(!confirm(`Preset "${name}" verwijderen?`)) return;
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}`, {method: "DELETE"});
    const d = await r.json();
    if(d.ok) loadPresets();
  }catch(e){}
}

async function engineStart(){
  const body = getBodyConfig();
  const r = await fetch("/api/engine/start", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  await r.json();
  refreshStatus();
}

async function engineStop(){
  if(!confirm("Engine stoppen?")) return;
  await fetch("/api/engine/stop", {method:"POST"});
  refreshStatus();
}

let _isPaused = false;
async function engineTogglePause(){
  const url = _isPaused ? "/api/engine/resume" : "/api/engine/pause";
  await fetch(url, {method:"POST"});
  _isPaused = !_isPaused;
  document.getElementById("pauseBtn").textContent = _isPaused ? "▶ RESUME" : "⏸ PAUSE";
  refreshStatus();
}

async function closeTrade(tid){
  if(!confirm("Trade sluiten?")) return;
  await fetch(`/api/trade/${tid}/close`, {method:"POST"});
  refreshStatus();
}

function switchTab(tab){
  S.currentTab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  document.getElementById("tab-live").style.display = tab==="live" ? "block" : "none";
  document.getElementById("tab-backtest").style.display = tab==="backtest" ? "block" : "none";
  document.getElementById("tab-logs").style.display = tab==="logs" ? "block" : "none";
  if(tab==="logs") refreshLogs();
}

function toggleStratChip(el){ el.classList.toggle("active"); }
function toggleTFChip(el){
  el.classList.toggle("active");
  // Auto-save engine config so changes persist even when not started
  try{ fetch("/api/engine/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({tfs: [...document.querySelectorAll("#cfgTFPicker .tf-chip.active")].map(c => c.dataset.tf)})}); }catch(e){}
}
function selectAllStrats(){ document.querySelectorAll("#btStratPicker .strat-chip").forEach(c => c.classList.add("active")); }
function selectNoStrats(){ document.querySelectorAll("#btStratPicker .strat-chip").forEach(c => c.classList.remove("active")); }
function getSelectedStrats(){
  return [...document.querySelectorAll("#btStratPicker .strat-chip.active")].map(c => c.dataset.sid);
}

async function runBacktest(){
  const btn = document.getElementById("btRunBtn");
  const selectedStrats = getSelectedStrats();
  if(selectedStrats.length === 0){
    document.getElementById("btResult").innerHTML = `<div style="color:var(--amber);padding:12px;background:rgba(251,191,36,.05);border-radius:6px">Selecteer minimaal één strategie.</div>`;
    return;
  }

  btn.disabled = true; btn.textContent = "Running...";
  const killzones = [...document.querySelectorAll(".killzone-chip.active")].map(c => c.dataset.kz);

  // Determine strategy mode: single → strategy=<SID>, multiple → strategy=MIX
  const isSingle = selectedStrats.length === 1;
  const strategyParam = isSingle ? selectedStrats[0] : "MIX";

  const baseBody = {
    strategy: strategyParam,
    strategies: selectedStrats,  // alleen gebruikt bij MIX
    pair: document.getElementById("btPair").value,
    start: document.getElementById("btStart").value,
    end: document.getElementById("btEnd").value,
    lot: parseInt(document.getElementById("btLot").value)||10,
    rr: parseFloat(document.getElementById("btRR").value)||2,
    disp_atr: parseFloat(document.getElementById("btDispAtr").value)||1.5,
    swing_length: parseInt(document.getElementById("btSwingLen").value)||5,
    killzones,
    min_range_pips: parseInt(document.getElementById("abMinRange").value) || null,
    max_range_pips: parseInt(document.getElementById("abMaxRange").value) || null,
    tp_range_mult: parseFloat(document.getElementById("abTpMult").value)||1.5,
    use_rr: document.getElementById("abUseRR").value === "true",
  };
  const tfChoice = document.getElementById("btTF").value;
  const isCombined = tfChoice === "15M+1H";

  try{
    if(!isCombined){
      const body = {...baseBody, tf: tfChoice};
      const r = await fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
      const d = await r.json();
      d._strategies_run = selectedStrats;
      renderBacktestResult(d);
    } else {
      btn.textContent = "Running 15M+1H...";
      const [r15, r1h] = await Promise.all([
        fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({...baseBody, tf:"15M"})}).then(r=>r.json()),
        fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({...baseBody, tf:"1H"})}).then(r=>r.json()),
      ]);
      const trades15 = (r15.trades || []).map(t => ({...t, tf:"15M"}));
      const trades1h = (r1h.trades || []).map(t => ({...t, tf:"1H"}));
      const allTrades = [...trades15, ...trades1h].sort((a,b)=>(a.ts||"").localeCompare(b.ts||""));
      const wins = allTrades.filter(t => t.pnl > 0);
      const losses = allTrades.filter(t => t.pnl <= 0);
      const totalPnl = allTrades.reduce((s,t)=>s+t.pnl, 0);
      const sumW = wins.reduce((s,t)=>s+t.pnl,0), sumL = Math.abs(losses.reduce((s,t)=>s+t.pnl,0));
      let _equity = 10000, _peak = 10000, _maxDD = 0;
      allTrades.forEach(t => {
        _equity += t.pnl;
        if(_equity > _peak) _peak = _equity;
        const dd = _equity - _peak;
        if(dd < _maxDD) _maxDD = dd;
      });
      const combinedStats = {
        total: allTrades.length,
        wins: wins.length, losses: losses.length,
        wr: allTrades.length ? Math.round(1000*wins.length/allTrades.length)/10 : 0,
        pnl: Math.round(totalPnl*100)/100,
        pf: losses.length && sumL>0 ? Math.round(100*sumW/sumL)/100 : (wins.length>0 ? 999 : 0),
        max_dd: Math.round(_maxDD*100)/100,
      };
      // Merge per-strategy summaries if present
      const mergedPerStrat = {};
      ["15M","1H"].forEach(tf => {
        const src = tf==="15M"?r15:r1h;
        if(src && src.per_strategy){
          Object.entries(src.per_strategy).forEach(([sid, st]) => {
            if(!mergedPerStrat[sid]) mergedPerStrat[sid] = {total:0,wins:0,wr:0,pnl:0};
            mergedPerStrat[sid].total += (st.total||0);
            mergedPerStrat[sid].wins += (st.wins||0);
            mergedPerStrat[sid].pnl  = Math.round(100*(mergedPerStrat[sid].pnl + (st.pnl||0)))/100;
          });
        }
      });
      Object.values(mergedPerStrat).forEach(st => {
        st.wr = st.total ? Math.round(1000*st.wins/st.total)/10 : 0;
      });

      const combined = {
        mode: "combined-tf",
        trades: allTrades,
        stats: combinedStats,
        per_tf: {"15M": r15.stats, "1H": r1h.stats},
        per_strategy: Object.keys(mergedPerStrat).length ? mergedPerStrat : undefined,
        diagnostics: {tf_15M: r15.diagnostics || r15.diagnostics_per_strategy, tf_1H: r1h.diagnostics || r1h.diagnostics_per_strategy},
        _strategies_run: selectedStrats,
      };
      renderBacktestResult(combined);
    }
  }catch(e){
    document.getElementById("btResult").innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = "▶ RUN BACKTEST";
  }
}

function renderBacktestResult(d){
  const el = document.getElementById("btResult");
  if(d.error){ el.innerHTML = `<div style="color:var(--red);padding:12px;background:rgba(248,113,113,.05);border-radius:6px">${d.error}</div>`; return; }

  const st = d.stats || {total:0};
  const trades = d.trades || [];

  // Top stats grid
  let html = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px">
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">TRADES</div><div style="font-size:18px;font-weight:700">${st.total||0}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">WIN RATE</div><div style="font-size:18px;font-weight:700">${st.wr||0}%</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">P&L</div><div style="font-size:18px;font-weight:700" class="${(st.pnl||0)>=0?'pnl-pos':'pnl-neg'}">€${st.pnl||0}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">PF</div><div style="font-size:18px;font-weight:700">${st.pf||0}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">MAX DD</div><div style="font-size:18px;font-weight:700" class="pnl-neg">€${st.max_dd||0}</div></div>
    </div>
  `;

  // Per-strategy breakdown (ALL mode)
  if(d.per_strategy){
    html += `<div class="card-title" style="margin-top:8px">Per Strategy</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px">
        ${Object.entries(d.per_strategy).map(([sid, s]) => `
          <div style="padding:10px;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:6px">
            <div style="font-size:11px;font-weight:700;color:var(--glow2);letter-spacing:1px;margin-bottom:6px">${sid}</div>
            <div style="font-size:10px;color:var(--text3)">Trades: <b style="color:var(--text);font-size:13px">${s.total||0}</b></div>
            <div style="font-size:10px;color:var(--text3)">WR: <b style="color:var(--text);font-size:13px">${s.wr||0}%</b></div>
            <div style="font-size:10px;color:var(--text3)">P&L: <b class="${(s.pnl||0)>=0?'pnl-pos':'pnl-neg'};font-size:13px">€${s.pnl||0}</b></div>
          </div>
        `).join("")}
      </div>`;
  }

  // Per-TF breakdown (15M+1H combined)
  if(d.per_tf){
    html += `<div class="card-title" style="margin-top:8px">Per Timeframe</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">
        ${Object.entries(d.per_tf).map(([tf, s]) => `
          <div style="padding:10px;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:6px">
            <div style="font-size:11px;font-weight:700;color:var(--glow2);letter-spacing:1px;margin-bottom:6px">${tf}</div>
            <div style="font-size:10px;color:var(--text3)">Trades: <b style="color:var(--text);font-size:13px">${(s&&s.total)||0}</b></div>
            <div style="font-size:10px;color:var(--text3)">WR: <b style="color:var(--text);font-size:13px">${(s&&s.wr)||0}%</b></div>
            <div style="font-size:10px;color:var(--text3)">P&L: <b class="${((s&&s.pnl)||0)>=0?'pnl-pos':'pnl-neg'};font-size:13px">€${(s&&s.pnl)||0}</b></div>
          </div>
        `).join("")}
      </div>`;
  }

  // Diagnostics — show which filter rejected setups
  const diags = d.diagnostics || d.diagnostics_per_strategy || null;
  if(diags){
    html += `<div class="card-title" style="margin-top:8px;cursor:pointer" onclick="document.getElementById('diagBody').style.display = document.getElementById('diagBody').style.display==='none' ? 'block' : 'none'">Diagnostics ▼ (klik om in/uit te klappen)</div>
      <div id="diagBody" style="display:block;margin-bottom:16px;font-family:'JetBrains Mono',monospace;font-size:11px">`;
    // Per-strategy diagnostics
    if(d.diagnostics_per_strategy){
      Object.entries(d.diagnostics_per_strategy).forEach(([sid, dg]) => {
        html += `<div style="padding:8px;background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;margin-bottom:6px"><b style="color:var(--glow2)">${sid}:</b> ${Object.entries(dg||{}).map(([k,v]) => `<span style="color:var(--text3);margin-right:10px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
      });
    } else if(d.diagnostics){
      // Either single strategy diag, or combined-tf with tf_15M/tf_1H sub-keys
      const dg = d.diagnostics;
      if(dg.tf_15M || dg.tf_1H){
        Object.entries(dg).forEach(([tf, sub]) => {
          html += `<div style="padding:8px;background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;margin-bottom:6px"><b style="color:var(--glow2)">${tf}:</b> ${Object.entries(sub||{}).map(([k,v]) => `<span style="color:var(--text3);margin-right:10px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
        });
      } else {
        html += `<div style="padding:8px;background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px">${Object.entries(dg).map(([k,v]) => `<span style="color:var(--text3);margin-right:10px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
      }
    }
    html += `</div>`;
  }

  if(d.msg && trades.length===0){
    html += `<div style="color:var(--text3);padding:12px;font-style:italic">${d.msg}</div>`;
  }

  // Trades table
  if(trades.length){
    // Store globally voor desktop trade detail
    window._dtBacktestTrades = trades;
    html += `
      <div class="card-title" style="margin-top:8px">Trades (last 30) <span style="font-size:10px;color:var(--text3);font-weight:400">klik op rij voor detail</span></div>
      <table>
        <thead><tr><th>Time</th><th>Strat</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Hit</th><th>Pips</th><th>P&L</th><th>Extra</th></tr></thead>
        <tbody>${trades.slice(-30).reverse().map((t, idx) => `
          <tr class="trade-tr" data-tidx="${trades.length-1-idx}" style="cursor:pointer" onclick="toggleDtTradeDetail(${trades.length-1-idx}, this)">
            <td>${(t.ts||"").slice(0,16)}</td>
            <td>${t.strategy||"-"}</td>
            <td>${t.pair}</td>
            <td class="dir-${t.direction}">${t.direction}</td>
            <td>${t.entry}</td><td>${t.exit}</td><td>${t.hit}</td>
            <td>${t.pips}</td>
            <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">€${t.pnl}</td>
            <td style="font-size:10px;color:var(--text3)">${t.killzone||t.prior_trend||t.trend||t.broken_dir||t.tf||"-"}</td>
          </tr>
        `).join("")}</tbody>
      </table>
      <div id="dtTradeDetail" style="margin-top:12px;display:none"></div>
    `;
  }

  el.innerHTML = html;
}

// Desktop: trade detail panel onder de tabel
async function toggleDtTradeDetail(tradeIdx, rowEl){
  const trade = (window._dtBacktestTrades || [])[tradeIdx];
  if(!trade) return;
  const panel = document.getElementById("dtTradeDetail");
  if(!panel) return;
  // Toggle: close if same trade is already open
  if(panel.dataset.tidx === String(tradeIdx) && panel.style.display !== "none"){
    panel.style.display = "none";
    panel.dataset.tidx = "";
    document.querySelectorAll(".trade-tr").forEach(tr => tr.style.background = "");
    return;
  }
  panel.dataset.tidx = String(tradeIdx);
  // Highlight row
  document.querySelectorAll(".trade-tr").forEach(tr => tr.style.background = "");
  if(rowEl) rowEl.style.background = "rgba(255,255,255,.05)";

  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  const tf = trade.tf || document.getElementById("btTF").value || "15M";
  let durationStr = "-";
  if(trade.exit_ts && trade.ts){
    try{
      const t1 = new Date(trade.ts.replace(" ", "T"));
      const t2 = new Date(trade.exit_ts.replace(" ", "T"));
      const diff = Math.abs(t2-t1);
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = hours > 0 ? `${hours}u ${mins}m` : `${mins}m`;
    }catch(e){}
  }
  const rr = Math.abs(trade.tp - trade.entry) / Math.max(0.000001, Math.abs(trade.entry - trade.sl));

  panel.style.display = "block";
  panel.innerHTML = `
    <div style="padding:14px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="font-size:12px;color:var(--glow2);font-weight:700;letter-spacing:1px;text-transform:uppercase">Trade Detail · ${trade.pair} ${trade.direction} ${trade.strategy?'· '+trade.strategy:''}</div>
        <button class="btn" style="padding:4px 10px;font-size:10px" onclick="toggleDtTradeDetail(${tradeIdx})">✕ Close</button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;font-size:11px;margin-bottom:12px">
        <div><span style="color:var(--text3)">Time</span><br><b>${(trade.ts||"").slice(0,16)}</b></div>
        <div><span style="color:var(--text3)">Entry</span><br><b>${trade.entry.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">Exit</span><br><b>${trade.exit.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">SL</span><br><b style="color:var(--red)">${trade.sl.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">TP</span><br><b style="color:var(--green)">${trade.tp.toFixed(dec)}</b></div>
        <div><span style="color:var(--text3)">RR</span><br><b>${rr.toFixed(2)}</b></div>
        <div><span style="color:var(--text3)">Duration</span><br><b>${durationStr}</b></div>
        <div><span style="color:var(--text3)">Hit</span><br><b>${trade.hit}</b></div>
        <div><span style="color:var(--text3)">Pips</span><br><b>${trade.pips}</b></div>
        <div><span style="color:var(--text3)">P&L</span><br><b class="${trade.pnl>=0?'pnl-pos':'pnl-neg'}">€${trade.pnl}</b></div>
        ${trade.killzone ? `<div><span style="color:var(--text3)">Killzone</span><br><b>${trade.killzone}</b></div>` : ""}
        ${trade.prior_trend ? `<div><span style="color:var(--text3)">Prior Trend</span><br><b>${trade.prior_trend}</b></div>` : ""}
        ${trade.trend ? `<div><span style="color:var(--text3)">Trend</span><br><b>${trade.trend}</b></div>` : ""}
        ${trade.broken_dir ? `<div><span style="color:var(--text3)">Break Dir</span><br><b>${trade.broken_dir}</b></div>` : ""}
        ${trade.range_pips ? `<div><span style="color:var(--text3)">Asian Range</span><br><b>${trade.range_pips} pips</b></div>` : ""}
      </div>
      <canvas id="dtMiniChart" style="width:100%;height:280px;background:rgba(0,0,0,.4);border-radius:6px;display:block"></canvas>
      <div style="font-size:10px;color:var(--text3);text-align:center;margin-top:6px">Entry-bar gemarkeerd met arrow · SL (red) / TP (green) / Entry (white dashed) levels</div>
    </div>
  `;

  const canvas = document.getElementById("dtMiniChart");
  try{
    const r = await fetch(`/api/candles_around?pair=${trade.pair}&tf=${tf}&ts=${encodeURIComponent(trade.ts)}&bars_before=40&bars_after=30`);
    const d = await r.json();
    if(d.ok && d.candles && d.candles.length > 0){
      renderDtMiniChart(canvas, d.candles, trade, d.target_idx);
    } else {
      canvas.style.display = "none";
      const msg = document.createElement("div");
      msg.style.cssText = "padding:12px;text-align:center;color:var(--text3);font-size:11px;font-style:italic";
      msg.textContent = `Chart unavailable: ${d.error || "no data"}`;
      canvas.parentNode.insertBefore(msg, canvas);
    }
  }catch(e){
    canvas.style.display = "none";
  }

  // Scroll into view
  panel.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function renderDtMiniChart(canvas, candles, trade, targetIdx){
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0,0,W,H);
  let minP = Infinity, maxP = -Infinity;
  candles.forEach(c => { if(c.l<minP)minP=c.l; if(c.h>maxP)maxP=c.h; });
  minP = Math.min(minP, trade.sl, trade.tp, trade.entry);
  maxP = Math.max(maxP, trade.sl, trade.tp, trade.entry);
  const pad = (maxP-minP)*0.05; minP -= pad; maxP += pad;
  const padL=6, padR=70, padT=8, padB=8;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const cw = Math.max(1, plotW/candles.length*0.7);
  const cs = plotW/candles.length;
  const yScale = p => padT + (1-(p-minP)/(maxP-minP))*plotH;
  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  ctx.strokeStyle = "rgba(255,255,255,.05)"; ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono"; ctx.fillStyle = "rgba(255,255,255,.4)";
  for(let i=0;i<=4;i++){
    const y = padT + (i/4)*plotH;
    const p = maxP - (i/4)*(maxP-minP);
    ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke();
    ctx.fillText(p.toFixed(dec), W-padR+4, y+3);
  }
  // SL line
  const ySL = yScale(trade.sl);
  ctx.strokeStyle = "rgba(248,113,113,.7)"; ctx.lineWidth = 1; ctx.setLineDash([5,4]);
  ctx.beginPath(); ctx.moveTo(padL,ySL); ctx.lineTo(W-padR,ySL); ctx.stroke();
  ctx.fillStyle = "rgba(248,113,113,.9)"; ctx.fillRect(W-padR+1, ySL-8, padR-1, 16);
  ctx.fillStyle = "#fff"; ctx.fillText("SL "+trade.sl.toFixed(dec), W-padR+4, ySL+3);
  // TP line
  const yTP = yScale(trade.tp);
  ctx.strokeStyle = "rgba(52,211,153,.7)";
  ctx.beginPath(); ctx.moveTo(padL,yTP); ctx.lineTo(W-padR,yTP); ctx.stroke();
  ctx.fillStyle = "rgba(52,211,153,.9)"; ctx.fillRect(W-padR+1, yTP-8, padR-1, 16);
  ctx.fillStyle = "#fff"; ctx.fillText("TP "+trade.tp.toFixed(dec), W-padR+4, yTP+3);
  ctx.setLineDash([]);
  // Entry line
  const yEN = yScale(trade.entry);
  ctx.strokeStyle = "rgba(255,255,255,.6)"; ctx.lineWidth = 1; ctx.setLineDash([3,4]);
  ctx.beginPath(); ctx.moveTo(padL,yEN); ctx.lineTo(W-padR,yEN); ctx.stroke();
  ctx.setLineDash([]);
  // Candles
  candles.forEach((c,i) => {
    const x = padL + i*cs + cs/2;
    const yO = yScale(c.o), yC = yScale(c.c);
    const yH = yScale(c.h), yL = yScale(c.l);
    const color = c.c >= c.o ? "rgba(52,211,153,.85)" : "rgba(248,113,113,.85)";
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
    ctx.fillStyle = color;
    const bodyTop = Math.min(yO,yC), bodyH = Math.max(1, Math.abs(yC-yO));
    ctx.fillRect(x-cw/2, bodyTop, cw, bodyH);
  });
  // Entry marker
  if(targetIdx >= 0 && targetIdx < candles.length){
    const xT = padL + targetIdx*cs + cs/2;
    ctx.strokeStyle = "rgba(255,255,255,.4)"; ctx.lineWidth = 1; ctx.setLineDash([2,3]);
    ctx.beginPath(); ctx.moveTo(xT, padT); ctx.lineTo(xT, padT+plotH); ctx.stroke();
    ctx.setLineDash([]);
    const arrowY = padT + 12;
    ctx.fillStyle = trade.direction === "LONG" ? "rgba(52,211,153,1)" : "rgba(248,113,113,1)";
    ctx.beginPath();
    if(trade.direction === "LONG"){
      ctx.moveTo(xT, arrowY-4); ctx.lineTo(xT-4, arrowY+3); ctx.lineTo(xT+4, arrowY+3);
    } else {
      ctx.moveTo(xT, arrowY+4); ctx.lineTo(xT-4, arrowY-3); ctx.lineTo(xT+4, arrowY-3);
    }
    ctx.closePath(); ctx.fill();
  }
}

async function refreshLogs(){
  try{
    const r = await fetch("/api/logs");
    const d = await r.json();
    document.getElementById("logArea").innerHTML = (d.logs||[]).slice(-100).reverse().map(l => `<div class="log-line log-${l.level}">[${l.ts}] [${l.level}] ${l.msg}</div>`).join("");
  }catch(e){}
}

// init
refreshStatus();
loadPresets();
setInterval(refreshStatus, 5000);
setInterval(()=>{ if(S.currentTab==="logs") refreshLogs(); }, 10000);

// ── Market chart state + rendering ──
let chartState = { pair: "EURUSD", tf: "15M", candles: [] };

async function loadChartData(){
  try{
    const r = await fetch(`/api/candles?pair=${chartState.pair}&tf=${chartState.tf}&limit=200`);
    const d = await r.json();
    if(d.ok && d.candles){
      chartState.candles = d.candles;
      document.getElementById("chartCandles").textContent = d.candles.length;
      if(d.candles.length > 0){
        document.getElementById("chartLivePrice").textContent = d.candles[d.candles.length-1].c.toFixed(chartState.pair==="XAUUSD"?2:5);
      }
      renderChart();
    }
  }catch(e){ console.error("chart load:", e); }
}

function renderChart(){
  const canvas = document.getElementById("priceChart");
  if(!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const candles = chartState.candles;
  if(!candles.length) return;

  // Compute min/max
  let minP = Infinity, maxP = -Infinity;
  candles.forEach(c => {
    if(c.l < minP) minP = c.l;
    if(c.h > maxP) maxP = c.h;
  });
  const pad = (maxP - minP) * 0.05;
  minP -= pad; maxP += pad;

  const padL = 8, padR = 60, padT = 8, padB = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const cw = Math.max(1, plotW / candles.length * 0.7);  // candle width
  const cs = plotW / candles.length;                       // candle spacing

  const yScale = (p) => padT + (1 - (p - minP) / (maxP - minP)) * plotH;

  // Grid lines
  ctx.strokeStyle = "rgba(255,255,255,.05)"; ctx.lineWidth = 1;
  ctx.font = "10px JetBrains Mono";
  ctx.fillStyle = "rgba(255,255,255,.4)";
  for(let i = 0; i <= 4; i++){
    const y = padT + (i / 4) * plotH;
    const p = maxP - (i / 4) * (maxP - minP);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(p.toFixed(chartState.pair==="XAUUSD"?2:5), W - padR + 4, y + 3);
  }

  // Draw candles
  candles.forEach((c, i) => {
    const x = padL + i * cs + cs/2;
    const yO = yScale(c.o), yC = yScale(c.c);
    const yH = yScale(c.h), yL = yScale(c.l);
    const bullish = c.c >= c.o;
    const color = bullish ? "rgba(52,211,153,.85)" : "rgba(248,113,113,.85)";
    // Wick
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();
    // Body
    ctx.fillStyle = color;
    const bodyTop = Math.min(yO, yC), bodyH = Math.max(1, Math.abs(yC - yO));
    ctx.fillRect(x - cw/2, bodyTop, cw, bodyH);
  });

  // Latest price line
  const latest = candles[candles.length - 1];
  const yL = yScale(latest.c);
  ctx.strokeStyle = "rgba(255,255,255,.6)"; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(padL, yL); ctx.lineTo(W - padR, yL); ctx.stroke();
  ctx.setLineDash([]);
  // Price label
  ctx.fillStyle = "rgba(255,255,255,.15)";
  ctx.fillRect(W - padR + 1, yL - 8, padR - 1, 16);
  ctx.fillStyle = "#ffffff";
  ctx.fillText(latest.c.toFixed(chartState.pair==="XAUUSD"?2:5), W - padR + 4, yL + 3);
}

function switchChartPair(pair){
  chartState.pair = pair;
  document.querySelectorAll(".chart-btn[data-pair]").forEach(b => b.classList.toggle("active", b.dataset.pair === pair));
  document.getElementById("chartPairLabel").textContent = pair;
  loadChartData();
}
function switchChartTF(tf){
  chartState.tf = tf;
  document.querySelectorAll(".chart-btn[data-tf]").forEach(b => b.classList.toggle("active", b.dataset.tf === tf));
  document.getElementById("chartTFLabel").textContent = tf;
  loadChartData();
}

loadChartData();
setInterval(loadChartData, 30000);  // refresh chart every 30s
window.addEventListener("resize", () => renderChart());
// Default dates for backtester (last 30 days)
(function(){
  const end = new Date();
  const start = new Date(); start.setDate(start.getDate()-30);
  document.getElementById("btStart").value = start.toISOString().slice(0,10);
  document.getElementById("btEnd").value = end.toISOString().slice(0,10);
})();
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════
# MOBILE HTML TEMPLATE
# ════════════════════════════════════════════════════════════

MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>GAMAN-X Mobile</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Noto+Serif+JP:wght@900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#04020f; --bg2:#0a0a18; --card:rgba(15,15,25,.85);
  --border:rgba(42,42,63,.5); --border2:rgba(255,255,255,.25);
  --text:#fafafa; --text2:#e5e7eb; --text3:#6b6b7a;
  --glow:#ffffff; --glow2:#e5e7eb; --glow3:#f3f4f6;
  --green:#22c55e; --red:#f87171; --amber:#fbbf24;
}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:13px;overflow-x:hidden;min-height:100vh}
body::before{
  content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse at top,rgba(255,255,255,.10) 0%,transparent 60%);
  pointer-events:none;z-index:0;
}
#app{position:relative;z-index:2;padding-bottom:70px}

/* Topbar */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;background:rgba(0,0,0,.6);border-bottom:1px solid var(--border);backdrop-filter:blur(8px);position:sticky;top:0;z-index:100}
.logo{font-family:'Noto Serif JP',serif;font-size:20px;font-weight:900;color:var(--glow);text-shadow:0 0 10px rgba(255,255,255,.4);letter-spacing:1px}
.logo span{font-family:'Inter',sans-serif;font-size:9px;font-weight:600;color:var(--text2);letter-spacing:1.5px;margin-left:6px;opacity:.7}
.engine-pill{display:flex;align-items:center;gap:5px;padding:5px 10px;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:20px;font-size:10px;font-family:'JetBrains Mono',monospace}
.dot{width:6px;height:6px;border-radius:50%;background:var(--text3)}
.dot.live{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.paused{background:var(--amber);box-shadow:0 0 6px var(--amber)}

/* Bottom Tab Bar */
.tabbar{position:fixed;bottom:0;left:0;right:0;display:flex;background:rgba(0,0,0,.85);border-top:1px solid var(--border);backdrop-filter:blur(10px);z-index:100;height:60px}
.tabbar-tab{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;color:var(--text3);transition:.2s;font-size:10px}
.tabbar-tab .icn{font-size:18px;margin-bottom:2px}
.tabbar-tab.active{color:var(--glow)}

/* Cards */
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;margin:12px;backdrop-filter:blur(8px)}
.card-title{font-size:10px;font-weight:700;color:var(--glow2);letter-spacing:1px;text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between}

/* Strategy cards (Live tab) */
.strategies{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.strat-card{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:10px;transition:.2s}
.strat-card.enabled{border-color:var(--glow);box-shadow:0 0 10px rgba(255,255,255,.08)}
.strat-card.placeholder{opacity:.5}
.strat-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:4px}
.strat-name{font-weight:700;font-size:11px;color:var(--text);line-height:1.2}
.strat-id{font-family:'JetBrains Mono',monospace;font-size:8px;color:var(--text3);letter-spacing:1px;margin-top:2px}
.strat-stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px;font-size:9px;color:var(--text2);margin-top:6px}
.strat-stats div{background:rgba(0,0,0,.3);padding:3px 4px;border-radius:3px;text-align:center}
.strat-stats b{color:var(--text);display:block;font-size:11px}

/* Switch */
.switch{position:relative;width:32px;height:18px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#1a1a2e;border-radius:18px;cursor:pointer;transition:.3s;border:1px solid var(--border2)}
.slider::before{content:'';position:absolute;width:12px;height:12px;left:2px;top:2px;background:var(--text3);border-radius:50%;transition:.3s}
.switch input:checked + .slider{background:rgba(255,255,255,.2);border-color:var(--glow)}
.switch input:checked + .slider::before{transform:translateX(14px);background:var(--glow);box-shadow:0 0 6px var(--glow)}

/* Buttons */
.btn{background:rgba(255,255,255,.05);border:1px solid var(--border2);color:var(--text);padding:10px 16px;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;font-weight:600;width:100%;transition:.2s}
.btn:active{transform:scale(0.97)}
.btn.primary{background:rgba(52,211,153,.15);border-color:var(--green);color:var(--green)}
.btn.danger{background:rgba(248,113,113,.15);border-color:var(--red);color:var(--red)}
.btn.warn{background:rgba(251,191,36,.15);border-color:var(--amber);color:var(--amber)}
.btn-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}

/* Forms */
.form-group{display:flex;flex-direction:column;margin-bottom:10px}
.form-group label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.form-group input,.form-group select{background:rgba(0,0,0,.3);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:6px;font-family:inherit;font-size:14px;width:100%}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--glow)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}

/* Strategy chips (Backtester) */
.strat-chip{display:inline-flex;align-items:center;padding:8px 12px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--text2);transition:.2s;user-select:none;margin:3px 3px 3px 0}
.strat-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}
.strat-chip::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text3);margin-right:6px;transition:.2s}
.strat-chip.active::before{background:var(--green);box-shadow:0 0 6px var(--green)}
.tf-chip{display:inline-flex;align-items:center;padding:7px 14px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--text2);transition:.2s;user-select:none;font-weight:600;margin:3px 4px 3px 0}
.tf-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}

.killzone-chip{display:inline-block;padding:6px 10px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:14px;font-size:10px;margin:3px 3px 3px 0;cursor:pointer;color:var(--text2);user-select:none}
.killzone-chip.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}

/* Tables */
.trades-list{display:flex;flex-direction:column;gap:6px}
.trade-row{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:11px}
.trade-row .row1{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.trade-row .row2{display:flex;justify-content:space-between;color:var(--text3);font-size:10px;font-family:'JetBrains Mono',monospace}
.dir-LONG{color:var(--green);font-weight:700}
.dir-SHORT{color:var(--red);font-weight:700}
.pnl-pos{color:var(--green)}
.pnl-neg{color:var(--red)}

/* Stats grid */
.stats-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:14px}
.stat-tile{padding:10px 6px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center}
.stat-tile .lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
.stat-tile .val{font-size:16px;font-weight:700}

/* Logs */
.log-area{background:rgba(0,0,0,.4);border:1px solid var(--border);border-radius:6px;padding:10px;font-family:'JetBrains Mono',monospace;font-size:10px;max-height:60vh;overflow:auto}
.log-line{padding:3px 0;border-bottom:1px solid rgba(255,255,255,.03);word-break:break-word}
.log-INFO{color:var(--text2)}
.log-WARN{color:var(--amber)}
.log-ERROR{color:var(--red)}
.log-TRADE{color:var(--green)}

/* Collapsibles */
.collapse-head{cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between}
.collapse-head::after{content:'▼';font-size:9px;color:var(--text3);transition:transform .25s}
.collapse-section.collapsed .collapse-head::after{transform:rotate(-90deg)}
.collapse-body{max-height:3000px;overflow:hidden;transition:max-height .35s ease,opacity .25s;opacity:1}
.collapse-section.collapsed .collapse-body{max-height:0;opacity:0;margin:0;padding:0}

/* Chart */
#mobChart{width:100%;height:200px;background:rgba(0,0,0,.4);border-radius:6px;display:block}
.chart-toggles{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.chart-btn{background:rgba(255,255,255,.03);border:1px solid var(--border);color:var(--text2);padding:5px 10px;border-radius:4px;font-size:10px;cursor:pointer;font-family:inherit}
.chart-btn.active{background:rgba(255,255,255,.15);border-color:var(--glow);color:var(--text)}

/* Diagnostics */
.diag-row{padding:8px;background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:6px;margin-bottom:6px;font-family:'JetBrains Mono',monospace;font-size:10px;word-break:break-all}
.diag-row .sid{color:var(--glow2);font-weight:700;display:block;margin-bottom:4px}

.tabpage{display:none}
.tabpage.active{display:block}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.9);color:var(--text);padding:10px 18px;border-radius:20px;border:1px solid var(--border);font-size:11px;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
.placeholder-msg{padding:20px;text-align:center;color:var(--text3);font-size:11px;font-style:italic}
</style>
</head>
<body>
<div id="app">

<div class="topbar">
  <div class="logo">無限<span>GAMAN-X</span></div>
  <div class="engine-pill"><span class="dot" id="engineDot"></span><span id="engineLabel">OFFLINE</span></div>
</div>

<!-- LIVE TAB -->
<div class="tabpage active" id="page-live">

  <div class="card">
    <div class="card-title">Engine Control</div>
    <div class="btn-row">
      <button class="btn primary" onclick="engineStart()" style="font-size:11px;padding:8px 6px">▶ START</button>
      <button class="btn warn" onclick="engineTogglePause()" id="pauseBtn" style="font-size:11px;padding:8px 6px">⏸ PAUSE</button>
      <button class="btn danger" onclick="engineStop()" style="font-size:11px;padding:8px 6px">⏹ STOP</button>
    </div>
    <div style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace;text-align:center" id="scanInfo">Scans: 0</div>
  </div>

  <div class="card">
    <div class="card-title">Market Chart</div>
    <div class="chart-toggles">
      <button class="chart-btn active" data-pair="EURUSD" onclick="switchChartPair('EURUSD')">EURUSD</button>
      <button class="chart-btn" data-pair="XAUUSD" onclick="switchChartPair('XAUUSD')">XAUUSD</button>
      <span style="color:var(--text3);align-self:center">|</span>
      <button class="chart-btn active" data-tf="15M" onclick="switchChartTF('15M')">15M</button>
      <button class="chart-btn" data-tf="1H" onclick="switchChartTF('1H')">1H</button>
      <button class="chart-btn" data-tf="4H" onclick="switchChartTF('4H')">4H</button>
    </div>
    <canvas id="mobChart"></canvas>
    <div style="margin-top:6px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);display:flex;justify-content:space-between">
      <span id="chartPairLabel">EURUSD</span>
      <span>Live: <span id="chartLivePrice" style="color:var(--glow)">-</span></span>
      <span><span id="chartCandles">-</span> candles</span>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Strategies</div>
    <div class="strategies" id="stratGrid"></div>
  </div>

  <div class="card">
    <div class="card-title">Open Positions <span id="openCount" style="color:var(--text3);font-weight:400">0 open</span></div>
    <div id="openPos"><div class="placeholder-msg">No open positions</div></div>
  </div>

  <div class="card">
    <div class="card-title">Recent Trades</div>
    <div id="closedTrades"><div class="placeholder-msg">No closed trades yet</div></div>
  </div>

</div>

<!-- CONFIG TAB -->
<div class="tabpage" id="page-config">

  <div class="card collapse-section">
    <div class="card-title collapse-head" onclick="this.parentElement.classList.toggle('collapsed')">General Config</div>
    <div class="collapse-body">
      <div class="form-row">
        <div class="form-group"><label>Pair</label><select id="cfgPair"><option value="EURUSD">EURUSD</option><option value="XAUUSD">XAUUSD</option><option value="BOTH" selected>BOTH</option></select></div>
        <div class="form-group"><label>RR</label><input type="number" id="cfgRR" value="2" min="0.5" step="0.5"></div>
      </div>
      <div class="form-group">
        <label>Timeframes (tap 1 of meer)</label>
        <div id="cfgTFPicker">
          <span class="tf-chip active" data-tf="15M" onclick="toggleTFChip(this)">15M</span>
          <span class="tf-chip" data-tf="1H" onclick="toggleTFChip(this)">1H</span>
          <span class="tf-chip" data-tf="4H" onclick="toggleTFChip(this)">4H</span>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>Lot EUR</label><input type="number" id="cfgLotEur" value="10" min="1"></div>
        <div class="form-group"><label>Lot XAU</label><input type="number" id="cfgLotXau" value="1" min="1"></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>Capital</label><input type="number" id="cfgCap" value="10000" min="100"></div>
        <div class="form-group"></div>
      </div>
      <div class="form-group"><label>Discord webhook</label><input type="text" id="cfgDiscord" placeholder="https://discord.com/api/webhooks/..."></div>
    </div>
  </div>

  <div class="card collapse-section collapsed">
    <div class="card-title collapse-head" onclick="this.parentElement.classList.toggle('collapsed')">Silver Bullet</div>
    <div class="collapse-body">
      <label style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;display:block">Killzones</label>
      <div style="margin-bottom:10px">
        <span class="killzone-chip active" data-kz="london" onclick="toggleKZ(this)">London 03-04 NY</span>
        <span class="killzone-chip active" data-kz="ny_am" onclick="toggleKZ(this)">NY AM 10-11</span>
        <span class="killzone-chip active" data-kz="ny_pm" onclick="toggleKZ(this)">NY PM 14-15</span>
      </div>
      <div class="form-group"><label>Displacement ATR multiplier</label><input type="number" id="sbDispAtr" value="1.5" step="0.1" min="1"></div>
    </div>
  </div>

  <div class="card collapse-section collapsed">
    <div class="card-title collapse-head" onclick="this.parentElement.classList.toggle('collapsed')">CHoCH</div>
    <div class="collapse-body">
      <div class="form-group"><label>Swing length (pivot bars)</label><input type="number" id="chSwingLen" value="5" min="3" max="20"></div>
    </div>
  </div>

  <div class="card collapse-section collapsed">
    <div class="card-title collapse-head" onclick="this.parentElement.classList.toggle('collapsed')">BOS</div>
    <div class="collapse-body">
      <div class="form-group"><label>Swing length (pivot bars)</label><input type="number" id="bosSwingLen" value="5" min="3" max="20"></div>
    </div>
  </div>

  <div class="card collapse-section collapsed">
    <div class="card-title collapse-head" onclick="this.parentElement.classList.toggle('collapsed')">Asian Breakout</div>
    <div class="collapse-body">
      <div class="form-row">
        <div class="form-group"><label>Min range pips</label><input type="number" id="abMinRange" placeholder="auto" min="0" step="5"></div>
        <div class="form-group"><label>Max range pips</label><input type="number" id="abMaxRange" placeholder="auto" min="0" step="10"></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>TP range × mult</label><input type="number" id="abTpMult" value="1.5" min="0.5" step="0.1"></div>
        <div class="form-group"><label>Use RR</label><select id="abUseRR"><option value="false" selected>Range proj</option><option value="true">RR-based</option></select></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Config Presets</div>
    <div style="display:flex;gap:6px;margin-bottom:10px">
      <input type="text" id="presetName" placeholder="Naam (bv. EUR-Aggressive)" style="flex:1;background:rgba(0,0,0,.3);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:6px;font-family:inherit;font-size:13px">
      <button class="btn primary" onclick="savePreset()" style="white-space:nowrap;width:auto;font-size:11px;padding:8px 14px">💾 Save</button>
    </div>
    <div id="presetList"></div>
  </div>

</div>

<!-- BACKTESTER TAB -->
<div class="tabpage" id="page-backtest">

  <div class="card">
    <div class="card-title">Backtester</div>

    <label style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;display:block">Strategies (1 of meer)</label>
    <div id="btStratPicker" style="margin-bottom:8px">
      <span class="strat-chip active" data-sid="SB" onclick="toggleStratChip(this)">Silver Bullet</span>
      <span class="strat-chip" data-sid="CH" onclick="toggleStratChip(this)">CHoCH</span>
      <span class="strat-chip" data-sid="BOS" onclick="toggleStratChip(this)">BOS</span>
      <span class="strat-chip" data-sid="AB" onclick="toggleStratChip(this)">Asian BO</span>
    </div>
    <div class="btn-row" style="grid-template-columns:1fr 1fr;margin-bottom:14px">
      <button class="btn" style="font-size:10px;padding:6px" onclick="selectAllStrats()">Select All</button>
      <button class="btn" style="font-size:10px;padding:6px" onclick="selectNoStrats()">Select None</button>
    </div>

    <div class="form-row">
      <div class="form-group"><label>Pair</label><select id="btPair"><option>EURUSD</option><option>XAUUSD</option></select></div>
      <div class="form-group"><label>Timeframe</label><select id="btTF"><option value="15M" selected>15M</option><option value="1H">1H</option><option value="4H">4H</option><option value="15M+1H">15M+1H</option></select></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Start date</label><input type="date" id="btStart"></div>
      <div class="form-group"><label>End date</label><input type="date" id="btEnd"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Lot</label><input type="number" id="btLot" value="10" min="1"></div>
      <div class="form-group"><label>RR</label><input type="number" id="btRR" value="2" step="0.5"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Disp ATR (SB)</label><input type="number" id="btDispAtr" value="1.5" step="0.1"></div>
      <div class="form-group"><label>Swing (CH/BOS)</label><input type="number" id="btSwingLen" value="5" min="3" max="20"></div>
    </div>

    <button class="btn primary" onclick="runBacktest()" id="btRunBtn">▶ RUN BACKTEST</button>
    <div id="btResult" style="margin-top:16px"></div>
  </div>

</div>

<!-- LOGS TAB -->
<div class="tabpage" id="page-logs">
  <div class="card">
    <div class="card-title">System Logs <button class="btn" onclick="refreshLogs()" style="width:auto;padding:4px 10px;font-size:10px">↻</button></div>
    <div class="log-area" id="logArea"></div>
  </div>
</div>

</div>

<div class="tabbar">
  <div class="tabbar-tab active" data-tab="live" onclick="switchTab('live')"><span class="icn">▤</span>Live</div>
  <div class="tabbar-tab" data-tab="config" onclick="switchTab('config')"><span class="icn">⚙</span>Config</div>
  <div class="tabbar-tab" data-tab="backtest" onclick="switchTab('backtest')"><span class="icn">⊞</span>Backtest</div>
  <div class="tabbar-tab" data-tab="logs" onclick="switchTab('logs')"><span class="icn">≣</span>Logs</div>
</div>

<div class="toast" id="toast"></div>

<script>
const STRATEGIES = [
  {id:"SB",  name:"Silver Bullet",  sub:"Killzone FVG", placeholder:false},
  {id:"CH",  name:"CHoCH",          sub:"Reversal", placeholder:false},
  {id:"BOS", name:"BOS",            sub:"Continuation", placeholder:false},
  {id:"AB",  name:"Asian BO",       sub:"Session", placeholder:false},
];

let S = { status: null, currentTab: "live", config: null };

function showToast(msg, ms=2500){
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

function switchTab(tab){
  S.currentTab = tab;
  document.querySelectorAll(".tabbar-tab").forEach(t => t.classList.toggle("active", t.dataset.tab===tab));
  document.querySelectorAll(".tabpage").forEach(p => p.classList.toggle("active", p.id===`page-${tab}`));
  if(tab==="logs") refreshLogs();
  if(tab==="live") loadChartData();
}

async function refreshStatus(){
  try{
    const r = await fetch("/api/status");
    const d = await r.json();
    S.status = d;
    if(!S.config){ S.config = d.config; renderConfig(); }
    renderTopbar();
    renderStrategies();
    renderOpenTrades();
    renderClosed();
  }catch(e){ console.error("status fetch:", e); }
}

function renderTopbar(){
  const d = S.status;
  const dot = document.getElementById("engineDot");
  const lbl = document.getElementById("engineLabel");
  if(d.running && d.paused){ dot.className = "dot paused"; lbl.textContent = "PAUSED"; }
  else if(d.running){ dot.className = "dot live"; lbl.textContent = "LIVE"; }
  else { dot.className = "dot"; lbl.textContent = "OFFLINE"; }
  document.getElementById("scanInfo").textContent = `Scans: ${d.scan_count} | Last: ${d.last_scan || "-"}`;
}

function renderStrategies(){
  const grid = document.getElementById("stratGrid");
  const stats = (S.status && S.status.stats) || {};
  const cfg = (S.status && S.status.config && S.status.config.strategies) || {};
  grid.innerHTML = STRATEGIES.map(s => {
    const st = stats[s.id] || {trades:0,wr:0,pnl:0,open:0};
    const enabled = cfg[s.id] && cfg[s.id].enabled;
    return `
      <div class="strat-card ${enabled?'enabled':''}">
        <div class="strat-head">
          <div>
            <div class="strat-name">${s.name}</div>
            <div class="strat-id">${s.id} · ${s.sub}</div>
          </div>
          <label class="switch"><input type="checkbox" ${enabled?'checked':''} onchange="toggleStrategy('${s.id}',this.checked)"><span class="slider"></span></label>
        </div>
        <div class="strat-stats">
          <div><b>${st.trades}</b>tr</div>
          <div><b>${st.wr}%</b>wr</div>
          <div><b>€${st.pnl.toFixed(0)}</b>pnl</div>
        </div>
      </div>
    `;
  }).join("");
}

function renderOpenTrades(){
  const trades = (S.status && S.status.open_trades) || [];
  document.getElementById("openCount").textContent = `${trades.length} open`;
  const el = document.getElementById("openPos");
  if(!trades.length){ el.innerHTML = '<div class="placeholder-msg">No open positions</div>'; return; }
  window._liveOpenTrades = trades;
  el.innerHTML = `<div class="trades-list">${trades.map((t,idx) => `
    <div class="trade-row" style="cursor:pointer" onclick="toggleMobLiveTradeDetail('open', ${idx}, this, event)">
      <div class="row1">
        <span><b>${t.id}</b> <span class="dir-${t.direction}">${t.direction}</span> ${t.pair}${t.tf?' · '+t.tf:''}</span>
        <span class="${(t.pnl_eur||0)>=0?'pnl-pos':'pnl-neg'}" style="font-weight:700">€${(t.pnl_eur||0).toFixed(2)}</span>
      </div>
      <div class="row2">
        <span>Entry ${t.entry.toFixed(t.pair==="XAUUSD"?2:5)}</span>
        <span>Live ${(t.live_price||0).toFixed(t.pair==="XAUUSD"?2:5)}</span>
        <button class="btn danger" style="width:auto;padding:3px 10px;font-size:9px" onclick="event.stopPropagation();closeTrade('${t.id}')">✕ Close</button>
      </div>
      <div class="trade-detail" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"></div>
    </div>
  `).join("")}</div>`;
}

function renderClosed(){
  const trades = ((S.status && S.status.closed_trades) || []).slice(-15).reverse();
  const el = document.getElementById("closedTrades");
  if(!trades.length){ el.innerHTML = '<div class="placeholder-msg">No closed trades yet</div>'; return; }
  window._liveClosedTrades = trades;
  el.innerHTML = `<div class="trades-list">${trades.map((t, idx) => `
    <div class="trade-row" style="cursor:pointer" onclick="toggleMobLiveTradeDetail('closed', ${idx}, this, event)">
      <div class="row1">
        <span><b>${t.id}</b> <span class="dir-${t.direction}">${t.direction}</span> ${t.pair}${t.tf?' · '+t.tf:''}</span>
        <span class="${(t.pnl_eur||0)>=0?'pnl-pos':'pnl-neg'}" style="font-weight:700">€${(t.pnl_eur||0).toFixed(2)}</span>
      </div>
      <div class="row2">
        <span>${(t.closed_at||"").slice(11,16)} ${t.hit}</span>
        <span>${(t.pips||0).toFixed(1)}p</span>
      </div>
      <div class="trade-detail" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"></div>
    </div>
  `).join("")}</div>`;
}

function renderConfig(){
  if(!S.config) return;
  document.getElementById("cfgPair").value = S.config.pair;
  // Multi-TF chips
  const activeTFs = S.config.tfs || (S.config.tf ? [S.config.tf] : ["15M"]);
  document.querySelectorAll("#cfgTFPicker .tf-chip").forEach(c => {
    c.classList.toggle("active", activeTFs.includes(c.dataset.tf));
  });
  document.getElementById("cfgLotEur").value = S.config.lot_eur;
  document.getElementById("cfgLotXau").value = S.config.lot_xau;
  document.getElementById("cfgRR").value = S.config.rr;
  document.getElementById("cfgCap").value = S.config.capital;
  document.getElementById("cfgDiscord").value = S.config.discord_webhook;
  const sb = S.config.strategies.SB || {};
  document.getElementById("sbDispAtr").value = sb.disp_atr || 1.5;
  document.querySelectorAll(".killzone-chip").forEach(c => {
    if((sb.killzones||[]).includes(c.dataset.kz)) c.classList.add("active");
    else c.classList.remove("active");
  });
  const ch = S.config.strategies.CH || {};
  document.getElementById("chSwingLen").value = ch.swing_length || 5;
  const bos = S.config.strategies.BOS || {};
  document.getElementById("bosSwingLen").value = bos.swing_length || 5;
  const ab = S.config.strategies.AB || {};
  document.getElementById("abMinRange").value = ab.min_range_pips || "";
  document.getElementById("abMaxRange").value = ab.max_range_pips || "";
  document.getElementById("abTpMult").value = ab.tp_range_mult || 1.5;
  document.getElementById("abUseRR").value = ab.use_rr ? "true" : "false";
}

function toggleKZ(el){ el.classList.toggle("active"); }
function toggleStratChip(el){ el.classList.toggle("active"); }
function toggleTFChip(el){
  el.classList.toggle("active");
  // Auto-save engine config so changes persist even when not started
  try{ fetch("/api/engine/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({tfs: [...document.querySelectorAll("#cfgTFPicker .tf-chip.active")].map(c => c.dataset.tf)})}); }catch(e){}
}
function selectAllStrats(){ document.querySelectorAll("#btStratPicker .strat-chip").forEach(c => c.classList.add("active")); }
function selectNoStrats(){ document.querySelectorAll("#btStratPicker .strat-chip").forEach(c => c.classList.remove("active")); }
function getSelectedStrats(){ return [...document.querySelectorAll("#btStratPicker .strat-chip.active")].map(c => c.dataset.sid); }

async function toggleStrategy(sid, enabled){
  const strategies = {}; strategies[sid] = {enabled};
  await fetch("/api/engine/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({strategies})});
  refreshStatus();
}

function getBodyConfig(){
  const killzones = [...document.querySelectorAll(".killzone-chip.active")].map(c => c.dataset.kz);
  return {
    pair: document.getElementById("cfgPair").value,
    tfs: [...document.querySelectorAll("#cfgTFPicker .tf-chip.active")].map(c => c.dataset.tf),
    lot_eur: parseInt(document.getElementById("cfgLotEur").value)||10,
    lot_xau: parseInt(document.getElementById("cfgLotXau").value)||1,
    rr: parseFloat(document.getElementById("cfgRR").value)||2.0,
    capital: parseFloat(document.getElementById("cfgCap").value)||10000,
    discord_webhook: document.getElementById("cfgDiscord").value.trim(),
    strategies: {
      SB: {killzones, disp_atr: parseFloat(document.getElementById("sbDispAtr").value)||1.5},
      CH: {swing_length: parseInt(document.getElementById("chSwingLen").value)||5},
      BOS: {swing_length: parseInt(document.getElementById("bosSwingLen").value)||5},
      AB: {
        min_range_pips: parseInt(document.getElementById("abMinRange").value) || null,
        max_range_pips: parseInt(document.getElementById("abMaxRange").value) || null,
        tp_range_mult: parseFloat(document.getElementById("abTpMult").value)||1.5,
        use_rr: document.getElementById("abUseRR").value === "true",
      },
    },
  };
}

// Mobile live trade detail expand
async function toggleMobLiveTradeDetail(kind, tradeIdx, rowEl, ev){
  if(ev) ev.stopPropagation();
  const detailEl = rowEl.querySelector(".trade-detail");
  if(!detailEl) return;
  if(detailEl.style.display !== "none"){
    detailEl.style.display = "none";
    detailEl.innerHTML = "";
    return;
  }
  const list = kind === "open" ? (window._liveOpenTrades || []) : (window._liveClosedTrades || []);
  const trade = list[tradeIdx];
  if(!trade) return;
  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  const tf = trade.tf || "15M";
  const startTs = trade.opened_at || trade.ts;
  const endTs = trade.closed_at;
  let durationStr = "-";
  if(startTs && endTs){
    try{
      const t1 = new Date(startTs.replace(" ", "T"));
      const t2 = new Date(endTs.replace(" ", "T"));
      const diff = Math.abs(t2-t1);
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = hours > 0 ? `${hours}u ${mins}m` : `${mins}m`;
    }catch(e){}
  } else if(startTs && kind === "open"){
    try{
      const t1 = new Date(startTs.replace(" ", "T"));
      const diff = Math.abs(Date.now() - t1.getTime());
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = (hours > 0 ? `${hours}u ${mins}m` : `${mins}m`) + " open";
    }catch(e){}
  }
  const rr = Math.abs(trade.tp - trade.entry) / Math.max(0.000001, Math.abs(trade.entry - trade.sl));
  const meta = trade.meta || {};
  const exitVal = trade.exit !== undefined ? trade.exit : (trade.live_price || 0);

  detailEl.style.display = "block";
  detailEl.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:10px;margin-bottom:10px">
      <div><span style="color:var(--text3)">Opened:</span> <b>${(startTs||"").slice(5,16)}</b></div>
      ${kind==="closed" ? `<div><span style="color:var(--text3)">Closed:</span> <b>${(endTs||"").slice(5,16)}</b></div>` : `<div><span style="color:var(--text3)">Duration:</span> <b>${durationStr}</b></div>`}
      <div><span style="color:var(--text3)">Entry:</span> <b>${trade.entry.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">${kind==="closed"?"Exit":"Current"}:</span> <b>${exitVal.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">SL:</span> <b style="color:var(--red)">${trade.sl.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">TP:</span> <b style="color:var(--green)">${trade.tp.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">RR:</span> <b>${rr.toFixed(2)}</b></div>
      <div><span style="color:var(--text3)">Lot:</span> <b>${trade.lotsize||"-"}</b></div>
      ${kind==="closed" ? `<div><span style="color:var(--text3)">Hit:</span> <b>${trade.hit}</b></div><div><span style="color:var(--text3)">Pips:</span> <b>${trade.pips}</b></div>` : ""}
      ${meta.killzone ? `<div><span style="color:var(--text3)">KZ:</span> <b>${meta.killzone}</b></div>` : ""}
      ${meta.prior_trend ? `<div><span style="color:var(--text3)">Prior:</span> <b>${meta.prior_trend}</b></div>` : ""}
      ${meta.trend ? `<div><span style="color:var(--text3)">Trend:</span> <b>${meta.trend}</b></div>` : ""}
      ${meta.broken_dir ? `<div><span style="color:var(--text3)">Break:</span> <b>${meta.broken_dir}</b></div>` : ""}
    </div>
    <canvas class="mini-chart" style="width:100%;height:150px;background:rgba(0,0,0,.4);border-radius:6px;display:block"></canvas>
    <div style="font-size:9px;color:var(--text3);text-align:center;margin-top:4px">Entry-bar gemarkeerd · SL/TP levels</div>
  `;
  const canvas = detailEl.querySelector(".mini-chart");
  try{
    const r = await fetch(`/api/candles_around?pair=${trade.pair}&tf=${tf}&ts=${encodeURIComponent(startTs)}&bars_before=30&bars_after=20`);
    const d = await r.json();
    if(d.ok && d.candles && d.candles.length > 0){
      renderMiniChart(canvas, d.candles, {
        entry: trade.entry, sl: trade.sl, tp: trade.tp,
        direction: trade.direction, pair: trade.pair
      }, d.target_idx);
    } else {
      canvas.style.display = "none";
      const msg = document.createElement("div");
      msg.style.cssText = "padding:8px;text-align:center;color:var(--text3);font-size:10px;font-style:italic";
      msg.textContent = `Chart unavailable: ${d.error || "no data"}`;
      canvas.parentNode.insertBefore(msg, canvas);
    }
  }catch(e){ canvas.style.display = "none"; }
}

// Preset management (mobile)
async function loadPresets(){
  try{
    const r = await fetch("/api/presets");
    const d = await r.json();
    const list = document.getElementById("presetList");
    if(!list) return;
    const presets = d.presets || {};
    const names = Object.keys(presets);
    if(names.length === 0){
      list.innerHTML = '<div style="color:var(--text3);font-size:11px;text-align:center;padding:10px;font-style:italic">Geen presets</div>';
      return;
    }
    list.innerHTML = names.map(name => {
      const p = presets[name];
      const s = p.summary || {};
      return `
        <div style="background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:6px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <div style="font-weight:600;color:var(--text);font-size:12px">${name}</div>
            <div style="display:flex;gap:4px">
              <button class="btn" style="width:auto;padding:4px 8px;font-size:10px" onclick="applyPreset('${name}')">↓</button>
              <button class="btn danger" style="width:auto;padding:4px 8px;font-size:10px" onclick="deletePreset('${name}')">✕</button>
            </div>
          </div>
          <div style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace">${s.pair||"?"} · ${(s.tfs||[]).join("+")} · ${(s.strats||[]).join(",") || "none"} · RR ${s.rr||"?"}</div>
        </div>
      `;
    }).join("");
  }catch(e){}
}

async function savePreset(){
  const nameInput = document.getElementById("presetName");
  const name = (nameInput.value || "").trim();
  if(!name){ showToast("Geef een naam op"); return; }
  if(!/^[A-Za-z0-9_\-\.\s]+$/.test(name)){ showToast("Alleen letters/cijfers/_-."); return; }
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({config: getBodyConfig()})
    });
    const d = await r.json();
    if(d.ok){
      nameInput.value = "";
      showToast("Preset opgeslagen");
      loadPresets();
    } else { showToast("Save failed"); }
  }catch(e){ showToast("Error: " + e.message); }
}

async function applyPreset(name){
  if(!confirm(`Preset "${name}" toepassen?`)) return;
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}/apply`, {method: "POST"});
    const d = await r.json();
    if(d.ok){
      S.config = d.config;
      renderConfig();
      showToast("Preset toegepast");
    } else { showToast("Apply failed"); }
  }catch(e){}
}

async function deletePreset(name){
  if(!confirm(`Preset "${name}" verwijderen?`)) return;
  try{
    const r = await fetch(`/api/presets/${encodeURIComponent(name)}`, {method: "DELETE"});
    const d = await r.json();
    if(d.ok){ showToast("Preset verwijderd"); loadPresets(); }
  }catch(e){}
}

async function engineStart(){
  const body = getBodyConfig();
  await fetch("/api/engine/start", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  showToast("Engine started");
  refreshStatus();
}
async function engineStop(){
  if(!confirm("Engine stoppen?")) return;
  await fetch("/api/engine/stop", {method:"POST"});
  showToast("Engine stopped");
  refreshStatus();
}
let _isPaused = false;
async function engineTogglePause(){
  const url = _isPaused ? "/api/engine/resume" : "/api/engine/pause";
  await fetch(url, {method:"POST"});
  _isPaused = !_isPaused;
  document.getElementById("pauseBtn").textContent = _isPaused ? "▶ RESUME" : "⏸ PAUSE";
  showToast(_isPaused ? "Engine paused" : "Engine resumed");
  refreshStatus();
}
async function closeTrade(tid){
  if(!confirm("Trade sluiten?")) return;
  await fetch(`/api/trade/${tid}/close`, {method:"POST"});
  showToast("Trade closed");
  refreshStatus();
}

async function runBacktest(){
  const btn = document.getElementById("btRunBtn");
  const selectedStrats = getSelectedStrats();
  if(selectedStrats.length === 0){ showToast("Selecteer minimaal één strategie"); return; }
  btn.disabled = true; btn.textContent = "Running...";
  const killzones = [...document.querySelectorAll(".killzone-chip.active")].map(c => c.dataset.kz);
  const isSingle = selectedStrats.length === 1;
  const strategyParam = isSingle ? selectedStrats[0] : "MIX";
  const baseBody = {
    strategy: strategyParam, strategies: selectedStrats,
    pair: document.getElementById("btPair").value,
    start: document.getElementById("btStart").value,
    end: document.getElementById("btEnd").value,
    lot: parseInt(document.getElementById("btLot").value)||10,
    rr: parseFloat(document.getElementById("btRR").value)||2,
    disp_atr: parseFloat(document.getElementById("btDispAtr").value)||1.5,
    swing_length: parseInt(document.getElementById("btSwingLen").value)||5,
    killzones,
    min_range_pips: parseInt(document.getElementById("abMinRange").value) || null,
    max_range_pips: parseInt(document.getElementById("abMaxRange").value) || null,
    tp_range_mult: parseFloat(document.getElementById("abTpMult").value)||1.5,
    use_rr: document.getElementById("abUseRR").value === "true",
  };
  const tfChoice = document.getElementById("btTF").value;
  const isCombined = tfChoice === "15M+1H";
  try{
    if(!isCombined){
      const r = await fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({...baseBody, tf: tfChoice})});
      renderBacktestResult(await r.json());
    } else {
      btn.textContent = "Running 15M+1H...";
      const [r15, r1h] = await Promise.all([
        fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({...baseBody, tf:"15M"})}).then(r=>r.json()),
        fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({...baseBody, tf:"1H"})}).then(r=>r.json()),
      ]);
      const trades15 = (r15.trades || []).map(t => ({...t, tf:"15M"}));
      const trades1h = (r1h.trades || []).map(t => ({...t, tf:"1H"}));
      const allTrades = [...trades15, ...trades1h].sort((a,b)=>(a.ts||"").localeCompare(b.ts||""));
      const wins = allTrades.filter(t => t.pnl > 0);
      const losses = allTrades.filter(t => t.pnl <= 0);
      const totalPnl = allTrades.reduce((s,t)=>s+t.pnl,0);
      const sumW = wins.reduce((s,t)=>s+t.pnl,0);
      const sumL = Math.abs(losses.reduce((s,t)=>s+t.pnl,0));
      // Max drawdown calculation
      let equity = 10000, peak = 10000, maxDD = 0;
      allTrades.forEach(t => {
        equity += t.pnl;
        if(equity > peak) peak = equity;
        const dd = equity - peak;
        if(dd < maxDD) maxDD = dd;
      });
      // Merge per-strategy
      const mergedPerStrat = {};
      ["15M","1H"].forEach(tf => {
        const src = tf==="15M"?r15:r1h;
        if(src && src.per_strategy){
          Object.entries(src.per_strategy).forEach(([sid, st]) => {
            if(!mergedPerStrat[sid]) mergedPerStrat[sid] = {total:0,wins:0,losses:0,wr:0,pnl:0};
            mergedPerStrat[sid].total += (st.total||0);
            mergedPerStrat[sid].wins += (st.wins||0);
            mergedPerStrat[sid].losses += (st.losses||0);
            mergedPerStrat[sid].pnl = Math.round(100*(mergedPerStrat[sid].pnl + (st.pnl||0)))/100;
          });
        }
      });
      Object.values(mergedPerStrat).forEach(st => {
        st.wr = st.total ? Math.round(1000*st.wins/st.total)/10 : 0;
      });
      renderBacktestResult({
        mode:"combined-tf",
        trades:allTrades,
        stats:{
          total: allTrades.length,
          wins: wins.length,
          losses: losses.length,
          wr: allTrades.length ? Math.round(1000*wins.length/allTrades.length)/10 : 0,
          pnl: Math.round(totalPnl*100)/100,
          pf: losses.length && sumL>0 ? Math.round(100*sumW/sumL)/100 : (wins.length>0 ? 999 : 0),
          max_dd: Math.round(maxDD*100)/100,
        },
        per_tf:{"15M":r15.stats, "1H":r1h.stats},
        per_strategy: Object.keys(mergedPerStrat).length ? mergedPerStrat : undefined,
        diagnostics:{tf_15M:r15.diagnostics || r15.diagnostics_per_strategy, tf_1H:r1h.diagnostics || r1h.diagnostics_per_strategy},
      });
    }
  }catch(e){
    document.getElementById("btResult").innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = "▶ RUN BACKTEST";
  }
}

function renderBacktestResult(d){
  const el = document.getElementById("btResult");
  if(d.error){ el.innerHTML = `<div style="color:var(--red);padding:10px;background:rgba(248,113,113,.05);border-radius:6px">${d.error}</div>`; return; }
  const st = d.stats || {total:0};
  const trades = d.trades || [];
  let html = `
    <div class="stats-grid">
      <div class="stat-tile"><div class="lbl">Trades</div><div class="val">${st.total||0}</div></div>
      <div class="stat-tile"><div class="lbl">WR</div><div class="val">${st.wr||0}%</div></div>
      <div class="stat-tile"><div class="lbl">P&L</div><div class="val ${(st.pnl||0)>=0?'pnl-pos':'pnl-neg'}">€${st.pnl||0}</div></div>
      <div class="stat-tile"><div class="lbl">PF</div><div class="val">${st.pf||0}</div></div>
      <div class="stat-tile"><div class="lbl">Max DD</div><div class="val pnl-neg">€${st.max_dd||0}</div></div>
      <div class="stat-tile"><div class="lbl">Wins/Loss</div><div class="val" style="font-size:13px">${st.wins||0}/${st.losses||0}</div></div>
    </div>`;
  if(d.per_strategy){
    html += `<div class="card-title" style="margin:14px 0 6px">Per Strategy</div>`;
    Object.entries(d.per_strategy).forEach(([sid, s]) => {
      html += `<div class="trade-row" style="margin-bottom:4px"><div class="row1"><span><b>${sid}</b></span><span><b>${s.total||0}</b> trades · ${s.wr||0}% WR · <span class="${(s.pnl||0)>=0?'pnl-pos':'pnl-neg'}">€${s.pnl||0}</span></span></div></div>`;
    });
  }
  if(d.per_tf){
    html += `<div class="card-title" style="margin:14px 0 6px">Per Timeframe</div>`;
    Object.entries(d.per_tf).forEach(([tf, s]) => {
      html += `<div class="trade-row" style="margin-bottom:4px"><div class="row1"><span><b>${tf}</b></span><span><b>${(s&&s.total)||0}</b> · ${(s&&s.wr)||0}% · <span class="${((s&&s.pnl)||0)>=0?'pnl-pos':'pnl-neg'}">€${(s&&s.pnl)||0}</span></span></div></div>`;
    });
  }
  const diags = d.diagnostics_per_strategy || d.diagnostics;
  if(diags){
    html += `<div class="card-title collapse-head" style="margin:14px 0 6px;cursor:pointer" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display==='none' ? 'block' : 'none'">Diagnostics (tap)</div><div style="display:none">`;
    if(d.diagnostics_per_strategy){
      Object.entries(d.diagnostics_per_strategy).forEach(([sid, dg]) => {
        html += `<div class="diag-row"><span class="sid">${sid}</span>${Object.entries(dg||{}).map(([k,v])=>`<span style="color:var(--text3);margin-right:8px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
      });
    } else if(diags.tf_15M || diags.tf_1H){
      Object.entries(diags).forEach(([tf, sub]) => {
        if(!sub) return;
        if(typeof sub === "object" && (sub.SB || sub.CH || sub.BOS || sub.AB)){
          Object.entries(sub).forEach(([sid, dg]) => {
            html += `<div class="diag-row"><span class="sid">${tf} · ${sid}</span>${Object.entries(dg||{}).map(([k,v])=>`<span style="color:var(--text3);margin-right:8px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
          });
        } else {
          html += `<div class="diag-row"><span class="sid">${tf}</span>${Object.entries(sub||{}).map(([k,v])=>`<span style="color:var(--text3);margin-right:8px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
        }
      });
    } else {
      html += `<div class="diag-row">${Object.entries(diags).map(([k,v])=>`<span style="color:var(--text3);margin-right:8px">${k}=<b style="color:var(--text)">${v}</b></span>`).join("")}</div>`;
    }
    html += `</div>`;
  }
  if(d.msg && trades.length===0){
    html += `<div class="placeholder-msg">${d.msg}</div>`;
  }
  if(trades.length){
    // Store trades globally so tap handlers can access
    window._mobBacktestTrades = trades;
    html += `<div class="card-title" style="margin:14px 0 6px">Trades (last 30) <span style="font-size:9px;color:var(--text3);font-weight:400">tap voor detail</span></div>
      <div class="trades-list">${trades.slice(-30).reverse().map((t, idx) => `
        <div class="trade-row trade-tappable" data-tidx="${trades.length-1-idx}" onclick="toggleMobTradeDetail(this, ${trades.length-1-idx})" style="cursor:pointer">
          <div class="row1">
            <span>${(t.ts||"").slice(5,16)} <span class="dir-${t.direction}">${t.direction}</span> ${t.pair} ${t.strategy?'·'+t.strategy:''}</span>
            <span class="${t.pnl>=0?'pnl-pos':'pnl-neg'}" style="font-weight:700">€${t.pnl}</span>
          </div>
          <div class="row2">
            <span>${t.entry}→${t.exit}</span>
            <span>${t.hit} · ${t.pips}p</span>
          </div>
          <div class="trade-detail" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"></div>
        </div>
      `).join("")}</div>`;
  }
  el.innerHTML = html;
}

// Global trade detail toggle for mobile
async function toggleMobTradeDetail(rowEl, tradeIdx){
  const detailEl = rowEl.querySelector(".trade-detail");
  if(!detailEl) return;
  // If already open, close it
  if(detailEl.style.display !== "none"){
    detailEl.style.display = "none";
    detailEl.innerHTML = "";
    return;
  }
  const trade = (window._mobBacktestTrades || [])[tradeIdx];
  if(!trade){ return; }
  // Format detail panel
  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  const tf = trade.tf || document.getElementById("btTF").value || "15M";
  // Compute duration if we have exit_ts
  let durationStr = "-";
  if(trade.exit_ts && trade.ts){
    try{
      const t1 = new Date(trade.ts.replace(" ", "T"));
      const t2 = new Date(trade.exit_ts.replace(" ", "T"));
      const diff = Math.abs(t2-t1);
      const hours = Math.floor(diff / 3600000);
      const mins = Math.floor((diff % 3600000) / 60000);
      durationStr = hours > 0 ? `${hours}u ${mins}m` : `${mins}m`;
    }catch(e){}
  }
  // RR berekening
  const rr = Math.abs(trade.tp - trade.entry) / Math.max(0.000001, Math.abs(trade.entry - trade.sl));
  detailEl.style.display = "block";
  detailEl.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:10px;margin-bottom:10px">
      <div><span style="color:var(--text3)">Entry:</span> <b>${trade.entry.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">Exit:</span> <b>${trade.exit.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">SL:</span> <b style="color:var(--red)">${trade.sl.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">TP:</span> <b style="color:var(--green)">${trade.tp.toFixed(dec)}</b></div>
      <div><span style="color:var(--text3)">RR:</span> <b>${rr.toFixed(2)}</b></div>
      <div><span style="color:var(--text3)">Duration:</span> <b>${durationStr}</b></div>
      <div><span style="color:var(--text3)">Hit:</span> <b>${trade.hit}</b></div>
      <div><span style="color:var(--text3)">Pips:</span> <b>${trade.pips}</b></div>
      ${trade.killzone ? `<div><span style="color:var(--text3)">KZ:</span> <b>${trade.killzone}</b></div>` : ""}
      ${trade.prior_trend ? `<div><span style="color:var(--text3)">Prior:</span> <b>${trade.prior_trend}</b></div>` : ""}
      ${trade.trend ? `<div><span style="color:var(--text3)">Trend:</span> <b>${trade.trend}</b></div>` : ""}
      ${trade.broken_dir ? `<div><span style="color:var(--text3)">Break:</span> <b>${trade.broken_dir}</b></div>` : ""}
      ${trade.range_pips ? `<div><span style="color:var(--text3)">Range:</span> <b>${trade.range_pips}p</b></div>` : ""}
    </div>
    <canvas class="mini-chart" style="width:100%;height:150px;background:rgba(0,0,0,.4);border-radius:6px;display:block"></canvas>
    <div style="font-size:9px;color:var(--text3);text-align:center;margin-top:4px">Entry-bar gemarkeerd · SL (red) / TP (green) levels</div>
  `;
  // Fetch candles around trade timestamp
  const canvas = detailEl.querySelector(".mini-chart");
  try{
    const r = await fetch(`/api/candles_around?pair=${trade.pair}&tf=${tf}&ts=${encodeURIComponent(trade.ts)}&bars_before=30&bars_after=20`);
    const d = await r.json();
    if(d.ok && d.candles && d.candles.length > 0){
      renderMiniChart(canvas, d.candles, trade, d.target_idx);
    } else {
      canvas.style.display = "none";
      const msg = document.createElement("div");
      msg.style.cssText = "padding:8px;text-align:center;color:var(--text3);font-size:10px;font-style:italic";
      msg.textContent = `Chart unavailable: ${d.error || "no data"}`;
      canvas.parentNode.insertBefore(msg, canvas);
    }
  }catch(e){
    canvas.style.display = "none";
  }
}

function renderMiniChart(canvas, candles, trade, targetIdx){
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0,0,W,H);
  // Min/max including SL/TP
  let minP = Infinity, maxP = -Infinity;
  candles.forEach(c => { if(c.l<minP)minP=c.l; if(c.h>maxP)maxP=c.h; });
  minP = Math.min(minP, trade.sl, trade.tp, trade.entry);
  maxP = Math.max(maxP, trade.sl, trade.tp, trade.entry);
  const pad = (maxP-minP)*0.05; minP -= pad; maxP += pad;
  const padL=4, padR=55, padT=4, padB=4;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const cw = Math.max(1, plotW/candles.length*0.7);
  const cs = plotW/candles.length;
  const yScale = p => padT + (1-(p-minP)/(maxP-minP))*plotH;
  const dec = trade.pair === "XAUUSD" ? 2 : 5;
  // Grid + price labels
  ctx.strokeStyle = "rgba(255,255,255,.05)"; ctx.lineWidth = 1;
  ctx.font = "9px JetBrains Mono"; ctx.fillStyle = "rgba(255,255,255,.4)";
  for(let i=0;i<=3;i++){
    const y = padT + (i/3)*plotH;
    const p = maxP - (i/3)*(maxP-minP);
    ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke();
    ctx.fillText(p.toFixed(dec), W-padR+3, y+3);
  }
  // SL line (red)
  const ySL = yScale(trade.sl);
  ctx.strokeStyle = "rgba(248,113,113,.7)"; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(padL,ySL); ctx.lineTo(W-padR,ySL); ctx.stroke();
  ctx.fillStyle = "rgba(248,113,113,.9)"; ctx.fillRect(W-padR+1, ySL-7, padR-1, 14);
  ctx.fillStyle = "#fff"; ctx.fillText("SL "+trade.sl.toFixed(dec), W-padR+3, ySL+3);
  // TP line (green)
  const yTP = yScale(trade.tp);
  ctx.strokeStyle = "rgba(52,211,153,.7)";
  ctx.beginPath(); ctx.moveTo(padL,yTP); ctx.lineTo(W-padR,yTP); ctx.stroke();
  ctx.fillStyle = "rgba(52,211,153,.9)"; ctx.fillRect(W-padR+1, yTP-7, padR-1, 14);
  ctx.fillStyle = "#fff"; ctx.fillText("TP "+trade.tp.toFixed(dec), W-padR+3, yTP+3);
  ctx.setLineDash([]);
  // Entry line (white)
  const yEN = yScale(trade.entry);
  ctx.strokeStyle = "rgba(255,255,255,.6)"; ctx.lineWidth = 1; ctx.setLineDash([2,3]);
  ctx.beginPath(); ctx.moveTo(padL,yEN); ctx.lineTo(W-padR,yEN); ctx.stroke();
  ctx.setLineDash([]);
  // Candles
  candles.forEach((c,i) => {
    const x = padL + i*cs + cs/2;
    const yO = yScale(c.o), yC = yScale(c.c);
    const yH = yScale(c.h), yL = yScale(c.l);
    const color = c.c >= c.o ? "rgba(52,211,153,.85)" : "rgba(248,113,113,.85)";
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
    ctx.fillStyle = color;
    const bodyTop = Math.min(yO,yC), bodyH = Math.max(1, Math.abs(yC-yO));
    ctx.fillRect(x-cw/2, bodyTop, cw, bodyH);
  });
  // Entry marker — vertical line + arrow on target bar
  if(targetIdx >= 0 && targetIdx < candles.length){
    const xT = padL + targetIdx*cs + cs/2;
    ctx.strokeStyle = "rgba(255,255,255,.4)"; ctx.lineWidth = 1; ctx.setLineDash([2,2]);
    ctx.beginPath(); ctx.moveTo(xT, padT); ctx.lineTo(xT, padT+plotH); ctx.stroke();
    ctx.setLineDash([]);
    // Arrow above bar
    const arrowY = padT + 8;
    ctx.fillStyle = trade.direction === "LONG" ? "rgba(52,211,153,1)" : "rgba(248,113,113,1)";
    ctx.beginPath();
    if(trade.direction === "LONG"){
      ctx.moveTo(xT, arrowY-3); ctx.lineTo(xT-3, arrowY+2); ctx.lineTo(xT+3, arrowY+2);
    } else {
      ctx.moveTo(xT, arrowY+3); ctx.lineTo(xT-3, arrowY-2); ctx.lineTo(xT+3, arrowY-2);
    }
    ctx.closePath(); ctx.fill();
  }
}

async function refreshLogs(){
  try{
    const r = await fetch("/api/logs");
    const d = await r.json();
    document.getElementById("logArea").innerHTML = (d.logs||[]).slice(-80).reverse().map(l => `<div class="log-line log-${l.level}">[${(l.ts||"").slice(11,19)}] [${l.level}] ${l.msg}</div>`).join("");
  }catch(e){}
}

// ── Mobile chart ──
let chartState = { pair: "EURUSD", tf: "15M", candles: [] };

async function loadChartData(){
  try{
    const r = await fetch(`/api/candles?pair=${chartState.pair}&tf=${chartState.tf}&limit=150`);
    const d = await r.json();
    if(d.ok && d.candles){
      chartState.candles = d.candles;
      document.getElementById("chartCandles").textContent = d.candles.length;
      if(d.candles.length > 0){
        document.getElementById("chartLivePrice").textContent = d.candles[d.candles.length-1].c.toFixed(chartState.pair==="XAUUSD"?2:5);
      }
      renderChart();
    }
  }catch(e){}
}

function renderChart(){
  const canvas = document.getElementById("mobChart");
  if(!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0,0,W,H);
  const candles = chartState.candles; if(!candles.length) return;
  let minP=Infinity, maxP=-Infinity;
  candles.forEach(c => { if(c.l<minP)minP=c.l; if(c.h>maxP)maxP=c.h; });
  const pad = (maxP-minP)*0.05; minP -= pad; maxP += pad;
  const padL=4, padR=55, padT=4, padB=4;
  const plotW = W-padL-padR, plotH = H-padT-padB;
  const cw = Math.max(1, plotW/candles.length*0.7);
  const cs = plotW/candles.length;
  const yScale = p => padT + (1-(p-minP)/(maxP-minP))*plotH;
  ctx.strokeStyle = "rgba(255,255,255,.05)"; ctx.lineWidth = 1;
  ctx.font = "9px JetBrains Mono"; ctx.fillStyle = "rgba(255,255,255,.4)";
  for(let i=0;i<=3;i++){
    const y = padT + (i/3)*plotH;
    const p = maxP - (i/3)*(maxP-minP);
    ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke();
    ctx.fillText(p.toFixed(chartState.pair==="XAUUSD"?2:5), W-padR+3, y+3);
  }
  candles.forEach((c,i) => {
    const x = padL + i*cs + cs/2;
    const yO = yScale(c.o), yC = yScale(c.c);
    const yH = yScale(c.h), yL = yScale(c.l);
    const color = c.c >= c.o ? "rgba(52,211,153,.85)" : "rgba(248,113,113,.85)";
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
    ctx.fillStyle = color;
    const bodyTop = Math.min(yO,yC), bodyH = Math.max(1, Math.abs(yC-yO));
    ctx.fillRect(x-cw/2, bodyTop, cw, bodyH);
  });
  const latest = candles[candles.length-1];
  const yL = yScale(latest.c);
  ctx.strokeStyle = "rgba(255,255,255,.6)"; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(padL,yL); ctx.lineTo(W-padR,yL); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(255,255,255,.15)";
  ctx.fillRect(W-padR+1, yL-7, padR-1, 14);
  ctx.fillStyle = "#ffffff";
  ctx.fillText(latest.c.toFixed(chartState.pair==="XAUUSD"?2:5), W-padR+3, yL+3);
}

function switchChartPair(pair){
  chartState.pair = pair;
  document.querySelectorAll(".chart-btn[data-pair]").forEach(b => b.classList.toggle("active", b.dataset.pair===pair));
  document.getElementById("chartPairLabel").textContent = pair;
  loadChartData();
}
function switchChartTF(tf){
  chartState.tf = tf;
  document.querySelectorAll(".chart-btn[data-tf]").forEach(b => b.classList.toggle("active", b.dataset.tf===tf));
  loadChartData();
}

// Save config on any input blur (auto-persist)
document.addEventListener("blur", (e) => {
  if(e.target.matches("#page-config input, #page-config select")){
    fetch("/api/engine/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(getBodyConfig())});
  }
}, true);

// init
refreshStatus();
loadPresets();
setInterval(refreshStatus, 5000);
setInterval(()=>{ if(S.currentTab==="logs") refreshLogs(); }, 10000);
setInterval(()=>{ if(S.currentTab==="live") loadChartData(); }, 30000);
loadChartData();
window.addEventListener("resize", () => renderChart());
(function(){
  const end = new Date();
  const start = new Date(); start.setDate(start.getDate()-30);
  document.getElementById("btStart").value = start.toISOString().slice(0,10);
  document.getElementById("btEnd").value = end.toISOString().slice(0,10);
})();
</script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log("INFO", "="*60)
    log("INFO", f"GAMAN-X starting on port {PORT}")
    log("INFO", f"Dashboard: http://localhost:{PORT}")
    log("INFO", "="*60)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
