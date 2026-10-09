"""
analysis.py — Longbridge Options Radar
استراتيجية: اختراق متسلسل + Retest (Cascade Break & Retest)
"""
import numpy as np
import pandas as pd


# ============================================================
# المؤشرات
# ============================================================
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def rvol_series(df, n=20):
    avg = df["volume"].shift(1).rolling(n).mean()
    return df["volume"] / avg


# ============================================================
# أدوات
# ============================================================
MIN_RVOL = 1.5
MAX_RETEST_CANDLES = 5
RETEST_TOLERANCE_PCT = 0.3
BREAKOUT_LOOKBACK = 10


def prev_candle_hl(df, idx=-2):
    if df is None or len(df) < abs(idx):
        return None, None
    row = df.iloc[idx]
    return float(row["high"]), float(row["low"])


def last_price(df):
    if df is None or len(df) == 0:
        return None
    return float(df.iloc[-1]["close"])


# ============================================================
# 1. فحص الاختراق (بحجم)
# ============================================================
def check_breakout(df, level, direction, lookback=BREAKOUT_LOOKBACK, min_rvol=MIN_RVOL):
    if df is None or len(df) < 25 or level is None:
        return {"broken": False}

    rvol = rvol_series(df, 20)
    n = len(df)
    start = max(0, n - lookback)

    for i in range(n - 1, start - 1, -1):
        rv = rvol.iloc[i]
        if pd.isna(rv):
            continue
        rv = float(rv)
        row = df.iloc[i]

        if direction == "up":
            if float(row["close"]) > level and rv >= min_rvol:
                peak = float(df["high"].iloc[i:].max())
                return {
                    "broken": True,
                    "type": "up",
                    "level": float(level),
                    "break_index": i,
                    "break_close": float(row["close"]),
                    "rvol": round(rv, 2),
                    "peak_after": peak,
                    "candles_since": n - 1 - i,
                }
        else:
            if float(row["close"]) < level and rv >= min_rvol:
                trough = float(df["low"].iloc[i:].min())
                return {
                    "broken": True,
                    "type": "down",
                    "level": float(level),
                    "break_index": i,
                    "break_close": float(row["close"]),
                    "rvol": round(rv, 2),
                    "trough_after": trough,
                    "candles_since": n - 1 - i,
                }

    return {"broken": False}


# ============================================================
# 2. فحص Retest
# ============================================================
def check_retest(df, level, direction, max_candles=MAX_RETEST_CANDLES,
                 tolerance_pct=RETEST_TOLERANCE_PCT):
    if df is None or len(df) < 3 or level is None:
        return {"retested": False, "status": "no_data"}

    last = df.iloc[-1]
    last_close = float(last["close"])
    last_low   = float(last["low"])
    last_high  = float(last["high"])
    last_open  = float(last["open"])

    tol = level * (tolerance_pct / 100)

    if direction == "up":
        touched = last_low <= (level + tol)
        closed_above = last_close > level
        bullish = last_close > last_open
        if touched and closed_above and bullish:
            body = last_close - last_open
            full_range = max(last_high - last_low, 0.0001)
            body_ratio = body / full_range
            return {
                "retested": True,
                "status": "confirmed",
                "type": "up",
                "level": float(level),
                "close": last_close,
                "low": last_low,
                "high": last_high,
                "body_ratio": round(body_ratio, 2),
                "strong": body_ratio >= 0.6,
            }
        return {"retested": False, "status": "waiting"}
    else:
        touched = last_high >= (level - tol)
        closed_below = last_close < level
        bearish = last_close < last_open
        if touched and closed_below and bearish:
            body = last_open - last_close
            full_range = max(last_high - last_low, 0.0001)
            body_ratio = body / full_range
            return {
                "retested": True,
                "status": "confirmed",
                "type": "down",
                "level": float(level),
                "close": last_close,
                "low": last_low,
                "high": last_high,
                "body_ratio": round(body_ratio, 2),
                "strong": body_ratio >= 0.6,
            }
        return {"retested": False, "status": "waiting"}


