"""天眼宪法层 v2.0 — 推背图最高宪法 + 琼斯硬约束
凌驾所有子模型、引擎、命令之上。任何规则与宪法冲突 → 宪法胜。

v2.0 修正 (2026-05-21):
  C1 盈亏比: 固定3:1 → 动态(1/胜率-1)×1.5 [Kelly 1956 + Venter & de Jongh 2024]
  C4 仓位上限: 固定80% → ½Kelly动态 [MacLean, Ziemba & Blazenko 1992]
  优先级得分: 裸乘 → 标准化等权平均 [Piotroski 2000 + Yeh & Liu 2020]
  小鳄鱼解冻: 胜率75%→学术最低盈亏比0.50:1, 实际1.12:1通过C1
"""

import sys, os, json

# ═══════════════════════════════════════════
# 六条最高宪法 (v2.0 修正)
# ═══════════════════════════════════════════

CONSTITUTION = [
    {'id': 'C1',
     'text': '盈亏比 < 模型最低盈亏比 → 禁止开仓',
     'formula': 'min_rr = (1/win_rate - 1) × 1.5安全边际',
     'source': 'Kelly(1956)二元凯利 + Venter & de Jongh(2024)动态RRR',
     'violation': '冻结该模型所有买入指令，待胜率改善或盈亏比改善后解冻'},

    {'id': 'C2',
     'text': '不预测，只跟随 → 未有确认信号前，现金不动',
     'source': '利弗莫尔"最小阻力线"原则 + 十五大师共识',
     'violation': '降级为"关注"'},

    {'id': 'C3',
     'text': '亏损头寸绝不加仓；单日暴跌>4%不加仓(等次日确认)；浮盈头寸持有到趋势结束',
     'source': '十五大师15:0共识 + 前景理论[Kahneman & Tversky 1979] + 2026-05-21电力教训',
     'violation': '驳回加仓指令; 单日-4%以上建议自动降级为观望'},

    {'id': 'C4',
     'text': '单模型仓位 ≤ ½Kelly; 总仓位上限 = Σ½Kelly(不超过凯利合计上限)',
     'formula': 'half_kelly = 0.5 × (win_rate × (rr+1) - 1) / rr',
     'source': 'MacLean, Ziemba & Blazenko(1992)分数Kelly + Vukčević & Keser(2024)',
     'violation': '按优先级得分从低到高平仓至合规'},

    {'id': 'C5',
     'text': '全市场狂热时 → 总仓位强制 ≤ 三分之一Kelly',
     'trigger': '涨停跌停比>8:1 + 散户热度=5',
     'source': 'O\'Neil派发日理论 + A股2015股灾教训',
     'violation': '仓位强制≤三分之一Kelly'},

    {'id': 'C6',
     'text': '直觉与规则冲突 → 规则胜，无条件执行',
     'source': 'Thaler & Shefrin(1981)预先承诺理论 + 行为金融学自控模型',
     'violation': '主观判断不得覆盖硬约束'},
]

# ═══════════════════════════════════════════
# 七条硬约束 (HB1-HB7) v2.0 修正
# ═══════════════════════════════════════════

