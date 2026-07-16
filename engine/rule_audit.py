"""天眼规则审计引擎 (L2) — 矛盾检测+重叠分析+盲区扫描+置信度加权投票
检测86条规则之间的: 信号矛盾 | 条件重叠 | 市场状态盲区 | 买卖力失衡
"""
import sys, os, json
from collections import defaultdict
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rule_sources import RULE_SOURCES, get_dynamic_confidence

# ═══════════════════════════════════════════
# 规则分类标签
# ═══════════════════════════════════════════

RULE_CATEGORIES = {
    '打板': ['R14', 'R15', 'R16', 'R17', 'R18', 'R20', 'R24', 'R28', 'R29',
             'R63', 'R65', 'R74', 'R75', 'R76'],
    '排板': ['R28', 'R29'],
    '低吸': ['R01', 'R04', 'R21', 'R73'],
    '趋势': ['R08', 'R09', 'R10', 'R11', 'R12', 'R35', 'R39', 'R41', 'R42',
             'R43', 'R44', 'R45', 'R53', 'R54', 'R55', 'R56', 'R78', 'R79',
             'R80', 'R83'],
    '风控': ['R02', 'R03', 'R06', 'R07', 'R13', 'R22', 'R23', 'R25', 'R26',
             'R27', 'R30', 'R31', 'R32', 'R33', 'R34', 'R36', 'R37', 'R38',
             'R40', 'R46', 'R47', 'R49', 'R58', 'R59', 'R60', 'R62', 'R66',
             'R67', 'R68', 'R69', 'R70', 'R71', 'R72', 'R77', 'R86'],
    '宏观': ['R38', 'R48', 'R51', 'R52', 'R57'],
    '情绪': ['R19', 'R33', 'R68', 'R70', 'R72', 'R82'],
    '集中': ['R50', 'R61'],
    '仓位': ['R03', 'R11', 'R13', 'R40', 'R49', 'R52', 'R67', 'R69', 'R85'],
}

RULE_ACTIONS = {}
for rid, src in RULE_SOURCES.items():
    rule_text = src['rule']
    if any(w in rule_text for w in ['卖出', '清仓', '止损', '全清', '出局', '空仓', '离场']):
        RULE_ACTIONS[rid] = 'sell'
    elif any(w in rule_text for w in ['买入', '打板', '排板', '加仓', '低吸', '介入', '出击']):
        RULE_ACTIONS[rid] = 'buy'
    else:
        RULE_ACTIONS[rid] = 'hold'

MARKET_REGIMES = ['confirmed_uptrend', 'uptrend_pressure', 'correction', 'rally_attempt']
EMOTION_STAGES = ['主升', '高潮', '启动', '冰点', '退潮']

# ═══════════════════════════════════════════
# 矛盾检测
# ═══════════════════════════════════════════

CONTRADICTION_RULES = {
    '同向仓位冲突': {
        'desc': '两规则在同一条件下触发相反买卖方向',
        'severity': 'critical',
        'check': lambda r1, r2: (
            RULE_ACTIONS.get(r1) == 'buy' and RULE_ACTIONS.get(r2) == 'sell'
        ) or (
            RULE_ACTIONS.get(r1) == 'sell' and RULE_ACTIONS.get(r2) == 'buy'
        ),
    },
    '集中vs分散': {
        'desc': 'Druck/Loeb集中1-2只 vs 北京炒家多仓分散',
        'severity': 'warning',
        'pairs': [('R50', 'R67'), ('R61', 'R67')],
    },
    '持有周期冲突': {
        'desc': '赵老哥3天卖出(R20) vs 养家12天持有(R34) vs 徐翔30天(R07)',
        'severity': 'warning',
        'pairs': [('R20', 'R34'), ('R20', 'R07'), ('R07', 'R34')],
    },
    '止损幅度冲突': {
        'desc': '徐翔-10%(R02) vs Loeb正常-10%/熊市-3%(R59) vs 小鳄鱼-5%(R26)',
        'severity': 'info',
        'pairs': [('R02', 'R59'), ('R02', 'R26'), ('R59', 'R26')],
    },
    '牛市vs熊市互斥': {
        'desc': '牛市规则(加仓/打板) vs 熊市规则(空仓/现金) 在不同市场状态下互斥是正常的',
        'severity': 'info',
        'note': '这些矛盾是设计上的：市场状态切换时规则自然切换',
    },
}

