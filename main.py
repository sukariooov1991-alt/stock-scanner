"""
main.py — Longbridge Options Radar Scanner
النسخة النهائية — 4 فريمات، مساران، حذف الرمادي نهائياً
+ توحيد صريح للأوقات: time = بداية الشمعة، close_time = نهاية الشمعة
+ رفض الإشارات الميتة فوراً (السعر عند الوقف أو الهدف)
+ حذف فوري للميتة من كل الطبقات
"""
import os
import time
import uuid
import asyncio
import traceback
import requests
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date as date_cls

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from longbridge.openapi import (
    Config, QuoteContext, Period, AdjustType,
    TradeSessions, SubType, PushQuote,
)

from analysis import (
    atr, check_breakout, find_retest,
    scan_setup,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_analyze_cache: dict[str, tuple[float, dict]] = {}
_ANALYZE_TTL = 25

# ✅ فلاتر العقد
MAX_PREMIUM = 2.5
MAX_SPREAD  = 0.10
DTE_MIN     = 5
DTE_MAX     = 20
DTE_TARGET  = 10
OTM_MIN_PCT = 0.005
OTM_MAX_PCT = 0.04
OTM_TARGET  = 0.02

# ✅ Cache
_candle_cache: dict[str, tuple[float, list]] = {}
_CANDLE_TTL = {"1h": 300, "4h": 900, "1d": 3600, "1w": 3600}
_CANDLE_CACHE_MAX = 5000

WHALE_MIN_VOLUME = 3000
WHALE_MIN_OI     = 5000
MONITOR_DAYS = 10
MONITOR_INTERVAL = 60
MAX_CONCURRENT = 5

NEARBY_STRIKES = 5
OPTION_QUOTE_BATCH = 50

VALID_COLORS = ("green", "red")

# حالات المراقبة التي تعني "الصفقة انتهت"
DEAD_STATUSES = ("stop_hit", "target_hit", "expired")

# ثواني كل إطار
_TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}

