"""
analysis.py — استراتيجية الاختراق وإعادة الاختبار
مساران: اختراق يومي/أسبوعي، أو اختراق 4H/يومي.

⚠️ عقد البيانات:
- المستدعي (main.py) يجب أن يُنشئ عمودين:
    * time       = وقت بداية الشمعة (من مزود البيانات، للعرض فقط).
    * close_time = وقت إغلاق الشمعة (لحظة توفر المعلومة).
- المنطق يستخدم close_time حصراً — لا fallback على time.
- المستدعي يجب أن يمرر شموعاً مكتملة فقط (main.py يستبعد غير المكتملة).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

MIN_RVOL = 1.5
MIN_RR = 2.0
MAX_RETEST_CANDLES = 5
MAX_FIRST_CANDLE_WINDOW = 5       # أول تأكيد خلال 5 شموع بعد Retest
MAX_SECOND_CANDLE_WINDOW = 5      # الشمعة الثانية خلال 5 شموع بعد الأولى
MAX_1H_CONFIRM_CANDLES = 5
BODY_RATIO_MIN = 0.6
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
TOLERANCE_PCT = 0.003
TOLERANCE_ATR_MULT = 0.25
BREAKOUT_LOOKBACK = 10
RVOL_PERIOD = 20

_REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}
_TIME_COLUMNS = {"time", "close_time"}


# ============================================================
# تجهيز البيانات والتحقق منها
# ============================================================
def _valid_df(df: Optional[pd.DataFrame]) -> bool:
    """يتطلب أعمدة OHLCV + time + close_time."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    if not _REQUIRED_COLUMNS.issubset(df.columns):
        return False
    if not _TIME_COLUMNS.issubset(df.columns):
        return False
    return True


def _sorted_df(df: pd.DataFrame) -> pd.DataFrame:
    """نسخة مرتبة زمنياً بفهرس رقمي متسلسل، تعتمد على close_time."""
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], errors="coerce", utc=True)
    out["close_time"] = pd.to_datetime(out["close_time"], errors="coerce", utc=True)
    out = out.dropna(subset=["time", "close_time"])
    out = out.sort_values("close_time", kind="stable")
    out = out.drop_duplicates(subset=["close_time"], keep="last")
    return out.reset_index(drop=True)


def _time_of(df: Optional[pd.DataFrame], pos: int):
    """وقت الإغلاق للصف pos (يُستخدم كمرجع زمني في كل المنطق)."""
    if df is None or "close_time" not in df.columns or pos < 0 or pos >= len(df):
        return None
    return df.iloc[pos]["close_time"]


def _positions_after(df: pd.DataFrame, timestamp, strict: bool = True) -> list[int]:
    """مواقع الصفوف بعد timestamp (وفق close_time)."""
    if "close_time" not in df.columns or timestamp is None:
        return []
    ts = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(ts):
        return []
    mask = df["close_time"] > ts if strict else df["close_time"] >= ts
    return np.flatnonzero(mask.to_numpy()).tolist()


# ============================================================
# المؤشرات
# ============================================================
def atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> pd.Series:
    if not _valid_df(df):
        return pd.Series(dtype=float)
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rvol_series(df: pd.DataFrame, n: int = RVOL_PERIOD) -> pd.Series:
    if not _valid_df(df):
        return pd.Series(dtype=float)
    avg = df["volume"].astype(float).shift(1).rolling(n, min_periods=n).mean()
    return df["volume"].astype(float) / avg.replace(0, np.nan)


# ============================================================
# أدوات عامة
# ============================================================
def prev_candle_hl(df: pd.DataFrame, idx: int = -2):
    if not _valid_df(df) or len(df) < abs(idx):
        return None, None
    row = df.iloc[idx]
    return float(row["high"]), float(row["low"])


def _body_ratio(row) -> float:
    rng = max(float(row["high"]) - float(row["low"]), 1e-8)
    return abs(float(row["close"]) - float(row["open"])) / rng


def _is_bullish(row) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row) -> bool:
    return float(row["close"]) < float(row["open"])


