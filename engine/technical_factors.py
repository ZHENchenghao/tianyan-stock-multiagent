# -*- coding: utf-8 -*-
"""
天眼 v6.1 技术因子打分模块 — technical_factors.py
=================================================
废除对称 Z-score, 引入不对称涨幅惩罚 + 非线性 RSI 衰减.

核心公式:
  gain_20d = (close_today / close_20d_ago - 1) * 100

  score_asymmetric(gain):
    gain < 0%           →  0
    gain in [0%, 15%]   →  gain          (线性奖励, 0~15)
    gain in (15%, 20%]  →  0             (中性区)
    gain > 20%          →  -(gain-20)*3  (赶顶惩罚, 无上限倒扣)

  rsi_lambda(rsi):
    rsi <= 55           →  1.0
    55 < rsi < 70       →  1 - ((rsi-55)/15)^2  (二次衰减)
    rsi >= 70           →  0.0  (热熔断)

用法:
  from engine.technical_factors import score_technical_momentum, asymmetric_gain_score

  # 仅打分 (不需要数据库)
  s = asymmetric_gain_score(12.5)  # → 12.5

  # 完整流程 (从 DB 读数据 + 打分 + RSI 衰减)
  result = score_technical_momentum('sh000300')
  # → {'gain_20d': 2.8, 'gain_score': 2.8, 'rsi14': 44.2,
  #     'lambda': 1.0, 'final_score': 2.8, 'verdict': '健康右侧'}


边界测试验证 (2026-06-01):

  >>> asymmetric_gain_score(-10)    # 还在跌 → 0
  0.0
  >>> asymmetric_gain_score(0)      # 刚好不涨不跌 → 0
  0.0
  >>> asymmetric_gain_score(5)      # 微涨 → 5 (线性)
  5.0
  >>> asymmetric_gain_score(15)     # 健康右侧边界 → 15 (满分)
  15.0
  >>> asymmetric_gain_score(17)     # 进入中性区 → 0
  0.0
  >>> asymmetric_gain_score(20)     # 中性区上限 → 0
  0.0
  >>> asymmetric_gain_score(20.01)  # 刚好越界 → -0.03
  -0.03
  >>> asymmetric_gain_score(22)     # 轻微赶顶 → -6
  -6.0
  >>> asymmetric_gain_score(25)     # 严重赶顶 → -15
  -15.0
  >>> asymmetric_gain_score(30)     # 极端赶顶 → -30
  -30.0
  >>> asymmetric_gain_score(50)     # 崩涨 → -90 (绝对垫底)
  -90.0

  >>> rsi_lambda(50)               # RSI 正常 → 无损
  1.0
  >>> rsi_lambda(60)               # RSI 略高 → 11% 衰减
  0.888...
  >>> rsi_lambda(65)               # RSI 偏高 → 56% 保留
  0.555...
  >>> rsi_lambda(68)               # RSI 危险 → 25% 保留
  0.248...
  >>> rsi_lambda(69.5)             # RSI 即将熔断 → 6.5% 保留
  0.065...
  >>> rsi_lambda(70)               # RSI 熔断 → 归零
  0.0
"""

import os
import ssl
import warnings
from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

# ── 环境 ────────────────────────────────────────────
ssl._create_default_https_context = ssl._create_unverified_context
os.environ["PYTHONHTTPSVERIFY"] = "0"
warnings.filterwarnings("ignore")

DB_PATH = r"D:\FreeFinanceData\data\duckdb\finance.db"


# ═══════════════════════════════════════════════════════
# 1. 不对称涨幅评分 — 纯函数, 零外部依赖
# ═══════════════════════════════════════════════════════

def asymmetric_gain_score(gain_20d_pct):
    """不对称 20 日涨幅评分.

    设计原理:
      旧版 Z-score 对称打分: 涨 30% 和跌 30% 得相同绝对值, 完全不合理.
      新版: 涨多了一定是风险, 绝不线性加分.

    Args:
      gain_20d_pct: 近 20 个交易日累计涨跌幅 (%, 如 12.5 表示 +12.5%)

    Returns:
      float: 得分. 范围 (-inf, 15], 负数表示扣分.

    -------- 边界表 --------
      gain          score        区间           含义
      ─────────     ─────        ────           ────
      -10.0         0.0          负收益区       还在跌,不给分
       -0.01        0.0          负收益区       微跌,不给分
        0.0         0.0          健康启动       刚好不涨
        5.0         5.0          健康启动       线性奖励
       10.0        10.0          健康启动       涨幅适中
       15.0        15.0          健康启动上限   满分
       15.01        0.0          中性区         进入观察
       17.0         0.0          中性区         不加不扣
       20.0         0.0          中性区上限     不加不扣
       20.01       -0.03         惩罚区         刚好越界
       22.0        -6.0          惩罚区         轻度赶顶
       25.0       -15.0          惩罚区         严重赶顶
       30.0       -30.0          惩罚区         极端
       50.0       -90.0          惩罚区         崩涨, 绝对垫底
    """
    if gain_20d_pct is None:
        return 0.0
    if gain_20d_pct < 0:
        return 0.0
    elif gain_20d_pct <= 15:
        return float(gain_20d_pct)
    elif gain_20d_pct <= 20:
        return 0.0
    else:
        return -(gain_20d_pct - 20.0) * 3.0


