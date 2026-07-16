"""子模型⑪ Wyckoff(威科夫) — 量价分析 + 吸筹/派发模型 + 主力行为识别
技术分析五大巨人之一。采访JP摩根、利弗莫尔总结出完整量价系统。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'Wyckoff'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R78: 价涨量增→健康上涨；放量滞涨→主力出货
    for c in candidates:
        if c.get('vol_price_divergence', False):
            rules.append({'rule_id': 'R78', 'master': MASTER, 'action': '不买/减仓',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '放量滞涨→主力出货信号，回避'})

    # R79: Spring弹簧效应→假跌破支撑后快速拉回→供应耗尽
    for c in candidates:
        if (c.get('spring_pattern', False) and
            c.get('spring_recovery_pct', 0) >= 3):
            rules.append({'rule_id': 'R79', 'master': MASTER, 'action': '买入',
                          'position': 0.15, 'symbol': c['code'],
                          'stop_loss': c.get('spring_low', 0) * 0.98,
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': 'Spring弹簧→假跌破后快速拉回→供应耗尽→买入'})

    # R80: SOS强势信号→带量突破确认
    for c in candidates:
        if (c.get('sos_signal', False) and
            c.get('sos_vol_ratio', 0) >= 1.5):
            rules.append({'rule_id': 'R80', 'master': MASTER, 'action': '买入',
                          'position': 0.20, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': 'SOS强势信号→带量突破确认→加仓'})

    # R81: UT上冲回落→假突破阻力后快速回落→需求耗尽
    for p in positions:
        if p.get('utad_pattern', False):
            rules.append({'rule_id': 'R81', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': 'UT上冲回落→假突破+需求耗尽→清仓'})

    # R82: 努力vs结果: 量价背离→趋势不可持续
    for c in candidates:
        if c.get('effort_result_divergence', False):
            eff = c.get('effort_desc', '价格微涨量巨大')
            rules.append({'rule_id': 'R82', 'master': MASTER, 'action': '不买',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'努力vs结果背离→{eff}→抛压出现'})

    return rules
