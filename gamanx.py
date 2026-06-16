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

    def detect_signal(self, df, pair):
        """Scan laatste candle voor Silver Bullet setup.
        Returns: signal dict of None.
        """
        if df is None or len(df) < self.sweep_lookback + 5:
            return None

        # Check current bar is in killzone (NY time)
        latest_ny = df.index[-1].astimezone(TZ_NY)
        kz_name = self.in_killzone(latest_ny)
        if kz_name is None:
            return None

        # Get ATR for displacement check
        cur_atr = atr(df, 14)
        if cur_atr <= 0:
            return None

        # Look at last few bars for displacement + sweep + FVG
        # Scan bars i = len-3..len-1 (skip very last for FVG 3-candle pattern)
        n = len(df)
        for i in range(n-4, n-1):
            # Displacement check: this bar's range > 1.5x ATR
            bar_range = df["high"].iloc[i] - df["low"].iloc[i]
            if bar_range < self.displacement_atr_mult * cur_atr:
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
                # Long setup: prior was sweep of low (took out lows), now reversal up
                # Need: low[i] < lookback_low AND close[i] > lookback_low
                if df["low"].iloc[i] < lookback_low and close > lookback_low:
                    swept = True
                    sl_level = df["low"].iloc[i]  # below the sweep
            else:
                # Short setup: sweep of high
                if df["high"].iloc[i] > lookback_high and close < lookback_high:
                    swept = True
                    sl_level = df["high"].iloc[i]

            if not swept:
                continue

            # FVG check: bar i+1 should have created an FVG
            fvg = detect_fvg(df, i, disp_dir)
            if fvg is None:
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
                continue

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

        return None


# ════════════════════════════════════════════════════════════
# PLACEHOLDER DETECTORS — CHoCH, BOS, Asian Breakout
# ════════════════════════════════════════════════════════════

class CHoCHDetector:
    """Placeholder — niet geïmplementeerd in deze sessie."""
    def __init__(self, **kwargs): pass
    def detect_signal(self, df, pair): return None


class BOSDetector:
    """Placeholder — niet geïmplementeerd in deze sessie."""
    def __init__(self, **kwargs): pass
    def detect_signal(self, df, pair): return None


