"""
天眼 V8 → 信号聚合 + 裁决引擎
=============================
骨架来自 GitHub tradermonty/edge-signal-aggregator + druckenmiller 模式
血肉用天眼已有的8个上游模块。

三层串联:
  上游层: layer1_macro / layer2_market / fragility / scenario / holdings / bayesian
  聚合层: aggregate_signals() — 加权 + 去重 + 冲突检测 + 来源追踪
  裁决层: synthesize_conviction() — 0-100综合分 + 4模式分类 + 仓位映射

用法:
  python engine/signal_aggregator.py          → 打印裁决仪表盘
  from engine.signal_aggregator import run_full_pipeline  → 返回结构化dict
"""
import sys, os, json, math
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

today = date.today()
today_str = today.isoformat()

# ═══════════════════════════════════════════
# 0. 上游模块权重配置 (对应druckenmiller 7组件)
# ═══════════════════════════════════════════
SIGNAL_WEIGHTS = {
    # 宏观体制 (最高权重 — 对应五级裁决链 L1)
    'macro_regime': {
        'weight': 0.22,
        'source': 'layer1_macro_regime()',
        'key_fields': ['regime', 'wti_status', 'us10y_status', 'stress_triggers'],
        'description': '宏观体制与流动性闸门 — 华创WTI双维框架',
    },
    # 市场结构 (对应 L3)
    'market_structure': {
        'weight': 0.18,
        'source': 'layer2_market_structure()',
        'key_fields': ['oneil_state', 'emotion_label', 'vol_trend', 'up_ratio'],
        'description': "市场量能与情绪 — O'Neil+养家",
    },
    # 脆弱性地图
    'fragility': {
        'weight': 0.15,
        'source': 'layer2b_fragility()',
        'key_fields': ['energy_score', 'overall_fragility', 'dominant_signal', 'attack_stance'],
        'description': '攻防能量对比 — 市场脆弱性地图',
    },
    # 9维交叉矩阵 (所有持仓汇总)
    'cross_validation': {
        'weight': 0.18,
        'source': 'cross_validation_matrix() × N',
        'key_fields': ['bullish_count', 'bearish_count', 'verdict'],
        'description': '9维交叉验证矩阵 — 纯投票制',
    },
    # 情景推演
    'scenario': {
        'weight': 0.12,
        'source': 'scenario_engine 六法则',
        'key_fields': ['bayesian_posterior', 'scenario_probs', 'stress_test'],
        'description': '六法则情景推演 — 预期差+四象限+百分位+压力测试',
    },
    # 贝叶斯认知熔断
    'bayesian': {
        'weight': 0.10,
        'source': 'bayesian_loop_update()',
        'key_fields': ['posterior', 'error_streak', 'alert_level'],
        'description': '贝叶斯学习回路 — 认知熔断监测',
    },
    # 铁律校验
    'iron_laws': {
        'weight': 0.05,
        'source': 'unified_adapter._build_iron_laws()',
        'key_fields': ['negative_list_vetoes', 'correction_lines'],
        'description': '铁律硬约束 — 负面清单+纠错线',
    },
}

# 4模式分类 (对应druckenmiller 4 patterns)
CONVICTION_PATTERNS = {
    'RISK_ON_进攻': {
        'trigger': '综合分≥70 且 宏观≠DEFENSE 且 贝叶斯未熔断',
        'exposure': '70-100%',
        'description': '多维度共振看多 — 重仓出击窗口',
    },
    'STRUCTURAL_结构性': {
        'trigger': '综合分40-69 或 宏观CAUTION但市场强势',
        'exposure': '40-70%',
        'description': '多空交织 — 结构性行情，精选方向',
    },
    'DEFENSE_防御': {
        'trigger': '综合分20-39 或 宏观DEFENSE 或 贝叶斯熔断',
        'exposure': '10-40%',
        'description': '宏观压制或认知熔断 — 保本优先',
    },
    'CRISIS_危机': {
        'trigger': '综合分<20 或 stress_triggers≥4',
        'exposure': '0-20%',
        'description': '多因子共振危机 — 现金+黄金避险',
    },
}


