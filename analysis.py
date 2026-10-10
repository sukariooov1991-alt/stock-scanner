"""
analysis.py — استراتيجية الاختراق وإعادة الاختبار (النسخة النهائية)
مساران فقط — Retest على 4H — مهلة 5 شموع
"""
import numpy as np
import pandas as pd

# ============================================================
# ثوابت
# ============================================================
MIN_RVOL = 1.5
MIN_RR = 2.0
MAX_RETEST_CANDLES = 5
BODY_RATIO_MIN = 0.6
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
TOLERANCE_PCT = 0.003
TOLERANCE_ATR_MULT = 0.25
BREAKOUT_LOOKBACK = 10


# ============================================================
# مؤشرات
# ============================================================
def atr(df, n=ATR_PERIOD):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def rvol_series(df, n=20):
    avg = df["volume"].shift(1).rolling(n).mean()
    return df["volume"] / avg


# ============================================================
# أدوات
# ============================================================
def prev_candle_hl(df, idx=-2):
    if df is None or len(df) < abs(idx):
        return None, None
    row = df.iloc[idx]
    return float(row["high"]), float(row["low"])


def _time_of(df, idx):
    if df is None or "time" not in df.columns:
        return None
    try:
        return df["time"].iloc[idx]
    except Exception:
        return None


def _body_ratio(row):
    rng = max(float(row["high"]) - float(row["low"]), 0.0001)
    return abs(float(row["close"]) - float(row["open"])) / rng


def _is_bullish(row):
    return float(row["close"]) > float(row["open"])


def _is_bearish(row):
    return float(row["close"]) < float(row["open"])


def _high_since(df, since_time):
    if since_time is None or df is None or "time" not in df.columns:
        return None
    try:
        sub = df[df["time"] > since_time]
        if len(sub) == 0:
            return None
        return float(sub["high"].max())
    except Exception:
        return None


def _low_since(df, since_time):
    if since_time is None or df is None or "time" not in df.columns:
        return None
    try:
        sub = df[df["time"] > since_time]
        if len(sub) == 0:
            return None
        return float(sub["low"].min())
    except Exception:
        return None


def compute_tolerance(level, atr_val):
    pct_part = abs(level) * TOLERANCE_PCT
    atr_part = atr_val * TOLERANCE_ATR_MULT
    return min(pct_part, atr_part)


# ============================================================
# 1. فحص الاختراق
# ============================================================
def check_breakout(df, level, direction, lookback=BREAKOUT_LOOKBACK):
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
        if rv < MIN_RVOL:
            continue

        row = df.iloc[i]
        close = float(row["close"])

        if direction == "up" and close > level:
            return {
                "broken": True,
                "type": "up",
                "level": float(level),
                "break_index": i,
                "break_time": _time_of(df, i),
                "break_close": close,
                "rvol": round(rv, 2),
            }
        if direction == "down" and close < level:
            return {
                "broken": True,
                "type": "down",
                "level": float(level),
                "break_index": i,
                "break_time": _time_of(df, i),
                "break_close": close,
                "rvol": round(rv, 2),
            }

    return {"broken": False}