def _extreme_until(df, since_time, until_time, direction, include_since=True):
    """
    يحسب أقصى high/أدنى low ضمن نافذة زمنية [since_time, until_time].
    يعتمد على close_time. لا يقرأ صفوفاً بعد until_time.
    """
    if not _valid_df(df) or since_time is None or until_time is None:
        return None
    start = pd.to_datetime(since_time, errors="coerce", utc=True)
    end = pd.to_datetime(until_time, errors="coerce", utc=True)
    if pd.isna(start) or pd.isna(end) or end < start:
        return None
    if include_since:
        mask = (df["close_time"] >= start) & (df["close_time"] <= end)
    else:
        mask = (df["close_time"] > start) & (df["close_time"] <= end)
    sub = df.loc[mask]
    if sub.empty:
        return None
    col = "high" if direction == "up" else "low"
    return float(sub[col].max() if direction == "up" else sub[col].min())


def compute_tolerance(level: float, atr_val: float) -> float:
    if not np.isfinite(level) or not np.isfinite(atr_val) or atr_val <= 0:
        return 0.0
    return min(abs(level) * TOLERANCE_PCT, atr_val * TOLERANCE_ATR_MULT)


# ============================================================
# 1. فحص الاختراق
# ============================================================
def check_breakout(df, level, direction, lookback=BREAKOUT_LOOKBACK):
    """
    يبحث عن أحدث اختراق مؤكد بالحجم (RVOL ≥ 1.5).
    عند العثور على الأحدث، لا يعود للبحث عن اختراق أقدم.
    """
    if not _valid_df(df) or len(df) < max(25, RVOL_PERIOD + 1) or level is None:
        return {"broken": False, "reason": "insufficient_data"}

    data = _sorted_df(df)
    rv_series = rvol_series(data, RVOL_PERIOD)
    start = max(0, len(data) - int(lookback))

    for i in range(len(data) - 1, start - 1, -1):
        rv = rv_series.iloc[i]
        if pd.isna(rv) or float(rv) < MIN_RVOL:
            continue
        row = data.iloc[i]
        close = float(row["close"])
        if direction == "up" and close > float(level):
            return {
                "broken": True, "type": "up", "level": float(level),
                "break_index": i, "break_time": _time_of(data, i),
                "break_close": close, "rvol": round(float(rv), 2),
            }
        if direction == "down" and close < float(level):
            return {
                "broken": True, "type": "down", "level": float(level),
                "break_index": i, "break_time": _time_of(data, i),
                "break_close": close, "rvol": round(float(rv), 2),
            }
    return {"broken": False, "reason": "no_qualifying_breakout"}


# ============================================================
# 2. فحص Retest
# ============================================================
def find_retest(df, level, direction, break_time=None, atr_val=None,
                max_candles=MAX_RETEST_CANDLES):
    if not _valid_df(df) or len(df) < 5 or level is None:
        return {"retested": False, "status": "no_data"}

    data = _sorted_df(df)
    if atr_val is None or not np.isfinite(atr_val) or atr_val <= 0:
        atr_values = atr(data, ATR_PERIOD)
        atr_val = float(atr_values.iloc[-1]) if len(atr_values) and pd.notna(atr_values.iloc[-1]) else np.nan
    if not np.isfinite(atr_val) or atr_val <= 0:
        return {"retested": False, "status": "invalid_atr"}

    tol = compute_tolerance(float(level), float(atr_val))
    if break_time is not None:
        after = _positions_after(data, break_time, strict=True)
    else:
        after = list(range(max(0, len(data) - int(max_candles)), len(data)))
    if not after:
        return {"retested": False, "status": "no_candles_after"}

    positions = after[:int(max_candles)]
    for i in positions:
        row = data.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])

        # الإبطال يسري منذ أول شمعة بعد الاختراق وحتى Retest
        if direction == "up" and close < level:
            return {"retested": False, "status": "invalidated_before_retest",
                    "violation_idx": i, "violation_time": _time_of(data, i)}
        if direction == "down" and close > level:
            return {"retested": False, "status": "invalidated_before_retest",
                    "violation_idx": i, "violation_time": _time_of(data, i)}

        if direction == "up" and low <= float(level) + tol and close > level:
            return {
                "retested": True, "status": "ok", "type": "up", "level": float(level),
                "retest_idx": i, "retest_time": _time_of(data, i),
                "retest_high": high, "retest_low": low,
                "retest_open": float(row["open"]), "retest_close": close,
                "tolerance": round(tol, 6),
                "candles_since_break": positions.index(i) + 1,
            }
        if direction == "down" and high >= float(level) - tol and close < level:
            return {
                "retested": True, "status": "ok", "type": "down", "level": float(level),
                "retest_idx": i, "retest_time": _time_of(data, i),
                "retest_high": high, "retest_low": low,
                "retest_open": float(row["open"]), "retest_close": close,
                "tolerance": round(tol, 6),
                "candles_since_break": positions.index(i) + 1,
            }

    return {"retested": False, "status": "no_retest_in_window"}


