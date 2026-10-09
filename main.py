"""
main.py — Longbridge Options Radar Scanner
FastAPI + Longbridge + Telegram Alerts + Monitoring
"""
import os
import time
import uuid
import asyncio
import traceback
import requests
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date as date_cls, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from longbridge.openapi import (
    Config, QuoteContext, Period, AdjustType,
    TradeSessions, SubType, PushQuote,
)

from analysis import (
    ema, rsi, atr, adx, vwap, rvol, macd,
    swing_highs, swing_lows,
    trend_status, detect_fvgs, detect_sweep,
    check_entry_sequence, calculate_score,
    classify_setup, compute_levels, scan_setup,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_sent_alerts: set[str] = set()
_analyze_cache: dict[str, tuple[float, dict]] = {}
_ANALYZE_TTL = 25

WHALE_MIN_VOLUME = 3000
WHALE_MIN_OI     = 5000

MONITOR_DAYS = 10   # مدة المراقبة القصوى
MONITOR_INTERVAL = 60  # كل 60 ثانية

# ============================================================
# قائمة المسح — 100 سهم
# ============================================================
SCAN_SYMBOLS = [
    "NVDA", "AAPL", "GOOG", "GOOGL", "MSFT", "AMZN", "META", "AVGO", "TSLA", "MU",
    "AMD", "WMT", "INTC", "ASML", "PLTR", "CSCO", "LRCX", "AMAT", "COST", "PANW",
    "NFLX", "CRWD", "KLAC", "TXN", "MRVL", "LIN", "AMGN", "ADI", "QCOM", "SHOP",
    "STX", "GILD", "TMUS", "PEP", "ARM", "WDC", "PDD", "ISRG", "FTNT", "VRTX",
    "BKNG", "SBUX", "ADP", "DDOG", "CDNS", "ABNB", "SNPS", "ADBE", "MAR", "CEG",
    "APP", "CSX", "MELI", "MNST", "DASH", "CTAS", "CMCSA", "REGN", "INTU", "MDLZ",
    "ROST", "MPWR", "ORLY", "HON", "AEP", "MSTR", "TRI", "NXPI", "PCAR", "FAST",
    "BKR", "EA", "FANG", "TEAM", "MCHP", "PYPL", "CCEP", "XEL", "WDAY", "ADSK",
    "EXC", "KDP", "IDXX", "TTWO", "ODFL", "PAYX", "ROP", "AXON", "FER", "DXCM",
    "ZS", "ALNY", "GEHC", "CTSH", "KHC", "CPRT", "INSM", "VRSK", "CHTR", "MRNA",
]

# ============================================================
# حالة المسح + المراقبة
# ============================================================
_scans: dict[str, dict] = {}
_SCAN_TTL = 3600

# المراقبة: { symbol: { card, entry, target, stop, direction, expires_at, status, alerts_sent } }
_monitoring: dict[str, dict] = {}


def get_ctx():
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.US"


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] {e}", flush=True)
        return False


def candles_to_df(candles):
    import pandas as pd
    return pd.DataFrame([{
        "time": c.timestamp, "open": float(c.open), "high": float(c.high),
        "low": float(c.low), "close": float(c.close), "volume": int(c.volume),
    } for c in candles])


PERIOD_MAP = {
    "15m": Period.Min_15, "1h": Period.Min_60,
    "4h": Period.Min_240, "1d": Period.Day, "1w": Period.Week,
}


def fetch_candles(symbol, timeframe, count=200):
    ctx = get_ctx()
    p = PERIOD_MAP.get(timeframe.lower())
    if p is None:
        raise ValueError(f"فريم غير مدعوم: {timeframe}")
    return ctx.candlesticks(norm(symbol), p, count,
                            AdjustType.NoAdjust,
                            trade_sessions=TradeSessions.All)


def get_current_price(symbol: str) -> float | None:
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        q = ctx.quote([sym])
        if q:
            return float(q[0].last_done)
    except Exception:
        pass
    return None


# ============================================================
# خيارات
# ============================================================
def _strike_of(c):
    for attr in ("strike_price", "strike", "price"):
        v = getattr(c, attr, None)
        if v is not None:
            try: return float(v)
            except Exception: pass
    return 0.0


def _call_of(c):
    v = getattr(c, "call_symbol", None)
    if v: return v
    co = getattr(c, "call", None)
    return getattr(co, "symbol", None) if co else None


