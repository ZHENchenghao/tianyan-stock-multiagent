"""子模型⑬ 退学炒股 — 空仓艺术 + 回撤线机制 + 情绪节点
5万→14个月150倍。1994年生，2015年退学职业炒股。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '退学炒股'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R69: 回撤线机制: 资金在回撤线上→全仓；触及回撤线→分仓
    peak = market_data.get('capital_peak', 100000)
    current = market_data.get('current_capital', 100000)
    drawdown = (current - peak) / peak if peak > 0 else 0
    if drawdown <= -0.10:
        for p in positions:
            rules.append({'rule_id': 'R69', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'回撤{drawdown:.1%}触及回撤线→强制分仓'})

    # R70: 三种冲动情境→禁止交易
    impulse = market_data.get('impulse_warning', '')
    if impulse:
        rules.append({'rule_id': 'R70', 'master': MASTER, 'action': '空仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f'冲动情境({impulse})→禁止交易'})
    # 三种: 看到别人赚钱(踏空心理) / 连续成功后(自信爆棚) / 大亏后想扳本(报复性交易)

    # R71: 连亏后→空仓反思，不报复性交易
    if market_data.get('consecutive_losses', 0) >= 2:
        rules.append({'rule_id': 'R71', 'master': MASTER, 'action': '空仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f"连亏{market_data['consecutive_losses']}笔→空仓反思，不报复"})

    # R72: 行情好多做，行情不好少做
    if market_data.get('emotion_stage') in ('退潮', '冰点'):
        rules.append({'rule_id': 'R72', 'master': MASTER, 'action': '空仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f"行情{market_data['emotion_stage']}→少做或不做"})

    return rules
