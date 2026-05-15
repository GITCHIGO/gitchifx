"""
GITCHI TRADING DASHBOARD v3
============================
Start: python gitchi.py
Open:  http://localhost:5000
Vereisten: pip install flask yfinance pandas
"""
from flask import Flask, jsonify, request, Response
import json, math, datetime, threading, time

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    raise SystemExit("Installeer eerst: pip install flask yfinance pandas")

app = Flask(__name__)

STATE_FILE   = "gitchi_state.json"
PRESETS_FILE = "gitchi_presets.json"

# ─── DISCORD NOTIFICATIES ─────────────────────────────────────────────────────
def send_discord(webhook_url, message, color=0x7c3aed):
    """Stuur een embed bericht naar Discord via webhook."""
    if not webhook_url:
        return
    try:
        import requests
        payload = {
            "embeds": [{
                "description": message,
                "color": color,
                "footer": {"text": f"GAMAN Trading · {fmt_time_brussels()}"}
            }]
        }
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"[DISCORD] Fout: {e}")

# ─── LIVE TRADING ENGINE STATE ───────────────────────────────────────────────
class LiveEngine:
    def __init__(self):
        self.running        = False
        self.thread         = None
        self.config         = {}
        self.open_trades    = []
        self.closed_trades  = []
        self.logs           = []
        self.scan_count     = 0
        self.last_scan      = None
        self.start_time     = None   # tijdstip engine gestart (Brussels, als string)
        self.start_ts       = None   # unix timestamp voor uptime berekening
        self.paused         = False  # pauze zonder config te verliezen
        self.daily_pnl      = 0.0   # P&L vandaag
        self.daily_reset    = None  # datum van laatste reset
        self.stopped_by_risk= False # gestopt door risicobeheer
        self.lock           = threading.Lock()
        self.recent_entries = {}  # pair+tf -> timestamp van laatste entry
        self._load_state()

    def _discord(self, msg, color=0x7c3aed):
        webhook = self.config.get("discord_webhook","") or "https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ"
        if webhook:
            threading.Thread(target=send_discord, args=(webhook, msg, color), daemon=True).start()

    def _reset_daily_pnl_if_needed(self):
        today = now_brussels().date()
        if self.daily_reset != today:
            self.daily_pnl   = 0.0
            self.daily_reset = today
            self.stopped_by_risk = False

    def _check_daily_loss_limit(self):
        max_daily = float(self.config.get("max_daily_loss", 0))
        if max_daily <= 0:
            return False
        if self.daily_pnl <= -max_daily:
            self.log("RISK", f"⛔ Dagelijks verlies limiet bereikt: €{self.daily_pnl:.2f} / -€{max_daily:.2f}")
            self._discord(f"⛔ **Dagelijks verlies limiet bereikt**\nVerlies: €{self.daily_pnl:.2f}\nLimiet: -€{max_daily:.2f}\nEngine gestopt.", 0xff0000)
            self.stopped_by_risk = True
            self.running = False
            self._save_state()
            return True
        return False

    def _save_state(self):
        try:
            with self.lock:
                state = {
                    "config":         self.config,
                    "open_trades":    self.open_trades,
                    "closed_trades":  self.closed_trades,
                    "logs":           self.logs[-500:],
                    "scan_count":     self.scan_count,
                    "daily_pnl":      self.daily_pnl,
                    "daily_reset":    str(self.daily_reset) if self.daily_reset else None,
                    "recent_entries": self.recent_entries,
                    "saved_at":       fmt_brussels(),
                }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[STATE] Save fout: {e}")

    def _load_state(self):
        try:
            import os
            if not os.path.exists(STATE_FILE):
                return
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.config         = state.get("config", {})
            self.open_trades    = state.get("open_trades", [])
            self.closed_trades  = state.get("closed_trades", [])
            self.logs           = state.get("logs", [])
            self.scan_count     = state.get("scan_count", 0)
            self.daily_pnl      = state.get("daily_pnl", 0.0)
            self.recent_entries = state.get("recent_entries", {})
            # Herstel daily_reset datum
            dr = state.get("daily_reset")
            if dr:
                try:
                    import datetime as _dt
                    self.daily_reset = _dt.date.fromisoformat(dr)
                except: pass
            saved_at = state.get("saved_at", "?")
            self.log("START", f"State hersteld van {saved_at} | {len(self.open_trades)} open, {len(self.closed_trades)} gesloten trades")
            print(f"[STATE] Hersteld: {len(self.open_trades)} open trades, {len(self.closed_trades)} gesloten trades")
        except Exception as e:
            print(f"[STATE] Load fout: {e}")

    def log(self, level, msg):
        entry = {
            "time":  fmt_time_brussels(),
            "level": level,
            "msg":   msg
        }
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def start(self, config):
        if self.running:
            return False
        self.config  = config
        self.running = True
        self.paused  = False
        self.start_time = fmt_time_brussels()
        self.start_ts   = time.time()
        self.thread  = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        pairs = "EURUSD + XAUUSD" if config.get("trade_both") else config.get("pair","?")
        self.log("START", f"Engine gestart — {pairs} {config.get('tf')} | Score≥{config.get('min_score')}")
        self._discord(
            f"🚀 **GAMAN Engine Gestart**\n"
            f"Pair: **{pairs}** | TF: **{config.get('tf')}**\n"
            f"Min Score: {config.get('min_score')}\n"
            f"Max dagelijks verlies: €{config.get('max_daily_loss',0)} | Max trades: {config.get('max_trades',0)}",
            0x7c3aed
        )
        self._save_state()
        return True

    def stop(self):
        self.running = False
        self.paused  = False
        self.log("STOP", "Engine gestopt door gebruiker")
        self._discord("🔴 **GAMAN Engine Gestopt** door gebruiker", 0xf59e0b)
        self._save_state()

    def pause(self):
        if not self.running: return False
        self.paused = True
        self.log("PAUSE", "⏸ Engine gepauzeerd — geen nieuwe trades")
        self._discord("⏸ **GAMAN Engine Gepauzeerd** — bestaande trades blijven open", 0xf59e0b)
        return True

    def resume(self):
        if not self.running: return False
        self.paused = False
        self.log("RESUME", "▶ Engine hervat — scans actief")
        self._discord("▶ **GAMAN Engine Hervat** — scans actief", 0x22c55e)
        return True

    def _is_weekend(self):
        """
        Markt gesloten in Brusselse tijd:
        - Vrijdag 23:00 → Zondag 23:00 Brussels
        - Elke weekdag (ma-do) 23:00 → 00:00 Brussels (dagelijkse gap)
        """
        now_b = now_brussels()
        wd    = now_b.weekday()  # 0=Ma, 4=Vr, 5=Za, 6=Zo
        h     = now_b.hour
        m     = now_b.minute

        # Zaterdag: altijd gesloten
        if wd == 5: return True

        # Vrijdag na 23:00
        if wd == 4 and h >= 23: return True

        # Zondag voor 23:00
        if wd == 6 and h < 23: return True

        # Weekdagen (ma-do) 23:00-00:00 dagelijkse gap
        if wd in [0,1,2,3] and h == 23: return True

        return False

    def _run(self):
        while self.running:
            if self.paused:
                time.sleep(1)
                continue
            try:
                if self._is_weekend():
                    self.log("INFO", "Weekend — markt gesloten, wacht...")
                    time.sleep(60)
                    continue

                # Reset dagelijks P&L als nieuwe dag
                self._reset_daily_pnl_if_needed()

                # Check dagelijks verlies limiet
                if self._check_daily_loss_limit():
                    break

                self.scan_count += 1
                self.last_scan   = fmt_time_brussels()

                # Bepaal pairs en timeframes
                max_trades = int(self.config.get("max_trades", 0))
                tf_map = {"15M+1H":["15M","1H"],"1H+4H":["1H","4H"],"ALL":["15M","1H","4H"]}
                cfg_pair = self.config.get("pair","EURUSD")
                cfg_tf   = self.config.get("tf","1H")
                pairs = ["EURUSD","XAUUSD"] if cfg_pair=="BOTH" or self.config.get("trade_both") else [cfg_pair]
                tfs   = tf_map.get(cfg_tf, [cfg_tf])

                for pair in pairs:
                    for tf in tfs:
                        with self.lock:
                            n_open = len(self.open_trades)
                        if max_trades > 0 and n_open >= max_trades:
                            break
                        self._scan(pair_override=pair, tf_override=tf)

                self._monitor_open_trades()

                if self.scan_count % 5 == 0:
                    self._save_state()

            except Exception as e:
                self.log("ERROR", f"Engine fout: {e}")

            time.sleep(20)

    def _scan(self, pair_override=None, tf_override=None):
        """Scan voor nieuwe setups."""
        cfg    = self.config
        pair   = pair_override or cfg.get("pair","EURUSD")
        if pair == "BOTH": pair = "EURUSD"  # fallback
        tf     = tf_override or cfg.get("tf","1H")
        # Resolve multi-TF to single TF voor live scanning
        if "+" in tf or tf == "ALL": tf = "1H"
        min_sc = int(cfg.get("min_score",2))
        use_ob = bool(cfg.get("use_ob",True))
        use_tr = bool(cfg.get("use_trend",False))
        use_eq = bool(cfg.get("use_eq",True))
        use_kz = bool(cfg.get("use_session",False))
        use_sw = bool(cfg.get("use_sweep",False))
        # Lotsize per pair
        if pair == "XAUUSD":
            lotsize = float(cfg.get("lotsize_xau", cfg.get("lotsize", 1)))
        else:
            lotsize = float(cfg.get("lotsize_eur", cfg.get("lotsize", 1)))

        # Geen nieuwe trade als er al één open is voor dit pair
        with self.lock:
            open_pairs = [t["pair"] for t in self.open_trades]
        if pair in open_pairs:
            return

        # Cooldown: wacht minstens 5 minuten na sluiten van een trade op dit pair+tf
        cooldown_key = f"{pair}_{tf}"
        cooldown_secs = 300  # 5 minuten
        last_entry = self.recent_entries.get(cooldown_key, 0)
        if time.time() - last_entry < cooldown_secs:
            return

        df = fetch_candles(pair, tf)
        if df is None or len(df) < 25:
            self.log("WARN", f"Onvoldoende data voor {pair} {tf}")
            return

        bias  = calc_bias(df, pair)
        score = bias["total_score"]

        if abs(score) < min_sc:
            return

        direction = "LONG" if score >= min_sc else ("SHORT" if score <= -min_sc else None)
        if direction is None:
            return

        # Killzone filter — Brusselse tijd
        if use_kz:
            now_b = now_brussels()
            h = now_b.hour
            # London KZ: 09:00-12:00 Brussels (07:00-10:00 UTC)
            # NY KZ: 14:00-17:00 Brussels (12:00-15:00 UTC)
            in_kz = (9 <= h < 12) or (14 <= h < 17)
            if not in_kz:
                return

        eq      = bias["equilibrium"]
        highs   = df["high"].values
        lows    = df["low"].values
        n       = len(df)

        # Trend filter
        if use_tr:
            lb2   = min(30, n)
            h_arr = highs[-lb2:]
            l_arr = lows[-lb2:]
            m2    = len(h_arr)//2
            if m2 > 0:
                hh = h_arr[m2:].max() > h_arr[:m2].max()
                hl = l_arr[m2:].min() > l_arr[:m2].min()
                lh = h_arr[m2:].max() < h_arr[:m2].max()
                ll = l_arr[m2:].min() < l_arr[:m2].min()
                trend = 1 if (hh and hl) else (-1 if (lh and ll) else 0)
                if trend != 0 and trend != (1 if direction=="LONG" else -1):
                    return

        # FVG scan — met displacement check (punt 3)
        fvg = None
        for fi in range(n-1, max(n-20, 2), -1):
            f = detect_fvg(df, fi, check_displacement=True)
            if f is None: continue
            if f["type"] != ("bull" if direction=="LONG" else "bear"): continue
            # Mitigatie check
            mitigated = False
            for k in range(f["formed_at"]+1, n):
                if f["type"]=="bull" and float(df.iloc[k]["low"])  < f["bottom"]: mitigated=True; break
                if f["type"]=="bear" and float(df.iloc[k]["high"]) > f["top"]:   mitigated=True; break
            if mitigated: continue
            if use_eq:
                lb_f = min(20, fi)
                eq_f = (float(highs[fi-lb_f:fi].max()) + float(lows[fi-lb_f:fi].min())) / 2
                mid  = (f["top"] + f["bottom"]) / 2
                if direction=="LONG"  and mid >= eq_f: continue
                if direction=="SHORT" and mid <= eq_f: continue
            fvg = f
            break

        if fvg is None:
            return

        # OB scan
        ob = None
        if use_ob:
            for oi in range(fvg["formed_at"], max(fvg["formed_at"]-20, 1), -1):
                o = detect_ob(df, oi)
                if o and o["type"] == ("bull" if direction=="LONG" else "bear"):
                    ob = o; break
            if ob is None:
                return

        # Liquidity Sweep filter — vereist dat er vóór de FVG vorming een sweep was
        sweep = None
        if use_sw:
            sweep = detect_liquidity_sweep(
                df, fvg["formed_at"],
                lookback_swing=20, lookback_sweep=5,
                direction=direction
            )
            if sweep is None:
                return

        # Check of prijs al in de FVG zone zit (retrace)
        current_price = float(df.iloc[-1]["close"])
        in_fvg = False
        if direction=="LONG"  and fvg["bottom"] <= current_price <= fvg["top"]: in_fvg = True
        if direction=="SHORT" and fvg["bottom"] <= current_price <= fvg["top"]: in_fvg = True

        if not in_fvg:
            return

        # Consequent Encroachment: entry op 50% van de FVG (midpunt)
        entry = (fvg["top"] + fvg["bottom"]) / 2

        # Bouw filters string op
        filters_used = ["FVG"]
        if use_ob:  filters_used.append("OB")
        if use_eq:  filters_used.append("EQ")
        if use_kz:  filters_used.append("KZ")
        if use_tr:  filters_used.append("Trend")
        if use_sw:  filters_used.append("Sweep")
        filters_str = " + ".join(filters_used)

        trade = {
            "id":          len(self.closed_trades) + len(self.open_trades) + 1,
            "pair":        pair,
            "tf":          tf,
            "direction":   direction,
            "entry_price": round(entry, 5),
            "sl":          None,   # manueel in te stellen via dashboard
            "tp":          None,   # manueel in te stellen via dashboard
            "lotsize":     lotsize,
            "bias_score":  score,
            "filters":     filters_str,
            "opened_at":   fmt_brussels(),
            "opened_ts":   int(now_brussels().timestamp()),
            "fvg_top":     fvg["top"],
            "fvg_bottom":  fvg["bottom"],
            "pnl_eur":     0.0,
        }

        with self.lock:
            self.open_trades.append(trade)
            self.recent_entries[f"{pair}_{tf}"] = time.time()

        self.log("TRADE", f"▲ OPEN {direction} {pair} @ {entry:.5f} | Score:{score} | {filters_str} | SL/TP: manueel instellen")
        dir_emoji = "📈" if direction == "LONG" else "📉"
        self._discord(
            f"{dir_emoji} **TRADE GEOPEND — {pair}**\n"
            f"Richting: **{direction}** | TF: {tf}\n"
            f"Entry: `{entry:.5f}`\n"
            f"Bias Score: **{score:+d}** | Filters: `{filters_str}`\n"
            f"⚠️ Stel SL en TP in via het dashboard",
            0x7c3aed if direction=="LONG" else 0xf59e0b
        )
        self._save_state()

    def _monitor_open_trades(self):
        """Check of SL of TP geraakt is voor open trades.
        SL/TP worden alleen gecheckt als ze manueel zijn ingesteld via het dashboard."""
        if not self.open_trades:
            return

        with self.lock:
            trades_to_check = list(self.open_trades)

        for trade in trades_to_check:
            pair   = trade["pair"]
            price  = fetch_price(pair)
            if not price:
                continue

            pip_v     = PIP.get(pair, 0.0001)
            pip_e     = PIP_EUR.get(pair, 0.10)
            lot       = trade["lotsize"]
            entry     = trade["entry_price"]
            sl        = trade.get("sl")    # None als nog niet ingesteld
            tp        = trade.get("tp")    # None als nog niet ingesteld
            direction = trade["direction"]

            # Bereken live P&L
            if direction == "LONG":
                pips = (price - entry) / pip_v
            else:
                pips = (entry - price) / pip_v
            pnl = round(pips * pip_e * lot, 2)

            # Update live prijs en P&L
            with self.lock:
                for t in self.open_trades:
                    if t["id"] == trade["id"]:
                        t["pnl_eur"]    = pnl
                        t["live_price"] = round(price, 5)

            # Check SL/TP — alleen als manueel ingesteld
            if sl is None and tp is None:
                continue

            hit = None
            exit_price = None
            if direction == "LONG":
                if sl is not None and price <= sl:   hit="SL"; exit_price=sl
                elif tp is not None and price >= tp: hit="TP"; exit_price=tp
            else:
                if sl is not None and price >= sl:   hit="SL"; exit_price=sl
                elif tp is not None and price <= tp: hit="TP"; exit_price=tp

            if hit:
                final_pips = round(((exit_price-entry) if direction=="LONG" else (entry-exit_price)) / pip_v, 1)
                final_pnl  = round(final_pips * pip_e * lot, 2)
                closed = {
                    **trade,
                    "exit_price":  round(exit_price, 5),
                    "closed_at":   fmt_brussels(),
                    "closed_ts":   int(now_brussels().timestamp()),
                    "pips":        final_pips,
                    "pnl_eur":     final_pnl,
                    "outcome":     "win" if final_pnl >= 0 else "loss",
                }
                with self.lock:
                    self.open_trades   = [t for t in self.open_trades if t["id"] != trade["id"]]
                    self.closed_trades.append(closed)

                icon = "✓" if hit=="TP" else "✗"
                self.log("TRADE", f"{icon} CLOSE {direction} {pair} @ {exit_price:.5f} | {hit} | {final_pips:+.1f} pips | €{final_pnl:+.2f}")

                # Update dagelijks P&L
                with self.lock:
                    self.daily_pnl += final_pnl

                # Discord notificatie
                result_emoji = "✅" if final_pnl >= 0 else "❌"
                hit_emoji    = "🎯" if hit=="TP" else "🛡️"
                color = 0x34d399 if final_pnl >= 0 else 0xf87171
                self._discord(
                    f"{result_emoji} **TRADE GESLOTEN — {hit_emoji} {hit}**\n"
                    f"Pair: **{pair}** | Richting: **{direction}**\n"
                    f"Entry: `{trade['entry_price']:.5f}` → Exit: `{exit_price:.5f}`\n"
                    f"Pips: `{final_pips:+.1f}` | P&L: **€{final_pnl:+.2f}**\n"
                    f"Dagelijks P&L: €{self.daily_pnl:+.2f}",
                    color
                )
                self._save_state()

engine = LiveEngine()

SYMBOLS   = {"EURUSD":"EURUSD=X","XAUUSD":"GC=F"}
TF_YF     = {"15M":"15m","1H":"1h","4H":"1h"}
TF_PERIOD = {"15M":"7d","1H":"730d","4H":"730d"}
PIP       = {"EURUSD":0.0001,"XAUUSD":0.10}
PIP_EUR   = {"EURUSD":0.10,"XAUUSD":0.92}

# ─── BRUSSELSE TIJD HELPER ───────────────────────────────────────────
import pytz as _pytz
BRUSSELS_TZ = _pytz.timezone("Europe/Brussels")
UTC_TZ      = _pytz.utc

def now_brussels():
    """Huidige tijd in Brussel — altijd via UTC als basis (VPS-safe)."""
    utc_now = datetime.datetime.now(UTC_TZ)
    return utc_now.astimezone(BRUSSELS_TZ)