SCAN_SYMBOLS = list(dict.fromkeys([
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "BRK-B", "TSLA", "AVGO",
    "WMT", "LLY", "JPM", "V", "UNH", "XOM", "MA", "ORCL", "COST", "HD",
    "PG", "JNJ", "NFLX", "ABBV", "BAC", "CRM", "TMUS", "CVX", "AMD", "KO",
    "PEP", "TMO", "LIN", "WFC", "ADBE", "MRK", "DIS", "ACN", "CSCO", "ABT",
    "MCD", "INTU", "VZ", "QCOM", "GE", "TXN", "DHR", "IBM", "CAT", "AXP",
    "AMGN", "NOW", "PM", "NEE", "PFE", "RTX", "SPGI", "UNP", "UBER", "GS",
    "HON", "ISRG", "T", "LOW", "ELV", "SYK", "BKNG", "BLK", "ETN", "VRTX",
    "C", "TJX", "MDT", "REGN", "BA", "PLD", "MMC", "CB", "SCHW", "ADP",
    "BSX", "CI", "DE", "ADI", "LMT", "MDLZ", "FI", "BMY", "SO", "CVS",
    "AMAT", "SBUX", "GILD", "PANW", "KLAC", "LRCX", "INTC", "PGR",
    "PLTR", "SHOP", "SNOW", "DDOG", "CRWD", "ZS", "NET", "MDB", "TEAM", "WDAY",
    "ADSK", "CDNS", "SNPS", "ANSS", "FTNT", "OKTA", "DOCU", "TWLO", "HUBS", "ZM",
    "SQ", "PYPL", "COIN", "HOOD", "SOFI", "AFRM", "MRVL", "NXPI", "MCHP", "ON",
    "WDC", "STX", "DELL", "HPQ", "HPE", "NTAP", "SMCI", "ARM", "TER", "ROP",
    "TYL", "PTC", "VRSN", "CTSH", "INFY", "WIT", "EPAM", "GLOB", "FIS", "SSNC",
    "JKHY", "FFIV", "AKAM", "JNPR", "MSI", "APH", "TEL", "GLW", "KEYS",
    "HUM", "HCA", "MCK", "ABC", "CAH", "DGX", "LH", "BAX", "BDX", "BIIB",
    "ILMN", "IDXX", "A", "MTD", "WAT", "RMD", "HOLX", "COO", "EW", "DXCM",
    "PODD", "ALGN", "ZBH", "TFX", "STE", "XRAY", "ALC", "CRL", "IQV",
    "PKI", "RVTY", "BIO", "TECH", "QGEN", "MOH", "CNC", "ALHC", "OSCR",
    "CLOV", "HIMS", "DOCS", "VEEV", "TDOC", "AMWL", "ONEM", "PHR", "HCTI", "CERT",
    "EVH", "PGNY", "ACCD", "GDRX", "HNGR",
    "USB", "PNC", "TFC", "MTB", "FITB", "HBAN", "RF", "KEY", "CFG", "STT",
    "BK", "NTRS", "MS", "PRU", "MET", "AFL", "ALL", "TRV", "AIG", "AJG",
    "AON", "MCO", "ICE", "CME", "NDAQ", "CBOE", "MSCI", "MKTX", "TW", "CINF",
    "WRB", "L", "RE", "GL", "CNA", "HIG", "ACGL", "EG", "AIZ", "SYF",
    "DFS", "ALLY", "COF", "BX", "KKR", "APO", "ARES", "OWL", "TPG", "CG",
    "TGT", "KR", "SYY", "ADM", "GIS", "K", "HSY", "MKC", "CL", "KMB",
    "CHD", "EL", "YUM", "CMG", "DPZ", "WEN", "QSR", "DKNG", "MAR", "HLT",
    "H", "RCL", "CCL", "NCLH", "LUV", "DAL", "AAL", "UAL", "F", "GM",
    "RIVN", "LCID", "NIO", "XPEV", "LI", "TM", "HMC", "STLA", "RACE", "APTV",
    "BWA", "LEA", "MGA", "ALV", "TSCO", "ORLY", "AZO", "AAP", "GPC", "LKQ",
    "ULTA", "BBY", "DKS", "ROST", "BURL", "M", "JWN", "KSS", "GPS", "ANF",
    "AEO", "URBN", "PLCE", "BJ", "PSMT", "DLTR", "DG", "FIVE", "OLLI", "BIG",
    "WBA",
    "CMI", "PCAR", "MMM", "EMR", "PH", "ROK", "DOV", "IR", "ITW", "CSX",
    "NSC", "UPS", "FDX", "GD", "NOC", "LHX", "HII", "TDG", "HEI", "TXT",
    "AXON", "WM", "RSG", "CWST", "SRCL", "CLH", "ECOL", "USX", "SAIA", "ODFL",
    "XPO", "CHRW", "EXPD", "HUBG", "LSTR", "JBHT", "KNX", "WERN", "SNDR", "ARCB",
    "MRTN", "GWW", "FAST", "POOL", "WSO", "BECN", "BLDR", "UFPI", "BCC", "LPX",
    "MAS", "GEV", "VRT", "GNRC", "PWR", "AME",
    "COP", "EOG", "PXD", "DVN", "OXY", "HAL", "SLB", "BKR", "PSX", "VLO",
    "MPC", "KMI", "WMB", "OKE", "ET", "EPD", "PAA", "TRGP", "HES", "MRO",
    "APA", "CTRA", "FANG", "HESM", "DINO", "PBF", "DK", "PARR",
    "CMCSA", "CHTR", "WBD", "PARA", "FOXA", "DUK", "D", "AEP", "EXC", "XEL",
    "SRE", "PEG", "ED", "WEC", "ES", "AEE", "DTE", "PPL", "FE", "ETR",
    "CMS", "CNP", "NI", "AES", "NRG", "VST", "CEG", "PSEG", "PNW", "LNT",
    "EVRG", "OGE", "PCG", "EIX", "AWK", "WTRG", "SJW", "CWT", "MSEX", "YORW",
    "ARTNA",
]))

_scans: dict[str, dict] = {}
_SCAN_TTL = 3600
_monitoring: dict[str, dict] = {}


def get_ctx():
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol: str) -> str:
    s = symbol.strip().upper().replace("-", ".")
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