# ============================================================
# 2. فحص Retest
# ============================================================
def find_retest(df, level, direction, break_time=None,
                atr_val=None, max_candles=MAX_RETEST_CANDLES):
    if df is None or len(df) < 5 or level is None:
        return {"retested": False, "status": "no_data"}

    if atr_val is None or atr_val <= 0:
        atr_val = float(atr(df, ATR_PERIOD).iloc[-1])
    tol = compute_tolerance(level, atr_val)

    # حدد فهرس البداية (أول شمعة بعد الاختراق)
    if break_time is not None and "time" in df.columns:
        try:
            after = df.index[df["time"] > break_time].tolist()
            if not after:
                return {"retested": False, "status": "no_candles_after"}
            start_idx = after[0]
        except Exception:
            start_idx = max(0, len(df) - max_candles)
    else:
        start_idx = max(0, len(df) - max_candles)

    end_idx = min(len(df) - 1, start_idx + max_candles - 1)

    for i in range(start_idx, end_idx + 1):
        row = df.iloc[i]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if direction == "up":
            touched = low <= (level + tol)
            if touched and close > level:
                return {
                    "retested": True, "status": "ok", "type": "up",
                    "level": float(level), "retest_idx": i,
                    "retest_time": _time_of(df, i),
                    "retest_high": high, "retest_low": low,
                    "retest_open": float(row["open"]),
                    "retest_close": close,
                    "tolerance": round(tol, 4),
                    "candles_since_break": i - start_idx + 1,
                }
        else:
            touched = high >= (level - tol)
            if touched and close < level:
                return {
                    "retested": True, "status": "ok", "type": "down",
                    "level": float(level), "retest_idx": i,
                    "retest_time": _time_of(df, i),
                    "retest_high": high, "retest_low": low,
                    "retest_open": float(row["open"]),
                    "retest_close": close,
                    "tolerance": round(tol, 4),
                    "candles_since_break": i - start_idx + 1,
                }

    return {"retested": False, "status": "no_retest_in_window"}


# ============================================================
# 3. فحص الثبات
# ============================================================
def check_stability(df, level, direction, from_idx):
    if df is None or from_idx >= len(df) - 1:
        return {"stable": True, "reason": "no_candles_after"}

    for i in range(from_idx + 1, len(df)):
        close = float(df.iloc[i]["close"])
        if direction == "up" and close < level:
            return {"stable": False,
                    "reason": f"close_below_at_{i}",
                    "violation_idx": i}
        if direction == "down" and close > level:
            return {"stable": False,
                    "reason": f"close_above_at_{i}",
                    "violation_idx": i}

    return {"stable": True, "reason": "no_violation"}


# ============================================================
# 4. المسار الأول: الشمعة الثانية
# ============================================================
def find_second_candle(df, level, direction, retest_idx):
    if df is None or retest_idx >= len(df) - 2:
        return {"found": False, "reason": "no_room"}

    first_idx = None
    for i in range(retest_idx + 1, len(df)):
        close = float(df.iloc[i]["close"])
        if direction == "up" and close > level:
            first_idx = i
            break
        if direction == "down" and close < level:
            first_idx = i
            break

    if first_idx is None:
        return {"found": False, "reason": "no_first"}

    second_idx = first_idx + 1
    if second_idx >= len(df):
        return {"found": False, "reason": "no_second"}

    row2 = df.iloc[second_idx]
    close2 = float(row2["close"])

    if direction == "up" and close2 <= level:
        return {"found": False, "reason": "second_wrong_side"}
    if direction == "down" and close2 >= level:
        return {"found": False, "reason": "second_wrong_side"}

    return {
        "found": True,
        "first_idx": first_idx,
        "second_idx": second_idx,
        "entry": close2,
        "entry_time": _time_of(df, second_idx),
        "body_ratio": round(_body_ratio(row2), 2),
    }


# ============================================================
# 5. المسار الثاني: تأكيد 1H
# ============================================================
def find_confirmation_1h(df_1h, retest_info, direction):
    if df_1h is None or len(df_1h) < 2 or not retest_info:
        return {"confirmed": False}

    retest_high = retest_info.get("retest_high")
    retest_low = retest_info.get("retest_low")
    retest_time = retest_info.get("retest_time")

    if retest_time is not None and "time" in df_1h.columns:
        try:
            candidates = df_1h.index[df_1h["time"] > retest_time].tolist()
        except Exception:
            candidates = list(range(len(df_1h)))
    else:
        candidates = list(range(len(df_1h)))

    if not candidates:
        return {"confirmed": False, "reason": "no_1h_after_retest"}

    for i in candidates:
        row = df_1h.iloc[i]
        close = float(row["close"])
        if direction == "up" and retest_high is not None and close > retest_high:
            return {"confirmed": True, "confirm_idx": i,
                    "entry": close,
                    "confirm_time": _time_of(df_1h, i),
                    "body_ratio": round(_body_ratio(row), 2)}
        if direction == "down" and retest_low is not None and close < retest_low:
            return {"confirmed": True, "confirm_idx": i,
                    "entry": close,
                    "confirm_time": _time_of(df_1h, i),
                    "body_ratio": round(_body_ratio(row), 2)}

    return {"confirmed": False, "reason": "no_confirmation"}