def detect_contradictions():
    """扫描全部规则对，检测买卖矛盾"""
    results = []
    all_rids = list(RULE_SOURCES.keys())

    # 1. 硬编码矛盾对
    for name, info in CONTRADICTION_RULES.items():
        for r1, r2 in info.get('pairs', []):
            if r1 in RULE_SOURCES and r2 in RULE_SOURCES:
                results.append({
                    'type': '矛盾对',
                    'rule1': r1, 'rule2': r2,
                    'master1': RULE_SOURCES[r1]['master'],
                    'master2': RULE_SOURCES[r2]['master'],
                    'desc': name,
                    'severity': info['severity'],
                    'note': info.get('note', info.get('desc', '')),
                })

    # 2. 自动检测：同类别内买卖方向矛盾
    for cat, rids in RULE_CATEGORIES.items():
        for r1, r2 in combinations(rids, 2):
            if r1 not in RULE_SOURCES or r2 not in RULE_SOURCES:
                continue
            a1, a2 = RULE_ACTIONS.get(r1, 'hold'), RULE_ACTIONS.get(r2, 'hold')
            if (a1 == 'buy' and a2 == 'sell') or (a1 == 'sell' and a2 == 'buy'):
                # 只在趋势类中标记（打板类买卖共存是正常的）
                if cat in ('趋势', '风控'):
                    results.append({
                        'type': '自动检测矛盾',
                        'rule1': r1, 'rule2': r2,
                        'master1': RULE_SOURCES[r1]['master'],
                        'master2': RULE_SOURCES[r2]['master'],
                        'desc': f'同类别({cat})买卖方向矛盾',
                        'severity': 'warning',
                        'note': f'{r1}({RULE_ACTIONS[r1]}) vs {r2}({RULE_ACTIONS[r2]})',
                    })

    return results

# ═══════════════════════════════════════════
# 重叠分析
# ═══════════════════════════════════════════

def detect_overlaps():
    """检测规则条件重叠——多个规则本质检测同一信号"""
    overlap_groups = []

    # 跌破均线类重叠
    ma_rules = [r for r in RULE_SOURCES if '日线' in RULE_SOURCES[r]['rule'] or '均线' in RULE_SOURCES[r]['rule']]
    if len(ma_rules) > 1:
        overlap_groups.append({
            'type': '均线卖出重叠',
            'rules': ma_rules,
            'masters': list(set(RULE_SOURCES[r]['master'] for r in ma_rules)),
            'desc': '多条规则均触发于跌破X日均线，建议合并为加权投票',
            'suggestion': '保留回测效果最好的1-2条，其余降为确认信号',
        })

    # 放量突破类重叠
    breakout_rules = []
    for r, s in RULE_SOURCES.items():
        if '突破' in s['rule'] and '放量' in s['rule']:
            breakout_rules.append(r)
    if len(breakout_rules) > 2:
        overlap_groups.append({
            'type': '放量突破重叠',
            'rules': breakout_rules,
            'masters': list(set(RULE_SOURCES[r]['master'] for r in breakout_rules)),
            'desc': '多条突破规则条件高度相似',
            'suggestion': '按时间框架分层(日内/日线/周线)',
        })

    # 止损类重叠
    stop_rules = [r for r, s in RULE_SOURCES.items()
                  if any(w in s['rule'] for w in ['止损', '清仓', '卖出', '全清', '出局'])]
    overlap_groups.append({
        'type': '止损规则重叠',
        'rules': stop_rules,
        'masters': list(set(RULE_SOURCES[r]['master'] for r in stop_rules)),
        'desc': f'{len(stop_rules)}条止损/清仓规则，需分层(日止损/周止损/月止损)',
        'suggestion': '按宪法HB1-HB7分层执行，卖出无条件优先',
    })

    return overlap_groups

# ═══════════════════════════════════════════
# 盲区扫描
# ═══════════════════════════════════════════

def scan_blind_spots():
    """扫描市场状态下无规则覆盖的盲区"""
    blind_spots = []

    # 按市场状态检查覆盖率
    regime_coverage = defaultdict(list)
    emotion_coverage = defaultdict(list)

    for rid, s in RULE_SOURCES.items():
        rule = s['rule']
        master = s['master']
        # 判规则适用的市场状态
        if any(w in rule for w in ['退潮', '冰点', '下跌', '熊市', '低迷', '空仓', '现金']):
            for emo in ['退潮', '冰点']:
                emotion_coverage[emo].append(rid)
        if any(w in rule for w in ['主升', '高潮', '涨停', '连板', '打板', '突破', '上升']):
            for emo in ['主升', '高潮', '启动']:
                emotion_coverage[emo].append(rid)
        if any(w in rule for w in ['大盘下跌', '熊市', '修正']):
            regime_coverage['correction'].append(rid)
        if any(w in rule for w in ['突破', '上升', '牛市', '主升']):
            regime_coverage['confirmed_uptrend'].append(rid)

    # 检查盲区
    for regime in MARKET_REGIMES:
        coverage = regime_coverage.get(regime, [])
        if len(coverage) < 5:
            blind_spots.append({
                'type': '市场状态盲区',
                'regime': regime,
                'coverage_count': len(coverage),
                'desc': f'O\'Neil状态"{regime}"下仅有{len(coverage)}条规则覆盖',
                'severity': 'warning' if len(coverage) < 3 else 'info',
            })

    for emo in EMOTION_STAGES:
        coverage = emotion_coverage.get(emo, [])
        if len(coverage) < 5:
            blind_spots.append({
                'type': '情绪周期盲区',
                'emotion': emo,
                'coverage_count': len(coverage),
                'desc': f'养家"{emo}"阶段仅有{len(coverage)}条规则覆盖',
                'severity': 'warning' if len(coverage) < 3 else 'info',
            })

    # 检查是否有大师完全没有卖出规则
    for master in set(s['master'] for s in RULE_SOURCES.values()):
        mrules = [r for r, s in RULE_SOURCES.items() if s['master'] == master]
        has_sell = any(RULE_ACTIONS.get(r) == 'sell' for r in mrules)
        if not has_sell:
            blind_spots.append({
                'type': '卖出规则缺失',
                'master': master,
                'desc': f'{master}完全没有卖出/止损规则',
                'severity': 'critical',
            })

    return blind_spots