def candles_to_df(candles, timeframe=None):
    """
    تحويل الشموع إلى DataFrame بأوقات موحّدة:
      - time:       وقت بداية الشمعة.
      - close_time: وقت إغلاق الشمعة = البداية + مدة الإطار.
    تُستبعد كل شمعة لم يكتمل وقت إغلاقها.
    """
    import pandas as pd
    rows = [{
        "time": c.timestamp, "open": float(c.open), "high": float(c.high),
        "low": float(c.low), "close": float(c.close), "volume": int(c.volume),
    } for c in candles]
    df = pd.DataFrame(rows)
    if df.empty or timeframe not in _TF_SECONDS or "time" not in df:
        return df

    seconds = _TF_SECONDS[timeframe]
    try:
        raw = df["time"]
        if pd.api.types.is_numeric_dtype(raw):
            starts = pd.to_datetime(raw, unit="s", utc=True)
        else:
            starts = pd.to_datetime(raw, utc=True)
        closes = starts + pd.Timedelta(seconds=seconds)
        now = pd.Timestamp.now(tz="UTC")

        mask = closes <= now
        df = df.loc[mask].copy()
        starts = starts.loc[mask]
        closes = closes.loc[mask]
        if df.empty:
            return df.reset_index(drop=True)

        df["time"] = starts.values
        df["close_time"] = closes.values
    except Exception:
        df = df.iloc[:-1].copy()
        try:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df["close_time"] = df["time"] + pd.Timedelta(seconds=seconds)
        except Exception:
            pass
    return df.reset_index(drop=True)


PERIOD_MAP = {
    "1h": Period.Min_60,
    "4h": Period.Min_240,
    "1d": Period.Day,
    "1w": Period.Week,
}


def fetch_candles(symbol, timeframe, count=200):
    tf = timeframe.lower()
    key = f"{symbol.upper()}:{tf}:{count}"
    now = time.time()
    ttl = _CANDLE_TTL.get(tf, 60)

    if key in _candle_cache:
        ts, cached = _candle_cache[key]
        if now - ts < ttl and len(cached) >= count:
            return cached

    ctx = get_ctx()
    p = PERIOD_MAP.get(tf)
    if p is None:
        raise ValueError(f"فريم غير مدعوم: {timeframe}")

    candles = ctx.candlesticks(norm(symbol), p, count,
                                AdjustType.NoAdjust,
                                trade_sessions=TradeSessions.All)

    if len(_candle_cache) > _CANDLE_CACHE_MAX:
        oldest = sorted(_candle_cache.items(), key=lambda x: x[1][0])[:1000]
        for k, _ in oldest:
            _candle_cache.pop(k, None)

    _candle_cache[key] = (now, candles)
    return candles


def get_current_price(symbol: str):
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


def _empty_option_result():
    return {
        "strike": "—", "expiry": "—", "dte": "—", "premium": "—", "delta": None,
        "call_oi": [], "put_oi": [],
        "total_call_oi": 0, "total_put_oi": 0,
        "total_call_vol": 0, "total_put_vol": 0,
        "whales": [],
        "filter_pass": False, "filter_reason": "",
    }


def _pick_strike(candidates, price, target_pct, is_call):
    if not candidates:
        return None
    target_price = price * (1 + target_pct) if is_call else price * (1 - target_pct)
    lo = price * (1 + OTM_MIN_PCT) if is_call else price * (1 - OTM_MAX_PCT)
    hi = price * (1 + OTM_MAX_PCT) if is_call else price * (1 - OTM_MIN_PCT)

    in_range = [c for c in candidates if lo <= _strike_of(c) <= hi]
    if not in_range:
        return None
    return min(in_range, key=lambda c: abs(_strike_of(c) - target_price))