def run_upstream_modules():
    """Step 1: 逐一跑上游模块，返回标准化信号dict。每个模块独立跑，互不依赖。"""
    signals = {}
    errors = []

    # --- 宏观体制 ---
    try:
        from unified_verdict import layer1_macro_regime
        l1 = layer1_macro_regime()
        signals['macro_regime'] = {
            'status': 'ok',
            'regime': l1.get('regime', 'UNKNOWN'),
            'regime_desc': l1.get('regime_desc', ''),
            'stress_triggers': l1.get('stress_triggers', 0),
            'veto_list': l1.get('veto_list', []),
            'wti': l1.get('wti'),
            'us10y': l1.get('us10y'),
            'cnh': l1.get('cnh'),
            'spx': l1.get('spx'),
            'spx_status': l1.get('spx_status', ''),
            'nasdaq': l1.get('nasdaq'),
            'direction': 'BEARISH' if 'DEFENSE' in l1.get('regime', '') else 'NEUTRAL',
            'raw_score': _regime_to_score(l1),
        }
    except Exception as e:
        errors.append(f'macro_regime: {e}')
        signals['macro_regime'] = {'status': 'error', 'error': str(e)}

    # --- 市场结构 ---
    try:
        from unified_verdict import layer2_market_structure
        l2 = layer2_market_structure(signals.get('macro_regime', {}))
        signals['market_structure'] = {
            'status': 'ok',
            'oneil_state': l2.get('oneil_state', '?'),
            'emotion_label': l2.get('emotion_label', '?'),
            'emotion_score': l2.get('emotion_score', 50),
            'vol_trend': l2.get('vol_analysis', {}).get('vol_trend', '?'),
            'vol_sufficient': l2.get('vol_analysis', {}).get('vol_sufficient', False),
            'up_ratio': l2.get('up_ratio'),
            'base_win_rate': l2.get('base_win_rate', 0.45),
            'adj_win_rate': l2.get('adj_win_rate', 0.30),
            'direction': _market_to_direction(l2),
            'raw_score': _market_to_score(l2),
        }
    except Exception as e:
        errors.append(f'market_structure: {e}')
        signals['market_structure'] = {'status': 'error', 'error': str(e)}

    # --- 脆弱性 ---
    try:
        from unified_verdict import layer2b_fragility
        frag = layer2b_fragility()
        signals['fragility'] = {
            'status': 'ok',
            'energy_score': frag.get('energy_score', 0),
            'fragility_score': frag.get('overall_fragility', 0),
            'dominant_signal': frag.get('dominant_signal', '?'),
            'attack_stance': frag.get('attack_stance', '?'),
            'available': frag.get('available', False),
            'direction': 'BULLISH' if frag.get('energy_score', 0) > frag.get('overall_fragility', 0) else 'BEARISH',
            'raw_score': _fragility_to_score(frag),
        }
    except Exception as e:
        errors.append(f'fragility: {e}')
        signals['fragility'] = {'status': 'error', 'error': str(e)}

    # --- 持仓交叉验证 ---
    try:
        from unified_verdict import generate_unified_report
        report = generate_unified_report()
        holdings = report.get('holdings', [])
        cross_signals = _extract_cross_validation(holdings)
        signals['cross_validation'] = cross_signals
        # 同时保存持仓详情
        signals['_holdings'] = holdings
        signals['_layer1'] = report.get('layer1', {})
        signals['_bayes_meltdown'] = report.get('bayes_meltdown', False)
    except Exception as e:
        errors.append(f'cross_validation: {e}')
        signals['cross_validation'] = {'status': 'error', 'error': str(e)}

    # --- 贝叶斯 ---
    try:
        bayes = _load_bayesian_state()
        signals['bayesian'] = {
            'status': 'ok',
            'posterior': bayes.get('posterior', 0.5),
            'error_streak': bayes.get('error_streak', 0),
            'alert_level': bayes.get('alert_level', 'green'),
            'meltdown': bayes.get('alert_level') == 'red',
            'direction': 'BEARISH' if bayes.get('alert_level') == 'red' else 'NEUTRAL',
            'raw_score': _bayes_to_score(bayes),
        }
    except Exception as e:
        errors.append(f'bayesian: {e}')
        signals['bayesian'] = {'status': 'error', 'error': str(e)}

    # --- 铁律 ---
    try:
        iron = _check_iron_laws(signals)
        signals['iron_laws'] = {
            'status': 'ok',
            'vetoes': iron.get('vetoes', []),
            'corrections_triggered': iron.get('corrections', []),
            'direction': 'BEARISH' if iron.get('vetoes') else 'NEUTRAL',
            'raw_score': _iron_to_score(iron),
        }
    except Exception as e:
        errors.append(f'iron_laws: {e}')
        signals['iron_laws'] = {'status': 'error', 'error': str(e)}

    return signals, errors