# ============================================================
# 3. طرق الدخول الثلاث
# ============================================================
def detect_entry_methods(df, level, direction):
    methods = []
    if df is None or len(df) < 3 or level is None:
        return methods

    retest = check_retest(df, level, direction)
    if not retest.get("retested"):
        return methods

    last = df.iloc[-1]
    close = float(last["close"])

    methods.append({
        "name": "retest_close",
        "label": "إغلاق Retest",
        "entry": round(close, 2),
    })

    if retest.get("strong"):
        methods.append({
            "name": "pattern",
            "label": "نمط تأكيدي",
            "entry": round(close, 2),
        })

    if len(df) >= 2:
        retest_candle = df.iloc[-2]
        current = df.iloc[-1]
        if direction == "up":
            if float(current["close"]) > float(retest_candle["high"]):
                methods.append({
                    "name": "break_retest",
                    "label": "اختراق قمة Retest",
                    "entry": round(float(current["close"]), 2),
                })
        else:
            if float(current["close"]) < float(retest_candle["low"]):
                methods.append({
                    "name": "break_retest",
                    "label": "اختراق قاع Retest",
                    "entry": round(float(current["close"]), 2),
                })

    return methods


# ============================================================
# 4. لقطة الإطار (✅ محدّثة)
# ============================================================
def _tf_snapshot(df, label):
    """لقطة إطار: دعم/مقاومة خاصّة به + حالة الاختراق"""
    if df is None or len(df) < 5:
        return {
            "label": label,
            "support": None,
            "resistance": None,
            "broke_resistance": False,
            "broke_support": False,
        }

    sup, res = prev_candle_hl(df, -2)
    price = float(df.iloc[-1]["close"])

    broke_res = (res is not None) and (price > res)
    broke_sup = (sup is not None) and (price < sup)

    return {
        "label": label,
        "support": round(sup, 2) if sup else None,
        "resistance": round(res, 2) if res else None,
        "broke_resistance": broke_res,
        "broke_support": broke_sup,
    }


def _empty_result():
    return {
        "color": "gray",
        "label": "لا إشارة",
        "status": "none",
        "direction": None,
        "levels": {},
        "breakout_stage": None,
        "timeframes": [],
        "support_resistance": {},
    }


# ============================================================
# 5. الدالة الرئيسية
# ============================================================
def scan_setup(df_weekly, df_daily, df_4h, df_1h, df_15m):
    result = _empty_result()

    if df_weekly is None or df_daily is None:
        return result

    w_high, w_low = prev_candle_hl(df_weekly, -2)
    d_high, d_low = prev_candle_hl(df_daily, -2)
    h4_high, h4_low = prev_candle_hl(df_4h, -2)
    h1_high, h1_low = prev_candle_hl(df_1h, -2)

    # ═══ المرحلة 1: اليومي اخترق الأسبوعي ═══
    if w_high is not None:
        daily_break_up = check_breakout(df_daily, w_high, "up")
        if daily_break_up.get("broken"):
            retest = check_retest(df_4h, w_high, "up")
            methods = detect_entry_methods(df_4h, w_high, "up")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="call", stage="daily_break_weekly",
                    break_info=daily_break_up, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    if w_low is not None:
        daily_break_dn = check_breakout(df_daily, w_low, "down")
        if daily_break_dn.get("broken"):
            retest = check_retest(df_4h, w_low, "down")
            methods = detect_entry_methods(df_4h, w_low, "down")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="put", stage="daily_break_weekly",
                    break_info=daily_break_dn, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    # ═══ المرحلة 2: 4H اخترق اليومي ═══
    if d_high is not None:
        h4_break_up = check_breakout(df_4h, d_high, "up")
        if h4_break_up.get("broken"):
            retest = check_retest(df_1h, d_high, "up")
            methods = detect_entry_methods(df_1h, d_high, "up")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="call", stage="4h_break_daily",
                    break_info=h4_break_up, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    if d_low is not None:
        h4_break_dn = check_breakout(df_4h, d_low, "down")
        if h4_break_dn.get("broken"):
            retest = check_retest(df_1h, d_low, "down")
            methods = detect_entry_methods(df_1h, d_low, "down")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="put", stage="4h_break_daily",
                    break_info=h4_break_dn, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    # ═══ المرحلة 3: 1H اخترق 4H ═══
    if df_15m is None:
        df_15m = df_1h

    if h4_high is not None:
        h1_break_up = check_breakout(df_1h, h4_high, "up")
        if h1_break_up.get("broken"):
            retest = check_retest(df_15m, h4_high, "up")
            methods = detect_entry_methods(df_15m, h4_high, "up")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="call", stage="1h_break_4h",
                    break_info=h1_break_up, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    if h4_low is not None:
        h1_break_dn = check_breakout(df_1h, h4_low, "down")
        if h1_break_dn.get("broken"):
            retest = check_retest(df_15m, h4_low, "down")
            methods = detect_entry_methods(df_15m, h4_low, "down")
            if retest.get("retested") and methods:
                return _build_confirmed(
                    direction="put", stage="1h_break_4h",
                    break_info=h1_break_dn, retest=retest, methods=methods,
                    df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
                    w_high=w_high, w_low=w_low, d_high=d_high, d_low=d_low,
                    h4_high=h4_high, h4_low=h4_low, h1_high=h1_high, h1_low=h1_low,
                )

    # ═══ لا شيء — نعرض لقطات الإطارات ═══
    result["support_resistance"] = {
        "weekly": {"support": round(w_low, 2) if w_low else None,
                   "resistance": round(w_high, 2) if w_high else None},
        "daily": {"support": round(d_low, 2) if d_low else None,
                  "resistance": round(d_high, 2) if d_high else None},
        "4h":    {"support": round(h4_low, 2) if h4_low else None,
                  "resistance": round(h4_high, 2) if h4_high else None},
        "1h":    {"support": round(h1_low, 2) if h1_low else None,
                  "resistance": round(h1_high, 2) if h1_high else None},
    }

    result["timeframes"] = [
        _tf_snapshot(df_weekly, "1W"),
        _tf_snapshot(df_daily,  "1D"),
        _tf_snapshot(df_4h,     "4H"),
        _tf_snapshot(df_1h,     "1H"),
        _tf_snapshot(df_15m,    "15M"),
    ]

    return result


