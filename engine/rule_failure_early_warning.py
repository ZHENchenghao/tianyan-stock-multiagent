# -*- coding: utf-8 -*-
"""
天眼 v4.0 规则失效预警引擎
==========================
第二阶段: 某条规则连续失效时自动降权/冻结, 不等亏钱才发现。

三种检测器:
  CuSum累积和: 准确率偏离历史均值超过阈值 → 告警
  滚动窗口衰减: 最近N个信号准确率 < 历史-1σ → 标记衰减
  市场状态归因: 规则是否只在特定市场状态失效 → 状态依赖

输入: signal_history.json (83,138条回测信号)
输出: 每条规则的风险等级(绿/黄/橙/红) + 建议操作

参考:
  Page (1954) Biometrika — CuSum变点检测
  Bailey & Lopez de Prado (2014) — 回测过拟合概率
  天眼铁律#1: 先搜GitHub再写代码

用法:
  python engine/rule_failure_early_warning.py          # 检查所有规则
  python engine/rule_failure_early_warning.py --rule R26 # 单条规则
  python tianyan.py rules                              # CLI入口
"""

import sys, os, json, math
from datetime import datetime, date, timedelta
from collections import defaultdict
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNAL_FILE = os.path.join(BASE, '..', 'signal_history.json')
RULE_GRADES_FILE = os.path.join(BASE, '..', 'rule_grades.json')

# ═══════════════════════ 配置 ═══════════════════════
CUSUM_THRESHOLD = 5.0        # CuSum告警阈值 (Page 1954)
ROLLING_WINDOW = 20          # 滚动窗口信号数
MIN_SIGNALS_FOR_CHECK = 30   # 最少信号数才能检查
DECAY_SIGMA = 1.0            # 衰减检测: 偏离1σ
CONSECUTIVE_BAD = 8          # 连续错误数 → 冻结
ACCURACY_FREEZE = 0.40       # 准确率 < 40% → 冻结
ACCURACY_WARN = 0.50         # 准确率 < 50% → 观察


