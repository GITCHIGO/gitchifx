"""
GITCHI TRADING DASHBOARD v3
============================
Start: python gitchi.py
Open:  http://localhost:5000
Requireten: pip install flask yfinance pandas
"""
from flask import Flask, jsonify, request, Response
import json, math, datetime, threading, time

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    raise SystemExit("Installer eerst: pip install flask yfinance pandas")

app = Flask(__name__)

STATE_FILE   = "gitchi_state.json"
PRESETS_FILE = "gitchi_presets.json"

# ─── DISCORD NOTIFICATIES ─────────────────────────────────────────────────────
def send_discord(webhook_url, message, color=0x7c3aed):
    """Sth een embed bericht to Discord via webhook."""
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
        print(f"[DISCORD] Error: {e}")


def send_discord_with_image(webhook_url, message, image_bytes, color=0x7c3aed, filename="chart.png"):
    """Stuurt een Discord embed met afbeelding (PNG bytes).
    Image wordt als file attachment meegezonden en als embed image gebruikt.
    """
    if not webhook_url:
        return
    try:
        import requests
        payload = {
            "embeds": [{
                "description": message,
                "color": color,
                "image": {"url": f"attachment://{filename}"},
                "footer": {"text": f"GAMAN Trading · {fmt_time_brussels()}"}
            }]
        }
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": (filename, image_bytes, "image/png"),
        }
        requests.post(webhook_url, files=files, timeout=15)
    except Exception as e:
        print(f"[DISCORD-IMG] Error: {e}")


# ─── TRADE CHART RENDERER ──────────────────────────────────────────────────
# Renders een candlestick chart met entry/exit markers en SL/TP lijnen.
# Geoptimaliseerd voor lage RAM (matplotlib Agg backend, expliciet plt.close).
def render_trade_chart(df, trade, exit_data=None):
    """Render een candlestick chart voor een trade.

    Args:
        df: DataFrame met OHLC data (50-100 candles rond entry/exit)
        trade: trade dict met entry_price, direction, sl, tp, pair, tf, opened_ts
        exit_data: optioneel dict met exit_price, exit_ts, pnl_eur, hit

    Returns: PNG bytes of None bij failure
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # geen GUI, lichter
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from io import BytesIO

        if df is None or len(df) < 5:
            return None

        # Pak relevant deel: candles rond entry/exit
        n = len(df)
        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values

        # Index/tijd as
        try:
            times = df.index.to_pydatetime() if hasattr(df.index, "to_pydatetime") else list(range(n))
        except Exception:
            times = list(range(n))

        # Maak figure — donker thema passend bij dashboard
        fig, ax = plt.subplots(figsize=(11, 5.5), facecolor="#0a1428")
        ax.set_facecolor("#04020f")

        # Candles tekenen
        width = 0.7
        for i in range(n):
            color = "#22d3ee" if closes[i] >= opens[i] else "#f87171"  # cyan up, red down
            # Wick
            ax.plot([i, i], [lows[i], highs[i]], color=color, linewidth=0.8, alpha=0.85)
            # Body
            body_low = min(opens[i], closes[i])
            body_high = max(opens[i], closes[i])
            ax.add_patch(plt.Rectangle((i - width / 2, body_low), width, max(body_high - body_low, 0.00001),
                                        facecolor=color, edgecolor=color, alpha=0.9))

        entry_price = trade.get("entry_price", 0)
        direction = trade.get("direction", "LONG")
        sl = trade.get("sl")
        tp = trade.get("tp")
        pair = trade.get("pair", "")
        tf = trade.get("tf", "")

        # Entry markeren — vind candle dichtst bij entry_ts
        entry_idx = None
        opened_ts = trade.get("opened_ts", 0)
        if opened_ts and hasattr(df.index, "view"):
            try:
                import pandas as pd
                entry_dt = pd.Timestamp(opened_ts, unit="s", tz="UTC")
                # Vind dichtstbijzijnde index
                diffs = abs(df.index - entry_dt)
                entry_idx = int(diffs.argmin())
            except Exception:
                entry_idx = max(0, n - 30)  # fallback
        else:
            entry_idx = max(0, n - 30)

        # Entry pijl en label
        entry_color = "#34d399" if direction == "LONG" else "#f87171"
        entry_marker = "^" if direction == "LONG" else "v"
        marker_y = entry_price * (0.9995 if direction == "LONG" else 1.0005)
        ax.scatter([entry_idx], [marker_y], marker=entry_marker, s=200, color=entry_color,
                   edgecolors="white", linewidths=1.5, zorder=10)
        ax.annotate(f" ENTRY {direction}\n {entry_price:.5f}",
                    xy=(entry_idx, marker_y), xytext=(8, 0), textcoords="offset points",
                    color=entry_color, fontsize=9, fontweight="bold",
                    verticalalignment="center")

        # Exit markeren als beschikbaar
        if exit_data:
            exit_idx = n - 1  # default laatste candle
            exit_ts = exit_data.get("exit_ts", 0)
            if exit_ts and hasattr(df.index, "view"):
                try:
                    import pandas as pd
                    exit_dt = pd.Timestamp(exit_ts, unit="s", tz="UTC")
                    diffs = abs(df.index - exit_dt)
                    exit_idx = int(diffs.argmin())
                except Exception:
                    pass

            exit_price = exit_data.get("exit_price", closes[-1])
            pnl = exit_data.get("pnl_eur", 0)
            hit = exit_data.get("hit", "?")
            exit_color = "#34d399" if pnl >= 0 else "#f87171"
            exit_marker = "v" if direction == "LONG" else "^"
            marker_y2 = exit_price * (1.0005 if direction == "LONG" else 0.9995)
            ax.scatter([exit_idx], [marker_y2], marker=exit_marker, s=200, color=exit_color,
                       edgecolors="white", linewidths=1.5, zorder=10)
            ax.annotate(f" EXIT ({hit})\n {exit_price:.5f}\n {pnl:+.2f} EUR",
                        xy=(exit_idx, marker_y2), xytext=(8, 0), textcoords="offset points",
                        color=exit_color, fontsize=9, fontweight="bold",
                        verticalalignment="center")

        # SL en TP horizontale lijnen
        if sl is not None:
            ax.axhline(y=sl, color="#f87171", linestyle="--", linewidth=1.2, alpha=0.7, label=f"SL: {sl:.5f}")
        if tp is not None:
            ax.axhline(y=tp, color="#34d399", linestyle="--", linewidth=1.2, alpha=0.7, label=f"TP: {tp:.5f}")
        # Entry-prijs lijn
        ax.axhline(y=entry_price, color="#7dd3fc", linestyle=":", linewidth=1, alpha=0.5)

        # Styling
        ax.set_title(f"GAMAN  ·  {pair} {tf}  ·  Trade #{trade.get('id', '?')}",
                     color="#e0f2fe", fontsize=13, fontweight="bold", pad=10)
        ax.tick_params(colors="#7dd3fc", labelsize=8)
        ax.spines["bottom"].set_color("#1f3a5e")
        ax.spines["top"].set_color("#1f3a5e")
        ax.spines["left"].set_color("#1f3a5e")
        ax.spines["right"].set_color("#1f3a5e")
        ax.grid(True, color="#1f3a5e", alpha=0.3, linewidth=0.5)
        ax.set_xlim(-1, n)

        # Legend
        if sl is not None or tp is not None:
            leg = ax.legend(loc="upper left", framealpha=0.7, facecolor="#0a1428",
                            edgecolor="#1f3a5e", labelcolor="#e0f2fe", fontsize=9)

        # X-axis labels
        ax.set_xticks([])  # geen tick labels — minder clutter

        plt.tight_layout()

        # Render naar bytes
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=80, facecolor=fig.get_facecolor(),
                    bbox_inches="tight", pad_inches=0.1)
        buf.seek(0)
        png_bytes = buf.read()

        # CRITICAL: cleanup memory — anders memory leak na N renders
        plt.close(fig)
        plt.close("all")
        buf.close()

        return png_bytes
    except Exception as e:
        print(f"[CHART-RENDER] Error: {e}")
        import traceback; traceback.print_exc()
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except:
            pass
        return None


def _check_ram_available_mb():
    """Returns approximate free RAM in MB, or 999 if check fails (default to OK)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 999

# ─── MT5 BRIDGE ─────────────────────────────────────────────────────────────
# Communicatie met de GAMAN_Bridge.mq5 Expert Advisor via file-based bridge.
# EA schrijft heartbeat + leest orders. GAMAN schrijft orders + leest heartbeat.

class MT5Bridge:
    """File-based bridge tussen GAMAN en MetaTrader 5 Expert Advisor.

    Locaties:
    - Heartbeat (van EA):   {files_dir}/gaman_heartbeat.json
    - Order command (naar EA): {files_dir}/gaman_order.json
    - Order result (van EA):   {files_dir}/gaman_result.json
    """

    # Default MT5 Files folder voor IC Markets installatie
    DEFAULT_FILES_DIR = r"C:\Program Files\MetaTrader 5\MQL5\Files"

    # Symbol mapping: GAMAN naam -> MT5 broker naam
    # IC Markets demo gebruikt standaard namen, geen suffix
    SYMBOL_MAP = {
        "EURUSD": "EURUSD",
        "XAUUSD": "XAUUSD",
    }

    def __init__(self, files_dir=None):
        self.files_dir = files_dir or self.DEFAULT_FILES_DIR
        self.last_heartbeat_check = 0
        self.last_heartbeat_data = None
        self.last_heartbeat_age = None  # seconden sinds laatste heartbeat
        self.pending_orders = {}  # order_id -> trade dict, wachten op result

    def _path(self, filename):
        import os
        return os.path.join(self.files_dir, filename)

    def is_available(self):
        """Check of de files directory bestaat en schrijfbaar is."""
        import os
        try:
            return os.path.isdir(self.files_dir) and os.access(self.files_dir, os.W_OK)
        except Exception:
            return False

    def read_heartbeat(self):
        """Lees de laatste heartbeat van de EA.
        Returns: dict met EA status of None als file ontbreekt/ongeldig.
        """
        import os, json, time
        path = self._path("gaman_heartbeat.json")
        if not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            age = time.time() - mtime
            with open(path, "r") as f:
                content = f.read().strip()
            if not content:
                return None
            data = json.loads(content)
            data["_age_sec"] = age
            self.last_heartbeat_data = data
            self.last_heartbeat_age = age
            return data
        except Exception as e:
            print(f"[MT5-BRIDGE] Heartbeat read error: {e}")
            return None

    def is_ea_alive(self, max_age_sec=30):
        """Check of EA recent een heartbeat heeft geschreven (max 30 sec geleden)."""
        hb = self.read_heartbeat()
        if hb is None:
            return False
        return hb.get("_age_sec", 999) < max_age_sec

    def send_order(self, action, symbol, side=None, volume=None, sl=None, tp=None, ticket=None):
        """Schrijf order command naar EA.
        action: "OPEN", "CLOSE", "MODIFY"
        Returns: order_id (str) voor later result lookup
        """
        import os, json, time, uuid
        broker_symbol = self.SYMBOL_MAP.get(symbol, symbol)
        order_id = f"{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
        cmd = {
            "id":     order_id,
            "action": action,
            "symbol": broker_symbol,
        }
        if side is not None:   cmd["side"]   = side
        if volume is not None: cmd["volume"] = round(float(volume), 2)
        if sl is not None:     cmd["sl"]     = round(float(sl), 5)
        if tp is not None:     cmd["tp"]     = round(float(tp), 5)
        if ticket is not None: cmd["ticket"] = str(ticket)

        path = self._path("gaman_order.json")
        try:
            with open(path, "w") as f:
                json.dump(cmd, f)
            print(f"[MT5-BRIDGE] Sent {action} {symbol} {side or ''} vol={volume} sl={sl} tp={tp} id={order_id}")
            return order_id
        except Exception as e:
            print(f"[MT5-BRIDGE] Failed to send order: {e}")
            return None

    def wait_for_result(self, order_id, timeout_sec=10):
        """Wacht op EA result voor specifieke order_id.
        Returns: result dict of None bij timeout.
        """
        import os, json, time
        path = self._path("gaman_result.json")
        start = time.time()
        while time.time() - start < timeout_sec:
            if os.path.isfile(path):
                try:
                    with open(path, "r") as f:
                        content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        if data.get("id") == order_id:
                            # Result is voor ons - delete file
                            try: os.remove(path)
                            except: pass
                            return data
                except Exception:
                    pass
            time.sleep(0.3)
        return None


# Singleton bridge instance — global state, lazy init
_MT5_BRIDGE = None
def get_mt5_bridge():
    global _MT5_BRIDGE
    if _MT5_BRIDGE is None:
        _MT5_BRIDGE = MT5Bridge()
    return _MT5_BRIDGE


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
        self.start_ts       = None   # unix timestamp for uptime berekening
        self.paused         = False  # pauze zonder config te verliezen
        self.daily_pnl      = 0.0   # P&L today
        self.daily_reset    = None  # datum from last reset
        self.stopped_by_risk= False # gestopt door risicobeheer
        self.lock           = threading.Lock()
        self.recent_entries = {}  # pair+tf -> timestamp from last entry
        self._load_state()

    def _discord(self, msg, color=0x7c3aed):
        webhook = self.config.get("discord_webhook","") or "https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ"
        if webhook:
            threading.Thread(target=send_discord, args=(webhook, msg, color), daemon=True).start()

    def _discord_with_trade_chart(self, msg, trade, color=0x7c3aed, exit_data=None):
        """Stuur Discord notificatie met chart screenshot van de trade.

        - Async (eigen thread) zodat engine niet wacht
        - RAM check: skip plaatje als <100MB vrij
        - Fallback: bij render failure → gewone tekst-only notification
        - Toggle via config.send_chart_screenshots (default uit)
        """
        webhook = self.config.get("discord_webhook","") or "https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ"
        if not webhook:
            return

        # Toggle check — als chart screenshots uit staan, val terug op gewone notification
        if not self.config.get("send_chart_screenshots", False):
            threading.Thread(target=send_discord, args=(webhook, msg, color), daemon=True).start()
            return

        # RAM check — als VPS krap zit, skip plaatje
        free_mb = _check_ram_available_mb()
        if free_mb < 100:
            print(f"[CHART-SKIP] Low RAM ({free_mb}MB), skipping chart render")
            threading.Thread(target=send_discord, args=(webhook, msg, color), daemon=True).start()
            return

        def _render_and_send():
            try:
                # Fetch candles rond entry/exit
                pair = trade.get("pair")
                tf = trade.get("tf", "1H")
                df = fetch_candles(pair, tf)
                if df is None or len(df) < 5:
                    # Fallback: gewone tekst
                    send_discord(webhook, msg, color)
                    return

                # Pak laatste 60-80 candles voor context
                df_chart = df.tail(80) if len(df) > 80 else df

                png = render_trade_chart(df_chart, trade, exit_data=exit_data)
                if png:
                    send_discord_with_image(webhook, msg, png, color=color, filename=f"trade_{trade.get('id', 0)}.png")
                else:
                    # Fallback: tekst zonder plaatje
                    send_discord(webhook, msg, color)
            except Exception as e:
                print(f"[CHART-DISCORD] Error: {e}")
                # Fallback: stuur in elk geval de tekst
                try:
                    send_discord(webhook, msg, color)
                except Exception:
                    pass

        threading.Thread(target=_render_and_send, daemon=True).start()

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
            self.log("RISK", f"STOP Daily verlies limiet bereikt: €{self.daily_pnl:.2f} / -€{max_daily:.2f}")
            self._discord(f"STOP **Daily verlies limiet bereikt**\nVerlies: €{self.daily_pnl:.2f}\nLimiet: -€{max_daily:.2f}\nEngine stopped.", 0xff0000)
            self.stopped_by_risk = True
            self.running = False
            self._save_state()
            return True
        return False

    def _check_max_risk_eur(self, pair, entry, sl, lotsize):
        """Skip trade if computed € risk exceeds max_risk_eur limit.
        Returns: (allowed: bool, risk_eur: float)
        """
        max_risk = float(self.config.get("max_risk_eur", 0))
        if max_risk <= 0:
            return True, 0.0
        try:
            pip_size = 0.0001 if pair == "EURUSD" else 0.01
            sl_pips = abs(entry - sl) / pip_size
            # micro lot conversion: GAMAN lotsize 1 = 0.01 standard lot
            # pip value per 0.01 lot: EURUSD ~ €0.09, XAUUSD ~ €0.92 (approx)
            pip_value_per_micro = 0.10 if pair == "EURUSD" else 1.0
            risk_eur = sl_pips * pip_value_per_micro * lotsize
            if risk_eur > max_risk:
                self.log("RISK", f"SKIP {pair}: berekend risico €{risk_eur:.2f} > max €{max_risk:.2f}")
                return False, risk_eur
            return True, risk_eur
        except Exception:
            return True, 0.0

    def _check_hard_close_pnl(self, trade, current_price):
        """Force close trade if P&L exceeds hard_close_pnl_eur (positive OR negative).
        Returns: True if closed, False otherwise.
        Note: breaks RR strategy, gebruiken met voorzichtigheid.
        """
        cap = float(self.config.get("hard_close_pnl_eur", 0))
        if cap <= 0:
            return False
        try:
            pair = trade["pair"]
            direction = trade["direction"]
            entry = trade["entry"]
            lotsize = trade.get("lotsize", 1)
            pip_size = 0.0001 if pair == "EURUSD" else 0.01
            pip_value_per_micro = 0.10 if pair == "EURUSD" else 1.0
            move_pips = ((current_price - entry) / pip_size) if direction == "LONG" else ((entry - current_price) / pip_size)
            current_pnl = move_pips * pip_value_per_micro * lotsize
            if abs(current_pnl) >= cap:
                self.log("RISK", f"HARD-CLOSE #{trade['id']} {pair}: P&L €{current_pnl:.2f} >= cap €{cap:.2f}")
                return True
        except Exception:
            pass
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
            print(f"[STATE] Save error: {e}")

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
            self.log("START", f"State hersteld from {saved_at} | {len(self.open_trades)} open, {len(self.closed_trades)} closed trades")
            print(f"[STATE] Hersteld: {len(self.open_trades)} open trades, {len(self.closed_trades)} closed trades")
        except Exception as e:
            print(f"[STATE] Load error: {e}")

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
        self.log("START", f"Engine started — {pairs} {config.get('tf')} | Score>={config.get('min_score')}")
        self._discord(
            f"GO **GAMAN Engine Gestart**\n"
            f"Pair: **{pairs}** | TF: **{config.get('tf')}**\n"
            f"Min Score: {config.get('min_score')}\n"
            f"Max dayelijks verlies: €{config.get('max_daily_loss',0)} | Max trades: {config.get('max_trades',0)}",
            0x7c3aed
        )
        self._save_state()
        return True

    def stop(self):
        self.running = False
        self.paused  = False
        self.log("STOP", "Engine stopped door gebruiker")
        self._discord("RED **GAMAN Engine Stopped** door gebruiker", 0xf59e0b)
        self._save_state()

    def pause(self):
        if not self.running: return False
        self.paused = True
        self.log("PAUSE", "|| Engine paused — none new trades")
        self._discord("|| **GAMAN Engine Paused** — bestaande trades blijven open", 0xf59e0b)
        return True

    def resume(self):
        if not self.running: return False
        self.paused = False
        self.log("RESUME", "> Engine resumed — scans actief")
        self._discord("> **GAMAN Engine Resume** — scans actief", 0x22c55e)
        return True

    def _is_weekend(self):
        """
        Market closed in Brusselse tijd:
        - Vrijday 23:00 → Zonday 23:00 Brussels
        - Elke weekday (ma-do) 23:00 → 00:00 Brussels (dayelijkse gap)
        """
        now_b = now_brussels()
        wd    = now_b.weekday()  # 0=Ma, 4=Vr, 5=Za, 6=Zo
        h     = now_b.hour
        m     = now_b.minute

        # Zaterday: altijd closed
        if wd == 5: return True

        # Vrijday na 23:00
        if wd == 4 and h >= 23: return True

        # Zonday for 23:00
        if wd == 6 and h < 23: return True

        # Weekdayen (ma-do) 23:00-00:00 dayelijkse gap
        if wd in [0,1,2,3] and h == 23: return True

        return False

    def _run(self):
        while self.running:
            if self.paused:
                time.sleep(1)
                continue
            try:
                if self._is_weekend():
                    self.log("INFO", "Weekend — markt closed, wait...")
                    time.sleep(60)
                    continue

                # Reset dayelijks P&L als new day
                self._reset_daily_pnl_if_needed()

                # Check dayelijks verlies limiet
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
                self.log("ERROR", f"Engine error: {e}")

            time.sleep(20)

    def _scan(self, pair_override=None, tf_override=None):
        """Scan for new setups."""
        cfg    = self.config
        pair   = pair_override or cfg.get("pair","EURUSD")
        if pair == "BOTH": pair = "EURUSD"  # fallback
        tf     = tf_override or cfg.get("tf","1H")
        # Resolve multi-TF to single TF for live scanning
        if "+" in tf or tf == "ALL": tf = "1H"
        min_sc = int(cfg.get("min_score",2))
        use_ob = bool(cfg.get("use_ob",True))
        use_tr = bool(cfg.get("use_trend",False))
        use_eq = bool(cfg.get("use_eq",True))
        use_kz = bool(cfg.get("use_session",False))
        use_sw = bool(cfg.get("use_sweep",False))
        use_htf      = bool(cfg.get("use_htf_bias", False))
        use_smt      = bool(cfg.get("use_smt", False))
        skip_asian   = bool(cfg.get("skip_asian", False))
        req_htf_of   = bool(cfg.get("require_htf_orderflow", False))  # J3 moet positief in trade richting
        req_dol      = bool(cfg.get("require_dol", False))            # J2 moet positief in trade richting
        auto_sltp    = bool(cfg.get("auto_sltp", False))              # automatice SL/TP berekening
        rr           = float(cfg.get("rr", 2.0))                      # allen gebruikt als auto_sltp aan staat
        # Lotsize per pair
        if pair == "XAUUSD":
            lotsize = float(cfg.get("lotsize_xau", cfg.get("lotsize", 1)))
        else:
            lotsize = float(cfg.get("lotsize_eur", cfg.get("lotsize", 1)))

        # None new trade als er al één open is for dit pair
        with self.lock:
            open_pairs = [t["pair"] for t in self.open_trades]
        if pair in open_pairs:
            return

        # Cooldown: wait minstens 5 min na sluiten from een trade op dit pair+tf
        cooldown_key = f"{pair}_{tf}"
        cooldown_secs = 300  # 5 min
        last_entry = self.recent_entries.get(cooldown_key, 0)
        if time.time() - last_entry < cooldown_secs:
            return

        # Skip holidays: op US/UK/EU bank holidays is liquiditeit dun → geen entries
        if cfg.get("skip_holidays", True):
            today_b = now_brussels().date()
            holiday = is_bank_holiday(today_b)
            if holiday:
                # Log once per scan cycle om spam te vermijden
                key = f"_holiday_log_{today_b}"
                if not getattr(self, key, False):
                    self.log("INFO", f"Holiday vandaag ({holiday}) - geen new trades")
                    setattr(self, key, True)
                return

        df = fetch_candles(pair, tf)
        if df is None or len(df) < 25:
            self.log("WARN", f"Insufficient data for {pair} {tf}")
            return

        bias  = calc_bias(df, pair)
        score = bias["total_score"]

        if abs(score) < min_sc:
            return

        direction = "LONG" if score >= min_sc else ("SHORT" if score <= -min_sc else None)
        if direction is None:
            return

        # Required Judges check — ZEKER bepaalde judges moeten in juiste richting staan
        # ICT prioriteit: J3 (HTF Order Flow) en J2 (Draw on Liquidity) zijn fundamenteel
        if req_htf_of:
            j3 = bias.get("j3", 0)
            if direction == "LONG"  and j3 != 1:  return
            if direction == "SHORT" and j3 != -1: return
        if req_dol:
            j2 = bias.get("j2", 0)
            if direction == "LONG"  and j2 != 1:  return
            if direction == "SHORT" and j2 != -1: return

        # Skip Asian Session (00:00-08:00 Brusselse tijd) — laagvolume, choppy
        if skip_asian:
            now_b = now_brussels()
            if 0 <= now_b.hour < 8:
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

        # HTF Bias filter — check hogere TF richting
        if use_htf:
            htf_result = check_htf_bias(pair, tf, direction)
            if htf_result is None:
                # No data → kunnen we niet verifiëren, beter skippen for veiligheid
                return
            if not htf_result["valid"]:
                return

        # SMT Divergence filter — check DXY divergentie (1H DXY, gecached 90s)
        if use_smt:
            smt_result = detect_smt_divergence(df, direction, pair)
            if smt_result is None:
                # DXY data niet beschikbaar → skip uit forzichtigheid
                return
            if not smt_result["valid"]:
                return

        # Consequent Encroachment principe:
        # Idealiter entry op FVG midpoint (50%), maar dat is THEORETISCH — als prijs daar al
        # voorbij is moeten we ofwel skippen (te ver) ofwel op huidige prijs entren (realistisch).
        # We gebruiken huidige live prijs als entry MITS de prijs nog dicht genoeg bij de
        # FVG midpoint is. "Dicht genoeg" = binnen 60% van FVG breedte vanaf midpoint.
        fvg_mid = (fvg["top"] + fvg["bottom"]) / 2
        fvg_width = abs(fvg["top"] - fvg["bottom"])
        max_distance = fvg_width * 0.6  # toleratie: 60% van FVG breedte
        distance = abs(current_price - fvg_mid)
        if distance > max_distance:
            # Prijs is te ver van FVG midpoint gedreven — entry niet meer realistisch
            return
        entry = current_price  # echte marktprijs, niet theoretische FVG midpoint

        # Bouw filters string op
        filters_used = ["FVG"]
        if use_ob:  filters_used.append("OB")
        if use_eq:  filters_used.append("EQ")
        if use_kz:  filters_used.append("KZ")
        if use_tr:  filters_used.append("Trend")
        if use_sw:  filters_used.append("Sweep")
        if use_htf: filters_used.append("HTF")
        if use_smt: filters_used.append("SMT")
        if skip_asian: filters_used.append("!Asia")
        if req_htf_of: filters_used.append("ReqJ3")
        if req_dol:    filters_used.append("ReqJ2")
        filters_str = " + ".join(filters_used)

        # Automatice SL/TP berekening (als toggle aan staat)
        auto_sl = None
        auto_tp = None
        sl_method = None
        sl_pips_calc = 0
        if auto_sltp:
            auto_sl, auto_tp, sl_pips_calc, sl_method = compute_sl_tp(df, fvg, direction, entry, pair, rr=rr)
            if auto_sl is None:
                # Risk te groot of te klein → trade overslaan
                self.log("WARN", f"Auto SL/TP berekening mislukt for {pair} {tf} ({sl_method}) — trade overgeslagen")
                return

        trade = {
            "id":          len(self.closed_trades) + len(self.open_trades) + 1,
            "pair":        pair,
            "tf":          tf,
            "direction":   direction,
            "entry_price": round(entry, 5),
            "sl":          auto_sl,   # automatic berekend OF None (manual)
            "tp":          auto_tp,   # automatic berekend OF None (manual)
            "sl_method":   sl_method, # "swing", "recent_low", "atr_fallback", "hard_fallback"
            "sl_pips":     sl_pips_calc,
            "lotsize":     lotsize,
            "bias_score":  score,
            "filters":     filters_str,
            "opened_at":   fmt_brussels(),
            "opened_ts":   int(now_brussels().timestamp()),
            "fvg_top":     fvg["top"],
            "fvg_bottom":  fvg["bottom"],
            "pnl_eur":     0.0,
        }

        # Max risk per trade check (skip als € risico > limiet)
        if auto_sl is not None:
            allowed, risk_eur = self._check_max_risk_eur(pair, current_price, auto_sl, lotsize)
            if not allowed:
                return  # skip trade entirely

        with self.lock:
            self.open_trades.append(trade)
            self.recent_entries[f"{pair}_{tf}"] = time.time()

        # ── MT5 EXECUTION ──
        # Stuur order naar MT5 demo via EA bridge als toggle aan staat
        if self.config.get("mt5_execution_enabled", False) and auto_sl is not None and auto_tp is not None:
            try:
                bridge = get_mt5_bridge()
                if bridge.is_ea_alive(max_age_sec=30):
                    side = "BUY" if direction == "LONG" else "SELL"
                    # KRITIEK: GAMAN gebruikt "micro lot" units (1 = 0.01 standard).
                    # MT5 gebruikt standard lots. We moeten /100 doen, met minimum 0.01.
                    mt5_volume = max(0.01, round(lotsize / 100.0, 2))
                    order_id = bridge.send_order(
                        action="OPEN",
                        symbol=pair,
                        side=side,
                        volume=mt5_volume,
                        sl=auto_sl,
                        tp=auto_tp,
                    )
                    if order_id:
                        # Wacht max 10 sec op result van EA
                        result = bridge.wait_for_result(order_id, timeout_sec=10)
                        if result and result.get("success"):
                            trade["mt5_ticket"]      = result.get("ticket")
                            trade["mt5_entry_price"] = result.get("entry_price")
                            trade["mt5_volume"]      = mt5_volume
                            self.log("TRADE", f"  -> MT5 ticket {result.get('ticket')} filled @ {result.get('entry_price')} (vol={mt5_volume})")
                        else:
                            err = result.get("error", "unknown") if result else "timeout"
                            self.log("WARN", f"  -> MT5 order failed: {err}")
                            self._discord(f"⚠️ MT5 order FAILED for {pair}: {err}", color=0xf87171)
                else:
                    self.log("WARN", f"  -> MT5 EA niet beschikbaar (heartbeat te oud)")
            except Exception as e:
                self.log("ERROR", f"  -> MT5 bridge error: {e}")

        if auto_sltp:
            self.log("TRADE", f"▲ OPEN {direction} {pair} @ {entry:.5f} | SL:{auto_sl:.5f} ({sl_pips_calc}p, {sl_method}) | TP:{auto_tp:.5f} | Score:{score} | {filters_str}")
        else:
            self.log("TRADE", f"▲ OPEN {direction} {pair} @ {entry:.5f} | Score:{score} | {filters_str} | SL/TP: manual instellen")
        dir_emoji = "CHART" if direction == "LONG" else "📉"
        if auto_sltp:
            self._discord_with_trade_chart(
                f"**TRADE OPENED — {pair}**\n"
                f"Direction: **{direction}** | TF: {tf}\n"
                f"Entry: `{entry:.5f}`\n"
                f"Stop Loss: `{auto_sl:.5f}` ({sl_pips_calc} pips, via *{sl_method}*)\n"
                f"Take Profit: `{auto_tp:.5f}` (RR {rr})\n"
                f"Bias Score: **{score:+d}** | Filters: `{filters_str}`",
                trade,
                color=0x22d3ee if direction=="LONG" else 0xf59e0b
            )
        else:
            self._discord_with_trade_chart(
                f"**TRADE OPENED — {pair}**\n"
                f"Direction: **{direction}** | TF: {tf}\n"
                f"Entry: `{entry:.5f}`\n"
                f"Bias Score: **{score:+d}** | Filters: `{filters_str}`\n"
                f"! Set SL en TP via dashboard",
                trade,
                color=0x22d3ee if direction=="LONG" else 0xf59e0b
            )
        self._save_state()

    def _monitor_open_trades(self):
        """Check of SL of TP geraakt is for open trades.
        SL/TP worden allen gecheckt als ze manual zijn set via het dashboard."""
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
            sl        = trade.get("sl")    # None als nog niet set
            tp        = trade.get("tp")    # None als nog niet set
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

            # ── BIAS SHIFT WARNUWING ──
            # Sth Discord alert als bias significant verschuift tegen trade richting
            # Spam-preventie: 1× per trade, na minimum waittijd op basis from TF
            if not trade.get("bias_warned", False):
                tf = trade.get("tf", "1H")
                entry_score = trade.get("bias_score", 0)
                opened_ts   = trade.get("opened_ts", 0)
                age_seconds = int(now_brussels().timestamp()) - opened_ts
                # Min waittijd per TF: 15M→30min, 1H→1u, 4H→2u
                min_wait = {"15M": 1800, "1H": 3600, "4H": 7200}.get(tf, 3600)
                if age_seconds >= min_wait:
                    try:
                        df_now = fetch_candles(pair, tf)
                        if df_now is not None and len(df_now) >= 25:
                            current_bias = calc_bias(df_now, pair)
                            current_score = current_bias.get("total_score", 0)
                            shift = current_score - entry_score
                            # For SHORT (entry negatief): waarschuwing als score 2+ punten OMHOOG gaat
                            # For LONG (entry positief): waarschuwing als score 2+ punten OMLAAG gaat
                            shifted_against = False
                            if direction == "SHORT" and shift >= 2:
                                shifted_against = True
                            elif direction == "LONG" and shift <= -2:
                                shifted_against = True
                            if shifted_against:
                                j2_now = current_bias.get("j2", 0)
                                j3_now = current_bias.get("j3", 0)
                                hours_old = round(age_seconds / 3600, 1)
                                shift_str = f"{shift:+d}"
                                emoji = "!"
                                self._discord(
                                    f"{emoji} **Bias Shift — Trade #{trade['id']} {pair} {direction}**\n"
                                    f"In trade sinds: **{hours_old}u** | TF: {tf}\n"
                                    f"Entry bias: `{entry_score:+d}` → Huidige: `{current_score:+d}` ({shift_str} pts)\n"
                                    f"J2 (DOL): `{j2_now:+d}` | J3 (HTF Order Flow): `{j3_now:+d}`\n"
                                    f"TIP Overweeg SL trail of partial close. Market momentum verweakt.",
                                    0xfbbf24  # amber/oranje
                                )
                                self.log("WARN", f"! Bias shift trade #{trade['id']} {pair}: {entry_score:+d} → {current_score:+d}")
                                # Markeer trade zodat we niet again waarschuwen
                                with self.lock:
                                    for t in self.open_trades:
                                        if t["id"] == trade["id"]:
                                            t["bias_warned"] = True
                                self._save_state()
                    except Exception as e:
                        print(f"[BIAS-SHIFT] check error: {e}")

            # Check SL/TP — allen als manual set
            if sl is None and tp is None:
                continue

            hit = None
            exit_price = None
            # Check hard close P&L cap eerst (kan TP/SL overrulen)
            if self._check_hard_close_pnl(trade, price):
                hit = "HARDCAP"
                exit_price = price
            elif direction == "LONG":
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

                # ── MT5 CLOSE ──
                # Stuur close command naar MT5 als trade via EA werd geopend
                if self.config.get("mt5_execution_enabled", False) and trade.get("mt5_ticket"):
                    try:
                        bridge = get_mt5_bridge()
                        if bridge.is_ea_alive(max_age_sec=30):
                            order_id = bridge.send_order(
                                action="CLOSE",
                                symbol=pair,
                                ticket=trade.get("mt5_ticket"),
                            )
                            if order_id:
                                result = bridge.wait_for_result(order_id, timeout_sec=10)
                                if result and result.get("success"):
                                    self.log("TRADE", f"  -> MT5 ticket {trade.get('mt5_ticket')} closed")
                                else:
                                    err = result.get("error", "unknown") if result else "timeout"
                                    self.log("WARN", f"  -> MT5 close failed: {err}")
                        else:
                            self.log("WARN", f"  -> MT5 EA niet beschikbaar voor close")
                    except Exception as e:
                        self.log("ERROR", f"  -> MT5 close bridge error: {e}")

                icon = "OK" if hit=="TP" else "X"
                self.log("TRADE", f"{icon} CLOSE {direction} {pair} @ {exit_price:.5f} | {hit} | {final_pips:+.1f} pips | €{final_pnl:+.2f}")

                # Update dayelijks P&L
                with self.lock:
                    self.daily_pnl += final_pnl

                # Discord notificatie met chart screenshot
                result_emoji = "OK" if final_pnl >= 0 else "X"
                hit_emoji    = "TARGET" if hit=="TP" else "🛡"
                color = 0x34d399 if final_pnl >= 0 else 0xf87171
                # Bereid exit data voor chart marker
                exit_data = {
                    "exit_price": exit_price,
                    "exit_ts":    int(now_brussels().timestamp()),
                    "pnl_eur":    final_pnl,
                    "hit":        hit,
                }
                self._discord_with_trade_chart(
                    f"**TRADE CLOSED — {hit}**\n"
                    f"Pair: **{pair}** | Direction: **{direction}**\n"
                    f"Entry: `{trade['entry_price']:.5f}` -> Exit: `{exit_price:.5f}`\n"
                    f"Pips: `{final_pips:+.1f}` | P&L: **€{final_pnl:+.2f}**\n"
                    f"Daily P&L: €{self.daily_pnl:+.2f}",
                    trade,
                    color=color,
                    exit_data=exit_data
                )
                self._save_state()