def aggregate_signals(signals):
    """
    Step 2: 信号聚合 (对应 edge-signal-aggregator)
    - 加权打分
    - 冲突检测
    - 来源追踪
    """
    weighted_signals = []
    total_weight = 0
    weighted_sum = 0

    contradictions = []

    for key, cfg in SIGNAL_WEIGHTS.items():
        sig = signals.get(key, {})
        if sig.get('status') != 'ok':
            continue

        weight = cfg['weight']
        raw = sig.get('raw_score', 50)
        direction = sig.get('direction', 'NEUTRAL')

        weighted_signals.append({
            'module': key,
            'description': cfg['description'],
            'weight': weight,
            'raw_score': raw,
            'weighted_contribution': round(raw * weight, 2),
            'direction': direction,
        })

        total_weight += weight
        weighted_sum += raw * weight

    # 归一化综合分
    composite_score = round(weighted_sum / total_weight, 1) if total_weight > 0 else 50.0

    # 冲突检测: 方向相反的信号
    bullish_modules = [s for s in weighted_signals if s['direction'] == 'BULLISH']
    bearish_modules = [s for s in weighted_signals if s['direction'] == 'BEARISH']

    if bullish_modules and bearish_modules:
        for b in bullish_modules:
            for br in bearish_modules:
                contradictions.append({
                    'type': '方向冲突',
                    'bullish_source': f"{b['module']}({b['raw_score']})",
                    'bearish_source': f"{br['module']}({br['raw_score']})",
                    'weight_diff': round(b['weight'] - br['weight'], 2),
                    'resolution_hint': _resolve_contradiction(b, br),
                })

    return {
        'composite_score': composite_score,
        'total_weight': round(total_weight, 2),
        'weighted_signals': weighted_signals,
        'contradictions': contradictions,
        'bullish_count': len(bullish_modules),
        'bearish_count': len(bearish_modules),
        'neutral_count': len(weighted_signals) - len(bullish_modules) - len(bearish_modules),
    }


