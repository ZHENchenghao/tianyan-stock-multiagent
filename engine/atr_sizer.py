# -*- coding: utf-8 -*-
"""
天眼 v8 路线A · ATR波动率定仓器
================================
替代 HRP: 不看相关性树, 每只标的按自身 ATR 独立定仓。
波动大→少买, 波动小→多买, 但绝不压制动量本身。

公式:
  weight_i ∝ 1 / (ATR_i / close_i)  即波动率倒数
  上限15%, 下限3%, 归一化后等权兜底

与 HRP 的本质区别:
  HRP:    看全矩阵协方差 → 高波动妖股被其他低波股"挤出去"
  ATR:    每只独立计算 → 高波动妖股买少点但一定买
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from typing import Dict, List, Optional


MAX_WEIGHT = 0.15
MIN_WEIGHT = 0.03
ATR_PERIOD = 20          # ATR计算周期
BASE_RISK = 0.02         # 目标日波动率 2%


def compute_atr_weights(codes: List[str],
                        price_df: pd.DataFrame) -> Dict[str, float]:
    """
    ATR波动率定仓: 每只标的独立计算权重。

    Args:
      codes:    候选股票代码列表
      price_df: 历史价格透视表 (index=date, columns=code)

    Returns:
      {code: weight} 归一化后的目标权重
    """
    n = len(codes)
    if n == 0:
        return {}
    if n == 1:
        return {codes[0]: 1.0}

    # 只保留有数据的列
    valid = [c for c in codes if c in price_df.columns]
    if len(valid) < 1:
        return {c: 1.0 / n for c in codes}

    prices = price_df[valid]

    # 日收益率
    returns = prices.pct_change().dropna()
    if len(returns) < ATR_PERIOD:
        w = 1.0 / len(valid)
        return {c: w for c in valid}

    # 每只标的的日波动率 (年化)
    daily_vol = returns.std()  # Series, index=code
    ann_vol = daily_vol * np.sqrt(242)

    # 波动率目标倒数: w ∝ 1/σ
    # 越稳定 → 权重越大; 越妖 → 权重越小
    inv_vol = 1.0 / daily_vol.replace(0, np.nan)

    # 处理 NaN (数据不足的标的 → 给最小权重)
    inv_vol = inv_vol.fillna(inv_vol.min() if inv_vol.notna().any() else 1.0)

    # 归一化
    total = inv_vol.sum()
    if total <= 0:
        w = 1.0 / len(valid)
        return {c: w for c in valid}

    weights = (inv_vol / total).to_dict()

    # 硬截断: 上限15%, 下限3%
    weights = {k: max(MIN_WEIGHT, min(MAX_WEIGHT, v)) for k, v in weights.items()}

    # 截断后重新归一化 (超额只流向未触顶资产)
    for _ in range(10):
        over = {k: v - MAX_WEIGHT for k, v in weights.items() if v > MAX_WEIGHT}
        if not over:
            break
        excess = sum(over.values())
        for k in over:
            weights[k] = MAX_WEIGHT
        under = {k: v for k, v in weights.items() if v < MAX_WEIGHT - 1e-6}
        total_under = sum(under.values())
        if total_under <= 0:
            break
        for k in under:
            weights[k] += excess * (under[k] / total_under)

    # 最终归一化
    total = sum(weights.values())
    if total > 1e-10:
        weights = {k: v / total for k, v in weights.items()}

    # 确保池子里每个标的都有权重
    for c in codes:
        if c not in weights:
            weights[c] = 0.0

    return weights


# ════════════════════════════════════════════════════
# CLI 自检
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from engine.hrp_optimizer import HRPOptimizer

    opt = HRPOptimizer()
    codes = ['sh600519','sz000858','sh601398','sh600036',
             'sh600900','sz300750','sz002594','sh600809']
    price_df = opt._fetch_prices(codes)

    if price_df is not None:
        atr_w = compute_atr_weights(codes, price_df)

        # 对比: HRP权重
        from engine.hrp_optimizer import (detect_zombies, check_crisis_mode,
                                           compute_penalized_covariance, run_hrp)
        zombies, _ = detect_zombies(codes)
        clean = [c for c in codes if c not in zombies]
        clean = [c for c in clean if c in price_df.columns]
        clean_prices = price_df[clean]
        _, cm = check_crisis_mode(clean_prices)
        pcov = compute_penalized_covariance(clean_prices, clean)
        hrp_w = run_hrp(clean_prices, clean, cm, pcov)

        # 波动率数据
        returns = price_df.pct_change().dropna()
        ann_vol = (returns.std() * np.sqrt(242) * 100).to_dict()

        print('=' * 70)
        print(f'  {"代码":16s} {"年化波":>8s} {"HRP权重":>10s} {"ATR权重":>10s} {"差异":>10s}')
        print('=' * 70)
        for c in codes:
            hrp = hrp_w.get(c, 0)
            atr = atr_w.get(c, 0)
            vol = ann_vol.get(c, 0)
            diff = atr - hrp
            marker = '← 动量得解放' if diff > 0.03 else ('→ 被压制' if diff < -0.03 else '')
            print(f'  {c:16s} {vol:7.1f}% {hrp:9.1%} {atr:9.1%} {diff:+9.1%} {marker}')
        print('=' * 70)
    else:
        print('数据拉取失败, 请检查 DuckDB')