def _put_of(c):
    v = getattr(c, "put_symbol", None)
    if v: return v
    po = getattr(c, "put", None)
    return getattr(po, "symbol", None) if po else None


def fetch_option_data(symbol, direction, price, strategy="swing"):
    ctx = get_ctx()
    sym = norm(symbol)
    result = {"strike":"—","expiry":"—","dte":"—","premium":"—","delta":None,
              "call_oi":[],"put_oi":[],"total_call_oi":0,"total_put_oi":0,
              "total_call_vol":0,"total_put_vol":0,"whales":[]}
    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates: return result
        today = datetime.now(timezone.utc).date()
        target_dte = 10
        parsed = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today: parsed.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today: parsed.append(dd)
                except Exception: continue

        valid = [(d, (d - today).days) for d in parsed if 5 <= (d - today).days <= 20]
        if not valid:
            valid = [(d, (d - today).days) for d in parsed]
        if not valid:
            return result

        exp_date, dte = min(valid, key=lambda x: abs(x[1] - target_dte))
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        chain = ctx.option_chain_info_by_date(sym, exp_date)
        if not chain: return result

        base_match = re.match(r'^([A-Z]+)', sym.replace(".US", ""))
        base_sym = base_match.group(1) if base_match else sym.replace(".US", "")
        yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
        prefix = f"{base_sym}{yy}{mm}{dd}"

        def build_call_sym(sk):
            return f"{prefix}C{str(int(round(sk * 1000))).zfill(8)}.US"

        def build_put_sym(sk):
            return f"{prefix}P{str(int(round(sk * 1000))).zfill(8)}.US"

        if direction == "bullish":
            target = price * 1.005
            cands = [c for c in chain if _call_of(c) and _strike_of(c) > price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _call_of(best); strike = _strike_of(best); opt_type = "C"
        else:
            target = price * 0.995
            cands = [c for c in chain if _put_of(c) and _strike_of(c) < price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _put_of(best); strike = _strike_of(best); opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"
        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                for attr in ("last_done", "last", "price"):
                    v = getattr(oq, attr, None)
                    if v is not None:
                        result["premium"] = round(float(v), 2); break
                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception: pass

        all_call_syms, all_put_syms = [], []
        strikes_map = {}

        for c in chain:
            sk = _strike_of(c)
            if sk <= 0: continue
            cs = _call_of(c) or build_call_sym(sk)
            ps = _put_of(c)  or build_put_sym(sk)
            all_call_syms.append(cs)
            all_put_syms.append(ps)
            strikes_map[sk] = (cs, ps)

        qmap = {}
        all_syms = list(set(all_call_syms + all_put_syms))
        for i in range(0, len(all_syms), 30):
            try:
                qs = ctx.option_quote(all_syms[i:i+30])
                if qs:
                    for q in qs: qmap[q.symbol] = q
            except Exception: pass

        tc_oi = tp_oi = tc_v = tp_v = 0
        for s in all_call_syms:
            if s in qmap:
                q = qmap[s]
                tc_oi += int(getattr(q, "open_interest", 0) or 0)
                tc_v  += int(getattr(q, "volume", 0) or 0)
        for s in all_put_syms:
            if s in qmap:
                q = qmap[s]
                tp_oi += int(getattr(q, "open_interest", 0) or 0)
                tp_v  += int(getattr(q, "volume", 0) or 0)

        result["total_call_oi"]  = tc_oi
        result["total_put_oi"]   = tp_oi
        result["total_call_vol"] = tc_v
        result["total_put_vol"]  = tp_v

        nearby = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:5]
        cd, pd_ = [], []
        for c in nearby:
            sk = int(_strike_of(c))
            cs, ps = strikes_map.get(_strike_of(c), (None, None))
            if cs and cs in qmap:
                q = qmap[cs]
                cd.append({"strike": sk,
                           "oi": int(getattr(q,"open_interest",0) or 0),
                           "volume": int(getattr(q,"volume",0) or 0)})
            if ps and ps in qmap:
                q = qmap[ps]
                pd_.append({"strike": sk,
                            "oi": int(getattr(q,"open_interest",0) or 0),
                            "volume": int(getattr(q,"volume",0) or 0)})
        cd.sort(key=lambda x: x["strike"], reverse=True)
        pd_.sort(key=lambda x: x["strike"], reverse=True)
        result["call_oi"] = cd
        result["put_oi"]  = pd_

        whales = []
        wide = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:10]
        for c in wide:
            sk = int(_strike_of(c))
            cs, ps = strikes_map.get(_strike_of(c), (None, None))
            for sym_opt, tp_ in ((cs, "CALL"), (ps, "PUT")):
                if not sym_opt or sym_opt not in qmap: continue
                q = qmap[sym_opt]
                vol = int(getattr(q, "volume", 0) or 0)
                oi  = int(getattr(q, "open_interest", 0) or 0)
                if vol < WHALE_MIN_VOLUME and oi < WHALE_MIN_OI: continue
                bid = float(getattr(q, "bid", 0) or 0)
                ask = float(getattr(q, "ask", 0) or 0)
                last = float(getattr(q, "last_done", 0) or getattr(q, "last", 0) or 0)
                dw = "mid"
                if ask > bid > 0:
                    sp = ask - bid
                    pos = (last - bid) / sp if sp > 0 else 0.5
                    if pos >= 0.7: dw = "buy"
                    elif pos <= 0.3: dw = "sell"
                whales.append({"strike": sk, "type": tp_, "volume": vol, "oi": oi,
                               "bid": round(bid,2), "ask": round(ask,2),
                               "last": round(last,2), "direction": dw})
        whales.sort(key=lambda w: w["volume"], reverse=True)
        result["whales"] = whales[:5]
    except Exception as e:
        print(f"[OPT] {e}", flush=True)
    return result