def synthesize_conviction(aggregated, signals):
    """
    Step 3: 综合裁决 (对应 druckenmiller strategy_synthesizer)
    0-100 → 4模式 → 仓位映射
    """
    score = aggregated['composite_score']
    contradictions = aggregated['contradictions']
    bayes_meltdown = signals.get('_bayes_meltdown', False)
    regime = signals.get('macro_regime', {}).get('regime', 'UNKNOWN')

    # 模式判定
    if score < 20 or signals.get('macro_regime', {}).get('stress_triggers', 0) >= 4:
        pattern = 'CRISIS_危机'
    elif score < 40 or 'DEFENSE' in regime or bayes_meltdown:
        pattern = 'DEFENSE_防御'
    elif score < 70 or 'CAUTION' in regime:
        pattern = 'STRUCTURAL_结构性'
    else:
        pattern = 'RISK_ON_进攻'

    pattern_info = CONVICTION_PATTERNS.get(pattern, CONVICTION_PATTERNS['STRUCTURAL_结构性'])

    # 仓位映射
    if score >= 80:
        target_exposure = 0.90
        guidance = '多维度共振看多，重仓出击'
    elif score >= 60:
        target_exposure = 0.70
        guidance = '信号偏多，标准风控仓位'
    elif score >= 40:
        target_exposure = 0.50
        guidance = '多空交织，半仓应对'
    elif score >= 20:
        target_exposure = 0.25
        guidance = '防御为主，保本优先'
    else:
        target_exposure = 0.10
        guidance = '危机模式，现金为王'

    # 贝叶斯熔断覆盖
    if bayes_meltdown:
        target_exposure = min(target_exposure, 0.20)
        guidance = f'🔴 贝叶斯熔断生效 → {guidance}，仓位上限锁定20%'

    # 信号收敛度
    convergence = _calc_convergence(aggregated)

    return {
        'conviction_score': score,
        'pattern': pattern,
        'pattern_desc': pattern_info['description'],
        'target_exposure': target_exposure,
        'guidance': guidance,
        'signal_convergence': convergence,
        'contradiction_count': len(contradictions),
        'contradictions_pending': len([c for c in contradictions if not c.get('resolved')]),
        'bayes_meltdown_active': bayes_meltdown,
    }


def run_full_pipeline():
    """一键全链: 上游 → 聚合 → 裁决 → 返回结构化dict"""
    # Step 1: 上游
    signals, errors = run_upstream_modules()

    # Step 2: 聚合
    aggregated = aggregate_signals(signals)

    # Step 3: 裁决
    conviction = synthesize_conviction(aggregated, signals)

    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'date': today_str,
        # === 上游信号明细 ===
        'upstream': {
            'modules_run': len([s for s in signals.values() if isinstance(s, dict) and s.get('status') == 'ok']),
            'modules_failed': len(errors),
            'errors': errors,
            'signal_detail': {k: {
                'status': v.get('status', '?'),
                'direction': v.get('direction', '?'),
                'raw_score': v.get('raw_score', '?'),
            } for k, v in signals.items() if k != '_holdings' and k != '_layer1' and k != '_bayes_meltdown'},
        },
        # === 聚合仪表盘 ===
        'aggregation': {
            'composite_score': aggregated['composite_score'],
            'weighted_signals': aggregated['weighted_signals'],
            'contradictions': aggregated['contradictions'],
            'vote_summary': f"{aggregated['bullish_count']}看多/{aggregated['neutral_count']}中性/{aggregated['bearish_count']}看空",
        },
        # === 综合裁决 ===
        'conviction': conviction,
        # === 持仓摘要 ===
        'holdings_summary': _summarize_holdings(signals.get('_holdings', [])),
        # === 铁律 ===
        'iron_laws': {
            '五级裁决链': {
                'L1宏观': signals.get('macro_regime', {}).get('regime', '?'),
                'L2账户': '月亏5%/回撤10%硬约束',
                'L3市场': signals.get('market_structure', {}).get('oneil_state', '?'),
                'L4板块': '资金流>景气',
                'L5个股': '蒂尔Q4致命Bug无条件排除',
            },
            '纠错线': _collect_correction_lines(signals.get('_holdings', [])),
        },
    }


# ═══════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════

def _regime_to_score(l1):
    """宏观体制 → 0-100信号分"""
    regime = l1.get('regime', '')
    if 'PANIC' in regime or 'CRISIS' in regime:
        return 5
    elif 'DEFENSE' in regime:
        return 20
    elif 'CAUTION' in regime:
        return 40
    elif 'NEUTRAL' in regime:
        return 60
    else:
        return 75

def _market_to_direction(l2):
    state = l2.get('oneil_state', '')
    if 'confirmed_uptrend' in state or '上升' in str(state):
        return 'BULLISH'
    elif 'correction' in state or '修正' in str(state) or '下降' in str(state):
        return 'BEARISH'
    return 'NEUTRAL'