# ============================================================
# 3. فحص الثبات
# ============================================================
def check_stability(df, level, direction, from_idx, until_idx=None):
    if not _valid_df(df):
        return {"stable": False, "reason": "no_data"}
    data = _sorted_df(df)
    start = int(from_idx)
    end = len(data) - 1 if until_idx is None else min(int(until_idx), len(data) - 1)
    if start < 0 or start >= len(data):
        return {"stable": False, "reason": "invalid_start_index"}
    if start >= end:
        return {"stable": True, "reason": "no_candles_after"}

    for i in range(start + 1, end + 1):
        close = float(data.iloc[i]["close"])
        if direction == "up" and close < level:
            return {"stable": False, "reason": f"close_below_at_{i}", "violation_idx": i}
        if direction == "down" and close > level:
            return {"stable": False, "reason": f"close_above_at_{i}", "violation_idx": i}
    return {"stable": True, "reason": "no_violation"}


# ============================================================
# 4. المسار الأول: الشمعة الأولى والثانية
# ============================================================
def find_second_candle(df, level, direction, retest_idx,
                       max_window=MAX_SECOND_CANDLE_WINDOW,
                       max_first_window=MAX_FIRST_CANDLE_WINDOW):
    """
    شمعة Retest ليست تأكيداً.
    الشمعة الأولى: أول إغلاق في اتجاه الصفقة خلال max_first_window شموع بعد Retest.
    الشمعة الثانية: أول إغلاق في الاتجاه بعد الأولى، خلال max_window شموع بعدها.
    إذا أُغلقت أي شمعة ضد المستوى في فترة الانتظار، تُلغى الإشارة.
    """
    if not _valid_df(df) or retest_idx < 0 or retest_idx >= len(df):
        return {"found": False, "reason": "invalid_retest_index"}

    data = _sorted_df(df)
    retest_idx = int(retest_idx)
    first_end = min(len(data) - 1, retest_idx + int(max_first_window))
    first_idx = None

    for i in range(retest_idx + 1, first_end + 1):
        close = float(data.iloc[i]["close"])
        if direction == "up" and close < level:
            return {"found": False, "reason": "invalidated_before_first", "violation_idx": i}
        if direction == "down" and close > level:
            return {"found": False, "reason": "invalidated_before_first", "violation_idx": i}
        if (direction == "up" and close > level) or (direction == "down" and close < level):
            first_idx = i
            break

    if first_idx is None:
        return {"found": False, "reason": "no_first_in_window"}

    second_end = min(len(data) - 1, first_idx + int(max_window))
    for j in range(first_idx + 1, second_end + 1):
        row = data.iloc[j]
        close = float(row["close"])
        if direction == "up" and close < level:
            return {"found": False, "reason": "invalidated_in_window", "violation_idx": j}
        if direction == "down" and close > level:
            return {"found": False, "reason": "invalidated_in_window", "violation_idx": j}

        if (direction == "up" and close > level) or (direction == "down" and close < level):
            return {
                "found": True, "first_idx": first_idx, "second_idx": j,
                "entry": close, "entry_time": _time_of(data, j),
                "body_ratio": round(_body_ratio(row), 2),
                "candles_after_first": j - first_idx,
            }
    return {"found": False, "reason": "no_second_in_window"}


# ============================================================
# 5. المسار الثاني: تأكيد 1H
# ============================================================
def find_confirmation_1h(df_1h, retest_info, direction):
    if not _valid_df(df_1h) or len(df_1h) < 2 or not retest_info:
        return {"confirmed": False, "reason": "no_data"}

    data = _sorted_df(df_1h)
    retest_high = retest_info.get("retest_high")
    retest_low = retest_info.get("retest_low")
    retest_time = retest_info.get("retest_time")
    if retest_time is None:
        return {"confirmed": False, "reason": "missing_retest_time"}

    candidates = _positions_after(data, retest_time, strict=True)[:MAX_1H_CONFIRM_CANDLES]
    if not candidates:
        return {"confirmed": False, "reason": "no_1h_after_retest"}

    for i in candidates:
        row = data.iloc[i]
        close = float(row["close"])
        if direction == "up" and retest_high is not None and close > float(retest_high):
            return {"confirmed": True, "confirm_idx": i, "entry": close,
                    "confirm_time": _time_of(data, i),
                    "body_ratio": round(_body_ratio(row), 2)}
        if direction == "down" and retest_low is not None and close < float(retest_low):
            return {"confirmed": True, "confirm_idx": i, "entry": close,
                    "confirm_time": _time_of(data, i),
                    "body_ratio": round(_body_ratio(row), 2)}
    return {"confirmed": False, "reason": "no_confirmation"}


