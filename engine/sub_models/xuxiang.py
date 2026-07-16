"""子模型① 徐翔 — 情绪冰点抄底 + 妖股识别 + AB预案
推背图规则 R01-R07
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '徐翔'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    """扫描徐翔7条规则，返回触发的规则列表"""
    rules = []
    score = priority_score(MASTER)

    # R01: 连续3天跌停≥15只 + 大盘缩量30日新低 → 首次开板14:30后买入，仓位20%
    if (market_data.get('limit_down_streak', 0) >= 3 and
        market_data.get('limit_down_daily', 0) >= 15 and
        market_data.get('volume_30d_low', False)):
        for c in candidates:
            if c.get('first_open_after_lockdown', False):
                rules.append({
                    'rule_id': 'R01', 'master': MASTER,
                    'action': '买入', 'position': 0.20,
                    'symbol': c['code'], 'entry_time': '14:30',
                    'risk_reward': PARAMS['risk_reward'],
                    'priority_score': score,
                    'desc': '冰点抄底: 连3天跌停≥15+大盘缩量新低+首次开板'
                })

    # R04: 妖股流通市值<50亿 + 无机构持仓 + 涨停
    for c in candidates:
        if (c.get('float_mv', 999) < 50 and
            c.get('institution_holding', 0) == 0 and
            c.get('is_limit_up', False)):
            rules.append({
                'rule_id': 'R04', 'master': MASTER,
                'action': '买入', 'position': 0.15,
                'symbol': c['code'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"妖股: 流通市值{c.get('float_mv')}亿+无机构+涨停"
            })

    # R05: 新题材首次涨停潮 → 3天后关注龙头回调
    if market_data.get('new_theme_first_limit_up', False):
        for c in candidates:
            if c.get('is_theme_leader', False):
                rules.append({
                    'rule_id': 'R05', 'master': MASTER,
                    'action': '关注',
                    'symbol': c['code'],
                    'risk_reward': PARAMS['risk_reward'],
                    'priority_score': score,
                    'desc': '新题材涨停潮→3天后关注龙头回调'
                })

    # R06: 连续3个涨停后放量(量比>2) → 减半仓
    for p in positions:
        if (p.get('consecutive_limit_up', 0) >= 3 and
            p.get('vol_ratio', 1) > 2):
            rules.append({
                'rule_id': 'R06', 'master': MASTER,
                'action': '减半仓', 'position': p.get('position', 0) * 0.5,
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"连3涨停后放量→减半仓"
            })

    # R07: 持股满30天 → 清仓
    for p in positions:
        if p.get('hold_days', 0) >= 30:
            rules.append({
                'rule_id': 'R07', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"持股{p['hold_days']}天≥30→全部卖出"
            })

    return rules