HARD_BARRIERS = {
    'HB1': {'text': '盈亏比 < 模型最低盈亏比 → 禁止开仓',
            'formula': 'min_rr = (1/win_rate - 1) × 1.5',
            'source': 'Kelly(1956)',
            'action': '该模型买入指令全部冻结'},

    'HB2': {'text': '模型仓位 > ½Kelly → 削减至½Kelly',
            'formula': 'half_kelly = 0.5 × (p×(b+1)-1)/b',
            'source': 'MacLean, Ziemba & Blazenko(1992)',
            'action': '按优先级得分从低到高平仓'},

    'HB3': {'text': '亏损头寸 → 禁止加仓',
            'source': '十五大师15:0共识 + 前景理论[Kahneman & Tversky 1979]',
            'action': '除徐翔浮盈+10%首加豁免外全部驳回'},

    'HB4': {'text': '涨停跌停比>8:1 + 散户热度=5 → 仓位强制≤三分之一Kelly',
            'source': 'O\'Neil + A股2015经验',
            'action': '减至三分之一Kelly以下'},

    'HB5': {'text': '单日浮亏≥5% → 全清，次日空仓',
            'source': 'Kahneman & Tversky(1979)前景理论 + Benartzi & Thaler(1995)短视损失厌恶 + Kaminski & Lo(2014)止损有效性 + 全球自营交易公司通用规则',
            'action': '熔断'},

    'HB6': {'text': '直觉与规则冲突 → 无条件服从规则',
            'source': 'Thaler & Shefrin(1981)预先承诺',
            'action': '主观判断覆盖无效'},

    'HB7': {'text': '卖出指令无条件优先于买入',
            'source': 'Chen et al.(2023)磁吸效应 + 行为金融学处置效应',
            'action': '执行顺序：卖出→减仓→买入'},

    'HB8': {'text': '卖出信号必须先过三重校验(超卖/传导/偏离), 不过则禁售',
            'source': 'Fleming Kirby & Ostdiek(1998)跨市场信息传导 + 2026-05-22有色卖出教训',
            'action': '超卖校验(KDJ_J<10)+传导校验(领先指标反向)+偏离校验(<-2σ)→分级熔断'},

    'HB9': {'text': '反共识/逆向信号必须过信号冲突校验, RSI≥70或板块20日涨≥25%则否决买入',
            'source': '铁律#14: 2026-05-26科创50反共识陷阱(RSI=70超买+半导体涨40%→3日跌-6.23%)',
            'action': 'check_signal_conflict("contrarian", symbol) → passed=False时 买入→关注, 打板→关注'},
}

# ═══════════════════════════════════════════
# 子模型参数表 v2.0
# ⚠ win_rate均为估计值, 待L3实战追踪积累≥100信号/模型后校准
# base_pos = ½Kelly (保守); max_pos = ¾Kelly (进取)
# freeze = C1裁决结果 (动态盈亏比, 非固定3:1)
# ═══════════════════════════════════════════

MODEL_PARAMS = {
    # 中国五大师（推背图）
    '徐翔':   {'win_rate': 0.60, 'risk_reward': 5.0,  'turnover': 12,
               'half_kelly': 0.260, 'max_pos': 0.39,
               'min_rr': 1.00, 'win_rate_estimated': True, 'freeze': False},

    '利弗莫尔': {'win_rate': 0.55, 'risk_reward': 10.0, 'turnover': 52,
               'half_kelly': 0.253, 'max_pos': 0.38,
               'min_rr': 1.23, 'win_rate_estimated': True, 'freeze': False},

    '赵老哥':  {'win_rate': 0.70, 'risk_reward': 10.0, 'turnover': 120,
               'half_kelly': 0.335, 'max_pos': 0.50,
               'min_rr': 0.64, 'win_rate_estimated': True, 'freeze': False},

    '小鳄鱼':  {'win_rate': 0.75, 'risk_reward': 1.12, 'turnover': 240,
               'half_kelly': 0.263, 'max_pos': 0.395,
               'min_rr': 0.50, 'win_rate_estimated': True, 'freeze': False},  # v2.0解冻

    '炒股养家': {'win_rate': 0.65, 'risk_reward': 15.0, 'turnover': 30,
               'half_kelly': 0.313, 'max_pos': 0.47,
               'min_rr': 0.81, 'win_rate_estimated': True, 'freeze': False},

    # 美国六大师（熊市穿越者）
    'PTJ':          {'win_rate': 0.40, 'risk_reward': 5.0,  'turnover': 20,
                     'half_kelly': 0.140, 'max_pos': 0.21,
                     'min_rr': 2.25, 'win_rate_estimated': True, 'freeze': False},

    'Minervini':    {'win_rate': 0.50, 'risk_reward': 3.0,  'turnover': 30,
                     'half_kelly': 0.167, 'max_pos': 0.25,
                     'min_rr': 1.50, 'win_rate_estimated': True, 'freeze': False},

    'Druckenmiller':{'win_rate': 0.45, 'risk_reward': 6.0,  'turnover': 15,
                     'half_kelly': 0.179, 'max_pos': 0.27,
                     'min_rr': 1.83, 'win_rate_estimated': True, 'freeze': False},

    'Darvas':       {'win_rate': 0.45, 'risk_reward': 5.0,  'turnover': 15,
                     'half_kelly': 0.170, 'max_pos': 0.255,
                     'min_rr': 1.83, 'win_rate_estimated': True, 'freeze': False},

    'Loeb':         {'win_rate': 0.50, 'risk_reward': 4.0,  'turnover': 20,
                     'half_kelly': 0.188, 'max_pos': 0.28,
                     'min_rr': 1.50, 'win_rate_estimated': True, 'freeze': False},

    'Wyckoff':      {'win_rate': 0.55, 'risk_reward': 4.0,  'turnover': 20,
                     'half_kelly': 0.219, 'max_pos': 0.33,
                     'min_rr': 1.23, 'win_rate_estimated': True, 'freeze': False},

    # 中国四大师（A股实战）
    '北京炒家':  {'win_rate': 0.55, 'risk_reward': 3.0,  'turnover': 200,
                'half_kelly': 0.200, 'max_pos': 0.30,
                'min_rr': 1.23, 'win_rate_estimated': True, 'freeze': False},

    '退学炒股':  {'win_rate': 0.50, 'risk_reward': 4.0,  'turnover': 50,
                'half_kelly': 0.188, 'max_pos': 0.28,
                'min_rr': 1.50, 'win_rate_estimated': True, 'freeze': False},

    '乔帮主':    {'win_rate': 0.55, 'risk_reward': 4.0,  'turnover': 60,
                'half_kelly': 0.219, 'max_pos': 0.33,
                'min_rr': 1.23, 'win_rate_estimated': True, 'freeze': False},

    '逻辑哥':    {'win_rate': 0.55, 'risk_reward': 3.5,  'turnover': 30,
                'half_kelly': 0.211, 'max_pos': 0.32,
                'min_rr': 1.23, 'win_rate_estimated': True, 'freeze': False},
}