def fetch_option_data(symbol, direction, price, strategy="swing"):
    ctx = get_ctx()
    sym = norm(symbol)
    result = _empty_option_result()
    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates:
            result["filter_reason"] = "no_dates"
            return result

        today = datetime.now(timezone.utc).date()
        parsed = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today: parsed.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today: parsed.append(dd)
                except Exception: continue

        valid = [(d, (d - today).days) for d in parsed
                 if DTE_MIN <= (d - today).days <= DTE_MAX]
        if not valid:
            result["filter_reason"] = "no_valid_expiry"
            return result

        exp_date, dte = min(valid, key=lambda x: abs(x[1] - DTE_TARGET))
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        chain = ctx.option_chain_info_by_date(sym, exp_date)
        if not chain:
            result["filter_reason"] = "no_chain"
            return result

        base_match = re.match(r'^([A-Z.]+)', sym.replace(".US", ""))
        base_sym = base_match.group(1) if base_match else sym.replace(".US", "")
        yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
        prefix = f"{base_sym}{yy}{mm}{dd}"

        def build_call_sym(sk):
            return f"{prefix}C{str(int(round(sk * 1000))).zfill(8)}.US"

        def build_put_sym(sk):
            return f"{prefix}P{str(int(round(sk * 1000))).zfill(8)}.US"

        is_call = (direction == "bullish")

        if is_call:
            cands = [c for c in chain if _call_of(c) and _strike_of(c) > price]
            if not cands:
                result["filter_reason"] = "no_call_strike"
                return result
            best = _pick_strike(cands, price, OTM_TARGET, True)
            if best is None:
                result["filter_reason"] = "no_call_strike"
                return result
            option_symbol = _call_of(best)
            strike = _strike_of(best)
            opt_type = "C"
        else:
            cands = [c for c in chain if _put_of(c) and _strike_of(c) < price]
            if not cands:
                result["filter_reason"] = "no_put_strike"
                return result
            best = _pick_strike(cands, price, OTM_TARGET, False)
            if best is None:
                result["filter_reason"] = "no_put_strike"
                return result
            option_symbol = _put_of(best)
            strike = _strike_of(best)
            opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"

        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                last = None
                for attr in ("last_done", "last", "price"):
                    v = getattr(oq, attr, None)
                    if v is not None:
                        try:
                            last = float(v)
                            break
                        except (TypeError, ValueError):
                            continue

                bid = float(getattr(oq, "bid", 0) or 0)
                ask = float(getattr(oq, "ask", 0) or 0)
                spread = (ask - bid) if ask > bid > 0 else 999
                entry_premium = ask if ask > 0 else last
                if entry_premium is not None:
                    result["premium"] = round(entry_premium, 2)

                if entry_premium is None:
                    result["filter_reason"] = "no_premium"
                elif entry_premium > MAX_PREMIUM:
                    result["filter_reason"] = f"premium_too_high ({entry_premium})"
                elif spread > MAX_SPREAD:
                    result["filter_reason"] = f"spread_too_wide ({spread:.2f})"
                else:
                    result["filter_pass"] = True

                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception as e:
            result["filter_reason"] = f"quote_error: {e}"

        calls_above = sorted(
            [c for c in chain if _strike_of(c) > price and _call_of(c)],
            key=lambda c: _strike_of(c)
        )[:NEARBY_STRIKES]
        puts_below = sorted(
            [c for c in chain if _strike_of(c) < price and _put_of(c)],
            key=lambda c: -_strike_of(c)
        )[:NEARBY_STRIKES]

        strikes_map = {}
        for c in calls_above:
            sk = _strike_of(c)
            strikes_map.setdefault(sk, [None, None])[0] = _call_of(c) or build_call_sym(sk)
        for c in puts_below:
            sk = _strike_of(c)
            strikes_map.setdefault(sk, [None, None])[1] = _put_of(c) or build_put_sym(sk)

        all_call_syms = [v[0] for v in strikes_map.values() if v[0]]
        all_put_syms  = [v[1] for v in strikes_map.values() if v[1]]

        qmap = {}
        all_syms = list(set(all_call_syms + all_put_syms))
        for i in range(0, len(all_syms), OPTION_QUOTE_BATCH):
            try:
                qs = ctx.option_quote(all_syms[i:i+OPTION_QUOTE_BATCH])
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

        cd = []
        for sk, (cs, _) in sorted(strikes_map.items(), reverse=True):
            if cs and cs in qmap:
                q = qmap[cs]
                cd.append({"strike": int(sk),
                           "oi": int(getattr(q, "open_interest", 0) or 0),
                           "volume": int(getattr(q, "volume", 0) or 0)})
        pd_ = []
        for sk, (_, ps) in sorted(strikes_map.items(), reverse=True):
            if ps and ps in qmap:
                q = qmap[ps]
                pd_.append({"strike": int(sk),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0)})
        result["call_oi"] = cd
        result["put_oi"]  = pd_

        whales = []
        for sk, (cs, ps) in strikes_map.items():
            for sym_opt, tp_ in ((cs, "CALL"), (ps, "PUT")):
                if not sym_opt or sym_opt not in qmap: continue
                q = qmap[sym_opt]
                vol = int(getattr(q, "volume", 0) or 0)
                oi  = int(getattr(q, "open_interest", 0) or 0)
                if vol < WHALE_MIN_VOLUME and oi < WHALE_MIN_OI: continue
                bid = float(getattr(q, "bid", 0) or 0)
                ask = float(getattr(q, "ask", 0) or 0)
                last_w = float(getattr(q, "last_done", 0) or getattr(q, "last", 0) or 0)
                dw = "mid"
                if ask > bid > 0:
                    sp = ask - bid
                    pos = (last_w - bid) / sp if sp > 0 else 0.5
                    if pos >= 0.7: dw = "buy"
                    elif pos <= 0.3: dw = "sell"
                whales.append({"strike": int(sk), "type": tp_, "volume": vol, "oi": oi,
                               "bid": round(bid, 2), "ask": round(ask, 2),
                               "last": round(last_w, 2), "direction": dw})
        whales.sort(key=lambda w: w["volume"], reverse=True)
        result["whales"] = whales[:5]
    except Exception as e:
        print(f"[OPT] {symbol} {e}", flush=True)
        result["filter_reason"] = f"exception: {e}"
    return result