def _market_to_score(l2):
    state = l2.get('oneil_state', '')
    emo = l2.get('emotion_score', 50)
    vol_ok = l2.get('vol_analysis', {}).get('vol_sufficient', False)
    score = 50
    if 'confirmed_uptrend' in state:
        score += 20
    elif 'correction' in state:
        score -= 20
    if emo < 30:
        score -= 10
    elif emo > 70:
        score += 10
    if not vol_ok:
        score -= 10
    return max(0, min(100, score))

def _fragility_to_score(frag):
    energy = frag.get('energy_score', 0)
    fragility = frag.get('overall_fragility', 0)
    if energy > fragility:
        return 50 + min(30, (energy - fragility) * 10)
    else:
        return 50 - min(30, (fragility - energy) * 10)

def _extract_cross_validation(holdings):
    if not holdings:
        return {'status': 'ok', 'direction': 'NEUTRAL', 'raw_score': 50}
    total_bull = sum(1 for h in holdings if h.get('matrix', {}).get('verdict', '') in ('加仓', '持有偏多'))
    total_bear = sum(1 for h in holdings if h.get('matrix', {}).get('verdict', '') in ('减仓', '清仓', '观望偏空'))
    n = len(holdings)
    if n == 0:
        return {'status': 'ok', 'direction': 'NEUTRAL', 'raw_score': 50}
    net = (total_bull - total_bear) / n
    score = 50 + net * 40
    direction = 'BULLISH' if net > 0.2 else ('BEARISH' if net < -0.2 else 'NEUTRAL')
    return {
        'status': 'ok',
        'direction': direction,
        'raw_score': round(max(0, min(100, score)), 1),
        'bullish_holdings': total_bull,
        'bearish_holdings': total_bear,
        'total_holdings': n,
    }

def _load_bayesian_state():
    bsf = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bayesian_state.json')
    if os.path.exists(bsf):
        with open(bsf, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'posterior': 0.5, 'error_streak': 0, 'alert_level': 'green'}

def _bayes_to_score(bayes):
    posterior = bayes.get('posterior', 0.5)
    streak = bayes.get('error_streak', 0)
    alert = bayes.get('alert_level', 'green')
    if alert == 'red':
        return 5
    elif streak >= 3:
        return 20
    elif posterior < 0.45:
        return 35
    elif posterior > 0.55:
        return 70
    else:
        return 50

def _check_iron_laws(signals):
    vetoes = []
    corrections = []
    holdings = signals.get('_holdings', [])
    for h in holdings:
        v = h.get('matrix', {}).get('verdict', '')
        # 铁律#6: 极值区双向拦截
        dims = h.get('matrix', {}).get('dimensions', [])
        for d in dims:
            sig = d.get('signal', '')
            if 'oversold' in str(sig).lower() and v in ('减仓', '清仓'):
                vetoes.append({'holding': h.get('name'), 'law': '铁律#6超卖禁卖', 'override': '持有'})
            if 'overbought' in str(sig).lower() and v in ('加仓', '买入'):
                vetoes.append({'holding': h.get('name'), 'law': '铁律#6超买禁买', 'override': '持有'})
        # 纠错线触发
        corr = h.get('correction_line', '')
        if corr and ('触发' in str(corr) or '跌破' in str(corr)):
            corrections.append({'holding': h.get('name'), 'correction': corr})
    return {'vetoes': vetoes, 'corrections': corrections}

def _iron_to_score(iron):
    vetoes = iron.get('vetoes', [])
    if len(vetoes) >= 2:
        return 10
    elif len(vetoes) == 1:
        return 30
    return 50