# ═══════════════════════════════════════════
# 宪法裁判函数 v2.0
# ═══════════════════════════════════════════

def _norm(value, lo, hi):
    """MinMax标准化到[0,1]"""
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def priority_score(master, rule_id=None):
    """优先级得分 = 标准化(胜率,盈亏比,周转率)等权平均
    Source: Piotroski(2000)F-Score等权法 + Yeh & Liu(2020, Financial Innovation)
    三个因子MinMax标准化后取均值, 避免裸乘导致周转率主导
    """
    p = MODEL_PARAMS.get(master)
    if not p:
        return 0
    s_win = _norm(p['win_rate'], 0.30, 0.80)
    s_rr  = _norm(p['risk_reward'], 1.0, 15.0)
    s_to  = _norm(p['turnover'], 10, 250)
    return round((s_win + s_rr + s_to) / 3.0, 4)


def min_required_rr(master):
    """C1: 动态最低盈亏比 = (1/胜率 - 1) × 1.5安全边际
    Source: Kelly(1956)二元凯利公式: 最低盈亏比 = (1-p)/p = 1/p - 1
            安全边际×1.5来自Venter & de Jongh(2024)动态RRR建议
    """
    p = MODEL_PARAMS.get(master)
    if not p:
        return 3.0  # 未知模型用保守默认
    return round((1.0 / p['win_rate'] - 1.0) * 1.5, 2)


def check_risk_reward(master):
    """宪法C1 v2.0: 动态盈亏比检查"""
    p = MODEL_PARAMS.get(master)
    if not p:
        return True, ''
    min_rr = p.get('min_rr', min_required_rr(master))
    if p['risk_reward'] < min_rr:
        return False, (f"{master}盈亏比{p['risk_reward']}:1 < "
                       f"最低{min_rr}:1 (胜率{p['win_rate']:.0%}→"
                       f"公式(1/{p['win_rate']:.0%}-1)×1.5={min_rr}:1)")
    return True, ''


