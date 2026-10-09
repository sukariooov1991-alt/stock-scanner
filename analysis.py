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
# ثوابت
# ============================================================
MIN_RVOL = 1.5
MIN_RR = 2.0                      # ✅ FIX #2: فلتر R:R إلزامي
MAX_RETEST_CANDLES = 5
RETEST_TOLERANCE_PCT = 0.3
BREAKOUT_LOOKBACK = 10


# ============================================================
# أدوات
# ============================================================
def prev_candle_hl(df, idx=-2):
    """تُرجع (high, low) لشمعة سابقة."""
    if df is None or len(df) < abs(idx):
        return None, None
    row = df.iloc[idx]
    return float(row["high"]), float(row["low"])


def last_price(df):
    if df is None or len(df) == 0:
        return None
    return float(df.iloc[-1]["close"])


def _break_time(df, idx):
    if df is None or "time" not in df.columns:
        return None
    try:
        return df["time"].iloc[idx]
    except Exception:
        return None


def _filter_after_time(df, since_time):
    """يُرجع الشموع بعد since_time فقط (بعد زمن الاختراق)."""
    if df is None or len(df) == 0 or since_time is None:
        return df
    if "time" not in df.columns:
        return df
    try:
        out = df[df["time"] > since_time].reset_index(drop=True)
        return out if len(out) > 0 else df
    except Exception:
        return df


