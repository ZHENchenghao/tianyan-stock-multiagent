"""子模型⑫ 北京炒家 — 低位首板 + 极致分仓 + 防御为王
从30万亏到8万，再从8万做到近1亿。9个月实盘大赛收益近10倍。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '北京炒家'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R63: 流通市值20-50亿+量比≥3+封单>流通市值1%→候选
    for c in candidates:
        if (20 <= c.get('float_mv', 0) <= 50 and
            c.get('vol_ratio', 0) >= 3 and
            c.get('seal_to_float_pct', 0) >= 1):
            rules.append({'rule_id': 'R63', 'master': MASTER, 'action': '关注',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"流通市值{c['float_mv']}亿+量比{c['vol_ratio']}+封单>1%流通→候选"})

    # R64: 板块内涨停≥3只→板块效应确认
    if c.get('sector_limit_up_count', 0) >= 3:
        rules.append({'rule_id': 'R64', 'master': MASTER, 'action': '可进场',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f"板块涨停{c.get('sector_limit_up_count',0)}≥3→板块效应确认"})

    # R65: 换手板6-8%区间震荡>30分钟→首选
    for c in candidates:
        if (c.get('churn_6_8_pct_shake', False) and
            c.get('churn_minutes', 0) >= 30):
            rules.append({'rule_id': 'R65', 'master': MASTER, 'action': '买入',
                          'position': 0.10, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"换手板6-8%震荡{c['churn_minutes']}分钟→首选"})

    # R66: 低开→反弹分时高点必出，无论盈亏
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('open_low', False)):
            rules.append({'rule_id': 'R66', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '低开→反弹分时高点必出，无论盈亏'})

    # R67: 单票上限按资金规模分仓
    capital = market_data.get('total_capital', 100000)
    if capital < 800000:    max_per_stock = 0.50
    elif capital < 5000000:  max_per_stock = 0.25
    else:                    max_per_stock = 0.125
    for p in positions:
        if p.get('position', 0) > max_per_stock:
            rules.append({'rule_id': 'R67', 'master': MASTER, 'action': '减仓',
                          'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"单票{p['position']:.0%}超限{max_per_stock:.0%}(资金{capital/1e4:.0f}万)→减仓"})

    # R68: 退潮期(跌停>20家)→仓位≤20%或空仓
    if market_data.get('limit_down_daily', 0) >= 20:
        for p in positions:
            rules.append({'rule_id': 'R68', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"跌停{market_data['limit_down_daily']}>20→退潮→仓位≤20%或空仓"})

    return rules
