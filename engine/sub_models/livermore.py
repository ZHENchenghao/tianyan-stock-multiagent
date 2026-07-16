"""子模型② 利弗莫尔 — 关键点突破 + 最小阻力线 + 金字塔加仓
推背图规则 R08-R13
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '利弗莫尔'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    """扫描利弗莫尔6条规则"""
    rules = []
    score = priority_score(MASTER)

    # R08: 横盘30天以上 + 突破前高3% + 放量2倍 → 买入，仓位15%
    for c in candidates:
        if (c.get('consolidation_days', 0) >= 30 and
            c.get('breakout_pct', 0) >= 3 and
            c.get('vol_ratio', 1) >= 1.8 and
            c.get('close_near_high', False)):  # 收盘在最高点3%内
            rules.append({
                'rule_id': 'R08', 'master': MASTER,
                'action': '买入', 'position': 0.15,
                'symbol': c['code'],
                'entry_price': c.get('price'),
                'stop_loss': c.get('price', 0) * 0.985,  # 关键点-1.5%
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"关键点突破: 横盘{c['consolidation_days']}天+突破{c['breakout_pct']}%+放量{c['vol_ratio']}倍"
            })

    # R10: 突破后3天不涨 → 止损
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('days_since_entry', 0) >= 3 and
            p.get('pnl_pct', 0) <= 0.5):
            rules.append({
                'rule_id': 'R10', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"突破后{p['days_since_entry']}天涨幅≤0.5%→假突破止损"
            })

    # R11: 上涨趋势中每次回调5% → 加仓5%（最多3次）
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('pnl_pct', 0) > 0 and
            p.get('pullback_pct', 0) <= -5 and
            p.get('add_count', 0) < 3):
            rules.append({
                'rule_id': 'R11', 'master': MASTER,
                'action': '加仓', 'position': 0.05,
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"趋势回调{p['pullback_pct']}%→加仓5%(第{p['add_count']+1}次)"
            })

    # R12: 跌破20日均线 → 全部卖出
    for p in positions:
        if p.get('master') == MASTER and p.get('below_ma20', False):
            rules.append({
                'rule_id': 'R12', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': '跌破20日均线→全部卖出'
            })

    # R13: 杠杆上限不超过2倍（检测）
    for p in positions:
        if p.get('leverage', 1) > 2:
            rules.append({
                'rule_id': 'R13', 'master': MASTER,
                'action': '减仓',
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"杠杆{p['leverage']}倍>2倍上限→减仓"
            })

    return rules
