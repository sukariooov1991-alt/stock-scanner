"""
main.py — Longbridge Options Radar Scanner
النسخة النهائية — 4 فريمات، مساران، حذف الرمادي نهائياً
+ توحيد صريح للأوقات
+ رفض الإشارات الميتة
+ تحليل ذكي عبر Gemini (منفصل)
+ قائمة نظيفة: S&P 100 + Nasdaq 100 (ميجا + لارج كاب)
+ اختيار العقد: من الـ strikes الحقيقية، الأقرب لـ 2% OTM، مع تجربة بدائل
+ OI/Whales تُجلب دائماً (لا return مبكر)
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
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from longbridge.openapi import (
    Config, QuoteContext, Period, AdjustType,
    TradeSessions, SubType, PushQuote,
)

from market_time import candles_to_df

from analysis import (
    atr, find_retest,
    scan_setup,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ✅ Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS  = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
]
GEMINI_TIMEOUT = 12

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_analyze_cache: dict[str, tuple[float, dict]] = {}
_ANALYZE_TTL = 25

_ai_cache: dict[str, tuple[float, str]] = {}
_AI_CACHE_TTL = 1800

# ✅ فلاتر العقد
MAX_PREMIUM = 2.5
MAX_SPREAD  = 0.10
DTE_MIN     = 5
DTE_MAX     = 20
DTE_TARGET  = 10
OTM_TARGET  = 0.02          # الهدف: 2% OTM
OTM_MIN     = 0.005         # الحد الأدنى المسموح: 0.5% OTM
OTM_MAX     = 0.04          # الحد الأعلى المسموح: 4% OTM
MAX_CANDIDATES = 10         # كم strike نجرّب قبل الاستسلام

# ✅ فلتر السعر الأدنى
MIN_PRICE = 50.0

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
DEAD_STATUSES = ("stop_hit", "target_hit", "expired")


# ============================================================
# ✅ قائمة نظيفة: S&P 100 + Nasdaq 100 (ميجا + لارج كاب)
# ============================================================
SCAN_SYMBOLS = list(dict.fromkeys([
    # ── التكنولوجيا ──
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO",
    "TSLA", "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN",
    "CSCO", "IBM", "NOW", "INTU", "AMAT", "MU", "LRCX", "KLAC",
    "SNPS", "CDNS", "ANSS", "FTNT", "PANW", "CRWD", "DDOG", "ZS",
    "NET", "MDB", "TEAM", "WDAY", "ADSK", "ROP", "APH", "MSI",
    "TEL", "GLW", "KEYS", "HPQ", "DELL", "WDC", "STX", "NTAP",
    "SMCI", "ARM", "TER", "NXPI", "MCHP", "ON", "SWKS", "QRVO",
    "MPWR", "ENPH", "FSLR", "SNOW", "PLTR", "SHOP", "UBER", "LYFT",
    "ABNB", "DASH", "COIN", "PYPL", "SQ", "HOOD", "SOFI", "AFRM",
    "MRVL", "ADI", "MSCI", "FIS", "FI", "GPN", "JKHY", "FFIV",
    "AKAM", "JNPR", "VRSN", "CTSH", "INFY", "WIT", "EPAM", "GLOB",
    "SSNC", "TYL", "PTC",

    # ── الاتصالات والإعلام ──
    "NFLX", "DIS", "CMCSA", "CHTR", "TMUS", "VZ", "T", "EA",
    "TTWO", "RBLX", "PARA", "WBD", "FOXA", "FOX",

    # ── البنوك والمالية ──
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "BX",
    "KKR", "APO", "ARES", "OWL", "TPG", "CG", "USB", "PNC", "TFC",
    "MTB", "FITB", "HBAN", "RF", "KEY", "CFG", "STT", "BK",
    "NTRS", "PRU", "MET", "AFL", "ALL", "TRV", "AIG", "AJG",
    "AON", "MCO", "ICE", "CME", "NDAQ", "CBOE", "MKTX", "TW",
    "CINF", "WRB", "L", "RE", "GL", "CNA", "HIG", "ACGL", "EG",
    "AIZ", "SYF", "DFS", "ALLY", "COF", "AXP", "V", "MA",

    # ── الطاقة ──
    "XOM", "CVX", "COP", "EOG", "PXD", "DVN", "OXY", "HAL", "SLB",
    "BKR", "PSX", "VLO", "MPC", "KMI", "WMB", "OKE", "ET", "EPD",
    "PAA", "TRGP", "HES", "MRO", "APA", "CTRA", "FANG", "HESM",
    "DINO", "PBF", "DK", "PARR",

    # ── الرعاية الصحية ──
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT",
    "DHR", "BMY", "AMGN", "GILD", "VRTX", "REGN", "BIIB", "ILMN",
    "IDXX", "A", "MTD", "WAT", "RMD", "HOLX", "COO", "EW",
    "DXCM", "PODD", "ALGN", "ZBH", "TFX", "STE", "XRAY", "ALC",
    "CRL", "IQV", "PKI", "RVTY", "BIO", "TECH", "QGEN", "HUM",
    "HCA", "MCK", "ABC", "CAH", "DGX", "LH", "BAX", "BDX",
    "CI", "ELV", "CVS", "MOH", "CNC", "VEEV", "DOCS",

    # ── الاستهلاك ──
    "WMT", "COST", "HD", "LOW", "TGT", "KR", "SYY", "ADM",
    "GIS", "K", "HSY", "MKC", "CL", "KMB", "CHD", "EL",
    "YUM", "CMG", "DPZ", "WEN", "QSR", "DKNG", "MAR", "HLT",
    "H", "RCL", "CCL", "NCLH", "LUV", "DAL", "AAL", "UAL",
    "PG", "PEP", "KO", "MDLZ", "STZ", "TAP", "KHC",

    # ── التجزئة ──
    "ORLY", "AZO", "AAP", "GPC", "LKQ", "ULTA", "BBY", "DKS",
    "ROST", "BURL", "M",

    # ── الصناعة ──
    "GE", "BA", "CAT", "DE", "MMM", "HON", "LMT", "RTX", "GD",
    "NOC", "LHX", "HII", "TDG", "HEI", "TXT", "AXON", "WM",
    "RSG", "CMI", "PCAR", "EMR", "PH", "ROK", "DOV", "IR",
    "ITW", "ETN", "AME", "PWR", "GNRC", "VRT", "GEV",

    # ── النقل ──
    "UNP", "CSX", "NSC", "UPS", "FDX", "ODFL", "XPO", "CHRW",
    "EXPD", "JBHT", "KNX",

    # ── المرافق ──
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "XEL", "SRE", "PEG",
    "ED", "WEC", "ES", "AEE", "DTE", "PPL", "FE", "ETR", "CMS",
    "CNP", "NI", "AES", "NRG", "VST", "CEG", "PSEG", "PNW",
    "LNT", "EVRG", "OGE", "PCG", "EIX", "AWK", "WTRG",

    # ── مواد ──
    "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "NUE", "STLD",
    "DOW", "LYB", "PPG", "IFF", "ALB", "CE", "DD", "VMC",
    "MLM", "IP", "PKG",

    # ── عقارات ──
    "PLD", "AMT", "EQIX", "CCI", "PSA", "O", "SPG", "VICI",
    "WELL", "DLR", "AVB", "EQR", "MAA", "ESS", "UDR", "CPT",
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


# ============================================================
# Gemini
# ============================================================
def _build_ai_prompt(data: dict) -> str:
    sym = data.get("symbol", "")
    card = data.get("card", {}) or {}
    lv = data.get("levels", {}) or {}
    bi = data.get("break_info", {}) or {}
    tfs = data.get("timeframes", []) or []

    tf_lines = []
    for t in tfs:
        if t.get("broke_resistance"):
            st = "مخترق صعوداً"
        elif t.get("broke_support"):
            st = "مكسور هبوطاً"
        else:
            st = "محايد"
        tf_lines.append(
            f"- {t.get('label')}: مقاومة {t.get('resistance')} / "
            f"دعم {t.get('support')} / {st}"
        )

    direction_txt = "CALL (صاعد)" if card.get("color") == "green" else "PUT (هابط)"
    stage_txt = {
        "daily_break_weekly": "إغلاق يومي خارج قمة/قاع الأسبوع السابق",
        "4h_break_daily": "إغلاق 4H خارج قمة/قاع اليوم السابق",
    }.get(card.get("stage"), "—")

    return f"""أنت محلل فني محترف لأسواق الأسهم والخيارات الأمريكية.
