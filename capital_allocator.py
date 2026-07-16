# -*- coding: utf-8 -*-
"""
天眼 v6.1 资金分配器 — capital_allocator.py
===========================================
基于天眼综合得分 + 当前持仓 + 账户总资产, 输出资金分配方案.

算法: 凯利风格权重分配 + 硬约束优化
  1. 筛选: 只分配 action in {加仓, 持有} 的标的
  2. 权重: target_weight = tianyan_score / sum(all_qualified_scores)
  3. 约束: min_position / max_single_pct / cash_reserve
  4. 调仓: delta = target - current → buy/sell/hold 清单

用法:
  from capital_allocator import allocate

  plan = allocate(
      scored_sectors=[...],      # 天眼打分结果
      holdings=[...],            # 当前持仓 (portfolio.json)
      total_capital=500.0,       # 账户总资产
  )
  # → {buy_plan, sell_plan, hold_plan, summary}
"""

import json
import os
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))

# ── 默认约束 ──────────────────────────────────────────
DEFAULT_CASH_RESERVE_PCT = 0.10   # 10% 现金储备
DEFAULT_MIN_POSITION     = 50.0    # 单笔最低 50 元
DEFAULT_MAX_SINGLE_PCT   = 0.40   # 单一标的 ≤ 40% 总资产
DEFAULT_MAX_POSITIONS    = 4       # 最多同时持有 4 个标的

# ── 场外基金最小申购金额 ──────────────────────────────
FUND_MIN_SUBSCRIBE = 10.0  # 支付宝/天天基金最低 10 元