# ============================================================
# analyze_symbol
# ============================================================
def analyze_symbol(symbol: str) -> dict:
    df_weekly = candles_to_df(fetch_candles(symbol, "1w", 200))
    df_daily  = candles_to_df(fetch_candles(symbol, "1d", 300))
    df_4h     = candles_to_df(fetch_candles(symbol, "4h", 300))
    df_1h     = candles_to_df(fetch_candles(symbol, "1h", 300))

    def enrich(df):
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi"]   = rsi(df["close"], 14)
        df["atr"]   = atr(df, 14)
        df["adx"]   = adx(df, 14)
        df["vwap"]  = vwap(df)
        return df

    df_weekly = enrich(df_weekly)
    df_daily  = enrich(df_daily)
    df_4h     = enrich(df_4h)
    df_1h     = enrich(df_1h)

    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    scan = scan_setup(df_weekly, df_daily, df_4h, df_1h)

    card = {
        "color":  scan["color"],
        "label":  scan["label"],
        "score":  scan["score"],
        "status": scan["status"],
        "trend_w":  scan["trend_w"]["status"],
        "trend_d":  scan["trend_d"]["status"],
        "trend_4h": scan["trend_4h"]["status"],
        "rvol":     scan["rvol"],
        "sweep":    bool(scan.get("sweep")),
        "fvg":      bool(scan.get("fvg")),
        "rr":       scan["levels"].get("rr", 0),
    }

    direction = scan["direction"] or "bullish"
    levels = scan["levels"]

    def tf_snap(df, label, use_ema200=False):
        ts = trend_status(df, use_ema200=use_ema200)
        r = df.iloc[-1]
        return {
            "label": label,
            "trend": ts["status"],
            "ema20": round(float(r["ema20"]), 2),
            "ema50": round(float(r["ema50"]), 2),
            "rsi":   round(float(r["rsi"]), 1),
            "adx":   round(float(r["adx"]), 1),
            "rvol":  round(rvol(df), 2),
        }

    timeframes = [
        tf_snap(df_weekly, "1W", use_ema200=False),
        tf_snap(df_daily,  "1D", use_ema200=True),
        tf_snap(df_4h,     "4H", use_ema200=False),
        tf_snap(df_1h,     "1H", use_ema200=False),
    ]

    opt = fetch_option_data(symbol, direction, price, "swing")
    levels["strike"]  = opt.get("strike", "—")
    levels["expiry"]  = opt.get("expiry", "—")
    levels["dte"]     = opt.get("dte", "—")
    levels["premium"] = opt.get("premium", "—")

    supports = [
        round(float(df_daily["low"].iloc[-20:].min()), 2),
        round(float(df_weekly["low"].iloc[-4:].min()), 2),
    ]
    resistances = [
        round(float(df_daily["high"].iloc[-20:].max()), 2),
        round(float(df_weekly["high"].iloc[-4:].max()), 2),
    ]

    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "change": round(price - prev_close, 2),
        "changePercent": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "card": card,
        "levels": levels,
        "timeframes": timeframes,
        "vwap": round(float(df_1h["vwap"].iloc[-1]), 2),
        "supports": supports,
        "resistances": resistances,
        "call_oi": opt.get("call_oi", []),
        "put_oi":  opt.get("put_oi", []),
        "total_call_oi":  opt.get("total_call_oi", 0),
        "total_put_oi":   opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol":  opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def analyze_cached(symbol):
    key = symbol.upper().strip()
    now = time.time()
    if key in _analyze_cache:
        ts, data = _analyze_cache[key]
        if now - ts < _ANALYZE_TTL:
            return data
    data = analyze_symbol(symbol)
    _analyze_cache[key] = (now, data)
    return data