حلّل هذه الإشارة الفنية بإيجاز شديد في 3-4 أسطر عربية فقط.
ركّز على: قوة الزخم، جودة الاختراق، الثبات، السياق العام، وأهم مخاطبة أو مخاطرة.
لا تكرر الأرقام حرفياً، ولا تستخدم Markdown، ولا رموز تعبيرية، ولا عناوين.
اكتب نصاً متصلاً كأنك تخاطب متداولاً محترفاً.

البيانات:
- الرمز: {sym}
- الاتجاه: {direction_txt}
- المرحلة: {stage_txt}
- مستوى الاختراق: {lv.get('level_broken')}
- سعر الدخول: {lv.get('entry')}
- الوقف: {lv.get('stop')}
- الهدف: {lv.get('target1')}
- R:R: {lv.get('rr')}
- RVOL عند الاختراق: {bi.get('rvol')}
- نمط شمعة التأكيد: {lv.get('pattern')}
- السعر الحالي: {data.get('price')}

الفريمات:
{chr(10).join(tf_lines)}

اكتب 3-4 أسطر فقط.
"""


def _call_gemini_once(prompt: str, model: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/"
               f"models/{model}:generateContent?key={GEMINI_API_KEY}")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 260,
                "topP": 0.95,
            },
        }
        r = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        if r.status_code != 200:
            print(f"[GEMINI] {model} HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        d = r.json()
        if d.get("promptFeedback", {}).get("blockReason"):
            print(f"[GEMINI] {model} blocked: {d['promptFeedback']['blockReason']}", flush=True)
            return None
        cands = d.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        print(f"[GEMINI] {model} exception: {e}", flush=True)
        return None


def get_ai_analysis(data: dict) -> str | None:
    sym = (data.get("symbol") or "").upper().strip()
    if not GEMINI_API_KEY or not sym:
        return None
    now = time.time()
    cached = _ai_cache.get(sym)
    if cached and now - cached[0] < _AI_CACHE_TTL:
        return cached[1]
    prompt = _build_ai_prompt(data)
    for model in GEMINI_MODELS:
        text = _call_gemini_once(prompt, model)
        if text:
            _ai_cache[sym] = (now, text)
            return text
    return None


# تحويل الشموع موجود في market_time.py ويستخدم تقويم جلسات NYSE الفعلي.


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
                                trade_sessions=TradeSessions.Intraday)

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


def _quote_one(ctx, sym_opt):
    """يجلب اقتباس عقد واحد، يعيد dict أو None."""
    try:
        oqs = ctx.option_quote([sym_opt])
        if not oqs:
            return None
        oq = oqs[0]
        last = None
        for attr in ("last_done", "last", "price"):
            v = getattr(oq, attr, None)
            if v is not None:
                try:
                    last = float(v); break
                except (TypeError, ValueError):
                    continue
        bid = float(getattr(oq, "bid", 0) or 0)
        ask = float(getattr(oq, "ask", 0) or 0)
        return {"last": last, "bid": bid, "ask": ask, "raw": oq}
    except Exception:
        return None


def _try_strike(ctx, opt_sym):
    """يجرّب عقداً واحداً، يعيد (pass, premium, spread, delta, reason)."""
    if not opt_sym:
        return False, None, None, None, "no_symbol"
    q = _quote_one(ctx, opt_sym)
    if not q:
        return False, None, None, None, "no_quote"
    last = q["last"]; ask = q["ask"]; bid = q["bid"]
    spread = (ask - bid) if ask > bid > 0 else 999
    entry = ask if ask > 0 else last
    delta = None
    if hasattr(q["raw"], "delta"):
        try: delta = round(float(q["raw"].delta), 3)
        except Exception: pass
    if entry is None:
        return False, None, spread, delta, "no_premium"
    if entry > MAX_PREMIUM:
        return False, round(entry, 2), spread, delta, "premium_too_high"
    if spread > MAX_SPREAD:
        return False, round(entry, 2), spread, delta, "spread_too_wide"
    return True, round(entry, 2), spread, delta, "ok"


def fetch_option_data(symbol, direction, price, strategy="swing"):
    """
    يجلب بيانات الخيارات:
    - يجرّب أقرب الـ strikes الحقيقية إلى 2% OTM ضمن نطاق 0.5%–4%
    - يقبل أول عقد يستوفي الفلاتر (سعر + سبريد)
    - يجلب OI/Whales دائماً (بغض النظر عن نجاح العقد)
    """
    ctx = get_ctx()
    sym = norm(symbol)
    result = _empty_option_result()
    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates:
            result["filter_reason"] = "no_dates"
            return result

        # تاريخ DTE يُحسب حسب جلسة السوق الأمريكي، لا حسب UTC حتى لا ينحرف يومًا.
        today = datetime.now(ZoneInfo("America/New_York")).date()
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
        target_price = price * (1 + OTM_TARGET) if is_call else price * (1 - OTM_TARGET)

        # اجمع strikes الحقيقية داخل النطاق المتفق عليه 0.5%–4% OTM فقط.
        # لا نسمح بالانزلاق إلى عقد أبعد من النطاق لمجرد أن سعره/سبريده مناسب.
        cands = []
        for c in chain:
            strike = _strike_of(c)
            opt_sym = _call_of(c) if is_call else _put_of(c)
            if not opt_sym or strike is None or price <= 0:
                continue
            otm_pct = ((strike - price) / price) if is_call else ((price - strike) / price)
            if OTM_MIN <= otm_pct <= OTM_MAX:
                cands.append(c)

        if not cands:
            result["filter_reason"] = "no_call_strike_in_otm_range" if is_call else "no_put_strike_in_otm_range"
            # نكمل لجلب OI/Whales، لكن لا نختار عقدًا خارج النطاق.
        else:
            # الأقرب إلى 2% OTM أولاً، مع تجربة البدائل المقبولة فقط.
            cands.sort(key=lambda c: abs(_strike_of(c) - target_price))

            # ✅ 3) جرّب كل مرشح حتى نجد عقداً مقبولاً
            chosen = None
            for c in cands[:MAX_CANDIDATES]:
                sk = _strike_of(c)
                opt_sym = _call_of(c) if is_call else _put_of(c)
                if not opt_sym:
                    continue
                ok, prem, spread, delta, reason = _try_strike(ctx, opt_sym)
                if ok:
                    chosen = {"strike": sk, "sym": opt_sym,
                              "premium": prem, "delta": delta}
                    result["filter_pass"] = True
                    break
                else:
                    # سجّل آخر سبب فشل
                    result["filter_reason"] = f"{reason} @ {int(sk)}"

            if chosen:
                result["strike"] = f"{'C' if is_call else 'P'} {int(chosen['strike'])}"
                result["premium"] = chosen["premium"]
                if chosen["delta"] is not None:
                    result["delta"] = chosen["delta"]
                # احتفظ بالرمز لاستخدامه لاحقاً في التحليل
                result["_opt_sym"] = chosen["sym"]
            else:
                # لم نجد عقداً مقبولاً — لكن نكمل
                result["filter_reason"] = result["filter_reason"] or "no_suitable_contract"

        # ✅ 4) اجلب OI/Volume/Whales دائماً
        strikes_map = {}
        for c in chain:
            sk = _strike_of(c)
            if sk <= 0:
                continue
            cs = _call_of(c) or build_call_sym(sk)
            ps = _put_of(c)  or build_put_sym(sk)
            strikes_map[sk] = (cs, ps)

        # للعرض: 5 CALL فوق السعر + 5 PUT تحت السعر
        calls_above = sorted([s for s in strikes_map.keys() if s > price])[:NEARBY_STRIKES]
        puts_below  = sorted([s for s in strikes_map.keys() if s < price],
                              reverse=True)[:NEARBY_STRIKES]

        display_strikes = sorted(set(calls_above + puts_below))

        # اجمع كل الرموز المطلوبة
        all_syms_set = set()
        for sk in display_strikes:
            cs, ps = strikes_map.get(sk, (None, None))
            if cs: all_syms_set.add(cs)
            if ps: all_syms_set.add(ps)

        all_syms = list(all_syms_set)
        qmap = {}
        for i in range(0, len(all_syms), OPTION_QUOTE_BATCH):
            try:
                qs = ctx.option_quote(all_syms[i:i+OPTION_QUOTE_BATCH])
                if qs:
                    for q in qs: qmap[q.symbol] = q
            except Exception: pass

        # إجماليات CALL / PUT (من عيّنة العرض)
        tc_oi = tp_oi = tc_v = tp_v = 0
        cd = []
        for sk in sorted(display_strikes, reverse=True):
            cs, ps = strikes_map.get(sk, (None, None))
            if cs and cs in qmap:
                q = qmap[cs]
                oi = int(getattr(q, "open_interest", 0) or 0)
                vol = int(getattr(q, "volume", 0) or 0)
                tc_oi += oi; tc_v += vol
                cd.append({"strike": int(sk), "oi": oi, "volume": vol})
            if ps and ps in qmap:
                q = qmap[ps]
                oi = int(getattr(q, "open_interest", 0) or 0)
                vol = int(getattr(q, "volume", 0) or 0)
                tp_oi += oi; tp_v += vol

        pd_ = []
        for sk in sorted(display_strikes, reverse=True):
            cs, ps = strikes_map.get(sk, (None, None))
            if ps and ps in qmap:
                q = qmap[ps]
                pd_.append({"strike": int(sk),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0)})

        result["total_call_oi"]  = tc_oi
        result["total_put_oi"]   = tp_oi
        result["total_call_vol"] = tc_v
        result["total_put_vol"]  = tp_v
        result["call_oi"] = cd
        result["put_oi"]  = pd_

        # الحيتان
        whales = []
        for sk in display_strikes:
            cs, ps = strikes_map.get(sk, (None, None))
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

    result.pop("_opt_sym", None)
    return result


# ============================================================
# رفض الإشارات الميتة
# ============================================================
def _gray_result(symbol: str, price: float) -> dict:
    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "card": {"color": "gray", "label": "لا إشارة",
                 "status": "none", "direction": None, "stage": None},
    }


def _is_dead_signal(scan: dict, price: float) -> bool:
    if scan.get("color") not in VALID_COLORS:
        return False
    lv = scan.get("levels", {}) or {}
    stop_p = lv.get("stop"); tgt_p = lv.get("target1")
    if stop_p is None or tgt_p is None:
        return False
    try:
        sp = float(stop_p); tp = float(tgt_p)
    except (TypeError, ValueError):
        return False
    if scan["color"] == "green":
        return (price <= sp) or (price >= tp)
    return (price >= sp) or (price <= tp)


# ============================================================
# analyze_symbol — مع فلتر السعر
# ============================================================
def analyze_symbol(symbol: str) -> dict:
    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    # ✅ رفض الأسهم الرخيصة (قبل جلب الشموع — توفير كبير)
    if price < MIN_PRICE:
        return _gray_result(symbol, price)

    df_weekly = candles_to_df(fetch_candles(symbol, "1w", 150), "1w")
    df_daily  = candles_to_df(fetch_candles(symbol, "1d", 400), "1d")
    df_4h     = candles_to_df(fetch_candles(symbol, "4h", 200), "4h")
    df_1h     = candles_to_df(fetch_candles(symbol, "1h", 200), "1h")

    scan = scan_setup(df_weekly, df_daily, df_4h, df_1h)

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
        "break_info": scan.get("break_info", {}),
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
                    _purge_symbol_from_scans(sym)
                    await asyncio.to_thread(
                        send_telegram_alert,
                        f"⏰ <b>انتهت المدة</b> — {sym}\nمرت {MONITOR_DAYS} أيام."
                    )
                    continue

                price = await asyncio.to_thread(get_current_price, sym)
                if price is None: continue
                m["last_price"] = price
                direction = m["direction"]; target = m["target"]; stop = m["stop"]

                if "target" not in m["alerts_sent"]:
                    hit = (direction == "call" and price >= target) or \
                          (direction == "put" and price <= target)
                    if hit:
                        m["status"] = "target_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("target")
                        _purge_symbol_from_scans(sym)
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"✅ <b>تحقق الهدف</b> — {sym}\nالسعر: ${price:.2f}\nالهدف: ${target:.2f}")
                        continue

                if "stop" not in m["alerts_sent"]:
                    hit = (direction == "call" and price <= stop) or \
                          (direction == "put" and price >= stop)
                    if hit:
                        m["status"] = "stop_hit"
                        m["ended_at"] = now
                        m["alerts_sent"].add("stop")
                        _purge_symbol_from_scans(sym)
                        await asyncio.to_thread(
                            send_telegram_alert,
                            f"❌ <b>ضرب الوقف</b> — {sym}\nالسعر: ${price:.2f}\nالوقف: ${stop:.2f}")
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
        print(f"[STARTUP] symbols={len(SCAN_SYMBOLS)} min_price=${MIN_PRICE}", flush=True)
        if GEMINI_API_KEY:
            print(f"[STARTUP] Gemini enabled — models: {GEMINI_MODELS}", flush=True)
        else:
            print("[STARTUP] Gemini key MISSING — AI analysis disabled", flush=True)
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


@app.get("/api/test-gemini")
def test_gemini():
    if not GEMINI_API_KEY:
        return {"ok": False, "reason": "GEMINI_API_KEY not set",
                "models_tried": GEMINI_MODELS}
    sample = {
        "symbol": "TEST",
        "price": 100.0,
        "card": {"color": "green", "stage": "daily_break_weekly"},
        "levels": {"entry": 100, "stop": 98, "target1": 104, "rr": 2.0,
                   "level_broken": 99, "pattern": "hammer"},
        "break_info": {"rvol": 1.8},
        "timeframes": [
            {"label": "1W", "support": 95, "resistance": 99, "broke_resistance": True},
            {"label": "1D", "support": 97, "resistance": 99, "broke_resistance": True},
            {"label": "4H", "support": 98, "resistance": 101, "broke_resistance": False},
            {"label": "1H", "support": 99, "resistance": 102, "broke_resistance": False},
        ],
    }
    text = get_ai_analysis(sample)
    return {"ok": bool(text), "text": text, "models_tried": GEMINI_MODELS}


@app.get("/api/ai/{symbol}")
async def ai_analysis(symbol: str):
    if not GEMINI_API_KEY:
        return {"ok": False, "reason": "no_key"}
    sym = symbol.upper().strip()
    if not sym:
        return {"ok": False, "reason": "no_symbol"}

    cached = _ai_cache.get(sym)
    if cached and time.time() - cached[0] < _AI_CACHE_TTL:
        return {"ok": True, "text": cached[1], "cached": True}

    try:
        data = await asyncio.to_thread(analyze_cached, sym)
    except Exception as e:
        return {"ok": False, "reason": f"analyze_error: {e}"}

    if data.get("card", {}).get("color") not in VALID_COLORS:
        return {"ok": False, "reason": "no_signal"}

    try:
        text = await asyncio.to_thread(get_ai_analysis, data)
    except Exception as e:
        return {"ok": False, "reason": f"ai_error: {e}"}

    return {"ok": bool(text), "text": text}


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
        "ai_cache_size": len(_ai_cache),
        "monitoring_count": len(_monitoring),
        "symbols_total": len(SCAN_SYMBOLS),
        "min_price": MIN_PRICE,
        "max_premium": MAX_PREMIUM,
        "max_spread": MAX_SPREAD,
        "dte_min": DTE_MIN,
        "dte_max": DTE_MAX,
        "dte_target": DTE_TARGET,
        "otm_min": OTM_MIN,
        "otm_max": OTM_MAX,
        "otm_target": OTM_TARGET,
        "gemini_enabled": bool(GEMINI_API_KEY),
        "gemini_models": GEMINI_MODELS,
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