engine = LiveEngine()

SYMBOLS   = {"EURUSD":"EURUSD=X","XAUUSD":"GC=F","DXY":"DX-Y.NYB"}
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
    print("[TV] websocket-client niet geinstallerd — gebruik: pip install websocket-client")

TV_INSTRUMENT_MAP = {
    "EURUSD": "OANDA:EURUSD",
    "XAUUSD": "OANDA:XAUUSD",
    "DXY":    "TVC:DXY",
}

TV_INTERVAL_MAP = {
    "15M": "15",
    "1H":  "60",
    "4H":  "240",
    "1D":  "1D",
}

# ─── BANK HOLIDAYS ─────────────────────────────────────────────────
# Major US/UK/EU holidays die EURUSD liquidity beïnvloeden.
# Op deze dagen zijn banken dicht → spreads breder, slippage groter, dunne liquiditeit.
# Update jaarlijks: voeg nieuw jaar toe in januari.
BANK_HOLIDAYS = {
    # 2024 — historisch voor backtests
    "2024-01-01": "New Year's Day",
    "2024-01-15": "MLK Day (US)",
    "2024-02-19": "Presidents Day (US)",
    "2024-03-29": "Good Friday",
    "2024-04-01": "Easter Monday",
    "2024-05-01": "Labour Day (EU)",
    "2024-05-06": "Early May Bank Holiday (UK)",
    "2024-05-27": "Memorial Day (US)",
    "2024-06-19": "Juneteenth (US)",
    "2024-07-04": "Independence Day (US)",
    "2024-08-26": "Summer Bank Holiday (UK)",
    "2024-09-02": "Labor Day (US)",
    "2024-10-14": "Columbus Day (US)",
    "2024-11-11": "Veterans Day (US)",
    "2024-11-28": "Thanksgiving (US)",
    "2024-12-25": "Christmas Day",
    "2024-12-26": "Boxing Day",
    # 2025
    "2025-01-01": "New Year's Day",
    "2025-01-20": "MLK Day (US)",
    "2025-02-17": "Presidents Day (US)",
    "2025-04-18": "Good Friday",
    "2025-04-21": "Easter Monday",
    "2025-05-01": "Labour Day (EU)",
    "2025-05-05": "Early May Bank Holiday (UK)",
    "2025-05-26": "Memorial Day (US) + Spring Bank Holiday (UK)",
    "2025-06-19": "Juneteenth (US)",
    "2025-07-04": "Independence Day (US)",
    "2025-08-25": "Summer Bank Holiday (UK)",
    "2025-09-01": "Labor Day (US)",
    "2025-10-13": "Columbus Day (US)",
    "2025-11-11": "Veterans Day (US)",
    "2025-11-27": "Thanksgiving (US)",
    "2025-12-25": "Christmas Day",
    "2025-12-26": "Boxing Day",
    # 2026
    "2026-01-01": "New Year's Day",
    "2026-01-19": "MLK Day (US)",
    "2026-02-16": "Presidents Day (US)",
    "2026-04-03": "Good Friday",
    "2026-04-06": "Easter Monday",
    "2026-05-01": "Labour Day (EU)",
    "2026-05-04": "Early May Bank Holiday (UK)",
    "2026-05-25": "Memorial Day (US) + Spring Bank Holiday (UK)",
    "2026-06-19": "Juneteenth (US)",
    "2026-07-03": "Independence Day observed (US)",
    "2026-07-04": "Independence Day (US)",
    "2026-08-31": "Summer Bank Holiday (UK)",
    "2026-09-07": "Labor Day (US)",
    "2026-10-12": "Columbus Day (US)",
    "2026-11-11": "Veterans Day (US)",
    "2026-11-26": "Thanksgiving (US)",
    "2026-12-25": "Christmas Day",
    "2026-12-28": "Boxing Day observed",
}

def is_bank_holiday(date_input):
    """Check of een datum een major bank holiday is.
    Accepteert: datetime object, date object, of 'YYYY-MM-DD' string.
    Returns: holiday name (str) of None.
    """
    try:
        if hasattr(date_input, "strftime"):
            date_str = date_input.strftime("%Y-%m-%d")
        else:
            date_str = str(date_input)[:10]
        return BANK_HOLIDAYS.get(date_str)
    except Exception:
        return None

# ─── DATA SOURCE TRACKING ───────────────────────────────────────────
# Houdt bij welke bron de last succesvolle fetch leverde, per pair+tf
DATA_SOURCE = {}  # bv. {"EURUSD_15M": {"source": "TV", "ts": 1234567890, "bars": 500}}
DATA_SOURCE_LOCK = threading.Lock()

# ─── ECONOMIC NEWS CACHE ─────────────────────────────────────────────
# Cache for Forex Factory news data (30 min cache, allen EUR + USD events)
_NEWS_CACHE = {"data": None, "ts": 0}
_NEWS_LOCK  = threading.Lock()
NEWS_CACHE_SECS = 1800  # 30 min

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
    Geeft pandas DataFrame back met brusselse DatetimeIndex.
    Fallback to yFinance als TV niet beschikbaar is.
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
        print(f"[TV] No data for {pair} {tf} — fallback to yFinance")
        df_yf = fetch_candles_yf(pair, tf)
        if df_yf is not None and not df_yf.empty:
            _set_data_source(pair, tf, "yFinance (TV failed)", len(df_yf))
        return df_yf

    df = pd.DataFrame(collected)
    # Converteer UTC timestamps to Brusselse tijd
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
    """yFinance fallback for backtesting historische data."""
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
        # Converteer to Brusselse tijd
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Europe/Brussels").tz_localize(None)
        print(f"[YF] {pair} {tf} -> {len(df)} bars")
        return df
    except Exception as e:
        print(f"[YF] Error {pair} {tf}: {e}")
        return None

def fetch_candles(pair, tf, start=None, end=None):
    """Hoofdfunctie — TV WebSocket for live, yFinance for backtesting."""
    if start and end:
        # Backtesting: gebruik yFinance for historische data
        return fetch_candles_yf(pair, tf, start, end)
    else:
        # Live: gebruik TradingView WebSocket
        return fetch_ohlcv_tv(pair, tf)

def fetch_price(pair):
    """Live price — TV WebSocket eerst, yFinance als fallback."""
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
    """
    Bias score op basis from 5 judges (range -5 tot +5):
      J1 — Premium/Discount na displacement (echte swing-gebaseerd)
      J2 — Draw on Liquidity (equal H/L + PDH/PDL + PWH/PWL)
      J3 — HTF Order Flow (last BOS richting via swing structure)
      J4 — Daily Range Expansion (verfromgt errore Power of 3)
      J5 — Killzone Momentum (recent KZ closes)

    Positieve score = bullish bias, negatieve = bearish, abs(score) = strongte.
    """
    if df is None or len(df) < 20:
        empty_j = lambda: {"score":0,"label":"—","detail":""}
        return {"total_score":0,"verdict":"GEEN DATA","verdict_color":"#888",
                "j1":0,"j1_label":"—","j1_detail":"",
                "j2":0,"j2_label":"—","j2_detail":"",
                "j3":0,"j3_label":"—","j3_detail":"",
                "j4":0,"j4_label":"—","j4_detail":"",
                "j5":0,"j5_label":"—","j5_detail":"",
                "struct_label":"—","struct_conflict":False,
                "advice":"Laad data","session":"—","in_kz":False,
                "ote_low":0,"ote_high":0,"ote_705":0,"equilibrium":0,"range_high":0,"range_low":0}

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    n      = len(df)
    cur    = float(closes[-1])
    pip_v  = PIP.get(pair, 0.0001)

    # ============================================================
    # JUDGE 1 — Premium/Discount NA DISPLACEMENT
    # ICT: zoek bullish/bearish displacement, bouw zone op die swing
    # ============================================================
    j1 = 0
    j1_label = "— None swing"
    j1_detail = ""
    # Default range for oude OTE compatibiliteit
    lb = min(20, n-1)
    rh_default = float(highs[-lb:].max())
    rl_default = float(lows[-lb:].min())
    rs_default = rh_default - rl_default
    eq_default = (rh_default + rl_default) / 2

    # Zoek meest recent displacement (last 30 bars)
    swing_lb = min(30, n-1)
    displaced_bars = []  # lijst from (bar_index, type, body_size)
    avg_body = float(sum(abs(closes[i] - opens[i]) for i in range(n-swing_lb, n)) / swing_lb)
    for i in range(n - swing_lb, n - 1):
        body = abs(closes[i] - opens[i])
        if body < avg_body * 1.5:
            continue
        wick_total = (highs[i] - lows[i])
        wick_ratio = (wick_total - body) / wick_total if wick_total > 0 else 1
        if wick_ratio > 0.4:
            continue
        disp_type = "bull" if closes[i] > opens[i] else "bear"
        displaced_bars.append((i, disp_type, body))

    if displaced_bars:
        # Meest recent displacement
        last_disp = displaced_bars[-1]
        disp_idx, disp_type, _ = last_disp
        # For bullish displacement: zoek swing-low VOOR de displacement, swing-high NA
        # For bearish: omgekeerd
        if disp_type == "bull":
            # Swing low: laagste low in bars [disp_idx-10, disp_idx]
            sw_lo_start = max(0, disp_idx - 10)
            sw_low  = float(lows[sw_lo_start:disp_idx+1].min())
            # Swing high: hoogste high NA displacement t/m nu
            sw_high = float(highs[disp_idx:n].max())
            swing_range = sw_high - sw_low
            if swing_range > 0:
                eq_swing = (sw_high + sw_low) / 2
                buf = swing_range * 0.08
                pos_in_swing = (cur - sw_low) / swing_range * 100
                if cur < eq_swing - buf:
                    j1 = 1
                    j1_label = "▲ Discount (na bull displacement)"
                elif cur > eq_swing + buf:
                    j1 = -1
                    j1_label = "▼ Premium (in bull swing)"
                else:
                    j1_label = "— EQ Zone (bull swing)"
                j1_detail = f"Swing {sw_low:.5f}→{sw_high:.5f} | Positie:{pos_in_swing:.1f}%"
        else:  # bear displacement
            sw_hi_start = max(0, disp_idx - 10)
            sw_high = float(highs[sw_hi_start:disp_idx+1].max())
            sw_low  = float(lows[disp_idx:n].min())
            swing_range = sw_high - sw_low
            if swing_range > 0:
                eq_swing = (sw_high + sw_low) / 2
                buf = swing_range * 0.08
                pos_in_swing = (cur - sw_low) / swing_range * 100
                if cur > eq_swing + buf:
                    j1 = -1
                    j1_label = "▼ Premium (na bear displacement)"
                elif cur < eq_swing - buf:
                    j1 = 1
                    j1_label = "▲ Discount (in bear swing)"
                else:
                    j1_label = "— EQ Zone (bear swing)"
                j1_detail = f"Swing {sw_high:.5f}→{sw_low:.5f} | Positie:{pos_in_swing:.1f}%"

    # ============================================================
    # JUDGE 2 — Draw on Liquidity (UITGEBREID)
    # Equal H/L + Previous Day H/L + Previous Week H/L
    # ============================================================
    eth = pip_v * 5  # equal-high/low threshold
    bsl_candidates = []  # buyside liquidity (boven prijs)
    ssl_candidates = []  # sellside liquidity (onder prijs)

    # Equal highs/lows (zoals oude judge maar bredere lookback)
    llb = min(20, n-1)
    rh_arr = highs[-llb:]; rl_arr = lows[-llb:]
    for i in range(len(rh_arr)):
        for j in range(i+1, len(rh_arr)):
            if abs(rh_arr[i] - rh_arr[j]) < eth and rh_arr[i] > cur:
                bsl_candidates.append((float(rh_arr[i]), "EqH"))
            if abs(rl_arr[i] - rl_arr[j]) < eth and rl_arr[i] < cur:
                ssl_candidates.append((float(rl_arr[i]), "EqL"))

    # Previous Day High / Low (PDH / PDL)
    try:
        if hasattr(df.index[-1], "date"):
            today = df.index[-1].date()
            prev_days_df = df[df.index.date < today]
            if len(prev_days_df) > 0:
                last_prev_day = prev_days_df.index[-1].date()
                yest_df = prev_days_df[prev_days_df.index.date == last_prev_day]
                if len(yest_df):
                    pdh = float(yest_df["high"].max())
                    pdl = float(yest_df["low"].min())
                    if pdh > cur: bsl_candidates.append((pdh, "PDH"))
                    if pdl < cur: ssl_candidates.append((pdl, "PDL"))
    except: pass

    # Previous Week High / Low (PWH / PWL) — eenvoudig: last 5 trading dayen for deze week
    try:
        if hasattr(df.index[-1], "date") and hasattr(df.index[-1], "isocalendar"):
            this_week = df.index[-1].isocalendar()[1]
            prev_week_df = df[[idx.isocalendar()[1] != this_week for idx in df.index]]
            if len(prev_week_df) > 10:
                # Pak last week vóór deze
                last_prev_week = prev_week_df.index[-1].isocalendar()[1]
                lw_df = prev_week_df[[idx.isocalendar()[1] == last_prev_week for idx in prev_week_df.index]]
                if len(lw_df):
                    pwh = float(lw_df["high"].max())
                    pwl = float(lw_df["low"].min())
                    if pwh > cur: bsl_candidates.append((pwh, "PWH"))
                    if pwl < cur: ssl_candidates.append((pwl, "PWL"))
    except: pass

    # Bepaal closedstbijzijnde liquiditeit aan beide kanten
    nearest_bsl = min(bsl_candidates, key=lambda x: x[0] - cur) if bsl_candidates else None
    nearest_ssl = max(ssl_candidates, key=lambda x: x[0]) if ssl_candidates else None
    j2 = 0
    j2_label = "— None DOL"
    j2_detail = ""
    if nearest_bsl and nearest_ssl:
        db = nearest_bsl[0] - cur
        ds = cur - nearest_ssl[0]
        if db < ds * 0.9:   # BSL duidelijk closeder
            j2 = 1; j2_label = f"▲ Draw BSL ({nearest_bsl[1]})"
            j2_detail = f"BSL@{nearest_bsl[0]:.5f} ({db/pip_v:.0f}p)"
        elif ds < db * 0.9: # SSL duidelijk closeder
            j2 = -1; j2_label = f"▼ Draw SSL ({nearest_ssl[1]})"
            j2_detail = f"SSL@{nearest_ssl[0]:.5f} ({ds/pip_v:.0f}p)"
        else:
            j2 = 0; j2_label = "— DOL Neutraal"
            j2_detail = f"BSL@{nearest_bsl[0]:.5f} ~ SSL@{nearest_ssl[0]:.5f}"
    elif nearest_bsl:
        j2 = 1; j2_label = f"▲ Draw BSL ({nearest_bsl[1]})"
        j2_detail = f"BSL@{nearest_bsl[0]:.5f}"
    elif nearest_ssl:
        j2 = -1; j2_label = f"▼ Draw SSL ({nearest_ssl[1]})"
        j2_detail = f"SSL@{nearest_ssl[0]:.5f}"

    # ============================================================
    # JUDGE 3 — HTF Order Flow / Market Structure
    # Laatste BOS richting bepaalt order flow
    # ============================================================
    # Detecteer swing highs en lows in last 50 bars
    swing_lookback = min(50, n)
    sw_h_arr = highs[-swing_lookback:]
    sw_l_arr = lows[-swing_lookback:]
    swing_highs = []  # (idx_relatief, price)
    swing_lows  = []
    for i in range(2, len(sw_h_arr) - 2):
        # Swing high: hoger dan 2 buren aan beide kanten
        if sw_h_arr[i] > sw_h_arr[i-1] and sw_h_arr[i] > sw_h_arr[i-2] and \
           sw_h_arr[i] > sw_h_arr[i+1] and sw_h_arr[i] > sw_h_arr[i+2]:
            swing_highs.append((i, float(sw_h_arr[i])))
        if sw_l_arr[i] < sw_l_arr[i-1] and sw_l_arr[i] < sw_l_arr[i-2] and \
           sw_l_arr[i] < sw_l_arr[i+1] and sw_l_arr[i] < sw_l_arr[i+2]:
            swing_lows.append((i, float(sw_l_arr[i])))

    # Zoek de meest recent BOS:
    # Bullish BOS = close > meest recent onbroken swing high
    # Bearish BOS = close < meest recent onbroken swing low
    j3 = 0
    j3_label = "— None BOS"
    j3_detail = ""
    last_bull_bos_idx = -1
    last_bear_bos_idx = -1
    closes_arr = closes[-swing_lookback:]
    for sh_idx, sh_price in swing_highs:
        # Vind eerste candle na sh_idx die boven sh_price sluit
        for k in range(sh_idx + 1, len(closes_arr)):
            if closes_arr[k] > sh_price:
                last_bull_bos_idx = max(last_bull_bos_idx, k)
                break
    for sl_idx, sl_price in swing_lows:
        for k in range(sl_idx + 1, len(closes_arr)):
            if closes_arr[k] < sl_price:
                last_bear_bos_idx = max(last_bear_bos_idx, k)
                break

    if last_bull_bos_idx > last_bear_bos_idx and last_bull_bos_idx >= 0:
        j3 = 1
        j3_label = "▲ Bullish BOS"
        j3_detail = f"BOS bar -{swing_lookback - last_bull_bos_idx}"
    elif last_bear_bos_idx > last_bull_bos_idx and last_bear_bos_idx >= 0:
        j3 = -1
        j3_label = "▼ Bearish BOS"
        j3_detail = f"BOS bar -{swing_lookback - last_bear_bos_idx}"
    else:
        j3_detail = "None recent BOS"

    # ============================================================
    # JUDGE 4 — Daily Range Expansion (verfromgt errore Power of 3)
    # Hoe ontwikkelt huidige day-candle zich?
    # ============================================================
    j4 = 0
    j4_label = "— None day-data"
    j4_detail = ""
    try:
        if hasattr(df.index[-1], "date"):
            today = df.index[-1].date()
            today_df = df[df.index.date == today]
            if len(today_df) > 0:
                day_open = float(today_df["open"].iloc[0])
                day_high = float(today_df["high"].max())
                day_low  = float(today_df["low"].min())
                day_range = day_high - day_low
                above_open = cur > day_open
                if day_range > 0:
                    pos_in_day = (cur - day_low) / day_range * 100
                    # Bullish: boven open EN bovenste 50% from day-range
                    if above_open and pos_in_day >= 50:
                        j4 = 1
                        j4_label = "▲ Bullish expansion"
                        j4_detail = f"Boven open, top {pos_in_day:.0f}% range"
                    elif (not above_open) and pos_in_day < 50:
                        j4 = -1
                        j4_label = "▼ Bearish expansion"
                        j4_detail = f"Onder open, bottom {pos_in_day:.0f}% range"
                    else:
                        j4_label = "— Mixed (reversal?)"
                        j4_detail = f"Open:{day_open:.5f} Pos:{pos_in_day:.0f}%"
                else:
                    j4_label = "— None range"
    except: pass

    # ============================================================
    # JUDGE 5 — Killzone Momentum
    # Laatste 4 KZ candles richting (London 09-12 of NY 14-17 Brussel)
    # ============================================================
    j5 = 0
    j5_label = "— None KZ data"
    j5_detail = ""
    try:
        # Filter allen bars die binnen London KZ (07-10 UTC) of NY KZ (12-15 UTC) vielen
        kz_mask = []
        for idx in df.index:
            hour_utc = idx.hour if hasattr(idx, "hour") else None
            if hour_utc is None:
                kz_mask.append(False); continue
            in_london = 7 <= hour_utc < 10
            in_ny     = 12 <= hour_utc < 15
            kz_mask.append(in_london or in_ny)
        if any(kz_mask):
            kz_df = df[kz_mask]
            if len(kz_df) >= 3:
                last_kz = kz_df.iloc[-min(4, len(kz_df)):]
                bull_closes = sum(1 for _, r in last_kz.iterrows() if r["close"] > r["open"])
                bear_closes = sum(1 for _, r in last_kz.iterrows() if r["close"] < r["open"])
                total_kz = len(last_kz)
                if bull_closes >= total_kz * 0.75:
                    j5 = 1
                    j5_label = "▲ KZ Bullish"
                    j5_detail = f"{bull_closes}/{total_kz} bull KZ candles"
                elif bear_closes >= total_kz * 0.75:
                    j5 = -1
                    j5_label = "▼ KZ Bearish"
                    j5_detail = f"{bear_closes}/{total_kz} bear KZ candles"
                else:
                    j5_label = "— KZ Mixed"
                    j5_detail = f"{bull_closes}↑ {bear_closes}↓ KZ candles"
    except: pass

    # ============================================================
    # TOTAAL SCORE (-5 tot +5) en VERDICT
    # ============================================================
    total = j1 + j2 + j3 + j4 + j5
    total = max(-5, min(5, total))

    vmap = {
        5:  ("STRONG BULLISH",  "#15803d"),
        4:  ("BULLISH +4",      "#16a34a"),
        3:  ("BULLISH +3",      "#22c55e"),
        2:  ("BULLISH +2",      "#4ade80"),
        1:  ("WEAK BULL",       "#86efac"),
        0:  ("NO TREND",      "#94a3b8"),
        -1: ("WEAK BEAR",       "#fca5a5"),
        -2: ("BEARISH -2",      "#f87171"),
        -3: ("BEARISH -3",      "#ef4444"),
        -4: ("BEARISH -4",      "#dc2626"),
        -5: ("STRONG BEARISH",  "#b91c1c"),
    }
    vtext, vcol = vmap.get(total, ("—", "#888"))

    # Conflict check (J1 vs J3): premium/discount tegenstrijdig met HTF order flow
    sc = (j3 != 0 and j1 != 0 and j3 != j1)
    sa = (j3 != 0 and j3 == j1)
    struct_label = "J1+J3 Confirmd" if sa else ("! J1↔J3 Conflict" if sc else "— None Confluentie")

    # OTE zone uit default range (for backward compatibility met UI/analyse panel)
    ote_low  = rl_default + rs_default * 0.618 if rs_default > 0 else 0
    ote_705  = rl_default + rs_default * 0.705 if rs_default > 0 else 0
    ote_high = rl_default + rs_default * 0.79  if rs_default > 0 else 0

    # Session info
    now_b = now_brussels()
    def _sess(h, m):
        t = h*60 + m
        if 540 <= t < 720:  return "UK London Killzone", True
        if 840 <= t < 1020: return "US NY Killzone", True
        if 480 <= t < 540:  return "UK London (prep)", False
        if 720 <= t < 840:  return "London Close", False
        if 120 <= t < 480:  return "WORLD Asia/Tokyo", False
        return "Off Session", False
    session, in_kz = _sess(now_b.hour, now_b.minute)

    # Advice
    if total >= 3 and in_kz:
        advice = "TARGET Long — Killzone!"
    elif total <= -3 and in_kz:
        advice = "TARGET Short — Killzone!"
    elif total >= 3:
        advice = "Long — zoek FVG/OB"
    elif total <= -3:
        advice = "Short — zoek FVG/OB"
    elif abs(total) >= 1:
        advice = "Wait — weak"
    else:
        advice = "Stay out"

    return {
        "total_score": total, "verdict": vtext, "verdict_color": vcol,
        "j1": j1, "j1_label": j1_label, "j1_detail": j1_detail,
        "j2": j2, "j2_label": j2_label, "j2_detail": j2_detail,
        "j3": j3, "j3_label": j3_label, "j3_detail": j3_detail,
        "j4": j4, "j4_label": j4_label, "j4_detail": j4_detail,
        "j5": j5, "j5_label": j5_label, "j5_detail": j5_detail,
        "struct_label": struct_label, "struct_conflict": sc, "advice": advice,
        "session": session, "in_kz": in_kz,
        "ote_low": round(ote_low, 5), "ote_high": round(ote_high, 5), "ote_705": round(ote_705, 5),
        "equilibrium": round(eq_default, 5),
        "range_high": round(rh_default, 5), "range_low": round(rl_default, 5),
    }

def detect_fvg(df, i, check_displacement=False):
    """
    Bullish FVG: low[i] > high[i-2]  — gap omhoog
    Bearish FVG: high[i] < low[i-2]  — gap omlaag

    Punt 3 — Displacement check:
    De middelste candle (i-1) moet een echte impulscandle zijn:
    - Body > 1.5x gemiddelde body from last 10 candles
    - Wick ratio < 40% from totale range (echte displacement = kleine wicks)
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
        # Bereken gemiddelde body from last 10 candles
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
    Bullish OB: last bearish candle for bullish impuls die boven de OB high sluit.
    Bearish OB: last bullish candle for bearish impuls die onder de OB low sluit.
    Requirete: de next candle doorbreekt het niveau — grootte from impuls is NIET vereist
    (te strict was het probleem).
    """
    if i < 1: return None
    o1,c1 = float(df.iloc[i-1]["open"]), float(df.iloc[i-1]["close"])
    o0,c0 = float(df.iloc[i]["open"]),   float(df.iloc[i]["close"])
    # Bullish OB: previous candle bearish, huidige bullish en sluit boven high from OB candle
    if c1 < o1 and c0 > o0 and c0 > float(df.iloc[i-1]["high"]):
        return {"type":"bull","top":max(o1,c1),"bottom":min(o1,c1)}
    # Bearish OB: previous candle bullish, huidige bearish en sluit onder low from OB candle
    if c1 > o1 and c0 < o0 and c0 < float(df.iloc[i-1]["low"]):
        return {"type":"bear","top":max(o1,c1),"bottom":min(o1,c1)}
    return None

# ─── DXY CACHE + SMT DIVERGENCE ─────────────────────────────────────
# DXY hoeft niet bij elke 20s scan opgehaald te worden — 90s cache is genoeg
# omdat we toch to 1H candles kijken (die updaten elke 60 min)
_DXY_CACHE = {"df": None, "ts": 0}
_DXY_LOCK  = threading.Lock()

def get_dxy_1h(max_age_sec=90):
    """Haal 1H DXY candles op met cache. Returns DataFrame of None bij failure."""
    with _DXY_LOCK:
        age = time.time() - _DXY_CACHE["ts"]
        if _DXY_CACHE["df"] is not None and age < max_age_sec:
            return _DXY_CACHE["df"]
    try:
        df = fetch_ohlcv_tv("DXY", "1H", bars=50, timeout=6)
        if df is not None and not df.empty and len(df) >= 10:
            with _DXY_LOCK:
                _DXY_CACHE["df"] = df
                _DXY_CACHE["ts"] = time.time()
            return df
    except Exception as e:
        print(f"[DXY] fetch error: {e}")
    return None

def detect_smt_divergence(pair_df, direction, pair="EURUSD"):
    """
    SMT (Smart Money Tool) Divergence check tussen pair en DXY.

    Logica (EURUSD en XAUUSD correleren INVERS met DXY):
    - LONG signaal: pair zet lower low, MAAR DXY zet GEEN higher high
      → smart money dumpt dollars → pair zal stijgen SMT bullish
    - SHORT signaal: pair zet higher high, MAAR DXY zet GEEN lower low
      → smart money koopt dollars → pair zal dalen SMT bearish

    Returns: dict {"valid": bool, "reason": str} of None bij data failure
    """
    dxy = get_dxy_1h()
    if dxy is None or pair_df is None or len(pair_df) < 20 or len(dxy) < 10:
        return None  # none data → trade niet blokkeren (callr beslist)

    # Vergelijk last 10 bars from beide
    lookback = 10
    pair_recent = pair_df.iloc[-lookback:]
    # We pakken de last 10 1H DXY candles → ongeveer 10 h geschiedenis
    dxy_recent  = dxy.iloc[-lookback:]

    split = lookback // 2
    pair_low1    = float(pair_recent["low"].iloc[:split].min())
    pair_low2    = float(pair_recent["low"].iloc[split:].min())
    pair_high1   = float(pair_recent["high"].iloc[:split].max())
    pair_high2   = float(pair_recent["high"].iloc[split:].max())

    dxy_low1     = float(dxy_recent["low"].iloc[:split].min())
    dxy_low2     = float(dxy_recent["low"].iloc[split:].min())
    dxy_high1    = float(dxy_recent["high"].iloc[:split].max())
    dxy_high2    = float(dxy_recent["high"].iloc[split:].max())

    pair_made_lower_low   = pair_low2  < pair_low1
    pair_made_higher_high = pair_high2 > pair_high1
    dxy_made_higher_high  = dxy_high2  > dxy_high1
    dxy_made_lower_low    = dxy_low2   < dxy_low1

    if direction == "LONG":
        # Bullish SMT: pair LL maar DXY GEEN HH (= DXY weakker dan verwait)
        if pair_made_lower_low and not dxy_made_higher_high:
            return {"valid": True, "reason": "Pair LL zonder DXY HH (bullish div)"}
        return {"valid": False, "reason": "None bullish SMT divergentie"}
    else:  # SHORT
        # Bearish SMT: pair HH maar DXY GEEN LL (= DXY stronger dan verwait)
        if pair_made_higher_high and not dxy_made_lower_low:
            return {"valid": True, "reason": "Pair HH zonder DXY LL (bearish div)"}
        return {"valid": False, "reason": "None bearish SMT divergentie"}

def check_htf_bias(pair, current_tf, direction):
    """
    HTF Bias check: bij entry op TF X, controleer of de hogere TF dezelfde richting ondersteunt.

    Folderping:
      15M entry → check 1H bias
      1H  entry → check 4H bias
      4H  entry → back to 1H (D1 niet beschikbaar in TV map)

    Returns: dict {"valid": bool, "reason": str, "htf_score": int} of None bij data failure
    """
    htf_map = {"15M": "1H", "1H": "4H", "4H": "1H"}
    htf = htf_map.get(current_tf)
    if htf is None:
        return None

    try:
        htf_df = fetch_candles(pair, htf)
        if htf_df is None or len(htf_df) < 20:
            return None
        htf_bias = calc_bias(htf_df, pair)
        score = htf_bias.get("total_score", 0)
        # LONG: HTF moet niet strong bearish zijn (>= -1 OK)
        # SHORT: HTF moet niet strong bullish zijn (<= +1 OK)
        # Bij score 0 (neutraal): trade toegelaten (none tegenstand)
        if direction == "LONG":
            if score <= -1:
                return {"valid": False, "reason": f"{htf} bias is bearish ({score:+d})", "htf_score": score}
            return {"valid": True, "reason": f"{htf} bias ({score:+d})", "htf_score": score}
        else:
            if score >= 1:
                return {"valid": False, "reason": f"{htf} bias is bullish ({score:+d})", "htf_score": score}
            return {"valid": True, "reason": f"{htf} bias ({score:+d})", "htf_score": score}
    except Exception as e:
        print(f"[HTF] check error for {pair} {current_tf}: {e}")
        return None

