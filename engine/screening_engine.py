# -*- coding: utf-8 -*-
"""
天眼策略引擎 v5.0 — 选股评分引擎
================================
v5.0 重构 (2026-06-01):
  - screen_etf_sectors(): 废除内联动量计算, 改用预计算指标表
  - minervini_score():   8条件全部用真实数据, 不再"默认通过"
  - 新增三道 Minervini 空间硬闸门: 天花板/地板/均线多头

数据依赖:
  - DuckDB technical_indicators (由 data_syncer.py 收盘后预计算)
  - DuckDB kline_daily           (仅用于 250 日最高/最低价查询)
"""

import json
import os
import sys
from datetime import date

import duckdb
import numpy as np
import pandas as pd

DB = "D:/FreeFinanceData/data/duckdb/finance.db"

# -- 行业板块 → 指数代码映射 --------------------------
SECTOR_INDICES = {
    "有色金属":   "sh000819",
    "电力公用":   "sz399160",
    "电力":       "sz399160",   # 别名: 绿色电力/电力公用
    "绿色电力":   "sz399160",   # 别名: 国家战略级
    "新能源电池": "sz399261",
    "沪深300":    "sh000300",
    "上证50":     "sh000016",
    "中证500":    "sh000905",
    "创业板":     "sz399006",
    "科创50":     "sh000688",
    "军工":       "sz399967",
    "银行":       "sz399986",
    "白酒":       "sz399997",
    "深证成指":   "sz399001",
    "新能源车":   "sz399438",
}

# ── 国家战略级板块: 绿色电力 ─────────────────────────
# 符合双碳+新型电力系统核心战略, 具备极高政策确定性
# 即使在技术面短期滞后时, 系统给予战略托底持有信号
STRATEGIC_SECTORS = {
    "sz399160": {
        "name": "绿色电力",
        "policy": "国家双碳战略+新型电力系统核心",
        "score_bonus": 25,       # 基本面战略加分
        "certainty": "极高确定性",
        "action_override": "持有",  # 技术面弱势时不减仓, 战略坚守
    },
}


# =======================================================
# 工具
# =======================================================

def q(sql, params=None):
    return duckdb.connect(DB).execute(sql, params or []).fetchdf()