# ============================================================
# 6. كشف نمط الشمعة (وصفي فقط)
# ============================================================
def detect_pattern(df, idx, direction):
    if not _valid_df(df) or idx < 1 or idx >= len(df):
        return "none"
    data = _sorted_df(df)
    cur, prev = data.iloc[idx], data.iloc[idx - 1]
    cur_o, cur_c = float(cur["open"]), float(cur["close"])
    cur_h, cur_l = float(cur["high"]), float(cur["low"])
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    br = _body_ratio(cur)

    if direction == "up":
        if _is_bearish(prev) and _is_bullish(cur) and cur_o <= prev_c and cur_c >= prev_o:
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0 and (min(cur_o, cur_c) - cur_l) >= 2 * br * rng and br < 0.4:
            return "hammer"
    else:
        if _is_bullish(prev) and _is_bearish(cur) and cur_o >= prev_c and cur_c <= prev_o:
            return "engulfing"
        rng = cur_h - cur_l
        if rng > 0 and (cur_h - max(cur_o, cur_c)) >= 2 * br * rng and br < 0.4:
            return "shooting_star"
    return "none"


# ============================================================
# 7. بناء النتيجة
# ============================================================
def _build_result(direction, stage, entry, target1, target2, stop,
                  level, retest_info, break_info, sr_dict, tfs, pattern=""):
    if any(v is None or not np.isfinite(float(v)) for v in (entry, target1, stop)):
        return None
    entry, target1, stop = float(entry), float(target1), float(stop)
    risk = abs(entry - stop)
    reward = (target1 - entry) if direction == "up" else (entry - target1)
    if risk <= 0 or reward <= 0:
        return None
    rr = round(reward / risk, 2)
    if rr < MIN_RR:
        return None

    levels = {
        "entry": round(entry, 2), "stop": round(stop, 2),
        "target1": round(target1, 2), "rr": rr,
        "level_broken": round(float(level), 2), "pattern": pattern,
    }
    if target2 is not None and np.isfinite(float(target2)):
        target2 = float(target2)
        target2_is_ahead = target2 > entry if direction == "up" else target2 < entry
        if target2_is_ahead:
            levels["target2"] = round(target2, 2)

    return {
        "color": "green" if direction == "up" else "red",
        "label": "تأكيد CALL" if direction == "up" else "تأكيد PUT",
        "status": "confirmed", "direction": "call" if direction == "up" else "put",
        "breakout_stage": stage, "levels": levels, "break_info": break_info,
        "retest_info": retest_info, "support_resistance": sr_dict, "timeframes": tfs,
    }


def _tf_snapshot(df, label):
    if not _valid_df(df) or len(df) < 5:
        return {"label": label, "support": None, "resistance": None,
                "broke_resistance": False, "broke_support": False}
    data = _sorted_df(df)
    res, sup = prev_candle_hl(data, -2)
    price = float(data.iloc[-1]["close"])
    return {
        "label": label,
        "support": round(sup, 2) if sup is not None else None,
        "resistance": round(res, 2) if res is not None else None,
        "broke_resistance": res is not None and price > res,
        "broke_support": sup is not None and price < sup,
    }


def _empty_result(sr_dict=None, tfs=None):
    return {
        "color": "gray", "label": "لا إشارة", "status": "none",
        "direction": None, "breakout_stage": None, "levels": {},
        "timeframes": tfs or [], "support_resistance": sr_dict or {},
    }