def detect_liquidity_sweep(df, i, lookback_swing=20, lookback_sweep=5, direction="LONG"):
    """
    Detecteert een liquidity sweep (stop-run) net vóór bar i.

    ICT definitie (boek hoofdstuk over Liquidity + Turtle Soup):
    - For LONG entry willen we een SELLSIDE sweep zien:
      een recent bar's LOW dipte ONDER een eerdere swing low, maar de CLOSE
      kwam back BOVEN dat swing-low niveau (stops eronder geveegd, prijs
      kwam back → smart money kocht).
    - For SHORT entry willen we een BUYSIDE sweep zien:
      een recent bar's HIGH ging BOVEN een eerdere swing high, maar de CLOSE
      kwam back ONDER dat niveau.

    Parameters:
      i              — huidige bar index (we kijken to bars vóór deze)
      lookback_swing — hoever back om het swing high/low te bepalen (default 20)
      lookback_sweep — binnen hoeveel recent bars moet de sweep gebeurd zijn (default 5)
      direction      — "LONG" of "SHORT"

    Returns: dict met sweep info, of None als none sweep gevonden.
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

    # Scan de "recent" bars op een sweep
    recent_start = max(0, i - lookback_sweep)
    for j in range(recent_start, i):
        bar_high  = float(df.iloc[j]["high"])
        bar_low   = float(df.iloc[j]["low"])
        bar_close = float(df.iloc[j]["close"])

        if direction == "LONG":
            # Sellside sweep: low dipte onder oude swing low, close kwam back erboven
            if bar_low < swing_low and bar_close > swing_low:
                return {
                    "swept_level": swing_low,
                    "sweep_bar":   j,
                    "type":        "sellside",
                }
        else:  # SHORT
            # Buyside sweep: high ging boven oude swing high, close kwam back eronder
            if bar_high > swing_high and bar_close < swing_high:
                return {
                    "swept_level": swing_high,
                    "sweep_bar":   j,
                    "type":        "buyside",
                }
    return None

def calc_atr(df, period=14):
    """
    Average True Range — meet de gemiddelde volatiliteit per bar.
    Returns: float (ATR waarde in prijs-eenheden)
    """
    if df is None or len(df) < period + 1:
        return 0.0
    highs = df["high"].values
    lows  = df["low"].values
    closes = df["close"].values
    trs = []
    for i in range(len(df) - period, len(df)):
        if i < 1: continue
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0

def compute_sl_tp(df, fvg, direction, entry, pair, rr=2.0):
    """
    Berekent SL en TP volgens ICT methodologie met verbeteringen.

    SL prioriteit:
      1. Swing low (LONG) / swing high (SHORT) onder/boven FVG, met pair-buffer
      2. Laagste low / hoogste high from last 10 bars vóór FVG, met buffer
      3. ATR-gebaseerde fallback (1.5× ATR) — past zich aan volatiliteit aan
      4. Safeheid: minimum buffer per pair

    TP: pure RR multiple op de risk-afstand.

    Returns: (sl, tp, sl_pips, sl_method)
      sl, tp: float prijzen
      sl_pips: int aantal pips
      sl_method: string for logging ("swing", "recent_low", "atr_fallback", "hard_fallback")
    """
    pip_v = PIP.get(pair, 0.0001)

    # Per-pair buffers en fallbacks
    if pair == "XAUUSD":
        buffer_pips     = 30   # 30 cents buffer rond swing
        recent_buffer   = 50   # buffer rond recent low/high
        hard_min_pips   = 150  # minimum SL afstand in worst case
        atr_multiplier  = 1.5
    else:  # EURUSD en andere FX
        buffer_pips     = 3    # 3 pips buffer
        recent_buffer   = 5
        hard_min_pips   = 20
        atr_multiplier  = 1.5

    n_df = len(df)
    fvg_at = fvg.get("formed_at", n_df - 1)
    lb_start = max(0, fvg_at - 20)
    lb_end   = fvg_at

    atr = calc_atr(df, period=14)
    sl_method = "hard_fallback"
    sl = None

    if direction == "LONG":
        # Methode 1: echte swing low onder FVG bottom
        for si in range(lb_end - 1, lb_start, -1):
            if si < 1 or si >= n_df - 1: continue
            l_c = float(df.iloc[si]["low"])
            l_p = float(df.iloc[si-1]["low"])
            l_n = float(df.iloc[si+1]["low"])
            if l_c < l_p and l_c < l_n and l_c < fvg["bottom"]:
                sl = l_c - pip_v * buffer_pips
                sl_method = "swing"
                break

        # Methode 2: laagste low in last 10 bars vóór FVG
        if sl is None and lb_end > lb_start + 1:
            recent_low = float(df["low"].iloc[max(0, lb_end-10):lb_end].min())
            if recent_low < entry:
                sl = recent_low - pip_v * recent_buffer
                sl_method = "recent_low"

        # Methode 3: ATR fallback — past zich aan aan volatiliteit
        if sl is None and atr > 0:
            sl = entry - atr * atr_multiplier
            sl_method = "atr_fallback"

        # Methode 4: hard fallback — vaste minimum afstand
        if sl is None:
            sl = entry - pip_v * hard_min_pips
            sl_method = "hard_fallback"

        # Safeheid: zorg dat SL minimaal hard_min_pips weg is
        min_sl = entry - pip_v * hard_min_pips
        if sl > min_sl:  # te closed
            # SL is verder dan entry maar te closed — gebruik hard minimum
            pass  # acceptabel, structureel niveau respecteren
        risk = entry - sl
    else:  # SHORT
        for si in range(lb_end - 1, lb_start, -1):
            if si < 1 or si >= n_df - 1: continue
            h_c = float(df.iloc[si]["high"])
            h_p = float(df.iloc[si-1]["high"])
            h_n = float(df.iloc[si+1]["high"])
            if h_c > h_p and h_c > h_n and h_c > fvg["top"]:
                sl = h_c + pip_v * buffer_pips
                sl_method = "swing"
                break

        if sl is None and lb_end > lb_start + 1:
            recent_high = float(df["high"].iloc[max(0, lb_end-10):lb_end].max())
            if recent_high > entry:
                sl = recent_high + pip_v * recent_buffer
                sl_method = "recent_high"

        if sl is None and atr > 0:
            sl = entry + atr * atr_multiplier
            sl_method = "atr_fallback"

        if sl is None:
            sl = entry + pip_v * hard_min_pips
            sl_method = "hard_fallback"

        risk = sl - entry

    # Validatie: risk moet positief zijn en niet absurd groot
    if risk <= 0 or risk > entry * 0.05:
        return None, None, 0, "invalid"

    # TP = pure RR multiple
    if direction == "LONG":
        tp = entry + risk * rr
    else:
        tp = entry - risk * rr

    sl_pips = round(risk / pip_v, 1)
    return round(sl, 5), round(tp, 5), sl_pips, sl_method

def precompute_bias(df, pair):
    """
    Berekent bias score (5 judges, -5 tot +5) per bar for backtest performance.
    Usaget dezelfde 5-judge logica als calc_bias, met enkele vereenvoudigingen
    for speed (none PWH/PWL want te dh per bar).
    Geeft naast totale score ook j2 en j3 per bar back zodat required-judge
    filters in de backtester kunnen werken.
    """
    n = len(df)
    scores = [0] * n
    j2_per_bar = [0] * n
    j3_per_bar = [0] * n
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    pip_v  = PIP.get(pair, 0.0001)

    # Pre-extract datums for daily/KZ judges
    try:
        dates_arr = [idx.date() if hasattr(idx, "date") else None for idx in df.index]
        hours_utc = [idx.hour if hasattr(idx, "hour") else None for idx in df.index]
    except:
        dates_arr = [None] * n
        hours_utc = [None] * n

    for i in range(20, n):
        cur = float(closes[i])

        # ── JUDGE 1: Premium/Discount NA DISPLACEMENT ──
        j1 = 0
        swing_lb = min(30, i)
        avg_body = sum(abs(closes[k] - opens[k]) for k in range(i-swing_lb, i)) / swing_lb if swing_lb > 0 else 0
        last_disp_idx = -1
        last_disp_type = None
        for k in range(i-1, max(i-swing_lb, 0), -1):
            body = abs(closes[k] - opens[k])
            if body < avg_body * 1.5:
                continue
            wick_total = (highs[k] - lows[k])
            wick_ratio = (wick_total - body) / wick_total if wick_total > 0 else 1
            if wick_ratio > 0.4:
                continue
            last_disp_idx = k
            last_disp_type = "bull" if closes[k] > opens[k] else "bear"
            break

        if last_disp_idx >= 0:
            if last_disp_type == "bull":
                sw_lo_start = max(0, last_disp_idx - 10)
                sw_low  = float(lows[sw_lo_start:last_disp_idx+1].min())
                sw_high = float(highs[last_disp_idx:i+1].max())
                swing_range = sw_high - sw_low
                if swing_range > 0:
                    eq_sw = (sw_high + sw_low) / 2
                    buf = swing_range * 0.08
                    if cur < eq_sw - buf:   j1 = 1
                    elif cur > eq_sw + buf: j1 = -1
            else:
                sw_hi_start = max(0, last_disp_idx - 10)
                sw_high = float(highs[sw_hi_start:last_disp_idx+1].max())
                sw_low  = float(lows[last_disp_idx:i+1].min())
                swing_range = sw_high - sw_low
                if swing_range > 0:
                    eq_sw = (sw_high + sw_low) / 2
                    buf = swing_range * 0.08
                    if cur > eq_sw + buf:   j1 = -1
                    elif cur < eq_sw - buf: j1 = 1

        # ── JUDGE 2: Draw on Liquidity (equal H/L + PDH/PDL) ──
        # Equal highs/lows in last 20 bars
        llb = min(20, i)
        eth = pip_v * 5
        rh_arr = highs[i-llb:i]
        rl_arr = lows[i-llb:i]
        bsl_candidates = []
        ssl_candidates = []
        for a in range(len(rh_arr)):
            for b2 in range(a+1, len(rh_arr)):
                if abs(rh_arr[a]-rh_arr[b2]) < eth and rh_arr[a] > cur:
                    bsl_candidates.append(float(rh_arr[a]))
                if abs(rl_arr[a]-rl_arr[b2]) < eth and rl_arr[a] < cur:
                    ssl_candidates.append(float(rl_arr[a]))

        # PDH / PDL (eenvoudige versie: last calendar-day vóór huidige)
        if dates_arr[i] is not None:
            today_d = dates_arr[i]
            pdh = None; pdl = None
            for k in range(i-1, max(i-200, -1), -1):
                if dates_arr[k] is None: continue
                if dates_arr[k] != today_d:
                    last_prev_day = dates_arr[k]
                    # Verzamel die day z'n high/low
                    day_highs = []; day_lows = []
                    for j2 in range(k, max(k-50, -1), -1):
                        if dates_arr[j2] != last_prev_day:
                            break
                        day_highs.append(highs[j2])
                        day_lows.append(lows[j2])
                    if day_highs and day_lows:
                        pdh = max(day_highs)
                        pdl = min(day_lows)
                    break
            if pdh is not None and pdh > cur:
                bsl_candidates.append(float(pdh))
            if pdl is not None and pdl < cur:
                ssl_candidates.append(float(pdl))

        nearest_bsl = min(bsl_candidates) if bsl_candidates else None
        nearest_ssl = max(ssl_candidates) if ssl_candidates else None
        j2 = 0
        if nearest_bsl and nearest_ssl:
            db = nearest_bsl - cur
            ds = cur - nearest_ssl
            if db < ds * 0.9:   j2 = 1
            elif ds < db * 0.9: j2 = -1
        elif nearest_bsl: j2 = 1
        elif nearest_ssl: j2 = -1

        # ── JUDGE 3: HTF Order Flow (BOS detectie last 50 bars) ──
        j3 = 0
        sw_lookback = min(50, i)
        sw_h_arr = highs[i-sw_lookback:i]
        sw_l_arr = lows[i-sw_lookback:i]
        cl_arr   = closes[i-sw_lookback:i+1]  # +1 want we willen huidige close meerekenen
        swing_highs = []
        swing_lows  = []
        for k in range(2, len(sw_h_arr) - 2):
            if sw_h_arr[k] > sw_h_arr[k-1] and sw_h_arr[k] > sw_h_arr[k-2] and \
               sw_h_arr[k] > sw_h_arr[k+1] and sw_h_arr[k] > sw_h_arr[k+2]:
                swing_highs.append((k, float(sw_h_arr[k])))
            if sw_l_arr[k] < sw_l_arr[k-1] and sw_l_arr[k] < sw_l_arr[k-2] and \
               sw_l_arr[k] < sw_l_arr[k+1] and sw_l_arr[k] < sw_l_arr[k+2]:
                swing_lows.append((k, float(sw_l_arr[k])))

        last_bull_bos = -1
        last_bear_bos = -1
        for sh_idx, sh_p in swing_highs:
            for kk in range(sh_idx+1, len(cl_arr)):
                if cl_arr[kk] > sh_p:
                    last_bull_bos = max(last_bull_bos, kk)
                    break
        for sl_idx, sl_p in swing_lows:
            for kk in range(sl_idx+1, len(cl_arr)):
                if cl_arr[kk] < sl_p:
                    last_bear_bos = max(last_bear_bos, kk)
                    break
        if last_bull_bos > last_bear_bos and last_bull_bos >= 0:
            j3 = 1
        elif last_bear_bos > last_bull_bos and last_bear_bos >= 0:
            j3 = -1

        # ── JUDGE 4: Daily Range Expansion ──
        j4 = 0
        if dates_arr[i] is not None:
            today_d = dates_arr[i]
            # Verzamel today's bars
            day_open_p = None; day_high_p = None; day_low_p = None
            for k in range(i, max(i-50, -1), -1):
                if dates_arr[k] != today_d:
                    break
                if day_open_p is None:
                    day_open_p = float(opens[k])
                if day_high_p is None or highs[k] > day_high_p:
                    day_high_p = float(highs[k])
                if day_low_p is None or lows[k] < day_low_p:
                    day_low_p = float(lows[k])
            # Open is from eerste bar from de day (kleinste k die nog today is)
            for k in range(max(i-50, 0), i+1):
                if dates_arr[k] == today_d:
                    day_open_p = float(opens[k])
                    break
            if day_open_p is not None and day_high_p is not None and day_low_p is not None:
                day_range = day_high_p - day_low_p
                if day_range > 0:
                    pos_in_day = (cur - day_low_p) / day_range
                    above_open = cur > day_open_p
                    if above_open and pos_in_day >= 0.5:
                        j4 = 1
                    elif (not above_open) and pos_in_day < 0.5:
                        j4 = -1

        # ── JUDGE 5: Killzone Momentum (last 4 KZ candles) ──
        j5 = 0
        kz_indices = []
        for k in range(i, max(i-100, -1), -1):
            h_utc = hours_utc[k]
            if h_utc is None: continue
            if 7 <= h_utc < 10 or 12 <= h_utc < 15:
                kz_indices.append(k)
                if len(kz_indices) >= 4:
                    break
        if len(kz_indices) >= 3:
            bull_c = sum(1 for k in kz_indices if closes[k] > opens[k])
            bear_c = sum(1 for k in kz_indices if closes[k] < opens[k])
            total_kz = len(kz_indices)
            if bull_c >= total_kz * 0.75:   j5 = 1
            elif bear_c >= total_kz * 0.75: j5 = -1

        total = max(-5, min(5, j1 + j2 + j3 + j4 + j5))
        scores[i] = total
        j2_per_bar[i] = j2
        j3_per_bar[i] = j3

    return scores, df["close"].values, df["high"].values, df["low"].values, j2_per_bar, j3_per_bar

def run_backtest(pair, tf, start, end, capital, lotsize, rr, use_ob, use_trend, use_eq, min_score, use_session=False, use_sweep=False, use_htf_bias=False, use_smt=False, skip_asian=False, skip_holidays=False, require_htf_orderflow=False, require_dol=False, be_trigger=0.0, spread_pips=0.0, slippage_pips=0.0, lotsize_eur=None, lotsize_xau=None, max_daily_loss=0, max_trades=0, max_risk_pct=0, max_risk_eur_filter=0, hard_close_pnl_eur=0, **kwargs):
    # Select juiste lotsize op basis from pair
    if pair == "XAUUSD" and lotsize_xau is not None:
        lotsize = lotsize_xau
    elif pair == "EURUSD" and lotsize_eur is not None:
        lotsize = lotsize_eur

    # Max risico per trade: pas lotsize aan op basis from % from kapitaal
    if max_risk_pct > 0 and capital > 0:
        typical_sl = 20 if pair == "EURUSD" else 200
        pip_v_temp = PIP.get(pair, 0.0001)
        pip_e_temp = PIP_EUR.get(pair, 0.10)
        max_risk_eur = capital * max_risk_pct / 100
        auto_lot = max_risk_eur / (typical_sl * pip_e_temp)
        lotsize = max(1, round(auto_lot))
    df = fetch_candles(pair, tf, start, end)
    if df is None or len(df) < 30:
        return {"error":"No data for deze periode.", "trades":[], "stats":{}}

    pip_v = PIP.get(pair, 0.0001)
    pip_e = PIP_EUR.get(pair, 0.10)

    # ── HTF Bias data fetching (eenmalig) ──
    htf_df = None
    htf_bias_cache = {}  # bar timestamp → htf score, cached per timestamp
    if use_htf_bias:
        htf_map = {"15M": "1H", "1H": "4H", "4H": "1H"}
        htf_tf  = htf_map.get(tf)
        if htf_tf:
            try:
                htf_df = fetch_candles(pair, htf_tf, start, end)
            except Exception as e:
                print(f"[BT-HTF] fetch error: {e}")
                htf_df = None

    # ── DXY data fetching for SMT (eenmalig) ──
    dxy_df = None
    if use_smt:
        try:
            dxy_df = fetch_candles("DXY", "1H", start, end)
        except Exception as e:
            print(f"[BT-SMT] DXY fetch error: {e}")
            dxy_df = None

    # Bereken bias scores eenmalig for all bars
    bias_scores, closes, highs, lows, j2_arr, j3_arr = precompute_bias(df, pair)

    trades    = []
    n         = len(df)
    used_fvgs = set()
    daily_pnl = {}   # date -> pnl for max_daily_loss tracking

    # Pre-scan all FVGs en OBs eenmalig — met displacement check (punt 3)
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

        # Max dayelijks verlies check
        if max_daily_loss > 0:
            bar_date = str(df.index[i])[:10]
            if daily_pnl.get(bar_date, 0) <= -max_daily_loss:
                continue
        if abs(score) < min_score: continue

        direction = "LONG" if score >= min_score else ("SHORT" if score <= -min_score else None)
        if direction is None: continue

        # Required Judges check — bepaalde judges moeten in juiste richting staan
        if require_htf_orderflow:
            j3 = j3_arr[i]
            if direction == "LONG"  and j3 != 1:  continue
            if direction == "SHORT" and j3 != -1: continue
        if require_dol:
            j2 = j2_arr[i]
            if direction == "LONG"  and j2 != 1:  continue
            if direction == "SHORT" and j2 != -1: continue

        # Equilibrium op dit moment (for EQ filter)
        lb  = min(20, i)
        rh  = float(highs[i-lb:i].max())
        rl  = float(lows[i-lb:i].min())
        eq  = (rh + rl) / 2

        # ── Trend filter ──
        # Correct: kijkt of de last 30 bars een duidelijke HH+HL of LH+LL structh hebben
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
                # Allen skippen als trend duidelijk TEGEN de richting is
                # Als trend=0 (neutraal) laten we de trade door
                if trend != 0 and trend != (1 if direction=="LONG" else -1):
                    continue

        # ── Zoek actieve FVG ──
        # Kijk back maximaal 30 bars for een niet-gemitigeerde FVG
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
            # Dit is consistent — we vergelijken de FVG met de EQ from DAT moment
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
        # Zoek de meest recent OB in dezelfde richting, gevormd VOOR of OP de FVG bar
        ob = None
        if use_ob:
            for oi in range(fvg["formed_at"], max(fvg["formed_at"]-20, 1), -1):
                if oi not in all_obs: continue
                o = all_obs[oi]
                if o["type"] == ("bull" if direction=="LONG" else "bear"):
                    ob = o; break
            if ob is None: continue

        # ── Liquidity Sweep filter ──
        # Requiret dat er vóór de FVG vorming een sweep (stop-run) was
        if use_sweep:
            sweep = detect_liquidity_sweep(
                df, fvg["formed_at"],
                lookback_swing=20, lookback_sweep=5,
                direction=direction
            )
            if sweep is None: continue

        # ── Entry: wait tot prijs backkeert IN de FVG zone ──
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

        # None dubbele trade op hetzelfde tijdstip
        ts_entry = df.index[entry_bar]
        entry_ts = int(ts_entry.timestamp()) if hasattr(ts_entry,"timestamp") else 0
        if any(abs(t["entry_ts"] - entry_ts) < 1800 for t in trades):
            continue

        # ── Skip Asian Session ──
        if skip_asian:
            entry_hour = ts_entry.hour if hasattr(ts_entry, 'hour') else 0
            # ts_entry is UTC, 00:00-08:00 Brussel = 23:00-07:00 UTC ('s winters) of 22:00-06:00 ('s zomers)
            # We pakken een vereenvoudigde versie: 22:00-07:00 UTC dekt beide
            if entry_hour >= 22 or entry_hour < 7:
                continue

        # ── Skip Bank Holidays ──
        # Op US/UK/EU holidays is liquiditeit dun → skip om realistische backtest te krijgen
        if skip_holidays:
            try:
                entry_date = ts_entry.date() if hasattr(ts_entry, 'date') else None
                if entry_date and is_bank_holiday(entry_date):
                    continue
            except Exception:
                pass

        # ── Session filter ──
        if use_session:
            entry_hour_utc = ts_entry.hour if hasattr(ts_entry, 'hour') else 0
            in_london_kz = 7 <= entry_hour_utc < 10
            in_ny_kz     = 12 <= entry_hour_utc < 15
            if not (in_london_kz or in_ny_kz):
                continue

        # ── HTF Bias filter ──
        # Zoek de HTF candle die overeenkomt met deze entry tijd, bereken bias
        if use_htf_bias:
            if htf_df is None:
                continue  # none HTF data → skip uit forzichtigheid
            # Vind de meest recent HTF bar t.o.v. entry tijd
            try:
                htf_slice = htf_df[htf_df.index <= ts_entry]
                if len(htf_slice) < 20:
                    continue
                # Bias op de "geschiedenis tot nu toe" — gebruik last 50 HTF bars
                htf_window = htf_slice.iloc[-50:]
                htf_bias_dict = calc_bias(htf_window, pair)
                htf_score = htf_bias_dict.get("total_score", 0)
                if direction == "LONG" and htf_score <= -1:
                    continue
                if direction == "SHORT" and htf_score >= 1:
                    continue
            except Exception:
                continue

        # ── SMT Divergence filter (DXY) ──
        if use_smt:
            if dxy_df is None:
                continue  # none DXY data → skip
            try:
                # Pak last 10 1H DXY bars vóór entry tijd
                dxy_slice = dxy_df[dxy_df.index <= ts_entry]
                if len(dxy_slice) < 10:
                    continue
                dxy_recent  = dxy_slice.iloc[-10:]
                # En 10 bars from pair_df vóór entry
                pair_recent = df.iloc[max(0, entry_bar-10):entry_bar]
                if len(pair_recent) < 10:
                    continue

                split = 5
                pair_low1  = float(pair_recent["low"].iloc[:split].min())
                pair_low2  = float(pair_recent["low"].iloc[split:].min())
                pair_high1 = float(pair_recent["high"].iloc[:split].max())
                pair_high2 = float(pair_recent["high"].iloc[split:].max())
                dxy_low1   = float(dxy_recent["low"].iloc[:split].min())
                dxy_low2   = float(dxy_recent["low"].iloc[split:].min())
                dxy_high1  = float(dxy_recent["high"].iloc[:split].max())
                dxy_high2  = float(dxy_recent["high"].iloc[split:].max())

                pair_made_ll = pair_low2  < pair_low1
                pair_made_hh = pair_high2 > pair_high1
                dxy_made_hh  = dxy_high2  > dxy_high1
                dxy_made_ll  = dxy_low2   < dxy_low1

                if direction == "LONG":
                    # Bullish SMT vereist: pair LL én DXY none HH
                    if not (pair_made_ll and not dxy_made_hh):
                        continue
                else:  # SHORT
                    if not (pair_made_hh and not dxy_made_ll):
                        continue
            except Exception:
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

        # ── Max risk per trade filter (€) ──
        # Skip trade als berekend risico (sl_pips * pip waarde * lots) > limiet
        if max_risk_eur_filter > 0:
            trade_risk_eur = sl_pips * pip_e * lotsize
            if trade_risk_eur > max_risk_eur_filter:
                continue

        # ── Spread + Slippage: verlaagt de effectieve P&L ──
        # Totale transactiekosten in pips (entry + exit spread + slippage)
        # Dit wordt APART from de simulatie afgetrokken from het eindresultaat
        total_cost_pips = spread_pips + slippage_pips  # pips kost per trade

        # ── Forward simulatie met break-even ──
        outcome=None; exit_price=None; ts_exit=None
        current_sl = sl
        be_moved   = False
        be_active  = be_trigger > 0

        for j in range(entry_bar+1, min(entry_bar+200, n)):
            h2 = float(df.iloc[j]["high"])
            l2 = float(df.iloc[j]["low"])

            # Break-even check: verplaats SL to entry als be_trigger bereikt
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
                # Hard close P&L cap check (overrule TP/SL als P&L > cap)
                if hard_close_pnl_eur > 0:
                    # Worst-case P&L for this bar = l2 - entry (LONG), best = h2 - entry
                    worst_pnl = (l2 - entry_price) / pip_v * pip_e * lotsize
                    best_pnl  = (h2 - entry_price) / pip_v * pip_e * lotsize
                    if best_pnl >= hard_close_pnl_eur:
                        exit_price = entry_price + hard_close_pnl_eur / (pip_e * lotsize) * pip_v
                        outcome = "win"; ts_exit = df.index[j]; break
                    if worst_pnl <= -hard_close_pnl_eur:
                        exit_price = entry_price - hard_close_pnl_eur / (pip_e * lotsize) * pip_v
                        outcome = "loss"; ts_exit = df.index[j]; break
            else:
                if h2 >= current_sl: outcome="loss" if current_sl > entry_price else "be"; exit_price=current_sl; ts_exit=df.index[j]; break
                if l2 <= tp:         outcome="win";  exit_price=tp;          ts_exit=df.index[j]; break
                # Hard close P&L cap check (SHORT)
                if hard_close_pnl_eur > 0:
                    worst_pnl = (entry_price - h2) / pip_v * pip_e * lotsize
                    best_pnl  = (entry_price - l2) / pip_v * pip_e * lotsize
                    if best_pnl >= hard_close_pnl_eur:
                        exit_price = entry_price - hard_close_pnl_eur / (pip_e * lotsize) * pip_v
                        outcome = "win"; ts_exit = df.index[j]; break
                    if worst_pnl <= -hard_close_pnl_eur:
                        exit_price = entry_price + hard_close_pnl_eur / (pip_e * lotsize) * pip_v
                        outcome = "loss"; ts_exit = df.index[j]; break

        if outcome is None: continue

        # Bereken bruto pips (wat de markt deed)
        gross_pips = round(((exit_price-raw_entry) if direction=="LONG" else (raw_entry-exit_price)) / pip_v, 1)

        # Trek transactiekosten af — dit is de ECHTE winst/verlies
        pips = round(gross_pips - total_cost_pips, 1)
        pnl  = round(pips * pip_e * lotsize, 2)

        # Herbereken outcome op basis from netto pips (na kosten)
        final_outcome = "win" if pips > 0 else ("be" if pips == 0 else "loss")
        used_fvgs.add(fvg["formed_at"])

        # Track dayelijks P&L for max_daily_loss filter
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
            data=r.read(); print(f"[LW] {len(data)}b"); return Response(data,mimetype="application/javascript")
        except Exception as e: print(f"[LW] {e}")
    return Response("window.LightweightCharts={createChart:function(el){el.innerHTML='<div style=padding:40px;color:#7dd3fc>Chart library niet beschikbaar — controleer internet</div>';var s={setData:function(){},setMarkers:function(){},applyOptions:function(){}};return{addCandlestickSeries:function(){return s;},timeScale:function(){return{fitContent:function(){},applyOptions:function(){}};},applyOptions:function(){}};},CrosshairMode:{Normal:0}};",mimetype="application/javascript")

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
    # Voeg live FVG zones toe for de chart overlay
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
    result["alignment_color"] = "#34d399" if all_bull else ("#f87171" if all_bear else "#7dd3fc")
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
                "msg": f"Verbonden — last prijs: {last_price:.5f}",
                "detail": f"Laatste candle: {last_time} Brussels",
                "price": last_price
            }
        else:
            results["tradingview"] = {"status":"error","msg":"No data ontfromgen","detail":"TV WebSocket returneert lege DataFrame"}
    except Exception as e:
        results["tradingview"] = {"status":"error","msg":f"Connectionserror","detail":str(e)}

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
            results["yfinance"] = {"status":"warn","msg":"No data","detail":"yFinance geeft lege response"}
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
            results["data_quality"] = {"status":"warn","msg":"Kan niet vergelijken","detail":"Een from de bronnen is niet beschikbaar"}
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
            results["discord"] = {"status":"warn","msg":"None webhook set","detail":"Stel een Discord webhook in de config in"}
    except:
        results["discord"] = {"status":"warn","msg":"Niet gecontroleerd","detail":""}

    # 5. Engine status
    results["engine"] = {
        "status": "ok" if engine.running else "warn",
        "msg": "Active" if engine.running else "Stopped",
        "detail": f"Scans: {engine.scan_count} | Laatste scan: {engine.last_scan or '—'} | Open trades: {len(engine.open_trades)}"
    }
    if engine.stopped_by_risk:
        results["engine"]["status"] = "error"
        results["engine"]["msg"] = "Stopped door risicobeheer"

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
        results["system"] = {"status":"warn","msg":"psutil niet beschikbaar","detail":"pip install psutil for geheugen info"}

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

    # 9. Market status
    is_wknd = engine._is_weekend()
    results["market"] = {
        "status": "ok" if not is_wknd else "warn",
        "msg": "Market Open" if not is_wknd else "Market Closed",
        "detail": fmt_brussels() + " Brussels"
    }

    return jsonify(results)

@app.route("/api/engine/test_discord", methods=["POST"])
def engine_test_discord():
    d = request.json or {}
    webhook = d.get("webhook","")
    if not webhook:
        return jsonify({"ok":False,"error":"None webhook URL"})
    try:
        send_discord(webhook,
            "**Test Successful — GAMAN Engine is Live**\n"
            "The engine is connected and ready to trade.\n"
            "CHART You will receive notifications here when trades are opened or closed.",
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
    """Close all open trades op huidige marktprijs."""
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

        engine.log("TRADE", f"X MANUALLY CLOSED {direction} {pair} @ {price:.5f} | {pips:+.1f} pips | €{pnl:+.2f}")
        closed_count += 1

    engine._save_state()
    return jsonify({"ok": True, "closed": closed_count})

@app.route("/api/engine/close_trade", methods=["POST"])
def engine_close_trade():
    """Close één specifieke trade op marktprijs."""
    d        = request.json or {}
    trade_id = int(d.get("id", 0))

    with engine.lock:
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
    if not trade:
        return jsonify({"ok": False, "error": "Trade not found"})

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
        "close_reason": "manual",
    }
    with engine.lock:
        engine.open_trades    = [t for t in engine.open_trades if t["id"] != trade_id]
        engine.closed_trades.append(closed)
        engine.daily_pnl     += pnl

    engine.log("TRADE", f"X MANUALLY CLOSED {trade['direction']} {pair} @ {price:.5f} | {pips:+.1f} pips | €{pnl:+.2f}")
    result_emoji = "OK" if pnl >= 0 else "X"
    engine._discord(
        f"{result_emoji} **TRADE MANUALLY CLOSED — {pair}**\n"
        f"Richting: **{trade['direction']}** | Entry: `{entry:.5f}` → Exit: `{price:.5f}`\n"
        f"Pips: `{pips:+.1f}` | P&L: **€{pnl:+.2f}**\n"
        f"Daily P&L: €{engine.daily_pnl:+.2f}",
        0x34d399 if pnl >= 0 else 0xf87171
    )
    engine._save_state()
    return jsonify({"ok": True, "pnl": pnl, "pips": pips})

@app.route("/api/engine/set_sl_tp", methods=["POST"])
def engine_set_sl_tp():
    """Stel SL en/of TP in for een open trade.
    Validatie tegen LIVE prijs (niet entry) zodat trailing stops mogelijk zijn:
    je kan SL boven entry zetten als prijs hoger staat (profit lock / break-even).
    """
    d        = request.json or {}
    trade_id = int(d.get("id", 0))
    new_sl   = d.get("sl")   # None = niet modify
    new_tp   = d.get("tp")   # None = niet modify

    with engine.lock:
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
        if not trade:
            return jsonify({"ok": False, "error": "Trade not found"})

        entry     = trade["entry_price"]
        direction = trade["direction"]

    # Bepaal live prijs for validatie (buiten lock zodat fetch_price niet blokkeert)
    live_price = trade.get("live_price")
    if not live_price:
        try:
            live_price = fetch_price(trade["pair"])
        except Exception:
            live_price = None
    # Fallback to entry als none live prijs beschikbaar
    if not live_price or live_price <= 0:
        live_price = entry

    with engine.lock:
        # Re-fetch trade in lock for de write
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
        if not trade:
            return jsonify({"ok": False, "error": "Trade not found"})

        # Validatie tegen LIVE prijs — moet SL/TP aan de juiste kant zijn from waar prijs NU staat
        if new_sl is not None:
            new_sl = round(float(new_sl), 5)
            if direction == "LONG" and new_sl >= live_price:
                return jsonify({"ok": False, "error": f"SL ({new_sl}) moet ONDER huidige prijs ({live_price:.5f}) liggen — anders wordt hij direct gehit"})
            if direction == "SHORT" and new_sl <= live_price:
                return jsonify({"ok": False, "error": f"SL ({new_sl}) moet BOVEN huidige prijs ({live_price:.5f}) liggen — anders wordt hij direct gehit"})
            trade["sl"] = new_sl

        if new_tp is not None:
            new_tp = round(float(new_tp), 5)
            if direction == "LONG" and new_tp <= live_price:
                return jsonify({"ok": False, "error": f"TP ({new_tp}) moet BOVEN huidige prijs ({live_price:.5f}) liggen — anders wordt hij direct gehit"})
            if direction == "SHORT" and new_tp >= live_price:
                return jsonify({"ok": False, "error": f"TP ({new_tp}) moet ONDER huidige prijs ({live_price:.5f}) liggen — anders wordt hij direct gehit"})
            trade["tp"] = new_tp

    sl_str = f"{trade['sl']:.5f}" if trade.get("sl") else "—"
    tp_str = f"{trade['tp']:.5f}" if trade.get("tp") else "—"
    engine.log("TRADE", f"CFG SL/TP set #{trade_id} {trade['pair']} | SL:{sl_str} TP:{tp_str}")
    # None Discord notificatie bij manuale wijziging — jij doet het zelf, te veel ruis anders
    engine._save_state()
    return jsonify({"ok": True, "sl": trade.get("sl"), "tp": trade.get("tp")})

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
    """Geeft all saved presets back."""
    return jsonify(_load_presets())

@app.route("/api/presets/save", methods=["POST"])
def api_presets_save():
    """Sla huidige config op als preset met een naam."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    cfg  = d.get("config", {})
    if not name: return jsonify({"ok": False, "error": "Naam is verplicht"})
    if not cfg:  return jsonify({"ok": False, "error": "None config meegegeven"})
    presets = _load_presets()
    presets[name] = {
        "config":  cfg,
        "saved_at": fmt_time_brussels(),
    }
    ok = _save_presets(presets)
    return jsonify({"ok": ok, "presets": presets})