# ============================================================
# 6. كشف نمط الشمعة (وصفي)
# ============================================================
def detect_pattern(df, idx, direction):
    if df is None or idx < 1 or idx >= len(df):
        return "none"
    cur = df.iloc[idx]
    prev = df.iloc[idx - 1]
    cur_o = float(cur["open"]); cur_c = float(cur["close"])
    cur_h = float(cur["high"]); cur_l = float(cur["low"])
    prev_o = float(prev["open"]); prev_c = float(prev["close"])
    br = _body_ratio(cur)

    if direction == "up":
        if (_is_bearish(prev) and _is_bullish(cur)
                and cur_o <= prev_c and cur_c >= prev_o):
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0:
            lower_wick = min(cur_o, cur_c) - cur_l
            if lower_wick >= 2 * br * rng and br < 0.4:
                return "hammer"
    else:
        if (_is_bullish(prev) and _is_bearish(cur)
                and cur_o >= prev_c and cur_c <= prev_o):
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0:
            upper_wick = cur_h - max(cur_o, cur_c)
            if upper_wick >= 2 * br * rng and br < 0.4:
                return "shooting_star"

    return "none"


# ============================================================
# 7. بناء النتيجة
# ============================================================
def _build_result(direction, stage, entry, target1, target2, stop,
                  level, retest_info, break_info, sr_dict, tfs, pattern=""):
    risk = abs(entry - stop)
    reward = abs(target1 - entry)

    if risk <= 0 or reward <= 0:
        return None

    rr = round(reward / risk, 2)
    if rr < MIN_RR:
        return None

    color = "green" if direction == "up" else "red"
    label = "تأكيد CALL" if direction == "up" else "تأكيد PUT"

    levels = {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target1": round(target1, 2),
        "rr": rr,
        "level_broken": round(level, 2),
        "pattern": pattern,
    }
    if target2 is not None:
        levels["target2"] = round(target2, 2)

    return {
        "color": color,
        "label": label,
        "status": "confirmed",
        "direction": "call" if direction == "up" else "put",
        "breakout_stage": stage,
        "levels": levels,
        "break_info": break_info,
        "retest_info": retest_info,
        "support_resistance": sr_dict,
        "timeframes": tfs,
    }


def _tf_snapshot(df, label):
    if df is None or len(df) < 5:
        return {"label": label, "support": None, "resistance": None,
                "broke_resistance": False, "broke_support": False}
    res, sup = prev_candle_hl(df, -2)
    price = float(df.iloc[-1]["close"])
    return {
        "label": label,
        "support": round(sup, 2) if sup is not None else None,
        "resistance": round(res, 2) if res is not None else None,
        "broke_resistance": (res is not None) and (price > res),
        "broke_support": (sup is not None) and (price < sup),
    }