# ============================================================
# رفض الإشارات الميتة — تُعيد "gray" (محذوفة في كل الطبقات)
# ============================================================
def _gray_result(symbol: str, price: float) -> dict:
    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "card": {"color": "gray", "label": "لا إشارة",
                 "status": "none", "direction": None, "stage": None},
    }


def _is_dead_signal(scan: dict, price: float) -> bool:
    """
    ✅ إصلاح النقطة 1: إذا السعر الحالي وصل الوقف أو الهدف → الصفقة ميتة.
    - CALL: ميتة إذا (price <= stop) أو (price >= target1)
    - PUT:  ميتة إذا (price >= stop) أو (price <= target1)
    """
    if scan.get("color") not in VALID_COLORS:
        return False
    lv = scan.get("levels", {}) or {}
    stop_p = lv.get("stop")
    tgt_p = lv.get("target1")
    if stop_p is None or tgt_p is None:
        return False
    try:
        sp = float(stop_p)
        tp = float(tgt_p)
    except (TypeError, ValueError):
        return False
    if scan["color"] == "green":
        return (price <= sp) or (price >= tp)
    return (price >= sp) or (price <= tp)


# ============================================================
# analyze_symbol
# ============================================================
def analyze_symbol(symbol: str) -> dict:
    df_weekly = candles_to_df(fetch_candles(symbol, "1w", 150), "1w")
    df_daily  = candles_to_df(fetch_candles(symbol, "1d", 400), "1d")
    df_4h     = candles_to_df(fetch_candles(symbol, "4h", 200), "4h")
    df_1h     = candles_to_df(fetch_candles(symbol, "1h", 200), "1h")

    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    scan = scan_setup(df_weekly, df_daily, df_4h, df_1h)

    # ✅ إصلاح النقطة 1: رفض الرمادي + رفض الإشارات الميتة
    if scan["color"] not in VALID_COLORS or _is_dead_signal(scan, price):
        return _gray_result(symbol, price)

    card = {
        "color":     scan["color"],
        "label":     scan["label"],
        "status":    scan["status"],
        "direction": scan.get("direction"),
        "stage":     scan.get("breakout_stage"),
    }

    levels = scan.get("levels", {}) or {}

    direction_opt = "bullish" if scan["color"] == "green" else "bearish"
    opt = fetch_option_data(symbol, direction_opt, price, "swing")

    levels["strike"]  = opt.get("strike", "—")
    levels["expiry"]  = opt.get("expiry", "—")
    levels["dte"]     = opt.get("dte", "—")
    levels["premium"] = opt.get("premium", "—")

    sr = scan.get("support_resistance", {})
    timeframes = scan.get("timeframes", [])

    supports, resistances = [], []
    if sr.get("weekly"):
        if sr["weekly"].get("support"): supports.append(sr["weekly"]["support"])
        if sr["weekly"].get("resistance"): resistances.append(sr["weekly"]["resistance"])
    if sr.get("daily"):
        if sr["daily"].get("support"): supports.append(sr["daily"]["support"])
        if sr["daily"].get("resistance"): resistances.append(sr["daily"]["resistance"])

    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "change": round(price - prev_close, 2),
        "changePercent": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "card": card,
        "levels": levels,
        "timeframes": timeframes,
        "support_resistance": sr,
        "supports": supports,
        "resistances": resistances,
        "call_oi": opt.get("call_oi", []),
        "put_oi":  opt.get("put_oi", []),
        "total_call_oi":  opt.get("total_call_oi", 0),
        "total_put_oi":   opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol":  opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "filter_pass": opt.get("filter_pass", False),
        "filter_reason": opt.get("filter_reason", ""),
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
# ✅ إصلاح النقطة 2: حذف الرمز من كل المسحات عند موت الصفقة
# ============================================================
def _purge_symbol_from_scans(symbol: str):
    sym = symbol.upper().strip()
    for scan in _scans.values():
        rm = scan.get("results_map") or {}
        rm.pop(sym, None)
        results = scan.get("results") or []
        scan["results"] = [r for r in results if r.get("symbol") != sym]