def _resolve_contradiction(bullish, bearish):
    """冲突裁决: 五级裁决链 — L1宏观 > L3市场 > 脆弱性 > 贝叶斯"""
    priority = ['macro_regime', 'market_structure', 'fragility', 'cross_validation', 'bayesian', 'iron_laws']
    b_idx = priority.index(bullish['module']) if bullish['module'] in priority else 99
    br_idx = priority.index(bearish['module']) if bearish['module'] in priority else 99
    if b_idx < br_idx:
        return f"采信 {bullish['module']}（裁决链优先级更高）"
    elif br_idx < b_idx:
        return f"采信 {bearish['module']}（裁决链优先级更高）"
    else:
        return '同级冲突 → 取加权分高者或人工裁决'

def _calc_convergence(aggregated):
    signals = aggregated['weighted_signals']
    directions = [s['direction'] for s in signals]
    bullish = directions.count('BULLISH')
    bearish = directions.count('BEARISH')
    neutral = directions.count('NEUTRAL')
    total = len(directions)
    if total == 0:
        return 0
    # 收敛度 = 多数方向占比
    majority = max(bullish, bearish, neutral)
    return round(majority / total * 100, 1)

def _summarize_holdings(holdings):
    result = []
    for h in holdings:
        m = h.get('matrix', {})
        result.append({
            'name': h.get('name', '?'),
            'code': h.get('code', '?'),
            'verdict': m.get('verdict', '?'),
            'votes': f"{m.get('bullish_count', 0)}多/{m.get('bearish_count', 0)}空",
            'correction': h.get('correction_line', ''),
        })
    return result

def _collect_correction_lines(holdings):
    corrections = []
    for h in holdings:
        c = h.get('correction_line', '')
        if c:
            corrections.append({'holding': h.get('name', '?'), 'line': c})
    return corrections


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════
if __name__ == '__main__':
    print('=' * 70)
    print(f'  天眼 V8 信号聚合引擎 — {today_str}')
    print('=' * 70)

    result = run_full_pipeline()

    # 上游信号一览
    print('\n📡 【上游信号】')
    for k, v in result['upstream']['signal_detail'].items():
        direction_icon = {'BULLISH': '🟢', 'BEARISH': '🔴', 'NEUTRAL': '🟡'}.get(v.get('direction'), '⚪')
        print(f'  {direction_icon} {k}: score={v.get("raw_score")} dir={v.get("direction")}')

    # 聚合
    agg = result['aggregation']
    print(f'\n📊 【聚合仪表盘】')
    print(f'  综合分: {agg["composite_score"]}/100')
    print(f'  投票: {agg["vote_summary"]}')
    for s in agg['weighted_signals']:
        bar = '█' * int(s['weighted_contribution'] / 2)
        print(f'  {s["module"]}: w={s["weight"]:.0%} raw={s["raw_score"]} contrib={s["weighted_contribution"]:.1f} {bar}')

    # 冲突
    if agg['contradictions']:
        print(f'\n⚠️  【冲突检测】({len(agg["contradictions"])}个)')
        for c in agg['contradictions']:
            print(f'  🔴 {c["bullish_source"]} vs 🟢 {c["bearish_source"]}')
            print(f'     → {c["resolution_hint"]}')

    # 裁决
    conv = result['conviction']
    print(f'\n🎯 【综合裁决】')
    print(f'  模式: {conv["pattern"]} — {conv["pattern_desc"]}')
    print(f'  目标仓位: {conv["target_exposure"]:.0%}')
    print(f'  信号收敛度: {conv["signal_convergence"]}%')
    print(f'  指引: {conv["guidance"]}')

    # 持仓
    print(f'\n📋 【持仓快照】')
    for h in result['holdings_summary']:
        print(f'  {h["name"]}({h["code"]}): {h["verdict"]} ({h["votes"]})')

    # 铁律
    iron = result['iron_laws']
    print(f'\n⚖️  【五级裁决链】')
    for level, value in iron['五级裁决链'].items():
        print(f'  {level}: {value}')

    if result['upstream']['errors']:
        print(f'\n❌ 【上游错误】')
        for e in result['upstream']['errors']:
            print(f'  - {e}')