# ============================================================
# 6. بناء النتيجة المؤكدة
# ============================================================
def _build_confirmed(direction, stage, break_info, retest, methods,
                     df_w, df_d, df_4, df_1, df_15,
                     w_high, w_low, d_high, d_low, h4_high, h4_low,
                     h1_high, h1_low):
    best_method = methods[0]
    entry = best_method["entry"]
    level = retest["level"]

    if direction == "call":
        target = break_info.get("peak_after", entry)
    else:
        target = break_info.get("trough_after", entry)

    atr_val = float(atr(df_4, 14).iloc[-1])
    if direction == "call":
        stop = level - 0.5 * atr_val
        risk = entry - stop
        reward = target - entry
    else:
        stop = level + 0.5 * atr_val
        risk = stop - entry
        reward = entry - target

    rr = round(reward / risk, 2) if risk > 0 else 0

    color = "green" if direction == "call" else "red"
    label = "تأكيد CALL" if direction == "call" else "تأكيد PUT"

    result = {
        "color": color,
        "label": label,
        "status": "confirmed",
        "direction": direction,
        "breakout_stage": stage,
        "levels": {
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target1": round(target, 2),
            "rr": rr,
            "level_broken": round(level, 2),
        },
        "methods": methods,
        "break_info": break_info,
        "support_resistance": {
            "weekly": {"support": round(w_low, 2) if w_low else None,
                       "resistance": round(w_high, 2) if w_high else None},
            "daily": {"support": round(d_low, 2) if d_low else None,
                      "resistance": round(d_high, 2) if d_high else None},
            "4h":    {"support": round(h4_low, 2) if h4_low else None,
                      "resistance": round(h4_high, 2) if h4_high else None},
            "1h":    {"support": round(h1_low, 2) if h1_low else None,
                      "resistance": round(h1_high, 2) if h1_high else None},
        },
    }

    result["timeframes"] = [
        _tf_snapshot(df_w, "1W"),
        _tf_snapshot(df_d, "1D"),
        _tf_snapshot(df_4, "4H"),
        _tf_snapshot(df_1, "1H"),
        _tf_snapshot(df_15, "15M"),
    ]

    return result