def _get_latest_date(conn):
    """获取指标表最新交易日期"""
    row = conn.execute(
        "SELECT MAX(trade_date) FROM technical_indicators"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


# =======================================================
# Minervini 空间过滤器 v6.1 — 零硬编码, 全DB驱动
# =======================================================

def screen_minervini_with_db(stock_code, db_conn):
    """Minervini 空间硬闸门 — 三道全过才放行

    从 DuckDB 读取 250 日高低点 + 均线, 逐条判定:
      G1. 底部验证: close >= min_close_250 * 1.25  (防死钱陷阱)
      G2. 防追高:   close >= max_close_250 * 0.85  (距高点回撤 ≤ 15%)
      G3. 均线多头: MA50 > MA150 > MA200

    Args:
      stock_code: 统一代码, 如 'sh000300'
      db_conn:    duckdb.Connection (已打开)

    Returns:
      dict: {
        'passed': bool,
        'reason': str,          # 通过="PASS", 不通过=拦截原因码
        'checks': {             # 每个闸门的判定详情
            'floor':   {'ok': bool, 'detail': str, 'ratio': float},
            'ceiling': {'ok': bool, 'detail': str, 'ratio': float},
            'ma':      {'ok': bool, 'detail': str},
        },
        'data': {               # 原始数据, 供下游打分复用
            'close': float, 'max_high_250': float, 'min_low_250': float,
            'ma50': float, 'ma150': float, 'ma200': float,
            'trade_date': str,
        }
      }
    """
    from datetime import datetime as _dt, timedelta as _td

    # ── 1. 读取最新技术指标 (close 优先从指标表, NULL则从kline_daily兜底) ──
    tech = db_conn.execute(
        "SELECT t.trade_date, COALESCE(t.close, k.close) AS close, "
        "t.ma50, t.ma150, t.ma200 "
        "FROM technical_indicators t "
        "LEFT JOIN kline_daily k ON t.ts_code=k.ts_code AND t.trade_date=k.trade_date "
        "WHERE t.ts_code=? "
        "ORDER BY t.trade_date DESC LIMIT 1",
        [stock_code],
    ).fetchone()

    if tech is None:
        return {
            "passed": False,
            "reason": "FAIL_NO_DATA",
            "checks": {
                "floor":   {"ok": False, "detail": "无技术指标数据", "ratio": 0},
                "ceiling": {"ok": False, "detail": "无技术指标数据", "ratio": 0},
                "ma":      {"ok": False, "detail": "无均线数据"},
            },
            "data": {"close": 0, "max_high_250": 0, "min_low_250": 0,
                     "ma50": 0, "ma150": 0, "ma200": 0, "trade_date": ""},
        }

    trade_date = str(tech[0])[:10]
    close      = float(tech[1]) if tech[1] is not None else 0.0
    ma50       = float(tech[2]) if tech[2] is not None else 0.0
    ma150      = float(tech[3]) if tech[3] is not None else 0.0
    ma200      = float(tech[4]) if tech[4] is not None else 0.0

    # ── 1b. MA50/MA150 兜底: 从 kline_daily 动态计算 ──
    if ma50 <= 0 or ma150 <= 0:
        try:
            if ma50 <= 0:
                ma50_row = db_conn.execute(
                    "SELECT AVG(close) FROM ("
                    "  SELECT close FROM kline_daily WHERE ts_code=? AND trade_date <= ? "
                    "  ORDER BY trade_date DESC LIMIT 50"
                    ") sub", [stock_code, trade_date]
                ).fetchone()
                if ma50_row and ma50_row[0] is not None:
                    ma50 = float(ma50_row[0])
            if ma150 <= 0:
                ma150_row = db_conn.execute(
                    "SELECT AVG(close) FROM ("
                    "  SELECT close FROM kline_daily WHERE ts_code=? AND trade_date <= ? "
                    "  ORDER BY trade_date DESC LIMIT 150"
                    ") sub", [stock_code, trade_date]
                ).fetchone()
                if ma150_row and ma150_row[0] is not None:
                    ma150 = float(ma150_row[0])
        except Exception:
            pass  # 兜底失败也不阻塞, 下游会检查 ma_ok=False

    if close <= 0:
        return {
            "passed": False, "reason": "FAIL_NO_CLOSE",
            "checks": {
                "floor":   {"ok": False, "detail": "收盘价缺失", "ratio": 0},
                "ceiling": {"ok": False, "detail": "收盘价缺失", "ratio": 0},
                "ma":      {"ok": False, "detail": "无收盘价"},
            },
            "data": {"close": 0, "max_high_250": 0, "min_low_250": 0,
                     "ma50": ma50, "ma150": ma150, "ma200": ma200, "trade_date": trade_date},
        }

    # ── 2. 读取 250 日高低点 ──────────────────────────
    date_250_ago = (
        _dt.strptime(trade_date, "%Y-%m-%d") - _td(days=250)
    ).strftime("%Y-%m-%d")

    hl = db_conn.execute(
        "SELECT MAX(high), MIN(low) FROM kline_daily "
        "WHERE ts_code=? AND trade_date >= ?",
        [stock_code, date_250_ago],
    ).fetchone()

    max_high_250 = float(hl[0]) if hl and hl[0] is not None else 0.0
    min_low_250  = float(hl[1]) if hl and hl[1] is not None else 0.0

    if max_high_250 <= 0 or min_low_250 <= 0:
        return {
            "passed": False, "reason": "FAIL_NO_250RANGE",
            "checks": {
                "floor":   {"ok": False, "detail": "无250日高低点数据", "ratio": 0},
                "ceiling": {"ok": False, "detail": "无250日高低点数据", "ratio": 0},
                "ma":      {"ok": False, "detail": "跳过"},
            },
            "data": {"close": close, "max_high_250": max_high_250,
                     "min_low_250": min_low_250, "ma50": ma50, "ma150": ma150,
                     "ma200": ma200, "trade_date": trade_date},
        }

    # ── 3. 三道闸门判定 ────────────────────────────────

    # G1: 底部验证 — 防死钱陷阱
    floor_ratio = close / min_low_250
    floor_ok = floor_ratio >= 1.25
    if floor_ok:
        floor_detail = f"floor={floor_ratio:.2f}x OK"
    else:
        floor_detail = f"距250低点仅{((floor_ratio-1)*100):.1f}% < 25%"

    # G2: 防追高天花板 — 距高点回撤 ≤ 15%
    ceiling_ratio = close / max_high_250
    ceiling_ok = ceiling_ratio >= 0.85
    if ceiling_ok:
        ceiling_detail = f"ceiling={ceiling_ratio:.2f} OK"
    else:
        dd = (1 - ceiling_ratio) * 100
        ceiling_detail = f"距250高点回撤{dd:.1f}% > 15%"

    # G3: 均线多头 — MA50 > MA150 > MA200
    if ma50 > 0 and ma150 > 0 and ma200 > 0:
        ma_ok = (ma50 > ma150) and (ma150 > ma200)
        if ma_ok:
            ma_detail = f"MA50({ma50:.0f})>MA150({ma150:.0f})>MA200({ma200:.0f})"
        else:
            ma_detail = f"MA50({ma50:.0f}) MA150({ma150:.0f}) MA200({ma200:.0f}) 非多头"
    else:
        ma_ok = False
        ma_detail = "MA50/150/200 数据不完整"

    # ── 4. 汇总判定 ──────────────────────────────────
    all_passed = floor_ok and ceiling_ok and ma_ok

    if all_passed:
        reason = "PASS"
    else:
        # 优先级: 天花板 > 地板 > 均线 (严重程度)
        if not ceiling_ok:
            reason = "FAIL_TOO_HIGH"
        elif not floor_ok:
            reason = "FAIL_DEAD_MONEY"
        else:
            reason = "FAIL_MA_NOT_BULL"

    return {
        "passed": all_passed,
        "reason": reason,
        "checks": {
            "floor":   {"ok": floor_ok,   "detail": floor_detail,   "ratio": round(floor_ratio, 3)},
            "ceiling": {"ok": ceiling_ok, "detail": ceiling_detail, "ratio": round(ceiling_ratio, 3)},
            "ma":      {"ok": ma_ok,      "detail": ma_detail},
        },
        "data": {
            "close": close, "max_high_250": max_high_250, "min_low_250": min_low_250,
            "ma50": ma50, "ma150": ma150, "ma200": ma200, "trade_date": trade_date,
        },
    }


# =======================================================
# Minervini 8 条件趋势模板 v6.1 — 调用 screen_minervini_with_db
# =======================================================

def minervini_score(ts_code):
    """Minervini 8 条件趋势模板 v6.1

    条件 1-5 & 8: 独立判定
    条件 6-7:     委托 screen_minervini_with_db (无硬编码)
    """
    details = []

    # -- 读最新指标 --
    tech = q(
        "SELECT * FROM technical_indicators WHERE ts_code=? "
        "ORDER BY trade_date DESC LIMIT 1",
        [ts_code],
    )
    if tech.empty:
        return 0, ["技术指标缺失"]

    t = tech.iloc[0]
    close = t["close"]
    ma50 = t.get("ma50")
    ma150 = t.get("ma150")
    ma200 = t.get("ma200")

    # -- 读 2 期前 MA200 --
    from datetime import datetime as _dt, timedelta as _td
    t2 = None
    cmp_date = (
        _dt.strptime(str(t["trade_date"])[:10], "%Y-%m-%d") - _td(days=20)
    ).strftime("%Y-%m-%d")
    tech2 = q(
        "SELECT ma200, trade_date FROM technical_indicators "
        "WHERE ts_code=? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1",
        [ts_code, cmp_date],
    )
    if not tech2.empty:
        t2 = tech2.iloc[0]

    # -- 空间闸门: 委托 screen_minervini_with_db ----------
    conn = duckdb.connect(DB)
    gate = screen_minervini_with_db(ts_code, conn)
    conn.close()

    score = 0

    # 条件 1: 价格 > MA150 且 价格 > MA200
    if ma150 and ma200 and close and close > ma150 and close > ma200:
        score += 1
        details.append("C1[OK]价>MA150&MA200")
    elif ma150 is None or ma200 is None:
        details.append("C1?MA缺失")
    else:
        details.append(f"C1[NO]价{close:.0f}≤MA150/200")

    # 条件 2: MA150 > MA200
    if ma150 and ma200 and ma150 > ma200:
        score += 1
        details.append("C2[OK]MA150>MA200")
    else:
        details.append("C2[NO]MA150≤MA200")

    # 条件 3: MA200 上行
    if ma200 and t2 is not None and t2["ma200"] and ma200 > t2["ma200"]:
        score += 1
        details.append("C3[OK]MA200上行")
    elif t2 is None:
        details.append("C3?无历史MA200")
    else:
        details.append("C3[NO]MA200未上行")

    # 条件 4: MA50 > MA150 且 MA50 > MA200
    if ma50 and ma150 and ma200 and ma50 > ma150 and ma50 > ma200:
        score += 1
        details.append("C4[OK]MA50>MA150&MA200")
    else:
        details.append("C4[NO]MA50未领先长均")

    # 条件 5: 价格 > MA50
    if ma50 and close and close > ma50:
        score += 1
        details.append("C5[OK]价>MA50")
    else:
        details.append(f"C5[NO]价{close:.0f}≤MA50")

    # 条件 6: 地板 — close >= 250低点 × 1.25 (来自 screen_minervini_with_db)
    if gate["checks"]["floor"]["ok"]:
        score += 1
        details.append(f"C6[OK]{gate['checks']['floor']['detail']}")
    else:
        details.append(f"C6[NO]{gate['checks']['floor']['detail']}")

    # 条件 7: 天花板 — close >= 250高点 × 0.85
    if gate["checks"]["ceiling"]["ok"]:
        score += 1
        details.append(f"C7[OK]{gate['checks']['ceiling']['detail']}")
    else:
        details.append(f"C7[NO]{gate['checks']['ceiling']['detail']}")

    # 条件 8: RS ≥ 70
    rsi14 = t.get("rsi14")
    ret_20d = 0.0
    try:
        k20 = q(
            "SELECT close FROM kline_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 21",
            [ts_code],
        )
        if len(k20) >= 21:
            c0 = float(k20.iloc[0]["close"])
            c20 = float(k20.iloc[20]["close"])
            if c20 > 0:
                ret_20d = (c0 / c20 - 1) * 100
    except Exception:
        pass

    if rsi14 is not None and 55 <= rsi14 <= 75 and ret_20d > -5:
        score += 1
        details.append(f"C8[OK]RSI{rsi14:.0f}+动量")
    else:
        details.append(f"C8[NO]RSI{rsi14:.0f}/20日{ret_20d:+.1f}%")

    return min(8, score), details


# =======================================================
# 基本面评分
# =======================================================

def fundamental_score(ts_code):
    """基本面因子评分 0-100"""
    fin = q(
        """SELECT roe, pe_ttm, gross_margin, net_margin, operating_cf, net_profit
           FROM financial_statements WHERE ts_code=?
           ORDER BY report_date DESC LIMIT 1""",
        [ts_code],
    )

    if fin.empty:
        val = q(
            "SELECT pe_ttm, pb FROM valuation_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 1",
            [ts_code],
        )
        if val.empty:
            return 50
        v = val.iloc[0]
        return 60 if (v["pe_ttm"] and 10 < v["pe_ttm"] < 30) else 40

    f = fin.iloc[0]
    score = 0

    # ROE (20分)
    roe = f["roe"] or 0
    score += min(20, max(0, roe * 100))

    # PE 分位 (20分)
    pe = q(
        "SELECT pe_percentile_5y FROM valuation_daily WHERE ts_code=? "
        "ORDER BY trade_date DESC LIMIT 1",
        [ts_code],
    )
    if not pe.empty and pe.iloc[0]["pe_percentile_5y"]:
        pct = pe.iloc[0]["pe_percentile_5y"]
        score += min(20, max(0, (1 - pct / 100) * 20))

    # 成长 (20分)
    score += 10

    # 现金流 (20分)
    op_cf = f["operating_cf"] or 0
    np_val = f["net_profit"] or 1
    if np_val > 0:
        cf_ratio = op_cf / np_val
        score += min(20, max(0, cf_ratio * 10))

    # 毛利率 (20分)
    gm = f["gross_margin"] or 0
    score += min(20, gm * 50)

    return round(score, 1)


# =======================================================
# 资金面评分
# =======================================================

def capital_flow_score(ts_code):
    """资金面评分 0-100"""
    nb = q(
        "SELECT change_pct FROM north_bound_flow WHERE ts_code=? "
        "ORDER BY trade_date DESC LIMIT 5",
        [ts_code],
    )
    cf = q(
        "SELECT main_net_inflow FROM capital_flow WHERE ts_code=? "
        "ORDER BY trade_date DESC LIMIT 5",
        [ts_code],
    )
    score = 50
    if not nb.empty:
        avg_nb = nb["change_pct"].mean()
        score += min(30, max(-30, avg_nb * 100))
    if not cf.empty:
        avg_cf = cf["main_net_inflow"].mean()
        score += min(20, max(-20, avg_cf / 1e8))
    return round(max(0, min(100, score)), 1)


# =======================================================
# 综合选股 + 评分排序
# =======================================================

def screen_and_rank(conditions=None):
    """综合选股 + 评分排序 (MCP 多条件筛选入口)"""
    sys.path.insert(0, "C:/Users/Lenovo/Desktop/ceshi/天眼/mcp_server")
    from server import screen_stocks

    results = screen_stocks(conditions or [])
    stocks = results.get("results", []) if isinstance(results, dict) else []

    ranked = []
    for s in stocks:
        ts = s.get("ts_code", s.get("code", ""))
        if not ts:
            continue
        mv_score, mv_detail = minervini_score(ts)
        fund_score = fundamental_score(ts)
        cf_score = capital_flow_score(ts)

        overall = round(
            mv_score / 8 * 30 + fund_score * 0.25 + cf_score * 0.25 + 50 * 0.2, 1
        )

        ranked.append(
            {
                **s,
                "minervini": f"{mv_score}/8",
                "fund_score": fund_score,
                "capital_score": cf_score,
                "overall": overall,
            }
        )

    ranked.sort(key=lambda x: x["overall"], reverse=True)
    return ranked[:20]


# =======================================================
# ETF 板块选股 v5.0 (核心重构)
# =======================================================

def screen_etf_sectors():
    """ETF 行业板块选股 v5.0

    流程:
      1. 一次性从 DuckDB 读取最新交易日全部白名单指标 + 250日高低点
      2. 三道 Minervini 空间硬闸门 (一票否决):
         - 天花板: close >= max_high_250 × 0.85  (距历史高点回撤≤15%)
         - 地板:   close >= min_low_250  × 1.25  (高于历史低点25%+)
         - 均线:   ma_alignment == 'long' AND close > ma50
      3. 通过闸门的标的 → 进入评分矩阵
      4. 评分按降序输出

    Returns:
      list[dict]: 按 score 降序排列, 被拦截的不在列表中
    """
    conn = duckdb.connect(DB)

    # -- Step 0: 最新交易日期 --------------------------
    latest_date = _get_latest_date(conn)
    if latest_date is None:
        conn.close()
        print("[screening_engine] technical_indicators 无数据, 请先运行 data_syncer.py sync")
        return []

    from datetime import datetime as _dt, timedelta as _td
    date_250_ago = (
        _dt.strptime(latest_date, "%Y-%m-%d") - _td(days=250)
    ).strftime("%Y-%m-%d")

    # -- Step 1: 批量加载指标 + 250日高低点 -------------
    ts_codes = list(SECTOR_INDICES.values())
    placeholders = ", ".join(["?"] * len(ts_codes))

    sql = f"""
        WITH
        latest_tech AS (
            SELECT * FROM technical_indicators
            WHERE trade_date = ?
        ),
        latest_kline AS (
            SELECT DISTINCT ON (ts_code)
                ts_code, close AS k_close, high AS k_high, low AS k_low
            FROM kline_daily
            WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date DESC
        ),
        range_250 AS (
            SELECT
                ts_code,
                MAX(high) AS max_high_250,
                MIN(low)  AS min_low_250
            FROM kline_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date >= ?
            GROUP BY ts_code
        )
        SELECT
            COALESCE(t.close,      k.k_close)     AS close,
            COALESCE(t.high,       k.k_high)      AS high,
            COALESCE(t.low,        k.k_low)       AS low,
            t.ma5, t.ma10, t.ma20, t.ma50, t.ma60,
            t.ma120, t.ma150, t.ma200,
            t.macd_dif, t.macd_dea, t.macd_hist,
            t.kdj_k, t.kdj_d, t.kdj_j,
            t.boll_upper, t.boll_mid, t.boll_lower,
            t.rsi6, t.rsi14, t.rsi24,
            t.ma_alignment, t.vol_ma5, t.vol_ma20, t.volume_ratio,
            t.ts_code, t.trade_date,
            r.max_high_250,
            r.min_low_250
        FROM latest_tech t
        LEFT JOIN latest_kline k ON t.ts_code = k.ts_code
        LEFT JOIN range_250 r    ON t.ts_code = r.ts_code
        WHERE t.ts_code IN ({placeholders})
    """
    params = [latest_date] + ts_codes + ts_codes + [date_250_ago] + ts_codes
    df_all = conn.execute(sql, params).fetchdf()
    conn.close()

    if df_all.empty:
        print(f"[screening_engine] 最新日期 {latest_date} 无指标数据")
        return []

    # -- Step 2: 遍历 — screen_minervini_with_db 三道闸门 --
    passed_log = []
    rejected_log = []
    results = []

    conn2 = duckdb.connect(DB)  # 复用连接给 gate 函数

    for _, row in df_all.iterrows():
        ts_code = row["ts_code"]
        sector = _find_sector_name(ts_code)
        if sector is None:
            continue

        # ── 委托 screen_minervini_with_db ──────────────
        gate = screen_minervini_with_db(ts_code, conn2)

        if not gate["passed"]:
            reason = gate["reason"]
            chk = gate["checks"]
            d = gate["data"]

            if reason == "FAIL_TOO_HIGH":
                rejected_log.append(
                    f"[拦截] {ts_code} ({sector}): {reason} "
                    f"天花板{d['max_high_250']:.0f} 现价{d['close']:.0f} "
                    f"ratio={chk['ceiling']['ratio']:.2f} "
                    f"→{chk['ceiling']['detail']}"
                )
            elif reason == "FAIL_DEAD_MONEY":
                rejected_log.append(
                    f"[拦截] {ts_code} ({sector}): {reason} "
                    f"250低点{d['min_low_250']:.0f} "
                    f"ratio={chk['floor']['ratio']:.2f} "
                    f"→{chk['floor']['detail']}"
                )
            elif reason == "FAIL_MA_NOT_BULL":
                rejected_log.append(
                    f"[拦截] {ts_code} ({sector}): {reason} "
                    f"→{chk['ma']['detail']}"
                )
            else:
                rejected_log.append(
                    f"[拦截] {ts_code} ({sector}): {reason}"
                )
            continue

        # ── 通过 → 评分 ────────────────────────────────
        d = gate["data"]
        passed_log.append(f"[通过] {ts_code} ({sector})")
        score, detail = _score_sector(row, sector)
        results.append(
            {
                "sector": sector,
                "code": ts_code,
                "score": score,
                "detail": detail,
                "close": round(d["close"], 2),
                "rsi14": round(float(row.get("rsi14") or 0), 1),
                "ma_alignment": row.get("ma_alignment"),
                "macd": "golden" if (row.get("macd_dif") or 0) > (row.get("macd_dea") or 0) else "dead",
                "vol_ratio": round(float(row.get("volume_ratio") or 0), 2),
                "ceiling_dist": round((d["close"] / d["max_high_250"] - 1) * 100, 1) if d["max_high_250"] else None,
                "floor_dist": round((d["close"] / d["min_low_250"] - 1) * 100, 1) if d["min_low_250"] else None,
            }
        )

    conn2.close()

    # -- Step 3: 打印闸门日志 --------------------------
    if rejected_log:
        print(f"[screening_engine] 闸门拦截 {len(rejected_log)}/{len(df_all)}:")
        for msg in rejected_log:
            print(f"  {msg}")
    if passed_log:
        print(f"[screening_engine] 闸门通过 {len(passed_log)}/{len(df_all)}:")
        for msg in passed_log:
            print(f"  {msg}")

    # -- Step 4: 排序输出 ------------------------------
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# =======================================================
# 评分矩阵 v6.0 — 不对称涨幅惩罚 + 非线性 RSI 情绪衰减
# =======================================================

def _compute_gain_20d(ts_code):
    """从 kline_daily 读取 20 日累计涨幅"""
    try:
        row = q(
            "SELECT close FROM kline_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 21",
            [ts_code],
        )
        if len(row) >= 21:
            cur = float(row.iloc[0]["close"])
            prev = float(row.iloc[20]["close"])
            if prev > 0:
                return (cur / prev - 1) * 100
    except Exception:
        pass
    return None


def _compute_rsi_lambda(rsi14):
    """非线性 RSI 情绪衰减系数

    rsi <= 55        -> lambda = 1.0
    55 < rsi < 70    -> lambda = max(0, 1 - ((rsi-55)/15)^2)
    rsi >= 70        -> lambda = 0.0  (热熔断, 分数归零)
    """
    if rsi14 is None:
        return 1.0
    if rsi14 >= 70:
        return 0.0
    if rsi14 <= 55:
        return 1.0
    x = (rsi14 - 55) / 15.0
    return max(0.0, 1.0 - x * x)


def _compute_gain_score(gain_20d):
    """不对称涨幅评分: 右侧奖励, 赶顶惩罚

    gain < 0%          -> -5  (还在跌, 结构未修复)
    gain in [0%, 15%]  -> gain (0~15 线性加分)
    gain in (15%, 20%] -> 0   (中性区)
    gain > 20%         -> -(gain-20)*3  (每超1%扣3分)
    """
    if gain_20d is None:
        return 0
    if gain_20d < 0:
        return -5.0
    elif gain_20d <= 15:
        return gain_20d
    elif gain_20d <= 20:
        return 0.0
    else:
        return -(gain_20d - 20) * 3.0


def _score_sector(row, sector_name):
    """v6.0: 不对称涨幅惩罚 + 非线性 RSI 衰减

    公式: 最终得分 = (技术面总分 + 景气度得分) * lambda_RSI

    技术面 = 涨幅评分 + 趋势结构 + MACD + 成交量 + 布林
    涨幅评分: 健康右侧(0~15%)加分, 赶顶(>20%)无情倒扣
    lambda:   RSI<=55 → 1.0; 55<RSI<70 → 二次衰减; RSI>=70 → 0
    """
    ts_code = row["ts_code"]
    trade_date = str(row.get("trade_date", ""))[:10]

    close = row.get("close")
    ma50 = row.get("ma50")
    ma150 = row.get("ma150")
    ma200 = row.get("ma200")
    macd_dif = row.get("macd_dif") or 0
    macd_dea = row.get("macd_dea") or 0
    macd_hist = row.get("macd_hist") or 0
    rsi14 = row.get("rsi14") or 50
    vol_ratio = row.get("volume_ratio") or 1.0
    boll_mid = row.get("boll_mid")
    boll_upper = row.get("boll_upper")
    boll_lower = row.get("boll_lower")

    detail = []

    # -- A. 不对称涨幅评分 (核心反追涨) -----------------
    gain_20d = _compute_gain_20d(ts_code)
    gain_score = _compute_gain_score(gain_20d)

    if gain_20d is not None:
        tag = "跌" if gain_20d < 0 else ("+" if gain_20d >= 0 else "")
        if gain_20d > 20:
            detail.append(f"20日{tag}{gain_20d:.1f}%赶顶[扣{gain_score:.0f}]")
        elif gain_20d >= 0:
            detail.append(f"20日{tag}{gain_20d:.1f}%[{gain_score:+.0f}]")
        else:
            detail.append(f"20日{tag}{gain_20d:.1f}%[{gain_score:+.0f}]")
    else:
        detail.append("20日无数据")

    # -- B. 趋势结构 (0~20) ----------------------------
    trend_score = 0.0
    if ma50 and ma150 and ma200:
        if ma50 > ma150 > ma200:
            trend_score = 20
            detail.append("MA完美")
        elif ma50 > ma150:
            trend_score = 13
            detail.append("MA短>中")
        elif ma50 > ma200:
            trend_score = 7
            detail.append("MA50>200")
    elif ma50 and close and close > ma50:
        trend_score = 5
        detail.append("价>MA50")

    if close and ma50 and ma50 > 0:
        dist = (close / ma50 - 1) * 100
        if dist > 12:
            trend_score = max(0, trend_score - 5)
            detail.append(f"距MA50+{dist:.0f}%热")
        elif dist < 0:
            trend_score = max(0, trend_score - 3)
            detail.append("价<MA50")

    # -- C. MACD 动能 (0~10) ---------------------------
    macd_score = 0.0
    if macd_dif > macd_dea:
        if macd_hist > 0:
            prev_hist = _get_prev_macd_hist(ts_code, trade_date)
            if prev_hist is not None and macd_hist > prev_hist:
                macd_score = 10
                detail.append("MACD放大")
            else:
                macd_score = 8
                detail.append("MACD金叉")
        else:
            macd_score = 5
            detail.append("MACD将叉")
    else:
        macd_score = 2
        detail.append("MACD空")

    # -- D. 成交量 (0~10) ------------------------------
    vol_score = 0.0
    if vol_ratio is not None:
        if 0.7 <= vol_ratio <= 1.5:
            vol_score = 10
        elif 0.5 <= vol_ratio < 0.7:
            vol_score = 7
            detail.append("缩量")
        elif vol_ratio > 2.5:
            vol_score = 3
            detail.append(f"巨{vol_ratio:.1f}x")
        elif vol_ratio > 1.5:
            vol_score = 5
            detail.append(f"放{vol_ratio:.1f}x")
        else:
            vol_score = 2
            detail.append("地量")

    # -- E. 布林 (0~10) --------------------------------
    boll_score = 0.0
    if close and boll_mid and boll_upper and boll_lower:
        bw = boll_upper - boll_lower
        if bw > 0:
            bp = (close - boll_lower) / bw
            if 0.4 <= bp <= 0.8:
                boll_score = 10
            elif 0.2 <= bp < 0.4:
                boll_score = 5
                detail.append("布林偏下")
            elif bp > 0.8:
                boll_score = 4
                detail.append("布林触上")
            else:
                boll_score = 3
                detail.append("布林底")

    # -- 技术面总分 (归一化到0~50) ---------------------
    tech_raw = gain_score + trend_score + macd_score + vol_score + boll_score
    tech_norm = max(0, min(50, tech_raw + 15))
    detail.insert(0, f"技{tech_norm:.0f}/50")

    # -- RSI 情绪衰减 -----------------------------------
    lmbda = _compute_rsi_lambda(rsi14)
    if lmbda == 0.0:
        detail.append(f"RSI{rsi14:.0f}熔断[L=0]")
    elif lmbda < 1.0:
        detail.append(f"RSI{rsi14:.0f}[L={lmbda:.2f}]")
    else:
        detail.append(f"RSI{rsi14:.0f}[L=1]")

    # -- 景气度占位 (0~20, 后续接四层引擎) --------------
    prosperity = 10.0

    # -- 最终得分 ---------------------------------------
    final = (tech_norm + prosperity) * lmbda
    final = round(max(0, min(100, final)))
    detail.append(f"终={final}")

    return final, "; ".join(detail)


def _get_prev_macd_hist(ts_code, trade_date):
    """读取上一交易日的 MACD 红绿柱, 用于判断柱是否放大"""
    try:
        row = q(
            "SELECT macd_hist FROM technical_indicators "
            "WHERE ts_code=? AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1",
            [ts_code, trade_date],
        )
        if not row.empty:
            return float(row.iloc[0]["macd_hist"])
    except Exception:
        pass
    return None


def _find_sector_name(ts_code):
    """ts_code → 板块名称"""
    for name, code in SECTOR_INDICES.items():
        if code == ts_code:
            return name
    return None


# =======================================================
# ETF 选股报告 (格式化输出)
# =======================================================

def screen_etf_report():
    """ETF 选股报告 (可直接输出)"""
    results = screen_etf_sectors()
    if not results:
        return "选股数据不足，请先运行 python data_syncer.py sync"

    lines = ["=" * 78, "  天眼 ETF行业选股引擎 v5.0", "=" * 78]
    lines.append(
        f"  {'板块':10s} {'评分':>4s} {'RSI':>5s} {'MACD':>5s} "
        f"{'量比':>5s} {'距天花板':>8s} {'距地板':>8s} {'操作'}"
    )
    lines.append("-" * 78)

    for r in results:
        action_symbol = _action_label(r["score"])
        ceiling_str = f"{r['ceiling_dist']:+5.1f}%" if r.get("ceiling_dist") is not None else "N/A"
        floor_str = f"{r['floor_dist']:+5.1f}%" if r.get("floor_dist") is not None else "N/A"
        lines.append(
            f"  {r['sector']:10s} {r['score']:4d}分 "
            f"{r['rsi14']:5.1f} {r['macd']:5s} {r['vol_ratio']:5.2f} "
            f"{ceiling_str:>8s} {floor_str:>8s} {action_symbol}"
        )

    lines.append("-" * 78)

    buy = [r for r in results if r["score"] >= 70]
    watch = [r for r in results if 55 <= r["score"] < 70]
    if buy:
        lines.append(f"  建议买入: {', '.join(r['sector'] for r in buy)}")
    if watch:
        lines.append(f"  建议关注: {', '.join(r['sector'] for r in watch)}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _action_label(score):
    if score >= 70:
        return "[G]买入"
    elif score >= 55:
        return "[Y]关注"
    elif score >= 40:
        return "[ ]持有"
    else:
        return "[R]回避"


# =======================================================
# 诊断入口
# =======================================================

if __name__ == "__main__":
    print("ETF 板块选股 v5.0")
    print(screen_etf_report())
    print()

    print("综合选股 (MCP 条件筛选)")
    results = screen_and_rank()
    for i, r in enumerate(results):
        print(
            f"{i+1:2d}. {r['ts_code']:10s} {r.get('name',''):8s} "
            f"PE:{r.get('pe_ttm','?'):6s} MV:{r['minervini']} "
            f"综合:{r['overall']}"
        )