def allocate(scored_sectors, holdings, total_capital,
             cash_reserve_pct=DEFAULT_CASH_RESERVE_PCT,
             min_position=DEFAULT_MIN_POSITION,
             max_single_pct=DEFAULT_MAX_SINGLE_PCT,
             max_positions=DEFAULT_MAX_POSITIONS):
    """凯利风格资金分配.

    Args:
      scored_sectors: list[dict], 每个元素:
        {
          'sector': str,        # 板块名
          'code': str,          # 统一代码, 如 'sh000300'
          'fund_code': str,     # 联接基金代码, 如 '007404' (可选)
          'tianyan_score': float,  # 天眼综合得分 0-100
          'action': str,        # 加仓/持有/观望/减仓
          'close': float,       # 最新价
          'rsi14': float,       # RSI
          'gain_20d': float,    # 20日涨幅 %
          'verdict': str,       # 裁决标签
        }
      holdings: list[dict], 当前持仓 (来自 portfolio.json)
      total_capital: float, 账户总资产 (元)
      cash_reserve_pct: float, 现金储备比例
      min_position: float, 单笔最低金额
      max_single_pct: float, 单一标的上限比例
      max_positions: int, 最多持仓数

    Returns:
      dict: {
        'buy_plan':   [...],   # 买入清单
        'sell_plan':  [...],   # 卖出清单
        'hold_plan':  [...],   # 持有清单
        'watch_plan': [...],   # 观察清单
        'summary': {
          'total_capital': float,
          'cash_current': float,
          'cash_after': float,
          'deployed_current': float,
          'deployed_target': float,
          'new_buys': int,
          'sells': int,
          'rebalance_needed': bool,
        },
        'warnings': [...],     # 风控警告
      }
    """
    # ── 0. 输入校验 ────────────────────────────────────
    if not scored_sectors:
        return {
            'buy_plan': [], 'sell_plan': [], 'hold_plan': [], 'watch_plan': [],
            'summary': {
                'total_capital': total_capital,
                'cash_current': total_capital,
                'cash_after': total_capital,
                'deployed_current': 0,
                'deployed_target': 0,
                'new_buys': 0, 'sells': 0,
                'rebalance_needed': False,
            },
            'warnings': ['无打分数据, 全仓现金'],
        }

    total_capital = float(total_capital)
    cash_reserve = round(total_capital * cash_reserve_pct, 2)
    working_capital = total_capital - cash_reserve
    max_single = round(total_capital * max_single_pct, 2)

    # 当前持仓映射: fund_code → {amount, name, ...}
    current_map = {}
    for h in holdings:
        code = h.get('code', '')
        current_map[code] = {
            'name': h.get('name', ''),
            'amount': float(h.get('amount', 0)),
            'sector': h.get('sector', ''),
            'pnl_pct': float(h.get('pnl_pct', 0)),
            'role': h.get('role', ''),
        }
    deployed_current = sum(v['amount'] for v in current_map.values())
    cash_current = total_capital - deployed_current

    # ── 1. 筛选合格标的 ──────────────────────────────────
    qualified = []
    sell_candidates = []  # 减仓信号, 有持仓→卖
    warnings = []

    for s in scored_sectors:
        action = s.get('action', '观望')
        score = s.get('tianyan_score', 0)
        sector = s.get('sector', '?')
        rsi = s.get('rsi14', 50)
        fund_code = s.get('fund_code', '')

        # 加仓/持有 才纳入分配池
        if action in ('加仓', '持有'):
            # 无基金代码的标的 → 只能观察
            if not fund_code:
                warnings.append(
                    f"[无基金] {sector}({s.get('code','')}): 评分{score:.0f}但无联接基金可投 → 仅观察"
                )
                continue
            qualified.append(s)
        elif action == '减仓':
            # 检查是否有现存持仓 → 需要卖出
            if fund_code in current_map and current_map[fund_code]['amount'] > 0:
                amt = current_map[fund_code]['amount']
                sell_candidates.append({
                    'fund_code': fund_code,
                    'name': current_map[fund_code]['name'],
                    'sector': sector,
                    'current_amount': amt,
                    'target_amount': 0,
                    'delta': -amt,
                    'action': '卖出',
                    'tianyan_score': score,
                    'rsi14': rsi,
                    'reason': f"天眼评分{score:.0f}<35 RSI{rsi:.0f} → 建议减仓",
                })
        # 观望: 不操作, 不警告

    # ── 2. 无合格标的 → 全仓现金 + 卖出信号 ──────────────
    if not qualified:
        return {
            'buy_plan': [], 'sell_plan': sell_candidates,
            'hold_plan': [], 'watch_plan': [],
            'summary': {
                'total_capital': total_capital,
                'cash_current': cash_current,
                'cash_after': round(cash_current + sum(s['current_amount'] for s in sell_candidates), 2),
                'deployed_current': deployed_current,
                'deployed_target': 0,
                'new_buys': 0, 'sells': len(sell_candidates),
                'rebalance_needed': len(sell_candidates) > 0,
                'max_single_cap': max_single,
                'cash_reserve': cash_reserve,
                'working_capital': working_capital,
            },
            'warnings': warnings + (['无合格标的, 建议空仓'] if not sell_candidates else []),
        }

    # ── 3. 权重计算 (凯利风格: score加权) ──────────────
    total_score = sum(s['tianyan_score'] for s in qualified)
    if total_score <= 0:
        total_score = len(qualified) * 50  # 兜底

    for s in qualified:
        s['_weight'] = s['tianyan_score'] / total_score
        s['_target_raw'] = round(s['_weight'] * working_capital, 2)

    # ── 4. 约束裁剪 ──────────────────────────────────────
    # 4a. 排名: 按得分降序
    qualified.sort(key=lambda x: x['tianyan_score'], reverse=True)

    # 4b. 最多 max_positions 个
    if len(qualified) > max_positions:
        dropped = qualified[max_positions:]
        qualified = qualified[:max_positions]
        for d in dropped:
            warnings.append(
                f"[持仓上限] {d['sector']}: 评分{d['tianyan_score']:.0f}但持仓数已满{max_positions} → 观察池"
            )

    # 4c. 单标的上限裁剪
    for s in qualified:
        if s['_target_raw'] > max_single:
            s['_target_raw'] = max_single
            s['_capped'] = True

    # 4d. 最小持仓裁剪
    for s in qualified:
        if s['_target_raw'] < FUND_MIN_SUBSCRIBE:
            s['_target_raw'] = 0
            s['_below_min'] = True

    # 4e. 归一化: sum(targets) ≤ working_capital
    raw_sum = sum(s['_target_raw'] for s in qualified)
    if raw_sum > working_capital:
        scale = working_capital / raw_sum
        for s in qualified:
            s['_target_raw'] = round(s['_target_raw'] * scale, 2)

    # ── 5. 生成买卖计划 (delta分析) ─────────────────────
    buy_plan = []
    sell_plan = []
    hold_plan = []
    watch_plan = []

    for s in qualified:
        fund_code = s.get('fund_code', '')
        target = s['_target_raw']
        sector = s['sector']
        score = s['tianyan_score']

        # 查找当前持仓
        current_info = current_map.get(fund_code) if fund_code else None
        current_amt = current_info['amount'] if current_info else 0

        delta = round(target - current_amt, 2)

        plan_item = {
            'sector': sector,
            'code': s['code'],
            'fund_code': fund_code,
            'tianyan_score': score,
            'target_amount': target,
            'current_amount': current_amt,
            'delta': delta,
            'weight': round(s['_weight'], 3),
            'rsi14': s.get('rsi14'),
            'gain_20d': s.get('gain_20d'),
        }

        if delta >= min_position:
            # 买入
            plan_item['action'] = '买入'
            plan_item['reason'] = (
                f"评分{score:.0f}/100, 权重{s['_weight']:.1%}, "
                f"目标{target:.0f}元(当前{current_amt:.0f}元)"
            )
            # 最佳买入点: MA10
            buy_plan.append(plan_item)

        elif delta <= -min_position:
            # 卖出
            plan_item['action'] = '卖出'
            plan_item['reason'] = (
                f"评分{score:.0f}/100, 目标降至{target:.0f}元"
                f"(当前{current_amt:.0f}元, 减{abs(delta):.0f}元)"
            )
            sell_plan.append(plan_item)

        elif current_amt > 0:
            # 持有
            plan_item['action'] = '持有'
            plan_item['reason'] = (
                f"评分{score:.0f}/100, 目标{target:.0f}元≈当前{current_amt:.0f}元"
            )
            hold_plan.append(plan_item)
        else:
            # 金额太小, 暂时观察
            plan_item['action'] = '观察'
            plan_item['reason'] = (
                f"评分{score:.0f}/100, 目标{target:.0f}元<最低{FUND_MIN_SUBSCRIBE}元"
            )
            watch_plan.append(plan_item)

    # ── 5b. 合并 Gate 拦截的减仓信号 ──────────────────
    sell_plan.extend(sell_candidates)

    # ── 6. 汇总 ──────────────────────────────────────────
    deployed_target = sum(p['target_amount'] for p in buy_plan + sell_plan + hold_plan + watch_plan)
    cash_after = round(total_capital - deployed_target, 2)

    summary = {
        'total_capital': total_capital,
        'cash_current': round(cash_current, 2),
        'cash_after': cash_after,
        'deployed_current': round(deployed_current, 2),
        'deployed_target': round(deployed_target, 2),
        'new_buys': len(buy_plan),
        'sells': len(sell_plan),
        'holds': len(hold_plan),
        'watches': len(watch_plan),
        'rebalance_needed': len(buy_plan) > 0 or len(sell_plan) > 0,
        'max_single_cap': max_single,
        'cash_reserve': cash_reserve,
        'working_capital': working_capital,
    }

    return {
        'buy_plan': buy_plan,
        'sell_plan': sell_plan,
        'hold_plan': hold_plan,
        'watch_plan': watch_plan,
        'summary': summary,
        'warnings': warnings,
    }