@app.route("/api/presets/delete", methods=["POST"])
def api_presets_delete():
    """Delete een preset op naam."""
    d    = request.json or {}
    name = d.get("name", "").strip()
    presets = _load_presets()
    if name in presets:
        del presets[name]
        _save_presets(presets)
    return jsonify({"ok": True, "presets": presets})

@app.route("/api/presets/start", methods=["POST"])
def api_presets_start():
    """Start de engine direct met een saved preset (handig for mobile)."""
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
    """Geeft back welke datasource laatst is gebruikt per pair/tf.
    Overall status:
      - green (TV)         : all recent fetches via TradingView
      - oranje (yFinance)  : minstens één fallback to yFinance
      - grijs (No data)  : none recent fetches (< 5 min oud)
    """
    with DATA_SOURCE_LOCK:
        sources = dict(DATA_SOURCE)
    now = int(time.time())
    # Allen entries from last 5 min meetellen for de overall status
    recent = [s for s in sources.values() if now - s.get("ts", 0) < 300]
    if not recent:
        overall = {"status": "unknown", "label": "No data", "color": "#888"}
    elif all(s["source"] == "TV" for s in recent):
        overall = {"status": "tv", "label": "TradingView", "color": "#22c55e"}
    elif any("yFinance" in s["source"] for s in recent):
        overall = {"status": "yf", "label": "yFinance (TV down)", "color": "#f59e0b"}
    else:
        overall = {"status": "mixed", "label": "Mixed", "color": "#f59e0b"}
    return jsonify({"overall": overall, "details": sources})

# ─── ECONOMIC NEWS ENDPOINT ──────────────────────────────────────────
def _fetch_news_raw():
    """Haalt deze week's events op from Forex Factory (gratis JSON endpoint).
    Returns: list of event dicts of None bij failure.
    Cache: 30 min in _NEWS_CACHE.

    Probeert in deze volgorde:
    1. Forex Factory weekly JSON (gratis, officieel publiek)
    2. Mirror endpoint (backup)
    """
    with _NEWS_LOCK:
        age = time.time() - _NEWS_CACHE["ts"]
        if _NEWS_CACHE["data"] is not None and age < NEWS_CACHE_SECS:
            return _NEWS_CACHE["data"]

    # Lijst from URLs om te proberen (in volgorde)
    urls = [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://www.forexfactory.com/calendar.json",  # fallback if changed
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    import urllib.request, gzip, io
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                # Handle gzip decoding
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list) and len(data) > 0:
                with _NEWS_LOCK:
                    _NEWS_CACHE["data"] = data
                    _NEWS_CACHE["ts"]   = time.time()
                return data
        except Exception as e:
            print(f"[NEWS] {url} fetch error: {e}")
            continue

    return None

def _check_holiday(events):
    """Detecteert of today/tomorrow een major holiday is for US of EU.
    Forex Factory markeert holidays expliciet in event titles met 'Bank Holiday' etc.
    Returns: dict {"today": ..., "tomorrow": ...} met holiday info of None.
    """
    if not events:
        return {"today": None, "tomorrow": None}
    now_b = now_brussels()
    today_str    = now_b.strftime("%Y-%m-%d")
    tomorrow_str = (now_b + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    holiday_keywords = ["bank holiday", "holiday", "memorial day", "thanksgiving",
                        "christmas", "new year", "good friday", "easter",
                        "labor day", "independence day", "juneteenth"]

    today_h    = None
    tomorrow_h = None
    for e in events:
        title  = (e.get("title") or "").lower()
        country = (e.get("country") or "").upper()
        if country not in ("USD", "EUR"):
            continue
        date_str = e.get("date", "")[:10]  # "2026-05-26T..." → "2026-05-26"
        if any(kw in title for kw in holiday_keywords):
            if date_str == today_str and today_h is None:
                today_h = {"country": country, "title": e.get("title", "Holiday")}
            elif date_str == tomorrow_str and tomorrow_h is None:
                tomorrow_h = {"country": country, "title": e.get("title", "Holiday")}
    return {"today": today_h, "tomorrow": tomorrow_h}

@app.route("/api/news")
def api_news():
    """Geeft economische events back for today en tomorrow.
    Filtert op EUR + USD events, sorteert op tijd.
    Cache: 30 min via _NEWS_CACHE.
    """
    events = _fetch_news_raw()
    if events is None:
        return jsonify({"ok": False, "error": "News fetch mislukt", "events_today": [], "events_tomorrow": [], "holiday": {"today": None, "tomorrow": None}})

    now_b = now_brussels()
    today_str    = now_b.strftime("%Y-%m-%d")
    tomorrow_str = (now_b + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    def _process(e):
        """Zet één event om to onze gewenste structh."""
        try:
            # FF date format: "2026-05-26T08:30:00-04:00"
            ts_str = e.get("date", "")
            if not ts_str:
                return None
            # Parse en converteer to Brussel tijd
            from datetime import datetime as _dt
            # FF tijden zijn met timezone offset, parse als ISO
            dt_obj = _dt.fromisoformat(ts_str)
            # To Brussel
            try:
                from zoneinfo import ZoneInfo
                dt_brussels = dt_obj.astimezone(ZoneInfo("Europe/Brussels"))
            except Exception:
                dt_brussels = dt_obj  # fallback
            return {
                "time":    dt_brussels.strftime("%H:%M"),
                "date":    dt_brussels.strftime("%Y-%m-%d"),
                "country": (e.get("country") or "").upper(),
                "title":   e.get("title", ""),
                "impact":  (e.get("impact") or "").lower(),  # "high", "medium", "low", "holiday"
                "forecast":e.get("forecast", "") or "",
                "previous":e.get("previous", "") or "",
                "actual":  e.get("actual", "") or "",
            }
        except Exception:
            return None

    events_today    = []
    events_tomorrow = []
    for e in events:
        country = (e.get("country") or "").upper()
        if country not in ("USD", "EUR"):
            continue
        date_str = e.get("date", "")[:10]
        impact   = (e.get("impact") or "").lower()
        # Skip non-impact events (low) en speeches om ruis te verminderen
        if impact == "low":
            continue
        p = _process(e)
        if p is None:
            continue
        if p["date"] == today_str:
            events_today.append(p)
        elif p["date"] == tomorrow_str:
            events_tomorrow.append(p)

    # Sorteer op tijd
    events_today.sort(key=lambda x: x["time"])
    events_tomorrow.sort(key=lambda x: x["time"])

    holiday = _check_holiday(events)
    return jsonify({
        "ok": True,
        "events_today":    events_today,
        "events_tomorrow": events_tomorrow,
        "holiday":         holiday,
        "fetched_at":      now_b.strftime("%H:%M:%S"),
    })

@app.route("/api/mt5/status")
def api_mt5_status():
    """Status van MT5 bridge — EA heartbeat, balance, equity, open positions."""
    bridge = get_mt5_bridge()
    available = bridge.is_available()
    heartbeat = bridge.read_heartbeat() if available else None
    alive = heartbeat is not None and heartbeat.get("_age_sec", 999) < 30

    return jsonify({
        "ok":         True,
        "available":  available,
        "alive":      alive,
        "heartbeat":  heartbeat,
        "files_dir":  bridge.files_dir,
        "execution_enabled": engine.config.get("mt5_execution_enabled", False),
    })


@app.route("/api/trade/<int:trade_id>/candles")
def api_trade_candles(trade_id):
    """Fetcht candles rond een specifieke trade voor visualisatie in modal.

    Returns: candles (OHLC), entry/exit info, SL/TP, markers metadata
    """
    # Zoek trade — eerst in open, dan in closed
    with engine.lock:
        trade = next((t for t in engine.open_trades if t["id"] == trade_id), None)
        if trade is None:
            trade = next((t for t in engine.closed_trades if t["id"] == trade_id), None)
        if trade is None:
            return jsonify({"ok": False, "error": "Trade niet gevonden"}), 404
        # Maak een kopie zodat we buiten lock kunnen werken
        trade = dict(trade)

    pair = trade.get("pair", "EURUSD")
    tf   = trade.get("tf", "1H")

    try:
        df = fetch_candles(pair, tf)
        if df is None or len(df) < 5:
            return jsonify({"ok": False, "error": "Geen candle data beschikbaar"})

        opened_ts = trade.get("opened_ts", 0)
        closed_ts = trade.get("closed_ts", 0) or int(time.time())

        # Window: 50 candles vóór entry, 20 candles na exit (of nu)
        tf_seconds = {"15M": 900, "1H": 3600, "4H": 14400}.get(tf, 3600)
        window_before = 50 * tf_seconds
        window_after  = 20 * tf_seconds

        start_ts = opened_ts - window_before
        end_ts   = closed_ts + window_after

        # Filter dataframe
        try:
            import pandas as pd
            start_pd = pd.Timestamp(start_ts, unit="s", tz="UTC")
            end_pd   = pd.Timestamp(end_ts,   unit="s", tz="UTC")
            df_window = df[(df.index >= start_pd) & (df.index <= end_pd)]
            if len(df_window) < 5:
                # Te weinig data in window — neem laatste 80
                df_window = df.tail(80)
        except Exception:
            df_window = df.tail(80)

        # Bouw candles array
        candles = []
        for ts, row in df_window.iterrows():
            try:
                t_unix = int(ts.timestamp())
                candles.append({
                    "time":  t_unix,
                    "open":  round(float(row["open"]),  5),
                    "high":  round(float(row["high"]),  5),
                    "low":   round(float(row["low"]),   5),
                    "close": round(float(row["close"]), 5),
                })
            except Exception:
                continue

        return jsonify({
            "ok":          True,
            "candles":     candles,
            "trade_id":    trade_id,
            "pair":        pair,
            "tf":          tf,
            "direction":   trade.get("direction"),
            "entry_price": trade.get("entry_price"),
            "exit_price":  trade.get("exit_price"),
            "sl":          trade.get("sl"),
            "tp":          trade.get("tp"),
            "entry_ts":    opened_ts,
            "exit_ts":     trade.get("closed_ts"),
            "pnl_eur":     trade.get("pnl_eur"),
            "pips":        trade.get("pips"),
            "hit":         trade.get("hit"),
            "outcome":     trade.get("outcome"),
            "bias_score":  trade.get("bias_score"),
            "sl_pips":     trade.get("sl_pips"),
            "filters":     trade.get("filters"),
            "is_open":     trade.get("closed_ts") is None,
        })
    except Exception as e:
        print(f"[TRADE-CANDLES] Error: {e}")
        return jsonify({"ok": False, "error": str(e)})

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
        use_htf_bias = bool(d.get("use_htf_bias",False)),
        use_smt      = bool(d.get("use_smt",False)),
        skip_asian   = bool(d.get("skip_asian",False)),
        skip_holidays= bool(d.get("skip_holidays",False)),
        require_htf_orderflow = bool(d.get("require_htf_orderflow",False)),
        require_dol           = bool(d.get("require_dol",False)),
        be_trigger   = float(d.get("be_trigger",0.0)),
        spread_pips  = float(d.get("spread_pips",0.0)),
        slippage_pips= float(d.get("slippage_pips",0.0)),
        max_daily_loss= float(d.get("max_daily_loss",0)),
        max_trades    = int(d.get("max_trades",0)),
        max_risk_pct  = float(d.get("max_risk_pct",0)),
        max_risk_eur_filter = float(d.get("max_risk_eur",0)),
        hard_close_pnl_eur  = float(d.get("hard_close_pnl_eur",0)),
    )

    # Per-pair spread/slippage for BOTH
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

    # Run for elke combinatie from pair × timeframe
    all_trades  = []
    first_candles = None
    for p in pairs:
        # Usage pair-specifieke spread/slippage als BOTH
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
        return jsonify({"error":"None setups gevonden.","trades":[],"stats":{},"candles":first_candles or []})

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
    use_htf_bias = bool(d.get("use_htf_bias", False))
    use_smt      = bool(d.get("use_smt", False))
    skip_asian   = bool(d.get("skip_asian", False))
    skip_holidays= bool(d.get("skip_holidays", False))
    require_htf_orderflow = bool(d.get("require_htf_orderflow", False))
    require_dol           = bool(d.get("require_dol", False))
    pair_only             = d.get("pair_only", "BOTH")  # "EURUSD", "XAUUSD" of "BOTH"

    # Splits datum in 70% in-sample / 30% out-of-sample
    from datetime import datetime, timedelta
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end,   "%Y-%m-%d")
    total_days = (e - s).days
    split_days = int(total_days * 0.7)
    split_date = (s + timedelta(days=split_days)).strftime("%Y-%m-%d")

    # Grid from all combinaties — filter pairs op basis from pair_only
    if pair_only == "EURUSD":
        pairs = ["EURUSD"]
    elif pair_only == "XAUUSD":
        pairs = ["XAUUSD"]
    else:
        pairs = ["EURUSD", "XAUUSD"]
    timeframes = ["1H", "4H", "1H+4H"]
    rr_values  = [1.5, 2.0, 2.5]
    scores     = [2, 3]        # max 5 mogelijk, maar 4-5 levert zeer weinig trades
    ob_opts    = [True, False]
    trend_opts = [False]
    eq_opts    = [True, False]
    kz_opts    = [False]       # KZ op 4H irrelefromt
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
                                        use_htf_bias = use_htf_bias,
                                        use_smt      = use_smt,
                                        skip_asian   = skip_asian,
                                        skip_holidays= skip_holidays,
                                        require_htf_orderflow = require_htf_orderflow,
                                        require_dol           = require_dol,
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
                                        # Score: combinatie from beide periodes
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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Noto+Serif+JP:wght@700;900&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<script src="/static/lw-charts.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#04020f;
  --bg2:#080518;
  --bg3:#0d0820;
  --bg4:#110c28;
  --border:#2a2a3f;
  --border2:#3d3d5a;
  --glow:#ffffff;        /* pure neon white — for accenten en actieve states */
  --glow2:#e5e7eb;       /* off-white — hoofdkleur */
  --glow3:#f3f4f6;       /* very light gray for tekst en subtiel */
  --text:#fafafa;
  --text2:#a8a8b8;
  --text3:#6b6b7a;
  --green:#34d399;
  --green-d:#064e3b;
  --red:#f87171;
  --red-d:#450a0a;
  --amber:#fbbf24;
  --r:10px;
}
html,body{width:100%;min-height:100%;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;overflow-x:hidden}

/* Animated background — neon white gradients */
body::before{
  content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse 90% 60% at 50% -10%,rgba(255,255,255,.18) 0%,transparent 65%),
             radial-gradient(ellipse 50% 40% at 85% 90%,rgba(229,231,235,.12) 0%,transparent 55%),
             radial-gradient(ellipse 40% 30% at 5% 70%,rgba(255,255,255,.08) 0%,transparent 50%);
  pointer-events:none;z-index:0;
  animation:pulse-bg 4s ease-in-out infinite;
}
@keyframes pulse-bg{
  0%,100%{opacity:1;filter:brightness(1)}
  50%{opacity:.7;filter:brightness(1.3)}
}

/* Scanlines */
body::after{
  content:'';position:fixed;inset:0;
  background:repeating-linear-gradient(0deg,rgba(255,255,255,.02) 0px,rgba(255,255,255,.02) 1px,transparent 1px,transparent 4px);
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
  box-shadow:0 0 40px rgba(34,211,238,.15);
}
.logo{
  font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:600;
  color:var(--glow3);letter-spacing:2px;
  text-shadow:0 0 20px var(--glow),0 0 40px rgba(34,211,238,.5);
  animation:pulse-logo 4s ease-in-out infinite;
}
@keyframes pulse-logo{0%,100%{text-shadow:0 0 20px var(--glow),0 0 40px rgba(34,211,238,.5)}50%{text-shadow:0 0 30px var(--glow2),0 0 60px rgba(125,211,252,.7)}}

.tabs{display:flex;gap:2px;background:rgba(13,8,32,.8);padding:4px;border-radius:6px;border:1px solid var(--border)}
.tab-btn{padding:5px 16px;border-radius:4px;border:none;background:transparent;color:var(--text2);font-family:'Inter',sans-serif;font-size:12px;font-weight:500;cursor:pointer;transition:.2s;letter-spacing:.5px}
.tab-btn.active{background:rgba(34,211,238,.3);color:var(--glow3);box-shadow:0 0 12px rgba(34,211,238,.3);border:1px solid var(--border2)}

.topbar-right{margin-left:auto;display:flex;align-items:center;gap:12px}
#topbar-session{font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px;background:rgba(34,211,238,.15);color:var(--glow2);border:1px solid var(--border);letter-spacing:.5px}
#topbar-session.kz{background:rgba(34,211,238,.3);color:var(--glow3);border-color:var(--glow);box-shadow:0 0 12px rgba(34,211,238,.4);animation:kz-pulse 2s ease infinite}
@keyframes kz-pulse{0%,100%{box-shadow:0 0 8px rgba(34,211,238,.4)}50%{box-shadow:0 0 20px rgba(34,211,238,.8)}}
#topbar-price{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:600;color:var(--glow3);text-shadow:0 0 10px rgba(34,211,238,.6)}

.refresh-btn{display:flex;align-items:center;gap:5px;padding:5px 12px;border:1px solid var(--border2);border-radius:6px;background:rgba(34,211,238,.1);color:var(--glow2);font-size:12px;font-weight:500;cursor:pointer;font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.5px}
.refresh-btn:hover{border-color:var(--glow2);background:rgba(34,211,238,.2);box-shadow:0 0 12px rgba(34,211,238,.3)}

/* ── LAYOUT ── */
#app{display:grid;grid-template-columns:minmax(0,1fr) 480px;gap:14px;padding:14px;position:relative;z-index:2}
#left{display:flex;flex-direction:column;gap:14px;min-width:0}
#right{display:flex;flex-direction:column;gap:10px;padding-right:4px}

/* ── ANALYSIS SIDEBAR ── */
#analysis-sidebar{
  position:fixed;top:0;right:-520px;width:520px;height:100vh;
  background:rgba(8,5,24,.98);border-left:1px solid var(--border2);
  z-index:200;transition:right .35s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;
  backdrop-filter:blur(24px);box-shadow:-12px 0 60px rgba(34,211,238,.25);
}
#analysis-sidebar.open{right:0}
#sidebar-header{
  padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(34,211,238,.06);flex-shrink:0;
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
  background:rgba(34,211,238,.1);color:var(--glow2);
  font-size:11px;font-weight:500;cursor:pointer;
  font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.3px;
}
.analysis-trigger-btn:hover{border-color:var(--glow);background:rgba(34,211,238,.2);box-shadow:0 0 10px rgba(34,211,238,.3)}

/* ── CARD ── */
.card{
  position:relative;
  background:rgba(8,5,24,.7);
  border:1px solid var(--border);
  border-radius:var(--r);
  backdrop-filter:blur(12px);
  overflow:hidden;
  transition:border-color .3s, box-shadow .3s;
  box-shadow:0 0 0 1px rgba(34,211,238,.05), 0 0 20px rgba(34,211,238,.08);
}
.card::before{
  content:"我慢";
  position:absolute;
  right:8px; bottom:0px;
  font-family:'Noto Serif JP', 'Yu Mincho', serif;
  font-size:90px; font-weight:900;
  color:rgba(34,211,238,.025);
  line-height:1; letter-spacing:-4px;
  pointer-events:none;
  user-select:none;
  z-index:0;
}
.card > *{position:relative; z-index:1}
.card:hover{
  border-color:var(--border2);
  box-shadow:0 0 0 1px rgba(34,211,238,.12), 0 0 30px rgba(34,211,238,.18);
}
.card-header{
  padding:12px 16px 8px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  background:rgba(34,211,238,.04);
}
.card-title{font-size:12px;font-weight:600;color:var(--glow3);letter-spacing:1px;text-transform:uppercase;display:flex;align-items:center;gap:7px}
.card-dot{width:6px;height:6px;background:var(--glow);border-radius:50%;box-shadow:0 0 8px var(--glow);animation:dot-pulse 3s ease infinite}
@keyframes dot-pulse{0%,100%{box-shadow:0 0 6px var(--glow)}50%{box-shadow:0 0 14px var(--glow2)}}
.card-body{padding:14px 16px}

/* ── CHART TOOLBAR ── */
.chart-toolbar{display:flex;align-items:center;gap:8px;padding:9px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap;background:rgba(34,211,238,.03)}
.btn-group{display:flex;gap:1px;background:var(--border);border-radius:5px;overflow:hidden;border:1px solid var(--border)}
.toggle-btn{padding:4px 11px;border:none;background:var(--bg3);color:var(--text2);font-family:'Inter',sans-serif;font-size:11px;font-weight:500;cursor:pointer;transition:.15s;letter-spacing:.5px}
.toggle-btn.active{background:rgba(34,211,238,.35);color:var(--glow3);box-shadow:inset 0 0 10px rgba(34,211,238,.2)}
.toggle-btn:hover:not(.active){background:var(--bg4);color:var(--text)}
.chart-status{font-size:11px;color:var(--text3);margin-left:auto;font-family:'JetBrains Mono',monospace;letter-spacing:.5px}

.chart-wrap{height:260px;position:relative}
#chart{width:100%;height:100%}

/* ── WINRATE CARD ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat-item{background:rgba(34,211,238,.06);border:1px solid var(--border);border-radius:8px;padding:11px 12px;text-align:center;transition:.2s}
.stat-item:hover{border-color:var(--border2);box-shadow:0 0 16px rgba(34,211,238,.15)}
.stat-item .lbl{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px}
.stat-item .val{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:var(--glow3);line-height:1;text-shadow:0 0 10px rgba(34,211,238,.5)}
.stat-item .sub{font-size:10px;color:var(--text3);margin-top:2px}
.stat-item.green .val{color:var(--green);text-shadow:0 0 10px rgba(52,211,153,.4)}
.stat-item.red .val{color:var(--red);text-shadow:0 0 10px rgba(248,113,113,.4)}

.progress-wrap{margin-top:12px}
.progress-label{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:5px;letter-spacing:.5px}
.progress-bar{height:4px;background:rgba(34,211,238,.1);border-radius:99px;overflow:hidden;border:1px solid var(--border)}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--glow),var(--glow2));border-radius:99px;transition:width .8s ease;box-shadow:0 0 8px var(--glow)}

/* ── BIAS PANEL ── */
.bias-score-box{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:8px;margin-bottom:12px;border:1px solid var(--border);background:rgba(34,211,238,.06);transition:.3s}
.bias-big{font-family:'JetBrains Mono',monospace;font-size:34px;font-weight:700;line-height:1;min-width:46px;text-align:center}
.bias-verdict{font-size:13px;font-weight:600}
.bias-advice{font-size:11px;color:var(--text2);margin-top:2px}

