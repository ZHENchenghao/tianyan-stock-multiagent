"""子模型⑤ 炒股养家 — 情绪周期 + 一字板排板 + 格局持仓
推背图规则 R28-R34
优先级得分292.5 — 仅次于赵老哥
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '炒股养家'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    """扫描炒股养家7条规则"""
    rules = []
    score = priority_score(MASTER)  # 292.5

    # R28: 题材发酵首日 + 龙头一字板封单≥50万手 + 无开板 → 排板买入30%
    for c in candidates:
        if (c.get('theme_day1', False) and
            c.get('is_leader', False) and
            c.get('limit_up_seal', 0) >= 500000 and
            not c.get('board_opened', False)):
            rules.append({
                'rule_id': 'R28', 'master': MASTER,
                'action': '排板', 'position': 0.30,
                'symbol': c['code'],
                'entry_price': '涨停价',
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"题材首日+龙头一字板封单{c['limit_up_seal']/10000:.0f}万手→排板买入"
            })

    # R29: 排板未成交 → 次日继续排
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('action_type') == '排板' and
            not p.get('filled', False) and
            p.get('queue_days', 0) < 4):
            rules.append({
                'rule_id': 'R29', 'master': MASTER,
                'action': '排板',
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"排板第{p['queue_days']+1}天未成交→继续排"
            })

    # R31: 开板后换手率>30% → 减半仓
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('board_opened_today', False) and
            p.get('turnover_rate', 0) > 30):
            rules.append({
                'rule_id': 'R31', 'master': MASTER,
                'action': '减半仓', 'position': p.get('position', 0) * 0.5,
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"开板换手{p['turnover_rate']}%>30%→减半仓"
            })

    # R32: 连续3个一字板后开板 → 全部卖出
    for p in positions:
        if (p.get('consecutive_one_word', 0) >= 3 and
            p.get('board_opened_today', False)):
            rules.append({
                'rule_id': 'R32', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': '连续3个一字板后开板→全部卖出'
            })

    # R33: 情绪周期衰退期 → 空仓（全局）
    emotion = market_data.get('emotion_stage', '')
    if emotion in ('退潮', '极端冰点'):
        for p in positions:
            rules.append({
                'rule_id': 'R33', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f'情绪{emotion}→空仓铁律'
            })

    # R34: 持股满12天 → 全部卖出
    for p in positions:
        if p.get('master') == MASTER and p.get('hold_days', 0) >= 12:
            rules.append({
                'rule_id': 'R34', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"持股{p['hold_days']}天≥12→全部卖出"
            })

    return rules