# ═══════════════════════════════════════════════════════
# 2. RSI 非线性情绪衰减 — 纯函数
# ═══════════════════════════════════════════════════════

def rsi_lambda(rsi14):
    """非线性 RSI 情绪衰减系数.

    二次衰减公式: lambda = 1 - ((rsi - 55) / 15)^2

    原理:
      - RSI 从 55 到 70: lambda 从 1.0 平滑衰减到 0.0
      - 越接近 70 衰减越猛 (导数越来越大)
      - RSI >= 70 直接归零 (热熔断, 不留余地)

    Args:
      rsi14: RSI(14) 值, 0~100

    Returns:
      float: 衰减系数, [0.0, 1.0]

    -------- 边界表 --------
      rsi    lambda   衰减率   含义
      ───    ──────   ─────   ────
      25     1.000      0%    超卖, 满分保留
      40     1.000      0%    正常偏低
      50     1.000      0%    中性
      55     1.000      0%    衰减起点
      58     0.960      4%    轻微衰减
      60     0.889     11%    温和衰减
      63     0.716     28%    明显衰减
      65     0.556     44%    大幅衰减
      67     0.360     64%    严重衰减
      68     0.249     75%    悬崖边缘
      69     0.129     87%    只剩一成
      69.5   0.066     93%    垂死挣扎
      69.9   0.004     99.6%  名存实亡
      70     0.000    100%    热熔断
    """
    if rsi14 is None:
        return 1.0
    if rsi14 >= 70:
        return 0.0
    if rsi14 <= 55:
        return 1.0
    x = (rsi14 - 55.0) / 15.0
    return max(0.0, 1.0 - x * x)


# ═══════════════════════════════════════════════════════
# 3. 数据获取 — 从 DuckDB 读 20 日涨幅和最新 RSI
# ═══════════════════════════════════════════════════════

def compute_gain_20d(ts_code, db_path=None):
    """从 kline_daily 计算近 20 个交易日累计涨幅.

    Args:
      ts_code: 统一代码, 如 'sh000300'
      db_path: DuckDB 路径, 默认 DB_PATH

    Returns:
      float | None: 涨幅百分比, None 表示数据不足
    """
    if db_path is None:
        db_path = DB_PATH

    try:
        conn = duckdb.connect(db_path)
        rows = conn.execute(
            "SELECT close FROM kline_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 21",
            [ts_code],
        ).fetchall()
        conn.close()

        if len(rows) < 21:
            return None

        cur = float(rows[0][0])
        prev = float(rows[20][0])
        if prev <= 0:
            return None

        return (cur / prev - 1.0) * 100.0
    except Exception:
        return None


def get_latest_rsi(ts_code, db_path=None):
    """从 technical_indicators 读取最新 RSI(14).

    Args:
      ts_code: 统一代码
      db_path: DuckDB 路径

    Returns:
      float | None
    """
    if db_path is None:
        db_path = DB_PATH

    try:
        conn = duckdb.connect(db_path)
        row = conn.execute(
            "SELECT rsi14 FROM technical_indicators WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 1",
            [ts_code],
        ).fetchone()
        conn.close()

        if row and row[0] is not None:
            return float(row[0])
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
# 4. 综合打分入口
# ═══════════════════════════════════════════════════════

def score_technical_momentum(ts_code, db_path=None):
    """技术动量因子完整打分 — 涨幅不对称 + RSI 衰减.

    流程:
      gain_20d = compute_gain_20d(ts_code)
      raw_score = asymmetric_gain_score(gain_20d)
      rsi = get_latest_rsi(ts_code)
      lmbda = rsi_lambda(rsi)
      final = raw_score * lmbda

    Args:
      ts_code: 统一代码
      db_path: DuckDB 路径

    Returns:
      dict: {
        'ts_code': str,
        'gain_20d': float | None,
        'gain_score': float,        # 不对称涨幅原始得分 (-inf, 15]
        'rsi14': float | None,
        'lambda': float,            # RSI 衰减系数 [0, 1]
        'final_score': float,       # gain_score * lambda
        'verdict': str,             # 健康右侧 / 中性观察 / 赶顶警戒 / 熔断 / 数据不足
        'detail': str,
      }
    """
    gain_20d = compute_gain_20d(ts_code, db_path)
    raw_score = asymmetric_gain_score(gain_20d)
    rsi = get_latest_rsi(ts_code, db_path)
    lmbda = rsi_lambda(rsi)
    final = round(raw_score * lmbda, 2)

    # ── 裁决标签 ──
    if gain_20d is None:
        verdict = "数据不足"
    elif lmbda == 0.0:
        verdict = "熔断"
    elif gain_20d > 20:
        verdict = "赶顶警戒"
    elif gain_20d > 15:
        verdict = "中性观察"
    elif gain_20d >= 0:
        verdict = "健康右侧"
    else:
        verdict = "回调中"

    # ── 详情 ──
    parts = [f"20日涨{gain_20d:+.1f}%" if gain_20d is not None else "20日涨?",
             f"涨幅分{raw_score:+.1f}",
             f"RSI{rsi:.0f}" if rsi is not None else "RSI?",
             f"L={lmbda:.3f}",
             f"终分{final:+.1f}"]

    return {
        "ts_code": ts_code,
        "gain_20d": round(gain_20d, 2) if gain_20d is not None else None,
        "gain_score": round(raw_score, 2),
        "rsi14": round(rsi, 1) if rsi is not None else None,
        "lambda": round(lmbda, 4),
        "final_score": final,
        "verdict": verdict,
        "detail": " | ".join(parts),
    }