# ============================================================
# منطق المسح
# ============================================================
async def run_scan(scan_id: str):
    scan = _scans.get(scan_id)
    if not scan:
        return

    scan["status"] = "running"

    for i, sym in enumerate(SCAN_SYMBOLS):
        if scan.get("cancelled"):
            scan["status"] = "cancelled"
            return

        try:
            if sym in scan["results_map"]:
                continue

            data = await asyncio.to_thread(analyze_symbol, sym)
            scan["results_map"][sym] = data

            card = data.get("card", {})
            color = card.get("color", "gray")

            # ✅ أضف فقط CALL/PUT المؤكدة
            if color in ("green", "red"):
                scan["results"].append(data)

                # ✅ أضف إلى المراقبة التلقائية
                levels = data.get("levels", {})
                entry = levels.get("entry")
                target = levels.get("target1")
                stop = levels.get("stop")

                if entry and target and stop:
                    _monitoring[sym] = {
                        "symbol": sym,
                        "direction": "call" if color == "green" else "put",
                        "entry": float(entry),
                        "target": float(target),
                        "stop": float(stop),
                        "color": color,
                        "score": card.get("score", 0),
                        "created_at": time.time(),
                        "expires_at": time.time() + MONITOR_DAYS * 86400,
                        "status": "active",       # active / target_hit / stop_hit / expired
                        "alerts_sent": set(),
                        "last_price": float(data.get("price", entry)),
                    }
                    print(f"[MONITOR] added {sym} ({color})", flush=True)

            scan["completed"] = i + 1

        except Exception as e:
            print(f"[SCAN] {sym} error: {e}", flush=True)
            scan["errors"].append({"symbol": sym, "error": str(e)})
            scan["completed"] = i + 1

        await asyncio.sleep(0.15)

    scan["status"] = "done"
    scan["finished_at"] = time.time()
    print(f"[SCAN] {scan_id} done — found {len(scan['results'])} opportunities", flush=True)


def cleanup_old_scans():
    now = time.time()
    to_delete = []
    for sid, s in _scans.items():
        if now - s.get("started_at", now) > _SCAN_TTL:
            to_delete.append(sid)
    for sid in to_delete:
        _scans.pop(sid, None)