def fmt_brussels(dt=None):
    """Format datetime als brusselse tijd string."""
    if dt is None:
        dt = now_brussels()
    elif dt.tzinfo is None:
        dt = UTC_TZ.localize(dt).astimezone(BRUSSELS_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def fmt_time_brussels(dt=None):
    if dt is None:
        dt = now_brussels()
    elif dt.tzinfo is None:
        dt = UTC_TZ.localize(dt).astimezone(BRUSSELS_TZ)
    return dt.strftime("%H:%M:%S")

# ─── TRADINGVIEW WEBSOCKET DATA FETCHER ──────────────────────────────
try:
    import websocket as _websocket
    TV_WS_AVAILABLE = True
except ImportError:
    TV_WS_AVAILABLE = False
    print("[TV] websocket-client niet geinstalleerd — gebruik: pip install websocket-client")

TV_INSTRUMENT_MAP = {
    "EURUSD": "OANDA:EURUSD",
    "XAUUSD": "OANDA:XAUUSD",
}

TV_INTERVAL_MAP = {
    "15M": "15",
    "1H":  "60",
    "4H":  "240",
    "1D":  "1D",
}

# ─── DATA SOURCE TRACKING ───────────────────────────────────────────
# Houdt bij welke bron de laatste succesvolle fetch leverde, per pair+tf
DATA_SOURCE = {}  # bv. {"EURUSD_15M": {"source": "TV", "ts": 1234567890, "bars": 500}}
DATA_SOURCE_LOCK = threading.Lock()

def _set_data_source(pair, tf, source, bars=0):
    """Registreer welke bron de data leverde."""
    with DATA_SOURCE_LOCK:
        DATA_SOURCE[f"{pair}_{tf}"] = {
            "source": source,       # "TV" of "yFinance" of "yFinance (TV failed)"
            "ts":     int(time.time()),
            "bars":   bars,
            "time":   fmt_time_brussels(),
        }

def _rand_str(n=12):
    import string, random
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _tv_msg(func, args):
    body = json.dumps({"m": func, "p": args}, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"

def _prepend_header(msg):
    return f"~m~{len(msg)}~m~{msg}"

def fetch_ohlcv_tv(pair, tf, bars=500, timeout=8):
    """
    Haal OHLCV candles op via TradingView WebSocket.
    Geeft pandas DataFrame terug met brusselse DatetimeIndex.
    Fallback naar yFinance als TV niet beschikbaar is.
    """
    import re, threading

    if not TV_WS_AVAILABLE:
        df_yf = fetch_candles_yf(pair, tf)
        if df_yf is not None and not df_yf.empty:
            _set_data_source(pair, tf, "yFinance (TV unavailable)", len(df_yf))
        return df_yf

    instrument = TV_INSTRUMENT_MAP.get(pair, f"OANDA:{pair}")
    interval   = TV_INTERVAL_MAP.get(tf, "60")

    session_id = "qs_" + _rand_str(12)
    chart_id   = "cs_" + _rand_str(12)
    series_id  = "sds_1"

    collected = []
    done      = [False]
    error_msg = [None]

    def on_message(ws, raw):
        packets = re.split(r"~m~\d+~m~", raw)
        for pkt in packets:
            pkt = pkt.strip()
            if not pkt:
                continue
            if pkt.startswith("~h~"):
                try: ws.send(_prepend_header(pkt))
                except: pass
                continue
            try:
                data = json.loads(pkt)
            except:
                continue
            method = data.get("m", "")
            if method == "timescale_update":
                try:
                    series_data = data["p"][1].get(series_id, {})
                    bars_list   = series_data.get("s", [])
                    for bar in bars_list:
                        v = bar.get("v", [])
                        if len(v) >= 5:
                            collected.append({
                                "time":   v[0],
                                "open":   v[1],
                                "high":   v[2],
                                "low":    v[3],
                                "close":  v[4],
                                "volume": v[5] if len(v) > 5 else 0.0,
                            })
                    done[0] = True
                    try: ws.close()
                    except: pass
                except Exception as e:
                    error_msg[0] = str(e)

    def on_error(ws, err):
        error_msg[0] = str(err)
        done[0] = True

    def on_close(ws, code, reason):
        done[0] = True

    def on_open(ws):
        try:
            ws.send(_tv_msg("set_auth_token", ["unauthorized_user_token"]))
            ws.send(_tv_msg("chart_create_session", [chart_id, ""]))
            ws.send(_tv_msg("quote_create_session", [session_id]))
            ws.send(_tv_msg("quote_set_fields", [session_id, "ch", "chp", "lp"]))
            ws.send(_tv_msg("quote_add_symbols", [session_id, instrument]))
            symbol_spec = f'={{"symbol":"{instrument}","adjustment":"splits"}}'
            ws.send(_tv_msg("resolve_symbol", [chart_id, "symbol_1", symbol_spec]))
            ws.send(_tv_msg("create_series",
                            [chart_id, series_id, "s1", "symbol_1", interval, bars, ""]))
        except Exception as e:
            error_msg[0] = str(e)
            done[0] = True

    try:
        ws = _websocket.WebSocketApp(
            "wss://data.tradingview.com/socket.io/websocket",
            header={"Origin": "https://www.tradingview.com"},
            on_message=on_message,
            on_error=on_error,
            on_open=on_open,
            on_close=on_close,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()

        deadline = time.time() + timeout
        while not done[0] and time.time() < deadline:
            time.sleep(0.1)
        try: ws.close()
        except: pass

    except Exception as e:
        print(f"[TV] WebSocket error: {e}")
        df_yf = fetch_candles_yf(pair, tf)
        if df_yf is not None and not df_yf.empty:
            _set_data_source(pair, tf, "yFinance (TV failed)", len(df_yf))
        return df_yf

    if not collected:
        print(f"[TV] Geen data voor {pair} {tf} — fallback naar yFinance")
        df_yf = fetch_candles_yf(pair, tf)
        if df_yf is not None and not df_yf.empty:
            _set_data_source(pair, tf, "yFinance (TV failed)", len(df_yf))
        return df_yf

    df = pd.DataFrame(collected)
    # Converteer UTC timestamps naar Brusselse tijd
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["time"] = df["time"].dt.tz_convert("Europe/Brussels").dt.tz_localize(None)
    df = df.set_index("time").sort_index()
    df = df[~df.index.duplicated(keep="last")]

    print(f"[TV] {pair} {tf} -> {len(df)} bars (Brusselse tijd)")
    _set_data_source(pair, tf, "TV", len(df))
    return df

def fetch_price_tv(pair):
    """Haal live prijs op via TradingView WebSocket."""
    df = fetch_ohlcv_tv(pair, "15M", bars=5, timeout=6)
    if df is not None and not df.empty:
        return float(df["close"].iloc[-1])
    return None

def fetch_candles_yf(pair, tf, start=None, end=None):
    """yFinance fallback voor backtesting historische data."""
    sym = SYMBOLS.get(pair, "EURUSD=X")
    iv  = TF_YF.get(tf, "15m")
    try:
        t = yf.Ticker(sym)
        if start and end:
            s = datetime.datetime.strptime(start, "%Y-%m-%d")
            e = datetime.datetime.strptime(end, "%Y-%m-%d") + datetime.timedelta(days=1)
            now = datetime.datetime.now()
            if iv == "15m":
                cutoff = now - datetime.timedelta(days=58)
                effective_start = max(s, cutoff)
                if effective_start > e:
                    iv = "1h"
                    df = t.history(start=s, end=e, interval="1h")
                else:
                    chunks = []
                    chunk_start = effective_start
                    while chunk_start < e:
                        chunk_end = min(chunk_start + datetime.timedelta(days=55), e)
                        try:
                            chunk = t.history(start=chunk_start, end=chunk_end, interval="15m")
                            if chunk is not None and not chunk.empty:
                                chunks.append(chunk)
                        except: pass
                        chunk_start = chunk_end
                    if not chunks:
                        iv = "1h"
                        df = t.history(start=s, end=e, interval="1h")
                    else:
                        df = pd.concat(chunks)
                        df = df[~df.index.duplicated(keep='first')]
                        df = df.sort_index()
            else:
                df = t.history(start=s, end=e, interval=iv)
        else:
            period = TF_PERIOD.get(tf, "7d")
            df = t.history(period=period, interval=iv)

        if df is None or df.empty:
            return None
        df = df[["Open","High","Low","Close"]].copy()
        df.columns = ["open","high","low","close"]
        if tf == "4H":
            df = df.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna()
        # Converteer naar Brusselse tijd
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Europe/Brussels").tz_localize(None)
        print(f"[YF] {pair} {tf} -> {len(df)} bars")
        return df
    except Exception as e:
        print(f"[YF] Error {pair} {tf}: {e}")
        return None

def fetch_candles(pair, tf, start=None, end=None):
    """Hoofdfunctie — TV WebSocket voor live, yFinance voor backtesting."""
    if start and end:
        # Backtesting: gebruik yFinance voor historische data
        return fetch_candles_yf(pair, tf, start, end)
    else:
        # Live: gebruik TradingView WebSocket
        return fetch_ohlcv_tv(pair, tf)

def fetch_price(pair):
    """Live prijs — TV WebSocket eerst, yFinance als fallback."""
    price = fetch_price_tv(pair)
    if price and price > 0:
        return price
    # yFinance fallback
    try:
        t = yf.Ticker(SYMBOLS.get(pair, "EURUSD=X"))
        price = float(t.fast_info.last_price)
        if price and price > 0:
            return price
    except: pass
    return None



def df_to_list(df):
    if df is None: return []
    out=[]
    for ts,row in df.iterrows():
        try: ti=int(ts.timestamp())
        except: ti=0
        out.append({"time":ti,"open":round(float(row.open),5),"high":round(float(row.high),5),
                    "low":round(float(row.low),5),"close":round(float(row.close),5)})
    return out

def calc_bias(df, pair="EURUSD"):
    if df is None or len(df)<20:
        return {"total_score":0,"verdict":"GEEN DATA","verdict_color":"#888",
                "j1":0,"j1_label":"—","j1_detail":"","j2":0,"j2_label":"—","j2_detail":"",
                "j3":0,"j3_label":"—","j3_detail":"","struct_label":"—","struct_conflict":False,
                "advice":"Laad data","session":"—","in_kz":False,
                "ote_low":0,"ote_high":0,"ote_705":0,"equilibrium":0,"range_high":0,"range_low":0}
    closes=df["close"].values; highs=df["high"].values; lows=df["low"].values; n=len(df)
    lb=min(20,n-1); cur=float(closes[-1])
    rh=float(highs[-lb:].max()); rl=float(lows[-lb:].min()); rs=rh-rl; eq=(rh+rl)/2; buf=rs*0.08
    in_prem=cur>eq+buf; in_disc=cur<eq-buf
    j1=1 if in_disc else(-1 if in_prem else 0)
    pos_pct=round((cur-rl)/rs*100,1) if rs>0 else 50
    j1_label="▲ Discount" if in_disc else("▼ Premium" if in_prem else"— EQ Zone")
    j1_detail=f"Positie:{pos_pct}%  EQ:{eq:.5f}"
    ote_low=rl+rs*0.618; ote_705=rl+rs*0.705; ote_high=rl+rs*0.79
    llb=min(10,n-1); eth=PIP.get(pair,0.0001)*5
    bsl=None; ssl=None
    rh_arr=highs[-llb:]; rl_arr=lows[-llb:]
    for i in range(len(rh_arr)):
        for j in range(i+1,len(rh_arr)):
            if abs(rh_arr[i]-rh_arr[j])<eth and rh_arr[i]>cur:
                if bsl is None or rh_arr[i]<bsl: bsl=float(rh_arr[i])
    for i in range(len(rl_arr)):
        for j in range(i+1,len(rl_arr)):
            if abs(rl_arr[i]-rl_arr[j])<eth and rl_arr[i]<cur:
                if ssl is None or rl_arr[i]>ssl: ssl=float(rl_arr[i])
    db=(bsl-cur) if bsl else None; ds=(cur-ssl) if ssl else None
    if db is not None and ds is not None: j2=1 if db<ds else(-1 if ds<db else 0)
    elif db is not None: j2=1
    elif ds is not None: j2=-1
    else: j2=0
    j2_label="▲ Draw BSL" if j2>0 else("▼ Draw SSL" if j2<0 else"— Geen DOL")
    j2_detail=f"BSL@{bsl:.5f}" if bsl and j2>0 else(f"SSL@{ssl:.5f}" if ssl and j2<0 else"Geen gelijke H/L")
    today=df.index[-1].date() if hasattr(df.index[-1],"date") else None
    day_open=None
    if today:
        try:
            td=df[df.index.date==today]
            if len(td): day_open=float(td["open"].iloc[0])
        except: pass
    if day_open is None: day_open=float(df["open"].iloc[-min(8,n)])
    j3=1 if cur<day_open else(-1 if cur>day_open else 0)
    dpct=round((cur-day_open)/day_open*100,3) if day_open>0 else 0
    j3_label="▲ Onder Open" if j3>0 else("▼ Boven Open" if j3<0 else"— Op Open")
    j3_detail=f"DagOpen:{day_open:.5f}  Δ:{dpct:+.3f}%"
    h=highs[-min(30,n):]; l=lows[-min(30,n):]; m=len(h)//2
    hh=h[m:].max()>h[:m].max(); hl=l[m:].min()>l[:m].min()
    lh=h[m:].max()<h[:m].max(); ll=l[m:].min()<l[:m].min()
    sb=1 if(hh and hl) else(-1 if(lh and ll) else 0)
    sc=(sb!=0 and j1!=0 and sb!=j1); sa=(sb!=0 and sb==j1)
    struct_label="✓ Bevestigd" if sa else("⚠ Conflict" if sc else"— Geen")
    raw=j1+j2+j3
    total=max(-3,min(3,raw-(1 if sc and raw>0 else -1 if sc and raw<0 else 0)))
    vmap={3:("BULLISH ✓✓✓","#16a34a"),2:("BULLISH ✓✓","#22c55e"),1:("ZWAK BULL","#86efac"),
          0:("GEEN TREND","#94a3b8"),-1:("ZWAK BEAR","#fca5a5"),-2:("BEARISH ✓✓","#ef4444"),-3:("BEARISH ✓✓✓","#b91c1c")}
    vtext,vcol=vmap.get(total,("—","#888"))
    now_b=now_brussels()
    def _sess(h,m):
        t=h*60+m
        if 540<=t<720: return"🇬🇧 London Killzone",True
        if 840<=t<1020: return"🗽 NY Killzone",True
        if 480<=t<540: return"🇬🇧 London (prep)",False
        if 720<=t<840: return"London Close",False
        if 120<=t<480: return"🌏 Asia/Tokyo",False
        return"Off Session",False
    session,in_kz=_sess(now_b.hour,now_b.minute)
    advice=("🎯 Long — Killzone!" if total>=2 and in_kz else"Long — zoek FVG/OB" if total>=2 else
            "🎯 Short — Killzone!" if total<=-2 and in_kz else"Short — zoek FVG/OB" if total<=-2 else
            "Wacht — zwak" if abs(total)==1 else"Stay out")
    return{"total_score":total,"verdict":vtext,"verdict_color":vcol,
           "j1":j1,"j1_label":j1_label,"j1_detail":j1_detail,
           "j2":j2,"j2_label":j2_label,"j2_detail":j2_detail,
           "j3":j3,"j3_label":j3_label,"j3_detail":j3_detail,
           "struct_label":struct_label,"struct_conflict":sc,"advice":advice,
           "session":session,"in_kz":in_kz,
           "ote_low":round(ote_low,5),"ote_high":round(ote_high,5),"ote_705":round(ote_705,5),
           "equilibrium":round(eq,5),"range_high":round(rh,5),"range_low":round(rl,5)}

def detect_fvg(df, i, check_displacement=False):
    """
    Bullish FVG: low[i] > high[i-2]  — gap omhoog
    Bearish FVG: high[i] < low[i-2]  — gap omlaag

    Punt 3 — Displacement check:
    De middelste candle (i-1) moet een echte impulscandle zijn:
    - Body > 1.5x gemiddelde body van laatste 10 candles
    - Wick ratio < 40% van totale range (echte displacement = kleine wicks)
    """
    if i < 2: return None
    h2, l2 = float(df.iloc[i-2]["high"]), float(df.iloc[i-2]["low"])
    h0, l0 = float(df.iloc[i]["high"]),   float(df.iloc[i]["low"])
    h1, l1 = float(df.iloc[i-1]["high"]), float(df.iloc[i-1]["low"])
    o1, c1 = float(df.iloc[i-1]["open"]), float(df.iloc[i-1]["close"])

    fvg = None
    if l0 > h2: fvg = {"type":"bull","top":l0,"bottom":h2,"formed_at":i}
    elif h0 < l2: fvg = {"type":"bear","top":l2,"bottom":h0,"formed_at":i}

    if fvg is None: return None

    if check_displacement and i >= 12:
        # Bereken gemiddelde body van laatste 10 candles
        bodies = []
        for k in range(max(0, i-11), i):
            o = float(df.iloc[k]["open"]); c = float(df.iloc[k]["close"])
            bodies.append(abs(c - o))
        avg_body = sum(bodies) / len(bodies) if bodies else 0

        # Impulscandle body
        imp_body  = abs(c1 - o1)
        imp_range = h1 - l1 if h1 > l1 else 0.0001

        # Wick ratio: wicks / totale range
        upper_wick = h1 - max(o1, c1)
        lower_wick = min(o1, c1) - l1
        wick_ratio = (upper_wick + lower_wick) / imp_range

        # Displacement vereisten
        is_displacement = (
            imp_body > avg_body * 1.5 and  # grote body
            wick_ratio < 0.4               # kleine wicks
        )
        if not is_displacement:
            return None

    return fvg

def detect_ob(df, i):
    """
    Bullish OB: laatste bearish candle voor bullish impuls die boven de OB high sluit.
    Bearish OB: laatste bullish candle voor bearish impuls die onder de OB low sluit.
    Vereiste: de volgende candle doorbreekt het niveau — grootte van impuls is NIET vereist
    (te strict was het probleem).
    """
    if i < 1: return None
    o1,c1 = float(df.iloc[i-1]["open"]), float(df.iloc[i-1]["close"])
    o0,c0 = float(df.iloc[i]["open"]),   float(df.iloc[i]["close"])
    # Bullish OB: vorige candle bearish, huidige bullish en sluit boven high van OB candle
    if c1 < o1 and c0 > o0 and c0 > float(df.iloc[i-1]["high"]):
        return {"type":"bull","top":max(o1,c1),"bottom":min(o1,c1)}
    # Bearish OB: vorige candle bullish, huidige bearish en sluit onder low van OB candle
    if c1 > o1 and c0 < o0 and c0 < float(df.iloc[i-1]["low"]):
        return {"type":"bear","top":max(o1,c1),"bottom":min(o1,c1)}
    return None

def detect_liquidity_sweep(df, i, lookback_swing=20, lookback_sweep=5, direction="LONG"):
    """
    Detecteert een liquidity sweep (stop-run) net vóór bar i.

    ICT definitie (boek hoofdstuk over Liquidity + Turtle Soup):
    - Voor LONG entry willen we een SELLSIDE sweep zien:
      een recente bar's LOW dipte ONDER een eerdere swing low, maar de CLOSE
      kwam terug BOVEN dat swing-low niveau (stops eronder geveegd, prijs
      kwam terug → smart money kocht).
    - Voor SHORT entry willen we een BUYSIDE sweep zien:
      een recente bar's HIGH ging BOVEN een eerdere swing high, maar de CLOSE
      kwam terug ONDER dat niveau.

    Parameters:
      i              — huidige bar index (we kijken naar bars vóór deze)
      lookback_swing — hoever terug om het swing high/low te bepalen (default 20)
      lookback_sweep — binnen hoeveel recente bars moet de sweep gebeurd zijn (default 5)
      direction      — "LONG" of "SHORT"

    Returns: dict met sweep info, of None als geen sweep gevonden.
      {"swept_level": float, "sweep_bar": int, "type": "buyside"|"sellside"}
    """
    if i < lookback_swing + lookback_sweep:
        return None

    # Definieer de "oude" range waar liquiditeit zit
    old_start = max(0, i - lookback_swing - lookback_sweep)
    old_end   = max(0, i - lookback_sweep)
    if old_end - old_start < 5:
        return None

    old_highs = df["high"].values[old_start:old_end]
    old_lows  = df["low"].values[old_start:old_end]
    swing_high = float(old_highs.max())
    swing_low  = float(old_lows.min())

    # Scan de "recente" bars op een sweep
    recent_start = max(0, i - lookback_sweep)
    for j in range(recent_start, i):
        bar_high  = float(df.iloc[j]["high"])
        bar_low   = float(df.iloc[j]["low"])
        bar_close = float(df.iloc[j]["close"])

        if direction == "LONG":
            # Sellside sweep: low dipte onder oude swing low, close kwam terug erboven
            if bar_low < swing_low and bar_close > swing_low:
                return {
                    "swept_level": swing_low,
                    "sweep_bar":   j,
                    "type":        "sellside",
                }
        else:  # SHORT
            # Buyside sweep: high ging boven oude swing high, close kwam terug eronder
            if bar_high > swing_high and bar_close < swing_high:
                return {
                    "swept_level": swing_high,
                    "sweep_bar":   j,
                    "type":        "buyside",
                }
    return None

def precompute_bias(df, pair):
    """
    Berekent bias EENMALIG voor elke bar — veel efficiënter dan per bar opnieuw.
    Geeft een lijst van scores terug, één per bar.
    """
    n = len(df)
    scores = [0] * n
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(20, n):
        lb  = min(20, i)
        cur = float(closes[i])
        rh  = float(highs[i-lb:i].max())
        rl  = float(lows[i-lb:i].min())
        rs  = rh - rl
        eq  = (rh + rl) / 2
        buf = rs * 0.08

        # Judge 1: Premium/Discount
        j1 = 1 if cur < eq - buf else (-1 if cur > eq + buf else 0)

        # Judge 2: Draw on Liquidity — gelijke highs/lows
        llb = min(10, i)
        eth = PIP.get(pair, 0.0001) * 5
        rh_arr = highs[i-llb:i]
        rl_arr = lows[i-llb:i]
        bsl = None; ssl = None
        for a in range(len(rh_arr)):
            for b2 in range(a+1, len(rh_arr)):
                if abs(rh_arr[a]-rh_arr[b2]) < eth and rh_arr[a] > cur:
                    if bsl is None or rh_arr[a] < bsl: bsl = float(rh_arr[a])
        for a in range(len(rl_arr)):
            for b2 in range(a+1, len(rl_arr)):
                if abs(rl_arr[a]-rl_arr[b2]) < eth and rl_arr[a] < cur:
                    if ssl is None or rl_arr[a] > ssl: ssl = float(rl_arr[a])
        db = (bsl - cur) if bsl else None
        ds = (cur - ssl) if ssl else None
        if db is not None and ds is not None:
            j2 = 1 if db < ds else (-1 if ds < db else 0)
        elif db is not None: j2 = 1
        elif ds is not None: j2 = -1
        else: j2 = 0

        # Judge 3: Power of 3 — dagopening
        day_open = float(closes[max(0, i-8)])
        j3 = 1 if cur < day_open else (-1 if cur > day_open else 0)

        # Structuur filter: HH+HL of LH+LL over laatste 30 bars
        lb2 = min(30, i)
        h_arr = highs[i-lb2:i]
        l_arr = lows[i-lb2:i]
        m2 = len(h_arr) // 2
        if m2 > 0:
            hh = h_arr[m2:].max() > h_arr[:m2].max()
            hl = l_arr[m2:].min() > l_arr[:m2].min()
            lh = h_arr[m2:].max() < h_arr[:m2].max()
            ll = l_arr[m2:].min() < l_arr[:m2].min()
            sb = 1 if (hh and hl) else (-1 if (lh and ll) else 0)
        else:
            sb = 0

        raw = j1 + j2 + j3
        # Structuur conflict trekt 1 punt af
        sc = (sb != 0 and j1 != 0 and sb != j1)
        total = max(-3, min(3, raw - (1 if sc and raw > 0 else -1 if sc and raw < 0 else 0)))
        scores[i] = total

    return scores, df["close"].values, df["high"].values, df["low"].values

def run_backtest(pair, tf, start, end, capital, lotsize, rr, use_ob, use_trend, use_eq, min_score, use_session=False, use_sweep=False, be_trigger=0.0, spread_pips=0.0, slippage_pips=0.0, lotsize_eur=None, lotsize_xau=None, max_daily_loss=0, max_trades=0, max_risk_pct=0, **kwargs):
    # Selecteer juiste lotsize op basis van pair
    if pair == "XAUUSD" and lotsize_xau is not None:
        lotsize = lotsize_xau
    elif pair == "EURUSD" and lotsize_eur is not None:
        lotsize = lotsize_eur

    # Max risico per trade: pas lotsize aan op basis van % van kapitaal
    if max_risk_pct > 0 and capital > 0:
        typical_sl = 20 if pair == "EURUSD" else 200
        pip_v_temp = PIP.get(pair, 0.0001)
        pip_e_temp = PIP_EUR.get(pair, 0.10)
        max_risk_eur = capital * max_risk_pct / 100
        auto_lot = max_risk_eur / (typical_sl * pip_e_temp)
        lotsize = max(1, round(auto_lot))
    df = fetch_candles(pair, tf, start, end)
    if df is None or len(df) < 30:
        return {"error":"Geen data voor deze periode.", "trades":[], "stats":{}}

    pip_v = PIP.get(pair, 0.0001)
    pip_e = PIP_EUR.get(pair, 0.10)

    # Bereken bias scores eenmalig voor alle bars
    bias_scores, closes, highs, lows = precompute_bias(df, pair)

    trades    = []
    n         = len(df)
    used_fvgs = set()
    daily_pnl = {}   # date -> pnl voor max_daily_loss tracking

    # Pre-scan alle FVGs en OBs eenmalig — met displacement check (punt 3)
    all_fvgs = {}
    all_obs  = {}
    for i in range(2, n):
        f = detect_fvg(df, i, check_displacement=True)
        if f: all_fvgs[i] = f
    for i in range(1, n):
        o = detect_ob(df, i)
        if o: all_obs[i] = o

    for i in range(20, n - 2):
        score = bias_scores[i]

        # Max open trades check
        open_trades_count = len([t for t in trades if t.get("outcome") is None])
        if max_trades > 0 and open_trades_count >= max_trades:
            continue

        # Max dagelijks verlies check
        if max_daily_loss > 0:
            bar_date = str(df.index[i])[:10]
            if daily_pnl.get(bar_date, 0) <= -max_daily_loss:
                continue
        if abs(score) < min_score: continue

        direction = "LONG" if score >= min_score else ("SHORT" if score <= -min_score else None)
        if direction is None: continue

        # Equilibrium op dit moment (voor EQ filter)
        lb  = min(20, i)
        rh  = float(highs[i-lb:i].max())
        rl  = float(lows[i-lb:i].min())
        eq  = (rh + rl) / 2

        # ── Trend filter ──
        # Correct: kijkt of de laatste 30 bars een duidelijke HH+HL of LH+LL structuur hebben
        if use_trend:
            lb2   = min(30, i)
            h_arr = highs[i-lb2:i]
            l_arr = lows[i-lb2:i]
            m2    = len(h_arr) // 2
            if m2 > 0:
                hh = h_arr[m2:].max() > h_arr[:m2].max()
                hl = l_arr[m2:].min() > l_arr[:m2].min()
                lh = h_arr[m2:].max() < h_arr[:m2].max()
                ll = l_arr[m2:].min() < l_arr[:m2].min()
                trend = 1 if (hh and hl) else (-1 if (lh and ll) else 0)
                # Alleen skippen als trend duidelijk TEGEN de richting is
                # Als trend=0 (neutraal) laten we de trade door
                if trend != 0 and trend != (1 if direction=="LONG" else -1):
                    continue

        # ── Zoek actieve FVG ──
        # Kijk terug maximaal 30 bars voor een niet-gemitigeerde FVG
        fvg = None
        for fi in range(i-1, max(i-30, 2), -1):
            if fi not in all_fvgs: continue
            if fi in used_fvgs: continue
            f = all_fvgs[fi]
            if f["type"] != ("bull" if direction=="LONG" else "bear"): continue

            # Mitigatie check: is de FVG al volledig doorbroken?
            mitigated = False
            for k in range(f["formed_at"]+1, i+1):
                if f["type"]=="bull" and float(df.iloc[k]["low"])  < f["bottom"]: mitigated=True; break
                if f["type"]=="bear" and float(df.iloc[k]["high"]) > f["top"]:   mitigated=True; break
            if mitigated: continue

            # Equilibrium filter: gebruik de EQ op het moment dat de FVG gevormd werd
            # Dit is consistent — we vergelijken de FVG met de EQ van DAT moment
            if use_eq:
                lb_fvg = min(20, fi)
                eq_at_fvg = (float(highs[fi-lb_fvg:fi].max()) + float(lows[fi-lb_fvg:fi].min())) / 2
                fvg_mid   = (f["top"] + f["bottom"]) / 2
                if direction=="LONG"  and fvg_mid >= eq_at_fvg: continue
                if direction=="SHORT" and fvg_mid <= eq_at_fvg: continue

            fvg = f
            break

        if fvg is None: continue

        # ── OB filter ──
        # Zoek de meest recente OB in dezelfde richting, gevormd VOOR of OP de FVG bar
        ob = None
        if use_ob:
            for oi in range(fvg["formed_at"], max(fvg["formed_at"]-20, 1), -1):
                if oi not in all_obs: continue
                o = all_obs[oi]
                if o["type"] == ("bull" if direction=="LONG" else "bear"):
                    ob = o; break
            if ob is None: continue

        # ── Liquidity Sweep filter ──
        # Vereist dat er vóór de FVG vorming een sweep (stop-run) was
        if use_sweep:
            sweep = detect_liquidity_sweep(
                df, fvg["formed_at"],
                lookback_swing=20, lookback_sweep=5,
                direction=direction
            )
            if sweep is None: continue

        # ── Entry: wacht tot prijs terugkeert IN de FVG zone ──
        entry_bar   = None
        entry_price = None

        for j in range(fvg["formed_at"]+1, min(i+2, n-1)):
            c_high = float(df.iloc[j]["high"])
            c_low  = float(df.iloc[j]["low"])

            if direction == "LONG":
                if c_low <= fvg["top"] and c_low >= fvg["bottom"]:
                    entry_price = (fvg["top"] + fvg["bottom"]) / 2
                    entry_bar   = j; break
                elif c_low < fvg["bottom"]: break  # gemitigeerd
            else:
                if c_high >= fvg["bottom"] and c_high <= fvg["top"]:
                    entry_price = (fvg["top"] + fvg["bottom"]) / 2
                    entry_bar   = j; break
                elif c_high > fvg["top"]: break  # gemitigeerd

        if entry_bar is None or entry_price is None: continue

        # Geen dubbele trade op hetzelfde tijdstip
        ts_entry = df.index[entry_bar]
        entry_ts = int(ts_entry.timestamp()) if hasattr(ts_entry,"timestamp") else 0
        if any(abs(t["entry_ts"] - entry_ts) < 1800 for t in trades):
            continue

        # ── Session filter ──
        if use_session:
            entry_hour_utc = ts_entry.hour if hasattr(ts_entry, 'hour') else 0
            in_london_kz = 7 <= entry_hour_utc < 10
            in_ny_kz     = 12 <= entry_hour_utc < 15
            if not (in_london_kz or in_ny_kz):
                continue

        # ── Punt 2 — Consequent Encroachment: entry op 50% midpunt FVG ──
        raw_entry = entry_price  # = (fvg top + bottom) / 2 — al correct

        # ── Punt 1 — Swing Low/High SL ──
        # Zoek swing low/high in de bars VOOR de FVG formatie
        lb_sl_start = max(0, fvg["formed_at"] - 20)
        lb_sl_end   = fvg["formed_at"]

        if direction == "LONG":
            swing_low = None
            for si in range(lb_sl_end-1, lb_sl_start, -1):
                if si < 1 or si >= n-1: continue
                l_c = float(df.iloc[si]["low"])
                l_p = float(df.iloc[si-1]["low"])
                l_n = float(df.iloc[si+1]["low"])
                if l_c < l_p and l_c < l_n and l_c < fvg["bottom"]:
                    swing_low = l_c; break
            sl   = (swing_low - pip_v * 2) if swing_low else (fvg["bottom"] - pip_v * 15)
            risk = raw_entry - sl
            if risk <= 0 or risk > raw_entry * 0.05: continue
            tp   = raw_entry + risk * rr
        else:
            swing_high = None
            for si in range(lb_sl_end-1, lb_sl_start, -1):
                if si < 1 or si >= n-1: continue
                h_c = float(df.iloc[si]["high"])
                h_p = float(df.iloc[si-1]["high"])
                h_n = float(df.iloc[si+1]["high"])
                if h_c > h_p and h_c > h_n and h_c > fvg["top"]:
                    swing_high = h_c; break
            sl   = (swing_high + pip_v * 2) if swing_high else (fvg["top"] + pip_v * 15)
            risk = sl - raw_entry
            if risk <= 0 or risk > raw_entry * 0.05: continue
            tp   = raw_entry - risk * rr

        sl_pips = round(risk / pip_v, 1)

        # ── Spread + Slippage: verlaagt de effectieve P&L ──
        # Totale transactiekosten in pips (entry + exit spread + slippage)
        # Dit wordt APART van de simulatie afgetrokken van het eindresultaat
        total_cost_pips = spread_pips + slippage_pips  # pips kost per trade

        # ── Forward simulatie met break-even ──
        outcome=None; exit_price=None; ts_exit=None
        current_sl = sl
        be_moved   = False
        be_active  = be_trigger > 0

        for j in range(entry_bar+1, min(entry_bar+200, n)):
            h2 = float(df.iloc[j]["high"])
            l2 = float(df.iloc[j]["low"])

            # Break-even check: verplaats SL naar entry als be_trigger bereikt
            if be_active and not be_moved and risk > 0:
                if direction == "LONG":
                    be_level = entry_price + risk * be_trigger
                    if h2 >= be_level:
                        current_sl = entry_price + pip_v
                        be_moved   = True
                else:
                    be_level = entry_price - risk * be_trigger
                    if l2 <= be_level:
                        current_sl = entry_price - pip_v
                        be_moved   = True

            if direction == "LONG":
                if l2 <= current_sl: outcome="loss" if current_sl < entry_price else "be"; exit_price=current_sl; ts_exit=df.index[j]; break
                if h2 >= tp:         outcome="win";  exit_price=tp;          ts_exit=df.index[j]; break
            else:
                if h2 >= current_sl: outcome="loss" if current_sl > entry_price else "be"; exit_price=current_sl; ts_exit=df.index[j]; break
                if l2 <= tp:         outcome="win";  exit_price=tp;          ts_exit=df.index[j]; break

        if outcome is None: continue

        # Bereken bruto pips (wat de markt deed)
        gross_pips = round(((exit_price-raw_entry) if direction=="LONG" else (raw_entry-exit_price)) / pip_v, 1)

        # Trek transactiekosten af — dit is de ECHTE winst/verlies
        pips = round(gross_pips - total_cost_pips, 1)
        pnl  = round(pips * pip_e * lotsize, 2)

        # Herbereken outcome op basis van netto pips (na kosten)
        final_outcome = "win" if pips > 0 else ("be" if pips == 0 else "loss")
        used_fvgs.add(fvg["formed_at"])

        # Track dagelijks P&L voor max_daily_loss filter
        exit_date = str(ts_exit)[:10] if ts_exit else str(ts_entry)[:10]
        daily_pnl[exit_date] = daily_pnl.get(exit_date, 0) + pnl

        trades.append({
            "id":          len(trades)+1,
            "pair":        pair,
            "direction":   direction,
            "entry_price": round(entry_price, 5),
            "exit_price":  round(exit_price,  5),
            "sl":          round(sl, 5),
            "tp":          round(tp, 5),
            "sl_pips":     sl_pips,
            "tp_pips":     round(sl_pips*rr, 1),
            "outcome":     final_outcome,
            "be_moved":    be_moved,
            "pips":        pips,
            "pnl_eur":     pnl,
            "bias_score":  score,
            "session":     "—",
            "entry_time":  str(ts_entry)[:16],
            "exit_time":   str(ts_exit)[:16],
            "entry_ts":    entry_ts,
            "exit_ts":     int(ts_exit.timestamp()) if hasattr(ts_exit,"timestamp") else 0,
        })

    if not trades:
        stats={"total":0,"wins":0,"losses":0,"be":0,"winrate":0,"total_pnl":0,"best":0,"worst":0,"avg_pips":0}
    else:
        wins  = [t for t in trades if t["outcome"]=="win"]
        losses= [t for t in trades if t["outcome"]=="loss"]
        bes   = [t for t in trades if t["outcome"]=="be"]
        pnls  = [t["pnl_eur"] for t in trades]
        pipsl = [t["pips"]    for t in trades]
        decided = len(wins) + len(losses)
        stats = {
            "total":     len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "be":        len(bes),
            "winrate":   round(len(wins)/decided*100, 1) if decided > 0 else 0,
            "total_pnl": round(sum(pnls), 2),
            "best":      round(max(pnls), 2),
            "worst":     round(min(pnls), 2),
            "avg_pips":  round(sum(pipsl)/len(pipsl), 1),
        }
    return {"trades":trades,"stats":stats,"candles":df_to_list(df)}

@app.route("/")
def index(): return Response(HTML,mimetype="text/html")

@app.route("/static/lw-charts.js")
def lw_charts():
    import urllib.request
    for url in ["https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js",
                "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"]:
        try:
            r=urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=8)
            data=r.read(); print(f"[LW] OK {len(data)}b"); return Response(data,mimetype="application/javascript")
        except Exception as e: print(f"[LW] {e}")
    return Response("window.LightweightCharts={createChart:function(el){el.innerHTML='<div style=padding:40px;color:#a78bfa>Chart library niet beschikbaar — controleer internet</div>';var s={setData:function(){},setMarkers:function(){},applyOptions:function(){}};return{addCandlestickSeries:function(){return s;},timeScale:function(){return{fitContent:function(){},applyOptions:function(){}};},applyOptions:function(){}};},CrosshairMode:{Normal:0}};",mimetype="application/javascript")

@app.route("/api/price")
def api_price():
    pair  = request.args.get("pair", "EURUSD")
    price = fetch_price(pair)
    prev  = fetch_price(pair)  # same call, just for structure
    return jsonify({"price": price, "pair": pair})

@app.route("/api/candles")
def api_candles():
    pair=request.args.get("pair","EURUSD"); tf=request.args.get("tf","15M")
    df=fetch_candles(pair,tf)
    candles=df_to_list(df)
    print(f"[CANDLES] {pair} {tf} -> {len(candles)} candles")
    return jsonify({"candles":candles})

@app.route("/api/bias")
def api_bias():
    pair = request.args.get("pair","EURUSD")
    tf   = request.args.get("tf","15M")
    df   = fetch_candles(pair, tf)
    bias = calc_bias(df, pair)
    bias["price"] = fetch_price(pair)
    # Voeg live FVG zones toe voor de chart overlay
    live_fvgs = []
    if df is not None and len(df) >= 3:
        n = len(df)
        for i in range(max(2, n-30), n):
            f = detect_fvg(df, i)
            if f:
                # Check niet-gemitigeerd
                mitigated = False
                for k in range(f["formed_at"]+1, n):
                    if f["type"]=="bull" and float(df.iloc[k]["low"])  < f["bottom"]: mitigated=True; break
                    if f["type"]=="bear" and float(df.iloc[k]["high"]) > f["top"]:   mitigated=True; break
                if not mitigated:
                    ts = df.index[f["formed_at"]]
                    live_fvgs.append({
                        "type":   f["type"],
                        "top":    round(f["top"],5),
                        "bottom": round(f["bottom"],5),
                        "time":   int(ts.timestamp()) if hasattr(ts,"timestamp") else 0,
                    })
    bias["live_fvgs"] = live_fvgs
    return jsonify(bias)

@app.route("/api/calendar")
def api_calendar():
    """Returns recurring high-impact events for the current month."""
    now = now_brussels()
    year, month = now.year, now.month

    # Find first Friday of the month (NFP)
    def first_friday(y, m):
        d = datetime.date(y, m, 1)
        while d.weekday() != 4:
            d += datetime.timedelta(days=1)
        return d

    # Find Wednesdays that are typically FOMC (approx — 8x per year)
    FOMC_MONTHS = [1, 3, 5, 6, 7, 9, 11, 12]

    events = []

    # NFP — first Friday, 14:30 Brussels
    nfp = first_friday(year, month)
    events.append({
        "date": str(nfp),
        "time": "14:30",
        "name": "NFP — Non-Farm Payrolls",
        "currency": "USD",
        "impact": "high",
        "note": "Avoid trading Thu/Fri this week"
    })

    # CPI — usually 2nd or 3rd week, approx 12th of month, 14:30 Brussels
    cpi_day = datetime.date(year, month, 12)
    while cpi_day.weekday() > 4:
        cpi_day += datetime.timedelta(days=1)
    events.append({
        "date": str(cpi_day),
        "time": "14:30",
        "name": "CPI — Consumer Price Index",
        "currency": "USD",
        "impact": "high",
        "note": "Wait 10min after release"
    })

    # FOMC — if this is a FOMC month, approx 3rd week Wednesday, 20:00 Brussels
    if month in FOMC_MONTHS:
        fomc = datetime.date(year, month, 15)
        while fomc.weekday() != 2:
            fomc += datetime.timedelta(days=1)
        events.append({
            "date": str(fomc),
            "time": "20:00",
            "name": "FOMC Rate Decision",
            "currency": "USD",
            "impact": "high",
            "note": "Avoid trading the full day"
        })

    # ECB — usually 1st or 2nd Thursday of the month (6x per year: Jan,Mar,Apr,Jun,Jul,Sep,Oct,Dec)
    ECB_MONTHS = [1, 3, 4, 6, 7, 9, 10, 12]
    if month in ECB_MONTHS:
        ecb = datetime.date(year, month, 1)
        thursdays = 0
        while thursdays < 2:
            if ecb.weekday() == 3:
                thursdays += 1
                if thursdays == 2:
                    break
            ecb += datetime.timedelta(days=1)
        events.append({
            "date": str(ecb),
            "time": "14:15",
            "name": "ECB Rate Decision",
            "currency": "EUR",
            "impact": "high",
            "note": "Major EUR volatility expected"
        })

    # PPI — usually a few days after CPI
    ppi_day = cpi_day + datetime.timedelta(days=2)
    while ppi_day.weekday() > 4:
        ppi_day += datetime.timedelta(days=1)
    events.append({
        "date": str(ppi_day),
        "time": "14:30",
        "name": "PPI — Producer Price Index",
        "currency": "USD",
        "impact": "medium",
        "note": "Wait 10min after release"
    })

    # Sort by date
    events.sort(key=lambda e: e["date"])

    # Flag if today is a high-impact day or NFP week
    today = str(now.date())
    nfp_week_start = str(nfp - datetime.timedelta(days=3))
    nfp_week_end   = str(nfp + datetime.timedelta(days=1))
    is_nfp_week    = nfp_week_start <= today <= nfp_week_end
    is_danger_day  = any(e["date"] == today and e["impact"] == "high" for e in events)

    return jsonify({
        "events": events,
        "today": today,
        "is_nfp_week": is_nfp_week,
        "is_danger_day": is_danger_day,
        "month_label": now.strftime("%B %Y"),
    })

@app.route("/api/multibias")
def api_multibias():
    """Returns bias for all 3 timeframes simultaneously."""
    pair = request.args.get("pair", "EURUSD")
    result = {}
    for tf in ["15M", "1H", "4H"]:
        df   = fetch_candles(pair, tf)
        bias = calc_bias(df, pair)
        result[tf] = {
            "total_score":   bias["total_score"],
            "verdict":       bias["verdict"],
            "verdict_color": bias["verdict_color"],
            "j1": bias["j1"], "j1_label": bias["j1_label"],
            "j2": bias["j2"], "j2_label": bias["j2_label"],
            "j3": bias["j3"], "j3_label": bias["j3_label"],
            "equilibrium":   bias["equilibrium"],
            "ote_low":       bias["ote_low"],
            "ote_high":      bias["ote_high"],
        }
    # HTF alignment: all 3 agree on same direction
    scores = [result[tf]["total_score"] for tf in ["15M","1H","4H"]]
    all_bull = all(s >= 1 for s in scores)
    all_bear = all(s <= -1 for s in scores)
    result["alignment"] = "BULL" if all_bull else ("BEAR" if all_bear else "MIXED")
    result["alignment_color"] = "#34d399" if all_bull else ("#f87171" if all_bear else "#a78bfa")
    return jsonify(result)

@app.route("/api/system/health")
def system_health():
    import os, sys
    results = {}

    # 1. TradingView WebSocket check
    try:
        df = fetch_ohlcv_tv("EURUSD", "15M", bars=5, timeout=8)
        if df is not None and not df.empty:
            last_price = float(df["close"].iloc[-1])
            last_time  = str(df.index[-1])
            results["tradingview"] = {
                "status": "ok",
                "msg": f"Verbonden — laatste prijs: {last_price:.5f}",
                "detail": f"Laatste candle: {last_time} Brussels",
                "price": last_price
            }
        else:
            results["tradingview"] = {"status":"error","msg":"Geen data ontvangen","detail":"TV WebSocket returneert lege DataFrame"}
    except Exception as e:
        results["tradingview"] = {"status":"error","msg":f"Verbindingsfout","detail":str(e)}

    # 2. yFinance check
    try:
        import yfinance as yf
        t  = yf.Ticker("EURUSD=X")
        df = t.history(period="1d", interval="1h")
        if df is not None and not df.empty:
            yf_price = float(df["Close"].iloc[-1])
            results["yfinance"] = {
                "status": "ok",
                "msg": f"Bereikbaar — prijs: {yf_price:.5f}",
                "detail": "Yahoo Finance API reageert correct",
                "price": yf_price
            }
        else:
            results["yfinance"] = {"status":"warn","msg":"Geen data","detail":"yFinance geeft lege response"}
    except Exception as e:
        results["yfinance"] = {"status":"error","msg":"Niet bereikbaar","detail":str(e)}

    # 3. Prijsverschil TV vs yFinance
    try:
        tv_p  = results.get("tradingview",{}).get("price",0)
        yf_p  = results.get("yfinance",{}).get("price",0)
        if tv_p and yf_p:
            diff_pips = abs(tv_p - yf_p) / 0.0001
            if diff_pips < 20:
                results["data_quality"] = {"status":"ok","msg":f"Prijsverschil: {diff_pips:.1f} pips","detail":"Data bronnen zijn consistent"}
            elif diff_pips < 100:
                results["data_quality"] = {"status":"warn","msg":f"Prijsverschil: {diff_pips:.1f} pips","detail":"Mogelijk lichte vertraging op yFinance"}
            else:
                results["data_quality"] = {"status":"error","msg":f"Groot prijsverschil: {diff_pips:.1f} pips","detail":"yFinance heeft grote vertraging — TV data wordt gebruikt"}
        else:
            results["data_quality"] = {"status":"warn","msg":"Kan niet vergelijken","detail":"Een van de bronnen is niet beschikbaar"}
    except:
        results["data_quality"] = {"status":"warn","msg":"Check niet mogelijk","detail":""}

    # 4. Discord webhook check
    try:
        webhook = engine.config.get("discord_webhook","") or "https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ"
        if webhook:
            import requests
            r = requests.head(webhook.rsplit("/",1)[0], timeout=3)
            results["discord"] = {"status":"ok","msg":"Discord bereikbaar","detail":"Webhook URL geconfigureerd"}
        else:
            results["discord"] = {"status":"warn","msg":"Geen webhook ingesteld","detail":"Stel een Discord webhook in de config in"}
    except:
        results["discord"] = {"status":"warn","msg":"Niet gecontroleerd","detail":""}

    # 5. Engine status
    results["engine"] = {
        "status": "ok" if engine.running else "warn",
        "msg": "Actief" if engine.running else "Gestopt",
        "detail": f"Scans: {engine.scan_count} | Laatste scan: {engine.last_scan or '—'} | Open trades: {len(engine.open_trades)}"
    }
    if engine.stopped_by_risk:
        results["engine"]["status"] = "error"
        results["engine"]["msg"] = "Gestopt door risicobeheer"

    # 6. Systeem resources
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=1)
        mem_pct = mem.percent
        status = "ok" if mem_pct < 80 else "warn" if mem_pct < 90 else "error"
        results["system"] = {
            "status": status,
            "msg": f"RAM: {mem_pct:.0f}% | CPU: {cpu:.0f}%",
            "detail": f"Vrij geheugen: {mem.available//1024//1024} MB"
        }
    except:
        results["system"] = {"status":"warn","msg":"psutil niet beschikbaar","detail":"pip install psutil voor geheugen info"}

    # 7. Uptime
    try:
        import psutil
        boot = datetime.datetime.fromtimestamp(psutil.boot_time(), tz=BRUSSELS_TZ)
        uptime = now_brussels() - boot.replace(tzinfo=None).replace(tzinfo=BRUSSELS_TZ)
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        results["uptime"] = {"status":"ok","msg":f"VPS uptime: {h}u {m}m","detail":f"Opgestart: {boot.strftime('%Y-%m-%d %H:%M')} Brussels"}
    except:
        results["uptime"] = {"status":"ok","msg":"Uptime niet beschikbaar","detail":""}

    # 8. Python versie
    results["python"] = {
        "status": "ok",
        "msg": f"Python {sys.version.split()[0]}",
        "detail": sys.executable
    }

    # 9. Markt status
    is_wknd = engine._is_weekend()
    results["market"] = {
        "status": "ok" if not is_wknd else "warn",
        "msg": "Markt Open" if not is_wknd else "Markt Gesloten",
        "detail": fmt_brussels() + " Brussels"
    }

    return jsonify(results)

@app.route("/api/engine/test_discord", methods=["POST"])
def engine_test_discord():
    d = request.json or {}
    webhook = d.get("webhook","")
    if not webhook:
        return jsonify({"ok":False,"error":"Geen webhook URL"})
    try:
        send_discord(webhook,
            "✅ **Test Successful — GAMAN Engine is Live**\n"
            "The engine is connected and ready to trade.\n"
            "📈 You will receive notifications here when trades are opened or closed.",
            0x7c3aed)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/engine/delete_trade", methods=["POST"])
def engine_delete_trade():
    d = request.json or {}
    trade_id = d.get("id")
    with engine.lock:
        engine.closed_trades = [t for t in engine.closed_trades if t.get("id") != trade_id]
    engine._save_state()
    return jsonify({"ok": True})

@app.route("/api/engine/close_all", methods=["POST"])
def engine_close_all():
    """Sluit alle open trades op huidige marktprijs."""
    closed_count = 0
    with engine.lock:
        trades_to_close = list(engine.open_trades)

    for trade in trades_to_close:
        pair  = trade["pair"]
        price = fetch_price(pair)
        if not price:
            price = trade["entry_price"]  # fallback

        pip_v = PIP.get(pair, 0.0001)
        pip_e = PIP_EUR.get(pair, 0.10)
        direction = trade["direction"]
        entry = trade["entry_price"]
        lot   = trade["lotsize"]

        if direction == "LONG":
            pips = round((price - entry) / pip_v, 1)
        else:
            pips = round((entry - price) / pip_v, 1)
        pnl = round(pips * pip_e * lot, 2)

        closed = {
            **trade,
            "exit_price": round(price, 5),
            "closed_at":  fmt_brussels(),
            "closed_ts":  int(now_brussels().timestamp()),
            "pips":       pips,
            "pnl_eur":    pnl,
            "outcome":    "win" if pnl >= 0 else "loss",
        }
        with engine.lock:
            engine.open_trades   = [t for t in engine.open_trades if t["id"] != trade["id"]]
            engine.closed_trades.append(closed)
            engine.daily_pnl    += pnl

        engine.log("TRADE", f"✕ MANUEEL GESLOTEN {direction} {pair} @ {price:.5f} | {pips:+.1f} pips | €{pnl:+.2f}")
        closed_count += 1

    engine._save_state()
    return jsonify({"ok": True, "closed": closed_count})

@app.route("/api/engine/close_trade", methods=["POST"])
def engine_close_trade():
    """Sluit één specifieke trade op marktprijs."""
    d        = request.json or {}
    trade_id = int(d.get("id", 0))

    with engine.lock:
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
    if not trade:
        return jsonify({"ok": False, "error": "Trade niet gevonden"})

    pair  = trade["pair"]
    price = fetch_price(pair) or trade["entry_price"]
    pip_v = PIP.get(pair, 0.0001)
    pip_e = PIP_EUR.get(pair, 0.10)
    entry = trade["entry_price"]
    lot   = trade["lotsize"]

    if trade["direction"] == "LONG":
        pips = round((price - entry) / pip_v, 1)
    else:
        pips = round((entry - price) / pip_v, 1)
    pnl = round(pips * pip_e * lot, 2)

    closed = {
        **trade,
        "exit_price": round(price, 5),
        "closed_at":  fmt_brussels(),
        "closed_ts":  int(now_brussels().timestamp()),
        "pips":       pips,
        "pnl_eur":    pnl,
        "outcome":    "win" if pnl >= 0 else "loss",
        "close_reason": "manueel",
    }
    with engine.lock:
        engine.open_trades    = [t for t in engine.open_trades if t["id"] != trade_id]
        engine.closed_trades.append(closed)
        engine.daily_pnl     += pnl

    engine.log("TRADE", f"✕ MANUEEL GESLOTEN {trade['direction']} {pair} @ {price:.5f} | {pips:+.1f} pips | €{pnl:+.2f}")
    result_emoji = "✅" if pnl >= 0 else "❌"
    engine._discord(
        f"{result_emoji} **TRADE MANUEEL GESLOTEN — {pair}**\n"
        f"Richting: **{trade['direction']}** | Entry: `{entry:.5f}` → Exit: `{price:.5f}`\n"
        f"Pips: `{pips:+.1f}` | P&L: **€{pnl:+.2f}**\n"
        f"Dagelijks P&L: €{engine.daily_pnl:+.2f}",
        0x34d399 if pnl >= 0 else 0xf87171
    )
    engine._save_state()
    return jsonify({"ok": True, "pnl": pnl, "pips": pips})

@app.route("/api/engine/set_sl_tp", methods=["POST"])
def engine_set_sl_tp():
    """Stel SL en/of TP in voor een open trade."""
    d        = request.json or {}
    trade_id = int(d.get("id", 0))
    new_sl   = d.get("sl")   # None = niet wijzigen
    new_tp   = d.get("tp")   # None = niet wijzigen

    with engine.lock:
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
        if not trade:
            return jsonify({"ok": False, "error": "Trade niet gevonden"})
        if new_sl is not None:
            trade["sl"] = round(float(new_sl), 5)
        if new_tp is not None:
            trade["tp"] = round(float(new_tp), 5)

    sl_str = f"{trade['sl']:.5f}" if trade["sl"] else "—"
    tp_str = f"{trade['tp']:.5f}" if trade["tp"] else "—"
    engine.log("TRADE", f"⚙ SL/TP ingesteld #{trade_id} {trade['pair']} | SL:{sl_str} TP:{tp_str}")
    engine._discord(
        f"⚙️ **SL/TP Ingesteld — {trade['pair']}**\n"
        f"Trade #{trade_id} | {trade['direction']} @ `{trade['entry_price']:.5f}`\n"
        f"Stop Loss: `{sl_str}` | Take Profit: `{tp_str}`",
        0x7c3aed
    )
    engine._save_state()
    return jsonify({"ok": True, "sl": trade["sl"], "tp": trade["tp"]})

@app.route("/api/engine/start", methods=["POST"])
def engine_start():
    cfg = request.json or {}
    ok  = engine.start(cfg)
    return jsonify({"ok": ok, "running": engine.running})

@app.route("/api/engine/stop", methods=["POST"])
def engine_stop():
    engine.stop()
    return jsonify({"ok": True, "running": engine.running})

@app.route("/api/engine/status")
def engine_status():
    with engine.lock:
        open_trades   = list(engine.open_trades)
        closed_trades = list(engine.closed_trades)
        logs          = list(engine.logs[-100:])
    total  = len(closed_trades)
    wins   = sum(1 for t in closed_trades if t.get("outcome")=="win")
    tot_pnl= round(sum(t.get("pnl_eur",0) for t in closed_trades), 2)
    # Bereken engine uptime
    uptime_str = "—"
    if engine.start_ts and engine.running:
        secs = int(time.time() - engine.start_ts)
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{h}u {m:02d}m {s:02d}s"
    return jsonify({
        "running":         engine.running,
        "paused":          engine.paused,
        "scan_count":      engine.scan_count,
        "last_scan":       engine.last_scan,
        "start_time":      engine.start_time,
        "uptime":          uptime_str,
        "is_weekend":      engine._is_weekend(),
        "daily_pnl":       round(engine.daily_pnl, 2),
        "stopped_by_risk": engine.stopped_by_risk,
        "config":          engine.config,
        "open_trades":     open_trades,
        "closed_trades":   closed_trades,
        "logs":            logs,
        "stats": {
            "total":     total,
            "wins":      wins,
            "losses":    total - wins,
            "winrate":   round(wins/total*100, 1) if total > 0 else 0,
            "total_pnl": tot_pnl,
        }
    })

@app.route("/api/engine/clear", methods=["POST"])
def engine_clear():
    with engine.lock:
        engine.closed_trades = []
        engine.logs = []
    return jsonify({"ok": True})

@app.route("/api/engine/pause", methods=["POST"])
def engine_pause():
    ok = engine.pause()
    return jsonify({"ok": ok, "paused": engine.paused})

@app.route("/api/engine/resume", methods=["POST"])
def engine_resume():
    ok = engine.resume()
    return jsonify({"ok": ok, "paused": engine.paused})

# ─── CONFIG PRESETS ─────────────────────────────────────────────────
def _load_presets():
    try:
        import os
        if not os.path.exists(PRESETS_FILE): return {}
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {}

def _save_presets(presets):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
        return True
    except: return False

@app.route("/api/presets", methods=["GET"])
def api_presets_list():
    """Geeft alle opgeslagen presets terug."""
    return jsonify(_load_presets())

@app.route("/api/presets/save", methods=["POST"])
def api_presets_save():
    """Sla huidige config op als preset met een naam."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    cfg  = d.get("config", {})
    if not name: return jsonify({"ok": False, "error": "Naam is verplicht"})
    if not cfg:  return jsonify({"ok": False, "error": "Geen config meegegeven"})
    presets = _load_presets()
    presets[name] = {
        "config":  cfg,
        "saved_at": fmt_time_brussels(),
    }
    ok = _save_presets(presets)
    return jsonify({"ok": ok, "presets": presets})

@app.route("/api/presets/delete", methods=["POST"])
def api_presets_delete():
    """Verwijder een preset op naam."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    presets = _load_presets()
    if name in presets:
        del presets[name]
        _save_presets(presets)
    return jsonify({"ok": True, "presets": presets})

@app.route("/api/presets/start", methods=["POST"])
def api_presets_start():
    """Start de engine direct met een opgeslagen preset (handig voor mobile)."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    presets = _load_presets()
    if name not in presets:
        return jsonify({"ok": False, "error": f"Preset '{name}' niet gevonden"})
    cfg = presets[name]["config"]
    if engine.running:
        return jsonify({"ok": False, "error": "Engine draait al — stop eerst"})
    ok = engine.start(cfg)
    return jsonify({"ok": ok, "running": engine.running, "preset": name})

@app.route("/api/datasource")
def api_datasource():
    """Geeft terug welke datasource laatst is gebruikt per pair/tf.
    Overall status:
      - groen (TV)         : alle recente fetches via TradingView
      - oranje (yFinance)  : minstens één fallback naar yFinance
      - grijs (Geen data)  : geen recente fetches (< 5 min oud)
    """
    with DATA_SOURCE_LOCK:
        sources = dict(DATA_SOURCE)
    now = int(time.time())
    # Alleen entries van laatste 5 minuten meetellen voor de overall status
    recent = [s for s in sources.values() if now - s.get("ts", 0) < 300]
    if not recent:
        overall = {"status": "unknown", "label": "Geen data", "color": "#888"}
    elif all(s["source"] == "TV" for s in recent):
        overall = {"status": "tv", "label": "TradingView", "color": "#22c55e"}
    elif any("yFinance" in s["source"] for s in recent):
        overall = {"status": "yf", "label": "yFinance (TV down)", "color": "#f59e0b"}
    else:
        overall = {"status": "mixed", "label": "Gemengd", "color": "#f59e0b"}
    return jsonify({"overall": overall, "details": sources})

@app.route("/api/backtest",methods=["POST"])
def api_backtest():
    d    = request.json or {}
    pair = d.get("pair","EURUSD")
    tf   = d.get("tf","1H")
    kwargs = dict(
        start        = d.get("start"),
        end          = d.get("end"),
        capital      = float(d.get("capital",10000)),
        lotsize      = float(d.get("lotsize",1)),
        lotsize_eur  = float(d.get("lotsize_eur", d.get("lotsize",1))),
        lotsize_xau  = float(d.get("lotsize_xau", d.get("lotsize",1))),
        rr           = float(d.get("rr",2)),
        use_ob       = bool(d.get("use_ob",True)),
        use_trend    = bool(d.get("use_trend",True)),
        use_eq       = bool(d.get("use_eq",True)),
        min_score    = int(d.get("min_score",2)),
        use_session  = bool(d.get("use_session",False)),
        use_sweep    = bool(d.get("use_sweep",False)),
        be_trigger   = float(d.get("be_trigger",0.0)),
        spread_pips  = float(d.get("spread_pips",0.0)),
        slippage_pips= float(d.get("slippage_pips",0.0)),
        max_daily_loss= float(d.get("max_daily_loss",0)),
        max_trades    = int(d.get("max_trades",0)),
        max_risk_pct  = float(d.get("max_risk_pct",0)),
    )

    # Per-pair spread/slippage voor BOTH
    spread_xau   = d.get("spread_pips_xau")
    slippage_xau = d.get("slippage_pips_xau")

    # Bepaal welke pairs en timeframes we moeten runnen
    pairs = ["EURUSD","XAUUSD"] if pair=="BOTH" else [pair]
    tf_map = {
        "15M+1H": ["15M","1H"],
        "1H+4H":  ["1H","4H"],
        "ALL":    ["15M","1H","4H"],
    }
    timeframes = tf_map.get(tf, [tf])

    # Run voor elke combinatie van pair × timeframe
    all_trades  = []
    first_candles = None
    for p in pairs:
        # Gebruik pair-specifieke spread/slippage als BOTH
        pair_kwargs = dict(kwargs)
        if p == "XAUUSD" and spread_xau is not None:
            pair_kwargs["spread_pips"]   = float(spread_xau)
            pair_kwargs["slippage_pips"] = float(slippage_xau or 5)
        for t in timeframes:
            r = run_backtest(p, tf=t, **pair_kwargs)
            if r.get("trades"):
                all_trades.extend(r["trades"])
            if first_candles is None and r.get("candles"):
                first_candles = r["candles"]

    # Renummer trades chronologisch
    all_trades.sort(key=lambda x: x.get("entry_ts",0))
    for i, t in enumerate(all_trades, 1):
        t["id"] = i

    if not all_trades:
        return jsonify({"error":"Geen setups gevonden.","trades":[],"stats":{},"candles":first_candles or []})

    # Gecombineerde stats
    def merge_stats(trades):
        wins   = [t for t in trades if t["outcome"]=="win"]
        losses = [t for t in trades if t["outcome"]=="loss"]
        bes    = [t for t in trades if t["outcome"]=="be"]
        pnls   = [t["pnl_eur"] for t in trades]
        pipsl  = [t["pips"] for t in trades]
        decided= len(wins)+len(losses)
        return {
            "total":     len(trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "be":        len(bes),
            "winrate":   round(len(wins)/decided*100,1) if decided>0 else 0,
            "total_pnl": round(sum(pnls),2) if pnls else 0,
            "best":      round(max(pnls),2) if pnls else 0,
            "worst":     round(min(pnls),2) if pnls else 0,
            "avg_pips":  round(sum(pipsl)/len(pipsl),1) if pipsl else 0,
        }

    return jsonify({"trades":all_trades,"stats":merge_stats(all_trades),"candles":first_candles or []})

@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    d            = request.json or {}
    start        = d.get("start")
    end          = d.get("end")
    spread_pips  = float(d.get("spread_pips", 1.5))
    slippage_pips= float(d.get("slippage_pips", 0.5))
    capital      = float(d.get("capital", 10000))
    lotsize      = float(d.get("lotsize", 1))
    lotsize_eur  = float(d.get("lotsize_eur", lotsize))
    lotsize_xau  = float(d.get("lotsize_xau", lotsize))
    use_sweep    = bool(d.get("use_sweep", False))

    # Splits datum in 70% in-sample / 30% out-of-sample
    from datetime import datetime, timedelta
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end,   "%Y-%m-%d")
    total_days = (e - s).days
    split_days = int(total_days * 0.7)
    split_date = (s + timedelta(days=split_days)).strftime("%Y-%m-%d")

    # Grid van alle combinaties
    pairs      = ["EURUSD", "XAUUSD"]
    timeframes = ["1H", "4H", "1H+4H"]
    rr_values  = [1.5, 2.0, 2.5]
    scores     = [2]           # score 3 geeft te weinig trades
    ob_opts    = [True, False]
    trend_opts = [False]
    eq_opts    = [True, False]
    kz_opts    = [False]       # KZ op 4H irrelevant
    be_opts    = [0.0]         # BE verdubbelt combinaties, weinig winst

    results = []
    combo_id = 0

    for pair in pairs:
        for tf in timeframes:
            for rr in rr_values:
                for score in scores:
                    for use_ob in ob_opts:
                        for use_eq in eq_opts:
                            for use_kz in kz_opts:
                                for be in be_opts:
                                    combo_id += 1
                                    kwargs = dict(
                                        start        = start,
                                        end          = split_date,
                                        capital      = capital,
                                        lotsize      = lotsize,
                                        lotsize_eur  = lotsize_eur,
                                        lotsize_xau  = lotsize_xau,
                                        rr           = rr,
                                        use_ob       = use_ob,
                                        use_trend    = False,
                                        use_eq       = use_eq,
                                        min_score    = score,
                                        use_session  = use_kz,
                                        use_sweep    = use_sweep,
                                        be_trigger   = be,
                                        spread_pips  = spread_pips,
                                        slippage_pips= slippage_pips,
                                    )

                                    # In-sample run
                                    tfs = {"1H+4H":["1H","4H"]}.get(tf,[tf])
                                    is_trades = []
                                    for t in tfs:
                                        r = run_backtest(pair, tf=t, **kwargs)
                                        is_trades.extend(r.get("trades",[]))

                                    if len(is_trades) < 5:
                                        continue

                                    is_stats = _calc_stats(is_trades)
                                    if is_stats["winrate"] < 45 or is_stats["total_pnl"] <= 0:
                                        continue

                                    # Out-of-sample validatie
                                    oos_kwargs = {**kwargs, "start": split_date, "end": end}
                                    oos_trades = []
                                    for t in tfs:
                                        r2 = run_backtest(pair, tf=t, **oos_kwargs)
                                        oos_trades.extend(r2.get("trades",[]))

                                    oos_stats = _calc_stats(oos_trades) if oos_trades else {"winrate":0,"total_pnl":0,"total":0}

                                    results.append({
                                        "id":        combo_id,
                                        "pair":      pair,
                                        "tf":        tf,
                                        "rr":        rr,
                                        "score":     score,
                                        "ob":        use_ob,
                                        "eq":        use_eq,
                                        "kz":        use_kz,
                                        "be":        be,
                                        "is_trades": is_stats["total"],
                                        "is_wr":     is_stats["winrate"],
                                        "is_pnl":    is_stats["total_pnl"],
                                        "oos_trades":oos_stats["total"],
                                        "oos_wr":    oos_stats["winrate"],
                                        "oos_pnl":   oos_stats["total_pnl"],
                                        # Score: combinatie van beide periodes
                                        "score_val": (is_stats["winrate"] * 0.4 +
                                                      oos_stats["winrate"] * 0.6 +
                                                      (1 if oos_stats["total_pnl"] > 0 else -10)),
                                    })

    # Sorteer op score (out-of-sample zwaarder gewogen)
    results.sort(key=lambda x: x["score_val"], reverse=True)
    return jsonify({
        "results":    results[:15],
        "total_tested": combo_id,
        "split_date": split_date,
        "in_sample":  f"{start} → {split_date}",
        "out_sample": f"{split_date} → {end}",
    })

def _calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"winrate":0,"total_pnl":0}
    wins   = [t for t in trades if t["outcome"]=="win"]
    losses = [t for t in trades if t["outcome"]=="loss"]
    pnls   = [t["pnl_eur"] for t in trades]
    decided= len(wins)+len(losses)
    return {
        "total":     len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "winrate":   round(len(wins)/decided*100,1) if decided>0 else 0,
        "total_pnl": round(sum(pnls),2),
    }


HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GITCHI</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<script src="/static/lw-charts.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#04020f;
  --bg2:#080518;
  --bg3:#0d0820;
  --bg4:#110c28;
  --border:#2d1f5e;
  --border2:#3d2b7a;
  --glow:#7c3aed;
  --glow2:#a78bfa;
  --glow3:#c4b5fd;
  --text:#e8e0ff;
  --text2:#a89bcc;
  --text3:#5a4e80;
  --green:#34d399;
  --green-d:#064e3b;
  --red:#f87171;
  --red-d:#450a0a;
  --amber:#fbbf24;
  --r:10px;
}
html,body{width:100%;height:100%;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;overflow-x:hidden}

/* Animated background */
body::before{
  content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse 90% 60% at 50% -10%,rgba(124,58,237,.35) 0%,transparent 65%),
             radial-gradient(ellipse 50% 40% at 85% 90%,rgba(167,139,250,.2) 0%,transparent 55%),
             radial-gradient(ellipse 40% 30% at 5% 70%,rgba(124,58,237,.15) 0%,transparent 50%);
  pointer-events:none;z-index:0;
  animation:pulse-bg 4s ease-in-out infinite;
}
@keyframes pulse-bg{
  0%,100%{opacity:1;filter:brightness(1)}
  50%{opacity:.7;filter:brightness(1.4)}
}

/* Scanlines */
body::after{
  content:'';position:fixed;inset:0;
  background:repeating-linear-gradient(0deg,rgba(124,58,237,.025) 0px,rgba(124,58,237,.025) 1px,transparent 1px,transparent 4px);
  pointer-events:none;z-index:1;
}

#app-wrap{position:relative;z-index:2}

/* ── TOPBAR ── */
#topbar{
  height:52px;display:flex;align-items:center;gap:20px;padding:0 20px;
  background:rgba(8,5,24,.85);
  border-bottom:1px solid var(--border);
  backdrop-filter:blur(20px);
  position:sticky;top:0;z-index:50;
  box-shadow:0 0 40px rgba(124,58,237,.15);
}
.logo{
  font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:600;
  color:var(--glow3);letter-spacing:2px;
  text-shadow:0 0 20px var(--glow),0 0 40px rgba(124,58,237,.5);
  animation:pulse-logo 4s ease-in-out infinite;
}
@keyframes pulse-logo{0%,100%{text-shadow:0 0 20px var(--glow),0 0 40px rgba(124,58,237,.5)}50%{text-shadow:0 0 30px var(--glow2),0 0 60px rgba(167,139,250,.7)}}

.tabs{display:flex;gap:2px;background:rgba(13,8,32,.8);padding:4px;border-radius:6px;border:1px solid var(--border)}
.tab-btn{padding:5px 16px;border-radius:4px;border:none;background:transparent;color:var(--text2);font-family:'Inter',sans-serif;font-size:12px;font-weight:500;cursor:pointer;transition:.2s;letter-spacing:.5px}
.tab-btn.active{background:rgba(124,58,237,.3);color:var(--glow3);box-shadow:0 0 12px rgba(124,58,237,.3);border:1px solid var(--border2)}

.topbar-right{margin-left:auto;display:flex;align-items:center;gap:12px}
#topbar-session{font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px;background:rgba(124,58,237,.15);color:var(--glow2);border:1px solid var(--border);letter-spacing:.5px}
#topbar-session.kz{background:rgba(124,58,237,.3);color:var(--glow3);border-color:var(--glow);box-shadow:0 0 12px rgba(124,58,237,.4);animation:kz-pulse 2s ease infinite}
@keyframes kz-pulse{0%,100%{box-shadow:0 0 8px rgba(124,58,237,.4)}50%{box-shadow:0 0 20px rgba(124,58,237,.8)}}
#topbar-price{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:600;color:var(--glow3);text-shadow:0 0 10px rgba(124,58,237,.6)}

.refresh-btn{display:flex;align-items:center;gap:5px;padding:5px 12px;border:1px solid var(--border2);border-radius:6px;background:rgba(124,58,237,.1);color:var(--glow2);font-size:12px;font-weight:500;cursor:pointer;font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.5px}
.refresh-btn:hover{border-color:var(--glow2);background:rgba(124,58,237,.2);box-shadow:0 0 12px rgba(124,58,237,.3)}

/* ── LAYOUT ── */
#app{display:grid;grid-template-columns:minmax(0,1fr) 480px;gap:14px;padding:14px;position:relative;z-index:2}
#left{display:flex;flex-direction:column;gap:14px;min-width:0;overflow-y:auto;overflow-x:hidden;max-height:calc(100vh - 76px)}
#left::-webkit-scrollbar{width:3px}
#left::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}
#right{display:flex;flex-direction:column;gap:10px;overflow-y:auto;overflow-x:hidden;max-height:calc(100vh - 76px);padding-right:4px}
#right::-webkit-scrollbar{width:5px}
#right::-webkit-scrollbar-track{background:rgba(124,58,237,.08);border-radius:99px}
#right::-webkit-scrollbar-thumb{background:var(--glow);border-radius:99px}
#right::-webkit-scrollbar-thumb:hover{background:var(--glow2)}

/* ── ANALYSIS SIDEBAR ── */
#analysis-sidebar{
  position:fixed;top:0;right:-520px;width:520px;height:100vh;
  background:rgba(8,5,24,.98);border-left:1px solid var(--border2);
  z-index:200;transition:right .35s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;
  backdrop-filter:blur(24px);box-shadow:-12px 0 60px rgba(124,58,237,.25);
}
#analysis-sidebar.open{right:0}
#sidebar-header{
  padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(124,58,237,.06);flex-shrink:0;
}
.sidebar-title{font-size:12px;font-weight:700;color:var(--glow3);letter-spacing:1px;text-transform:uppercase;display:flex;align-items:center;gap:8px}
#sidebar-close{width:28px;height:28px;border-radius:50%;border:1px solid var(--border2);background:transparent;color:var(--text2);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;transition:.15s;line-height:1}
#sidebar-close:hover{background:rgba(248,113,113,.15);color:var(--red);border-color:var(--red)}
#sidebar-content{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}
#sidebar-content::-webkit-scrollbar{width:4px}
#sidebar-content::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}
#sidebar-overlay{position:fixed;inset:0;background:rgba(4,2,15,.6);z-index:199;display:none;backdrop-filter:blur(3px);cursor:pointer}
#sidebar-overlay.open{display:block}
.analysis-trigger-btn{
  display:flex;align-items:center;gap:5px;padding:4px 10px;
  border:1px solid var(--border2);border-radius:5px;
  background:rgba(124,58,237,.1);color:var(--glow2);
  font-size:11px;font-weight:500;cursor:pointer;
  font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.3px;
}
.analysis-trigger-btn:hover{border-color:var(--glow);background:rgba(124,58,237,.2);box-shadow:0 0 10px rgba(124,58,237,.3)}

/* ── CARD ── */
.card{
  background:rgba(8,5,24,.7);
  border:1px solid var(--border);
  border-radius:var(--r);
  backdrop-filter:blur(12px);
  overflow:hidden;
  transition:border-color .3s;
}
.card:hover{border-color:var(--border2)}
.card-header{
  padding:12px 16px 8px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  background:rgba(124,58,237,.04);
}
.card-title{font-size:12px;font-weight:600;color:var(--glow3);letter-spacing:1px;text-transform:uppercase;display:flex;align-items:center;gap:7px}
.card-dot{width:6px;height:6px;background:var(--glow);border-radius:50%;box-shadow:0 0 8px var(--glow);animation:dot-pulse 3s ease infinite}
@keyframes dot-pulse{0%,100%{box-shadow:0 0 6px var(--glow)}50%{box-shadow:0 0 14px var(--glow2)}}
.card-body{padding:14px 16px}

/* ── CHART TOOLBAR ── */
.chart-toolbar{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap;background:rgba(124,58,237,.03)}
.btn-group{display:flex;gap:1px;background:var(--border);border-radius:5px;overflow:hidden;border:1px solid var(--border)}
.toggle-btn{padding:4px 11px;border:none;background:var(--bg3);color:var(--text2);font-family:'Inter',sans-serif;font-size:11px;font-weight:500;cursor:pointer;transition:.15s;letter-spacing:.5px}
.toggle-btn.active{background:rgba(124,58,237,.35);color:var(--glow3);box-shadow:inset 0 0 10px rgba(124,58,237,.2)}
.toggle-btn:hover:not(.active){background:var(--bg4);color:var(--text)}
.chart-status{font-size:11px;color:var(--text3);margin-left:auto;font-family:'JetBrains Mono',monospace;letter-spacing:.5px}

.chart-wrap{height:260px;position:relative}
#chart{width:100%;height:100%}

/* ── WINRATE CARD ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat-item{background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:8px;padding:11px 12px;text-align:center;transition:.2s}
.stat-item:hover{border-color:var(--border2);box-shadow:0 0 16px rgba(124,58,237,.15)}
.stat-item .lbl{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px}
.stat-item .val{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:var(--glow3);line-height:1;text-shadow:0 0 10px rgba(124,58,237,.5)}
.stat-item .sub{font-size:10px;color:var(--text3);margin-top:2px}
.stat-item.green .val{color:var(--green);text-shadow:0 0 10px rgba(52,211,153,.4)}
.stat-item.red .val{color:var(--red);text-shadow:0 0 10px rgba(248,113,113,.4)}

.progress-wrap{margin-top:12px}
.progress-label{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:5px;letter-spacing:.5px}
.progress-bar{height:4px;background:rgba(124,58,237,.1);border-radius:99px;overflow:hidden;border:1px solid var(--border)}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--glow),var(--glow2));border-radius:99px;transition:width .8s ease;box-shadow:0 0 8px var(--glow)}

/* ── BIAS PANEL ── */
.bias-score-box{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:8px;margin-bottom:12px;border:1px solid var(--border);background:rgba(124,58,237,.06);transition:.3s}
.bias-big{font-family:'JetBrains Mono',monospace;font-size:34px;font-weight:700;line-height:1;min-width:46px;text-align:center}
.bias-verdict{font-size:13px;font-weight:600}
.bias-advice{font-size:11px;color:var(--text2);margin-top:2px}

.judge-row{display:grid;grid-template-columns:80px 1fr 22px;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(45,31,94,.5)}
.judge-row:last-child{border-bottom:none}
.judge-name{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.judge-bar-bg{background:rgba(124,58,237,.08);border-radius:99px;height:3px;border:none}
.judge-bar{height:100%;border-radius:99px;transition:width .4s}
.judge-val{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;text-align:right}
.judge-detail{grid-column:2/-1;font-size:9px;color:var(--text3);margin-top:1px;letter-spacing:.3px}

.pill{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;letter-spacing:.5px}
.pill-green{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}
.pill-red{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
.pill-amber{background:rgba(251,191,36,.1);color:var(--amber);border:1px solid rgba(251,191,36,.2)}
.pill-purple{background:rgba(124,58,237,.15);color:var(--glow2);border:1px solid var(--border2)}
.pill-gray{background:rgba(90,78,128,.1);color:var(--text3);border:1px solid var(--border)}

.ote-box{background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-top:12px}
.ote-title{font-size:9px;font-weight:700;color:var(--glow);text-transform:uppercase;letter-spacing:1px;margin-bottom:7px}
.ote-row{display:flex;justify-content:space-between;font-size:11px;color:var(--text2);margin-bottom:3px;font-family:'JetBrains Mono',monospace}
.ote-row span:last-child{color:var(--glow3)}

/* ── FORM ── */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.form-group{display:flex;flex-direction:column;gap:3px}
.form-group label{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.form-group input,.form-group select{
  padding:7px 9px;border:1px solid var(--border2);border-radius:5px;
  font-family:'Inter',sans-serif;font-size:12px;color:var(--text);
  background:rgba(13,8,32,.95);transition:.15s;outline:none;
  color-scheme:dark;
}
.form-group input:focus,.form-group select:focus{border-color:var(--glow2);box-shadow:0 0 0 3px rgba(124,58,237,.2)}
.form-group select option{background:#0d0820;color:var(--text)}
.form-group input[type="number"]::-webkit-inner-spin-button,
.form-group input[type="number"]::-webkit-outer-spin-button{
  filter:invert(1) hue-rotate(200deg);opacity:.6;
}
.form-group input[type="date"]::-webkit-calendar-picker-indicator{
  filter:invert(1) hue-rotate(200deg);opacity:.6;cursor:pointer;
}

/* Toggle switches */
.toggle-section{margin-top:10px;padding:10px 12px;background:rgba(124,58,237,.04);border:1px solid var(--border);border-radius:8px}
.toggle-section-title{font-size:10px;font-weight:700;color:var(--glow2);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:4px 0}
.toggle-label{font-size:11px;color:var(--text2)}
.toggle-label small{display:block;font-size:9px;color:var(--text3);margin-top:1px}
.switch{position:relative;width:36px;height:20px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:rgba(45,31,94,.8);border-radius:20px;cursor:pointer;transition:.3s;border:1px solid var(--border2)}
.slider::before{content:'';position:absolute;width:14px;height:14px;left:2px;top:2px;background:var(--text3);border-radius:50%;transition:.3s}
.switch input:checked+.slider{background:rgba(124,58,237,.4);border-color:var(--glow);box-shadow:0 0 8px rgba(124,58,237,.4)}
.switch input:checked+.slider::before{transform:translateX(16px);background:var(--glow3)}

.btn-primary{width:100%;margin-top:12px;padding:10px;border:1px solid var(--glow);border-radius:6px;background:rgba(124,58,237,.2);color:var(--glow3);font-family:'Inter',sans-serif;font-size:13px;font-weight:600;cursor:pointer;letter-spacing:1px;transition:.2s;box-shadow:0 0 16px rgba(124,58,237,.2)}
.btn-primary:hover{background:rgba(124,58,237,.35);box-shadow:0 0 24px rgba(124,58,237,.4)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto;overflow-y:auto;max-height:220px;margin-top:12px}
.tbl-wrap::-webkit-scrollbar{width:3px;height:3px}
.tbl-wrap::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}
table{width:100%;border-collapse:collapse;font-size:11px}
thead th{padding:7px 9px;text-align:left;font-size:9px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;background:rgba(124,58,237,.06);border-bottom:1px solid var(--border);white-space:nowrap}
tbody td{padding:7px 9px;border-bottom:1px solid rgba(45,31,94,.4);color:var(--text2);font-family:'JetBrains Mono',monospace;font-size:10px;white-space:nowrap}
tbody tr:hover{background:rgba(124,58,237,.06)}
tbody tr:last-child td{border-bottom:none}
.win{color:var(--green);font-weight:700}
.loss{color:var(--red);font-weight:700}

/* ── BT STATS ── */
.bt-stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}
.bt-stat{background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:7px;padding:10px;text-align:center}
.bt-stat .l{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.bt-stat .v{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:700}

.best-worst{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.bw-box{padding:8px 10px;border-radius:7px;font-size:11px;font-family:'JetBrains Mono',monospace}
.bw-box .ttl{font-size:9px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;opacity:.7}

.csv-btn{font-size:10px;padding:4px 10px;border:1px solid var(--border2);border-radius:4px;background:transparent;color:var(--text2);cursor:pointer;font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.5px}
.csv-btn:hover{border-color:var(--glow2);color:var(--glow2)}

.spinner{width:14px;height:14px;border:2px solid rgba(124,58,237,.3);border-top-color:var(--glow2);border-radius:50%;animation:spin .6s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}

::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}

/* ── CALENDAR ── */
.cal-event{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid rgba(45,31,94,.4)}
.cal-event:last-child{border-bottom:none}
.cal-date{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);min-width:70px;line-height:1.4}
.cal-time{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--glow2);font-weight:600}
.cal-name{font-size:11px;color:var(--text);font-weight:500;line-height:1.3}
.cal-note{font-size:10px;color:var(--text3);margin-top:1px}
.cal-badge{padding:1px 6px;border-radius:3px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;flex-shrink:0;margin-top:1px}
.cal-high{background:rgba(248,113,113,.15);color:var(--red);border:1px solid rgba(248,113,113,.25)}
.cal-medium{background:rgba(251,191,36,.1);color:var(--amber);border:1px solid rgba(251,191,36,.2)}
.cal-warning{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:11px;color:var(--red);display:flex;align-items:center;gap:6px}

/* ── MULTIBIAS ── */
.tf-bias-row{display:grid;grid-template-columns:36px 1fr auto;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid rgba(45,31,94,.4)}
.tf-bias-row:last-child{border-bottom:none}
.tf-label{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--glow2)}
.tf-bars{display:flex;flex-direction:column;gap:3px}
.tf-bar-row{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--text3)}
.tf-mini-bar{height:3px;border-radius:99px;transition:width .4s}
.tf-verdict{font-size:11px;font-weight:600;text-align:right;min-width:80px;font-family:'JetBrains Mono',monospace}
.align-badge{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border-radius:7px;margin-bottom:12px;font-size:12px;font-weight:600;letter-spacing:.5px;border:1px solid}

/* ── MODAL ── */
.modal-overlay{position:fixed;inset:0;background:rgba(4,2,15,.85);z-index:1000;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px)}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:min(900px,95vw);max-height:90vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 0 60px rgba(124,58,237,.3)}
.modal-header{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-size:13px;font-weight:600;color:var(--glow3);letter-spacing:1px;text-transform:uppercase}
.modal-close{width:28px;height:28px;border-radius:50%;border:1px solid var(--border2);background:transparent;color:var(--text2);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;transition:.15s}
.modal-close:hover{background:rgba(248,113,113,.15);color:var(--red);border-color:var(--red)}
.modal-body{padding:16px;overflow-y:auto;flex:1}
.modal-chart{height:320px;border-radius:8px;overflow:hidden;border:1px solid var(--border)}
.modal-details{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.modal-stat{background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:7px;padding:10px;text-align:center}
.modal-stat .l{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.modal-stat .v{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700}
</style>
</head>
<body>
<div id="app-wrap">

<div id="topbar">
  <div class="logo">我慢 <span style="font-size:11px;letter-spacing:1px;opacity:.7">GAMAN</span></div>
  <div class="tabs">
    <button class="tab-btn active" onclick="switchPage('live')">Live Trading</button>
    <button class="tab-btn" onclick="switchPage('backtest')">Backtester</button>
    <button class="tab-btn" onclick="switchPage('system')">System</button>
  </div>
  <div class="topbar-right">
    <div id="topbar-session">—</div>
    <div id="topbar-price">—</div>
    <div id="refresh-countdown" style="font-size:10px;color:var(--text3);font-family:'JetBrains Mono',monospace;min-width:30px">20s</div>
    <button class="analysis-trigger-btn" id="sidebar-open-btn" onclick="openSidebar()">
      <i data-lucide="bar-chart-2" style="width:13px;height:13px"></i> Analyse
    </button>
    <button class="refresh-btn" id="main-refresh" onclick="loadAll()">
      <i id="ri" data-lucide="refresh-cw" style="width:13px;height:13px"></i> Refresh
    </button>
  </div>
</div>

<!-- WEEKEND BANNER -->
<div id="weekend-banner" style="display:none;background:linear-gradient(90deg,rgba(248,113,113,.15),rgba(124,58,237,.1));border-bottom:1px solid rgba(248,113,113,.3);padding:8px 24px;text-align:center;font-size:12px;font-weight:600;color:var(--red);letter-spacing:1px;z-index:49;position:relative">
  <i data-lucide="moon" style="width:13px;height:13px;vertical-align:middle;margin-right:6px"></i>
  MARKT GESLOTEN — Weekend &nbsp;·&nbsp; Geen nieuwe trades mogelijk &nbsp;·&nbsp; Opent zondag 23:00 Brussels
</div>
<div id="market-open-banner" style="display:none;background:linear-gradient(90deg,rgba(52,211,153,.1),rgba(124,58,237,.08));border-bottom:1px solid rgba(52,211,153,.2);padding:6px 24px;text-align:center;font-size:12px;font-weight:600;color:var(--green);letter-spacing:1px;z-index:49;position:relative">
  <i data-lucide="activity" style="width:13px;height:13px;vertical-align:middle;margin-right:6px"></i>
  MARKT OPEN &nbsp;·&nbsp; Forex sessies actief
</div>

<!-- ANALYSIS SIDEBAR OVERLAY -->
<div id="sidebar-overlay" onclick="closeSidebar()"></div>

<!-- ANALYSIS SIDEBAR -->
<div id="analysis-sidebar">
  <div id="sidebar-header">
    <div class="sidebar-title"><div class="card-dot"></div>ICT Analyse Panel</div>
    <button id="sidebar-close" onclick="closeSidebar()">✕</button>
  </div>
  <div id="sidebar-content">

    <!-- ICT BIAS JUDGE -->
    <div class="card">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>ICT Bias Judge</div></div>
      <div class="card-body" style="max-height:320px;overflow-y:auto">
        <div class="bias-score-box" id="bias-box">
          <div class="bias-big" id="bias-num">—</div>
          <div><div class="bias-verdict" id="bias-vt">Laden...</div><div class="bias-advice" id="bias-adv">Klik Refresh</div></div>
        </div>
        <div id="judges">
          <div class="judge-row"><div class="judge-name">P/D Zone</div><div class="judge-bar-bg"><div class="judge-bar" id="j1b" style="width:50%"></div></div><div class="judge-val" id="j1v">—</div><div class="judge-detail" id="j1d"></div></div>
          <div class="judge-row"><div class="judge-name">DOL</div><div class="judge-bar-bg"><div class="judge-bar" id="j2b" style="width:50%"></div></div><div class="judge-val" id="j2v">—</div><div class="judge-detail" id="j2d"></div></div>
          <div class="judge-row"><div class="judge-name">PO3 Open</div><div class="judge-bar-bg"><div class="judge-bar" id="j3b" style="width:50%"></div></div><div class="judge-val" id="j3v">—</div><div class="judge-detail" id="j3d"></div></div>
          <div class="judge-row"><div class="judge-name">BOS Filter</div><div id="struct-pill"></div><div></div></div>
        </div>
        <div class="ote-box">
          <div class="ote-title">OTE Zone (62–79%)</div>
          <div class="ote-row"><span>Low 62%</span><span id="ote-l">—</span></div>
          <div class="ote-row"><span>Sweet 70.5%</span><span id="ote-m">—</span></div>
          <div class="ote-row"><span>High 79%</span><span id="ote-h">—</span></div>
          <div class="ote-row" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)"><span>Equilibrium</span><span id="ote-eq">—</span></div>
        </div>
      </div>
    </div>

    <!-- MULTI-TF BIAS -->
    <div class="card">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Multi-TF Bias</div>
        <span id="align-label" style="font-size:10px;letter-spacing:.5px"></span>
      </div>
      <div class="card-body" style="max-height:260px;overflow-y:auto">
        <div class="align-badge" id="align-badge"><span id="align-text">Laden...</span></div>
        <div id="mtf-rows"></div>
      </div>
    </div>

    <!-- ECONOMISCHE KALENDER -->
    <div class="card">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Economische Kalender</div>
        <span id="cal-month" style="font-size:10px;color:var(--text3)"></span>
      </div>
      <div class="card-body" style="max-height:260px;overflow-y:auto">
        <div id="cal-warning"></div>
        <div id="cal-events"><div style="color:var(--text3);font-size:11px;padding:8px 0">Laden...</div></div>
      </div>
    </div>

  </div>
</div>

<div id="app">
  <!-- LEFT -->
  <div id="left">
    <!-- CHART -->
    <div class="card">
      <div class="chart-toolbar">
        <div class="btn-group" id="pg">
          <button class="toggle-btn active" onclick="setPair('EURUSD')">EUR/USD</button>
          <button class="toggle-btn" onclick="setPair('XAUUSD')">XAU/USD</button>
        </div>
        <div class="btn-group" id="tg">
          <button class="toggle-btn active" onclick="setTF('15M')">15M</button>
          <button class="toggle-btn" onclick="setTF('1H')">1H</button>
          <button class="toggle-btn" onclick="setTF('4H')">4H</button>
        </div>
        <span id="chart-status" class="chart-status"></span>
      </div>
      <div class="chart-wrap"><div id="chart"></div></div>
    </div>

    <!-- OPEN POSITIES (live trading) -->
    <div class="card" id="live-open-card">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Open Posities</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="data-source-badge" style="font-size:10px;padding:3px 10px;border-radius:20px;background:rgba(90,78,128,.2);color:var(--text3);border:1px solid var(--border);display:inline-flex;align-items:center;gap:6px" title="Welke databron wordt momenteel gebruikt">
            <span id="ds-dot" style="width:7px;height:7px;border-radius:50%;background:#888;display:inline-block"></span>
            <span id="ds-label">Data: —</span>
          </span>
          <span id="engine-status-badge" style="font-size:10px;padding:3px 10px;border-radius:20px;background:rgba(90,78,128,.2);color:var(--text3);border:1px solid var(--border)">● GESTOPT</span>
          <span id="engine-scan-info" style="font-size:10px;color:var(--text3)"></span>
        </div>
      </div>
      <div class="card-body" style="padding:10px 16px">
        <div class="tbl-wrap" style="max-height:220px">
          <table>
            <thead><tr><th>#</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Live Prijs</th><th>Live P&L</th><th>SL instellen</th><th>TP instellen</th><th>Filters</th><th>Actie</th></tr></thead>
            <tbody id="live-open-tbody">
              <tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">Geen open posities</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- LIVE STATS BAR -->
    <div class="card" id="live-stats-card">
      <div class="card-body" style="padding:10px 16px">
        <div class="stat-grid">
          <div class="stat-item purple"><div class="lbl">Trades</div><div class="val" id="lt-total">0</div><div class="sub">deze sessie</div></div>
          <div class="stat-item"><div class="lbl">Winrate</div><div class="val" id="lt-wr">—</div><div class="sub" id="lt-wl">0W / 0L</div></div>
          <div class="stat-item" id="lt-pnl-card"><div class="lbl">Totaal P&L</div><div class="val" id="lt-pnl">€0.00</div><div class="sub">deze sessie</div></div>
          <div class="stat-item"><div class="lbl">Scans</div><div class="val" id="lt-scans">0</div><div class="sub" id="lt-last-scan">laatste: —</div></div>
        </div>
      </div>
    </div>

    <!-- TRADE LOG -->
    <div class="card" id="live-log-card">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Trade Log</div>
        <div style="display:flex;gap:6px">
          <button class="csv-btn" onclick="exportLiveCSV()">↓ CSV</button>
          <button class="csv-btn" onclick="clearLiveLog()">wis</button>
        </div>
      </div>
      <div class="card-body" style="padding:10px 16px">
        <div class="tbl-wrap" style="max-height:200px">
          <table>
            <thead><tr><th>#</th><th>Geopend</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Pips</th><th>P&L €</th><th>Score</th></tr></thead>
            <tbody id="live-closed-tbody">
              <tr><td colspan="9" style="text-align:center;padding:16px;color:var(--text3)">Nog geen gesloten trades</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ENGINE LOG -->
    <div class="card" id="live-engine-log-card">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Engine Log</div>
        <div style="display:flex;gap:6px">
          <button class="csv-btn" onclick="exportEngineLogCSV()">↓ Log CSV</button>
          <button class="csv-btn" onclick="exportLiveCSV()">↓ Trades CSV</button>
        </div>
      </div>
      <div id="engine-log-list" style="padding:8px 16px;font-family:'JetBrains Mono',monospace;font-size:10px;max-height:180px;overflow-y:auto;color:var(--text2)">
        <div style="color:var(--text3);padding:8px 0">Engine nog niet gestart...</div>
      </div>
    </div>

    <!-- OPTIMIZER RESULTS -->
    <div class="card" id="opt-card" style="display:none">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Optimizer Resultaten</div>
        <span id="opt-meta" style="font-size:10px;color:var(--text3)"></span>
      </div>
      <div class="card-body">
        <!-- Uitleg -->
        <div style="background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:11px;color:var(--text2);line-height:1.6">
          <strong style="color:var(--glow2)">Hoe lezen?</strong><br>
          <span style="color:var(--green)">IS</span> = In-Sample (70% van je periode, gebruikt om te leren)<br>
          <span style="color:var(--amber)">OOS</span> = Out-of-Sample (30% apart gehouden, nooit gezien)<br>
          Een goede config heeft <strong>beide</strong> groen. Enkel IS groen = overfitting.
        </div>
        <div id="opt-summary" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px"></div>
        <div class="tbl-wrap" style="max-height:360px">
          <table id="opt-table">
            <thead><tr>
              <th>#</th><th>Pair</th><th>TF</th><th>RR</th><th>OB</th><th>EQ</th><th>KZ</th><th>BE</th>
              <th style="color:var(--green)">IS Trades</th>
              <th style="color:var(--green)">IS WR%</th>
              <th style="color:var(--green)">IS P&L</th>
              <th style="color:var(--amber)">OOS Trades</th>
              <th style="color:var(--amber)">OOS WR%</th>
              <th style="color:var(--amber)">OOS P&L</th>
              <th>Gebruik</th>
            </tr></thead>
            <tbody id="opt-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- SYSTEM TAB -->
    <div id="system-panel" style="display:none">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><div class="card-dot"></div>System Health</div>
          <button onclick="runSystemCheck()" style="font-size:10px;padding:3px 10px;border:1px solid var(--border2);border-radius:4px;background:rgba(124,58,237,.1);color:var(--glow2);cursor:pointer;font-family:'Inter',sans-serif;display:flex;align-items:center;gap:4px">
            <i data-lucide="refresh-cw" style="width:11px;height:11px"></i> Hercheck
          </button>
        </div>
        <div class="card-body">
          <div id="sys-checks" style="display:flex;flex-direction:column;gap:8px">
            <div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Klik Hercheck om te starten...</div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:0">
        <div class="card-header"><div class="card-title"><div class="card-dot"></div>System Info</div></div>
        <div class="card-body" id="sys-info">
          <div style="color:var(--text3);font-size:12px">Laden...</div>
        </div>
      </div>
    </div>

    <!-- BT RESULTS -->
    <div class="card" id="bt-results" style="display:none">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Backtest Resultaten</div>
        <button class="csv-btn" onclick="exportCSV()">↓ CSV</button>
      </div>
      <div class="card-body">
        <div class="bt-stat-grid" id="bt-sg"></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>Datum</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>SL</th><th>TP</th><th>Pips</th><th>P&L €</th><th>Score</th><th>Sessie</th></tr></thead>
            <tbody id="bt-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- RIGHT -->
  <div id="right">
    <!-- LIVE ENGINE CONFIG -->
    <div class="card" id="live-engine-config">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>Live Trading Config</div></div>
      <div class="card-body">
        <div class="form-grid">
          <div class="form-group"><label>Pair</label>
            <select id="lt-pair"><option value="EURUSD">EUR/USD</option><option value="XAUUSD">XAU/USD</option><option value="BOTH">BOTH</option></select>
          </div>
          <div class="form-group"><label>Timeframe</label>
            <select id="lt-tf"><option value="15M">15M</option><option value="1H" selected>1H</option><option value="4H">4H</option><option value="15M+1H">15M + 1H</option><option value="1H+4H">1H + 4H</option><option value="ALL">15M + 1H + 4H</option></select>
          </div>
          <div class="form-group"><label>Kapitaal (€)</label>
            <input type="number" id="lt-capital" value="10000" min="100">
          </div>
          <div class="form-group"><label>Lot EURUSD (micro)</label>
            <input type="number" id="lt-lot-eur" value="1" min="1" step="1" title="Lotsize voor EUR/USD trades">
          </div>
          <div class="form-group"><label>Lot XAUUSD (micro)</label>
            <input type="number" id="lt-lot-xau" value="1" min="1" step="1" title="Lotsize voor XAU/USD trades">
          </div>
          <div class="form-group"><label>Min bias score (1–3)</label>
            <input type="number" id="lt-score" value="2" min="1" max="3">
          </div>
          <div class="form-group"><label>Spread (pips)</label>
            <input type="number" id="lt-spread" value="1.5" min="0" step="0.1" title="EURUSD ≈ 1.5 pips">
          </div>
          <div class="form-group"><label>Slippage (pips)</label>
            <input type="number" id="lt-slip" value="0.5" min="0" step="0.1" title="Typisch 0.5 pip">
          </div>
        </div>

        <div class="toggle-section">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <div class="toggle-section-title" style="margin-bottom:0">Risicobeheer</div>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:9px;color:var(--text3)" id="risk-toggle-label">AAN</span>
              <label class="switch"><input type="checkbox" id="lt-risk-toggle" checked onchange="toggleRisk(this)"><span class="slider"></span></label>
            </div>
          </div>
          <div class="form-grid" style="margin-bottom:0" id="risk-fields">
            <div class="form-group">
              <label>Max dagelijks verlies (€)</label>
              <input type="number" id="lt-max-loss" value="0" min="0" step="10" title="0 = uitgeschakeld">
            </div>
            <div class="form-group">
              <label>Max open trades</label>
              <input type="number" id="lt-max-trades" value="0" min="0" step="1" title="0 = onbeperkt">
            </div>
            <div class="form-group">
              <label>Max risico per trade (%)</label>
              <input type="number" id="lt-risk-pct" value="0" min="0" max="10" step="0.5" title="0 = vaste lotsize">
            </div>
          </div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Discord Notificaties</div>
          <div class="form-group" style="margin-top:6px">
            <label>Webhook URL</label>
            <input type="text" id="lt-discord" value="https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ" placeholder="https://discord.com/api/webhooks/..." style="font-size:10px">
          </div>
          <button onclick="testDiscord()" style="margin-top:6px;width:100%;padding:6px;border:1px solid var(--border2);border-radius:5px;background:rgba(124,58,237,.1);color:var(--glow2);font-size:11px;font-family:'Inter',sans-serif;cursor:pointer;transition:.2s" onmouseover="this.style.background='rgba(124,58,237,.2)'" onmouseout="this.style.background='rgba(124,58,237,.1)'">
            🔔 Test Discord Notificatie
          </button>
          <div style="font-size:9px;color:var(--text3);margin-top:4px;line-height:1.5">
            Server Instellingen → Integraties → Webhooks → Nieuwe Webhook → URL kopiëren
          </div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Strategie Filters</div>
          <div class="toggle-row">
            <div class="toggle-label">FVG<small>Altijd vereist — trigger signaal</small></div>
            <label class="switch"><input type="checkbox" checked disabled><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Order Block (OB)<small>OB in dezelfde richting als FVG</small></div>
            <label class="switch"><input type="checkbox" id="lt-ob" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Trend filter<small>Alleen mee met HH/HL of LH/LL</small></div>
            <label class="switch"><input type="checkbox" id="lt-trend"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Equilibrium filter<small>FVG op goede kant van EQ</small></div>
            <label class="switch"><input type="checkbox" id="lt-eq" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Killzone filter<small>Alleen London KZ en NY KZ</small></div>
            <label class="switch"><input type="checkbox" id="lt-session"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Liquidity Sweep<small>FVG na stop-run van swing high/low</small></div>
            <label class="switch"><input type="checkbox" id="lt-sweep"><span class="slider"></span></label>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px">
          <button class="btn-primary" id="lt-start-btn" onclick="startEngine()" style="margin:0;padding:10px;display:flex;align-items:center;justify-content:center;gap:6px">
            <i data-lucide="play" style="width:14px;height:14px"></i> EXECUTE
          </button>
          <button id="lt-stop-btn" onclick="stopEngine()" disabled
            style="padding:10px;border:1px solid var(--red);border-radius:6px;background:rgba(248,113,113,.1);color:var(--red);font-family:'Inter',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:.2s;opacity:.4;display:flex;align-items:center;justify-content:center;gap:6px">
            <i data-lucide="square" style="width:14px;height:14px"></i> SHUT DOWN
          </button>
        </div>
        <button id="lt-pause-btn" onclick="pauseEngine()"
          style="display:none;margin-top:6px;width:100%;padding:8px;border:1px solid rgba(251,191,36,.3);border-radius:6px;background:rgba(251,191,36,.1);color:var(--amber);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:.2s">
          ⏸ Pauze
        </button>
        <div id="lt-scan-countdown" style="text-align:center;font-size:10px;color:var(--text3);margin-top:6px;min-height:14px"></div>
        <button onclick="closeAllTrades()" style="margin-top:4px;width:100%;padding:8px;border:1px solid rgba(248,113,113,.4);border-radius:6px;background:rgba(248,113,113,.06);color:var(--red);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px;display:flex;align-items:center;justify-content:center;gap:6px">
          <i data-lucide="x-circle" style="width:13px;height:13px"></i> Sluit Alle Open Trades
        </button>

        <!-- CONFIG PRESETS -->
        <div class="toggle-section" style="margin-top:12px">
          <div class="toggle-section-title">Config Presets</div>
          <div style="display:flex;gap:6px;margin-bottom:8px">
            <input id="lt-preset-name" type="text" placeholder="Naam preset..." maxlength="30"
              style="flex:1;padding:5px 8px;border-radius:5px;border:1px solid var(--border2);background:rgba(8,5,24,.8);color:var(--text);font-family:'Inter',sans-serif;font-size:11px;outline:none">
            <button onclick="savePreset()"
              style="padding:5px 10px;border-radius:5px;border:1px solid var(--border2);background:rgba(124,58,237,.15);color:var(--glow2);font-family:'Inter',sans-serif;font-size:11px;cursor:pointer;white-space:nowrap">
              Opslaan
            </button>
          </div>
          <div id="lt-presets-list" style="display:flex;flex-direction:column;gap:4px">
            <div style="font-size:10px;color:var(--text3)">Nog geen presets opgeslagen.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- LIVE BIAS (now in sidebar) -->
    <!-- MULTIBIAS (now in sidebar) -->
    <!-- CALENDAR (now in sidebar) -->

    <!-- BT WINRATE -->
    <div class="card" id="bt-wr-card" style="display:none">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>Resultaten</div></div>
      <div class="card-body">
        <div class="stat-grid" style="grid-template-columns:1fr 1fr;gap:10px">
          <div class="stat-item"><div class="lbl">Winrate</div><div class="val" id="bt-wr">—</div><div class="sub" id="bt-wr-s">0 trades</div></div>
          <div class="stat-item"><div class="lbl">Totaal P&L</div><div class="val" id="bt-pnl" style="font-size:15px">—</div><div class="sub" id="bt-pnl-s">—</div></div>
        </div>
        <div class="progress-wrap" style="margin-top:10px">
          <div class="progress-label"><span>Winrate</span><span id="bt-wr-pct">0%</span></div>
          <div class="progress-bar"><div class="progress-fill" id="bt-wr-bar" style="width:0%"></div></div>
        </div>
        <div class="best-worst">
          <div class="bw-box" style="background:rgba(52,211,153,.06);border:1px solid rgba(52,211,153,.15);color:var(--green)">
            <div class="ttl">Best trade</div><div id="bt-best">—</div>
          </div>
          <div class="bw-box" style="background:rgba(248,113,113,.06);border:1px solid rgba(248,113,113,.15);color:var(--red)">
            <div class="ttl">Worst trade</div><div id="bt-worst">—</div>
          </div>
        </div>
      </div>
    </div>

    <!-- BT FORM -->
    <div class="card" id="bt-form" style="display:none">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>Backtest Configuratie</div></div>
      <div class="card-body">
        <div class="form-grid">
          <div class="form-group"><label>Pair</label><select id="bt-pair" onchange="updateBtPairFields()"><option value="EURUSD">EUR/USD</option><option value="XAUUSD">XAU/USD</option><option value="BOTH">BOTH</option></select></div>
          <div class="form-group"><label>Timeframe</label><select id="bt-tf" onchange="updateTFNote()"><option value="15M">15M</option><option value="1H" selected>1H</option><option value="4H">4H</option><option value="15M+1H">15M + 1H</option><option value="1H+4H">1H + 4H</option><option value="ALL">15M + 1H + 4H</option></select></div>
          <div class="form-group"><label>Startdatum</label><input type="date" id="bt-start" value="2026-04-01"></div>
          <div class="form-group"><label>Einddatum</label><input type="date" id="bt-end" value="2026-04-30"></div>
          <div class="form-group"><label>Kapitaal (€)</label><input type="number" id="bt-cap" value="10000" min="100"></div>
          <div class="form-group"><label>Lot EURUSD (micro)</label><input type="number" id="bt-lot-eur" value="1" min="1" step="1"></div>
          <div class="form-group"><label>Lot XAUUSD (micro)</label><input type="number" id="bt-lot-xau" value="1" min="1" step="1"></div>
          <div class="form-group"><label>Risk:Reward ratio</label><input type="number" id="bt-rr" value="2" min="0.5" step="0.5"></div>
          <div class="form-group"><label>Min bias score (1–3)</label><input type="number" id="bt-score" value="2" min="1" max="3"></div>
          <div class="form-group"><label>Break-even bij (xR)</label><input type="number" id="bt-be" value="0" min="0" max="2" step="0.1" title="0 = uit. Bv: 0.5 = SL naar BE bij 50% van TP"></div>
          <!-- Spread/Slippage - enkel EURUSD of XAUUSD -->
          <div id="bt-spread-single">
            <div class="form-group"><label>Spread (pips)</label><input type="number" id="bt-spread" value="1.5" min="0" step="0.1" title="EURUSD ≈ 1.5 pips, XAUUSD ≈ 35 pips"></div>
            <div class="form-group"><label>Slippage (pips)</label><input type="number" id="bt-slip" value="0.5" min="0" step="0.1" title="Typisch 0.5-1 pip voor marktorders"></div>
          </div>
          <!-- Spread/Slippage - BOTH -->
          <div id="bt-spread-both" style="display:none">
            <div class="form-group"><label>Spread EURUSD (pips)</label><input type="number" id="bt-spread-eur" value="1.5" min="0" step="0.1"></div>
            <div class="form-group"><label>Slippage EURUSD (pips)</label><input type="number" id="bt-slip-eur" value="0.5" min="0" step="0.1"></div>
            <div class="form-group"><label>Spread XAUUSD (pips)</label><input type="number" id="bt-spread-xau" value="35" min="0" step="1"></div>
            <div class="form-group"><label>Slippage XAUUSD (pips)</label><input type="number" id="bt-slip-xau" value="5" min="0" step="1"></div>
          </div>
          <div class="form-group"><label>Max dagelijks verlies (€)</label><input type="number" id="bt-max-loss" value="0" min="0" step="10" title="0 = uitgeschakeld"></div>
          <div class="form-group"><label>Max open trades</label><input type="number" id="bt-max-trades" value="0" min="0" step="1" title="0 = onbeperkt"></div>
          <div class="form-group"><label>Max risico per trade (%)</label><input type="number" id="bt-risk-pct" value="0" min="0" max="10" step="0.5" title="0 = vaste lotsize"></div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Strategie Filters</div>
          <div class="toggle-row">
            <div class="toggle-label">FVG (Fair Value Gap)<small>Altijd vereist — trigger signaal</small></div>
            <label class="switch"><input type="checkbox" checked disabled><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Order Block (OB)<small>Zoek OB in dezelfde richting als FVG</small></div>
            <label class="switch"><input type="checkbox" id="use-ob" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Trend filter (HH/HL of LH/LL)<small>Alleen traden met de trend mee</small></div>
            <label class="switch"><input type="checkbox" id="use-trend" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Equilibrium filter<small>FVG moet op goede kant van EQ staan</small></div>
            <label class="switch"><input type="checkbox" id="use-eq" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Killzone filter<small>Alleen London KZ (09-12) en NY KZ (14-17)</small></div>
            <label class="switch"><input type="checkbox" id="use-session"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Liquidity Sweep<small>FVG na stop-run van swing high/low</small></div>
            <label class="switch"><input type="checkbox" id="use-sweep"><span class="slider"></span></label>
          </div>
        </div>

        <div id="tf-note" style="display:none;margin-top:8px;padding:8px 10px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:6px;font-size:10px;color:var(--amber)">
          ⚠ 15M data is beperkt tot de laatste 60 dagen door yFinance. Voor oudere periodes wordt automatisch 1H gebruikt.
        </div>

        <button class="btn-primary" onclick="runBacktest()" id="bt-run" style="display:flex;align-items:center;justify-content:center;gap:6px">
          <i data-lucide="play" style="width:14px;height:14px"></i> Run Backtest
        </button>
        <button onclick="runOptimizer()" id="bt-opt"
          style="width:100%;margin-top:8px;padding:10px;border:1px solid var(--amber);border-radius:6px;background:rgba(251,191,36,.08);color:var(--amber);font-family:'Inter',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px;display:flex;align-items:center;justify-content:center;gap:6px">
          <i data-lucide="zap" style="width:14px;height:14px"></i> Optimaliseer — Beste Configuratie Vinden
        </button>
      </div>
    </div>
  </div>
</div>
</div>

<!-- TRADE DETAIL MODAL -->
<div class="modal-overlay" id="trade-modal" style="display:none" onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div class="modal-title" id="modal-title">Trade Detail</div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="modal-chart" id="modal-chart"></div>
      <div class="modal-details" id="modal-details"></div>
    </div>
  </div>
</div>

<script>
const S={pair:"EURUSD",tf:"15M",page:"live",chart:null,series:null,btTrades:[],fvgSeries:[],
         lastCandleTime:null,lastCandleOpen:0,lastCandleHigh:0,lastCandleLow:0,livePrice:0};

function initChart(){
  const el=document.getElementById("chart");
  el.innerHTML="";
  S.chart=LightweightCharts.createChart(el,{
    layout:{background:{type:"Solid",color:"#04020f"},textColor:"#5a4e80"},
    grid:{vertLines:{color:"rgba(45,31,94,.3)"},horzLines:{color:"rgba(45,31,94,.3)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#2d1f5e",timeVisible:true,secondsVisible:false,rightOffset:12,barSpacing:8},
    rightPriceScale:{borderColor:"#2d1f5e"},
    handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true},
    handleScale:{mouseWheel:true,pinch:true},
  });
  new ResizeObserver(()=>{if(S.chart)S.chart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);
  S.series=S.chart.addCandlestickSeries({
    upColor:"#7c3aed",downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#a78bfa",borderDownColor:"#3d2b7a",
    wickUpColor:"#a78bfa",wickDownColor:"#3d2b7a",
    priceFormat: { type:"price", precision:5, minMove:0.00001 },
  });
}

function updateChartPriceFormat(){
  if(!S.series) return;
  const isGold = S.pair==="XAUUSD";
  S.series.applyOptions({
    priceFormat: isGold
      ? { type:"price", precision:2, minMove:0.01 }
      : { type:"price", precision:5, minMove:0.00001 },
  });
}

async function loadCandles(){
  document.getElementById("chart-status").textContent="Laden...";
  try{
    const r=await fetch(`/api/candles?pair=${S.pair}&tf=${S.tf}`);
    const d=await r.json();
    if(d.candles&&d.candles.length>0){
      S.series.setMarkers([]);
      S.series.setData(d.candles.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
      S.chart.timeScale().fitContent();
      S.chart.applyOptions({rightPriceScale:{autoScale:true}});
      updateChartPriceFormat();
      // Store last candle for 30s live price updates
      const last = d.candles[d.candles.length-1];
      S.lastCandleTime = last.time;
      S.lastCandleOpen = last.open;
      S.lastCandleHigh = last.high;
      S.lastCandleLow  = last.low;
      document.getElementById("chart-status").textContent=`${d.candles.length} candles · ${S.pair} ${S.tf} · 20s`;
    } else {
      document.getElementById("chart-status").textContent="Geen data — probeer andere TF";
    }
  }catch(e){document.getElementById("chart-status").textContent="Verbindingsfout"}
}

async function loadBias(){
  try{
    const r=await fetch(`/api/bias?pair=${S.pair}&tf=${S.tf}`);
    const b=await r.json();
    updateBias(b);
    // Teken live FVG zones op de chart als we in live modus zijn
    if(S.page==="live" && b.live_fvgs && S.series){
      drawLiveFVGs(b.live_fvgs);
    }
  }catch(e){}
}

function drawLiveFVGs(fvgs){
  // Verwijder bestaande FVG series
  if(S.fvgSeries){
    S.fvgSeries.forEach(s=>{ try{ S.chart.removeSeries(s); }catch(e){} });
  }
  S.fvgSeries = [];
  if(!fvgs || !fvgs.length) return;
  fvgs.forEach(fvg=>{
    try{
      // Gebruik een area series als visuele zone
      const s = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(124,58,237,0)" : "rgba(248,113,113,0)",
        lineWidth: 0,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      // Teken twee horizontale lijnen voor de FVG zone
      const topLine = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(124,58,237,0.6)" : "rgba(248,113,113,0.6)",
        lineWidth: 1,
        lineStyle: 2, // dashed
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      const botLine = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(124,58,237,0.6)" : "rgba(248,113,113,0.6)",
        lineWidth: 1,
        lineStyle: 2,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      const now = Math.floor(Date.now()/1000);
      topLine.setData([{time: fvg.time, value: fvg.top}, {time: now, value: fvg.top}]);
      botLine.setData([{time: fvg.time, value: fvg.bottom}, {time: now, value: fvg.bottom}]);
      S.fvgSeries.push(topLine, botLine);
    }catch(e){}
  });
}

function jColor(s){return s>0?"#34d399":s<0?"#f87171":"#5a4e80"}
function jWidth(s){return s>0?"75%":s<0?"25%":"50%"}

function updateBias(b){
  const s=b.total_score;
  document.getElementById("bias-num").textContent=s>=0?`+${s}`:`${s}`;
  document.getElementById("bias-num").style.color=b.verdict_color;
  document.getElementById("bias-num").style.textShadow=`0 0 20px ${b.verdict_color}80`;
  document.getElementById("bias-vt").textContent=b.verdict;
  document.getElementById("bias-vt").style.color=b.verdict_color;
  document.getElementById("bias-adv").textContent=b.advice;
  const box=document.getElementById("bias-box");
  box.style.borderColor=s>=2?"rgba(52,211,153,.3)":s<=-2?"rgba(248,113,113,.3)":"var(--border)";
  box.style.background=s>=2?"rgba(52,211,153,.05)":s<=-2?"rgba(248,113,113,.05)":"rgba(124,58,237,.06)";

  function setJ(id,score,detail){
    document.getElementById(id+"b").style.width=jWidth(score);
    document.getElementById(id+"b").style.background=jColor(score);
    document.getElementById(id+"b").style.boxShadow=score!==0?`0 0 6px ${jColor(score)}`:"none";
    document.getElementById(id+"v").textContent=score>0?"▲":score<0?"▼":"—";
    document.getElementById(id+"v").style.color=jColor(score);
    document.getElementById(id+"d").textContent=detail;
  }
  setJ("j1",b.j1,b.j1_detail);
  setJ("j2",b.j2,b.j2_detail);
  setJ("j3",b.j3,b.j3_detail);

  const sp=document.getElementById("struct-pill");
  sp.innerHTML=`<span class="pill ${b.struct_conflict?"pill-amber":b.struct_label.includes("✓")?"pill-green":"pill-gray"}">${b.struct_label}</span>`;

  document.getElementById("ote-l").textContent=b.ote_low;
  document.getElementById("ote-m").textContent=b.ote_705;
  document.getElementById("ote-h").textContent=b.ote_high;
  document.getElementById("ote-eq").textContent=b.equilibrium;

  // Sessie topbar
  const sess=document.getElementById("topbar-session");
  sess.textContent=b.session||"—";
  sess.className=b.in_kz?"kz":"";

  if(b.price && b.price > 0){
    const dec = S.pair==="XAUUSD" ? 2 : 5;
    S.livePrice = b.price;
    document.getElementById("topbar-price").textContent = b.price.toFixed(dec);
  }

  // Winrate sessie card
  document.getElementById("wr-score").textContent=s>=0?`+${s}`:`${s}`;
  document.getElementById("wr-score").style.color=b.verdict_color;
  document.getElementById("wr-verdict").textContent=b.verdict;
  document.getElementById("wr-verdict").style.color=b.verdict_color;
  document.getElementById("wr-session").textContent=b.session;
  document.getElementById("wr-ote-l").textContent=b.ote_low;
  document.getElementById("wr-ote-h").textContent=b.ote_high;
  document.getElementById("wr-pair").textContent=`${S.pair} · ${S.tf}`;
  const pct=Math.round((s+3)/6*100);
  document.getElementById("wr-bar").style.width=pct+"%";
  document.getElementById("wr-pct").textContent=pct+"%";
}

function setPair(p){
  S.pair=p;
  S.livePrice=0;
  S.lastCandleTime=null;
  document.querySelectorAll("#pg .toggle-btn").forEach((b,i)=>b.classList.toggle("active",(i===0&&p==="EURUSD")||(i===1&&p==="XAUUSD")));
  updateChartPriceFormat();
  loadAll();
}
function setTF(t){
  S.tf=t;
  S.lastCandleTime=null;
  document.querySelectorAll("#tg .toggle-btn").forEach((b,i)=>b.classList.toggle("active",["15M","1H","4H"][i]===t));
  loadAll();
}

async function loadAll(){
  const btn=document.getElementById("main-refresh");
  const ri=document.getElementById("ri");
  btn.disabled=true;
  ri.innerHTML='<span class="spinner"></span>';
  await Promise.all([loadCandles(),loadBias(),loadMultiBias(),loadCalendar(),checkMarketStatus()]);
  btn.disabled=false;
  ri.textContent="⟳";
}

async function checkMarketStatus(){
  try{
    const r = await fetch("/api/engine/status");
    const d = await r.json();
    const banner     = document.getElementById("weekend-banner");
    const openBanner = document.getElementById("market-open-banner");
    if(banner)     banner.style.display     = d.is_weekend ? "block" : "none";
    if(openBanner) openBanner.style.display = d.is_weekend ? "none"  : "block";
  }catch(e){
    // Fallback: check client-side using Brussels time
    const now = new Date();
    const bxl = new Date(now.toLocaleString("en-US", {timeZone: "Europe/Brussels"}));
    const wd  = bxl.getDay();   // 0=Zo, 1=Ma, 5=Vr, 6=Za
    const h   = bxl.getHours();
    let isWeekend = false;
    if(wd === 6) isWeekend = true;                               // Zaterdag
    if(wd === 5 && h >= 23) isWeekend = true;                   // Vrijdag na 23:00
    if(wd === 0 && h < 23)  isWeekend = true;                   // Zondag voor 23:00
    if([1,2,3,4].includes(wd) && h === 23) isWeekend = true;   // Ma-Do 23:00-00:00
    const banner     = document.getElementById("weekend-banner");
    const openBanner = document.getElementById("market-open-banner");
    if(banner)     banner.style.display     = isWeekend ? "block" : "none";
    if(openBanner) openBanner.style.display = isWeekend ? "none"  : "block";
  }
}

function switchPage(page){
  S.page=page;
  document.querySelectorAll(".tab-btn").forEach((b,i)=>b.classList.toggle("active",
    (i===0&&page==="live")||(i===1&&page==="backtest")||(i===2&&page==="system")));
  const live=page==="live", bt=page==="backtest", sys=page==="system";
  const liveIds=["live-engine-config","live-open-card","live-stats-card","live-log-card","live-engine-log-card"];
  liveIds.forEach(id=>{ const el=document.getElementById(id); if(el) el.style.display=live?"":"none"; });
  const sideBtn=document.getElementById("sidebar-open-btn");
  if(sideBtn) sideBtn.style.display=live?"":"none";
  document.getElementById("bt-form").style.display=bt?"":"none";
  document.getElementById("bt-results").style.display="none";
  document.getElementById("bt-wr-card").style.display="none";
  document.getElementById("opt-card").style.display="none";
  document.getElementById("system-panel").style.display=sys?"":"none";
  if(!live) closeSidebar();
  if(sys) runSystemCheck();
  if(live){ S.series&&S.series.setMarkers([]); }
  lucide.createIcons();
}

function toggleRisk(cb){
  const fields = document.getElementById("risk-fields");
  const label  = document.getElementById("risk-toggle-label");
  if(cb.checked){
    fields.style.opacity="1";
    fields.style.pointerEvents="";
    label.textContent="AAN";
  } else {
    fields.style.opacity=".3";
    fields.style.pointerEvents="none";
    label.textContent="UIT";
  }
}

async function testDiscord(){
  const webhook = document.getElementById("lt-discord").value.trim();
  if(!webhook){ alert("Vul eerst een Discord webhook URL in."); return; }
  const r = await fetch("/api/engine/test_discord",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({webhook})
  });
  const d = await r.json();
  if(d.ok){
    alert("✅ Test notificatie verzonden! Controleer je Discord.");
  } else {
    alert("❌ Fout: " + (d.error||"Onbekende fout"));
  }
}

async function closeAllTrades(){
  if(!confirm("Weet je zeker dat je alle open trades wil sluiten op de huidige marktprijs?")) return;
  const r = await fetch("/api/engine/close_all",{method:"POST"});
  const d = await r.json();
  if(d.ok){
    alert(`✅ ${d.closed} trade(s) gesloten.`);
  }
}

async function runSystemCheck(){
  const container = document.getElementById("sys-checks");
  const info      = document.getElementById("sys-info");
  if(!container) return;
  container.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Checks uitvoeren...</div>';

  try{
    const r = await fetch("/api/system/health");
    const d = await r.json();

    const labels = {
      tradingview:  "TradingView WebSocket",
      yfinance:     "yFinance API",
      data_quality: "Data Kwaliteit",
      discord:      "Discord Webhook",
      engine:       "Trading Engine",
      system:       "Systeem Resources",
      uptime:       "VPS Uptime",
      python:       "Python Runtime",
      market:       "Markt Status",
    };

    const icons = {
      tradingview:  "wifi",
      yfinance:     "database",
      data_quality: "git-compare",
      discord:      "bell",
      engine:       "cpu",
      system:       "server",
      uptime:       "clock",
      python:       "code-2",
      market:       "trending-up",
    };

    const order = ["market","engine","tradingview","yfinance","data_quality","discord","system","uptime","python"];

    container.innerHTML = order.map(key=>{
      const c = d[key] || {};
      const s = c.status || "warn";
      const color = s==="ok"?"var(--green)":s==="error"?"var(--red)":"var(--amber)";
      const bg    = s==="ok"?"rgba(52,211,153,.06)":s==="error"?"rgba(248,113,113,.06)":"rgba(251,191,36,.06)";
      const border= s==="ok"?"rgba(52,211,153,.15)":s==="error"?"rgba(248,113,113,.15)":"rgba(251,191,36,.15)";
      return `<div style="display:flex;align-items:center;gap:12px;padding:10px 14px;background:${bg};border:1px solid ${border};border-radius:8px">
        <i data-lucide="${icons[key]||'circle'}" style="width:16px;height:16px;color:${color};flex-shrink:0"></i>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <span style="font-size:12px;font-weight:600;color:var(--text)">${labels[key]||key}</span>
            <span style="font-size:11px;font-weight:600;color:${color}">${c.msg||"—"}</span>
          </div>
          ${c.detail?`<div style="font-size:10px;color:var(--text3);margin-top:2px;font-family:'JetBrains Mono',monospace">${c.detail}</div>`:""}
        </div>
      </div>`;
    }).join("");

    lucide.createIcons();

    // System info
    info.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px">
        <div style="color:var(--text3)">Brussels tijd</div>
        <div style="color:var(--text);font-family:'JetBrains Mono',monospace">${new Date().toLocaleString("nl-BE",{timeZone:"Europe/Brussels"})}</div>
        <div style="color:var(--text3)">Engine scans</div>
        <div style="color:var(--text)">${d.engine?.detail||"—"}</div>
        <div style="color:var(--text3)">Data bron</div>
        <div style="color:var(--glow2)">TradingView WebSocket (OANDA)</div>
        <div style="color:var(--text3)">Fallback</div>
        <div style="color:var(--text2)">yFinance (15min vertraging)</div>
      </div>`;

  }catch(e){
    container.innerHTML = `<div style="color:var(--red);font-size:12px;text-align:center;padding:20px">Fout bij ophalen system info: ${e.message}</div>`;
  }
}

function openSidebar(){
  document.getElementById("analysis-sidebar").classList.add("open");
  document.getElementById("sidebar-overlay").classList.add("open");
  // Refresh bias data when sidebar opens
  loadBias().catch(console.error);
  loadMultiBias().catch(console.error);
  loadCalendar().catch(console.error);
}

function closeSidebar(){
  document.getElementById("analysis-sidebar").classList.remove("open");
  document.getElementById("sidebar-overlay").classList.remove("open");
}

// Close sidebar on Escape key
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeSidebar(); });

async function runOptimizer(){
  const btn = document.getElementById("bt-opt");
  btn.disabled = true;
  btn.textContent = "⟳ Bezig... (dit duurt 1-3 minuten)";

  const isBoth = document.getElementById("bt-pair").value === "BOTH";
  const body = {
    start:         document.getElementById("bt-start").value,
    end:           document.getElementById("bt-end").value,
    capital:       document.getElementById("bt-cap").value,
    lotsize:       parseFloat(document.getElementById("bt-lot-eur").value)||1,
    lotsize_eur:   parseFloat(document.getElementById("bt-lot-eur").value)||1,
    lotsize_xau:   parseFloat(document.getElementById("bt-lot-xau").value)||1,
    spread_pips:   isBoth ? parseFloat(document.getElementById("bt-spread-eur").value)||1.5 : parseFloat(document.getElementById("bt-spread").value)||1.5,
    slippage_pips: isBoth ? parseFloat(document.getElementById("bt-slip-eur").value)||0.5   : parseFloat(document.getElementById("bt-slip").value)||0.5,
    spread_pips_xau:   isBoth ? parseFloat(document.getElementById("bt-spread-xau").value)||35 : null,
    slippage_pips_xau: isBoth ? parseFloat(document.getElementById("bt-slip-xau").value)||5   : null,
    use_sweep:     document.getElementById("use-sweep").checked,
  };

  try{
    const r = await fetch("/api/optimize",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)
    });
    const d = await r.json();
    renderOptimizer(d);
    document.getElementById("opt-card").style.display="";
    document.getElementById("opt-card").scrollIntoView({behavior:"smooth"});
  }catch(e){
    alert("Optimizer fout: "+e.message);
  }
  btn.disabled = false;
  btn.textContent = "⚡ Optimaliseer — Beste Configuratie Vinden";
}

function renderOptimizer(d){
  // Meta info
  document.getElementById("opt-meta").textContent =
    `${d.total_tested} combinaties getest · Split: ${d.split_date}`;

  // Summary cards
  const best = d.results[0];
  document.getElementById("opt-summary").innerHTML = `
    <div class="bt-stat"><div class="l">Geteste configs</div><div class="v" style="color:var(--purple)">${d.total_tested}</div></div>
    <div class="bt-stat"><div class="l">IS periode</div><div class="v" style="font-size:11px;color:var(--green)">${d.in_sample}</div></div>
    <div class="bt-stat"><div class="l">OOS periode</div><div class="v" style="font-size:11px;color:var(--amber)">${d.out_sample}</div></div>`;

  // Tabel
  const tbody = document.getElementById("opt-tbody");
  tbody.innerHTML = "";

  d.results.forEach((r,i)=>{
    const oos_good = r.oos_pnl > 0 && r.oos_wr >= 50;
    const is_good  = r.is_pnl  > 0 && r.is_wr  >= 50;
    const row_bg   = oos_good && is_good ? "rgba(52,211,153,.04)" :
                     oos_good ? "rgba(251,191,36,.04)" : "";
      const tr = document.createElement("tr");
    tr.style.background = row_bg;
    tr.dataset.config = JSON.stringify(r);
    tr.innerHTML = `
      <td style="color:var(--glow2);font-weight:700">${i+1}</td>
      <td>${r.pair}</td>
      <td>${r.tf}</td>
      <td>${r.rr}</td>
      <td>${r.ob?"✓":"—"}</td>
      <td>${r.eq?"✓":"—"}</td>
      <td>${r.kz?"✓":"—"}</td>
      <td>${r.be>0?r.be:"—"}</td>
      <td style="color:var(--green)">${r.is_trades}</td>
      <td style="color:${r.is_wr>=55?"var(--green)":"var(--red)"};font-weight:600">${r.is_wr}%</td>
      <td style="color:${r.is_pnl>0?"var(--green)":"var(--red)"};font-weight:600">€${r.is_pnl}</td>
      <td style="color:var(--amber)">${r.oos_trades}</td>
      <td style="color:${r.oos_wr>=50?"var(--green)":"var(--red)"};font-weight:700">${r.oos_wr}%</td>
      <td style="color:${r.oos_pnl>0?"var(--green)":"var(--red)"};font-weight:700">€${r.oos_pnl}</td>
      <td><button class="apply-cfg-btn" style="font-size:9px;padding:2px 8px;border:1px solid var(--border2);border-radius:4px;background:transparent;color:var(--glow2);cursor:pointer">↗ Gebruik</button></td>`;
    tr.querySelector(".apply-cfg-btn").addEventListener("click", ()=>applyConfig(r));
    tbody.appendChild(tr);
    tbody.appendChild(tr);
  });
}

function applyConfig(r){
  // Pas de backtest configuratie aan naar de geselecteerde configuratie
  document.getElementById("bt-pair").value  = r.pair;
  document.getElementById("bt-tf").value    = r.tf;
  document.getElementById("bt-rr").value    = r.rr;
  document.getElementById("bt-score").value = r.score||2;
  document.getElementById("use-ob").checked = r.ob;
  document.getElementById("use-eq").checked = r.eq;
  document.getElementById("use-session").checked = r.kz;
  document.getElementById("bt-be").value    = r.be||0;
  updateTFNote();
  // Scroll naar backtest config
  document.getElementById("bt-run").scrollIntoView({behavior:"smooth"});
}

async function runBacktest(){
  const btn=document.getElementById("bt-run");
  btn.disabled=true;btn.textContent="⟳ Bezig...";
  const body={
    pair:document.getElementById("bt-pair").value,
    tf:document.getElementById("bt-tf").value,
    start:document.getElementById("bt-start").value,
    end:document.getElementById("bt-end").value,
    capital:document.getElementById("bt-cap").value,
    lotsize:parseFloat(document.getElementById("bt-lot-eur").value)||1,
    lotsize_eur:parseFloat(document.getElementById("bt-lot-eur").value)||1,
    lotsize_xau:parseFloat(document.getElementById("bt-lot-xau").value)||1,
    rr:parseFloat(document.getElementById("bt-rr").value),
    min_score:parseInt(document.getElementById("bt-score").value),
    be_trigger:parseFloat(document.getElementById("bt-be").value)||0,
    spread_pips:parseFloat(document.getElementById("bt-spread-single").style.display==="none" ?
                document.getElementById("bt-spread-eur").value : document.getElementById("bt-spread").value)||0,
    slippage_pips:parseFloat(document.getElementById("bt-spread-single").style.display==="none" ?
                  document.getElementById("bt-slip-eur").value : document.getElementById("bt-slip").value)||0,
    spread_pips_xau: document.getElementById("bt-spread-single").style.display==="none" ?
                     parseFloat(document.getElementById("bt-spread-xau").value)||35 : null,
    slippage_pips_xau: document.getElementById("bt-spread-single").style.display==="none" ?
                       parseFloat(document.getElementById("bt-slip-xau").value)||5 : null,
    max_daily_loss:parseFloat(document.getElementById("bt-max-loss").value)||0,
    max_trades:parseInt(document.getElementById("bt-max-trades").value)||0,
    max_risk_pct:parseFloat(document.getElementById("bt-risk-pct").value)||0,
    use_ob:document.getElementById("use-ob").checked,
    use_trend:document.getElementById("use-trend").checked,
    use_eq:document.getElementById("use-eq").checked,
    use_session:document.getElementById("use-session").checked,
    use_sweep:document.getElementById("use-sweep").checked,
  };
  S.pair = body.pair==="BOTH" ? "EURUSD" : body.pair;
  S.tf   = body.tf;
  try{
    const r=await fetch("/api/backtest",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){alert(d.error);btn.disabled=false;btn.textContent="▶ Run Backtest";return}
    S.btTrades=d.trades;
    if(d.candles&&d.candles.length>0){
      S.series.setMarkers([]);
      S.series.setData(d.candles.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
      S.chart.timeScale().fitContent();
      S.chart.applyOptions({rightPriceScale:{autoScale:true}});
    }
    // Markers
    const markers=[];
    d.trades.forEach(t=>{
      if(t.entry_ts) markers.push({time:t.entry_ts,position:t.direction==="LONG"?"belowBar":"aboveBar",color:t.direction==="LONG"?"#7c3aed":"#f59e0b",shape:t.direction==="LONG"?"arrowUp":"arrowDown",text:`${t.direction} ${t.entry_price}`});
      if(t.exit_ts)  markers.push({time:t.exit_ts,position:"inBar",color:t.outcome==="win"?"#34d399":"#f87171",shape:"circle",text:`€${t.pnl_eur}`});
    });
    markers.sort((a,b)=>a.time-b.time);
    S.series.setMarkers(markers);
    renderBtStats(d.stats,d.trades);
    document.getElementById("bt-results").style.display="";
    document.getElementById("bt-wr-card").style.display="";
  }catch(e){alert("Fout: "+e.message)}
  btn.disabled=false;btn.textContent="▶ Run Backtest";
}

function renderBtStats(s,trades){
  const pc=s.total_pnl>=0?"var(--green)":"var(--red)";
  const wr=s.total>0?s.winrate:0;
  document.getElementById("bt-wr").textContent=s.total>0?wr+"%":"—";
  document.getElementById("bt-wr-s").textContent=`${s.wins}W / ${s.losses}L${s.be>0?" / "+s.be+"BE":""} van ${s.total}`;
  document.getElementById("bt-pnl").textContent=s.total>0?`€${s.total_pnl}`:"—";
  document.getElementById("bt-pnl").style.color=pc;
  document.getElementById("bt-pnl-s").textContent=`gem. ${s.avg_pips} pips`;
  document.getElementById("bt-best").textContent=`€${s.best}`;
  document.getElementById("bt-worst").textContent=`€${s.worst}`;
  document.getElementById("bt-wr-bar").style.width=wr+"%";
  document.getElementById("bt-wr-pct").textContent=wr+"%";
  document.getElementById("bt-sg").innerHTML=`
    <div class="bt-stat"><div class="l">Trades</div><div class="v" style="color:#a78bfa">${s.total}</div></div>
    <div class="bt-stat"><div class="l">Winrate</div><div class="v" style="color:${wr>=50?"var(--green)":"var(--red)"}">${wr}%</div></div>
    <div class="bt-stat"><div class="l">Totaal P&L</div><div class="v" style="color:${pc}">€${s.total_pnl}</div></div>
    <div class="bt-stat"><div class="l">Gem. Pips</div><div class="v" style="color:var(--glow3)">${s.avg_pips}</div></div>`;
  const tbody=document.getElementById("bt-tbody");
  tbody.innerHTML="";
  if(!trades.length){
    tbody.innerHTML='<tr><td colspan="12" style="text-align:center;padding:24px;color:var(--text3)">Geen setups gevonden in deze periode</td></tr>';
    return;
  }
  trades.forEach(t=>{
    const w=t.outcome==="win";
    const be=t.outcome==="be";
    const outcomeClass = w ? "win" : be ? "" : "loss";
    const outcomeStyle = be ? "color:var(--text3)" : "";
    const tr=document.createElement("tr");
    tr.style.cursor="pointer";
    tr.title="Klik voor trade detail";
    tr.innerHTML=`<td>${t.id}</td><td>${t.entry_time}</td><td>${t.pair}</td>
      <td><span class="pill ${t.direction==="LONG"?"pill-purple":"pill-amber"}">${t.direction}</span></td>
      <td>${t.entry_price}</td><td>${t.exit_price}</td><td>${t.sl}</td><td>${t.tp}</td>
      <td class="${outcomeClass}" style="${outcomeStyle}">${t.pips>0?"+":""}${t.pips}${be?" (BE)":""}</td>
      <td class="${outcomeClass}" style="${outcomeStyle}">${t.pnl_eur>0?"+":""}€${t.pnl_eur}${be?" (BE)":""}</td>
      <td>${t.bias_score>0?"+":""}${t.bias_score}</td><td>${(t.session||"").substring(0,16)}</td>`;
    tr.addEventListener("click",()=>openTradeModal(t));
    tbody.appendChild(tr);
  });
}

function updateBtPairFields(){
  const pair = document.getElementById("bt-pair").value;
  const isBoth = pair === "BOTH";
  document.getElementById("bt-spread-single").style.display = isBoth ? "none" : "";
  document.getElementById("bt-spread-both").style.display   = isBoth ? "" : "none";
  // Stel standaard spread/slippage in op basis van pair
  if(pair === "XAUUSD"){
    document.getElementById("bt-spread").value = "35";
    document.getElementById("bt-slip").value   = "5";
  } else if(pair === "EURUSD"){
    document.getElementById("bt-spread").value = "1.5";
    document.getElementById("bt-slip").value   = "0.5";
  }
}

function updateTFNote(){
  const tf = document.getElementById("bt-tf");
  const note = document.getElementById("tf-note");
  if(!tf || !note) return;
  const v = tf.value;
  note.style.display = (v==="15M"||v==="15M+1H"||v==="ALL") ? "block" : "none";
}

function exportCSV(){
  if(!S.btTrades.length) return;
  const h=["Pair","Richting","Entry","SL","TP","PnL (€)"];
  const rows=S.btTrades.map(t=>[t.pair,t.direction,t.entry_price,t.sl,t.tp,t.pnl_eur]);
  const csv=[h,...rows].map(r=>r.join(";")).join("\n");
  const a=document.createElement("a");
  a.href="data:text/csv;charset=utf-8,\uFEFF"+encodeURIComponent(csv);
  a.download=`gitchi_backtest_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
}

// ── MULTI-TF BIAS ──
async function loadMultiBias(){
  try{
    const r=await fetch(`/api/multibias?pair=${S.pair}`);
    const d=await r.json();
    renderMultiBias(d);
  }catch(e){}
}

function renderMultiBias(d){
  const badge=document.getElementById("align-badge");
  const text=document.getElementById("align-text");
  const ac=d.alignment_color||"#a78bfa";
  badge.style.background=`${ac}15`;
  badge.style.borderColor=`${ac}40`;
  badge.style.color=ac;
  text.textContent=d.alignment==="BULL"?"▲ HTF Aligned — BULLISH":d.alignment==="BEAR"?"▼ HTF Aligned — BEARISH":"↔ Mixed — No HTF Alignment";

  const rows=document.getElementById("mtf-rows");
  rows.innerHTML="";
  ["15M","1H","4H"].forEach(tf=>{
    const b=d[tf];
    if(!b) return;
    const s=b.total_score;
    const vc=b.verdict_color;
    const jColor=j=>j>0?"#34d399":j<0?"#f87171":"#5a4e80";
    const jW=j=>j>0?"70%":j<0?"30%":"50%";
    const row=document.createElement("div");
    row.className="tf-bias-row";
    row.innerHTML=`
      <div class="tf-label">${tf}</div>
      <div class="tf-bars">
        <div class="tf-bar-row">
          <span style="width:50px">P/D</span>
          <div style="flex:1;background:rgba(124,58,237,.08);border-radius:99px;height:3px">
            <div class="tf-mini-bar" style="width:${jW(b.j1)};background:${jColor(b.j1)}"></div>
          </div>
          <span style="width:12px;text-align:right;color:${jColor(b.j1)}">${b.j1>0?"▲":b.j1<0?"▼":"—"}</span>
        </div>
        <div class="tf-bar-row">
          <span style="width:50px">DOL</span>
          <div style="flex:1;background:rgba(124,58,237,.08);border-radius:99px;height:3px">
            <div class="tf-mini-bar" style="width:${jW(b.j2)};background:${jColor(b.j2)}"></div>
          </div>
          <span style="width:12px;text-align:right;color:${jColor(b.j2)}">${b.j2>0?"▲":b.j2<0?"▼":"—"}</span>
        </div>
        <div class="tf-bar-row">
          <span style="width:50px">PO3</span>
          <div style="flex:1;background:rgba(124,58,237,.08);border-radius:99px;height:3px">
            <div class="tf-mini-bar" style="width:${jW(b.j3)};background:${jColor(b.j3)}"></div>
          </div>
          <span style="width:12px;text-align:right;color:${jColor(b.j3)}">${b.j3>0?"▲":b.j3<0?"▼":"—"}</span>
        </div>
      </div>
      <div class="tf-verdict" style="color:${vc}">${b.verdict}</div>`;
    rows.appendChild(row);
  });
}

// ── ECONOMIC CALENDAR ──
async function loadCalendar(){
  try{
    const r=await fetch("/api/calendar");
    const d=await r.json();
    renderCalendar(d);
  }catch(e){}
}

function renderCalendar(d){
  document.getElementById("cal-month").textContent=d.month_label||"";
  const warn=document.getElementById("cal-warning");
  if(d.is_danger_day){
    warn.innerHTML='<div class="cal-warning">⚠ Vandaag is er een high-impact event — wees voorzichtig!</div>';
  } else if(d.is_nfp_week){
    warn.innerHTML='<div class="cal-warning">⚠ NFP week — vermijd donderdag en vrijdag</div>';
  } else {
    warn.innerHTML="";
  }

  const el=document.getElementById("cal-events");
  if(!d.events||!d.events.length){el.innerHTML='<div style="color:var(--text3);font-size:11px">Geen events gevonden</div>';return}
  el.innerHTML=d.events.map(e=>{
    const isToday=e.date===d.today;
    const badge=e.impact==="high"?'<span class="cal-badge cal-high">HIGH</span>':'<span class="cal-badge cal-medium">MED</span>';
    const dateStr=e.date.slice(5); // MM-DD
    return `<div class="cal-event" style="${isToday?"background:rgba(124,58,237,.06);border-radius:6px;padding:6px 8px;margin:0 -8px":""}">
      <div class="cal-date">${dateStr}<br><span class="cal-time">${e.time}</span></div>
      <div style="flex:1">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
          <div class="cal-name">${e.name}</div>${badge}
          <span style="font-size:9px;color:var(--text3);margin-left:auto">${e.currency}</span>
        </div>
        <div class="cal-note">${e.note}</div>
      </div>
    </div>`;
  }).join("");
}

// ── TRADE DETAIL MODAL ──
let modalChart=null, modalSeries=null;

function openTradeModal(trade){
  // Show modal
  document.getElementById("trade-modal").style.display="flex";
  const w=trade.outcome==="win";
  document.getElementById("modal-title").textContent=
    `Trade #${trade.id} — ${trade.pair} ${trade.direction} — ${w?"✓ WIN":"✗ LOSS"}`;

  // Details
  document.getElementById("modal-details").innerHTML=`
    <div class="modal-stat"><div class="l">Entry</div><div class="v" style="color:var(--glow3)">${trade.entry_price}</div></div>
    <div class="modal-stat"><div class="l">Exit</div><div class="v" style="color:${w?"var(--green)":"var(--red)"}">${trade.exit_price}</div></div>
    <div class="modal-stat"><div class="l">Stop Loss</div><div class="v" style="color:var(--red)">${trade.sl}</div></div>
    <div class="modal-stat"><div class="l">Take Profit</div><div class="v" style="color:var(--green)">${trade.tp}</div></div>
    <div class="modal-stat"><div class="l">Pips</div><div class="v" style="color:${w?"var(--green)":"var(--red)"}">${trade.pips>0?"+":""}${trade.pips}</div></div>
    <div class="modal-stat"><div class="l">P&L</div><div class="v" style="color:${w?"var(--green)":"var(--red)"}">${trade.pnl_eur>0?"+":""}€${trade.pnl_eur}</div></div>
    <div class="modal-stat"><div class="l">Bias Score</div><div class="v" style="color:var(--glow2)">${trade.bias_score>0?"+":""}${trade.bias_score}</div></div>
    <div class="modal-stat"><div class="l">SL Pips</div><div class="v" style="color:var(--text2)">${trade.sl_pips}</div></div>`;

  // Build mini chart from main chart data around the trade
  setTimeout(()=>buildModalChart(trade), 50);
}

function buildModalChart(trade){
  const el=document.getElementById("modal-chart");
  el.innerHTML="";

  if(modalChart){ try{modalChart.remove()}catch(e){} modalChart=null; }

  modalChart=LightweightCharts.createChart(el,{
    layout:{background:{type:"Solid",color:"#08050f"},textColor:"#5a4e80"},
    grid:{vertLines:{color:"rgba(45,31,94,.25)"},horzLines:{color:"rgba(45,31,94,.25)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#2d1f5e",timeVisible:true,secondsVisible:false},
    rightPriceScale:{borderColor:"#2d1f5e"},
    handleScroll:true,handleScale:true,
  });
  new ResizeObserver(()=>{if(modalChart)modalChart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);

  modalSeries=modalChart.addCandlestickSeries({
    upColor:"#7c3aed",downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#a78bfa",borderDownColor:"#3d2b7a",
    wickUpColor:"#a78bfa",wickDownColor:"#3d2b7a",
  });

  // Get candles around the trade from the main series data
  // Filter main chart data to window around trade entry/exit
  const allData=S.series?S.series.data():[];
  if(!allData||!allData.length){ el.innerHTML='<div style="padding:20px;color:#a78bfa;font-size:12px">Geen candledata beschikbaar — run de backtest opnieuw</div>'; return; }

  const entryTs=trade.entry_ts;
  const exitTs=trade.exit_ts;
  const window=Math.max(exitTs-entryTs, 86400)*3; // 3x the trade duration
  const filtered=allData.filter(c=>c.time>=entryTs-window && c.time<=exitTs+window);
  if(!filtered.length){ el.innerHTML='<div style="padding:20px;color:#a78bfa;font-size:12px">Geen data in dit venster</div>'; return; }

  modalSeries.setData(filtered);

  // SL/TP price lines
  modalSeries.createPriceLine({price:trade.entry_price,color:"#a78bfa",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"Entry"});
  modalSeries.createPriceLine({price:trade.sl,color:"#f87171",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"SL"});
  modalSeries.createPriceLine({price:trade.tp,color:"#34d399",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"TP"});

  // Entry/exit markers
  const markers=[
    {time:entryTs,position:trade.direction==="LONG"?"belowBar":"aboveBar",color:trade.direction==="LONG"?"#7c3aed":"#f59e0b",shape:trade.direction==="LONG"?"arrowUp":"arrowDown",text:`Entry ${trade.entry_price}`},
    {time:exitTs,position:"inBar",color:trade.outcome==="win"?"#34d399":"#f87171",shape:"circle",text:`Exit ${trade.exit_price}`},
  ];
  modalSeries.setMarkers(markers);
  modalChart.timeScale().fitContent();
}

function closeModal(e){
  if(e&&e.target!==document.getElementById("trade-modal")) return;
  document.getElementById("trade-modal").style.display="none";
  if(modalChart){ try{modalChart.remove()}catch(e){} modalChart=null; }
}

// ── LIVE ENGINE ──
async function setSlTp(id, type){
  const inp = document.getElementById(`${type}-inp-${id}`);
  if(!inp) return;
  const val = parseFloat(inp.value);
  if(!val || val <= 0){ alert(`Vul een geldig ${type.toUpperCase()} prijsniveau in.`); return; }
  const body = { id };
  body[type] = val;
  const r = await fetch("/api/engine/set_sl_tp",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json();
  if(!d.ok){ alert(d.error||"Instellen mislukt"); return; }
  // Visuele bevestiging
  inp.style.borderColor = "var(--green)";
  setTimeout(()=>pollEngineStatus(), 500);
}

async function closeTrade(id, pair, entry){
  if(!confirm(`Trade #${id} sluiten? (${pair} @ ${entry})`)) return;
  const r = await fetch("/api/engine/close_trade",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});
  const d = await r.json();
  if(d.ok){
    pollEngineStatus();
  } else {
    alert(d.error||"Sluiten mislukt");
  }
}

async function pauseEngine(){
  await fetch("/api/engine/pause",{method:"POST"});
  pollEngineStatus();
}

async function resumeEngine(){
  await fetch("/api/engine/resume",{method:"POST"});
  pollEngineStatus();
}

// ── CONFIG PRESETS ──
async function savePreset(){
  const name = document.getElementById("lt-preset-name").value.trim();
  if(!name){ alert("Geef de preset een naam."); return; }
  const cfg = {
    pair:        document.getElementById("lt-pair").value,
    tf:          document.getElementById("lt-tf").value,
    capital:     document.getElementById("lt-capital").value,
    lotsize_eur: parseFloat(document.getElementById("lt-lot-eur").value)||1,
    lotsize_xau: parseFloat(document.getElementById("lt-lot-xau").value)||1,
    lotsize:     parseFloat(document.getElementById("lt-lot-eur").value)||1,
    min_score:   document.getElementById("lt-score").value,
    use_ob:      document.getElementById("lt-ob").checked,
    use_trend:   document.getElementById("lt-trend").checked,
    use_eq:      document.getElementById("lt-eq").checked,
    use_session: document.getElementById("lt-session").checked,
    use_sweep:   document.getElementById("lt-sweep").checked,
    trade_both:  document.getElementById("lt-pair").value === "BOTH",
    spread_pips: parseFloat(document.getElementById("lt-spread").value)||0,
    slippage_pips: parseFloat(document.getElementById("lt-slip").value)||0,
    max_daily_loss: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-loss").value)||0) : 0,
    max_trades:     document.getElementById("lt-risk-toggle").checked ? (parseInt(document.getElementById("lt-max-trades").value)||0) : 0,
    max_risk_pct:   document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-risk-pct").value)||0) : 0,
    discord_webhook: document.getElementById("lt-discord").value.trim(),
  };
  const r = await fetch("/api/presets/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,config:cfg})});
  const d = await r.json();
  if(d.ok){
    document.getElementById("lt-preset-name").value = "";
    renderPresets(d.presets);
  } else { alert(d.error||"Opslaan mislukt"); }
}

async function deletePreset(name){
  if(!confirm(`Preset "${name}" verwijderen?`)) return;
  const r = await fetch("/api/presets/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
  const d = await r.json();
  renderPresets(d.presets);
}

async function loadPresetIntoForm(name){
  const r = await fetch("/api/presets");
  const presets = await r.json();
  if(!presets[name]) return;
  const c = presets[name].config;
  // Vul alle velden in
  if(c.pair)        document.getElementById("lt-pair").value = c.pair;
  if(c.tf)          document.getElementById("lt-tf").value   = c.tf;
  if(c.capital)     document.getElementById("lt-capital").value = c.capital;
  if(c.lotsize_eur) document.getElementById("lt-lot-eur").value = c.lotsize_eur;
  if(c.lotsize_xau) document.getElementById("lt-lot-xau").value = c.lotsize_xau;
  if(c.min_score)   document.getElementById("lt-score").value = c.min_score;
  document.getElementById("lt-ob").checked      = !!c.use_ob;
  document.getElementById("lt-trend").checked   = !!c.use_trend;
  document.getElementById("lt-eq").checked      = !!c.use_eq;
  document.getElementById("lt-session").checked = !!c.use_session;
  document.getElementById("lt-sweep").checked   = !!c.use_sweep;
  if(c.discord_webhook) document.getElementById("lt-discord").value = c.discord_webhook;
  if(c.spread_pips)   document.getElementById("lt-spread").value = c.spread_pips;
  if(c.slippage_pips) document.getElementById("lt-slip").value   = c.slippage_pips;
}

function renderPresets(presets){
  const el = document.getElementById("lt-presets-list");
  if(!el) return;
  const names = Object.keys(presets||{});
  if(!names.length){
    el.innerHTML = '<div style="font-size:10px;color:var(--text3)">Nog geen presets opgeslagen.</div>';
    return;
  }
  el.innerHTML = names.map(n=>`
    <div style="display:flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:rgba(8,5,24,.6)">
      <span style="flex:1;font-size:11px;color:var(--text2)">${n}</span>
      <span style="font-size:9px;color:var(--text3)">${(presets[n].saved_at||"").slice(0,16)}</span>
      <button onclick="loadPresetIntoForm('${n}')" style="padding:2px 8px;border-radius:4px;border:1px solid var(--border2);background:rgba(124,58,237,.15);color:var(--glow2);font-size:10px;cursor:pointer">Laden</button>
      <button onclick="deletePreset('${n}')" style="padding:2px 8px;border-radius:4px;border:1px solid rgba(248,113,113,.3);background:rgba(248,113,113,.08);color:var(--red);font-size:10px;cursor:pointer">✕</button>
    </div>`).join("");
}

async function initPresets(){
  const r = await fetch("/api/presets");
  const d = await r.json();
  renderPresets(d);
}
initPresets();

async function startEngine(){
  const cfg = {
    pair:        document.getElementById("lt-pair").value,
    tf:          document.getElementById("lt-tf").value,
    capital:     document.getElementById("lt-capital").value,
    lotsize_eur: parseFloat(document.getElementById("lt-lot-eur").value)||1,
    lotsize_xau: parseFloat(document.getElementById("lt-lot-xau").value)||1,
    lotsize:     parseFloat(document.getElementById("lt-lot-eur").value)||1,
    min_score:   document.getElementById("lt-score").value,
    use_ob:      document.getElementById("lt-ob").checked,
    use_trend:   document.getElementById("lt-trend").checked,
    use_eq:      document.getElementById("lt-eq").checked,
    use_session: document.getElementById("lt-session").checked,
    use_sweep:   document.getElementById("lt-sweep").checked,
    be_trigger:  0,
    trade_both:  document.getElementById("lt-pair").value === "BOTH",
    spread_pips: parseFloat(document.getElementById("lt-spread").value)||0,
    slippage_pips: parseFloat(document.getElementById("lt-slip").value)||0,
    max_daily_loss: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-loss").value)||0) : 0,
    max_trades:     document.getElementById("lt-risk-toggle").checked ? (parseInt(document.getElementById("lt-max-trades").value)||0) : 0,
    max_risk_pct:   document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-risk-pct").value)||0) : 0,
    discord_webhook: document.getElementById("lt-discord").value.trim(),
  };
  // Sync chart to engine pair/tf
  S.pair = cfg.pair==="BOTH" ? "EURUSD" : cfg.pair;
  S.tf   = cfg.tf;
  document.querySelectorAll("#pg .toggle-btn").forEach((b,i)=>b.classList.toggle("active",(i===0&&cfg.pair==="EURUSD")||(i===1&&cfg.pair==="XAUUSD")));
  document.querySelectorAll("#tg .toggle-btn").forEach((b,i)=>b.classList.toggle("active",["15M","1H","4H"][i]===cfg.tf));
  updateChartPriceFormat();
  await loadAll();

  const r = await fetch("/api/engine/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});
  const d = await r.json();
  if(d.ok || d.running){
    document.getElementById("lt-start-btn").disabled=true;
    document.getElementById("lt-start-btn").style.opacity=".4";
    document.getElementById("lt-stop-btn").disabled=false;
    document.getElementById("lt-stop-btn").style.opacity="1";
    document.getElementById("engine-status-badge").textContent="● ACTIEF";
    document.getElementById("engine-status-badge").style.background="rgba(52,211,153,.15)";
    document.getElementById("engine-status-badge").style.color="var(--green)";
    document.getElementById("engine-status-badge").style.borderColor="rgba(52,211,153,.3)";
  }
}

async function stopEngine(){
  await fetch("/api/engine/stop",{method:"POST"});
  document.getElementById("lt-start-btn").disabled=false;
  document.getElementById("lt-start-btn").style.opacity="1";
  document.getElementById("lt-stop-btn").disabled=true;
  document.getElementById("lt-stop-btn").style.opacity=".4";
  document.getElementById("engine-status-badge").textContent="● GESTOPT";
  document.getElementById("engine-status-badge").style.background="rgba(90,78,128,.2)";
  document.getElementById("engine-status-badge").style.color="var(--text3)";
  document.getElementById("engine-status-badge").style.borderColor="var(--border)";
}

async function clearLiveLog(){
  await fetch("/api/engine/clear",{method:"POST"});
  renderEngineStatus({open_trades:[],closed_trades:[],logs:[],stats:{total:0,wins:0,losses:0,winrate:0,total_pnl:0},scan_count:0,last_scan:null});
}

async function pollEngineStatus(){
  if(S.page !== "live") return;
  try{
    const r = await fetch("/api/engine/status");
    const d = await r.json();
    renderEngineStatus(d);
  }catch(e){}
}

function renderEngineStatus(d){
  // Open trades tellen bijhouden voor auto-refresh logica
  window._lastOpenTrades = (d.open_trades||[]).length;

  // Sync knoppen met echte engine status
  const startBtn  = document.getElementById("lt-start-btn");
  const stopBtn   = document.getElementById("lt-stop-btn");
  const pauseBtn  = document.getElementById("lt-pause-btn");
  if(startBtn && stopBtn){
    startBtn.disabled      = d.running;
    startBtn.style.opacity = d.running ? ".4" : "1";
    stopBtn.disabled       = !d.running;
    stopBtn.style.opacity  = d.running ? "1" : ".4";
  }
  // Pauze knop
  if(pauseBtn){
    if(!d.running){
      pauseBtn.style.display = "none";
    } else {
      pauseBtn.style.display = "";
      if(d.paused){
        pauseBtn.textContent = "▶ Hervat";
        pauseBtn.style.background = "rgba(34,197,94,.15)";
        pauseBtn.style.color = "var(--green)";
        pauseBtn.style.borderColor = "rgba(34,197,94,.3)";
        pauseBtn.onclick = resumeEngine;
      } else {
        pauseBtn.textContent = "⏸ Pauze";
        pauseBtn.style.background = "rgba(251,191,36,.1)";
        pauseBtn.style.color = "var(--amber)";
        pauseBtn.style.borderColor = "rgba(251,191,36,.3)";
        pauseBtn.onclick = pauseEngine;
      }
    }
  }

  // Engine badge — toon ook paused status
  const badge = document.getElementById("engine-status-badge");
  if(badge){
    if(d.stopped_by_risk){
      badge.textContent = "⛔ GESTOPT — RISICO LIMIET";
      badge.style.background = "rgba(248,113,113,.2)";
      badge.style.color = "var(--red)";
      badge.style.borderColor = "rgba(248,113,113,.4)";
    } else if(d.running && d.paused){
      badge.textContent = "⏸ GEPAUZEERD";
      badge.style.background = "rgba(251,191,36,.12)";
      badge.style.color = "var(--amber)";
      badge.style.borderColor = "rgba(251,191,36,.3)";
    } else if(d.running){
      badge.textContent = "● ACTIEF";
      badge.style.background = "rgba(34,197,94,.12)";
      badge.style.color = "var(--green)";
      badge.style.borderColor = "rgba(34,197,94,.3)";
    } else {
      badge.textContent = "● GESTOPT";
      badge.style.background = "rgba(90,78,128,.2)";
      badge.style.color = "var(--text3)";
      badge.style.borderColor = "var(--border)";
    }
  }

  // Weekend / Market open banner
  const banner = document.getElementById("weekend-banner");
  const openBanner = document.getElementById("market-open-banner");
  if(banner) banner.style.display = d.is_weekend ? "block" : "none";
  if(openBanner) openBanner.style.display = d.is_weekend ? "none" : "block";

  // Dagelijks P&L tonen in scans card
  const dailyEl = document.getElementById("lt-last-scan");
  if(dailyEl && d.daily_pnl !== undefined){
    const dp = d.daily_pnl || 0;
    dailyEl.textContent = `laatste: ${d.last_scan||"—"} · Dagelijks: €${dp>=0?"+":""}${dp.toFixed(2)}`;
    dailyEl.style.color = dp >= 0 ? "var(--green)" : "var(--red)";
  }

  // Scan info + uptime
  const scansEl = document.getElementById("lt-scans");
  if(scansEl) scansEl.textContent = d.scan_count || 0;
  const scanInfoEl = document.getElementById("engine-scan-info");
  if(scanInfoEl){
    const uptimePart = d.uptime && d.running ? ` · ⏱ ${d.uptime}` : "";
    const pausedPart = d.paused ? " · ⏸ GEPAUZEERD" : "";
    scanInfoEl.textContent = d.last_scan ? `laatste scan: ${d.last_scan}${uptimePart}${pausedPart}` : "";
  }

  // Countdown timer — tijd tot volgende scan (20s interval)
  const cdEl = document.getElementById("lt-scan-countdown");
  if(cdEl && d.running && !d.paused){
    // Wis vorige timer
    if(window._scanCountdownTimer) clearInterval(window._scanCountdownTimer);
    let secs = 20;
    cdEl.textContent = `volgende scan: ${secs}s`;
    window._scanCountdownTimer = setInterval(()=>{
      secs--;
      if(secs <= 0){
        clearInterval(window._scanCountdownTimer);
        cdEl.textContent = "scant nu...";
        secs = 20;
      } else {
        cdEl.textContent = `volgende scan: ${secs}s`;
      }
    }, 1000);
  } else if(cdEl){
    if(window._scanCountdownTimer) clearInterval(window._scanCountdownTimer);
    cdEl.textContent = d.paused ? "⏸ gepauzeerd" : "";
  }

  // Stats
  const s = d.stats || {};
  document.getElementById("lt-total").textContent = s.total || 0;
  document.getElementById("lt-wr").textContent    = s.total > 0 ? s.winrate+"%" : "—";
  document.getElementById("lt-wl").textContent    = `${s.wins||0}W / ${s.losses||0}L`;
  const pnl = s.total_pnl || 0;
  document.getElementById("lt-pnl").textContent  = `€${pnl >= 0 ? "+":""}${pnl.toFixed(2)}`;
  document.getElementById("lt-pnl").style.color   = pnl >= 0 ? "var(--green)" : "var(--red)";

  // Open trades table
  const otb = document.getElementById("live-open-tbody");
  if(!d.open_trades || !d.open_trades.length){
    otb.innerHTML='<tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">Geen open posities</td></tr>';
  } else {
    otb.innerHTML = d.open_trades.map(t=>{
      const pnl  = t.pnl_eur || 0;
      const pc   = pnl >= 0 ? "win" : "loss";
      const slV  = t.sl   ? t.sl   : "";
      const tpV  = t.tp   ? t.tp   : "";
      const slPH = t.sl   ? t.sl   : "Geen SL";
      const tpPH = t.tp   ? t.tp   : "Geen TP";
      return `<tr>
        <td>${t.id}</td>
        <td>${t.pair}</td>
        <td><span class="pill ${t.direction==="LONG"?"pill-purple":"pill-amber"}">${t.direction}</span></td>
        <td>${t.entry_price}</td>
        <td style="color:var(--glow3)">${t.live_price||"—"}</td>
        <td class="${pc}">${pnl>=0?"+":""}€${pnl.toFixed(2)}</td>
        <td>
          <div style="display:flex;gap:4px;align-items:center">
            <input id="sl-inp-${t.id}" type="number" step="0.00001" value="${slV}" placeholder="${slPH}"
              style="width:90px;padding:3px 6px;border-radius:4px;border:1px solid ${t.sl?"var(--green)":"rgba(248,113,113,.4)"};background:rgba(8,5,24,.8);color:var(--text);font-size:10px;font-family:'JetBrains Mono',monospace">
            <button onclick="setSlTp(${t.id},'sl')" style="padding:3px 7px;border-radius:4px;border:1px solid var(--border2);background:rgba(34,197,94,.15);color:var(--green);font-size:10px;cursor:pointer;white-space:nowrap">✓ SL</button>
          </div>
        </td>
        <td>
          <div style="display:flex;gap:4px;align-items:center">
            <input id="tp-inp-${t.id}" type="number" step="0.00001" value="${tpV}" placeholder="${tpPH}"
              style="width:90px;padding:3px 6px;border-radius:4px;border:1px solid ${t.tp?"var(--green)":"rgba(124,58,237,.4)"};background:rgba(8,5,24,.8);color:var(--text);font-size:10px;font-family:'JetBrains Mono',monospace">
            <button onclick="setSlTp(${t.id},'tp')" style="padding:3px 7px;border-radius:4px;border:1px solid var(--border2);background:rgba(124,58,237,.15);color:var(--glow2);font-size:10px;cursor:pointer;white-space:nowrap">✓ TP</button>
          </div>
        </td>
        <td style="font-size:9px;color:var(--glow2)">${t.filters||"FVG"}</td>
        <td>
          <button onclick="closeTrade(${t.id},'${t.pair}',${t.entry_price})"
            style="padding:4px 10px;border-radius:5px;border:1px solid rgba(248,113,113,.4);background:rgba(248,113,113,.1);color:var(--red);font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap">
            ✕ Sluit
          </button>
        </td>
      </tr>`;
    }).join("");
    // Draw markers on chart for open trades
    const markers = d.open_trades.map(t=>({
      time: t.opened_ts,
      position: t.direction==="LONG"?"belowBar":"aboveBar",
      color: t.direction==="LONG"?"#7c3aed":"#f59e0b",
      shape: t.direction==="LONG"?"arrowUp":"arrowDown",
      text: `${t.direction} ${t.entry_price}`
    }));
    try{ S.series && S.series.setMarkers(markers); }catch(e){}
  }

  // Closed trades table
  const ctb = document.getElementById("live-closed-tbody");
  if(!d.closed_trades || !d.closed_trades.length){
    ctb.innerHTML='<tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">Nog geen gesloten trades</td></tr>';
  } else {
    ctb.innerHTML = [...d.closed_trades].reverse().map(t=>{
      const w = t.outcome==="win";
      const be = t.outcome==="be";
      const cls = w?"win":be?"":"loss";
      return `<tr>
        <td>${t.id}</td>
        <td style="font-size:9px">${(t.opened_at||"").slice(5,16)}</td>
        <td>${t.pair}</td>
        <td><span class="pill ${t.direction==="LONG"?"pill-purple":"pill-amber"}">${t.direction}</span></td>
        <td>${t.entry_price}</td><td>${t.exit_price||"—"}</td>
        <td class="${cls}">${(t.pips||0)>0?"+":""}${t.pips||0}${be?" (BE)":""}</td>
        <td class="${cls}">${(t.pnl_eur||0)>=0?"+":""}€${(t.pnl_eur||0).toFixed(2)}</td>
        <td>${t.bias_score>0?"+":""}${t.bias_score}</td>
        <td><button onclick="deleteTrade(${t.id})" style="font-size:9px;padding:1px 6px;border:1px solid rgba(248,113,113,.3);border-radius:3px;background:transparent;color:var(--red);cursor:pointer">✕</button></td>
      </tr>`;
    }).join("");
  }

  // Engine log
  const el = document.getElementById("engine-log-list");
  if(d.logs && d.logs.length){
    el.innerHTML = [...d.logs].reverse().map(l=>{
      const col = l.level==="TRADE" ? "var(--glow2)" : l.level==="ERROR" ? "var(--red)" : l.level==="START"||l.level==="STOP" ? "var(--amber)" : "var(--text2)";
      return `<div style="padding:2px 0;border-bottom:1px solid rgba(45,31,94,.3)"><span style="color:var(--text3)">${l.time}</span> <span style="color:${col}">[${l.level}]</span> ${l.msg}</div>`;
    }).join("");
  }
}

function exportEngineLogCSV(){
  fetch("/api/engine/status").then(r=>r.json()).then(d=>{
    const rows=[["Pair","Richting","Entry","SL","TP","PnL (€)","Datum"]];
    (d.closed_trades||[]).forEach(t=>rows.push([
      t.pair,t.direction,t.entry_price,t.sl,t.tp,
      t.pnl_eur||"",t.opened_at||""
    ]));
    const csv=rows.map(r=>r.join(";")).join("\n");
    const a=document.createElement("a");
    a.href="data:text/csv;charset=utf-8,\uFEFF"+encodeURIComponent(csv);
    a.download=`gitchi_trades_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
  });
}

async function deleteTrade(id){
  if(!confirm(`Trade #${id} verwijderen uit de log?`)) return;
  await fetch("/api/engine/delete_trade",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id})
  });
}

function exportLiveCSV(){
  fetch("/api/engine/status").then(r=>r.json()).then(d=>{
    if(!d.closed_trades||!d.closed_trades.length) return;
    const rows=[["Pair","Richting","Entry","SL","TP","PnL (€)","Datum"]];
    d.closed_trades.forEach(t=>rows.push([
      t.pair,t.direction,t.entry_price,t.sl,t.tp,
      t.pnl_eur||"",t.opened_at||""
    ]));
    const csv=rows.map(r=>r.join(";")).join("\n");
    const a=document.createElement("a");
    a.href="data:text/csv;charset=utf-8,\uFEFF"+encodeURIComponent(csv);
    a.download=`gitchi_live_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
  });
}

document.addEventListener("DOMContentLoaded",()=>{
  lucide.createIcons();
  try{initChart()}catch(e){document.getElementById("chart").innerHTML=`<div style="padding:40px;color:#a78bfa;font-family:Inter,sans-serif">Chart fout: ${e.message}</div>`}
  loadAll().catch(console.error);

  // ── Live price fetch every 30 seconds from server ──
  // Stores latest price in S.livePrice, chart updates every second
  async function fetchLivePrice(){
    if(S.page !== "live") return;
    try{
      const r = await fetch(`/api/price?pair=${S.pair}`);
      const d = await r.json();
      if(d.price && d.price > 0){
        const dec = S.pair==="XAUUSD" ? 2 : 5;
        const priceEl = document.getElementById("topbar-price");
        const oldPrice = S.livePrice || 0;
        S.livePrice = d.price;
        priceEl.textContent = d.price.toFixed(dec);
        priceEl.style.color = d.price > oldPrice ? "var(--green)" : d.price < oldPrice ? "var(--red)" : "var(--glow3)";
        setTimeout(()=>{ priceEl.style.color = "var(--glow3)"; }, 800);
      }
    }catch(e){}
  }

  // Fetch price every 30 seconds
  setInterval(fetchLivePrice, 20000);

  // ── Update the live candle every second using latest fetched price ──
  setInterval(()=>{
    if(S.page !== "live") return;
    if(!S.series || !S.lastCandleTime || S.livePrice <= 0) return;
    try{
      const price = S.livePrice;
      // Always update the last known candle with current price
      // This moves the close in real time like TradingView
      S.lastCandleHigh = Math.max(S.lastCandleHigh, price);
      S.lastCandleLow  = Math.min(S.lastCandleLow,  price);
      S.series.update({
        time:  S.lastCandleTime,
        open:  S.lastCandleOpen,
        high:  S.lastCandleHigh,
        low:   S.lastCandleLow,
        close: price,
      });
    }catch(e){}
  }, 1000);

  // ── Countdown timer ──
  let countdown = 30;
  setInterval(()=>{
    if(S.page !== "live") return;
    countdown--;
    if(countdown <= 0){ countdown = 30; fetchLivePrice(); }
    const el = document.getElementById("refresh-countdown");
    if(el) el.textContent = countdown + "s";
  }, 1000);

  // ── Full candle reload every 5 minutes ──
  setInterval(()=>{
    if(S.page === "live") loadCandles().catch(console.error);
  }, 300000);

  // ── Engine status poll every 5 seconds ──
  setInterval(pollEngineStatus, 5000);

  // ── Auto-refresh na 6u om DOM bloat te voorkomen ──
  // Refresht alleen als er geen open trades zijn (veilig)
  const PAGE_START = Date.now();
  setInterval(()=>{
    const ageH = (Date.now() - PAGE_START) / 3600000;
    if(ageH >= 6){
      const hasOpen = (window._lastOpenTrades||0) > 0;
      if(!hasOpen){
        console.log("[GAMAN] Auto-refresh na 6u uptime — geen open trades");
        location.reload();
      } else {
        console.log("[GAMAN] Auto-refresh uitgesteld — open trades aanwezig");
      }
    }
  }, 300000); // check elke 5 minuten

  // ── Data source indicator poll every 5 seconds ──
  async function updateDataSource(){
    try{
      const r = await fetch("/api/datasource");
      const d = await r.json();
      const o = d.overall || {};
      const dot = document.getElementById("ds-dot");
      const lbl = document.getElementById("ds-label");
      const badge = document.getElementById("data-source-badge");
      if(!dot || !lbl || !badge) return;
      dot.style.background = o.color || "#888";
      lbl.textContent = "Data: " + (o.label || "—");
      // Badge background subtiel kleuren zodat het opvalt bij fallback
      if(o.status === "tv"){
        badge.style.background = "rgba(34,197,94,.12)";
        badge.style.borderColor = "rgba(34,197,94,.3)";
        badge.style.color = "#22c55e";
      } else if(o.status === "yf" || o.status === "mixed"){
        badge.style.background = "rgba(245,158,11,.12)";
        badge.style.borderColor = "rgba(245,158,11,.3)";
        badge.style.color = "#f59e0b";
      } else {
        badge.style.background = "rgba(90,78,128,.2)";
        badge.style.borderColor = "var(--border)";
        badge.style.color = "var(--text3)";
      }
      // Tooltip met per-pair details
      const det = d.details || {};
      const lines = Object.keys(det).sort().map(k => {
        const s = det[k];
        return `${k}: ${s.source} (${s.bars} bars @ ${s.time})`;
      });
      badge.title = lines.length ? lines.join("\n") : "Nog geen fetches geregistreerd";
    }catch(e){
      const lbl = document.getElementById("ds-label");
      if(lbl) lbl.textContent = "Data: ?";
    }
  }
  setInterval(updateDataSource, 5000);
  updateDataSource();
});
</script>
</body>
</html>"""

@app.route("/mobile")
def mobile():
    return Response(MOBILE_HTML, mimetype="text/html")

MOBILE_HTML = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>我慢 GAMAN</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#04020f;--bg2:#080518;--border:#2d1f5e;--border2:#3d2b7a;
  --glow:#7c3aed;--glow2:#a78bfa;--glow3:#c4b5fd;
  --text:#e8e0ff;--text2:#a89bcc;--text3:#5a4e80;
  --green:#34d399;--red:#f87171;--amber:#fbbf24;
}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;min-height:100vh;padding:16px}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 50% at 50% -10%,rgba(124,58,237,.3),transparent);pointer-events:none;z-index:0}