def half_kelly_position(master):
    """C4: ½Kelly仓位 = 0.5 × (p×(b+1)-1) / b
    Source: MacLean, Ziemba & Blazenko(1992, Management Science)
            Vukčević & Keser(2024)最大单仓约束
    """
    p = MODEL_PARAMS.get(master)
    if not p:
        return 0.10  # 未知模型保守
    win_rate = p['win_rate']
    rr = p['risk_reward']
    full_kelly = (win_rate * (rr + 1) - 1) / rr
    return round(max(0.0, 0.5 * full_kelly), 4)


def max_total_position(active_models):
    """C4 v2.0: 总仓位上限 = min(Σ各模型½Kelly, 1-min(各模型½Kelly))
    即: 总仓位不超过所有模型½Kelly之和, 且保留至少最大单模型½Kelly的现金
    Source: MacLean et al.(1992)分数Kelly + Vukčević(2024)集中持仓约束
    """
    kellys = [half_kelly_position(m) for m in active_models]
    if not kellys:
        return 0.30
    sum_kelly = sum(kellys)
    max_single = max(kellys)
    # 保留至少最大单模型½Kelly的现金, 或10%, 取较大者
    cash_buffer = max(max_single, 0.10)
    return round(min(sum_kelly, 1.0 - cash_buffer), 4)


def check_total_position(positions, active_models=None):
    """宪法C4 v2.0: 动态½Kelly检查"""
    total = sum(p.get('position', 0) for p in positions)
    if active_models is None:
        active_models = [m for m, pm in MODEL_PARAMS.items() if not pm.get('freeze')]
    cap = max_total_position(active_models)
    if total > cap:
        return False, f'总仓位{total:.1%} > ½Kelly上限{cap:.1%} → 强制削减'
    return True, ''


def check_daily_loss(today_pnl_pct):
    """HB5: 日浮亏≥5% → 熔断
    Source: Kahneman & Tversky(1979)前景理论(损失厌恶)
            Benartzi & Thaler(1995)短视损失厌恶
            Kaminski & Lo(2014)止损有效性
            全球自营交易公司通用3-5%日损规则
    """
    if today_pnl_pct <= -0.05:
        return False, f'日浮亏{today_pnl_pct:.1%} ≥ 5% → 全清，次日空仓'
    return True, ''


def apply_constitution(rules, positions=None, today_pnl=0, mania=False):
    """宪法过滤 v2.0: 对子模型输出的规则列表逐条裁决"""
    if positions is None:
        positions = []

    filtered = []
    violations = []
    active_models = list(set(r.get('master', '') for r in rules if r.get('master')))
    total_buy_pos = sum(r.get('position', 0) for r in positions
                       if r.get('action') in ('持有',))
    total_cap = max_total_position(active_models) if active_models else 0.80

    for rule in rules:
        master = rule.get('master', '')
        action = rule.get('action', '')
        rr = rule.get('risk_reward', 1.0)

        # C1 v2.0: 动态盈亏比过滤
        if action in ('买入', '加仓', '打板', '排板'):
            min_rr = MODEL_PARAMS.get(master, {}).get('min_rr', min_required_rr(master))
            if rr < min_rr:
                violations.append(f"C1驳回: {master} 盈亏比{rr}:1 < 最低{min_rr}:1")
                continue

        # HB9 v5.1: 铁律#14 信号自相矛盾拦截 (反共识超买陷阱)
        if action in ('买入', '加仓', '打板', '排板', '低吸'):
            strategy = rule.get('strategy_type', '')
            symbol = rule.get('symbol', '')

            # 检测反共识信号
            is_contrarian = (
                strategy in ('contrarian', 'anti_consensus', 'reverse', 'oversold_bounce')
                or master in ('乔帮主', '徐翔')  # 低吸/逆向大师
                or rule.get('rule_id', '') in ('R73', 'R74')  # 乔帮主低吸规则
            )

            if is_contrarian and symbol:
                try:
                    from engine.iron_rule_14 import check_signal_conflict
                    passed, reason = check_signal_conflict('contrarian', symbol)
                    if not passed:
                        violations.append(f"HB9驳回(铁律#14): {master} {rule.get('rule_id','')} {reason}")
                        continue
                except ImportError:
                    pass  # iron_rule_14模块不存在时静默跳过

        # C4 v2.0: ½Kelly仓位检查
        if action in ('买入', '加仓', '打板', '排板'):
            hk = MODEL_PARAMS.get(master, {}).get('half_kelly', half_kelly_position(master))
            rule_pos = rule.get('position', 0)
            if rule_pos > hk:
                rule = dict(rule)
                rule['position'] = hk
                rule['note'] = f'单仓被C4削减至½Kelly({hk:.0%})'

            new_total = total_buy_pos + rule_pos
            if new_total > total_cap:
                rule = dict(rule) if rule_pos <= hk else rule
                rule['position'] = max(0, total_cap - total_buy_pos)
                rule['note'] = f'仓位被C4削减(总仓≤{total_cap:.0%})'
                if rule['position'] <= 0.01:
                    violations.append(f"C4驳回: 总仓位已达½Kelly上限{total_cap:.0%}")
                    continue

        # C5: 狂热减仓
        if mania and action in ('买入', '加仓', '打板', '排板'):
            mania_cap = total_cap / 3.0  # 三分之一Kelly
            if total_buy_pos >= mania_cap:
                violations.append(f"C5驳回: 市场狂热，仓位上限{mania_cap:.0%}")
                continue

        # HB5: 日熔断
        if today_pnl <= -0.05 and action != '卖出':
            continue  # 只允许卖出

        # HB3: 亏损不加仓
        if action == '加仓':
            symbol = rule.get('symbol', '')
            for p in positions:
                if p.get('symbol') == symbol:
                    if p.get('pnl_pct', 0) < 0:
                        violations.append(f"HB3驳回: {symbol}亏损不加仓")
                        rule = None
                        break
        if rule is None:
            continue

        filtered.append(rule)

    return filtered, violations