# ============================================================
# المسح
# ============================================================
async def analyze_one_safe(sym, semaphore):
    async with semaphore:
        try:
            data = await asyncio.to_thread(analyze_symbol, sym)
            return sym, data, None
        except Exception as e:
            return sym, None, str(e)


async def run_scan(scan_id: str):
    scan = _scans.get(scan_id)
    if not scan:
        return

    scan["status"] = "running"
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    for i in range(0, len(SCAN_SYMBOLS), MAX_CONCURRENT):
        if scan.get("cancelled"):
            scan["status"] = "cancelled"
            return

        batch = SCAN_SYMBOLS[i:i + MAX_CONCURRENT]
        results = await asyncio.gather(
            *[analyze_one_safe(sym, semaphore) for sym in batch]
        )

        for sym, data, error in results:
            if error:
                print(f"[SCAN] {sym} error: {error}", flush=True)
                scan["errors"].append({"symbol": sym, "error": error})
            elif data:
                card = data.get("card", {})
                color = card.get("color", "gray")

                if color in VALID_COLORS:
                    scan["results_map"][sym] = data
                    scan["results"].append(data)

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
                            "created_at": time.time(),
                            "expires_at": time.time() + MONITOR_DAYS * 86400,
                            "status": "active",
                            "alerts_sent": set(),
                            "last_price": float(data.get("price", entry)),
                        }
                        print(f"[MONITOR] added {sym} ({color})", flush=True)

            scan["completed"] += 1

    scan["status"] = "done"
    scan["finished_at"] = time.time()
    print(f"[SCAN] {scan_id} done — found {len(scan['results'])} opportunities", flush=True)


def cleanup_old_scans():
    now = time.time()
    for sid in [k for k, v in _scans.items()
                if now - v.get("started_at", now) > _SCAN_TTL]:
        _scans.pop(sid, None)