.logo{text-align:center;font-size:22px;font-weight:700;color:var(--glow3);letter-spacing:2px;margin-bottom:4px;position:relative}
.logo span{font-size:11px;opacity:.6;letter-spacing:3px;display:block;margin-top:2px}
.price-bar{text-align:center;font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:700;color:var(--glow3);margin:8px 0;text-shadow:0 0 20px rgba(124,58,237,.6)}
.market-badge{text-align:center;margin-bottom:16px}
.badge{display:inline-block;padding:4px 14px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.5px}
.badge.open{background:rgba(52,211,153,.15);color:var(--green);border:1px solid rgba(52,211,153,.3)}
.badge.closed{background:rgba(248,113,113,.15);color:var(--red);border:1px solid rgba(248,113,113,.3)}
.badge.active{background:rgba(52,211,153,.15);color:var(--green);border:1px solid rgba(52,211,153,.3)}
.badge.stopped{background:rgba(90,78,128,.2);color:var(--text3);border:1px solid var(--border)}

.card{background:rgba(8,5,24,.8);border:1px solid var(--border);border-radius:12px;margin-bottom:12px;overflow:hidden;position:relative}
.card-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-title{font-size:11px;font-weight:700;color:var(--glow2);text-transform:uppercase;letter-spacing:1px}
.card-body{padding:14px 16px}

