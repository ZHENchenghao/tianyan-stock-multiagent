"""子模型⑩ Loeb(杰拉尔德·勒伯) — 统领理由 + 接受亏损 + 现金为王
1929年崩盘前2个月系统性退出。E.F. Hutton创始合伙人。华尔街五大最佳交易员之一。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'Loeb'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R58: 统领理由被违反→立即卖出，不问价格
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('rationale_violated', False)):
            rules.append({'rule_id': 'R58', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"统领理由被违反→立即卖出，不问价格"})

    # R59: 止损: 正常市场10%/熊市3%
    for p in positions:
        loss_limit = 0.03 if market_data.get('bear_market', False) else 0.10
        if p.get('pnl_pct', 0) <= -loss_limit:
            rules.append({'rule_id': 'R59', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"亏损{p['pnl_pct']:.1%}≥{loss_limit:.0%}→止损"})

    # R60: 现金为王→等真正机会，不为"保持投资"而买
    if not market_data.get('has_genuine_opportunity', True):
        rules.append({'rule_id': 'R60', 'master': MASTER, 'action': '空仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '无真正机会→现金为王，不为"保持投资"而买'})

    # R61: 集中1-4只，不做撒胡椒面
    if len(positions) > 4:
        rules.append({'rule_id': 'R61', 'master': MASTER, 'action': '关注',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f'持仓{len(positions)}>4只→过度分散，建议集中1-4只'})

    # R62: 买入前必须写下：为什么买/预期涨多少/最多等多久/最多亏多少
    for c in candidates:
        if c.get('ready_to_buy', False) and not c.get('rationale_written', False):
            rules.append({'rule_id': 'R62', 'master': MASTER, 'action': '禁止买入',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'{c["code"]}未写统领理由→禁止开仓'})

    return rules