# ═══════════════════════════════════════════════════════
# 格式化输出
# ═══════════════════════════════════════════════════════

def print_allocation(plan, title="天眼 v6.1 资金分配方案"):
    """格式化打印分配方案"""
    buy = plan['buy_plan']
    sell = plan['sell_plan']
    hold = plan['hold_plan']
    watch = plan['watch_plan']
    s = plan['summary']

    print()
    print("=" * 65)
    print(f"  {title}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    print(f"\n  账户总资产: {s['total_capital']:.0f}元")
    print(f"  当前已部署: {s['deployed_current']:.0f}元 | 现金: {s['cash_current']:.0f}元")
    print(f"  目标部署:   {s['deployed_target']:.0f}元 | 现金: {s['cash_after']:.0f}元")
    print(f"  约束: 单标的上限{s['max_single_cap']:.0f}元 | 现金储备{s['cash_reserve']:.0f}元")
    print(f"  可用资金: {s['working_capital']:.0f}元")

    # ── 买入 ──
    if buy:
        print(f"\n  -- [G] 买入计划 ({len(buy)}笔) --")
        print(f"  {'板块':10s} {'基金代码':8s} {'评分':>4s} {'目标':>8s} {'当前':>8s} {'增量':>8s}")
        print(f"  {'-'*55}")
        total_buy = 0
        for p in buy:
            total_buy += p['delta']
            print(f"  {p['sector']:10s} {p.get('fund_code',''):8s} "
                  f"{p['tianyan_score']:4.0f}分 {p['target_amount']:8.0f}元 "
                  f"{p['current_amount']:8.0f}元 {p['delta']:+8.0f}元")
        print(f"  {'合计':>30s} {total_buy:+8.0f}元")

    # ── 卖出 ──
    if sell:
        print(f"\n  -- [R] 卖出计划 ({len(sell)}笔) --")
        print(f"  {'板块':10s} {'基金代码':8s} {'评分':>4s} {'当前':>8s} {'目标':>8s} {'减仓':>8s}")
        print(f"  {'-'*55}")
        total_sell = 0
        for p in sell:
            total_sell += abs(p['delta'])
            print(f"  {p['sector']:10s} {p.get('fund_code',''):8s} "
                  f"{p['tianyan_score']:4.0f}分 {p['current_amount']:8.0f}元 "
                  f"{p['target_amount']:8.0f}元 {p['delta']:+8.0f}元")
        print(f"  {'合计':>30s} {total_sell:+8.0f}元")

    # ── 持有 ──
    if hold:
        print(f"\n  -- [ ] 持有 ({len(hold)}笔) --")
        for p in hold:
            print(f"  {p['sector']:10s} {p.get('fund_code',''):8s} "
                  f"{p['tianyan_score']:4.0f}分 {p['target_amount']:.0f}元 → 不动")

    # ── 观察 ──
    if watch:
        print(f"\n  -- [Y] 观察池 ({len(watch)}笔) --")
        for p in watch:
            print(f"  {p['sector']:10s} {p.get('fund_code',''):8s} "
                  f"{p['tianyan_score']:4.0f}分 → {p['reason']}")

    # ── 警告 ──
    if plan['warnings']:
        print(f"\n  -- [!] 风控警告 --")
        for w in plan['warnings']:
            print(f"  {w}")

    print(f"\n{'='*65}")
    if plan['summary']['rebalance_needed']:
        print(f"  [ACTION REQUIRED] 需调仓: "
              f"买{s['new_buys']}笔 / 卖{s['sells']}笔 / 持{s['holds']}笔")
    else:
        print(f"  [NO ACTION] 无需调仓, 现有配置最优")
    print(f"{'='*65}\n")


# ═══════════════════════════════════════════════════════
# 便捷函数: 从 portfolio.json 加载 + 一键分配
# ═══════════════════════════════════════════════════════

def load_portfolio():
    """读取当前持仓"""
    pf_path = os.path.join(BASE, 'portfolio.json')
    if os.path.exists(pf_path):
        with open(pf_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'holdings': [], 'cash': 0}


def quick_allocate(scored_sectors, total_capital=None):
    """一键分配: 自动加载持仓 + 计算总资产

    Args:
      scored_sectors: 天眼打分结果列表
      total_capital: 总资产, None→从 portfolio.json 计算

    Returns:
      dict: 完整分配方案
    """
    pf = load_portfolio()
    holdings = pf.get('holdings', [])
    cash = float(pf.get('cash', 0))
    deployed = sum(float(h.get('amount', 0)) for h in holdings)

    if total_capital is None:
        total_capital = cash + deployed

    return allocate(scored_sectors, holdings, total_capital)


# ═══════════════════════════════════════════════════════
# CLI 自测
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    # 模拟数据测试
    demo_sectors = [
        {'sector': '电力', 'code': 'sh000819', 'fund_code': '021753',
         'tianyan_score': 78, 'action': '加仓', 'close': 2800,
         'rsi14': 48, 'gain_20d': 3.2, 'verdict': '健康右侧'},
        {'sector': '沪深300', 'code': 'sh000300', 'fund_code': '007404',
         'tianyan_score': 62, 'action': '持有', 'close': 4100,
         'rsi14': 55, 'gain_20d': 0.5, 'verdict': '中性观察'},
        {'sector': '新能源车', 'code': 'sz399438', 'fund_code': '018927',
         'tianyan_score': 55, 'action': '持有', 'close': 3200,
         'rsi14': 58, 'gain_20d': 12.0, 'verdict': '健康右侧'},
        {'sector': '白酒', 'code': 'sz399997', 'fund_code': '',
         'tianyan_score': 28, 'action': '减仓', 'close': 15000,
         'rsi14': 72, 'gain_20d': 28.0, 'verdict': 'HB9驳回'},
        {'sector': '科创50', 'code': 'sh000688', 'fund_code': '011613',
         'tianyan_score': 42, 'action': '观望', 'close': 1680,
         'rsi14': 65, 'gain_20d': -5.3, 'verdict': '回调中'},
    ]

    # 用真实 portfolio.json 测试
    plan = quick_allocate(demo_sectors, 500)
    print_allocation(plan)

    # 纯模拟测试 (无真实持仓)
    print("\n--- 纯模拟测试 (10000元, 无持仓) ---")
    plan2 = allocate(demo_sectors, [], 10000.0)
    print_allocation(plan2, "模拟分配 (10000元)")