# ============================================================
# 1. فحص الاختراق
# ============================================================
def check_breakout(df, level, direction, lookback=BREAKOUT_LOOKBACK, min_rvol=MIN_RVOL):
    """
    يفحص آخر `lookback` شمعة بحثاً عن اختراق مؤكد بالحجم.
    - peak_after / trough_after: أعلى/أدنى نقطة *بعد* شمعة الاختراق (لا تشملها).
    """
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
                # ✅ FIX #3: نستثني شمعة الاختراق نفسها من حساب الذروة
                after = df["high"].iloc[i + 1:]
                peak = float(after.max()) if len(after) > 0 else None
                return {
                    "broken": True,
                    "type": "up",
                    "level": float(level),
                    "break_index": i,
                    "break_close": float(row["close"]),
                    "break_time": _break_time(df, i),
                    "rvol": round(rv, 2),
                    "peak_after": peak,
                    "candles_since": n - 1 - i,
                }
        else:
            if float(row["close"]) < level and rv >= min_rvol:
                after = df["low"].iloc[i + 1:]
                trough = float(after.min()) if len(after) > 0 else None
                return {
                    "broken": True,
                    "type": "down",
                    "level": float(level),
                    "break_index": i,
                    "break_close": float(row["close"]),
                    "break_time": _break_time(df, i),
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
    """
    ✅ FIX #5: يفحص آخر max_candles شموع (وليس الأخيرة فقط).
    يُرجع retest_index (index داخل df) وإغلاق شمعة الـ Retest.
    ✅ FIX #6: max_candles مُستخدم فعلياً.
    ✅ FIX #10: strong يتطلب body_ratio ≥ 0.6 + إغلاق قوي في الاتجاه.
    """
    if df is None or len(df) < 3 or level is None:
        return {"retested": False, "status": "no_data"}

    n = len(df)
    start = max(0, n - max_candles)
    tol = level * (tolerance_pct / 100)

    for i in range(n - 1, start - 1, -1):
        row = df.iloc[i]
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        op = float(row["open"])
        full_range = max(high - low, 0.0001)

        if direction == "up":
            touched = low <= (level + tol)
            closed_above = close > level
            bullish = close > op
            if touched and closed_above and bullish:
                body = close - op
                body_ratio = body / full_range
                close_pos = (close - low) / full_range
                return {
                    "retested": True,
                    "status": "confirmed",
                    "type": "up",
                    "level": float(level),
                    "close": close,
                    "low": low,
                    "high": high,
                    "open": op,
                    "body_ratio": round(body_ratio, 2),
                    "strong": body_ratio >= 0.6 and close_pos >= 0.7,
                    "retest_index": i,
                    "candles_since_retest": n - 1 - i,
                }
        else:
            touched = high >= (level - tol)
            closed_below = close < level
            bearish = close < op
            if touched and closed_below and bearish:
                body = op - close
                body_ratio = body / full_range
                close_pos = (high - close) / full_range
                return {
                    "retested": True,
                    "status": "confirmed",
                    "type": "down",
                    "level": float(level),
                    "close": close,
                    "low": low,
                    "high": high,
                    "open": op,
                    "body_ratio": round(body_ratio, 2),
                    "strong": body_ratio >= 0.6 and close_pos >= 0.7,
                    "retest_index": i,
                    "candles_since_retest": n - 1 - i,
                }

    return {"retested": False, "status": "waiting"}


# ============================================================
# 3. طرق الدخول الثلاث
# ============================================================
def detect_entry_methods(df, level, direction):
    """
    1. إغلاق شمعة Retest (الافتراضي)
    2. نمط تأكيدي (جسم قوي)
    3. اختراق قمة/قاع شمعة Retest بشمعة لاحقة
    ✅ FIX #4: entry = إغلاق شمعة الـ Retest (وليس آخر شمعة).
    """
    methods = []
    if df is None or len(df) < 3 or level is None:
        return methods

    retest = check_retest(df, level, direction)
    if not retest.get("retested"):
        return methods

    retest_close = float(retest["close"])     # ✅ إغلاق شمعة Retest
    methods.append({
        "name": "retest_close",
        "label": "إغلاق Retest",
        "entry": round(retest_close, 2),
    })

    if retest.get("strong"):
        methods.append({
            "name": "pattern",
            "label": "نمط تأكيدي",
            "entry": round(retest_close, 2),
        })

    retest_idx = retest.get("retest_index", -1)
    if 0 <= retest_idx < len(df) - 1:
        retest_candle = df.iloc[retest_idx]
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
# 4. لقطة الإطار
# ============================================================
def _tf_snapshot(df, label):
    """✅ FIX #1: prev_candle_hl تُرجع (high, low) — التسميات صُححت."""
    if df is None or len(df) < 5:
        return {
            "label": label,
            "support": None,
            "resistance": None,
            "broke_resistance": False,
            "broke_support": False,
        }

    res, sup = prev_candle_hl(df, -2)          # ✅ الترتيب الصحيح
    price = float(df.iloc[-1]["close"])

    broke_res = (res is not None) and (price > res)
    broke_sup = (sup is not None) and (price < sup)

    return {
        "label": label,
        "support": round(sup, 2) if sup is not None else None,
        "resistance": round(res, 2) if res is not None else None,
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


def _sr_dict(w_low, w_high, d_low, d_high, h4_low, h4_high, h1_low, h1_high):
    return {
        "weekly": {"support": round(w_low, 2) if w_low else None,
                   "resistance": round(w_high, 2) if w_high else None},
        "daily":  {"support": round(d_low, 2) if d_low else None,
                   "resistance": round(d_high, 2) if d_high else None},
        "4h":     {"support": round(h4_low, 2) if h4_low else None,
                   "resistance": round(h4_high, 2) if h4_high else None},
        "1h":     {"support": round(h1_low, 2) if h1_low else None,
                   "resistance": round(h1_high, 2) if h1_high else None},
    }


def _tf_list(df_w, df_d, df_4, df_1, df_15):
    return [
        _tf_snapshot(df_w, "1W"),
        _tf_snapshot(df_d, "1D"),
        _tf_snapshot(df_4, "4H"),
        _tf_snapshot(df_1, "1H"),
        _tf_snapshot(df_15, "15M"),
    ]


# ============================================================
# 5. الدالة الرئيسية
# ============================================================
def scan_setup(df_weekly, df_daily, df_4h, df_1h, df_15m):
    result = _empty_result()

    if df_weekly is None or df_daily is None or df_4h is None or df_1h is None:
        return result

    w_high, w_low = prev_candle_hl(df_weekly, -2)
    d_high, d_low = prev_candle_hl(df_daily, -2)
    h4_high, h4_low = prev_candle_hl(df_4h, -2)
    h1_high, h1_low = prev_candle_hl(df_1h, -2)

    if df_15m is None:
        df_15m = df_1h

    sr = _sr_dict(w_low, w_high, d_low, d_high, h4_low, h4_high, h1_low, h1_high)

    # ═══ المرحلة 1: اليومي اخترق الأسبوعي → Retest على 4H ═══
    for direction, level, retest_df in (
        ("up",   w_high, df_4h),
        ("down", w_low,  df_4h),
    ):
        if level is None:
            continue
        brk = check_breakout(df_daily, level, direction)
        if not brk.get("broken"):
            continue
        # ✅ FIX #8: نُقصّ شموع الـ Retest لتبدأ بعد زمن الاختراق
        rdf = _filter_after_time(retest_df, brk.get("break_time"))
        if rdf is None or len(rdf) < 3:
            continue
        retest = check_retest(rdf, level, direction)
        if not retest.get("retested"):
            continue
        methods = detect_entry_methods(rdf, level, direction)
        if not methods:
            continue
        built = _build_confirmed(
            direction="call" if direction == "up" else "put",
            stage="daily_break_weekly",
            break_info=brk, retest=retest, methods=methods,
            df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
            sr=sr,
        )
        if built is not None:
            return built

    # ═══ المرحلة 2: 4H اخترق اليومي → Retest على 1H ═══
    for direction, level, retest_df in (
        ("up",   d_high, df_1h),
        ("down", d_low,  df_1h),
    ):
        if level is None:
            continue
        brk = check_breakout(df_4h, level, direction)
        if not brk.get("broken"):
            continue
        rdf = _filter_after_time(retest_df, brk.get("break_time"))
        if rdf is None or len(rdf) < 3:
            continue
        retest = check_retest(rdf, level, direction)
        if not retest.get("retested"):
            continue
        methods = detect_entry_methods(rdf, level, direction)
        if not methods:
            continue
        built = _build_confirmed(
            direction="call" if direction == "up" else "put",
            stage="4h_break_daily",
            break_info=brk, retest=retest, methods=methods,
            df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
            sr=sr,
        )
        if built is not None:
            return built

    # ═══ المرحلة 3: 1H اخترق 4H → Retest على 15m ═══
    for direction, level, retest_df in (
        ("up",   h4_high, df_15m),
        ("down", h4_low,  df_15m),
    ):
        if level is None:
            continue
        brk = check_breakout(df_1h, level, direction)
        if not brk.get("broken"):
            continue
        rdf = _filter_after_time(retest_df, brk.get("break_time"))
        if rdf is None or len(rdf) < 3:
            continue
        retest = check_retest(rdf, level, direction)
        if not retest.get("retested"):
            continue
        methods = detect_entry_methods(rdf, level, direction)
        if not methods:
            continue
        built = _build_confirmed(
            direction="call" if direction == "up" else "put",
            stage="1h_break_4h",
            break_info=brk, retest=retest, methods=methods,
            df_w=df_weekly, df_d=df_daily, df_4=df_4h, df_1=df_1h, df_15=df_15m,
            sr=sr,
        )
        if built is not None:
            return built

    # ═══ لا شيء — لقطات الإطارات فقط ═══
    result["support_resistance"] = sr
    result["timeframes"] = _tf_list(df_weekly, df_daily, df_4h, df_1h, df_15m)
    return result


# ============================================================
# 6. بناء النتيجة المؤكدة
# ============================================================
def _build_confirmed(direction, stage, break_info, retest, methods,
                     df_w, df_d, df_4, df_1, df_15, sr):
    best_method = methods[0]
    entry = float(best_method["entry"])
    level = float(retest["level"])

    if direction == "call":
        target = break_info.get("peak_after")
    else:
        target = break_info.get("trough_after")

    if target is None:
        return None
    target = float(target)

    atr_series = atr(df_4, 14)
    atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0

    if direction == "call":
        stop = level - 0.5 * atr_val
        risk = entry - stop
        reward = target - entry
    else:
        stop = level + 0.5 * atr_val
        risk = stop - entry
        reward = entry - target

    if risk <= 0 or reward <= 0:
        return None

    rr = round(reward / risk, 2)

    # ✅ FIX #2: فلتر R:R إلزامي
    if rr < MIN_RR:
        return None

    color = "green" if direction == "call" else "red"
    label = "تأكيد CALL" if direction == "call" else "تأكيد PUT"

    return {
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
        "support_resistance": sr,
        "timeframes": _tf_list(df_w, df_d, df_4, df_1, df_15),
    }