# ============================================================
# 8. الدالة الرئيسية
# ============================================================
def scan_setup(df_weekly, df_daily, df_4h, df_1h):
    frames = [df_weekly, df_daily, df_4h, df_1h]
    if any(not _valid_df(d) or len(d) < 25 for d in frames):
        return _empty_result()

    df_weekly, df_daily, df_4h, df_1h = [_sorted_df(d) for d in frames]

    w_high, w_low = prev_candle_hl(df_weekly, -2)
    d_high, d_low = prev_candle_hl(df_daily, -2)
    sr_dict = {
        "weekly": {"support": round(w_low, 2) if w_low is not None else None,
                   "resistance": round(w_high, 2) if w_high is not None else None},
        "daily": {"support": round(d_low, 2) if d_low is not None else None,
                  "resistance": round(d_high, 2) if d_high is not None else None},
    }
    tfs = [_tf_snapshot(df_weekly, "1W"), _tf_snapshot(df_daily, "1D"),
           _tf_snapshot(df_4h, "4H"), _tf_snapshot(df_1h, "1H")]

    atr4s, atr1s = atr(df_4h), atr(df_1h)
    atr_4h = float(atr4s.iloc[-1]) if len(atr4s) and pd.notna(atr4s.iloc[-1]) else np.nan
    atr_1h = float(atr1s.iloc[-1]) if len(atr1s) and pd.notna(atr1s.iloc[-1]) else np.nan
    if not np.isfinite(atr_4h) or atr_4h <= 0 or not np.isfinite(atr_1h) or atr_1h <= 0:
        return _empty_result(sr_dict, tfs)

    # ── المسار الأول: إغلاق يومي خارج قمة/قاع الأسبوع السابق
    for direction, level in (("up", w_high), ("down", w_low)):
        if level is None:
            continue
        brk = check_breakout(df_daily, level, direction)
        if not brk.get("broken"):
            continue
        retest = find_retest(df_4h, level, direction, brk["break_time"], atr_4h)
        if not retest.get("retested"):
            continue
        second = find_second_candle(df_4h, level, direction, retest["retest_idx"])
        if not second.get("found"):
            continue
        stab = check_stability(df_4h, level, direction,
                                retest["retest_idx"], second["second_idx"])
        if not stab["stable"]:
            continue

        entry = second["entry"]
        target1 = _extreme_until(df_4h, brk["break_time"], second["entry_time"],
                                  direction, include_since=True)
        if target1 is None:
            continue
        stop = (retest["retest_low"] - ATR_MULTIPLIER * atr_4h
                if direction == "up"
                else retest["retest_high"] + ATR_MULTIPLIER * atr_4h)
        result = _build_result(
            direction, "daily_break_weekly", entry, target1, None,
            stop, level, retest, brk, sr_dict, tfs,
            detect_pattern(df_4h, second["second_idx"], direction),
        )
        if result:
            return result

    # ── المسار الثاني: إغلاق 4H خارج قمة/قاع اليوم السابق
    for direction, level in (("up", d_high), ("down", d_low)):
        if level is None:
            continue
        brk = check_breakout(df_4h, level, direction)
        if not brk.get("broken"):
            continue
        retest = find_retest(df_4h, level, direction, brk["break_time"], atr_4h)
        if not retest.get("retested"):
            continue
        confirm = find_confirmation_1h(df_1h, retest, direction)
        if not confirm.get("confirmed"):
            continue

        confirm_time = confirm.get("confirm_time")
        if confirm_time is None:
            continue
        # افحص شموع 4H التي close_time <= وقت التأكيد
        eligible = np.flatnonzero(
            (df_4h["close_time"] <= pd.to_datetime(confirm_time, utc=True)).to_numpy()
        )
        stability_end = int(eligible[-1]) if len(eligible) else int(retest["retest_idx"])
        stab = check_stability(df_4h, level, direction,
                                retest["retest_idx"], stability_end)
        if not stab["stable"]:
            continue

        entry = confirm["entry"]
        target1 = _extreme_until(df_4h, brk["break_time"], confirm_time,
                                  direction, include_since=True)
        if target1 is None:
            continue
        target2 = w_high if direction == "up" else w_low
        stop = (retest["retest_low"] - ATR_MULTIPLIER * atr_1h
                if direction == "up"
                else retest["retest_high"] + ATR_MULTIPLIER * atr_1h)
        result = _build_result(
            direction, "4h_break_daily", entry, target1, target2,
            stop, level, retest, brk, sr_dict, tfs,
            detect_pattern(df_1h, confirm["confirm_idx"], direction),
        )
        if result:
            return result

    return _empty_result(sr_dict, tfs)


# ============================================================
# 9. تصدير
# ============================================================
def last_price(df):
    if not _valid_df(df):
        return None
    return float(df.iloc[-1]["close"])