# ============================================================
# مراقبة البطاقات النشطة
# ============================================================
async def monitor_task():
    """كل 60 ثانية — يفحص كل بطاقة نشطة"""
    await asyncio.sleep(60)

    while True:
        try:
            if not _monitoring:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            now = time.time()

            for sym in list(_monitoring.keys()):
                m = _monitoring.get(sym)
                if not m:
                    continue

                # ✅ إذا انتهى الأمر — تجاهل
                if m["status"] != "active":
                    # إذا انتهت المراقبة > يوم — احذفها تلقائياً
                    if m["status"] in ("target_hit", "stop_hit", "expired"):
                        if now - m.get("ended_at", now) > 86400:
                            _monitoring.pop(sym, None)
                    continue

                # ✅ انتهت المدة؟
                if now >= m["expires_at"]:
                    m["status"] = "expired"
                    m["ended_at"] = now
                    await asyncio.to_thread(
                        send_telegram_alert,
                        f"⏰ <b>انتهت المدة</b> — {sym}\nمرت {MONITOR_DAYS} أيام بدون هدف أو وقف."
                    )
                    continue

                # ✅ اجلب السعر
                price = await asyncio.to_thread(get_current_price, sym)
                if price is None:
                    continue

                m["last_price"] = price
                direction = m["direction"]
                target = m["target"]
                stop = m["stop"]

                # ✅ فحص الهدف
                if "target" not in m["alerts_sent"]:
                    if direction == "call" and price >= target:
                        m["status"] = "target_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("target")
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"✅ <b>تحقق الهدف</b> — {sym}\n"
                            f"السعر: ${price:.2f}\n"
                            f"الهدف: ${target:.2f}\n"
                            f"الربح: {((price-target)/target*100):.2f}%"
                        )
                        continue
                    if direction == "put" and price <= target:
                        m["status"] = "target_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("target")
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"✅ <b>تحقق الهدف</b> — {sym}\n"
                            f"السعر: ${price:.2f}\n"
                            f"الهدف: ${target:.2f}"
                        )
                        continue

                # ✅ فحص الوقف
                if "stop" not in m["alerts_sent"]:
                    if direction == "call" and price <= stop:
                        m["status"] = "stop_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("stop")
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"❌ <b>ضرب الوقف</b> — {sym}\n"
                            f"السعر: ${price:.2f}\n"
                            f"الوقف: ${stop:.2f}"
                        )
                        continue
                    if direction == "put" and price >= stop:
                        m["status"] = "stop_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("stop")
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"❌ <b>ضرب الوقف</b> — {sym}\n"
                            f"السعر: ${price:.2f}\n"
                            f"الوقف: ${stop:.2f}"
                        )
                        continue

        except Exception as e:
            print(f"[MONITOR] error: {e}", flush=True)

        await asyncio.sleep(MONITOR_INTERVAL)


