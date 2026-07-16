"""子模型⑨ Darvas(达瓦斯) — 箱体理论 + 远离噪音
职业舞者，全球巡演中自学炒股。3.6万→2年半200万+美元。唯一工具=巴伦周刊+电报。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'Darvas'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R53: 箱顶: 价格创新高后连续3天不突破→此为箱顶
    for c in candidates:
        if c.get('box_formed', False):
            box_high = c.get('box_high', 0)
            box_low = c.get('box_low', 0)
            cur = c.get('price', 0)
            box_range = (box_high - box_low) / box_low * 100 if box_low > 0 else 0
            rules.append({'rule_id': 'R53', 'master': MASTER, 'action': '监控',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"箱体: {box_low:.1f}-{box_high:.1f} 幅度{box_range:.1f}%"})

    # R54: 放量突破箱顶→买入
    for c in candidates:
        if (c.get('box_breakout', False) and
            c.get('box_breakout_vol', 1) >= 1.5):
            stop = c.get('box_low', c.get('price', 0) * 0.95)
            rules.append({'rule_id': 'R54', 'master': MASTER, 'action': '买入',
                          'position': 0.15, 'symbol': c['code'],
                          'entry_price': c.get('price'),
                          'stop_loss': stop,
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"放量突破箱顶{c.get('box_high')}→买入，止损=箱底{stop}"})

    # R55: 跌破箱底→止损出局
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('price', 0) < p.get('box_low', 0)):
            rules.append({'rule_id': 'R55', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"跌破箱底{p.get('box_low')}→止损出局"})

    # R56: 形成新的更高箱体→加仓（金字塔）
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('new_box_higher', False)):
            rules.append({'rule_id': 'R56', 'master': MASTER, 'action': '加仓',
                          'position': 0.10, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '形成新高箱体→金字塔加仓'})

    # R57: 大盘下跌时不做任何买入
    if market_data.get('index_declining', False):
        rules.append({'rule_id': 'R57', 'master': MASTER, 'action': '不买',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '大盘下跌→箱体假突破多→不做任何买入'})

    return rules