# ============================================================
# المراقبة
# ============================================================
async def monitor_task():
    await asyncio.sleep(60)
    while True:
        try:
            if not _monitoring:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            now = time.time()
            for sym in list(_monitoring.keys()):
                m = _monitoring.get(sym)
                if not m: continue

                if m["status"] != "active":
                    if m["status"] in DEAD_STATUSES:
                        if now - m.get("ended_at", now) > 86400:
                            _monitoring.pop(sym, None)
                    continue

                if now >= m["expires_at"]:
                    m["status"] = "expired"
                    m["ended_at"] = now
                    # ✅ إصلاح النقطة 2
                    _purge_symbol_from_scans(sym)
                    await asyncio.to_thread(
                        send_telegram_alert,
                        f"⏰ <b>انتهت المدة</b> — {sym}\nمرت {MONITOR_DAYS} أيام."
                    )
                    continue

                price = await asyncio.to_thread(get_current_price, sym)
                if price is None: continue
                m["last_price"] = price
                direction = m["direction"]
                target = m["target"]
                stop = m["stop"]

                if "target" not in m["alerts_sent"]:
                    hit = (direction == "call" and price >= target) or \
                          (direction == "put" and price <= target)
                    if hit:
                        m["status"] = "target_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("target")
                        # ✅ إصلاح النقطة 2
                        _purge_symbol_from_scans(sym)
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"✅ <b>تحقق الهدف</b> — {sym}\nالسعر: ${price:.2f}\nالهدف: ${target:.2f}"
                        )
                        continue

                if "stop" not in m["alerts_sent"]:
                    hit = (direction == "call" and price <= stop) or \
                          (direction == "put" and price >= stop)
                    if hit:
                        m["status"] = "stop_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("stop")
                        # ✅ إصلاح النقطة 2
                        _purge_symbol_from_scans(sym)
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"❌ <b>ضرب الوقف</b> — {sym}\nالسعر: ${price:.2f}\nالوقف: ${stop:.2f}"
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
            except Exception: pass

    ka = asyncio.create_task(keepalive())
    mt = asyncio.create_task(monitor_task())
    yield
    ka.cancel(); mt.cancel()
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
        "id": scan_id, "status": "starting",
        "total": len(SCAN_SYMBOLS), "completed": 0,
        "results": [], "results_map": {},
        "errors": [], "started_at": time.time(),
    }
    asyncio.create_task(run_scan(scan_id))
    return {"scan_id": scan_id, "total": len(SCAN_SYMBOLS)}


@app.get("/api/scan/{scan_id}")
def scan_status(scan_id: str):
    scan = _scans.get(scan_id)
    if not scan:
        return JSONResponse(status_code=404, content={"error": "scan not found"})
    results = []
    for r in scan["results"]:
        sym = r.get("symbol")
        m = _monitoring.get(sym, {})
        # ✅ إصلاح النقطة 3: لا نُرسل الصفقات الميتة
        if m.get("status") in DEAD_STATUSES:
            continue
        r_copy = dict(r)
        r_copy["monitor_status"] = m.get("status", "none")
        r_copy["monitor_price"] = m.get("last_price")
        r_copy["monitor_expires_at"] = m.get("expires_at")
        results.append(r_copy)
    return {
        "scan_id": scan_id, "status": scan["status"],
        "total": scan["total"], "completed": scan["completed"],
        "found": len(results), "results": results,
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


@app.get("/api/monitor/{symbol}")
def monitor_status(symbol: str):
    sym = symbol.upper().strip()
    m = _monitoring.get(sym)
    if not m:
        return {"monitored": False}
    return {
        "monitored": True, "symbol": sym, "status": m["status"],
        "entry": m["entry"], "target": m["target"], "stop": m["stop"],
        "last_price": m.get("last_price"),
        "expires_at": m["expires_at"], "direction": m["direction"],
    }


@app.post("/api/monitor/{symbol}/remove")
def monitor_remove(symbol: str):
    sym = symbol.upper().strip()
    if sym in _monitoring:
        _monitoring.pop(sym, None)
    _purge_symbol_from_scans(sym)
    return {"ok": True}


@app.get("/api/monitoring")
def get_monitoring():
    return {"count": len(_monitoring), "symbols": list(_monitoring.keys())}


@app.get("/api/cache-stats")
def cache_stats():
    return {
        "candle_cache_size": len(_candle_cache),
        "analyze_cache_size": len(_analyze_cache),
        "monitoring_count": len(_monitoring),
        "symbols_total": len(SCAN_SYMBOLS),
        "max_premium": MAX_PREMIUM,
        "max_spread": MAX_SPREAD,
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