# ============================================================
# FastAPI
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    try:
        ctx = get_ctx()
        ctx.set_on_quote(_on_quote)
        print("[STARTUP] ready", flush=True)
    except Exception as e:
        print(f"[STARTUP] error: {e}", flush=True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            send_telegram_alert("🚀 <b>Longbridge Scanner</b> — النظام يعمل")
        except Exception: pass

    async def keepalive():
        while True:
            await asyncio.sleep(300)
            try:
                get_ctx().quote(["AAPL.US"])
                cleanup_old_scans()
            except Exception:
                pass

    ka = asyncio.create_task(keepalive())
    mt = asyncio.create_task(monitor_task())

    yield

    ka.cancel()
    mt.cancel()
    global _quote_ctx
    _quote_ctx = None


app = FastAPI(title="Longbridge Scanner", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/status")
def status():
    try:
        q = get_ctx().quote(["AAPL.US"])
        if q:
            return {"connected": True, "price": str(q[0].last_done), "symbol": q[0].symbol}
        return {"connected": False}
    except Exception as e:
        return JSONResponse(status_code=503, content={"connected": False, "error": str(e)})


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/test-telegram")
def test_telegram():
    ok = send_telegram_alert("✅ <b>اختبار ناجح</b>")
    return {"sent": ok, "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


@app.get("/api/symbols")
def get_symbols():
    return {"total": len(SCAN_SYMBOLS), "symbols": SCAN_SYMBOLS}


@app.post("/api/scan/start")
async def scan_start():
    cleanup_old_scans()

    scan_id = str(uuid.uuid4())
    _scans[scan_id] = {
        "id": scan_id,
        "status": "starting",
        "total": len(SCAN_SYMBOLS),
        "completed": 0,
        "results": [],
        "results_map": {},
        "errors": [],
        "started_at": time.time(),
    }

    asyncio.create_task(run_scan(scan_id))

    return {"scan_id": scan_id, "total": len(SCAN_SYMBOLS)}


@app.get("/api/scan/{scan_id}")
def scan_status(scan_id: str):
    scan = _scans.get(scan_id)
    if not scan:
        return JSONResponse(status_code=404, content={"error": "scan not found"})

    # أضف حالة المراقبة لكل بطاقة
    results = []
    for r in scan["results"]:
        sym = r.get("symbol")
        m = _monitoring.get(sym, {})
        r_copy = dict(r)
        r_copy["monitor_status"] = m.get("status", "none")
        r_copy["monitor_price"] = m.get("last_price")
        r_copy["monitor_expires_at"] = m.get("expires_at")
        results.append(r_copy)

    return {
        "scan_id": scan_id,
        "status": scan["status"],
        "total": scan["total"],
        "completed": scan["completed"],
        "found": len(scan["results"]),
        "results": results,
        "errors_count": len(scan["errors"]),
    }


@app.post("/api/scan/{scan_id}/cancel")
def scan_cancel(scan_id: str):
    scan = _scans.get(scan_id)
    if not scan:
        return JSONResponse(status_code=404, content={"error": "scan not found"})
    scan["cancelled"] = True
    return {"ok": True}


@app.get("/api/scan/latest")
def scan_latest():
    if not _scans:
        return JSONResponse(status_code=404, content={"error": "no scans yet"})
    latest_id = max(_scans.keys(), key=lambda k: _scans[k].get("started_at", 0))
    return scan_status(latest_id)


# ✅ حالة المراقبة لبطاقة واحدة (للتحديث اللحظي)
@app.get("/api/monitor/{symbol}")
def monitor_status(symbol: str):
    sym = symbol.upper().strip()
    m = _monitoring.get(sym)
    if not m:
        return {"monitored": False}
    return {
        "monitored": True,
        "symbol": sym,
        "status": m["status"],
        "entry": m["entry"],
        "target": m["target"],
        "stop": m["stop"],
        "last_price": m.get("last_price"),
        "expires_at": m["expires_at"],
        "direction": m["direction"],
    }


# ✅ إيقاف المراقبة (عند حذف البطاقة)
@app.post("/api/monitor/{symbol}/remove")
def monitor_remove(symbol: str):
    sym = symbol.upper().strip()
    if sym in _monitoring:
        _monitoring.pop(sym, None)
        print(f"[MONITOR] removed {sym}", flush=True)
    return {"ok": True}


# ✅ جميع البطاقات المُراقَبة
@app.get("/api/monitoring")
def get_monitoring():
    return {
        "count": len(_monitoring),
        "symbols": list(_monitoring.keys()),
        "details": [
            {
                "symbol": s,
                "status": m["status"],
                "entry": m["entry"],
                "target": m["target"],
                "stop": m["stop"],
                "last_price": m.get("last_price"),
                "direction": m["direction"],
                "score": m["score"],
            }
            for s, m in _monitoring.items()
        ],
    }


@app.get("/api/analyze/{symbol}")
def analyze(symbol: str):
    try:
        return analyze_cached(symbol)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc().split("\n")[-10:],
        })


@app.get("/api/price/{symbol}")
def price_only(symbol: str):
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        try:
            candles = ctx.candlesticks(sym, Period.Min_1, 1, AdjustType.NoAdjust,
                                       trade_sessions=TradeSessions.All)
            if candles:
                last = candles[-1]
                return {"symbol": symbol.upper(), "price": float(last.close),
                        "timestamp": last.timestamp.isoformat()}
        except Exception: pass
        q = ctx.quote([sym])
        if q:
            return {"symbol": symbol.upper(), "price": float(q[0].last_done),
                    "timestamp": q[0].timestamp.isoformat()}
        return JSONResponse(status_code=404, content={"error": "no data"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class ConnectionManager:
    def __init__(self): self.active = {}
    async def connect(self, symbol, ws):
        await ws.accept()
        self.active.setdefault(symbol, []).append(ws)
    def disconnect(self, symbol, ws):
        if symbol in self.active and ws in self.active[symbol]:
            self.active[symbol].remove(ws)
    async def broadcast(self, symbol, message):
        for ws in list(self.active.get(symbol, [])):
            try: await ws.send_json(message)
            except Exception: self.disconnect(symbol, ws)


manager = ConnectionManager()


def _on_quote(symbol, event):
    global _event_loop
    if _event_loop is None: return
    try:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(symbol.replace(".US", ""),
                              {"symbol": symbol.replace(".US", ""),
                               "price": float(event.last_done)}),
            _event_loop)
    except Exception: pass


@app.websocket("/ws/{symbol}")
async def ws_endpoint(ws: WebSocket, symbol: str):
    symbol = symbol.upper()
    await manager.connect(symbol, ws)
    sym_us = norm(symbol)
    if sym_us not in _subscribed:
        try:
            ctx = get_ctx()
            ctx.subscribe([sym_us], [SubType.Quote])
            _subscribed.add(sym_us)
        except Exception: pass
    try:
        while True:
            await asyncio.wait_for(ws.receive_text(), timeout=90)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        manager.disconnect(symbol, ws)


@app.get("/")
def index(): return FileResponse("index.html")

@app.get("/style.css")
def css(): return FileResponse("style.css")

@app.get("/app.js")
def js(): return FileResponse("app.js")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