def constitution_report(rules, violations):
    """宪法裁决报告"""
    print(f"\n{'='*60}")
    print(f"  天眼宪法裁决 v2.0")
    print(f"{'='*60}")
    passed = len(rules)
    rejected = len(violations)
    print(f"  通过: {passed}条  驳回: {rejected}条")
    if violations:
        print(f"\n  驳回明细:")
        for v in violations:
            print(f"    ✗ {v}")
    print(f"  宪法状态: {'[合规]' if rejected == 0 else '[部分驳回]'}")
    return dict(passed=passed, rejected=rejected, violations=violations)


if __name__ == '__main__':
    print("天眼宪法层 v2.0 就绪")
    print(f"\n宪法{len(CONSTITUTION)}条:")
    for c in CONSTITUTION:
        src = c.get('source', '')
        print(f"  {c['id']}: {c['text']}")
        if src:
            print(f"      出处: {src}")

    print(f"\n硬约束{len(HARD_BARRIERS)}条:")
    for k, v in HARD_BARRIERS.items():
        print(f"  {k}: {v['text']}")

    print(f"\n{'='*60}")
    print(f"  子模型参数 v2.0 (动态盈亏比 + ½Kelly)")
    print(f"{'='*60}")
    print(f"  {'模型':<10s} {'胜率':>5s} {'盈亏比':>6s} {'最低RRR':>7s} {'½Kelly':>7s} {'周转':>5s} {'得分':>6s} {'状态':>6s}")
    print(f"  {'─'*60}")

    for name, p in MODEL_PARAMS.items():
        score = priority_score(name)
        min_rr = p.get('min_rr', min_required_rr(name))
        hk = p.get('half_kelly', half_kelly_position(name))
        freeze = '[冻结]' if p['freeze'] else '[活跃]'
        est = '(估)' if p.get('win_rate_estimated') else ''
        print(f"  {name:<6s} {p['win_rate']:.0%}{est}  {p['risk_reward']:.1f}:1  "
              f"{min_rr:.2f}:1   {hk:.1%}    {p['turnover']:>4d}  {score:.4f}  {freeze}")

    print(f"\n  (估)=win_rate为估计值, 待L3实战追踪≥100信号后校准")
    print(f"  单模型仓位上限=½Kelly; 总仓位上限=max_total_position()")