# ═══════════════════════════════════════════
# 投票权重计算
# ═══════════════════════════════════════════

def calc_voting_weight(rule_id):
    """计算每条规则在投票中的权重

    权重 = 动态置信度 × 类别权重 × 宪法合规因子 × 存活状态因子
    """
    dyn_conf = get_dynamic_confidence(rule_id)
    state = RULE_SOURCES.get(rule_id, {}).get('state', 'active')

    # 存活状态因子
    state_factor = {'active': 1.0, 'probation': 0.5, 'frozen': 0.0, 'retired': 0.0}

    # 宪法合规因子（小鳄鱼买入=0）
    constitution_factor = 1.0
    if state == 'frozen' and RULE_ACTIONS.get(rule_id) == 'buy':
        constitution_factor = 0.0

    return round(dyn_conf * state_factor.get(state, 0.5) * constitution_factor, 4)

def weighted_vote(rules, symbol=None):
    """对同一标的的多条规则进行置信度加权投票

    返回: {action: total_weight, ...}
    """
    votes = defaultdict(float)
    for r in rules:
        if symbol and r.get('symbol') != symbol:
            continue
        action = r.get('action', 'hold')
        weight = r.get('dynamic_confidence', calc_voting_weight(r.get('rule_id', '')))
        votes[action] += weight

    return dict(votes)

# ═══════════════════════════════════════════
# 综合审计报告
# ═══════════════════════════════════════════

def run_full_audit():
    """完整规则审计"""
    print(f"\n{'='*60}")
    print(f"  天眼规则审计 (L2)")
    print(f"{'='*60}")

    # 1. 矛盾检测
    contradictions = detect_contradictions()
    print(f"\n  --- 矛盾检测 ({len(contradictions)}项) ---")
    sev_order = {'critical': 0, 'warning': 1, 'info': 2}
    for c in sorted(contradictions, key=lambda x: sev_order.get(x['severity'], 9)):
        icon = '[严重]' if c['severity'] == 'critical' else ('[警告]' if c['severity'] == 'warning' else '[信息]')
        print(f"  {icon} {c['desc']}: {c['rule1']}({c['master1']}) vs {c['rule2']}({c['master2']})")

    # 2. 重叠分析
    overlaps = detect_overlaps()
    print(f"\n  --- 重叠分析 ({len(overlaps)}组) ---")
    for o in overlaps:
        print(f"  [{o['type']}] {o['desc']}")
        print(f"    涉及: {', '.join(o['masters'])} ({len(o['rules'])}条)")
        print(f"    建议: {o['suggestion']}")

    # 3. 盲区扫描
    blind_spots = scan_blind_spots()
    critical_blinds = [b for b in blind_spots if b['severity'] == 'critical']
    warning_blinds = [b for b in blind_spots if b['severity'] == 'warning']
    print(f"\n  --- 盲区扫描 ({len(blind_spots)}个) ---")
    print(f"    严重: {len(critical_blinds)}个 / 警告: {len(warning_blinds)}个 / 信息: {len(blind_spots)-len(critical_blinds)-len(warning_blinds)}个")
    for b in blind_spots:
        icon = '[严重]' if b['severity'] == 'critical' else ('[警告]' if b['severity'] == 'warning' else '[信息]')
        print(f"  {icon} {b['desc']}")

    # 4. 投票权重(示例)
    print(f"\n  --- 投票权重分布(动态) ---")
    weights = {}
    for rid in RULE_SOURCES:
        w = calc_voting_weight(rid)
        if w > 0:
            weights[rid] = w
    if weights:
        top5 = sorted(weights.items(), key=lambda x: -x[1])[:5]
        print(f"  Top5高权重规则:")
        for rid, w in top5:
            s = RULE_SOURCES[rid]
            print(f"    {rid} [{s['master']}] {s['rule'][:40]}... 权重:{w:.3f}")

    return {
        'contradictions': contradictions,
        'overlaps': overlaps,
        'blind_spots': blind_spots,
        'weights': weights,
    }

if __name__ == '__main__':
    run_full_audit()