class AsianBreakoutDetector:
    """Placeholder — niet geïmplementeerd in deze sessie."""
    def __init__(self, **kwargs): pass
    def detect_signal(self, df, pair): return None


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
            "tf": "15M",
            "capital": 10000,
            "lot_eur": 10,                   # micro lots
            "lot_xau": 1,
            "rr": 2.0,
            "discord_webhook": "",
            "strategies": {
                "SB":  {"enabled": True,  "killzones": ["london","ny_am","ny_pm"], "disp_atr": 1.5},
                "CH":  {"enabled": False, "swing_length": 5},
                "BOS": {"enabled": False, "swing_length": 5},
                "AB":  {"enabled": False, "min_range_pips_eur": 20, "max_range_pips_eur": 80},
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
        tf = self.config["tf"]

        # Monitor open trades first (close at SL/TP)
        self._monitor_open_trades()

        # Check open trades cap
        if len(self.open_trades) >= self.config.get("max_open_trades", 4):
            return

        # Iterate strategies
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

            # Scan each pair
            for pair in pairs:
                try:
                    df = fetch_candles(pair, tf)
                    if df is None or len(df) < 30:
                        continue
                    signal = detector.detect_signal(df, pair)
                    if signal:
                        # Dedup check — don't open if same strategy+pair recently
                        if self._has_recent_trade(sid, pair, minutes=30):
                            continue
                        self._open_trade(signal)
                except Exception as e:
                    log("WARN", f"Scan {sid} {pair}: {e}")

    def _has_recent_trade(self, sid, pair, minutes=30):
        cutoff = (now_brussels() - timedelta(minutes=minutes)).timestamp()
        for t in self.open_trades + self.closed_trades:
            if t.get("strategy") != sid or t.get("pair") != pair:
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
            "direction":   signal["direction"],
            "entry":       signal["entry"],
            "sl":          signal["sl"],
            "tp":          signal["tp"],
            "lotsize":     lot,
            "opened_at":   fmt_brussels(),
            "opened_ts":   int(now_brussels().timestamp()),
            "meta":        {k: v for k, v in signal.items() if k not in ("strategy","pair","direction","entry","sl","tp")},
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
# BACKTESTER — Silver Bullet
# ════════════════════════════════════════════════════════════

def backtest_silver_bullet(pair, tf, start_date, end_date, capital=10000,
                           lot=10, rr=2.0, killzones=None, disp_atr=1.5):
    """Run Silver Bullet backtest over historical period.
    Returns: dict met trades, stats.
    """
    log("INFO", f"Backtest SB {pair} {tf} {start_date} -> {end_date}")

    df = fetch_candles(pair, tf, start=start_date, end=end_date)
    if df is None or len(df) < 50:
        return {"error": "No data for this period"}

    detector = SilverBulletDetector(
        enabled_killzones=killzones,
        displacement_atr_mult=disp_atr,
        rr=rr,
    )

    pip_size = PIP.get(pair, 0.0001)
    pip_value = PIP_EUR.get(pair, 0.10)
    trades = []
    n = len(df)

    # Simulate bar-by-bar
    for i in range(50, n - 1):
        window = df.iloc[:i+1]  # data up to current bar
        signal = detector.detect_signal(window, pair)
        if signal is None:
            continue

        # Check dedup: skip if last trade closed recently
        if trades:
            last_close_idx = trades[-1].get("close_idx", -1)
            if last_close_idx >= i - 2:
                continue

        # Simulate forward to find exit
        entry = signal["entry"]
        sl = signal["sl"]
        tp = signal["tp"]
        direction = signal["direction"]

        exit_idx = None
        exit_price = None
        hit = None
        for j in range(i+1, min(i+50, n)):  # max 50 bars forward
            h = df["high"].iloc[j]
            l = df["low"].iloc[j]
            if direction == "LONG":
                if l <= sl:
                    exit_idx = j; exit_price = sl; hit = "SL"; break
                if h >= tp:
                    exit_idx = j; exit_price = tp; hit = "TP"; break
            else:
                if h >= sl:
                    exit_idx = j; exit_price = sl; hit = "SL"; break
                if l <= tp:
                    exit_idx = j; exit_price = tp; hit = "TP"; break

        if exit_idx is None:
            # Force close at end of window
            exit_idx = min(i+50, n-1)
            exit_price = float(df["close"].iloc[exit_idx])
            hit = "TIMEOUT"

        # Compute P&L
        if direction == "LONG":
            pips = (exit_price - entry) / pip_size
        else:
            pips = (entry - exit_price) / pip_size
        pnl = pips * pip_value * lot

        trades.append({
            "ts":         str(df.index[i]),
            "exit_ts":    str(df.index[exit_idx]),
            "pair":       pair,
            "direction":  direction,
            "entry":      round(entry, 5),
            "exit":       round(exit_price, 5),
            "sl":         round(sl, 5),
            "tp":         round(tp, 5),
            "pips":       round(pips, 1),
            "pnl":        round(pnl, 2),
            "hit":        hit,
            "killzone":   signal.get("killzone"),
            "close_idx":  exit_idx,
        })

    # Stats
    if not trades:
        return {"trades": [], "stats": {"total": 0}, "msg": "No setups found in this period"}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    pf = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses else float("inf")

    # Max drawdown (peak-to-trough on equity curve)
    equity = capital
    peak = capital
    max_dd = 0
    for t in trades:
        equity += t["pnl"]
        if equity > peak: peak = equity
        dd = equity - peak
        if dd < max_dd: max_dd = dd

    stats = {
        "total":   len(trades),
        "wins":    len(wins),
        "losses":  len(losses),
        "wr":      round(100 * len(wins) / len(trades), 1),
        "pnl":     round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pf":      round(pf, 2) if pf != float("inf") else 999,
        "max_dd":  round(max_dd, 2),
    }
    return {"trades": trades, "stats": stats}


# ════════════════════════════════════════════════════════════
# FLASK APP
# ════════════════════════════════════════════════════════════

app = Flask(__name__)
engine = GamanXEngine()

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/status")
def api_status():
    return jsonify({"ok": True, **engine.get_status()})

@app.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    body = request.json or {}
    # Update config from request
    cfg = engine.config
    cfg["pair"] = body.get("pair", cfg["pair"])
    cfg["tf"]   = body.get("tf",   cfg["tf"])
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
    # Allow partial updates
    for k, v in body.items():
        if k == "strategies" and isinstance(v, dict):
            for sid, sc in v.items():
                if sid in cfg["strategies"]:
                    cfg["strategies"][sid].update(sc)
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

    if strategy != "SB":
        return jsonify({"error": f"Strategy {strategy} not yet implemented"}), 400

    killzones = body.get("killzones", ["london","ny_am","ny_pm"])
    disp_atr = float(body.get("disp_atr", 1.5))

    result = backtest_silver_bullet(
        pair=pair, tf=tf,
        start_date=start, end_date=end,
        capital=capital, lot=lot, rr=rr,
        killzones=killzones, disp_atr=disp_atr,
    )
    return jsonify(result)

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
.grid{display:grid;grid-template-columns:1fr 360px;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
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
      <label>Timeframe</label>
      <select id="cfgTF"><option value="15M" selected>15M</option><option value="1H">1H</option><option value="4H">4H</option></select>
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

</div>
</div>
</div>

<!-- BACKTEST TAB -->
<div id="tab-backtest" style="display:none">
<div class="card">
  <div class="card-title">Backtester</div>
  <div class="form-row">
    <div class="form-group">
      <label>Strategy</label>
      <select id="btStrat">
        <option value="SB">Silver Bullet</option>
        <option value="CH" disabled>CHoCH (coming soon)</option>
        <option value="BOS" disabled>BOS (coming soon)</option>
        <option value="AB" disabled>Asian Breakout (coming soon)</option>
      </select>
    </div>
    <div class="form-group"><label>Pair</label><select id="btPair"><option>EURUSD</option><option>XAUUSD</option></select></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Timeframe</label><select id="btTF"><option value="15M" selected>15M</option><option value="1H">1H</option><option value="4H">4H</option></select></div>
    <div class="form-group"><label>Lot (micro)</label><input type="number" id="btLot" value="10" min="1"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>Start date</label><input type="date" id="btStart"></div>
    <div class="form-group"><label>End date</label><input type="date" id="btEnd"></div>
  </div>
  <div class="form-row">
    <div class="form-group"><label>RR</label><input type="number" id="btRR" value="2" step="0.5"></div>
    <div class="form-group"><label>Displacement ATR</label><input type="number" id="btDispAtr" value="1.5" step="0.1"></div>
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
  {id:"CH",  name:"CHoCH Reversal",    sub:"Structure trend reversal", placeholder:true},
  {id:"BOS", name:"BOS Continuation",  sub:"Trend continuation", placeholder:true},
  {id:"AB",  name:"Asian Breakout",    sub:"Session range breakout", placeholder:true},
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
  document.getElementById("openPos").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>Strat</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Live</th><th>SL</th><th>TP</th><th>P&L</th><th></th></tr></thead>
      <tbody>${trades.map(t => `
        <tr>
          <td>${t.id}</td>
          <td>${t.strategy}</td>
          <td>${t.pair}</td>
          <td class="dir-${t.direction}">${t.direction}</td>
          <td>${t.entry.toFixed(5)}</td>
          <td>${(t.live_price||0).toFixed(5)}</td>
          <td>${t.sl.toFixed(5)}</td>
          <td>${t.tp.toFixed(5)}</td>
          <td class="${(t.pnl_eur||0)>=0?'pnl-pos':'pnl-neg'}">€${(t.pnl_eur||0).toFixed(2)}</td>
          <td><button class="btn danger" style="padding:3px 8px;font-size:10px" onclick="closeTrade('${t.id}')">✕</button></td>
        </tr>
      `).join("")}</tbody>
    </table>
  `;
}

function renderClosed(){
  const trades = ((S.status && S.status.closed_trades) || []).slice(-20).reverse();
  if(!trades.length){
    document.getElementById("closedTrades").innerHTML = '<div class="placeholder-msg">No closed trades yet</div>';
    return;
  }
  document.getElementById("closedTrades").innerHTML = `
    <table>
      <thead><tr><th>Time</th><th>ID</th><th>Strat</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Hit</th><th>Pips</th><th>P&L</th></tr></thead>
      <tbody>${trades.map(t => `
        <tr>
          <td>${(t.closed_at||"").slice(11,19)}</td>
          <td>${t.id}</td>
          <td>${t.strategy}</td>
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
  `;
}

function renderConfig(){
  if(!S.config) return;
  document.getElementById("cfgPair").value = S.config.pair;
  document.getElementById("cfgTF").value = S.config.tf;
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
    tf: document.getElementById("cfgTF").value,
    lot_eur: parseInt(document.getElementById("cfgLotEur").value)||10,
    lot_xau: parseInt(document.getElementById("cfgLotXau").value)||1,
    rr: parseFloat(document.getElementById("cfgRR").value)||2.0,
    capital: parseFloat(document.getElementById("cfgCap").value)||10000,
    discord_webhook: document.getElementById("cfgDiscord").value.trim(),
    strategies: {
      SB: {killzones, disp_atr: parseFloat(document.getElementById("sbDispAtr").value)||1.5},
    },
  };
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

async function runBacktest(){
  const btn = document.getElementById("btRunBtn");
  btn.disabled = true; btn.textContent = "Running...";
  const killzones = [...document.querySelectorAll(".killzone-chip.active")].map(c => c.dataset.kz);
  const body = {
    strategy: document.getElementById("btStrat").value,
    pair: document.getElementById("btPair").value,
    tf: document.getElementById("btTF").value,
    start: document.getElementById("btStart").value,
    end: document.getElementById("btEnd").value,
    lot: parseInt(document.getElementById("btLot").value)||10,
    rr: parseFloat(document.getElementById("btRR").value)||2,
    disp_atr: parseFloat(document.getElementById("btDispAtr").value)||1.5,
    killzones,
  };
  try{
    const r = await fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const d = await r.json();
    renderBacktestResult(d);
  }catch(e){
    document.getElementById("btResult").innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
  }finally{
    btn.disabled = false; btn.textContent = "▶ RUN BACKTEST";
  }
}

function renderBacktestResult(d){
  const el = document.getElementById("btResult");
  if(d.error){ el.innerHTML = `<div style="color:var(--red);padding:12px;background:rgba(248,113,113,.05);border-radius:6px">${d.error}</div>`; return; }
  if(d.msg){ el.innerHTML = `<div style="color:var(--text3);padding:12px">${d.msg}</div>`; return; }
  const st = d.stats;
  const trades = d.trades || [];
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px">
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">TRADES</div><div style="font-size:18px;font-weight:700">${st.total}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">WIN RATE</div><div style="font-size:18px;font-weight:700">${st.wr}%</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">P&L</div><div style="font-size:18px;font-weight:700" class="${st.pnl>=0?'pnl-pos':'pnl-neg'}">€${st.pnl}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">PF</div><div style="font-size:18px;font-weight:700">${st.pf}</div></div>
      <div style="padding:10px;background:rgba(255,255,255,.03);border-radius:6px;text-align:center"><div style="font-size:10px;color:var(--text3)">MAX DD</div><div style="font-size:18px;font-weight:700" class="pnl-neg">€${st.max_dd}</div></div>
    </div>
    <table>
      <thead><tr><th>Time</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Hit</th><th>Pips</th><th>P&L</th><th>KZ</th></tr></thead>
      <tbody>${trades.slice(-30).reverse().map(t => `
        <tr>
          <td>${t.ts.slice(0,16)}</td><td>${t.pair}</td>
          <td class="dir-${t.direction}">${t.direction}</td>
          <td>${t.entry}</td><td>${t.exit}</td><td>${t.hit}</td>
          <td>${t.pips}</td>
          <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">€${t.pnl}</td>
          <td>${t.killzone||"-"}</td>
        </tr>
      `).join("")}</tbody>
    </table>
  `;
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
setInterval(refreshStatus, 5000);
setInterval(()=>{ if(S.currentTab==="logs") refreshLogs(); }, 10000);
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
# MAIN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log("INFO", "="*60)
    log("INFO", f"GAMAN-X starting on port {PORT}")
    log("INFO", f"Dashboard: http://localhost:{PORT}")
    log("INFO", "="*60)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