.judge-row{display:grid;grid-template-columns:80px 1fr 22px;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(31,58,94,.5)}
.judge-row:last-child{border-bottom:none}
.judge-name{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.judge-bar-bg{background:rgba(34,211,238,.08);border-radius:99px;height:3px;border:none}
.judge-bar{height:100%;border-radius:99px;transition:width .4s}
.judge-val{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;text-align:right}
.judge-detail{grid-column:2/-1;font-size:9px;color:var(--text3);margin-top:1px;letter-spacing:.3px}

.pill{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;letter-spacing:.5px}
.pill-green{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}
.pill-red{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
.pill-amber{background:rgba(251,191,36,.1);color:var(--amber);border:1px solid rgba(251,191,36,.2)}
.pill-purple{background:rgba(34,211,238,.15);color:var(--glow2);border:1px solid var(--border2)}
.pill-gray{background:rgba(90,78,128,.1);color:var(--text3);border:1px solid var(--border)}

.ote-box{background:rgba(34,211,238,.06);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-top:12px}
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
.form-group input:focus,.form-group select:focus{border-color:var(--glow2);box-shadow:0 0 0 3px rgba(34,211,238,.2)}
.form-group select option{background:#0d0820;color:var(--text)}
.form-group input[type="number"]::-webkit-inner-spin-button,
.form-group input[type="number"]::-webkit-outer-spin-button{
  filter:invert(1) hue-rotate(200deg);opacity:.6;
}
.form-group input[type="date"]::-webkit-calendar-picker-indicator{
  filter:invert(1) hue-rotate(200deg);opacity:.6;cursor:pointer;
}

/* Toggle switches */
.toggle-section{margin-top:10px;padding:10px 12px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px}
.toggle-section-title{font-size:10px;font-weight:700;color:var(--glow2);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
/* Accordion — collapsible sections */
.toggle-section.collapsible > .toggle-section-title{
  cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;
  transition:opacity .2s;margin-bottom:8px;
}
.toggle-section.collapsible > .toggle-section-title:hover{opacity:.75}
.toggle-section.collapsible > .toggle-section-title::after{
  content:'▼';font-size:9px;color:var(--text3);transition:transform .25s;margin-left:8px;font-weight:400;
}
.toggle-section.collapsible.collapsed > .toggle-section-title{margin-bottom:0}
.toggle-section.collapsible.collapsed > .toggle-section-title::after{transform:rotate(-90deg)}
.toggle-section.collapsible > .accordion-body{
  max-height:3000px;overflow:hidden;transition:max-height .35s ease,opacity .25s;opacity:1;
}
.toggle-section.collapsible.collapsed > .accordion-body{max-height:0;opacity:0}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:4px 0}
.toggle-label{font-size:11px;color:var(--text2)}
.toggle-label small{display:block;font-size:9px;color:var(--text3);margin-top:1px}
.switch{position:relative;width:36px;height:20px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:rgba(31,58,94,.8);border-radius:20px;cursor:pointer;transition:.3s;border:1px solid var(--border2)}
.slider::before{content:'';position:absolute;width:14px;height:14px;left:2px;top:2px;background:var(--text3);border-radius:50%;transition:.3s}
.switch input:checked+.slider{background:rgba(34,211,238,.4);border-color:var(--glow);box-shadow:0 0 8px rgba(34,211,238,.4)}
.switch input:checked+.slider::before{transform:translateX(16px);background:var(--glow3)}

.btn-primary{width:100%;margin-top:12px;padding:10px;border:1px solid var(--glow);border-radius:6px;background:linear-gradient(135deg,rgba(34,211,238,.18) 0%,rgba(125,211,252,.12) 100%);color:#fff;font-family:'Inter',sans-serif;font-size:13px;font-weight:600;cursor:pointer;letter-spacing:1px;transition:.2s;box-shadow:0 0 18px rgba(34,211,238,.35), inset 0 0 12px rgba(125,211,252,.1);text-shadow:0 0 8px rgba(34,211,238,.6)}
.btn-primary:hover{box-shadow:0 0 28px rgba(34,211,238,.6), inset 0 0 16px rgba(125,211,252,.2);border-color:var(--glow2)}
.btn-primary:hover{background:rgba(34,211,238,.35);box-shadow:0 0 24px rgba(34,211,238,.4)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto;overflow-y:auto;max-height:220px;margin-top:12px}
.tbl-wrap::-webkit-scrollbar{width:3px;height:3px}
.tbl-wrap::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}
table{width:100%;border-collapse:collapse;font-size:11px}
thead th{padding:7px 9px;text-align:left;font-size:9px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;background:rgba(34,211,238,.06);border-bottom:1px solid var(--border);white-space:nowrap}
tbody td{padding:7px 9px;border-bottom:1px solid rgba(31,58,94,.4);color:var(--text2);font-family:'JetBrains Mono',monospace;font-size:10px;white-space:nowrap}
tbody tr:hover{background:rgba(34,211,238,.06)}
tbody tr:last-child td{border-bottom:none}
.win{color:var(--green);font-weight:700}
.loss{color:var(--red);font-weight:700}

/* ── BT STATS ── */
.bt-stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}
.bt-stat{background:rgba(34,211,238,.06);border:1px solid var(--border);border-radius:7px;padding:10px;text-align:center}
.bt-stat .l{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.bt-stat .v{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:700}

.best-worst{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.bw-box{padding:8px 10px;border-radius:7px;font-size:11px;font-family:'JetBrains Mono',monospace}
.bw-box .ttl{font-size:9px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;opacity:.7}

.csv-btn{font-size:10px;padding:4px 10px;border:1px solid var(--border2);border-radius:4px;background:transparent;color:var(--text2);cursor:pointer;font-family:'Inter',sans-serif;transition:.2s;letter-spacing:.5px}
.csv-btn:hover{border-color:var(--glow2);color:var(--glow2)}

.spinner{width:14px;height:14px;border:2px solid rgba(34,211,238,.3);border-top-color:var(--glow2);border-radius:50%;animation:spin .6s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}

::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}

/* ── CALENDAR ── */
.cal-event{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid rgba(31,58,94,.4)}
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
.tf-bias-row{display:grid;grid-template-columns:36px 1fr auto;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid rgba(31,58,94,.4)}
.tf-bias-row:last-child{border-bottom:none}
.tf-label{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--glow2)}
.tf-bars{display:flex;flex-direction:column;gap:3px}
.tf-bar-row{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--text3)}
.tf-mini-bar{height:3px;border-radius:99px;transition:width .4s}
.tf-verdict{font-size:11px;font-weight:600;text-align:right;min-width:80px;font-family:'JetBrains Mono',monospace}
.align-badge{display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;border-radius:7px;margin-bottom:12px;font-size:12px;font-weight:600;letter-spacing:.5px;border:1px solid}

/* ── MODAL ── */
.modal-overlay{position:fixed;inset:0;background:rgba(4,2,15,.85);z-index:1000;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px)}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:min(900px,95vw);max-height:90vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 0 60px rgba(34,211,238,.3)}
.modal-header{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-size:13px;font-weight:600;color:var(--glow3);letter-spacing:1px;text-transform:uppercase}
.modal-close{width:28px;height:28px;border-radius:50%;border:1px solid var(--border2);background:transparent;color:var(--text2);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;transition:.15s}
.modal-close:hover{background:rgba(248,113,113,.15);color:var(--red);border-color:var(--red)}
.modal-body{padding:16px;overflow-y:auto;flex:1}
.modal-chart{height:320px;border-radius:8px;overflow:hidden;border:1px solid var(--border)}
.modal-details{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.modal-stat{background:rgba(34,211,238,.06);border:1px solid var(--border);border-radius:7px;padding:10px;text-align:center}
.modal-stat .l{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.modal-stat .v{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700}
</style>
</head>
<body>
<div id="app-wrap">

<div id="topbar">
  <div class="logo" style="display:flex;align-items:center;gap:8px">
    我慢 <span style="font-size:11px;letter-spacing:1px;opacity:.7">GAMAN</span>
  </div>
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
<div id="weekend-banner" style="display:none;background:linear-gradient(90deg,rgba(248,113,113,.15),rgba(34,211,238,.1));border-bottom:1px solid rgba(248,113,113,.3);padding:8px 24px;text-align:center;font-size:12px;font-weight:600;color:var(--red);letter-spacing:1px;z-index:49;position:relative">
  <i data-lucide="moon" style="width:13px;height:13px;vertical-align:middle;margin-right:6px"></i>
  MARKET CLOSED — Weekend &nbsp;·&nbsp; None new trades mogelijk &nbsp;·&nbsp; Opent zonday 23:00 Brussels
</div>
<div id="market-open-banner" style="display:none;background:linear-gradient(90deg,rgba(52,211,153,.1),rgba(34,211,238,.08));border-bottom:1px solid rgba(52,211,153,.2);padding:6px 24px;text-align:center;font-size:12px;font-weight:600;color:var(--green);letter-spacing:1px;z-index:49;position:relative">
  <i data-lucide="activity" style="width:13px;height:13px;vertical-align:middle;margin-right:6px"></i>
  MARKET OPEN &nbsp;·&nbsp; Forex sessies actief
</div>

<!-- ANALYSIS SIDEBAR OVERLAY -->
<div id="sidebar-overlay" onclick="closeSidebar()"></div>

<!-- ANALYSIS SIDEBAR -->
<div id="analysis-sidebar">
  <div id="sidebar-header">
    <div class="sidebar-title"><div class="card-dot"></div>ICT Analyse Panel</div>
    <button id="sidebar-close" onclick="closeSidebar()">X</button>
  </div>
  <div id="sidebar-content">

    <!-- ICT BIAS JUDGE -->
    <div class="card">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>ICT Bias Judge</div></div>
      <div class="card-body" style="max-height:320px;overflow-y:auto">
        <div class="bias-score-box" id="bias-box">
          <div class="bias-big" id="bias-num">—</div>
          <div><div class="bias-verdict" id="bias-vt">Loading...</div><div class="bias-advice" id="bias-adv">Click Refresh</div></div>
        </div>
        <div id="judges">
          <div class="judge-row"><div class="judge-name">P/D Swing</div><div class="judge-bar-bg"><div class="judge-bar" id="j1b" style="width:50%"></div></div><div class="judge-val" id="j1v">—</div><div class="judge-detail" id="j1d"></div></div>
          <div class="judge-row"><div class="judge-name">DOL</div><div class="judge-bar-bg"><div class="judge-bar" id="j2b" style="width:50%"></div></div><div class="judge-val" id="j2v">—</div><div class="judge-detail" id="j2d"></div></div>
          <div class="judge-row"><div class="judge-name">HTF Order Flow</div><div class="judge-bar-bg"><div class="judge-bar" id="j3b" style="width:50%"></div></div><div class="judge-val" id="j3v">—</div><div class="judge-detail" id="j3d"></div></div>
          <div class="judge-row"><div class="judge-name">Daily Expansion</div><div class="judge-bar-bg"><div class="judge-bar" id="j4b" style="width:50%"></div></div><div class="judge-val" id="j4v">—</div><div class="judge-detail" id="j4d"></div></div>
          <div class="judge-row"><div class="judge-name">KZ Momentum</div><div class="judge-bar-bg"><div class="judge-bar" id="j5b" style="width:50%"></div></div><div class="judge-val" id="j5v">—</div><div class="judge-detail" id="j5d"></div></div>
          <div class="judge-row"><div class="judge-name">Confluentie</div><div id="struct-pill"></div><div></div></div>
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
        <div class="align-badge" id="align-badge"><span id="align-text">Loading...</span></div>
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
        <div id="cal-events"><div style="color:var(--text3);font-size:11px;padding:8px 0">Loading...</div></div>
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
        <div class="card-title"><div class="card-dot"></div>Open Positions</div>
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
            <thead><tr><th>#</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Live Price</th><th>Live P&L</th><th>SL instellen</th><th>TP instellen</th><th>Filters</th><th>Actie</th></tr></thead>
            <tbody id="live-open-tbody">
              <tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">No open positions</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- LIVE STATS BAR -->
    <div class="card" id="live-stats-card">
      <div class="card-body" style="padding:10px 16px">
        <div class="stat-grid">
          <div class="stat-item purple"><div class="lbl">Trades</div><div class="val" id="lt-total">0</div><div class="sub">this session</div></div>
          <div class="stat-item"><div class="lbl">Winrate</div><div class="val" id="lt-wr">—</div><div class="sub" id="lt-wl">0W / 0L</div></div>
          <div class="stat-item" id="lt-pnl-card"><div class="lbl">Total P&L</div><div class="val" id="lt-pnl">€0.00</div><div class="sub">this session</div></div>
          <div class="stat-item"><div class="lbl">Scans</div><div class="val" id="lt-scans">0</div><div class="sub" id="lt-last-scan">last: —</div></div>
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
            <thead><tr><th>#</th><th>Opened</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Pips</th><th>P&L €</th><th>Score</th></tr></thead>
            <tbody id="live-closed-tbody">
              <tr><td colspan="9" style="text-align:center;padding:16px;color:var(--text3)">Nog none closed trades</td></tr>
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
        <div style="color:var(--text3);padding:8px 0">Engine not started...</div>
      </div>
    </div>

    <!-- OPTIMIZER RESULTS -->
    <div class="card" id="opt-card" style="display:none">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Optimizer Results</div>
        <span id="opt-meta" style="font-size:10px;color:var(--text3)"></span>
      </div>
      <div class="card-body">
        <!-- Uitleg -->
        <div style="background:rgba(34,211,238,.06);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:11px;color:var(--text2);line-height:1.6">
          <strong style="color:var(--glow2)">Hoe lezen?</strong><br>
          <span style="color:var(--green)">IS</span> = In-Sample (70% from je periode, gebruikt om te leren)<br>
          <span style="color:var(--amber)">OOS</span> = Out-of-Sample (30% apart gehouden, nooit gezien)<br>
          Een goede config heeft <strong>beide</strong> green. Enkel IS green = overfitting.
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
              <th>Usage</th>
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
          <button onclick="runSystemCheck()" style="font-size:10px;padding:3px 10px;border:1px solid var(--border2);border-radius:4px;background:rgba(34,211,238,.1);color:var(--glow2);cursor:pointer;font-family:'Inter',sans-serif;display:flex;align-items:center;gap:4px">
            <i data-lucide="refresh-cw" style="width:11px;height:11px"></i> Recheck
          </button>
        </div>
        <div class="card-body">
          <div id="sys-checks" style="display:flex;flex-direction:column;gap:8px">
            <div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Click Recheck to start...</div>
          </div>
        </div>
      </div>
      <div class="card" style="margin-top:0">
        <div class="card-header"><div class="card-title"><div class="card-dot"></div>System Info</div></div>
        <div class="card-body" id="sys-info">
          <div style="color:var(--text3);font-size:12px">Loading...</div>
        </div>
      </div>
    </div>

    <!-- BT RESULTS -->
    <div class="card" id="bt-results" style="display:none">
      <div class="card-header">
        <div class="card-title"><div class="card-dot"></div>Backtest Results</div>
        <button class="csv-btn" onclick="exportCSV()">↓ CSV</button>
      </div>
      <div class="card-body">
        <div class="bt-stat-grid" id="bt-sg"></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>Date</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>SL</th><th>TP</th><th>Pips</th><th>P&L €</th><th>Score</th><th>Session</th></tr></thead>
            <tbody id="bt-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- RIGHT -->
  <div id="right">
    <!-- LIVE ENGINE CONFIG -->
    <!-- ECONOMIC NEWS CARD -->
    <div class="card" id="news-card">
      <div class="card-header" onclick="toggleNewsCard()" style="cursor:pointer">
        <div class="card-title"><div class="card-dot"></div>NEWS Economic Calendar (EUR + USD)</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="news-updated" style="font-size:10px;color:var(--text3)">—</span>
          <span class="card-chev open" id="news-chev" style="color:var(--text3);font-size:14px">▼</span>
        </div>
      </div>
      <div class="card-body" id="news-card-body" style="padding:12px 16px">
        <!-- Holiday banner -->
        <div id="news-holiday-banner" style="display:none;padding:10px 14px;margin-bottom:12px;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.4);border-radius:8px;font-size:12px;color:var(--amber)"></div>

        <!-- Today tab + Tomorrow tab -->
        <div style="display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:10px">
          <button id="news-tab-today" onclick="switchNewsTab('today')" style="flex:1;padding:8px;border:none;background:transparent;color:var(--glow2);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid var(--glow)">TODAY</button>
          <button id="news-tab-tomorrow" onclick="switchNewsTab('tomorrow')" style="flex:1;padding:8px;border:none;background:transparent;color:var(--text3);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent">TOMORROW</button>
        </div>

        <!-- Events list -->
        <div id="news-events" style="font-family:'Inter',sans-serif">
          <div style="text-align:center;padding:14px;color:var(--text3);font-size:12px">Loading...</div>
        </div>
      </div>
    </div>

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
            <input type="number" id="lt-lot-eur" value="1" min="1" step="1" title="Lotsize for EUR/USD trades">
          </div>
          <div class="form-group"><label>Lot XAUUSD (micro)</label>
            <input type="number" id="lt-lot-xau" value="1" min="1" step="1" title="Lotsize for XAU/USD trades">
          </div>
          <div class="form-group"><label>Min bias score (1–5)</label>
            <input type="number" id="lt-score" value="2" min="1" max="5">
          </div>
          <div class="form-group"><label>Spread (pips)</label>
            <input type="number" id="lt-spread" value="1.5" min="0" step="0.1" title="EURUSD ~ 1.5 pips">
          </div>
          <div class="form-group"><label>Slippage (pips)</label>
            <input type="number" id="lt-slip" value="0.5" min="0" step="0.1" title="Typisch 0.5 pip">
          </div>
        </div>

        <div class="toggle-section">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <div class="toggle-section-title" style="margin-bottom:0">Riskbeheer</div>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:9px;color:var(--text3)" id="risk-toggle-label">AAN</span>
              <label class="switch"><input type="checkbox" id="lt-risk-toggle" checked onchange="toggleRisk(this)"><span class="slider"></span></label>
            </div>
          </div>
          <div class="form-grid" style="margin-bottom:0" id="risk-fields">
            <div class="form-group">
              <label>Max dayelijks verlies (€)</label>
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
            <div class="form-group">
              <label>Max risico per trade (€)</label>
              <input type="number" id="lt-max-risk-eur" value="0" min="0" step="5" title="0 = uit | Skip trade als berekend € risico > limiet">
            </div>
            <div class="form-group">
              <label>Hard close at P&amp;L (€)</label>
              <input type="number" id="lt-hard-close-eur" value="0" min="0" step="5" title="0 = uit | Sluit trade als P&L (+/-) deze waarde bereikt — BREEKT RR strategie!">
            </div>
          </div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Discord Notificaties</div>
          <div class="form-group" style="margin-top:6px">
            <label>Webhook URL</label>
            <input type="text" id="lt-discord" value="https://discord.com/api/webhooks/1503137188156674098/oyJCR7aObCaaTeLCui2MWWdPr2V_lbNcocfIO5WuJbosJWEealdd0xuzvDJ0cPK3tRAJ" placeholder="https://discord.com/api/webhooks/..." style="font-size:10px">
          </div>
          <button onclick="testDiscord()" style="margin-top:6px;width:100%;padding:6px;border:1px solid var(--border2);border-radius:5px;background:rgba(34,211,238,.1);color:var(--glow2);font-size:11px;font-family:'Inter',sans-serif;cursor:pointer;transition:.2s" onmouseover="this.style.background='rgba(34,211,238,.2)'" onmouseout="this.style.background='rgba(34,211,238,.1)'">
            BELL Test Discord Notificatie
          </button>
          <div style="font-size:9px;color:var(--text3);margin-top:4px;line-height:1.5">
            Server Instellingen → Integraties → Webhooks → New Webhook → URL kopiëren
          </div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Strategy Filters</div>
          <div class="toggle-row">
            <div class="toggle-label">FVG<small>Altijd vereist — trigger signaal</small></div>
            <label class="switch"><input type="checkbox" checked disabled><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Order Block (OB)<small>OB in dezelfde richting als FVG</small></div>
            <label class="switch"><input type="checkbox" id="lt-ob" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Trend filter<small>Allen mee met HH/HL of LH/LL</small></div>
            <label class="switch"><input type="checkbox" id="lt-trend"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Equilibrium filter<small>FVG op goede kant from EQ</small></div>
            <label class="switch"><input type="checkbox" id="lt-eq" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Killzone filter<small>Allen London KZ en NY KZ</small></div>
            <label class="switch"><input type="checkbox" id="lt-session"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Liquidity Sweep<small>FVG na stop-run from swing high/low</small></div>
            <label class="switch"><input type="checkbox" id="lt-sweep"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">HTF Bias<small>15M→1H, 1H→4H higher TF moet richting steunen</small></div>
            <label class="switch"><input type="checkbox" id="lt-htf"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">SMT Divergence (DXY)<small>Verifieer met DXY divergentie (1H)</small></div>
            <label class="switch"><input type="checkbox" id="lt-smt"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Skip Asian Session<small>None trades tussen 00:00-08:00 Brussel</small></div>
            <label class="switch"><input type="checkbox" id="lt-asian"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Skip Bank Holidays<small>No trades op US/UK/EU bank holidays (thin liquidity)</small></div>
            <label class="switch"><input type="checkbox" id="lt-skip-holidays" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row" style="border-top:1px dashed var(--border);padding-top:8px;margin-top:4px">
            <div class="toggle-label" style="color:var(--glow2)"> Require HTF Order Flow (J3)<small>BOS richting MOET kloppen — top ICT prioriteit</small></div>
            <label class="switch"><input type="checkbox" id="lt-req-htf"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label" style="color:var(--glow2)"> Require Draw on Liquidity (J2)<small>DOL richting MOET kloppen — top ICT prioriteit</small></div>
            <label class="switch"><input type="checkbox" id="lt-req-dol"><span class="slider"></span></label>
          </div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Auto SL/TP (ICT)</div>
          <div class="toggle-row">
            <div class="toggle-label">Automatice SL/TP berekening<small>Swing-based SL + RR multiple TP. Per-pair buffers (EUR/XAU).</small></div>
            <label class="switch"><input type="checkbox" id="lt-auto-sltp"><span class="slider"></span></label>
          </div>
          <div class="form-group" style="margin-top:10px">
            <label>Risk:Reward Ratio</label>
            <input type="number" id="lt-rr" value="2" min="0.5" max="10" step="0.1" title="Allen actief als Auto SL/TP aan staat">
          </div>
          <div style="font-size:10px;color:var(--text3);line-height:1.4;padding:6px 8px;background:rgba(34,211,238,.05);border-radius:6px">
            INFO Buffers per pair: <b>EUR</b> 3p swing / 5p recent / 20p hard fallback · <b>XAU</b> 30p / 50p / 150p. ATR-fallback for onbekende condities. Je can SL/TP behindaf nog manual aanpassen.
          </div>
        </div>

        <div class="toggle-section" style="margin-top:12px">
          <div class="toggle-section-title">Discord Chart Screenshots</div>
          <div class="toggle-row">
            <div class="toggle-label">Send chart screenshot on entry/exit<small>Server-rendered candlestick chart with entry/exit markers + SL/TP lines</small></div>
            <label class="switch"><input type="checkbox" id="lt-chart-screenshots"><span class="slider"></span></label>
          </div>
          <div style="font-size:10px;color:var(--text3);line-height:1.4;padding:6px 8px;background:rgba(34,211,238,.05);border-radius:6px;margin-top:6px">
            INFO Auto-skipt rendering bij RAM &lt;100MB om VPS te beschermen. Bij failure: fallback naar tekst-only notification.
          </div>
        </div>

        <div class="toggle-section" style="margin-top:12px;border:1px solid rgba(245,158,11,.4);padding:12px;border-radius:8px;background:rgba(245,158,11,.05)">
          <div class="toggle-section-title" style="color:#f59e0b">MT5 Demo Execution</div>
          <div class="toggle-row">
            <div class="toggle-label">Send orders to MT5 broker<small>Real orders to IC Markets demo via EA bridge</small></div>
            <label class="switch"><input type="checkbox" id="lt-mt5-execution"><span class="slider"></span></label>
          </div>
          <div id="mt5-status-box" style="font-size:11px;line-height:1.5;padding:8px;background:rgba(15,23,42,.5);border-radius:6px;margin-top:8px;font-family:JetBrains Mono,monospace">
            <div style="color:var(--text3)">Loading MT5 status...</div>
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
          PAUSE
        </button>
        <div id="lt-scan-countdown" style="text-align:center;font-size:10px;color:var(--text3);margin-top:6px;min-height:14px"></div>
        <button onclick="closeAllTrades()" style="margin-top:4px;width:100%;padding:8px;border:1px solid rgba(248,113,113,.4);border-radius:6px;background:rgba(248,113,113,.06);color:var(--red);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px;display:flex;align-items:center;justify-content:center;gap:6px">
          <i data-lucide="x-circle" style="width:13px;height:13px"></i> Close All Open Trades
        </button>

        <!-- CONFIG PRESETS -->
        <div class="toggle-section" style="margin-top:12px">
          <div class="toggle-section-title">Config Presets</div>
          <div style="display:flex;gap:6px;margin-bottom:8px">
            <input id="lt-preset-name" type="text" placeholder="Preset name..." maxlength="30"
              style="flex:1;padding:5px 8px;border-radius:5px;border:1px solid var(--border2);background:rgba(8,5,24,.8);color:var(--text);font-family:'Inter',sans-serif;font-size:11px;outline:none">
            <button onclick="savePreset()"
              style="padding:5px 10px;border-radius:5px;border:1px solid var(--border2);background:rgba(34,211,238,.15);color:var(--glow2);font-family:'Inter',sans-serif;font-size:11px;cursor:pointer;white-space:nowrap">
              Save
            </button>
          </div>
          <div id="lt-presets-list" style="display:flex;flex-direction:column;gap:4px">
            <div style="font-size:10px;color:var(--text3)">Nog none presets saved.</div>
          </div>
        </div>
      </div>
    </div>

    <!-- LIVE BIAS (now in sidebar) -->
    <!-- MULTIBIAS (now in sidebar) -->
    <!-- CALENDAR (now in sidebar) -->

    <!-- BT WINRATE -->
    <div class="card" id="bt-wr-card" style="display:none">
      <div class="card-header"><div class="card-title"><div class="card-dot"></div>Results</div></div>
      <div class="card-body">
        <div class="stat-grid" style="grid-template-columns:1fr 1fr;gap:10px">
          <div class="stat-item"><div class="lbl">Winrate</div><div class="val" id="bt-wr">—</div><div class="sub" id="bt-wr-s">0 trades</div></div>
          <div class="stat-item"><div class="lbl">Total P&L</div><div class="val" id="bt-pnl" style="font-size:15px">—</div><div class="sub" id="bt-pnl-s">—</div></div>
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
          <div class="form-group"><label>Start Date</label><input type="date" id="bt-start" value="2026-04-01"></div>
          <div class="form-group"><label>End Date</label><input type="date" id="bt-end" value="2026-04-30"></div>
          <div class="form-group"><label>Kapitaal (€)</label><input type="number" id="bt-cap" value="10000" min="100"></div>
          <div class="form-group"><label>Lot EURUSD (micro)</label><input type="number" id="bt-lot-eur" value="1" min="1" step="1"></div>
          <div class="form-group"><label>Lot XAUUSD (micro)</label><input type="number" id="bt-lot-xau" value="1" min="1" step="1"></div>
          <div class="form-group"><label>Risk:Reward ratio</label><input type="number" id="bt-rr" value="2" min="0.5" step="0.5"></div>
          <div class="form-group"><label>Min bias score (1–5)</label><input type="number" id="bt-score" value="2" min="1" max="5"></div>
          <div class="form-group"><label>Break-even bij (xR)</label><input type="number" id="bt-be" value="0" min="0" max="2" step="0.1" title="0 = uit. Bv: 0.5 = SL to BE bij 50% from TP"></div>
          <!-- Spread/Slippage - enkel EURUSD of XAUUSD -->
          <div id="bt-spread-single">
            <div class="form-group"><label>Spread (pips)</label><input type="number" id="bt-spread" value="1.5" min="0" step="0.1" title="EURUSD ~ 1.5 pips, XAUUSD ~ 35 pips"></div>
            <div class="form-group"><label>Slippage (pips)</label><input type="number" id="bt-slip" value="0.5" min="0" step="0.1" title="Typisch 0.5-1 pip for marktorders"></div>
          </div>
          <!-- Spread/Slippage - BOTH -->
          <div id="bt-spread-both" style="display:none">
            <div class="form-group"><label>Spread EURUSD (pips)</label><input type="number" id="bt-spread-eur" value="1.5" min="0" step="0.1"></div>
            <div class="form-group"><label>Slippage EURUSD (pips)</label><input type="number" id="bt-slip-eur" value="0.5" min="0" step="0.1"></div>
            <div class="form-group"><label>Spread XAUUSD (pips)</label><input type="number" id="bt-spread-xau" value="35" min="0" step="1"></div>
            <div class="form-group"><label>Slippage XAUUSD (pips)</label><input type="number" id="bt-slip-xau" value="5" min="0" step="1"></div>
          </div>
          <div class="form-group"><label>Max dayelijks verlies (€)</label><input type="number" id="bt-max-loss" value="0" min="0" step="10" title="0 = uitgeschakeld"></div>
          <div class="form-group"><label>Max open trades</label><input type="number" id="bt-max-trades" value="0" min="0" step="1" title="0 = onbeperkt"></div>
          <div class="form-group"><label>Max risico per trade (%)</label><input type="number" id="bt-risk-pct" value="0" min="0" max="10" step="0.5" title="0 = vaste lotsize"></div>
          <div class="form-group"><label>Max risico per trade (€)</label><input type="number" id="bt-max-risk-eur" value="0" min="0" step="5" title="0 = uit | Skip trade als berekend € risico > limiet"></div>
          <div class="form-group"><label>Hard close at P&amp;L (€)</label><input type="number" id="bt-hard-close-eur" value="0" min="0" step="5" title="0 = uit | Sluit bij P&L ± limiet — breekt RR strategie"></div>
        </div>

        <div class="toggle-section">
          <div class="toggle-section-title">Strategy Filters</div>
          <div class="toggle-row">
            <div class="toggle-label">FVG (Fair Value Gap)<small>Altijd vereist — trigger signaal</small></div>
            <label class="switch"><input type="checkbox" checked disabled><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Order Block (OB)<small>Zoek OB in dezelfde richting als FVG</small></div>
            <label class="switch"><input type="checkbox" id="use-ob" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Trend filter (HH/HL of LH/LL)<small>Allen traden met de trend mee</small></div>
            <label class="switch"><input type="checkbox" id="use-trend" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Equilibrium filter<small>FVG moet op goede kant from EQ staan</small></div>
            <label class="switch"><input type="checkbox" id="use-eq" checked><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Killzone filter<small>Allen London KZ (09-12) en NY KZ (14-17)</small></div>
            <label class="switch"><input type="checkbox" id="use-session"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Liquidity Sweep<small>FVG na stop-run from swing high/low</small></div>
            <label class="switch"><input type="checkbox" id="use-sweep"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">HTF Bias<small>15M→1H, 1H→4H higher TF moet richting steunen</small></div>
            <label class="switch"><input type="checkbox" id="use-htf"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">SMT Divergence (DXY)<small>Verifieer met DXY divergentie (1H)</small></div>
            <label class="switch"><input type="checkbox" id="use-smt"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Skip Asian Session<small>None trades tussen 00:00-08:00 Brussel</small></div>
            <label class="switch"><input type="checkbox" id="use-asian"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label">Skip Bank Holidays<small>No trades op US/UK/EU bank holidays</small></div>
            <label class="switch"><input type="checkbox" id="use-skip-holidays"><span class="slider"></span></label>
          </div>
          <div class="toggle-row" style="border-top:1px dashed var(--border);padding-top:8px;margin-top:4px">
            <div class="toggle-label" style="color:var(--glow2)"> Require HTF Order Flow (J3)<small>BOS richting MOET kloppen</small></div>
            <label class="switch"><input type="checkbox" id="use-req-htf"><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-label" style="color:var(--glow2)"> Require Draw on Liquidity (J2)<small>DOL richting MOET kloppen</small></div>
            <label class="switch"><input type="checkbox" id="use-req-dol"><span class="slider"></span></label>
          </div>
        </div>

        <div id="tf-note" style="display:none;margin-top:8px;padding:8px 10px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:6px;font-size:10px;color:var(--amber)">
          ! 15M data is beperkt tot de last 60 dayen door yFinance. For oudere periodes wordt automatic 1H gebruikt.
        </div>

        <button class="btn-primary" onclick="runBacktest()" id="bt-run" style="display:flex;align-items:center;justify-content:center;gap:6px">
          <i data-lucide="play" style="width:14px;height:14px"></i> Run Backtest
        </button>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
          <button onclick="runOptimizer('EURUSD')" id="bt-opt-eur"
            style="padding:10px;border:1px solid var(--amber);border-radius:6px;background:rgba(251,191,36,.08);color:var(--amber);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px;display:flex;align-items:center;justify-content:center;gap:6px">
            <i data-lucide="zap" style="width:13px;height:13px"></i> Optimize EURUSD
          </button>
          <button onclick="runOptimizer('XAUUSD')" id="bt-opt-xau"
            style="padding:10px;border:1px solid var(--amber);border-radius:6px;background:rgba(251,191,36,.08);color:var(--amber);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:.2s;letter-spacing:.5px;display:flex;align-items:center;justify-content:center;gap:6px">
            <i data-lucide="zap" style="width:13px;height:13px"></i> Optimize XAUUSD
          </button>
        </div>
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
      <button class="modal-close" onclick="closeModal()">X</button>
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
    grid:{vertLines:{color:"rgba(31,58,94,.3)"},horzLines:{color:"rgba(31,58,94,.3)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#2d1f5e",timeVisible:true,secondsVisible:false,rightOffset:12,barSpacing:8},
    rightPriceScale:{borderColor:"#1f3a5e",minimumWidth:80},
    handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true},
    handleScale:{mouseWheel:true,pinch:true},
  });
  new ResizeObserver(()=>{if(S.chart)S.chart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);
  S.series=S.chart.addCandlestickSeries({
    upColor:"#22d3ee",downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#7dd3fc",borderDownColor:"#3d2b7a",
    wickUpColor:"#7dd3fc",wickDownColor:"#3d2b7a",
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
  document.getElementById("chart-status").textContent="Loading...";
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
      document.getElementById("chart-status").textContent="No data — probeer andere TF";
    }
  }catch(e){document.getElementById("chart-status").textContent="Connectionserror"}
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
  // Delete bestaande FVG series
  if(S.fvgSeries){
    S.fvgSeries.forEach(s=>{ try{ S.chart.removeSeries(s); }catch(e){} });
  }
  S.fvgSeries = [];
  if(!fvgs || !fvgs.length) return;
  fvgs.forEach(fvg=>{
    try{
      // Usage een area series als visuele zone
      const s = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(34,211,238,0)" : "rgba(248,113,113,0)",
        lineWidth: 0,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      // Teken twee horizontale lijnen for de FVG zone
      const topLine = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(34,211,238,0.6)" : "rgba(248,113,113,0.6)",
        lineWidth: 1,
        lineStyle: 2, // dashed
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      const botLine = S.chart.addLineSeries({
        color: fvg.type==="bull" ? "rgba(34,211,238,0.6)" : "rgba(248,113,113,0.6)",
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
  box.style.background=s>=2?"rgba(52,211,153,.05)":s<=-2?"rgba(248,113,113,.05)":"rgba(34,211,238,.06)";

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
  setJ("j4",b.j4,b.j4_detail);
  setJ("j5",b.j5,b.j5_detail);

  const sp=document.getElementById("struct-pill");
  sp.innerHTML=`<span class="pill ${b.struct_conflict?"pill-amber":b.struct_label.includes("OK")?"pill-green":"pill-gray"}">${b.struct_label}</span>`;

  document.getElementById("ote-l").textContent=b.ote_low;
  document.getElementById("ote-m").textContent=b.ote_705;
  document.getElementById("ote-h").textContent=b.ote_high;
  document.getElementById("ote-eq").textContent=b.equilibrium;

  // Session topbar
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
  // Score nu in range -5..+5 → mappen to 0..100%
  const pct=Math.round((s+5)/10*100);
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
  ri.textContent="refresh";
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
    if(wd === 6) isWeekend = true;                               // Zaterday
    if(wd === 5 && h >= 23) isWeekend = true;                   // Vrijday na 23:00
    if(wd === 0 && h < 23)  isWeekend = true;                   // Zonday for 23:00
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
    alert("Test notificatie verzonden! Controleer je Discord.");
  } else {
    alert("X Error: " + (d.error||"Onbekende error"));
  }
}

async function closeAllTrades(){
  if(!confirm("Weet je zeker dat je all open trades wil sluiten op de huidige marktprijs?")) return;
  const r = await fetch("/api/engine/close_all",{method:"POST"});
  const d = await r.json();
  if(d.ok){
    alert(`${d.closed} trade(s) closed.`);
  }
}

async function runSystemCheck(){
  const container = document.getElementById("sys-checks");
  const info      = document.getElementById("sys-info");
  if(!container) return;
  container.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Checks execute...</div>';

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
      market:       "Market Status",
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
    container.innerHTML = `<div style="color:var(--red);font-size:12px;text-align:center;padding:20px">Error bij fetching system info: ${e.message}</div>`;
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

async function runOptimizer(pairChoice){
  // pairChoice = "EURUSD", "XAUUSD", of undefined (= BOTH, backward compat)
  if(!pairChoice) pairChoice = "BOTH";
  const btnId = pairChoice === "EURUSD" ? "bt-opt-eur" : (pairChoice === "XAUUSD" ? "bt-opt-xau" : "bt-opt-eur");
  const btn   = document.getElementById(btnId);
  const otherBtn = document.getElementById(pairChoice === "EURUSD" ? "bt-opt-xau" : "bt-opt-eur");
  const origText = btn ? btn.innerHTML : "";
  if(btn){ btn.disabled = true; btn.innerHTML = "refresh Bezig... (1-3 min)"; }
  if(otherBtn) otherBtn.disabled = true;

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
    use_htf_bias:  document.getElementById("use-htf").checked,
    use_smt:       document.getElementById("use-smt").checked,
    skip_asian:    document.getElementById("use-asian").checked,
    skip_holidays: document.getElementById("use-skip-holidays").checked,
    require_htf_orderflow: document.getElementById("use-req-htf").checked,
    require_dol:           document.getElementById("use-req-dol").checked,
    pair_only:     pairChoice,
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
    alert("Optimizer error: "+e.message);
  }
  if(btn){ btn.disabled = false; btn.innerHTML = origText; }
  if(otherBtn) otherBtn.disabled = false;
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
      <td>${r.ob?"OK":"—"}</td>
      <td>${r.eq?"OK":"—"}</td>
      <td>${r.kz?"OK":"—"}</td>
      <td>${r.be>0?r.be:"—"}</td>
      <td style="color:var(--green)">${r.is_trades}</td>
      <td style="color:${r.is_wr>=55?"var(--green)":"var(--red)"};font-weight:600">${r.is_wr}%</td>
      <td style="color:${r.is_pnl>0?"var(--green)":"var(--red)"};font-weight:600">€${r.is_pnl}</td>
      <td style="color:var(--amber)">${r.oos_trades}</td>
      <td style="color:${r.oos_wr>=50?"var(--green)":"var(--red)"};font-weight:700">${r.oos_wr}%</td>
      <td style="color:${r.oos_pnl>0?"var(--green)":"var(--red)"};font-weight:700">€${r.oos_pnl}</td>
      <td><button class="apply-cfg-btn" style="font-size:9px;padding:2px 8px;border:1px solid var(--border2);border-radius:4px;background:transparent;color:var(--glow2);cursor:pointer">↗ Usage</button></td>`;
    tr.querySelector(".apply-cfg-btn").addEventListener("click", ()=>applyConfig(r));
    tbody.appendChild(tr);
    tbody.appendChild(tr);
  });
}

function applyConfig(r){
  // Pas de backtest configuratie aan to de geselecteerde configuratie
  document.getElementById("bt-pair").value  = r.pair;
  document.getElementById("bt-tf").value    = r.tf;
  document.getElementById("bt-rr").value    = r.rr;
  document.getElementById("bt-score").value = r.score||2;
  document.getElementById("use-ob").checked = r.ob;
  document.getElementById("use-eq").checked = r.eq;
  document.getElementById("use-session").checked = r.kz;
  document.getElementById("bt-be").value    = r.be||0;
  updateTFNote();
  // Scroll to backtest config
  document.getElementById("bt-run").scrollIntoView({behavior:"smooth"});
}

async function runBacktest(){
  const btn=document.getElementById("bt-run");
  btn.disabled=true;btn.textContent="refresh Bezig...";
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
    max_risk_eur:parseFloat(document.getElementById("bt-max-risk-eur").value)||0,
    hard_close_pnl_eur:parseFloat(document.getElementById("bt-hard-close-eur").value)||0,
    use_ob:document.getElementById("use-ob").checked,
    use_trend:document.getElementById("use-trend").checked,
    use_eq:document.getElementById("use-eq").checked,
    use_session:document.getElementById("use-session").checked,
    use_sweep:document.getElementById("use-sweep").checked,
    use_htf_bias:document.getElementById("use-htf").checked,
    use_smt:document.getElementById("use-smt").checked,
    skip_asian:document.getElementById("use-asian").checked,
    skip_holidays:document.getElementById("use-skip-holidays").checked,
    require_htf_orderflow:document.getElementById("use-req-htf").checked,
    require_dol:document.getElementById("use-req-dol").checked,
  };
  S.pair = body.pair==="BOTH" ? "EURUSD" : body.pair;
  S.tf   = body.tf;
  try{
    const r=await fetch("/api/backtest",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){alert(d.error);btn.disabled=false;btn.textContent="> Run Backtest";return}
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
      if(t.entry_ts) markers.push({time:t.entry_ts,position:t.direction==="LONG"?"belowBar":"aboveBar",color:t.direction==="LONG"?"#22d3ee":"#f59e0b",shape:t.direction==="LONG"?"arrowUp":"arrowDown",text:`${t.direction} ${t.entry_price}`});
      if(t.exit_ts)  markers.push({time:t.exit_ts,position:"inBar",color:t.outcome==="win"?"#34d399":"#f87171",shape:"circle",text:`€${t.pnl_eur}`});
    });
    markers.sort((a,b)=>a.time-b.time);
    S.series.setMarkers(markers);
    renderBtStats(d.stats,d.trades);
    document.getElementById("bt-results").style.display="";
    document.getElementById("bt-wr-card").style.display="";
  }catch(e){alert("Error: "+e.message)}
  btn.disabled=false;btn.textContent="> Run Backtest";
}

function renderBtStats(s,trades){
  const pc=s.total_pnl>=0?"var(--green)":"var(--red)";
  const wr=s.total>0?s.winrate:0;
  document.getElementById("bt-wr").textContent=s.total>0?wr+"%":"—";
  document.getElementById("bt-wr-s").textContent=`${s.wins}W / ${s.losses}L${s.be>0?" / "+s.be+"BE":""} from ${s.total}`;
  document.getElementById("bt-pnl").textContent=s.total>0?`€${s.total_pnl}`:"—";
  document.getElementById("bt-pnl").style.color=pc;
  document.getElementById("bt-pnl-s").textContent=`gem. ${s.avg_pips} pips`;
  document.getElementById("bt-best").textContent=`€${s.best}`;
  document.getElementById("bt-worst").textContent=`€${s.worst}`;
  document.getElementById("bt-wr-bar").style.width=wr+"%";
  document.getElementById("bt-wr-pct").textContent=wr+"%";
  document.getElementById("bt-sg").innerHTML=`
    <div class="bt-stat"><div class="l">Trades</div><div class="v" style="color:#7dd3fc">${s.total}</div></div>
    <div class="bt-stat"><div class="l">Winrate</div><div class="v" style="color:${wr>=50?"var(--green)":"var(--red)"}">${wr}%</div></div>
    <div class="bt-stat"><div class="l">Total P&L</div><div class="v" style="color:${pc}">€${s.total_pnl}</div></div>
    <div class="bt-stat"><div class="l">Gem. Pips</div><div class="v" style="color:var(--glow3)">${s.avg_pips}</div></div>`;
  const tbody=document.getElementById("bt-tbody");
  tbody.innerHTML="";
  if(!trades.length){
    tbody.innerHTML='<tr><td colspan="12" style="text-align:center;padding:24px;color:var(--text3)">None setups gevonden in deze periode</td></tr>';
    return;
  }
  trades.forEach(t=>{
    const w=t.outcome==="win";
    const be=t.outcome==="be";
    const outcomeClass = w ? "win" : be ? "" : "loss";
    const outcomeStyle = be ? "color:var(--text3)" : "";
    const tr=document.createElement("tr");
    tr.style.cursor="pointer";
    tr.title="Click for trade detail";
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
  // Stel standaard spread/slippage in op basis from pair
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
  const ac=d.alignment_color||"#7dd3fc";
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
          <div style="flex:1;background:rgba(34,211,238,.08);border-radius:99px;height:3px">
            <div class="tf-mini-bar" style="width:${jW(b.j1)};background:${jColor(b.j1)}"></div>
          </div>
          <span style="width:12px;text-align:right;color:${jColor(b.j1)}">${b.j1>0?"▲":b.j1<0?"▼":"—"}</span>
        </div>
        <div class="tf-bar-row">
          <span style="width:50px">DOL</span>
          <div style="flex:1;background:rgba(34,211,238,.08);border-radius:99px;height:3px">
            <div class="tf-mini-bar" style="width:${jW(b.j2)};background:${jColor(b.j2)}"></div>
          </div>
          <span style="width:12px;text-align:right;color:${jColor(b.j2)}">${b.j2>0?"▲":b.j2<0?"▼":"—"}</span>
        </div>
        <div class="tf-bar-row">
          <span style="width:50px">PO3</span>
          <div style="flex:1;background:rgba(34,211,238,.08);border-radius:99px;height:3px">
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
    warn.innerHTML='<div class="cal-warning">! Today is er een high-impact event — wees forzichtig!</div>';
  } else if(d.is_nfp_week){
    warn.innerHTML='<div class="cal-warning">! NFP week — vermijd donderday en vrijday</div>';
  } else {
    warn.innerHTML="";
  }

  const el=document.getElementById("cal-events");
  if(!d.events||!d.events.length){el.innerHTML='<div style="color:var(--text3);font-size:11px">None events gevonden</div>';return}
  el.innerHTML=d.events.map(e=>{
    const isToday=e.date===d.today;
    const badge=e.impact==="high"?'<span class="cal-badge cal-high">HIGH</span>':'<span class="cal-badge cal-medium">MED</span>';
    const dateStr=e.date.slice(5); // MM-DD
    return `<div class="cal-event" style="${isToday?"background:rgba(34,211,238,.06);border-radius:6px;padding:6px 8px;margin:0 -8px":""}">
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

// ── ECONOMIC NEWS ──
let _newsCurrentTab = "today";
let _newsData = {events_today: [], events_tomorrow: [], holiday: {today: null, tomorrow: null}};

async function loadNews(){
  try{
    const r = await fetch("/api/news");
    const d = await r.json();
    if(!d.ok){
      document.getElementById("news-events").innerHTML = '<div style="text-align:center;padding:14px;color:var(--text3);font-size:12px">News temporarily unavailable</div>';
      return;
    }
    _newsData = d;
    document.getElementById("news-updated").textContent = `refresh ${d.fetched_at}`;
    renderHolidayBanner();
    renderNewsEvents();
  } catch(e) {
    document.getElementById("news-events").innerHTML = '<div style="text-align:center;padding:14px;color:var(--text3);font-size:12px">Loading error news</div>';
  }
}

function renderHolidayBanner(){
  const banner = document.getElementById("news-holiday-banner");
  const today    = _newsData.holiday && _newsData.holiday.today;
  const tomorrow = _newsData.holiday && _newsData.holiday.tomorrow;
  if(today){
    banner.style.display = "block";
    banner.innerHTML = `! <b>${today.title}</b> (${today.country}) today — verwait low liquidity en chop`;
  } else if(tomorrow){
    banner.style.display = "block";
    banner.innerHTML = `! Heads-up: <b>${tomorrow.title}</b> (${tomorrow.country}) tomorrow — plan rond low liquidity`;
  } else {
    banner.style.display = "none";
  }
}

function switchNewsTab(tab){
  _newsCurrentTab = tab;
  const todayBtn = document.getElementById("news-tab-today");
  const tomBtn   = document.getElementById("news-tab-tomorrow");
  if(tab === "today"){
    todayBtn.style.color = "var(--glow2)";
    todayBtn.style.borderBottomColor = "var(--glow)";
    tomBtn.style.color = "var(--text3)";
    tomBtn.style.borderBottomColor = "transparent";
  } else {
    tomBtn.style.color = "var(--glow2)";
    tomBtn.style.borderBottomColor = "var(--glow)";
    todayBtn.style.color = "var(--text3)";
    todayBtn.style.borderBottomColor = "transparent";
  }
  renderNewsEvents();
}

function renderNewsEvents(){
  const events = _newsCurrentTab === "today" ? _newsData.events_today : _newsData.events_tomorrow;
  const el = document.getElementById("news-events");
  if(!events || !events.length){
    const lbl = _newsCurrentTab === "today" ? "today" : "tomorrow";
    el.innerHTML = `<div style="text-align:center;padding:14px;color:var(--text3);font-size:12px">None significante EUR/USD events ${lbl}</div>`;
    return;
  }
  el.innerHTML = events.map(e => {
    let impactColor, impactBg, impactLabel;
    if(e.impact === "high"){
      impactColor = "#f87171"; impactBg = "rgba(248,113,113,.12)"; impactLabel = "HIGH";
    } else if(e.impact === "medium"){
      impactColor = "#fbbf24"; impactBg = "rgba(251,191,36,.12)"; impactLabel = "MED";
    } else if(e.impact === "holiday"){
      impactColor = "#a78bfa"; impactBg = "rgba(167,139,250,.12)"; impactLabel = "HOL";
    } else {
      impactColor = "#94a3b8"; impactBg = "rgba(148,163,184,.1)"; impactLabel = "LOW";
    }
    const curFlag = e.country === "USD" ? "US" : (e.country === "EUR" ? "EU" : "");
    return `<div style="display:grid;grid-template-columns:55px 30px 1fr auto;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid rgba(34,211,238,.08);font-size:12px">
      <div style="font-family:'JetBrains Mono',monospace;color:var(--glow3);font-weight:600">${e.time}</div>
      <div style="font-size:16px;text-align:center">${curFlag}</div>
      <div style="color:var(--text)">${e.title}${e.forecast?` <span style="color:var(--text3);font-size:10px">F:${e.forecast}</span>`:""}${e.previous?` <span style="color:var(--text3);font-size:10px">P:${e.previous}</span>`:""}</div>
      <div style="padding:2px 8px;border-radius:4px;background:${impactBg};color:${impactColor};font-size:9px;font-weight:700;letter-spacing:.5px">${impactLabel}</div>
    </div>`;
  }).join("");
}

function toggleNewsCard(){
  const body = document.getElementById("news-card-body");
  const chev = document.getElementById("news-chev");
  if(body.style.display === "none"){
    body.style.display = "";
    chev.style.transform = "rotate(0deg)";
  } else {
    body.style.display = "none";
    chev.style.transform = "rotate(-90deg)";
  }
}

// ── TRADE DETAIL MODAL ──
let modalChart=null, modalSeries=null;

function openTradeModal(trade){
  // Show modal
  document.getElementById("trade-modal").style.display="flex";
  const w=trade.outcome==="win";
  document.getElementById("modal-title").textContent=
    `Trade #${trade.id} — ${trade.pair} ${trade.direction} — ${w?"WIN":"X LOSS"}`;

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

// Live trade modal — fetcht candles via API endpoint (werkt voor alle historische trades)
async function openLiveTradeModal(tradeId){
  document.getElementById("trade-modal").style.display="flex";
  document.getElementById("modal-title").textContent = `Trade #${tradeId} — Loading...`;
  document.getElementById("modal-details").innerHTML = '<div style="padding:14px;color:var(--text3);font-size:12px">Loading trade data...</div>';
  const el = document.getElementById("modal-chart");
  el.innerHTML = '<div style="padding:20px;color:var(--text3);font-size:12px;text-align:center">Loading chart...</div>';

  try {
    const r = await fetch(`/api/trade/${tradeId}/candles`);
    const d = await r.json();
    if(!d.ok){
      document.getElementById("modal-title").textContent = `Trade #${tradeId} — Error`;
      el.innerHTML = `<div style="padding:20px;color:var(--red);font-size:12px;text-align:center">${d.error||"Failed to load"}</div>`;
      return;
    }

    // Update title
    const isOpen = d.is_open;
    const w = d.outcome === "win";
    const statusLabel = isOpen ? "OPEN" : (w ? "WIN" : (d.outcome === "be" ? "BE" : "LOSS"));
    document.getElementById("modal-title").textContent =
      `Trade #${tradeId} — ${d.pair} ${d.direction} — ${statusLabel}`;

    // Update details
    const exitDisp = d.exit_price !== null && d.exit_price !== undefined ? d.exit_price : "—";
    const pipsDisp = d.pips !== null && d.pips !== undefined ? ((d.pips>0?"+":"")+d.pips.toFixed(1)) : "—";
    const pnlDisp  = d.pnl_eur !== null && d.pnl_eur !== undefined ? ((d.pnl_eur>=0?"+":"")+"€"+d.pnl_eur.toFixed(2)) : "—";
    const cls = w ? "var(--green)" : (d.outcome === "loss" ? "var(--red)" : "var(--glow3)");
    document.getElementById("modal-details").innerHTML = `
      <div class="modal-stat"><div class="l">Entry</div><div class="v" style="color:var(--glow3)">${d.entry_price}</div></div>
      <div class="modal-stat"><div class="l">Exit</div><div class="v" style="color:${cls}">${exitDisp}</div></div>
      <div class="modal-stat"><div class="l">Stop Loss</div><div class="v" style="color:var(--red)">${d.sl||"—"}</div></div>
      <div class="modal-stat"><div class="l">Take Profit</div><div class="v" style="color:var(--green)">${d.tp||"—"}</div></div>
      <div class="modal-stat"><div class="l">Pips</div><div class="v" style="color:${cls}">${pipsDisp}</div></div>
      <div class="modal-stat"><div class="l">P&L</div><div class="v" style="color:${cls}">${pnlDisp}</div></div>
      <div class="modal-stat"><div class="l">Bias Score</div><div class="v" style="color:var(--glow2)">${d.bias_score>0?"+":""}${d.bias_score||0}</div></div>
      <div class="modal-stat"><div class="l">Hit</div><div class="v" style="color:var(--text2)">${d.hit||(isOpen?"open":"manual")}</div></div>`;

    // Render chart
    setTimeout(()=>buildLiveTradeChart(d), 50);
  } catch(e) {
    console.error("[openLiveTradeModal]", e);
    el.innerHTML = `<div style="padding:20px;color:var(--red);font-size:12px;text-align:center">Connection error</div>`;
  }
}

// Render chart in modal voor live trade data (van API)
function buildLiveTradeChart(d){
  const el = document.getElementById("modal-chart");
  el.innerHTML = "";

  if(modalChart){ try{modalChart.remove()}catch(e){} modalChart=null; }

  if(!d.candles || !d.candles.length){
    el.innerHTML = '<div style="padding:20px;color:var(--text3);font-size:12px;text-align:center">No candle data available</div>';
    return;
  }

  modalChart = LightweightCharts.createChart(el, {
    layout:{background:{type:"Solid",color:"#04020f"},textColor:"#7dd3fc"},
    grid:{vertLines:{color:"rgba(31,58,94,.25)"},horzLines:{color:"rgba(31,58,94,.25)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#1f3a5e",timeVisible:true,secondsVisible:false},
    rightPriceScale:{borderColor:"#1f3a5e",minimumWidth:80},
    handleScroll:true,handleScale:true,
  });
  new ResizeObserver(()=>{if(modalChart)modalChart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);

  modalSeries = modalChart.addCandlestickSeries({
    upColor:"#22d3ee", downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#7dd3fc", borderDownColor:"#f87171",
    wickUpColor:"#7dd3fc", wickDownColor:"#f87171",
    priceFormat:{type:"price",precision:5,minMove:0.00001},
  });

  modalSeries.setData(d.candles);

  // SL/TP/Entry lines
  if(d.entry_price) modalSeries.createPriceLine({price:d.entry_price,color:"#7dd3fc",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"Entry"});
  if(d.sl)  modalSeries.createPriceLine({price:d.sl,color:"#f87171",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"SL"});
  if(d.tp)  modalSeries.createPriceLine({price:d.tp,color:"#34d399",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"TP"});

  // Entry/exit markers
  const markers = [];
  if(d.entry_ts){
    markers.push({
      time: d.entry_ts,
      position: d.direction === "LONG" ? "belowBar" : "aboveBar",
      color: d.direction === "LONG" ? "#22d3ee" : "#f59e0b",
      shape: d.direction === "LONG" ? "arrowUp" : "arrowDown",
      text: `Entry ${d.entry_price}`
    });
  }
  if(d.exit_ts && d.exit_price){
    markers.push({
      time: d.exit_ts,
      position: "inBar",
      color: d.outcome === "win" ? "#34d399" : "#f87171",
      shape: "circle",
      text: `Exit ${d.exit_price}`
    });
  }
  if(markers.length) modalSeries.setMarkers(markers);

  modalChart.timeScale().fitContent();
}

function buildModalChart(trade){
  const el=document.getElementById("modal-chart");
  el.innerHTML="";

  if(modalChart){ try{modalChart.remove()}catch(e){} modalChart=null; }

  modalChart=LightweightCharts.createChart(el,{
    layout:{background:{type:"Solid",color:"#08050f"},textColor:"#5a4e80"},
    grid:{vertLines:{color:"rgba(31,58,94,.25)"},horzLines:{color:"rgba(31,58,94,.25)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#2d1f5e",timeVisible:true,secondsVisible:false},
    rightPriceScale:{borderColor:"#1f3a5e",minimumWidth:80},
    handleScroll:true,handleScale:true,
  });
  new ResizeObserver(()=>{if(modalChart)modalChart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);

  modalSeries=modalChart.addCandlestickSeries({
    upColor:"#22d3ee",downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#7dd3fc",borderDownColor:"#f87171",
    wickUpColor:"#7dd3fc",wickDownColor:"#f87171",
    priceFormat:{type:"price",precision:5,minMove:0.00001},
  });

  // Get candles around the trade from the main series data
  // Filter main chart data to window around trade entry/exit
  const allData=S.series?S.series.data():[];
  if(!allData||!allData.length){ el.innerHTML='<div style="padding:20px;color:#7dd3fc;font-size:12px">None candledata beschikbaar — run de backtest again</div>'; return; }

  const entryTs=trade.entry_ts;
  const exitTs=trade.exit_ts;
  const window=Math.max(exitTs-entryTs, 86400)*3; // 3x the trade duration
  const filtered=allData.filter(c=>c.time>=entryTs-window && c.time<=exitTs+window);
  if(!filtered.length){ el.innerHTML='<div style="padding:20px;color:#7dd3fc;font-size:12px">No data in dit venster</div>'; return; }

  modalSeries.setData(filtered);

  // SL/TP price lines
  modalSeries.createPriceLine({price:trade.entry_price,color:"#7dd3fc",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"Entry"});
  modalSeries.createPriceLine({price:trade.sl,color:"#f87171",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"SL"});
  modalSeries.createPriceLine({price:trade.tp,color:"#34d399",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"TP"});

  // Entry/exit markers
  const markers=[
    {time:entryTs,position:trade.direction==="LONG"?"belowBar":"aboveBar",color:trade.direction==="LONG"?"#22d3ee":"#f59e0b",shape:trade.direction==="LONG"?"arrowUp":"arrowDown",text:`Entry ${trade.entry_price}`},
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
  if(!val || val <= 0){ alert(`Vul een valid ${type.toUpperCase()} prijsniveau in.`); return; }
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
    alert(d.error||"Close mislukt");
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
    use_htf_bias:document.getElementById("lt-htf").checked,
    use_smt:     document.getElementById("lt-smt").checked,
    skip_asian:  document.getElementById("lt-asian").checked,
    skip_holidays: document.getElementById("lt-skip-holidays").checked,
    require_htf_orderflow: document.getElementById("lt-req-htf").checked,
    require_dol:           document.getElementById("lt-req-dol").checked,
    auto_sltp:             document.getElementById("lt-auto-sltp").checked,
    send_chart_screenshots: document.getElementById("lt-chart-screenshots").checked,
    mt5_execution_enabled:  document.getElementById("lt-mt5-execution").checked,
    rr:                    parseFloat(document.getElementById("lt-rr").value)||2,
    trade_both:  document.getElementById("lt-pair").value === "BOTH",
    spread_pips: parseFloat(document.getElementById("lt-spread").value)||0,
    slippage_pips: parseFloat(document.getElementById("lt-slip").value)||0,
    max_daily_loss: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-loss").value)||0) : 0,
    max_trades:     document.getElementById("lt-risk-toggle").checked ? (parseInt(document.getElementById("lt-max-trades").value)||0) : 0,
    max_risk_pct:   document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-risk-pct").value)||0) : 0,
    max_risk_eur:       document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-risk-eur").value)||0) : 0,
    hard_close_pnl_eur: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-hard-close-eur").value)||0) : 0,
    discord_webhook: document.getElementById("lt-discord").value.trim(),
  };
  const r = await fetch("/api/presets/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,config:cfg})});
  const d = await r.json();
  if(d.ok){
    document.getElementById("lt-preset-name").value = "";
    renderPresets(d.presets);
  } else { alert(d.error||"Save mislukt"); }
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
  // Vul all velden in
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
  document.getElementById("lt-htf").checked     = !!c.use_htf_bias;
  document.getElementById("lt-smt").checked     = !!c.use_smt;
  document.getElementById("lt-asian").checked   = !!c.skip_asian;
  document.getElementById("lt-skip-holidays").checked = c.skip_holidays !== false; // default aan
  document.getElementById("lt-req-htf").checked = !!c.require_htf_orderflow;
  document.getElementById("lt-req-dol").checked = !!c.require_dol;
  document.getElementById("lt-auto-sltp").checked = !!c.auto_sltp;
  document.getElementById("lt-chart-screenshots").checked = !!c.send_chart_screenshots;
  document.getElementById("lt-mt5-execution").checked = !!c.mt5_execution_enabled;
  if(c.rr) document.getElementById("lt-rr").value = c.rr;
  if(c.discord_webhook) document.getElementById("lt-discord").value = c.discord_webhook;
  if(c.spread_pips)   document.getElementById("lt-spread").value = c.spread_pips;
  if(c.slippage_pips) document.getElementById("lt-slip").value   = c.slippage_pips;
}

function renderPresets(presets){
  const el = document.getElementById("lt-presets-list");
  if(!el) return;
  const names = Object.keys(presets||{});
  if(!names.length){
    el.innerHTML = '<div style="font-size:10px;color:var(--text3)">Nog none presets saved.</div>';
    return;
  }
  el.innerHTML = names.map(n=>`
    <div style="display:flex;align-items:center;gap:6px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:rgba(8,5,24,.6)">
      <span style="flex:1;font-size:11px;color:var(--text2)">${n}</span>
      <span style="font-size:9px;color:var(--text3)">${(presets[n].saved_at||"").slice(0,16)}</span>
      <button onclick="loadPresetIntoForm('${n}')" style="padding:2px 8px;border-radius:4px;border:1px solid var(--border2);background:rgba(34,211,238,.15);color:var(--glow2);font-size:10px;cursor:pointer">Loading</button>
      <button onclick="deletePreset('${n}')" style="padding:2px 8px;border-radius:4px;border:1px solid rgba(248,113,113,.3);background:rgba(248,113,113,.08);color:var(--red);font-size:10px;cursor:pointer">X</button>
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
    use_htf_bias:document.getElementById("lt-htf").checked,
    use_smt:     document.getElementById("lt-smt").checked,
    skip_asian:  document.getElementById("lt-asian").checked,
    skip_holidays: document.getElementById("lt-skip-holidays").checked,
    require_htf_orderflow: document.getElementById("lt-req-htf").checked,
    require_dol:           document.getElementById("lt-req-dol").checked,
    auto_sltp:             document.getElementById("lt-auto-sltp").checked,
    send_chart_screenshots: document.getElementById("lt-chart-screenshots").checked,
    mt5_execution_enabled:  document.getElementById("lt-mt5-execution").checked,
    rr:                    parseFloat(document.getElementById("lt-rr").value)||2,
    be_trigger:  0,
    trade_both:  document.getElementById("lt-pair").value === "BOTH",
    spread_pips: parseFloat(document.getElementById("lt-spread").value)||0,
    slippage_pips: parseFloat(document.getElementById("lt-slip").value)||0,
    max_daily_loss: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-loss").value)||0) : 0,
    max_trades:     document.getElementById("lt-risk-toggle").checked ? (parseInt(document.getElementById("lt-max-trades").value)||0) : 0,
    max_risk_pct:   document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-risk-pct").value)||0) : 0,
    max_risk_eur:       document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-max-risk-eur").value)||0) : 0,
    hard_close_pnl_eur: document.getElementById("lt-risk-toggle").checked ? (parseFloat(document.getElementById("lt-hard-close-eur").value)||0) : 0,
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
    document.getElementById("engine-status-badge").textContent="● ACTIVE";
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
  // Open trades tellen bijhouden for auto-refresh logica
  window._lastOpenTrades = (d.open_trades||[]).length;

  // Sync buttons met echte engine status
  const startBtn  = document.getElementById("lt-start-btn");
  const stopBtn   = document.getElementById("lt-stop-btn");
  const pauseBtn  = document.getElementById("lt-pause-btn");
  if(startBtn && stopBtn){
    startBtn.disabled      = d.running;
    startBtn.style.opacity = d.running ? ".4" : "1";
    stopBtn.disabled       = !d.running;
    stopBtn.style.opacity  = d.running ? "1" : ".4";
  }
  // Pause knop
  if(pauseBtn){
    if(!d.running){
      pauseBtn.style.display = "none";
    } else {
      pauseBtn.style.display = "";
      if(d.paused){
        pauseBtn.textContent = "> Resume";
        pauseBtn.style.background = "rgba(34,197,94,.15)";
        pauseBtn.style.color = "var(--green)";
        pauseBtn.style.borderColor = "rgba(34,197,94,.3)";
        pauseBtn.onclick = resumeEngine;
      } else {
        pauseBtn.textContent = "PAUSE";
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
      badge.textContent = "STOP GESTOPT — RISICO LIMIET";
      badge.style.background = "rgba(248,113,113,.2)";
      badge.style.color = "var(--red)";
      badge.style.borderColor = "rgba(248,113,113,.4)";
    } else if(d.running && d.paused){
      badge.textContent = "|| GEPAUSEERD";
      badge.style.background = "rgba(251,191,36,.12)";
      badge.style.color = "var(--amber)";
      badge.style.borderColor = "rgba(251,191,36,.3)";
    } else if(d.running){
      badge.textContent = "● ACTIVE";
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

  // Daily P&L tonen in scans card
  const dailyEl = document.getElementById("lt-last-scan");
  if(dailyEl && d.daily_pnl !== undefined){
    const dp = d.daily_pnl || 0;
    dailyEl.textContent = `last: ${d.last_scan||"—"} · Daily: €${dp>=0?"+":""}${dp.toFixed(2)}`;
    dailyEl.style.color = dp >= 0 ? "var(--green)" : "var(--red)";
  }

  // Scan info + uptime
  const scansEl = document.getElementById("lt-scans");
  if(scansEl) scansEl.textContent = d.scan_count || 0;
  const scanInfoEl = document.getElementById("engine-scan-info");
  if(scanInfoEl){
    const uptimePart = d.uptime && d.running ? ` · timer ${d.uptime}` : "";
    const pausedPart = d.paused ? " · || GEPAUSEERD" : "";
    scanInfoEl.textContent = d.last_scan ? `last scan: ${d.last_scan}${uptimePart}${pausedPart}` : "";
  }

  // Countdown timer — tijd tot next scan (20s interval)
  const cdEl = document.getElementById("lt-scan-countdown");
  if(cdEl && d.running && !d.paused){
    // Wis previous timer
    if(window._scanCountdownTimer) clearInterval(window._scanCountdownTimer);
    let secs = 20;
    cdEl.textContent = `next scan: ${secs}s`;
    window._scanCountdownTimer = setInterval(()=>{
      secs--;
      if(secs <= 0){
        clearInterval(window._scanCountdownTimer);
        cdEl.textContent = "scant nu...";
        secs = 20;
      } else {
        cdEl.textContent = `next scan: ${secs}s`;
      }
    }, 1000);
  } else if(cdEl){
    if(window._scanCountdownTimer) clearInterval(window._scanCountdownTimer);
    cdEl.textContent = d.paused ? "|| gepauzeerd" : "";
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

  // SKIP re-render als gebruiker in een SL/TP input aan het typen is
  const activeEl = document.activeElement;
  const isTypingSlTp = activeEl && activeEl.tagName === "INPUT" &&
    (activeEl.id||"").match(/^(sl|tp)-inp-/);

  if(isTypingSlTp){
    // Update allen live prijs en P&L inline, bewaar de inputs
    (d.open_trades||[]).forEach(t=>{
      const pnl = t.pnl_eur || 0;
      const priceEl = document.querySelector(`[data-dt-price="${t.id}"]`);
      const pnlEl   = document.querySelector(`[data-dt-pnl="${t.id}"]`);
      if(priceEl) priceEl.textContent = t.live_price||"—";
      if(pnlEl){
        pnlEl.textContent = `${pnl>=0?"+":""}€${pnl.toFixed(2)}`;
        pnlEl.className = pnl >= 0 ? "win" : "loss";
      }
    });
  } else if(!d.open_trades || !d.open_trades.length){
    otb.innerHTML='<tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">No open positions</td></tr>';
  } else {
    otb.innerHTML = d.open_trades.map(t=>{
      const pnl  = t.pnl_eur || 0;
      const pc   = pnl >= 0 ? "win" : "loss";
      const slV  = t.sl   ? t.sl   : "";
      const tpV  = t.tp   ? t.tp   : "";
      const slPH = t.sl   ? t.sl   : "None SL";
      const tpPH = t.tp   ? t.tp   : "None TP";
      return `<tr>
        <td>${t.id}</td>
        <td>${t.pair}</td>
        <td><span class="pill ${t.direction==="LONG"?"pill-purple":"pill-amber"}">${t.direction}</span></td>
        <td>${t.entry_price}</td>
        <td data-dt-price="${t.id}" style="color:var(--glow3)">${t.live_price||"—"}</td>
        <td data-dt-pnl="${t.id}" class="${pc}">${pnl>=0?"+":""}€${pnl.toFixed(2)}</td>
        <td>
          <div style="display:flex;gap:4px;align-items:center">
            <input id="sl-inp-${t.id}" type="number" step="0.00001" value="${slV}" placeholder="${slPH}"
              style="width:90px;padding:3px 6px;border-radius:4px;border:1px solid ${t.sl?"var(--green)":"rgba(248,113,113,.4)"};background:rgba(8,5,24,.8);color:var(--text);font-size:10px;font-family:'JetBrains Mono',monospace">
            <button onclick="setSlTp(${t.id},'sl')" style="padding:3px 7px;border-radius:4px;border:1px solid var(--border2);background:rgba(34,197,94,.15);color:var(--green);font-size:10px;cursor:pointer;white-space:nowrap">SL</button>
          </div>
        </td>
        <td>
          <div style="display:flex;gap:4px;align-items:center">
            <input id="tp-inp-${t.id}" type="number" step="0.00001" value="${tpV}" placeholder="${tpPH}"
              style="width:90px;padding:3px 6px;border-radius:4px;border:1px solid ${t.tp?"var(--green)":"rgba(34,211,238,.4)"};background:rgba(8,5,24,.8);color:var(--text);font-size:10px;font-family:'JetBrains Mono',monospace">
            <button onclick="setSlTp(${t.id},'tp')" style="padding:3px 7px;border-radius:4px;border:1px solid var(--border2);background:rgba(34,211,238,.15);color:var(--glow2);font-size:10px;cursor:pointer;white-space:nowrap">TP</button>
          </div>
        </td>
        <td style="font-size:9px;color:var(--glow2)">${t.filters||"FVG"}</td>
        <td>
          <button onclick="closeTrade(${t.id},'${t.pair}',${t.entry_price})"
            style="padding:4px 10px;border-radius:5px;border:1px solid rgba(248,113,113,.4);background:rgba(248,113,113,.1);color:var(--red);font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap">
            Close
          </button>
        </td>
      </tr>`;
    }).join("");
    // Draw markers on chart for open trades
    const markers = d.open_trades.map(t=>({
      time: t.opened_ts,
      position: t.direction==="LONG"?"belowBar":"aboveBar",
      color: t.direction==="LONG"?"#22d3ee":"#f59e0b",
      shape: t.direction==="LONG"?"arrowUp":"arrowDown",
      text: `${t.direction} ${t.entry_price}`
    }));
    try{ S.series && S.series.setMarkers(markers); }catch(e){}
  }

  // Closed trades table
  const ctb = document.getElementById("live-closed-tbody");
  if(!d.closed_trades || !d.closed_trades.length){
    ctb.innerHTML='<tr><td colspan="10" style="text-align:center;padding:16px;color:var(--text3)">Nog none closed trades</td></tr>';
  } else {
    ctb.innerHTML = [...d.closed_trades].reverse().map(t=>{
      const w = t.outcome==="win";
      const be = t.outcome==="be";
      const cls = w?"win":be?"":"loss";
      return `<tr onclick="openLiveTradeModal(${t.id})" style="cursor:pointer" title="Click to view chart">
        <td>${t.id}</td>
        <td style="font-size:9px">${(t.opened_at||"").slice(5,16)}</td>
        <td>${t.pair}</td>
        <td><span class="pill ${t.direction==="LONG"?"pill-purple":"pill-amber"}">${t.direction}</span></td>
        <td>${t.entry_price}</td><td>${t.exit_price||"—"}</td>
        <td class="${cls}">${(t.pips||0)>0?"+":""}${t.pips||0}${be?" (BE)":""}</td>
        <td class="${cls}">${(t.pnl_eur||0)>=0?"+":""}€${(t.pnl_eur||0).toFixed(2)}</td>
        <td>${t.bias_score>0?"+":""}${t.bias_score}</td>
        <td><button onclick="event.stopPropagation();deleteTrade(${t.id})" style="font-size:9px;padding:1px 6px;border:1px solid rgba(248,113,113,.3);border-radius:3px;background:transparent;color:var(--red);cursor:pointer">X</button></td>
      </tr>`;
    }).join("");
  }

  // Engine log
  const el = document.getElementById("engine-log-list");
  if(d.logs && d.logs.length){
    el.innerHTML = [...d.logs].reverse().map(l=>{
      const col = l.level==="TRADE" ? "var(--glow2)" : l.level==="ERROR" ? "var(--red)" : l.level==="START"||l.level==="STOP" ? "var(--amber)" : "var(--text2)";
      return `<div style="padding:2px 0;border-bottom:1px solid rgba(31,58,94,.3)"><span style="color:var(--text3)">${l.time}</span> <span style="color:${col}">[${l.level}]</span> ${l.msg}</div>`;
    }).join("");
  }
}

function exportEngineLogCSV(){
  fetch("/api/engine/status").then(r=>r.json()).then(d=>{
    const rows=[["Pair","Richting","Entry","SL","TP","PnL (€)","Date"]];
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
    const rows=[["Pair","Richting","Entry","SL","TP","PnL (€)","Date"]];
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

  // ── Make right-panel toggle-sections collapsible ──
  // Auto-wraps content of each .toggle-section inside #live-engine-config in .accordion-body
  // and adds click handler on the section title to toggle collapse.
  try{
    const container = document.getElementById("live-engine-config");
    if(container){
      const sections = container.querySelectorAll(".toggle-section");
      sections.forEach((sec) => {
        // Find direct child .toggle-section-title (skip Riskbeheer which has wrapped title)
        const title = sec.querySelector(":scope > .toggle-section-title");
        if(!title) return;  // Riskbeheer skip — title sits inside a wrapper div
        // Wrap all siblings after title in .accordion-body
        const body = document.createElement("div");
        body.className = "accordion-body";
        const children = [...sec.children];
        children.forEach(node => {
          if(node === title) return;
          body.appendChild(node);
        });
        sec.appendChild(body);
        sec.classList.add("collapsible", "collapsed");
        title.addEventListener("click", (e) => {
          if(e.target.closest(".switch") || e.target.closest("input")) return;
          sec.classList.toggle("collapsed");
        });
      });
    }
  }catch(e){console.error("[accordion]", e);}

  try{initChart()}catch(e){document.getElementById("chart").innerHTML=`<div style="padding:40px;color:#fafafa;font-family:Inter,sans-serif">Chart error: ${e.message}</div>`}
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

  // ── News refresh every 5 minutes (cache is 30 min server-side maar UI refresht sneller) ──
  loadNews().catch(console.error);
  setInterval(()=>{ loadNews().catch(console.error); }, 300000);

  async function pollMT5Status(){
    try{
      const r = await fetch("/api/mt5/status");
      const d = await r.json();
      const box = document.getElementById("mt5-status-box");
      if(!box) return;

      if(!d.available){
        box.innerHTML = '<div style="color:#f87171">⊘ MT5 Files folder niet bereikbaar<br><span style="color:var(--text3);font-size:9px">'+d.files_dir+'</span></div>';
        return;
      }

      if(!d.alive){
        box.innerHTML = '<div style="color:#f59e0b">⚠ EA offline (geen heartbeat)<br><span style="color:var(--text3);font-size:9px">Check of EA op chart draait in MT5</span></div>';
        return;
      }

      const hb = d.heartbeat;
      const age = Math.round(hb._age_sec);
      const exec = d.execution_enabled ? '<span style="color:#34d399">● LIVE</span>' : '<span style="color:var(--text3)">○ paper</span>';
      box.innerHTML = `
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--text3)">EA Status:</span><span style="color:#34d399">● Connected (${age}s ago)</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--text3)">Account:</span><span>${hb.account}</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--text3)">Balance:</span><span style="color:#7dd3fc">€${hb.balance.toFixed(2)}</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--text3)">Equity:</span><span style="color:#7dd3fc">€${hb.equity.toFixed(2)}</span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="color:var(--text3)">Open positions:</span><span>${hb.open_positions}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--text3)">Execution mode:</span>${exec}</div>
      `;
    }catch(e){
      console.error("[MT5-STATUS]", e);
    }
  }

  // ── Engine status poll every 5 seconds ──
  setInterval(pollEngineStatus, 5000);

  // ── MT5 status poll every 10 seconds ──
  pollMT5Status();
  setInterval(pollMT5Status, 10000);

  // ── Auto-refresh na 6u om DOM bloat te forkomen ──
  // Refresht allen als er none open trades zijn (veilig)
  const PAGE_START = Date.now();
  setInterval(()=>{
    const ageH = (Date.now() - PAGE_START) / 3600000;
    if(ageH >= 6){
      const hasOpen = (window._lastOpenTrades||0) > 0;
      if(!hasOpen){
        console.log("[GAMAN] Auto-refresh na 6u uptime — none open trades");
        location.reload();
      } else {
        console.log("[GAMAN] Auto-refresh uitgesteld — open trades aanwezig");
      }
    }
  }, 300000); // check elke 5 min

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
      badge.title = lines.length ? lines.join("\n") : "Nog none fetches geregistreerd";
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
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>我慢 GAMAN</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Noto+Serif+JP:wght@700;900&display=swap" rel="stylesheet">
<script src="/static/lw-charts.js"></script>
<style>
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
:root {
  --bg:#04020f; --bg2:#0a1428; --card:rgba(15,15,25,.85);
  --border:rgba(42,42,63,.5); --border2:rgba(255,255,255,.25);
  --text:#fafafa; --text2:#e5e7eb; --text3:#6b6b7a;
  --glow:#ffffff; --glow2:#e5e7eb; --glow3:#f3f4f6;
  --green:#22c55e; --red:#f87171; --amber:#fbbf24;
}
body {
  margin:0; padding:0;
  background: radial-gradient(ellipse at top, #1a1a2e 0%, #04020f 60%);
  color:var(--text);
  font-family:'Inter',sans-serif;
  font-size:14px;
  min-height:100vh;
  padding-bottom:80px;
}
.header {
  position:sticky; top:0; z-index:100;
  background:linear-gradient(180deg, rgba(8,5,24,.98) 0%, rgba(8,5,24,.9) 100%);
  backdrop-filter: blur(10px);
  border-bottom:1px solid var(--border);
  padding:12px 16px 0;
}
.title-row {
  display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;
}
.logo {
  font-family:'Noto Serif JP', serif;
  font-size:22px; font-weight:900; color:var(--glow);
  text-shadow:0 0 12px rgba(255,255,255,.6), 0 0 24px rgba(255,255,255,.3);
  letter-spacing:1px;
  animation:logo-pulse 4s ease-in-out infinite;
}
@keyframes logo-pulse{
  0%,100%{text-shadow:0 0 10px rgba(255,255,255,.5), 0 0 20px rgba(255,255,255,.25)}
  50%{text-shadow:0 0 16px rgba(255,255,255,.7), 0 0 32px rgba(229,231,235,.4)}
}
.logo span { 
  color:var(--text); font-size:11px; font-weight:500; margin-left:6px;
  font-family:'Inter', sans-serif;
  letter-spacing:2px;
}
.live-price { font-family:'JetBrains Mono', monospace; font-size:14px; color:var(--glow3); }
.tabs {
  display:flex; gap:4px; margin-bottom:-1px;
}
.tab {
  flex:1; padding:12px 8px; border:none; background:transparent;
  color:var(--text3); font-family:inherit; font-size:13px; font-weight:600;
  cursor:pointer; border-bottom:2px solid transparent;
  transition:.2s;
}
.tab.active {
  color:var(--glow2);
  border-bottom-color:var(--glow);
}
.page { display:none; padding:16px; }
.page.active { display:block; }
.status-bar {
  display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; align-items:center;
}
.badge {
  display:inline-flex; align-items:center; gap:5px;
  padding:5px 10px; border-radius:14px;
  background:rgba(90,78,128,.2); border:1px solid var(--border);
  font-size:10px; font-weight:600; color:var(--text3);
}
.badge.open    { background:rgba(34,197,94,.12); color:var(--green); border-color:rgba(34,197,94,.3); }
.badge.closed  { background:rgba(248,113,113,.12); color:var(--red); border-color:rgba(248,113,113,.3); }
.badge.warn    { background:rgba(251,191,36,.12); color:var(--amber); border-color:rgba(251,191,36,.3); }
.badge.live    { background:rgba(34,211,238,.15); color:var(--glow2); border-color:var(--border2); }
.card {
  position:relative;
  background:var(--card);
  border:1px solid var(--border);
  border-radius:12px;
  margin-bottom:12px;
  overflow:hidden;
  box-shadow:0 0 0 1px rgba(34,211,238,.05), 0 0 16px rgba(34,211,238,.08);
}
.card::before{
  content:"我慢";
  position:absolute;
  right:6px; bottom:-8px;
  font-family:'Noto Serif JP', serif;
  font-size:72px; font-weight:900;
  color:rgba(34,211,238,.025);
  line-height:1; letter-spacing:-3px;
  pointer-events:none;
  user-select:none;
  z-index:0;
}
.card > * { position:relative; z-index:1; }
.card-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:12px 14px;
  background:linear-gradient(90deg, rgba(34,211,238,.06), transparent);
  border-bottom:1px solid var(--border);
  cursor:pointer; user-select:none;
}
.card-header.no-toggle { cursor:default; }
.card-title {
  display:flex; align-items:center; gap:6px;
  font-size:12px; font-weight:700; color:var(--glow2); text-transform:uppercase; letter-spacing:.5px;
}
.card-dot {
  width:6px; height:6px; border-radius:50%;
  background:var(--glow); box-shadow:0 0 8px var(--glow);
}
.card-chev { color:var(--text3); font-size:14px; transition:.2s; }
.card-chev.open { transform:rotate(180deg); }
.card-body { padding:14px; }
.card-body.collapsed { display:none; }
/* FORMS */
.form-group { margin-bottom:14px; }
.form-group:last-child { margin-bottom:0; }
.form-row {
  display:grid; grid-template-columns:1fr 1fr; gap:10px;
}
label {
  display:block; font-size:10px; font-weight:600;
  color:var(--text3); text-transform:uppercase; letter-spacing:.5px;
  margin-bottom:5px;
}
input[type="number"], input[type="text"], input[type="date"], select {
  width:100%;
  padding:11px 12px;
  border:1px solid var(--border2);
  border-radius:8px;
  background:rgba(8,5,24,.8);
  color:var(--text);
  font-family:'Inter',sans-serif;
  font-size:16px;
  outline:none;
  transition:.2s;
  -webkit-appearance:none;
  appearance:none;
}
input:focus, select:focus { border-color:var(--glow); box-shadow:0 0 0 3px rgba(34,211,238,.15); }
select {
  background-image: url("data:image/svg+xml,%3Csvg width='12' height='8' viewBox='0 0 12 8' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%23a78bfa' stroke-width='2' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;
  background-position:right 12px center;
  padding-right:34px;
}
/* TOGGLES */
.toggle-row {
  display:flex; align-items:center; justify-content:space-between;
  padding:10px 0;
  border-bottom:1px solid rgba(31,58,94,.3);
}
.toggle-row:last-child { border-bottom:none; }
.toggle-row.special { border-top:1px dashed var(--border); padding-top:14px; margin-top:6px; }
.toggle-info { flex:1; padding-right:10px; }
.toggle-name { font-size:13px; font-weight:600; color:var(--text); }
.toggle-name.glow { color:var(--glow2); }
.toggle-sub { font-size:10px; color:var(--text3); margin-top:2px; }
.switch {
  position:relative; display:inline-block;
  width:48px; height:28px; flex-shrink:0;
}
.switch input { opacity:0; width:0; height:0; }
.slider {
  position:absolute; top:0; left:0; right:0; bottom:0;
  background:rgba(90,78,128,.4); border-radius:28px;
  transition:.3s; cursor:pointer;
}
.slider:before {
  content:""; position:absolute;
  width:22px; height:22px; left:3px; top:3px;
  background:#fff; border-radius:50%;
  transition:.3s;
}
.switch input:checked + .slider { background:var(--glow); }
.switch input:checked + .slider:before { transform:translateX(20px); }
/* BUTTONS */
.btn {
  width:100%; padding:14px;
  border:none; border-radius:10px;
  font-family:'Inter',sans-serif; font-size:14px; font-weight:700;
  letter-spacing:.5px; cursor:pointer; transition:.2s;
  margin-bottom:8px;
  display:flex; align-items:center; justify-content:center; gap:6px;
}
.btn-primary {
  background:linear-gradient(135deg, var(--glow), #0e7490);
  color:#fff;
  box-shadow:0 0 18px rgba(34,211,238,.5), 0 4px 12px rgba(34,211,238,.3);
  text-shadow:0 0 8px rgba(34,211,238,.6);
}
.btn-primary:disabled { opacity:.4; }
.btn-stop {
  background:rgba(248,113,113,.12); color:var(--red);
  border:1px solid rgba(248,113,113,.4);
}
.btn-pause {
  background:rgba(251,191,36,.12); color:var(--amber);
  border:1px solid rgba(251,191,36,.4);
}
.btn-resume {
  background:rgba(34,197,94,.12); color:var(--green);
  border:1px solid rgba(34,197,94,.4);
}
.btn-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.btn-row .btn { margin-bottom:0; }
/* STATS GRID */
.stats-grid {
  display:grid; grid-template-columns:1fr 1fr; gap:8px;
  margin-bottom:12px;
}
.stat-box {
  background:rgba(8,5,24,.6);
  border:1px solid var(--border);
  border-radius:10px;
  padding:12px;
  text-align:center;
}
.stat-label {
  font-size:10px; color:var(--text3);
  text-transform:uppercase; letter-spacing:.5px;
  margin-bottom:4px;
}
.stat-val {
  font-family:'JetBrains Mono', monospace;
  font-size:20px; font-weight:700; color:var(--glow2);
}
.stat-val.green { color:var(--green); }
.stat-val.red   { color:var(--red); }
.stat-sub { font-size:9px; color:var(--text3); margin-top:2px; }
/* OPEN TRADES */
.trade-card {
  background:rgba(8,5,24,.6);
  border:1px solid var(--border);
  border-radius:10px;
  padding:12px;
  margin-bottom:10px;
}
.trade-head {
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:8px;
}
.pill {
  padding:3px 10px; border-radius:14px;
  font-size:10px; font-weight:700; letter-spacing:.5px;
}
.pill-long  { background:rgba(34,211,238,.2); color:var(--glow2); }
.pill-short { background:rgba(251,191,36,.2); color:var(--amber); }
.trade-pnl  { font-family:'JetBrains Mono', monospace; font-size:15px; font-weight:700; }
.win  { color:var(--green); }
.loss { color:var(--red); }
.trade-prices { font-size:10px; color:var(--text3); margin-bottom:10px; }
.sl-tp-row {
  margin-bottom:8px;
}
.sl-tp-label {
  font-size:9px; font-weight:600; letter-spacing:.5px;
  text-transform:uppercase; margin-bottom:4px;
}
.sl-tp-inp-wrap { display:flex; gap:6px; }
.sl-tp-inp-wrap input { flex:1; font-size:14px; font-family:'JetBrains Mono', monospace; padding:9px; }
.sl-tp-inp-wrap button {
  padding:9px 14px; border-radius:8px;
  font-family:'Inter',sans-serif; font-size:12px; font-weight:700; cursor:pointer;
  white-space:nowrap;
}
.sl-btn { background:rgba(248,113,113,.15); color:var(--red); border:1px solid rgba(248,113,113,.4); }
.tp-btn { background:rgba(34,197,94,.15); color:var(--green); border:1px solid rgba(34,197,94,.4); }
/* PRESETS */
.preset-item {
  background:rgba(8,5,24,.6);
  border:1px solid var(--border);
  border-radius:10px;
  padding:12px;
  margin-bottom:10px;
}
.preset-name { font-size:14px; font-weight:700; color:var(--glow2); margin-bottom:4px; }
.preset-info { font-size:10px; color:var(--text3); margin-bottom:10px; }
.preset-btns { display:grid; grid-template-columns:2fr 1fr; gap:6px; }
.preset-start {
  padding:10px; border:none; border-radius:8px;
  background:linear-gradient(135deg, var(--glow), #0e7490);
  color:#fff; font-family:'Inter',sans-serif; font-size:13px; font-weight:700; cursor:pointer;
}
.preset-del {
  padding:10px; border:1px solid rgba(248,113,113,.3); border-radius:8px;
  background:rgba(248,113,113,.08); color:var(--red);
  font-size:12px; cursor:pointer;
}
/* LOGS */
.log-list {
  font-family:'JetBrains Mono', monospace;
  font-size:10px; line-height:1.5;
  max-height:260px; overflow-y:auto;
  background:rgba(8,5,24,.6);
  border-radius:8px;
  padding:10px;
}
.log-line { padding:3px 0; border-bottom:1px solid rgba(31,58,94,.2); color:var(--text2); }
.log-line:last-child { border-bottom:none; }
.log-time { color:var(--text3); margin-right:6px; }
/* SYSTEM HEALTH */
.health-item {
  display:flex; align-items:center; justify-content:space-between;
  padding:10px 0; border-bottom:1px solid rgba(31,58,94,.3);
}
.health-item:last-child { border-bottom:none; }
.health-name { font-size:13px; font-weight:600; }
.health-status {
  display:inline-flex; align-items:center; gap:6px;
  font-size:11px; padding:4px 10px; border-radius:12px;
}
.health-ok    { background:rgba(34,197,94,.12); color:var(--green); }
.health-fail  { background:rgba(248,113,113,.12); color:var(--red); }
.health-warn  { background:rgba(251,191,36,.12); color:var(--amber); }
/* BACKTEST RESULTS */
.bt-stat-grid {
  display:grid; grid-template-columns:1fr 1fr; gap:8px;
  margin-top:14px; margin-bottom:14px;
}
.no-data {
  text-align:center; padding:20px; color:var(--text3); font-size:12px; font-style:italic;
}
.spinner {
  display:inline-block; width:14px; height:14px;
  border:2px solid rgba(34,211,238,.3); border-top-color:var(--glow);
  border-radius:50%; animation:spin .8s linear infinite;
}
@keyframes spin { to { transform:rotate(360deg); } }
.section-divider {
  height:1px; background:var(--border); margin:12px 0;
}
.toggle-section-title {
  font-size:10px; font-weight:700; color:var(--text3);
  text-transform:uppercase; letter-spacing:.8px;
  margin:14px 0 8px; padding-bottom:6px;
  border-bottom:1px solid var(--border);
}
.banner {
  padding:10px 12px; border-radius:8px;
  font-size:12px; margin-bottom:10px; text-align:center;
}
.banner-warn { background:rgba(251,191,36,.1); color:var(--amber); border:1px solid rgba(251,191,36,.3); }
.banner-info { background:rgba(34,211,238,.08); color:var(--glow2); border:1px solid var(--border2); }
</style>
</head>
<body>

<div class="header">
  <div class="title-row">
    <div class="logo" style="display:flex;align-items:center;gap:8px">
      <svg width="22" height="24" viewBox="0 0 32 36" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0">
        <ellipse cx="16" cy="6" rx="4" ry="4.5" fill="currentColor"/>
        <path d="M 9 11 L 23 11 L 25 20 L 22 20 L 22 32 L 18 32 L 18 22 L 14 22 L 14 32 L 10 32 L 10 20 L 7 20 Z" fill="currentColor"/>
        <rect x="24" y="2" width="1.5" height="18" transform="rotate(20 24 2)" fill="currentColor" opacity="0.8"/>
        <rect x="23.5" y="1" width="2.5" height="3" transform="rotate(20 23.5 1)" fill="currentColor"/>
      </svg>
      我慢 <span>GAMAN</span>
    </div>
    <div class="live-price" id="m-price">—</div>
  </div>
  <div class="tabs">
    <button class="tab active" onclick="mSwitchTab('live', event)">Live</button>
    <button class="tab" onclick="mSwitchTab('backtest', event)">Backtest</button>
    <button class="tab" onclick="mSwitchTab('system', event)">System</button>
  </div>
</div>

<!-- ============ LIVE PAGE ============ -->
<div class="page active" id="page-live">

  <div class="status-bar">
    <span class="badge" id="m-market">Market: —</span>
    <span class="badge" id="m-engine">Engine: —</span>
    <span class="badge live" id="m-uptime">timer —</span>
    <span class="badge live" id="m-datasource">● Data: —</span>
    <span class="badge" id="m-countdown">—</span>
  </div>

  <div id="m-risk-banner" style="display:none" class="banner banner-warn">
    STOP Engine stopped door risico limiet
  </div>

  <!-- STATS -->
  <div class="card">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Session Stats</div>
      <div style="font-size:10px;color:var(--text3)" id="m-daily-pnl">Daily: €0</div>
    </div>
    <div class="card-body">
      <div class="stats-grid">
        <div class="stat-box">
          <div class="stat-label">Trades</div>
          <div class="stat-val" id="m-trades">0</div>
          <div class="stat-sub">this session</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Winrate</div>
          <div class="stat-val" id="m-winrate">—</div>
          <div class="stat-sub" id="m-wl">0W / 0L</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Total P&L</div>
          <div class="stat-val" id="m-pnl">€0.00</div>
          <div class="stat-sub">this session</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Scans</div>
          <div class="stat-val" id="m-scans">0</div>
          <div class="stat-sub" id="m-lastscan">—</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ENGINE CONTROLS -->
  <div class="card">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Engine Control</div>
    </div>
    <div class="card-body">
      <div class="btn-row">
        <button class="btn btn-primary" id="m-exec-btn" onclick="mExecute()">EXECUTE</button>
        <button class="btn btn-stop" id="m-stop-btn" onclick="mShutdown()" disabled style="opacity:.4">■ SHUTDOWN</button>
      </div>
      <button class="btn btn-pause" id="m-pause-btn" onclick="mTogglePause()" style="display:none">PAUSE</button>
      <button class="btn btn-stop" onclick="mCloseAll()" style="background:rgba(248,113,113,.05);font-size:12px;padding:10px">
        Close All Open Trades
      </button>
    </div>
  </div>

  <!-- ECONOMIC NEWS -->
  <div class="card">
    <div class="card-header" onclick="mToggle('m-news-body','m-news-chev')">
      <div class="card-title"><div class="card-dot"></div>NEWS Economic Calendar</div>
      <span class="card-chev" id="m-news-chev">▼</span>
    </div>
    <div class="card-body collapsed" id="m-news-body">
      <div id="m-news-holiday" style="display:none;padding:10px;margin-bottom:10px;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.4);border-radius:8px;font-size:11px;color:var(--amber);text-align:center"></div>
      <div style="display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:10px">
        <button id="m-news-tab-today" onclick="mNewsTab('today')" style="flex:1;padding:10px;border:none;background:transparent;color:var(--glow2);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid var(--glow)">TODAY</button>
        <button id="m-news-tab-tomorrow" onclick="mNewsTab('tomorrow')" style="flex:1;padding:10px;border:none;background:transparent;color:var(--text3);font-family:'Inter',sans-serif;font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent">TOMORROW</button>
      </div>
      <div id="m-news-events">
        <div style="text-align:center;padding:14px;color:var(--text3);font-size:12px">Loading...</div>
      </div>
    </div>
  </div>

  <!-- OPEN POSITIONS (boven Live Config) -->
  <div class="card">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Open Positions</div>
      <span style="font-size:10px;color:var(--text3)" id="m-open-count">0 open</span>
    </div>
    <div class="card-body" id="m-open-body">
      <div class="no-data">No open positions</div>
    </div>
  </div>

  <!-- TRADE LOG (boven Live Config, collapsed) -->
  <div class="card">
    <div class="card-header" onclick="mToggle('m-tradelog-body','m-tradelog-chev')">
      <div class="card-title"><div class="card-dot"></div>Last Trades</div>
      <span class="card-chev" id="m-tradelog-chev">▼</span>
    </div>
    <div class="card-body collapsed" id="m-tradelog-body">
      <div id="m-trade-log"><div class="no-data">No trades yet</div></div>
    </div>
  </div>

  <!-- LIVE CONFIG (collapsible, standaard ingeklapt) -->
  <div class="card">
    <div class="card-header" onclick="mToggle('m-cfg-body','m-cfg-chev')">
      <div class="card-title"><div class="card-dot"></div>Live Config</div>
      <span class="card-chev" id="m-cfg-chev">▼</span>
    </div>
    <div class="card-body collapsed" id="m-cfg-body">
      <div class="form-row">
        <div class="form-group">
          <label>Pair</label>
          <select id="m-pair">
            <option value="EURUSD">EUR/USD</option>
            <option value="XAUUSD">XAU/USD</option>
            <option value="BOTH">BEIDE</option>
          </select>
        </div>
        <div class="form-group">
          <label>Timeframe</label>
          <select id="m-tf">
            <option value="15M">15 min</option>
            <option value="1H" selected>1 h</option>
            <option value="4H">4 h</option>
            <option value="15M+1H">15M + 1H</option>
            <option value="1H+4H">1H + 4H</option>
            <option value="ALL">15M + 1H + 4H</option>
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Kapitaal (€)</label>
          <input type="number" id="m-capital" value="10000" inputmode="decimal" min="100">
        </div>
        <div class="form-group">
          <label>Min Bias Score (1-5)</label>
          <input type="number" id="m-score" value="2" min="1" max="5" inputmode="numeric">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Lot EUR (micro)</label>
          <input type="number" id="m-lot-eur" value="1" min="1" inputmode="numeric">
        </div>
        <div class="form-group">
          <label>Lot XAU (micro)</label>
          <input type="number" id="m-lot-xau" value="1" min="1" inputmode="numeric">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Spread (pips)</label>
          <input type="number" id="m-spread" value="1.5" min="0" step="0.1" inputmode="decimal">
        </div>
        <div class="form-group">
          <label>Slippage (pips)</label>
          <input type="number" id="m-slip" value="0.5" min="0" step="0.1" inputmode="decimal">
        </div>
      </div>

      <div class="toggle-section-title">Strategy Filters</div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">FVG</div>
          <div class="toggle-sub">Altijd vereist — trigger signaal</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-fvg" checked disabled><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Order Block (OB)</div>
          <div class="toggle-sub">OB in dezelfde richting als FVG</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-ob" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Trend filter</div>
          <div class="toggle-sub">Allen mee met HH/HL of LH/LL</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-trend"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Equilibrium filter</div>
          <div class="toggle-sub">FVG aan goede kant from EQ</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-eq" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Killzone filter</div>
          <div class="toggle-sub">Allen London KZ en NY KZ</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-kz"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Liquidity Sweep</div>
          <div class="toggle-sub">FVG na stop-run from swing high/low</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-sweep"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">HTF Bias filter</div>
          <div class="toggle-sub">15M→1H, 1H→4H steun vereist</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-htf"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">SMT Divergence (DXY)</div>
          <div class="toggle-sub">DXY divergentie vereist</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-smt"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Skip Asian Session</div>
          <div class="toggle-sub">None trades 00:00-08:00 Brussel</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-asian"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Skip Bank Holidays</div>
          <div class="toggle-sub">No trades op US/UK/EU holidays</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-skip-holidays" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row special">
        <div class="toggle-info">
          <div class="toggle-name glow"> Require HTF Order Flow (J3)</div>
          <div class="toggle-sub">BOS richting MOET kloppen</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-req-htf"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name glow"> Require Draw on Liquidity (J2)</div>
          <div class="toggle-sub">DOL richting MOET kloppen</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-req-dol"><span class="slider"></span></label>
      </div>

      <div class="toggle-section-title">Auto SL/TP (ICT)</div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Automatice SL/TP</div>
          <div class="toggle-sub">Swing-based SL + RR multiple TP</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-auto-sltp"><span class="slider"></span></label>
      </div>
      <div class="form-group" style="margin-top:10px">
        <label>Risk:Reward Ratio</label>
        <input type="number" id="m-rr" value="2" min="0.5" max="10" step="0.1" inputmode="decimal">
      </div>
      <div style="font-size:10px;color:var(--text3);line-height:1.4;padding:8px;background:rgba(34,211,238,.05);border-radius:6px;margin-top:6px">
        INFO EUR buffers: 3/5/20p. XAU: 30/50/150p. SL/TP behindaf nog manual aanpasbaar.
      </div>

      <div class="toggle-section-title" style="margin-top:14px">Discord Charts</div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Chart screenshots on entry/exit</div>
          <div class="toggle-sub">Auto-skipt bij krap RAM</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-chart-screenshots"><span class="slider"></span></label>
      </div>

      <div class="toggle-section-title" style="margin-top:14px;color:#f59e0b">MT5 Demo Execution</div>
      <div class="toggle-row" style="border:1px solid rgba(245,158,11,.4);padding:10px;border-radius:8px;background:rgba(245,158,11,.05);margin-bottom:8px">
        <div class="toggle-info">
          <div class="toggle-name">Send orders to MT5 broker</div>
          <div class="toggle-sub">Real orders via EA bridge</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-mt5-execution"><span class="slider"></span></label>
      </div>
      <div id="m-mt5-status-box" style="font-size:10px;line-height:1.5;padding:8px;background:rgba(15,23,42,.5);border-radius:6px;margin-bottom:14px;font-family:JetBrains Mono,monospace">
        <div style="color:var(--text3)">Loading MT5 status...</div>
      </div>

      <div class="toggle-section-title">Riskbeheer</div>
      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-name">Riskbeheer aan</div>
          <div class="toggle-sub">Max verlies/trades/% controleren</div>
        </div>
        <label class="switch"><input type="checkbox" id="m-risk-toggle" checked><span class="slider"></span></label>
      </div>
      <div class="form-row" style="margin-top:10px">
        <div class="form-group">
          <label>Max verlies (€)</label>
          <input type="number" id="m-max-loss" value="0" min="0" inputmode="decimal">
        </div>
        <div class="form-group">
          <label>Max open trades</label>
          <input type="number" id="m-max-trades" value="0" min="0" inputmode="numeric">
        </div>
      </div>
      <div class="form-group">
        <label>Max risico per trade (%)</label>
        <input type="number" id="m-risk-pct" value="0" min="0" step="0.1" inputmode="decimal">
      </div>
      <div class="form-group">
        <label>Max risico per trade (€)</label>
        <input type="number" id="m-max-risk-eur" value="0" min="0" step="5" inputmode="decimal" placeholder="0 = uit">
      </div>
      <div class="form-group">
        <label>Hard close at P&amp;L (€)</label>
        <input type="number" id="m-hard-close-eur" value="0" min="0" step="5" inputmode="decimal" placeholder="0 = uit | breekt RR!">
      </div>

      <div class="toggle-section-title">Discord Notificaties</div>
      <div class="form-group">
        <label>Webhook URL</label>
        <input type="text" id="m-discord" placeholder="https://discord.com/api/webhooks/...">
      </div>
    </div>
  </div>

  <!-- PRESETS (collapsed) -->
  <div class="card">
    <div class="card-header" onclick="mToggle('m-presets-body','m-presets-chev')">
      <div class="card-title"><div class="card-dot"></div>Config Presets</div>
      <span class="card-chev" id="m-presets-chev">▼</span>
    </div>
    <div class="card-body collapsed" id="m-presets-body">
      <div class="form-row" style="margin-bottom:10px">
        <input type="text" id="m-preset-name" placeholder="Preset name..." maxlength="30">
        <button class="btn btn-primary" style="margin:0;padding:11px" onclick="mSavePreset()">SAVE Save</button>
      </div>
      <div id="m-presets-list">
        <div class="no-data">Loading...</div>
      </div>
    </div>
  </div>

  <!-- ENGINE LOG (collapsible, collapsed) -->
  <div class="card">
    <div class="card-header" onclick="mToggle('m-enginelog-body','m-enginelog-chev')">
      <div class="card-title"><div class="card-dot"></div>Engine Log</div>
      <span class="card-chev" id="m-enginelog-chev">▼</span>
    </div>
    <div class="card-body collapsed" id="m-enginelog-body">
      <div class="log-list" id="m-engine-log">Engine not started...</div>
    </div>
  </div>

</div>

<!-- ============ BACKTEST PAGE ============ -->
<div class="page" id="page-backtest">

  <div class="card">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Backtest Config</div>
    </div>
    <div class="card-body">
      <div class="form-row">
        <div class="form-group">
          <label>Pair</label>
          <select id="bt-m-pair">
            <option value="EURUSD" selected>EUR/USD</option>
            <option value="XAUUSD">XAU/USD</option>
            <option value="BOTH">BEIDE</option>
          </select>
        </div>
        <div class="form-group">
          <label>Timeframe</label>
          <select id="bt-m-tf">
            <option value="15M">15M</option>
            <option value="1H" selected>1H</option>
            <option value="4H">4H</option>
            <option value="15M+1H">15M + 1H</option>
            <option value="1H+4H">1H + 4H</option>
            <option value="ALL">15M + 1H + 4H</option>
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Start datum</label>
          <input type="date" id="bt-m-start">
        </div>
        <div class="form-group">
          <label>Eind datum</label>
          <input type="date" id="bt-m-end">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Kapitaal (€)</label>
          <input type="number" id="bt-m-cap" value="10000" inputmode="decimal">
        </div>
        <div class="form-group">
          <label>Risk:Reward</label>
          <input type="number" id="bt-m-rr" value="2" min="0.5" step="0.5" inputmode="decimal">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Lot EUR (micro)</label>
          <input type="number" id="bt-m-lot-eur" value="1" min="1" inputmode="numeric">
        </div>
        <div class="form-group">
          <label>Lot XAU (micro)</label>
          <input type="number" id="bt-m-lot-xau" value="1" min="1" inputmode="numeric">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Spread EUR (pips)</label>
          <input type="number" id="bt-m-spread" value="1.5" min="0" step="0.1" inputmode="decimal">
        </div>
        <div class="form-group">
          <label>Slip EUR (pips)</label>
          <input type="number" id="bt-m-slip" value="0.5" min="0" step="0.1" inputmode="decimal">
        </div>
      </div>
      <div id="bt-m-xau-row" style="display:none">
        <div class="form-row">
          <div class="form-group">
            <label>Spread XAU (pips)</label>
            <input type="number" id="bt-m-spread-xau" value="35" min="0" step="0.5" inputmode="decimal">
          </div>
          <div class="form-group">
            <label>Slip XAU (pips)</label>
            <input type="number" id="bt-m-slip-xau" value="5" min="0" step="0.5" inputmode="decimal">
          </div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Min Bias Score (1-5)</label>
          <input type="number" id="bt-m-score" value="2" min="1" max="5" inputmode="numeric">
        </div>
        <div class="form-group">
          <label>BE trigger (pips, 0=uit)</label>
          <input type="number" id="bt-m-be" value="0" min="0" step="1" inputmode="numeric">
        </div>
      </div>

      <div class="toggle-section-title">Strategy Filters</div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Order Block</div><div class="toggle-sub">OB confluentie</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-ob" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Trend filter</div><div class="toggle-sub">HH/HL of LH/LL</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-trend"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Equilibrium</div><div class="toggle-sub">P/D zone correct</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-eq" checked><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Killzone</div><div class="toggle-sub">London + NY KZ</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-kz"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Liquidity Sweep</div><div class="toggle-sub">FVG na stop-run</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-sweep"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">HTF Bias</div><div class="toggle-sub">15M→1H, 1H→4H</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-htf"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">SMT (DXY)</div><div class="toggle-sub">DXY divergentie</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-smt"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name">Skip Asian</div><div class="toggle-sub">00:00-08:00 Brussel uit</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-asian"><span class="slider"></span></label>
      </div>
      <div class="toggle-row special">
        <div class="toggle-info"><div class="toggle-name glow"> Require HTF Order Flow</div><div class="toggle-sub">J3 moet kloppen</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-req-htf"><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><div class="toggle-name glow"> Require Draw on Liquidity</div><div class="toggle-sub">J2 moet kloppen</div></div>
        <label class="switch"><input type="checkbox" id="bt-m-req-dol"><span class="slider"></span></label>
      </div>

      <div class="toggle-section-title">Riskbeheer</div>
      <div class="form-row">
        <div class="form-group">
          <label>Max dayverlies (€)</label>
          <input type="number" id="bt-m-max-loss" value="0" min="0" inputmode="decimal">
        </div>
        <div class="form-group">
          <label>Max open trades</label>
          <input type="number" id="bt-m-max-trades" value="0" min="0" inputmode="numeric">
        </div>
      </div>
      <div class="form-group">
        <label>Max risico per trade (%)</label>
        <input type="number" id="bt-m-risk-pct" value="0" min="0" step="0.1" inputmode="decimal">
      </div>
      <div class="form-group">
        <label>Max risico per trade (€)</label>
        <input type="number" id="bt-m-max-risk-eur" value="0" min="0" step="5" inputmode="decimal" placeholder="0 = uit">
      </div>
      <div class="form-group">
        <label>Hard close at P&amp;L (€)</label>
        <input type="number" id="bt-m-hard-close-eur" value="0" min="0" step="5" inputmode="decimal" placeholder="0 = uit | breekt RR!">
      </div>

      <button class="btn btn-primary" id="bt-m-run-btn" onclick="mRunBacktest()" style="margin-top:14px">RUN BACKTEST</button>
    </div>
  </div>

  <!-- BACKTEST RESULTS -->
  <div class="card" id="bt-m-result-card" style="display:none">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Resultaat</div>
    </div>
    <div class="card-body">
      <div class="bt-stat-grid">
        <div class="stat-box">
          <div class="stat-label">Trades</div>
          <div class="stat-val" id="bt-m-r-trades">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Winrate</div>
          <div class="stat-val" id="bt-m-r-wr">0%</div>
          <div class="stat-sub" id="bt-m-r-wl">0W / 0L</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Total P&L</div>
          <div class="stat-val" id="bt-m-r-pnl">€0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Max DD</div>
          <div class="stat-val" id="bt-m-r-dd">€0</div>
        </div>
      </div>
      <div class="card-header" onclick="mToggle('bt-m-trades-body','bt-m-trades-chev')" style="margin:0 -14px;padding:10px 14px">
        <div class="card-title" style="font-size:11px">All trades</div>
        <span class="card-chev" id="bt-m-trades-chev">▼</span>
      </div>
      <div class="card-body collapsed" id="bt-m-trades-body" style="padding:10px 0 0">
        <div id="bt-m-trades-list"></div>
      </div>
    </div>
  </div>

</div>

<!-- ============ SYSTEM PAGE ============ -->
<div class="page" id="page-system">

  <div class="card">
    <div class="card-header no-toggle">
      <div class="card-title"><div class="card-dot"></div>Systeem Status</div>
      <button onclick="mLoadHealth()" style="padding:6px 12px;border:1px solid var(--border2);border-radius:6px;background:rgba(34,211,238,.1);color:var(--glow2);font-size:11px;cursor:pointer">refresh Ververs</button>
    </div>
    <div class="card-body" id="m-health-body">
      <div class="no-data">Loading...</div>
    </div>
  </div>

</div>

<script>
let mRunning = false;
let mPaused = false;
let mPollTimer = null;
let mCountdownTimer = null;

// ─── TAB SWITCHING ─────────────────────────────────────────────
function mSwitchTab(name, evt){
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  if(evt && evt.target) evt.target.classList.add("active");
  else {
    // Find tab by matching onclick attr
    const tabs = document.querySelectorAll(".tab");
    const idx = {live:0, backtest:1, system:2}[name];
    if(tabs[idx]) tabs[idx].classList.add("active");
  }
  document.getElementById("page-"+name).classList.add("active");
  if(name === "system") mLoadHealth();
  if(name === "backtest") mSetDefaultDates();
  window.scrollTo({top:0, behavior:"smooth"});
}

function mToggle(bodyId, chevId){
  const body = document.getElementById(bodyId);
  const chev = document.getElementById(chevId);
  body.classList.toggle("collapsed");
  if(chev) chev.classList.toggle("open");
}

// ─── ENGINE STATUS POLLING ─────────────────────────────────────
async function mPoll(){
  try {
    const r = await fetch("/api/engine/status");
    const ed = await r.json();
    mRunning = !!ed.running;
    mPaused  = !!ed.paused;

    // Market status
    const mkt = document.getElementById("m-market");
    if(ed.is_weekend){
      mkt.textContent = "RED Market: closed"; mkt.className = "badge closed";
    } else {
      mkt.textContent = "GREEN Market: open"; mkt.className = "badge open";
    }

    // Engine badge
    const eng = document.getElementById("m-engine");
    if(ed.stopped_by_risk){
      eng.textContent = "STOP Risk stop"; eng.className = "badge closed";
      document.getElementById("m-risk-banner").style.display = "block";
    } else {
      document.getElementById("m-risk-banner").style.display = "none";
      if(mRunning && mPaused){
        eng.textContent = "|| Paused"; eng.className = "badge warn";
      } else if(mRunning){
        eng.textContent = "● Active"; eng.className = "badge open";
      } else {
        eng.textContent = "● Stopped"; eng.className = "badge";
      }
    }

    // Uptime
    document.getElementById("m-uptime").textContent = (mRunning && ed.uptime) ? `timer ${ed.uptime}` : "timer —";

    // Last scan + countdown
    document.getElementById("m-lastscan").textContent = ed.last_scan || "—";
    const cd = document.getElementById("m-countdown");
    if(mRunning && !mPaused){
      if(mCountdownTimer) clearInterval(mCountdownTimer);
      let s = 20;
      cd.textContent = `wait ${s}s`;
      mCountdownTimer = setInterval(()=>{
        s--; if(s<=0){ clearInterval(mCountdownTimer); cd.textContent="scant..."; }
        else cd.textContent=`wait ${s}s`;
      }, 1000);
    } else {
      if(mCountdownTimer) clearInterval(mCountdownTimer);
      cd.textContent = mPaused ? "|| pauze" : "—";
    }

    // Buttons
    const execBtn = document.getElementById("m-exec-btn");
    const stopBtn = document.getElementById("m-stop-btn");
    execBtn.disabled = mRunning;
    execBtn.style.opacity = mRunning ? ".4" : "1";
    stopBtn.disabled = !mRunning;
    stopBtn.style.opacity = mRunning ? "1" : ".4";

    const pauseBtn = document.getElementById("m-pause-btn");
    if(!mRunning){
      pauseBtn.style.display = "none";
    } else {
      pauseBtn.style.display = "";
      if(mPaused){
        pauseBtn.textContent = "> Resume";
        pauseBtn.className = "btn btn-resume";
      } else {
        pauseBtn.textContent = "PAUSE";
        pauseBtn.className = "btn btn-pause";
      }
    }

    // Stats
    const s = ed.stats || {};
    document.getElementById("m-trades").textContent = s.total || 0;
    document.getElementById("m-winrate").textContent = s.total > 0 ? s.winrate + "%" : "—";
    document.getElementById("m-wl").textContent = `${s.wins||0}W / ${s.losses||0}L`;
    document.getElementById("m-scans").textContent = ed.scan_count || 0;
    const pnl = s.total_pnl || 0;
    const pnlEl = document.getElementById("m-pnl");
    pnlEl.textContent = `${pnl>=0?"+":""}€${pnl.toFixed(2)}`;
    pnlEl.className = "stat-val " + (pnl >= 0 ? "green" : "red");
    document.getElementById("m-daily-pnl").textContent = `Daily: €${(ed.daily_pnl||0).toFixed(2)}`;

    // Live price (uit eerste open trade of bias panel)
    if(ed.open_trades && ed.open_trades.length){
      document.getElementById("m-price").textContent = ed.open_trades[0].live_price || "—";
    }

    // Open trades — bewaar input focus
    mRenderOpenTrades(ed.open_trades || []);
    document.getElementById("m-open-count").textContent = `${(ed.open_trades||[]).length} open`;

    // Trade log
    mRenderTradeLog(ed.closed_trades || []);

    // Engine log
    mRenderEngineLog(ed.logs || []);

  } catch(e) { /* stil falen */ }

  // Data source
  try {
    const dsr = await fetch("/api/datasource");
    const dsd = await dsr.json();
    const dso = dsd.overall || {};
    const dsEl = document.getElementById("m-datasource");
    dsEl.textContent = `● Data: ${dso.label || "—"}`;
    dsEl.style.color = dso.color || "var(--text3)";
  } catch(e) {}
}

// ─── OPEN TRADES RENDER (focus-safe) ───────────────────────────
function mRenderOpenTrades(trades){
  const body = document.getElementById("m-open-body");
  // Skip rerender als gebruiker aan het typen is in een SL/TP input
  const ae = document.activeElement;
  const typing = ae && ae.tagName === "INPUT" && (ae.id||"").match(/^m-(sl|tp)-/);

  if(typing){
    // Update allen live prijs + P&L per trade
    trades.forEach(t => {
      const card = body.querySelector(`[data-tid="${t.id}"]`);
      if(!card) return;
      const pricesEl = card.querySelector(".trade-prices");
      const pnlEl    = card.querySelector(".trade-pnl");
      if(pricesEl) pricesEl.textContent = `Entry: ${t.entry_price} → Live: ${t.live_price||"—"}`;
      if(pnlEl){
        const pnl = t.pnl_eur||0;
        pnlEl.textContent = `${pnl>=0?"+":""}€${pnl.toFixed(2)}`;
        pnlEl.className = "trade-pnl " + (pnl>=0 ? "win" : "loss");
      }
    });
    return;
  }

  if(!trades.length){
    body.innerHTML = '<div class="no-data">No open positions</div>';
    return;
  }
  body.innerHTML = trades.map(t => {
    const pnl = t.pnl_eur||0;
    const slV = t.sl || "";
    const tpV = t.tp || "";
    return `<div class="trade-card" data-tid="${t.id}">
      <div class="trade-head">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="pill ${t.direction==="LONG"?"pill-long":"pill-short"}">${t.direction}</span>
          <span style="font-size:13px;font-weight:700">${t.pair} #${t.id}</span>
        </div>
        <div class="trade-pnl ${pnl>=0?"win":"loss"}">${pnl>=0?"+":""}€${pnl.toFixed(2)}</div>
      </div>
      <div class="trade-prices">Entry: ${t.entry_price} → Live: ${t.live_price||"—"}</div>
      <div class="sl-tp-row">
        <div class="sl-tp-label" style="color:${t.sl?"var(--red)":"var(--text3)"}">STOP LOSS ${t.sl?"OK":""}</div>
        <div class="sl-tp-inp-wrap">
          <input id="m-sl-${t.id}" type="number" step="0.00001" value="${slV}" placeholder="${t.sl||"None SL"}" inputmode="decimal">
          <button class="sl-btn" onclick="mSetSlTp(${t.id},'sl')">SL</button>
        </div>
      </div>
      <div class="sl-tp-row">
        <div class="sl-tp-label" style="color:${t.tp?"var(--green)":"var(--text3)"}">TAKE PROFIT ${t.tp?"OK":""}</div>
        <div class="sl-tp-inp-wrap">
          <input id="m-tp-${t.id}" type="number" step="0.00001" value="${tpV}" placeholder="${t.tp||"None TP"}" inputmode="decimal">
          <button class="tp-btn" onclick="mSetSlTp(${t.id},'tp')">TP</button>
        </div>
      </div>
      <button onclick="mCloseTrade(${t.id},'${t.pair}',${t.entry_price})" style="width:100%;padding:10px;margin-top:4px;border-radius:8px;border:1px solid rgba(248,113,113,.4);background:rgba(248,113,113,.08);color:var(--red);font-size:12px;font-weight:700;cursor:pointer">
        Close trade
      </button>
    </div>`;
  }).join("");
}

function mRenderTradeLog(trades){
  const el = document.getElementById("m-trade-log");
  const recent = trades.slice(-10).reverse();
  if(!recent.length){ el.innerHTML = '<div class="no-data">No trades yet</div>'; return; }
  el.innerHTML = recent.map(t => {
    const pnl = t.pnl_eur||0;
    return `<div onclick="mOpenTradeModal(${t.id})" style="padding:8px 0;border-bottom:1px solid rgba(31,58,94,.2);display:flex;align-items:center;gap:8px;cursor:pointer">
      <span class="pill ${t.direction==="LONG"?"pill-long":"pill-short"}">${t.direction}</span>
      <div style="flex:1">
        <div style="font-size:12px;font-weight:600">${t.pair} <span style="font-size:9px;color:var(--text3)">#${t.id}</span></div>
        <div style="font-size:9px;color:var(--text3)">${(t.closed_at||"").slice(5,16)}</div>
      </div>
      <div style="text-align:right">
        <div class="${pnl>=0?"win":"loss"}" style="font-size:13px;font-weight:700">${pnl>=0?"+":""}€${pnl.toFixed(2)}</div>
        <div style="font-size:9px;color:var(--text3)">${t.pips||0}p</div>
      </div>
    </div>`;
  }).join("");
}

function mRenderEngineLog(logs){
  const el = document.getElementById("m-engine-log");
  const recent = logs.slice(-30).reverse();
  if(!recent.length){ el.innerHTML = '<div class="no-data">Engine not started...</div>'; return; }
  el.innerHTML = recent.map(l =>
    `<div class="log-line"><span class="log-time">${(l.time||"").slice(11,19)}</span>${l.msg||l.message||""}</div>`
  ).join("");
}

// ─── ENGINE CONTROL ────────────────────────────────────────────
function mBuildConfig(){
  return {
    pair:       document.getElementById("m-pair").value,
    tf:         document.getElementById("m-tf").value,
    capital:    document.getElementById("m-capital").value,
    lotsize_eur:parseFloat(document.getElementById("m-lot-eur").value)||1,
    lotsize_xau:parseFloat(document.getElementById("m-lot-xau").value)||1,
    lotsize:    parseFloat(document.getElementById("m-lot-eur").value)||1,
    min_score:  parseInt(document.getElementById("m-score").value)||2,
    spread_pips:parseFloat(document.getElementById("m-spread").value)||0,
    slippage_pips:parseFloat(document.getElementById("m-slip").value)||0,
    use_ob:     document.getElementById("m-ob").checked,
    use_trend:  document.getElementById("m-trend").checked,
    use_eq:     document.getElementById("m-eq").checked,
    use_session:document.getElementById("m-kz").checked,
    use_sweep:  document.getElementById("m-sweep").checked,
    use_htf_bias:document.getElementById("m-htf").checked,
    use_smt:    document.getElementById("m-smt").checked,
    skip_asian: document.getElementById("m-asian").checked,
    skip_holidays: document.getElementById("m-skip-holidays").checked,
    require_htf_orderflow: document.getElementById("m-req-htf").checked,
    require_dol:           document.getElementById("m-req-dol").checked,
    auto_sltp:             document.getElementById("m-auto-sltp").checked,
    send_chart_screenshots: document.getElementById("m-chart-screenshots").checked,
    mt5_execution_enabled:  document.getElementById("m-mt5-execution").checked,
    rr:                    parseFloat(document.getElementById("m-rr").value)||2,
    be_trigger: 0,
    trade_both: document.getElementById("m-pair").value === "BOTH",
    max_daily_loss: document.getElementById("m-risk-toggle").checked ? (parseFloat(document.getElementById("m-max-loss").value)||0) : 0,
    max_trades:     document.getElementById("m-risk-toggle").checked ? (parseInt(document.getElementById("m-max-trades").value)||0) : 0,
    max_risk_pct:   document.getElementById("m-risk-toggle").checked ? (parseFloat(document.getElementById("m-risk-pct").value)||0) : 0,
    max_risk_eur:       document.getElementById("m-risk-toggle").checked ? (parseFloat(document.getElementById("m-max-risk-eur").value)||0) : 0,
    hard_close_pnl_eur: document.getElementById("m-risk-toggle").checked ? (parseFloat(document.getElementById("m-hard-close-eur").value)||0) : 0,
    discord_webhook: document.getElementById("m-discord").value.trim(),
  };
}

async function mExecute(){
  if(mRunning){ alert("Engine draait al"); return; }
  const cfg = mBuildConfig();
  const r = await fetch("/api/engine/start", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(cfg)});
  const d = await r.json();
  if(!d.ok) alert(d.error || "Starten mislukt");
  mPoll();
}

async function mShutdown(){
  if(!confirm("Engine echt stoppen?")) return;
  await fetch("/api/engine/stop", {method:"POST"});
  mPoll();
}

async function mTogglePause(){
  const url = mPaused ? "/api/engine/resume" : "/api/engine/pause";
  await fetch(url, {method:"POST"});
  mPoll();
}

async function mCloseAll(){
  if(!confirm("ALLE open trades sluiten op huidige marktprijs?")) return;
  const r = await fetch("/api/engine/close_all", {method:"POST"});
  const d = await r.json();
  alert(`${d.closed||0} trades closed`);
  mPoll();
}

async function mSetSlTp(id, type){
  const inp = document.getElementById(`m-${type}-${id}`);
  if(!inp) return;
  const val = parseFloat(inp.value);
  if(!val || val <= 0){ alert(`Vul een valid ${type.toUpperCase()} niveau in.`); return; }
  const body = {id};
  body[type] = val;
  const r = await fetch("/api/engine/set_sl_tp", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await r.json();
  if(!d.ok){ alert(d.error || "Mislukt"); return; }
  inp.blur();
  setTimeout(mPoll, 300);
}

async function mCloseTrade(id, pair, entry){
  if(!confirm(`Trade #${id} sluiten?\n${pair} @ ${entry}`)) return;
  const r = await fetch("/api/engine/close_trade", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})});
  const d = await r.json();
  if(!d.ok) alert(d.error || "Close mislukt");
  mPoll();
}

// ─── PRESETS ───────────────────────────────────────────────────
async function mLoadPresets(){
  const r = await fetch("/api/presets");
  const presets = await r.json();
  const el = document.getElementById("m-presets-list");
  const names = Object.keys(presets||{});
  if(!names.length){
    el.innerHTML = '<div class="no-data">Nog none presets saved</div>';
    return;
  }
  el.innerHTML = names.map(n => {
    const c = presets[n].config || {};
    const reqBadges = [];
    if(c.require_htf_orderflow) reqBadges.push("ReqJ3");
    if(c.require_dol)           reqBadges.push("ReqJ2");
    const reqStr = reqBadges.length ? ` | ${reqBadges.join(",")}` : "";
    return `<div class="preset-item">
      <div class="preset-name">${n}</div>
      <div class="preset-info">${c.pair||"?"} ${c.tf||"?"} | Score>=${c.min_score||"?"}${reqStr}</div>
      <div class="preset-btns">
        <button class="preset-start" onclick="mStartPreset('${n}')">Start with this preset</button>
        <button class="preset-del" onclick="mDeletePreset('${n}')">X Wis</button>
      </div>
    </div>`;
  }).join("");
}

async function mSavePreset(){
  const name = document.getElementById("m-preset-name").value.trim();
  if(!name){ alert("Geef de preset een naam"); return; }
  const cfg = mBuildConfig();
  const r = await fetch("/api/presets/save", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name, config:cfg})});
  const d = await r.json();
  if(d.ok){
    document.getElementById("m-preset-name").value = "";
    mLoadPresets();
  } else alert(d.error || "Save mislukt");
}

async function mStartPreset(name){
  if(mRunning){ alert("Engine draait al — stop eerst"); return; }
  if(!confirm(`Engine starten met preset "${name}"?`)) return;
  const r = await fetch("/api/presets/start", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
  const d = await r.json();
  if(!d.ok) alert(d.error || "Starten mislukt");
  else mPoll();
}

async function mDeletePreset(name){
  if(!confirm(`Preset "${name}" verwijderen?`)) return;
  await fetch("/api/presets/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
  mLoadPresets();
}

// ─── BACKTESTER ────────────────────────────────────────────────
function mSetDefaultDates(){
  const end = document.getElementById("bt-m-end");
  const start = document.getElementById("bt-m-start");
  if(!end.value){
    const today = new Date();
    end.value = today.toISOString().slice(0,10);
  }
  if(!start.value){
    const past = new Date(); past.setMonth(past.getMonth() - 3);
    start.value = past.toISOString().slice(0,10);
  }
}

// Show/hide XAU spread/slip row als pair = BOTH
document.addEventListener("change", e => {
  if(e.target.id === "bt-m-pair"){
    document.getElementById("bt-m-xau-row").style.display = e.target.value === "BOTH" ? "" : "none";
  }
});

async function mRunBacktest(){
  const btn = document.getElementById("bt-m-run-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Bezig...';
  const isBoth = document.getElementById("bt-m-pair").value === "BOTH";
  const body = {
    pair:        document.getElementById("bt-m-pair").value,
    tf:          document.getElementById("bt-m-tf").value,
    start:       document.getElementById("bt-m-start").value,
    end:         document.getElementById("bt-m-end").value,
    capital:     document.getElementById("bt-m-cap").value,
    rr:          parseFloat(document.getElementById("bt-m-rr").value)||2,
    lotsize:     parseFloat(document.getElementById("bt-m-lot-eur").value)||1,
    lotsize_eur: parseFloat(document.getElementById("bt-m-lot-eur").value)||1,
    lotsize_xau: parseFloat(document.getElementById("bt-m-lot-xau").value)||1,
    spread_pips: parseFloat(document.getElementById("bt-m-spread").value)||1.5,
    slippage_pips: parseFloat(document.getElementById("bt-m-slip").value)||0.5,
    spread_pips_xau:   isBoth ? parseFloat(document.getElementById("bt-m-spread-xau").value)||35 : null,
    slippage_pips_xau: isBoth ? parseFloat(document.getElementById("bt-m-slip-xau").value)||5 : null,
    min_score:   parseInt(document.getElementById("bt-m-score").value)||2,
    be_trigger:  parseFloat(document.getElementById("bt-m-be").value)||0,
    use_ob:      document.getElementById("bt-m-ob").checked,
    use_trend:   document.getElementById("bt-m-trend").checked,
    use_eq:      document.getElementById("bt-m-eq").checked,
    use_session: document.getElementById("bt-m-kz").checked,
    use_sweep:   document.getElementById("bt-m-sweep").checked,
    use_htf_bias:document.getElementById("bt-m-htf").checked,
    use_smt:     document.getElementById("bt-m-smt").checked,
    skip_asian:  document.getElementById("bt-m-asian").checked,
    require_htf_orderflow: document.getElementById("bt-m-req-htf").checked,
    require_dol:           document.getElementById("bt-m-req-dol").checked,
    max_daily_loss: parseFloat(document.getElementById("bt-m-max-loss").value)||0,
    max_trades:     parseInt(document.getElementById("bt-m-max-trades").value)||0,
    max_risk_pct:   parseFloat(document.getElementById("bt-m-risk-pct").value)||0,
    max_risk_eur:       parseFloat(document.getElementById("bt-m-max-risk-eur").value)||0,
    hard_close_pnl_eur: parseFloat(document.getElementById("bt-m-hard-close-eur").value)||0,
  };
  try {
    const r = await fetch("/api/backtest", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const d = await r.json();
    if(d.error){ alert(d.error); return; }
    mShowBacktestResult(d);
  } catch(e) {
    alert("Backtest error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = "RUN BACKTEST";
  }
}

function mShowBacktestResult(d){
  document.getElementById("bt-m-result-card").style.display = "";
  const s = d.stats || {};
  document.getElementById("bt-m-r-trades").textContent = s.total || 0;
  document.getElementById("bt-m-r-wr").textContent = s.total > 0 ? s.winrate + "%" : "—";
  document.getElementById("bt-m-r-wl").textContent = `${s.wins||0}W / ${s.losses||0}L`;
  const pnl = s.total_pnl || 0;
  const pnlEl = document.getElementById("bt-m-r-pnl");
  pnlEl.textContent = `${pnl>=0?"+":""}€${pnl.toFixed(2)}`;
  pnlEl.className = "stat-val " + (pnl>=0 ? "green" : "red");
  document.getElementById("bt-m-r-dd").textContent = `€${(s.max_drawdown||0).toFixed(2)}`;
  document.getElementById("bt-m-r-dd").className = "stat-val red";

  // Trade list
  const trades = d.trades || [];
  const list = document.getElementById("bt-m-trades-list");
  if(!trades.length){
    list.innerHTML = '<div class="no-data">None trades</div>';
  } else {
    list.innerHTML = trades.slice(0, 100).map(t => {
      const pnl_t = t.pnl_eur || 0;
      return `<div style="padding:8px 0;border-bottom:1px solid rgba(31,58,94,.2);display:flex;align-items:center;gap:8px">
        <span class="pill ${t.direction==="LONG"?"pill-long":"pill-short"}">${t.direction}</span>
        <div style="flex:1;font-size:10px;color:var(--text3)">${(t.opened_at||"").slice(5,16)}</div>
        <div class="${pnl_t>=0?"win":"loss"}" style="font-size:12px;font-weight:700">${pnl_t>=0?"+":""}€${pnl_t.toFixed(2)}</div>
      </div>`;
    }).join("");
  }
  document.getElementById("bt-m-result-card").scrollIntoView({behavior:"smooth"});
}

// ─── SYSTEM HEALTH ─────────────────────────────────────────────
async function mLoadHealth(){
  const body = document.getElementById("m-health-body");
  body.innerHTML = '<div class="no-data"><span class="spinner"></span> Loading...</div>';
  try {
    const r = await fetch("/api/system/health");
    const d = await r.json();
    // d is een dict met named keys (tradingview, yfinance, etc.)
    const labels = {
      tradingview: "TradingView WebSocket",
      yfinance:    "yFinance Fallback",
      data_quality:"Data Kwaliteit",
      discord:     "Discord Webhook",
      engine:      "Engine Status",
      ram:         "RAM Memory",
      cpu:         "CPU Belasting",
      uptime:      "VPS Uptime",
      python:      "Python Versie",
      market:      "Market Status",
    };
    const keys = Object.keys(d);
    if(!keys.length){
      body.innerHTML = '<div class="no-data">None checks beschikbaar</div>';
      return;
    }
    body.innerHTML = keys.map(k => {
      const c = d[k];
      let cls = "health-warn";
      if(c.status === "ok")    cls = "health-ok";
      if(c.status === "error" || c.status === "fail") cls = "health-fail";
      if(c.status === "warn")  cls = "health-warn";
      const statusLabel = c.status === "ok" ? "OK" : (c.status === "error" || c.status === "fail" ? "X ERROR" : "! WARN");
      return `<div class="health-item">
        <div style="flex:1;min-width:0">
          <div class="health-name">${labels[k] || k}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:2px;word-break:break-word">${c.msg || c.detail || ""}</div>
        </div>
        <span class="health-status ${cls}">${statusLabel}</span>
      </div>`;
    }).join("");
  } catch(e) {
    body.innerHTML = `<div class="no-data">Error: ${e.message}</div>`;
  }
}

// ─── ECONOMIC NEWS (mobile) ────────────────────────────────────────
let _mNewsCurrentTab = "today";
let _mNewsData = {events_today: [], events_tomorrow: [], holiday: {today: null, tomorrow: null}};

async function mLoadNews(){
  try{
    const r = await fetch("/api/news");
    const d = await r.json();
    if(!d.ok){
      document.getElementById("m-news-events").innerHTML = '<div style="text-align:center;padding:14px;color:var(--text3);font-size:11px">News temporarily unavailable</div>';
      return;
    }
    _mNewsData = d;
    mRenderHolidayBanner();
    mRenderNewsEvents();
  } catch(e) {
    document.getElementById("m-news-events").innerHTML = '<div style="text-align:center;padding:14px;color:var(--text3);font-size:11px">Loading error</div>';
  }
}

function mRenderHolidayBanner(){
  const banner = document.getElementById("m-news-holiday");
  const today    = _mNewsData.holiday && _mNewsData.holiday.today;
  const tomorrow = _mNewsData.holiday && _mNewsData.holiday.tomorrow;
  if(today){
    banner.style.display = "block";
    banner.innerHTML = `! <b>${today.title}</b> (${today.country}) today — low liquidity`;
  } else if(tomorrow){
    banner.style.display = "block";
    banner.innerHTML = `! Tomorrow: <b>${tomorrow.title}</b> (${tomorrow.country})`;
  } else {
    banner.style.display = "none";
  }
}

function mNewsTab(tab){
  _mNewsCurrentTab = tab;
  const todayBtn = document.getElementById("m-news-tab-today");
  const tomBtn   = document.getElementById("m-news-tab-tomorrow");
  if(tab === "today"){
    todayBtn.style.color = "var(--glow2)";
    todayBtn.style.borderBottomColor = "var(--glow)";
    tomBtn.style.color = "var(--text3)";
    tomBtn.style.borderBottomColor = "transparent";
  } else {
    tomBtn.style.color = "var(--glow2)";
    tomBtn.style.borderBottomColor = "var(--glow)";
    todayBtn.style.color = "var(--text3)";
    todayBtn.style.borderBottomColor = "transparent";
  }
  mRenderNewsEvents();
}

function mRenderNewsEvents(){
  const events = _mNewsCurrentTab === "today" ? _mNewsData.events_today : _mNewsData.events_tomorrow;
  const el = document.getElementById("m-news-events");
  if(!events || !events.length){
    const lbl = _mNewsCurrentTab === "today" ? "today" : "tomorrow";
    el.innerHTML = `<div style="text-align:center;padding:14px;color:var(--text3);font-size:11px">None events ${lbl}</div>`;
    return;
  }
  el.innerHTML = events.map(e => {
    let impactColor, impactBg, impactLabel;
    if(e.impact === "high"){
      impactColor = "#f87171"; impactBg = "rgba(248,113,113,.12)"; impactLabel = "HIGH";
    } else if(e.impact === "medium"){
      impactColor = "#fbbf24"; impactBg = "rgba(251,191,36,.12)"; impactLabel = "MED";
    } else {
      impactColor = "#a78bfa"; impactBg = "rgba(167,139,250,.12)"; impactLabel = (e.impact||"").toUpperCase().slice(0,3) || "—";
    }
    const curFlag = e.country === "USD" ? "US" : (e.country === "EUR" ? "EU" : "");
    return `<div style="padding:8px 0;border-bottom:1px solid rgba(34,211,238,.08);display:grid;grid-template-columns:48px 22px 1fr auto;gap:8px;align-items:center;font-size:11px">
      <div style="font-family:'JetBrains Mono',monospace;color:var(--glow3);font-weight:600">${e.time}</div>
      <div style="font-size:14px">${curFlag}</div>
      <div style="color:var(--text);overflow:hidden;text-overflow:ellipsis">${e.title}</div>
      <div style="padding:2px 6px;border-radius:4px;background:${impactBg};color:${impactColor};font-size:9px;font-weight:700">${impactLabel}</div>
    </div>`;
  }).join("");
}

// ─── INITIAL LOAD ──────────────────────────────────────────────
mPoll();
mLoadPresets();
mSetDefaultDates();
mLoadNews();
async function mPollMT5Status(){
  try{
    const r = await fetch("/api/mt5/status");
    const d = await r.json();
    const box = document.getElementById("m-mt5-status-box");
    if(!box) return;
    if(!d.available){
      box.innerHTML = '<div style="color:#f87171">⊘ MT5 niet bereikbaar</div>';
      return;
    }
    if(!d.alive){
      box.innerHTML = '<div style="color:#f59e0b">⚠ EA offline</div>';
      return;
    }
    const hb = d.heartbeat;
    const age = Math.round(hb._age_sec);
    const exec = d.execution_enabled ? '<span style="color:#34d399">● LIVE</span>' : '<span style="color:var(--text3)">○ paper</span>';
    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--text3)">EA:</span><span style="color:#34d399">● Connected (${age}s)</span></div>
      <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--text3)">Account:</span><span>${hb.account}</span></div>
      <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--text3)">Balance:</span><span style="color:#7dd3fc">€${hb.balance.toFixed(2)}</span></div>
      <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--text3)">Equity:</span><span style="color:#7dd3fc">€${hb.equity.toFixed(2)}</span></div>
      <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--text3)">Positions:</span><span>${hb.open_positions}</span></div>
      <div style="display:flex;justify-content:space-between"><span style="color:var(--text3)">Mode:</span>${exec}</div>
    `;
  }catch(e){console.error("[mMT5]",e);}
}

mPollTimer = setInterval(mPoll, 5000);
mPollMT5Status();
setInterval(mPollMT5Status, 10000);
setInterval(mLoadNews, 300000);  // refresh news elke 5 min

// ─── MOBILE TRADE MODAL ──────────────────────────────────────────
let _mModalChart = null;
let _mModalSeries = null;

async function mOpenTradeModal(tradeId){
  document.getElementById("m-trade-modal").style.display = "flex";
  document.getElementById("m-modal-title").textContent = `Trade #${tradeId}`;
  document.getElementById("m-modal-details").innerHTML = '<div style="padding:14px;text-align:center;color:var(--text3);font-size:12px">Loading...</div>';
  const chartEl = document.getElementById("m-modal-chart");
  chartEl.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:11px">Loading chart...</div>';

  try {
    const r = await fetch(`/api/trade/${tradeId}/candles`);
    const d = await r.json();
    if(!d.ok){
      document.getElementById("m-modal-title").textContent = `Trade #${tradeId} — Error`;
      chartEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--red);font-size:11px">${d.error||"Failed"}</div>`;
      return;
    }

    const isOpen = d.is_open;
    const w = d.outcome === "win";
    const statusLabel = isOpen ? "OPEN" : (w ? "WIN" : (d.outcome === "be" ? "BE" : "LOSS"));
    document.getElementById("m-modal-title").textContent = `#${tradeId} · ${d.pair} ${d.direction} · ${statusLabel}`;

    const exitDisp = d.exit_price !== null && d.exit_price !== undefined ? d.exit_price : "—";
    const pipsDisp = d.pips !== null && d.pips !== undefined ? ((d.pips>0?"+":"")+d.pips.toFixed(1)) : "—";
    const pnlDisp  = d.pnl_eur !== null && d.pnl_eur !== undefined ? ((d.pnl_eur>=0?"+":"")+"€"+d.pnl_eur.toFixed(2)) : "—";
    const cls = w ? "var(--green)" : (d.outcome === "loss" ? "var(--red)" : "var(--glow3)");
    document.getElementById("m-modal-details").innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;padding:8px 4px;font-size:12px">
        <div><span style="color:var(--text3);font-size:10px">Entry:</span> <span style="color:var(--glow3);font-weight:600">${d.entry_price}</span></div>
        <div><span style="color:var(--text3);font-size:10px">Exit:</span> <span style="color:${cls};font-weight:600">${exitDisp}</span></div>
        <div><span style="color:var(--text3);font-size:10px">SL:</span> <span style="color:var(--red)">${d.sl||"—"}</span></div>
        <div><span style="color:var(--text3);font-size:10px">TP:</span> <span style="color:var(--green)">${d.tp||"—"}</span></div>
        <div><span style="color:var(--text3);font-size:10px">Pips:</span> <span style="color:${cls};font-weight:700">${pipsDisp}</span></div>
        <div><span style="color:var(--text3);font-size:10px">P&L:</span> <span style="color:${cls};font-weight:700">${pnlDisp}</span></div>
        <div><span style="color:var(--text3);font-size:10px">Score:</span> <span style="color:var(--glow2)">${d.bias_score>0?"+":""}${d.bias_score||0}</span></div>
        <div><span style="color:var(--text3);font-size:10px">Hit:</span> <span style="color:var(--text2)">${d.hit||(isOpen?"open":"-")}</span></div>
      </div>`;

    setTimeout(()=>mBuildModalChart(d), 50);
  } catch(e) {
    console.error("[mOpenTradeModal]", e);
    chartEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--red);font-size:11px">Connection error</div>`;
  }
}

function mBuildModalChart(d){
  const el = document.getElementById("m-modal-chart");
  el.innerHTML = "";
  if(_mModalChart){ try{_mModalChart.remove()}catch(e){} _mModalChart=null; }
  if(!d.candles || !d.candles.length){
    el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:11px">No candles</div>';
    return;
  }
  _mModalChart = LightweightCharts.createChart(el, {
    layout:{background:{type:"Solid",color:"#04020f"},textColor:"#7dd3fc"},
    grid:{vertLines:{color:"rgba(31,58,94,.25)"},horzLines:{color:"rgba(31,58,94,.25)"}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:"#1f3a5e",timeVisible:true,secondsVisible:false},
    rightPriceScale:{borderColor:"#1f3a5e",minimumWidth:80},
    handleScroll:true,handleScale:true,
  });
  new ResizeObserver(()=>{if(_mModalChart)_mModalChart.applyOptions({width:el.clientWidth,height:el.clientHeight})}).observe(el);
  _mModalSeries = _mModalChart.addCandlestickSeries({
    upColor:"#22d3ee", downColor:"rgba(13,8,32,.9)",
    borderUpColor:"#7dd3fc", borderDownColor:"#f87171",
    wickUpColor:"#7dd3fc", wickDownColor:"#f87171",
    priceFormat:{type:"price",precision:5,minMove:0.00001},
  });
  _mModalSeries.setData(d.candles);
  if(d.entry_price) _mModalSeries.createPriceLine({price:d.entry_price,color:"#7dd3fc",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"Entry"});
  if(d.sl) _mModalSeries.createPriceLine({price:d.sl,color:"#f87171",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"SL"});
  if(d.tp) _mModalSeries.createPriceLine({price:d.tp,color:"#34d399",lineWidth:1,lineStyle:2,axisLabelVisible:true,title:"TP"});
  const markers = [];
  if(d.entry_ts){
    markers.push({time:d.entry_ts, position:d.direction==="LONG"?"belowBar":"aboveBar", color:d.direction==="LONG"?"#22d3ee":"#f59e0b", shape:d.direction==="LONG"?"arrowUp":"arrowDown", text:`E ${d.entry_price}`});
  }
  if(d.exit_ts && d.exit_price){
    markers.push({time:d.exit_ts, position:"inBar", color:d.outcome==="win"?"#34d399":"#f87171", shape:"circle", text:`X ${d.exit_price}`});
  }
  if(markers.length) _mModalSeries.setMarkers(markers);
  _mModalChart.timeScale().fitContent();
}

function mCloseTradeModal(){
  document.getElementById("m-trade-modal").style.display = "none";
  if(_mModalChart){ try{_mModalChart.remove()}catch(e){} _mModalChart=null; }
}
</script>

<!-- MOBILE TRADE MODAL -->
<div id="m-trade-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;align-items:flex-end;backdrop-filter:blur(8px)" onclick="if(event.target===this)mCloseTradeModal()">
  <div style="width:100%;max-height:92vh;background:var(--bg);border-top:1px solid var(--border2);border-radius:18px 18px 0 0;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 -8px 32px rgba(34,211,238,.15)">
    <div style="padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">
      <div id="m-modal-title" style="font-size:14px;font-weight:700;color:var(--glow2);font-family:'Noto Serif JP',serif;letter-spacing:.3px">Trade</div>
      <button onclick="mCloseTradeModal()" style="background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);border-radius:6px;color:var(--red);font-size:18px;font-weight:600;padding:4px 12px;cursor:pointer">×</button>
    </div>
    <div id="m-modal-details" style="padding:8px 12px;border-bottom:1px solid var(--border)"></div>
    <div id="m-modal-chart" style="flex:1;min-height:300px;background:var(--bg)"></div>
  </div>
</div>

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