# ============================================================
# 8. الدالة الرئيسية
# ============================================================
def scan_setup(df_weekly, df_daily, df_4h, df_1h):
    empty = {
        "color": "gray", "label": "لا إشارة", "status": "none",
        "direction": None, "breakout_stage": None,
        "levels": {}, "timeframes": [], "support_resistance": {},
    }

    if any(d is None or len(d) < 25 for d in
           (df_weekly, df_daily, df_4h, df_1h)):
        return empty

    w_high, w_low = prev_candle_hl(df_weekly, -2)
    d_high, d_low = prev_candle_hl(df_daily, -2)

    sr_dict = {
        "weekly": {"support": round(w_low, 2) if w_low else None,
                   "resistance": round(w_high, 2) if w_high else None},
        "daily":  {"support": round(d_low, 2) if d_low else None,
                   "resistance": round(d_high, 2) if d_high else None},
    }

    tfs = [
        _tf_snapshot(df_weekly, "1W"),
        _tf_snapshot(df_daily, "1D"),
        _tf_snapshot(df_4h, "4H"),
        _tf_snapshot(df_1h, "1H"),
    ]

    atr_4h = float(atr(df_4h, ATR_PERIOD).iloc[-1])
    atr_1h = float(atr(df_1h, ATR_PERIOD).iloc[-1])

    # ══════════════════════════════════════════════
    # المسار الأول: اليومي اخترق الأسبوع → Retest 4H → شمعة ثانية 4H
    # ══════════════════════════════════════════════
    for direction, level in (("up", w_high), ("down", w_low)):
        if level is None:
            continue

        brk = check_breakout(df_daily, level, direction)
        if not brk["broken"]:
            continue

        retest = find_retest(df_4h, level, direction,
                              break_time=brk["break_time"], atr_val=atr_4h)
        if not retest["retested"]:
            continue

        stab = check_stability(df_4h, level, direction, retest["retest_idx"])
        if not stab["stable"]:
            continue

        second = find_second_candle(df_4h, level, direction, retest["retest_idx"])
        if not second["found"]:
            continue

        entry = second["entry"]
        pattern = detect_pattern(df_4h, second["second_idx"], direction)

        # الهدف: أعلى/أدنى نقطة على 4H منذ زمن الاختراق
        if direction == "up":
            target1 = _high_since(df_4h, brk["break_time"]) or entry
            stop = retest["retest_low"] - ATR_MULTIPLIER * atr_4h
        else:
            target1 = _low_since(df_4h, brk["break_time"]) or entry
            stop = retest["retest_high"] + ATR_MULTIPLIER * atr_4h

        res = _build_result(
            direction=direction, stage="daily_break_weekly",
            entry=entry, target1=target1, target2=None,
            stop=stop, level=level, retest_info=retest,
            break_info=brk, sr_dict=sr_dict, tfs=tfs, pattern=pattern,
        )
        if res:
            return res

    # ══════════════════════════════════════════════
    # المسار الثاني: 4H اخترق اليوم → Retest 4H → تأكيد 1H
    # ══════════════════════════════════════════════
    for direction, level in (("up", d_high), ("down", d_low)):
        if level is None:
            continue

        brk = check_breakout(df_4h, level, direction)
        if not brk["broken"]:
            continue

        retest = find_retest(df_4h, level, direction,
                              break_time=brk["break_time"], atr_val=atr_4h)
        if not retest["retested"]:
            continue

        stab = check_stability(df_4h, level, direction, retest["retest_idx"])
        if not stab["stable"]:
            continue

        confirm = find_confirmation_1h(df_1h, retest, direction)
        if not confirm["confirmed"]:
            continue

        entry = confirm["entry"]
        pattern = detect_pattern(df_1h, confirm["confirm_idx"], direction)

        # الهدف 1 من 4H منذ زمن الاختراق
        # الهدف 2: قمة/قاع الأسبوع السابق
        if direction == "up":
            target1 = _high_since(df_4h, brk["break_time"]) or entry
            target2 = w_high
            stop = retest["retest_low"] - ATR_MULTIPLIER * atr_1h
        else:
            target1 = _low_since(df_4h, brk["break_time"]) or entry
            target2 = w_low
            stop = retest["retest_high"] + ATR_MULTIPLIER * atr_1h

        res = _build_result(
            direction=direction, stage="4h_break_daily",
            entry=entry, target1=target1, target2=target2,
            stop=stop, level=level, retest_info=retest,
            break_info=brk, sr_dict=sr_dict, tfs=tfs, pattern=pattern,
        )
        if res:
            return res

    empty["support_resistance"] = sr_dict
    empty["timeframes"] = tfs
    return empty


# ============================================================
# 9. تصدير للخارج
# ============================================================
def last_price(df):
    if df is None or len(df) == 0:
        return None
    return float(df.iloc[-1]["close"])