def load_signals() -> list:
    """加载全部历史信号"""
    if not os.path.exists(SIGNAL_FILE):
        return []
    with open(SIGNAL_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_rule_grades() -> dict:
    """加载规则等级 (回测评级)"""
    if not os.path.exists(RULE_GRADES_FILE):
        return {}
    with open(RULE_GRADES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def group_by_rule(signals: list) -> dict:
    """按rule_id分组, 按日期排序"""
    groups = defaultdict(list)
    for s in signals:
        groups[s['rule_id']].append(s)
    for rid in groups:
        groups[rid].sort(key=lambda x: str(x.get('trade_date', '')))
    return dict(groups)


def calc_accuracy(rule_signals: list) -> float:
    """
    方向准确率: direction=1(看多) + fwd_return_10d>0 → 正确
               direction=-1(看空) + fwd_return_10d<0 → 正确
    """
    if not rule_signals:
        return 0.5
    correct = 0
    for s in rule_signals:
        d = s.get('direction', 0)
        r = s.get('fwd_return_10d', 0)
        if (d > 0 and r > 0) or (d < 0 and r < 0):
            correct += 1
    return correct / len(rule_signals)


# ═══════════════════════ 检测器 ═══════════════════════

def cusum_detect(rule_signals: list, threshold: float = CUSUM_THRESHOLD) -> dict:
    """
    CuSum累积和检测 (Page 1954 Biometrika)

    S_t = max(0, S_{t-1} + (μ_history - acc_t) - k)
    如果 S_t > h(阈值) → 检测到准确率下降变点

    返回: {csum_value, csum_alert, csum_level}
    """
    if len(rule_signals) < MIN_SIGNALS_FOR_CHECK:
        return {'cusum_value': 0.0, 'cusum_alert': False, 'cusum_level': 'insufficient_data'}

    # 历史基线: 前80%信号的准确率
    split = max(len(rule_signals) // 5, MIN_SIGNALS_FOR_CHECK)
    baseline_signals = rule_signals[:-split]
    recent_signals = rule_signals[-split:]

    hist_acc = calc_accuracy(baseline_signals)
    if hist_acc < 0.45:  # 基线太差, 规则本身就有问题
        return {'cusum_value': 0.0, 'cusum_alert': False, 'cusum_level': 'baseline_poor',
                'baseline_accuracy': round(hist_acc, 3)}

    # 对最近的信号逐条计算CuSum
    k = 0.5 * (1 - hist_acc)  # 允许的漂移量 (Page建议k=delta/2)
    S = 0.0
    max_S = 0.0
    alarm_idx = -1

    for i, s in enumerate(recent_signals):
        d = s.get('direction', 0)
        r = s.get('fwd_return_10d', 0)
        correct = 1 if (d > 0 and r > 0) or (d < 0 and r < 0) else 0
        S = max(0, S + (hist_acc - correct) - k)
        if S > max_S:
            max_S = S
        if S > threshold:
            alarm_idx = i
            break

    return {
        'cusum_value': round(max_S, 3),
        'cusum_alert': alarm_idx >= 0,
        'cusum_level': 'alert' if alarm_idx >= 0 else ('warning' if max_S > threshold * 0.7 else 'normal'),
        'baseline_accuracy': round(hist_acc, 3),
        'alarm_at_signal': alarm_idx if alarm_idx >= 0 else None,
    }


def rolling_decay_detect(rule_signals: list, window: int = ROLLING_WINDOW,
                          sigma: float = DECAY_SIGMA) -> dict:
    """
    滚动窗口衰减检测

    最近 window 个信号的准确率 vs 历史准确率分布:
      如果 < μ - 1σ → 衰减
      连续3个窗口满足 → 加速衰减
    """
    if len(rule_signals) < window + 30:
        return {'rolling_alert': False, 'rolling_level': 'insufficient_data'}

    # 滚动计算所有窗口的准确率
    all_windows = []
    for i in range(window, len(rule_signals) + 1):
        w = rule_signals[i - window:i]
        all_windows.append(calc_accuracy(w))

    if not all_windows:
        return {'rolling_alert': False, 'rolling_level': 'insufficient_data'}

    window_accs = np.array(all_windows)
    mu = np.mean(window_accs)
    sigma_val = np.std(window_accs) if len(window_accs) > 1 else 0.01

    recent_acc = window_accs[-1]
    z_score = (recent_acc - mu) / sigma_val if sigma_val > 0 else 0

    # 检查最近3个窗口
    if len(window_accs) >= 3:
        recent_3 = window_accs[-3:]
        below_threshold = sum(1 for a in recent_3 if a < mu - sigma)
    else:
        below_threshold = 1 if recent_acc < mu - sigma else 0

    alert = below_threshold >= 3
    level = 'alert' if alert else ('warning' if below_threshold >= 2 else 'normal')

    return {
        'rolling_alert': alert,
        'rolling_level': level,
        'recent_accuracy': round(recent_acc, 3),
        'historical_mean': round(mu, 3),
        'z_score': round(z_score, 2),
        'consecutive_low_windows': below_threshold,
    }


def consecutive_loss_check(rule_signals: list) -> dict:
    """连续错误检测 — 连续8个信号错误 → 冻结"""
    if len(rule_signals) < 8:
        return {'consecutive_alert': False, 'consecutive_count': 0}

    streak = 0
    max_streak = 0
    for s in reversed(rule_signals):  # 从最近开始
        d = s.get('direction', 0)
        r = s.get('fwd_return_10d', 0)
        correct = (d > 0 and r > 0) or (d < 0 and r < 0)
        if not correct:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            break

    return {
        'consecutive_alert': max_streak >= CONSECUTIVE_BAD,
        'consecutive_count': max_streak,
    }


# ═══════════════════════ 综合评估 ═══════════════════════

RISK_LEVELS = {
    'green':  {'label': '🟢 正常',   'action': '保持现有权重',       'priority': 0},
    'yellow': {'label': '🟡 关注',   'action': '降权至0.8x, 观察1周', 'priority': 1},
    'orange': {'label': '🟠 警告',   'action': '降权至0.5x, 停止新信号', 'priority': 2},
    'red':    {'label': '🔴 冻结',   'action': '规则冻结, 等市场恢复',   'priority': 3},
}


def assess_rule(rule_id: str, rule_signals: list, rule_grades: dict) -> dict:
    """
    对单条规则做完整评估, 返回风险等级 + 建议。
    铁律#10: 每条结果附带解释。
    """
    n = len(rule_signals)
    accuracy = calc_accuracy(rule_signals)

    # 三个检测器
    cusum = cusum_detect(rule_signals)
    rolling = rolling_decay_detect(rule_signals)
    consecutive = consecutive_loss_check(rule_signals)

    # 回测等级
    grade_info = rule_grades.get(rule_id, {})
    backtest_grade = grade_info.get('grade', 'C')

    # ── 综合判定 ──
    risk = 'green'
    reasons = []

    # 条件1: 准确率本身太低
    if n >= MIN_SIGNALS_FOR_CHECK:
        if accuracy < ACCURACY_FREEZE:
            risk = 'red'
            reasons.append(f'历史准确率{accuracy:.1%} < {ACCURACY_FREEZE:.0%}阈值')
        elif accuracy < ACCURACY_WARN:
            risk = 'orange'
            reasons.append(f'历史准确率{accuracy:.1%} < {ACCURACY_WARN:.0%}阈值')

    # 条件2: CuSum告警
    if cusum['cusum_alert']:
        if RISK_LEVELS[risk]['priority'] < 3:
            risk = 'orange'
        reasons.append(f'CuSum检测到准确率下降 (S={cusum["cusum_value"]:.1f}, 基线={cusum["baseline_accuracy"]:.1%})')

    # 条件3: 滚动窗口衰减
    if rolling['rolling_alert']:
        if RISK_LEVELS[risk]['priority'] < 3:
            risk = 'orange'
        reasons.append(f'滚动窗口连续衰减 (最近={rolling["recent_accuracy"]:.1%}, 均值={rolling["historical_mean"]:.1%})')

    # 条件4: 连续错误
    if consecutive['consecutive_alert']:
        risk = 'red'
        reasons.append(f'连续{consecutive["consecutive_count"]}个信号错误, 触发冻结')

    # 条件5: 回测D级 → 已是冻结状态
    if backtest_grade == 'D':
        risk = 'red'
        reasons.append('回测评级D级(胜率<50%), 应冻结')

    # 条件6: 信号太少
    if n < MIN_SIGNALS_FOR_CHECK:
        risk = 'green'
        reasons.append(f'信号不足({n}/{MIN_SIGNALS_FOR_CHECK}), 待积累')

    if not reasons:
        reasons.append('各检测器正常')

    # 规则名称
    rule_name = rule_signals[0].get('rule_name', rule_id) if rule_signals else rule_id
    master = rule_signals[0].get('master', '?') if rule_signals else '?'

    return {
        'rule_id': rule_id,
        'rule_name': rule_name,
        'master': master,
        'signal_count': n,
        'accuracy': round(accuracy, 3),
        'backtest_grade': backtest_grade,
        'risk_level': risk,
        'risk_label': RISK_LEVELS[risk]['label'],
        'action': RISK_LEVELS[risk]['action'],
        'reasons': reasons,
        'cusum': cusum,
        'rolling': rolling,
        'consecutive': consecutive,
    }


def assess_all_rules() -> list:
    """评估全部86条规则, 按风险从高到低排序"""
    signals = load_signals()
    if not signals:
        print('[!] signal_history.json 不存在或为空')
        return []

    groups = group_by_rule(signals)
    grades = load_rule_grades()

    results = []
    for rule_id, rule_signals in groups.items():
        result = assess_rule(rule_id, rule_signals, grades)
        results.append(result)

    results.sort(key=lambda r: RISK_LEVELS[r['risk_level']]['priority'], reverse=True)
    return results


# ═══════════════════════ 输出 ═══════════════════════

def print_summary(results: list):
    """打印规则失效预警总览"""

    # 统计
    counts = defaultdict(int)
    for r in results:
        counts[r['risk_level']] += 1
    total = len(results)

    print(f"\n{'='*70}")
    print(f"  规则失效预警引擎 · 评估报告")
    print(f"  生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  规则总数: {total} | 数据源: signal_history.json (83,138条)")
    print(f"{'='*70}")
    print(f"\n  分布: 🔴{counts.get('red',0)}冻结 🟠{counts.get('orange',0)}警告 "
          f"🟡{counts.get('yellow',0)}关注 🟢{counts.get('green',0)}正常")

    # 🔴 冻结规则
    reds = [r for r in results if r['risk_level'] == 'red']
    if reds:
        print(f"\n  {'─'*50}")
        print(f"  🔴 冻结规则 ({len(reds)}条) — 建议停止使用")
        for r in reds:
            print(f"\n  {r['rule_id']} | {r['rule_name'][:40]}")
            print(f"    大师: {r['master']} | 信号: {r['signal_count']}条 | 准确率: {r['accuracy']:.1%}")
            print(f"    回测: {r['backtest_grade']}级")
            for reason in r['reasons']:
                print(f"    → {reason}")
            print(f"    ⚡ 建议: {r['action']}")

    # 🟠 警告规则
    oranges = [r for r in results if r['risk_level'] == 'orange']
    if oranges:
        print(f"\n  {'─'*50}")
        print(f"  🟠 警告规则 ({len(oranges)}条) — 建议降权观察")
        for r in oranges:
            print(f"\n  {r['rule_id']} | {r['rule_name'][:40]}")
            print(f"    大师: {r['master']} | 信号: {r['signal_count']}条 | 准确率: {r['accuracy']:.1%}")
            print(f"    CuSum: S={r['cusum'].get('cusum_value',0):.2f} | 滚动: z={r['rolling'].get('z_score',0):.1f}")
            print(f"    ⚡ 建议: {r['action']}")

    # 🟡 关注
    yellows = [r for r in results if r['risk_level'] == 'yellow']
    if yellows:
        print(f"\n  {'─'*50}")
        print(f"  🟡 关注规则 ({len(yellows)}条)")
        for r in yellows[:5]:
            print(f"    {r['rule_id']}: {r['rule_name'][:35]} (准确率{r['accuracy']:.1%})")

    # 🟢 正常
    greens = [r for r in results if r['risk_level'] == 'green']
    print(f"\n  {'─'*50}")
    print(f"  🟢 正常规则: {len(greens)}条")
    print(f"     其中信号不足(<{MIN_SIGNALS_FOR_CHECK}): "
          f"{sum(1 for r in greens if r['signal_count'] < MIN_SIGNALS_FOR_CHECK)}条")

    print(f"\n{'='*70}")
    print(f"  说明: 检测器基于83,138条回测信号, 实盘信号积累后会更新")


# ═══════════════════════ CLI ═══════════════════════

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='天眼规则失效预警引擎')
    parser.add_argument('--rule', type=str, help='检查单条规则 (如R26)')
    parser.add_argument('--top', type=int, default=0, help='只显示Top N高风险规则')
    parser.add_argument('--json', action='store_true', help='输出JSON')
    args = parser.parse_args()

    signals = load_signals()
    groups = group_by_rule(signals)
    grades = load_rule_grades()

    if args.rule:
        rid = args.rule
        if rid not in groups:
            print(f'规则 {rid} 不存在 (共{len(groups)}条规则)')
            sys.exit(1)
        result = assess_rule(rid, groups[rid], grades)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            r = result
            print(f"\n{'='*60}")
            print(f"  {r['rule_id']} | {r['rule_name']}")
            print(f"  大师: {r['master']} | 信号: {r['signal_count']}条 | 准确率: {r['accuracy']:.1%}")
            print(f"  回测等级: {r['backtest_grade']} | 风险: {r['risk_label']}")
            print(f"  CuSum: S={r['cusum'].get('cusum_value',0):.2f} "
                  f"({r['cusum'].get('cusum_level','?')})")
            print(f"  滚动窗口: z={r['rolling'].get('z_score',0):.1f} 最近={r['rolling'].get('recent_accuracy',0):.1%}")
            print(f"  连续错误: {r['consecutive'].get('consecutive_count',0)}次")
            print(f"  建议: {r['action']}")
            for reason in r['reasons']:
                print(f"    → {reason}")
    else:
        results = assess_all_rules()
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        elif args.top > 0:
            for r in results[:args.top]:
                print(f"  {r['risk_label']} {r['rule_id']}: {r['rule_name'][:40]} "
                      f"({r['accuracy']:.1%}, {r['signal_count']}条)")
        else:
            print_summary(results)