.btn{width:100%;padding:16px;border-radius:10px;font-family:'Inter',sans-serif;font-size:16px;font-weight:700;cursor:pointer;border:none;transition:.2s;letter-spacing:.5px;margin-bottom:10px}
.btn-execute{background:linear-gradient(135deg,var(--glow),#5b21b6);color:#fff;box-shadow:0 4px 20px rgba(124,58,237,.4)}
.btn-execute:active{transform:scale(.97)}
.btn-shutdown{background:rgba(248,113,113,.1);color:var(--red);border:2px solid rgba(248,113,113,.4)}
.btn-shutdown:active{transform:scale(.97)}
.btn-close{background:rgba(248,113,113,.06);color:var(--red);border:1px solid rgba(248,113,113,.3);padding:12px;font-size:13px;margin-bottom:0}

.stat-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.stat-box{background:rgba(124,58,237,.06);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center}
.stat-lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.stat-val{font-size:20px;font-weight:700;color:var(--glow3)}
.stat-sub{font-size:10px;color:var(--text3);margin-top:2px}

.trade-row{padding:10px 0;border-bottom:1px solid rgba(45,31,94,.4);display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;align-items:center}
.trade-row:last-child{border-bottom:none}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700}
.pill-long{background:rgba(124,58,237,.2);color:var(--glow2);border:1px solid var(--border2)}
.pill-short{background:rgba(251,191,36,.15);color:var(--amber);border:1px solid rgba(251,191,36,.3)}
.win{color:var(--green);font-weight:600}
.loss{color:var(--red);font-weight:600}

.log-entry{font-family:'JetBrains Mono',monospace;font-size:10px;padding:4px 0;border-bottom:1px solid rgba(45,31,94,.3);color:var(--text2)}
.log-entry:last-child{border-bottom:none}
.log-time{color:var(--text3)}
.log-trade{color:var(--glow2)}
.log-error{color:var(--red)}
.log-info{color:var(--text2)}

.refresh-info{text-align:center;font-size:10px;color:var(--text3);margin-top:8px;margin-bottom:16px}
.engine-status{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;background:rgba(8,5,24,.8);border:1px solid var(--border);border-radius:10px;padding:12px 16px}
.engine-label{font-size:11px;color:var(--text3)}
.engine-val{font-size:12px;font-weight:600}

.no-data{text-align:center;color:var(--text3);font-size:12px;padding:16px 0}
</style>
</head>
<body>
<div style="position:relative;z-index:1">

  <div class="logo">我慢 <span>GAMAN</span></div>
  <div class="price-bar" id="m-price">—</div>
  <div class="market-badge"><span class="badge stopped" id="m-market">Laden...</span></div>
  <div style="text-align:center;margin-bottom:8px">
    <span id="m-data-source" style="display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:14px;background:rgba(90,78,128,.2);border:1px solid var(--border);font-size:10px;color:var(--text3)">
      <span id="m-ds-dot" style="width:7px;height:7px;border-radius:50%;background:#888;display:inline-block"></span>
      <span id="m-ds-label">Data: —</span>
    </span>
  </div>

  <!-- ENGINE STATUS -->
  <div class="engine-status">
    <div>
      <div class="engine-label">Engine Status</div>
      <div class="engine-val" id="m-engine-status">—</div>
    </div>
    <div style="text-align:right">
      <div class="engine-label">Uptime</div>
      <div class="engine-val" id="m-uptime" style="font-size:11px;color:var(--text3)">—</div>
    </div>
  </div>
  <div class="engine-status" style="margin-top:-8px;padding:8px 16px">
    <div style="font-size:10px;color:var(--text3)">Laatste scan</div>
    <div style="font-size:11px;color:var(--text3)" id="m-last-scan">—</div>
    <div style="font-size:10px;color:var(--glow2)" id="m-scan-countdown"></div>
  </div>

  <!-- KNOPPEN -->
  <button class="btn btn-execute" id="m-execute-btn" onclick="mExecute()">EXECUTE</button>
  <button class="btn btn-shutdown" id="m-shutdown-btn" onclick="mShutdown()">SHUT DOWN</button>
  <button class="btn" id="m-pause-btn" onclick="mTogglePause()"
    style="display:none;background:rgba(251,191,36,.1);color:var(--amber);border:2px solid rgba(251,191,36,.4);margin-bottom:10px">
    ⏸ Pauze
  </button>
  <button class="btn btn-close" onclick="mCloseAll()">✕ Sluit Alle Open Trades</button>

  <!-- PRESETS -->
  <div class="card">
    <div class="card-header"><div class="card-title">Config Presets</div></div>
    <div class="card-body" id="m-presets-body">
      <div class="no-data">Laden...</div>
    </div>
  </div>

  <!-- STATS -->
  <div style="margin-top:16px" class="card">
    <div class="card-header"><div class="card-title">Sessie Stats</div><span id="m-daily-pnl" style="font-size:11px"></span></div>
    <div class="card-body">
      <div class="stat-row">
        <div class="stat-box"><div class="stat-lbl">Trades</div><div class="stat-val" id="m-total">0</div><div class="stat-sub">deze sessie</div></div>
        <div class="stat-box"><div class="stat-lbl">Winrate</div><div class="stat-val" id="m-wr">—</div><div class="stat-sub" id="m-wl">0W / 0L</div></div>
      </div>
      <div class="stat-row" style="margin-bottom:0">
        <div class="stat-box"><div class="stat-lbl">Totaal P&L</div><div class="stat-val" id="m-pnl" style="font-size:18px">€0.00</div></div>
        <div class="stat-box"><div class="stat-lbl">Scans</div><div class="stat-val" id="m-scans">0</div></div>
      </div>
    </div>
  </div>

  <!-- OPEN POSITIES -->
  <div class="card">
    <div class="card-header"><div class="card-title">Open Posities</div><span id="m-open-count" style="font-size:10px;color:var(--text3)">0</span></div>
    <div class="card-body" id="m-open-trades"><div class="no-data">Geen open posities</div></div>
  </div>

  <!-- TRADE LOG -->
  <div class="card">
    <div class="card-header"><div class="card-title">Laatste Trades</div></div>
    <div class="card-body" id="m-trade-log"><div class="no-data">Nog geen trades</div></div>
  </div>

  <!-- ENGINE LOG -->
  <div class="card">
    <div class="card-header"><div class="card-title">Engine Log</div></div>
    <div class="card-body" id="m-engine-log"><div class="no-data">Engine nog niet gestart</div></div>
  </div>

  <!-- SYSTEM HEALTH -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">System Health</div>
      <button onclick="mRunHealthCheck()" style="font-size:10px;padding:3px 10px;border:1px solid var(--border2);border-radius:4px;background:rgba(124,58,237,.1);color:var(--glow2);cursor:pointer;font-family:'Inter',sans-serif">
        Check
      </button>
    </div>
    <div class="card-body" id="m-health-checks">
      <div class="no-data">Klik Check om te starten</div>
    </div>
  </div>

  <div class="refresh-info" id="m-refresh-info">Refresht elke 5 seconden</div>
</div>

<script>
let mRunning = false;
let mConfig  = {};

async function mPoll(){
  try{
    // Prijs
    const pr = await fetch("/api/price?pair=EURUSD");
    const pd = await pr.json();
    if(pd.price) document.getElementById("m-price").textContent = pd.price.toFixed(5);

    // Engine status
    const er = await fetch("/api/engine/status");
    const ed = await er.json();
    mRunning = ed.running;
    mConfig  = ed.config || {};

    // Market status
    const mkt = document.getElementById("m-market");
    if(ed.is_weekend){
      mkt.textContent="🔴 MARKT GESLOTEN"; mkt.className="badge closed";
    } else {
      mkt.textContent="🟢 MARKT OPEN"; mkt.className="badge open";
    }

    // Data source indicator
    try{
      const dsr = await fetch("/api/datasource");
      const dsd = await dsr.json();
      const dso = dsd.overall || {};
      const dsBadge = document.getElementById("m-data-source");
      const dsDot   = document.getElementById("m-ds-dot");
      const dsLbl   = document.getElementById("m-ds-label");
      if(dsBadge && dsDot && dsLbl){
        dsDot.style.background = dso.color || "#888";
        dsLbl.textContent = "Data: " + (dso.label || "—");
        if(dso.status === "tv"){
          dsBadge.style.background = "rgba(34,197,94,.12)";
          dsBadge.style.borderColor = "rgba(34,197,94,.3)";
          dsBadge.style.color = "#22c55e";
        } else if(dso.status === "yf" || dso.status === "mixed"){
          dsBadge.style.background = "rgba(245,158,11,.12)";
          dsBadge.style.borderColor = "rgba(245,158,11,.3)";
          dsBadge.style.color = "#f59e0b";
        } else {
          dsBadge.style.background = "rgba(90,78,128,.2)";
          dsBadge.style.borderColor = "var(--border)";
          dsBadge.style.color = "var(--text3)";
        }
      }
    }catch(e){ /* stil falen */ }

    // Engine status
    const esBadge = document.getElementById("m-engine-status");
    if(ed.stopped_by_risk){
      esBadge.textContent="⛔ GESTOPT — RISICO"; esBadge.style.color="var(--red)";
    } else if(ed.running && ed.paused){
      esBadge.textContent="⏸ GEPAUZEERD"; esBadge.style.color="var(--amber)";
    } else if(ed.running){
      esBadge.textContent="● ACTIEF"; esBadge.style.color="var(--green)";
    } else {
      esBadge.textContent="● GESTOPT"; esBadge.style.color="var(--text3)";
    }

    // Uptime
    const uptEl = document.getElementById("m-uptime");
    if(uptEl) uptEl.textContent = (ed.running && ed.uptime) ? ed.uptime : "—";

    // Countdown
    const cdEl = document.getElementById("m-scan-countdown");
    if(cdEl){
      if(ed.running && !ed.paused){
        if(window._mScanTimer) clearInterval(window._mScanTimer);
        let s = 20;
        cdEl.textContent = `volgende scan: ${s}s`;
        window._mScanTimer = setInterval(()=>{
          s--; if(s<=0){ clearInterval(window._mScanTimer); cdEl.textContent="scant nu..."; }
          else cdEl.textContent=`volgende scan: ${s}s`;
        },1000);
      } else {
        if(window._mScanTimer) clearInterval(window._mScanTimer);
        cdEl.textContent = ed.paused ? "⏸ gepauzeerd" : "";
      }
    }

    // Buttons
    document.getElementById("m-execute-btn").style.opacity  = ed.running ? ".4" : "1";
    document.getElementById("m-execute-btn").disabled       = ed.running;
    document.getElementById("m-shutdown-btn").style.opacity = ed.running ? "1" : ".4";
    document.getElementById("m-shutdown-btn").disabled      = !ed.running;

    // Pauze knop
    const pbtn = document.getElementById("m-pause-btn");
    if(pbtn){
      pbtn.style.display = ed.running ? "" : "none";
      if(ed.paused){
        pbtn.textContent="▶ Hervat";
        pbtn.style.background="rgba(34,197,94,.1)";
        pbtn.style.color="var(--green)";
        pbtn.style.borderColor="rgba(34,197,94,.4)";
      } else {
        pbtn.textContent="⏸ Pauze";
        pbtn.style.background="rgba(251,191,36,.1)";
        pbtn.style.color="var(--amber)";
        pbtn.style.borderColor="rgba(251,191,36,.4)";
      }
    }

    // Last scan
    const lsEl = document.getElementById("m-last-scan");
    if(lsEl) lsEl.textContent = ed.last_scan || "—";

    // Daily P&L
    const dp = ed.daily_pnl || 0;
    const dpEl = document.getElementById("m-daily-pnl");
    dpEl.textContent = `Dagelijks: €${dp>=0?"+":""}${dp.toFixed(2)}`;
    dpEl.style.color = dp >= 0 ? "var(--green)" : "var(--red)";

    // Stats
    const s = ed.stats || {};
    document.getElementById("m-total").textContent = s.total || 0;
    document.getElementById("m-wr").textContent    = s.total > 0 ? s.winrate+"%" : "—";
    document.getElementById("m-wl").textContent    = `${s.wins||0}W / ${s.losses||0}L`;
    const pnl = s.total_pnl || 0;
    document.getElementById("m-pnl").textContent   = `€${pnl>=0?"+":""}${pnl.toFixed(2)}`;
    document.getElementById("m-pnl").style.color   = pnl >= 0 ? "var(--green)" : "var(--red)";
    document.getElementById("m-scans").textContent = ed.scan_count || 0;

    // Open trades
    const ot = document.getElementById("m-open-trades");
    document.getElementById("m-open-count").textContent = (ed.open_trades||[]).length + " open";
    if(!ed.open_trades || !ed.open_trades.length){
      ot.innerHTML='<div class="no-data">Geen open posities</div>';
    } else {
      ot.innerHTML = ed.open_trades.map(t=>{
        const pnl  = t.pnl_eur||0;
        const slV  = t.sl || "";
        const tpV  = t.tp || "";
        return `<div style="padding:12px 0;border-bottom:1px solid rgba(45,31,94,.4)">
          <div class="trade-row" style="border:none;padding:0 0 8px 0">
            <span class="pill ${t.direction==="LONG"?"pill-long":"pill-short"}">${t.direction}</span>
            <div><div style="font-size:13px;font-weight:600">${t.pair} #${t.id}</div>
              <div style="font-size:10px;color:var(--text3)">Entry: ${t.entry_price} → Live: ${t.live_price||"—"}</div></div>
            <div class="${pnl>=0?"win":"loss"}" style="font-size:14px;text-align:right">${pnl>=0?"+":""}€${pnl.toFixed(2)}</div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:6px">
            <div>
              <div style="font-size:9px;color:${t.sl?"var(--red)":"var(--text3)"};margin-bottom:2px">STOP LOSS ${t.sl?"✓":""}</div>
              <div style="display:flex;gap:4px">
                <input id="m-sl-${t.id}" type="number" step="0.00001" value="${slV}" placeholder="${t.sl||"Geen SL"}"
                  style="flex:1;padding:6px;border-radius:5px;border:1px solid ${t.sl?"var(--red)":"rgba(248,113,113,.3)"};background:rgba(8,5,24,.8);color:var(--text);font-size:11px;font-family:'JetBrains Mono',monospace">
                <button onclick="mSetSlTp(${t.id},'sl')" style="padding:6px 10px;border-radius:5px;border:1px solid rgba(248,113,113,.4);background:rgba(248,113,113,.15);color:var(--red);font-size:11px;cursor:pointer">✓</button>
              </div>
            </div>
            <div>
              <div style="font-size:9px;color:${t.tp?"var(--green)":"var(--text3)"};margin-bottom:2px">TAKE PROFIT ${t.tp?"✓":""}</div>
              <div style="display:flex;gap:4px">
                <input id="m-tp-${t.id}" type="number" step="0.00001" value="${tpV}" placeholder="${t.tp||"Geen TP"}"
                  style="flex:1;padding:6px;border-radius:5px;border:1px solid ${t.tp?"var(--green)":"rgba(34,197,94,.3)"};background:rgba(8,5,24,.8);color:var(--text);font-size:11px;font-family:'JetBrains Mono',monospace">
                <button onclick="mSetSlTp(${t.id},'tp')" style="padding:6px 10px;border-radius:5px;border:1px solid rgba(34,197,94,.4);background:rgba(34,197,94,.15);color:var(--green);font-size:11px;cursor:pointer">✓</button>
              </div>
            </div>
          </div>
          <button onclick="mCloseTrade(${t.id},'${t.pair}',${t.entry_price})"
            style="width:100%;padding:8px;border-radius:6px;border:1px solid rgba(248,113,113,.4);background:rgba(248,113,113,.1);color:var(--red);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer">
            ✕ Sluit trade #${t.id}
          </button>
        </div>`;
      }).join("");
    }

    // Trade log
    const tl = document.getElementById("m-trade-log");
    const closed = (ed.closed_trades||[]).slice(-5).reverse();
    if(!closed.length){
      tl.innerHTML='<div class="no-data">Nog geen trades</div>';
    } else {
      tl.innerHTML = closed.map(t=>{
        const w = t.outcome==="win";
        return `<div class="trade-row">
          <span class="pill ${t.direction==="LONG"?"pill-long":"pill-short"}">${t.direction}</span>
          <div><div style="font-size:12px;font-weight:600">${t.pair}</div><div style="font-size:10px;color:var(--text3)">${(t.opened_at||"").slice(5,16)}</div></div>
          <div class="${w?"win":"loss"}" style="font-size:13px">${(t.pips||0)>0?"+":""}${t.pips||0}p</div>
          <div class="${w?"win":"loss"}" style="font-size:13px">${(t.pnl_eur||0)>=0?"+":""}€${(t.pnl_eur||0).toFixed(2)}</div>
        </div>`;
      }).join("");
    }

    // Engine log
    const el = document.getElementById("m-engine-log");
    const logs = (ed.logs||[]).slice(-10).reverse();
    if(!logs.length){
      el.innerHTML='<div class="no-data">Engine nog niet gestart</div>';
    } else {
      el.innerHTML = logs.map(l=>{
        const cls = l.level==="TRADE"?"log-trade":l.level==="ERROR"?"log-error":"log-info";
        return `<div class="log-entry"><span class="log-time">${l.time}</span> <span class="${cls}">${l.msg}</span></div>`;
      }).join("");
    }

    document.getElementById("m-refresh-info").textContent = "Bijgewerkt: " + new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("m-refresh-info").textContent = "Verbindingsfout — herprobeert...";
  }
}

async function mSetSlTp(id, type){
  const inp = document.getElementById(`m-${type}-${id}`);
  if(!inp) return;
  const val = parseFloat(inp.value);
  if(!val || val <= 0){ alert(`Vul een geldig ${type.toUpperCase()} niveau in.`); return; }
  const body = {id};
  body[type] = val;
  const r = await fetch("/api/engine/set_sl_tp",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d = await r.json();
  if(!d.ok){ alert(d.error||"Mislukt"); return; }
  inp.style.borderColor = "var(--green)";
  setTimeout(mPoll, 500);
}

async function mCloseTrade(id, pair, entry){
  if(!confirm(`Trade #${id} sluiten?\n${pair} @ ${entry}`)) return;
  const r = await fetch("/api/engine/close_trade",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});
  const d = await r.json();
  if(d.ok){ mPoll(); }
  else { alert(d.error||"Sluiten mislukt"); }
}

async function mTogglePause(){
  const isPaused = document.getElementById("m-pause-btn").textContent.includes("Hervat");
  if(isPaused){
    await fetch("/api/engine/resume",{method:"POST"});
  } else {
    await fetch("/api/engine/pause",{method:"POST"});
  }
  mPoll();
}

// ── Mobile Presets ──
async function mLoadPresets(){
  const r = await fetch("/api/presets");
  const presets = await r.json();
  const el = document.getElementById("m-presets-body");
  if(!el) return;
  const names = Object.keys(presets||{});
  if(!names.length){
    el.innerHTML='<div class="no-data">Geen presets — maak ze aan op de desktop.</div>';
    return;
  }
  el.innerHTML = names.map(n=>{
    const c = presets[n].config||{};
    const info = `${c.pair||"?"} ${c.tf||"?"} | Score≥${c.min_score||"?"}  | Lot EUR:${c.lotsize_eur||"?"} XAU:${c.lotsize_xau||"?"}`;
    return `<div style="padding:10px 0;border-bottom:1px solid rgba(45,31,94,.4)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:13px;font-weight:600;color:var(--glow2)">${n}</span>
        <span style="font-size:9px;color:var(--text3)">${(presets[n].saved_at||"").slice(0,16)}</span>
      </div>
      <div style="font-size:10px;color:var(--text3);margin-bottom:8px">${info}</div>
      <button onclick="mStartWithPreset('${n}')"
        style="width:100%;padding:10px;border-radius:8px;border:none;background:linear-gradient(135deg,var(--glow),#5b21b6);color:#fff;font-family:'Inter',sans-serif;font-size:13px;font-weight:700;cursor:pointer">
        ▶ Start met deze preset
      </button>
    </div>`;
  }).join("");
}

async function mStartWithPreset(name){
  if(mRunning){ alert("Engine draait al — stop eerst."); return; }
  if(!confirm(`Engine starten met preset "${name}"?`)) return;
  const r = await fetch("/api/presets/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
  const d = await r.json();
  if(d.ok){
    mPoll();
  } else {
    alert(d.error||"Starten mislukt");
  }
}

async function mExecute(){
  if(mRunning) return;
  if(!confirm("Engine starten met huidige VPS configuratie?")) return;
  const r = await fetch("/api/engine/status");
  const d = await r.json();
  const cfg = d.config || {};
  if(!Object.keys(cfg).length){ alert("Stel eerst de config in op de desktop versie."); return; }
  const res = await fetch("/api/engine/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});
  const rd = await res.json();
  if(rd.running) mPoll();
}

async function mShutdown(){
  if(!mRunning) return;
  if(!confirm("Engine stoppen?")) return;
  await fetch("/api/engine/stop",{method:"POST"});
  mPoll();
}

async function mCloseAll(){
  if(!confirm("Alle open trades sluiten op huidige marktprijs?")) return;
  const r = await fetch("/api/engine/close_all",{method:"POST"});
  const d = await r.json();
  if(d.ok) alert(`✅ ${d.closed} trade(s) gesloten.`);
  mPoll();
}

async function mRunHealthCheck(){
  const el = document.getElementById("m-health-checks");
  el.innerHTML = '<div class="no-data">Checks uitvoeren...</div>';
  try{
    const r = await fetch("/api/system/health");
    const d = await r.json();
    const order = ["market","engine","tradingview","yfinance","discord","system","uptime"];
    const labels = {
      market:"Markt",tradingview:"TradingView",yfinance:"yFinance",
      discord:"Discord",engine:"Engine",system:"Resources",uptime:"Uptime"
    };
    el.innerHTML = order.map(k=>{
      const c = d[k]||{};
      const s = c.status||"warn";
      const col = s==="ok"?"var(--green)":s==="error"?"var(--red)":"var(--amber)";
      const dot = `<span style="width:8px;height:8px;border-radius:50%;background:${col};display:inline-block;flex-shrink:0"></span>`;
      return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(45,31,94,.3)">
        ${dot}
        <div style="flex:1">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-size:11px;color:var(--text2)">${labels[k]||k}</span>
            <span style="font-size:11px;font-weight:600;color:${col}">${c.msg||"—"}</span>
          </div>
          ${c.detail?`<div style="font-size:9px;color:var(--text3);margin-top:1px">${c.detail}</div>`:""}
        </div>
      </div>`;
    }).join("");
  }catch(e){
    el.innerHTML = `<div class="no-data" style="color:var(--red)">Fout: ${e.message}</div>`;
  }
}

// Poll elke 5 seconden
mPoll();
mLoadPresets();
setInterval(mPoll, 5000);
</script>
</body>
</html>"""

if __name__=="__main__":
    import sys
    print("="*50)
    print("  GITCHI Trading Dashboard")
    print(f"  Python: {sys.executable}")
    print("  Open: http://localhost:5000")
    print("="*50)
    app.run(host="0.0.0.0",port=5000,debug=False)