# ═══════════════════════════════════════════════════════
# 5. 批量打分 + 排名 (供选股引擎调用)
# ═══════════════════════════════════════════════════════

def batch_score(ts_codes, db_path=None):
    """对一批标的批量打分, 按 final_score 降序排列.

    Args:
      ts_codes: list[str]
      db_path: DuckDB 路径

    Returns:
      list[dict]: 按 final_score 降序
    """
    results = [score_technical_momentum(code, db_path) for code in ts_codes]
    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════
# 6. CLI 自测
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 65)
    print("  天眼 v6.1 技术因子 — 边界测试")
    print("=" * 65)

    # ── 不对称涨幅评分 边界测试 ──
    print("\n--- asymmetric_gain_score 边界测试 ---")
    test_cases = [
        (-10.0, 0.0, "负收益, 不给分"),
        (-0.01, 0.0, "微跌, 不给分"),
        (0.0, 0.0, "刚好不涨"),
        (5.0, 5.0, "微涨, 线性奖励"),
        (10.0, 10.0, "适中涨幅"),
        (15.0, 15.0, "健康右侧满分"),
        (15.01, 0.0, "刚好入中性"),
        (17.0, 0.0, "中性区中间"),
        (20.0, 0.0, "中性区上限"),
        (20.01, -0.03, "刚好越界0.01%"),
        (22.0, -6.0, "轻度赶顶"),
        (25.0, -15.0, "严重赶顶"),
        (30.0, -30.0, "极端赶顶"),
        (50.0, -90.0, "崩涨垫底"),
        (None, 0.0, "None输入"),
    ]

    all_ok = True
    for gain, expected, desc in test_cases:
        actual = asymmetric_gain_score(gain)
        ok = abs(actual - expected) < 0.01
        if not ok:
            all_ok = False
        flag = "[OK]" if ok else "[FAIL]"
        print(f"  {flag} gain={gain} → {actual:+.2f} (期望{expected:+.2f}) {desc}")

    # ── RSI lambda 边界测试 ──
    print("\n--- rsi_lambda 边界测试 ---")
    rsi_cases = [
        (25, 1.0, "超卖, 满分"),
        (50, 1.0, "中性, 满分"),
        (55, 1.0, "衰减起点"),
        (58, 0.96, "轻微衰减"),
        (60, 0.8889, "温和衰减"),
        (63, 0.7156, "明显衰减"),
        (65, 0.5556, "大幅衰减"),
        (67, 0.3600, "严重衰减"),
        (68, 0.2489, "悬崖边缘"),
        (69, 0.1289, "只剩一成"),
        (69.5, 0.0656, "垂死挣扎"),
        (70, 0.0, "熔断"),
        (75, 0.0, "熔断以上"),
        (None, 1.0, "None输入"),
    ]

    rsi_ok = True
    for rsi, expected, desc in rsi_cases:
        actual = rsi_lambda(rsi)
        ok = abs(actual - expected) < 0.02
        if not ok:
            rsi_ok = False
        flag = "[OK]" if ok else "[FAIL]"
        print(f"  {flag} RSI={rsi} → lambda={actual:.4f} (期望{expected:.4f}) {desc}")

    # ── 综合判定 ──
    print(f"\n{'='*65}")
    if all_ok and rsi_ok:
        print("  全部边界测试通过 [OK]")
    else:
        print("  存在失败用例 [FAIL] — 请检查上方标记")
    print(f"{'='*65}")

    # ── 实盘数据验证 (如有 DuckDB) ──
    print("\n--- 实盘验证 (DuckDB) ---")
    try:
        test_codes = ["sh000300", "sh000688", "sh000819", "sz399006"]
        results = batch_score(test_codes)
        for r in results:
            print(f"  {r['ts_code']:12s} {r['verdict']:6s} {r['detail']}")
    except Exception as e:
        print(f"  DuckDB 不可用: {e}")
