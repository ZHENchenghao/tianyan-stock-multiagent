"""子模型⑭ 乔帮主 — 龙头低吸王 + 蛇口游资 + 42月500倍
北大计算机系，原亚马逊程序员。2012→2015: 42个月500倍，从20万到过亿。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '乔帮主'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R73: 龙头首碰5/10日线→低吸（龙回头）
    for c in candidates:
        if (c.get('is_sector_leader', False) and
            c.get('touching_ma5_10', False) and
            c.get('first_touch', True)):
            rules.append({'rule_id': 'R73', 'master': MASTER, 'action': '低吸',
                          'position': 0.12, 'symbol': c['code'],
                          'entry_price': c.get('ma5_or_ma10'),
                          'stop_loss': c.get('ma5_or_ma10', 0) * 0.95,
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '龙头首碰5/10日线→龙回头低吸'})

    # R74: 换手龙首阴反包→次日弱转强封板时介入
    for c in candidates:
        if (c.get('is_change_hand_dragon', False) and
            c.get('first_shadow_reversal', False) and
            c.get('next_day_weak_to_strong', False)):
            rules.append({'rule_id': 'R74', 'master': MASTER, 'action': '买入',
                          'position': 0.12, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '换手龙首阴反包+弱转强封板→介入'})

    # R75: 尾盘封板（下午板）→全天充分换手，炸板率极低
    for c in candidates:
        if (c.get('afternoon_board', False) and
            c.get('full_day_churn', False)):
            rules.append({'rule_id': 'R75', 'master': MASTER, 'action': '打板',
                          'position': 0.15, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '尾盘封板+全天充分换手→确定性高'})

    # R76: 高位横盘换手板→8-9%高位长时间震仓后封板
    for c in candidates:
        if (c.get('high_level_churn_board', False) and
            c.get('churn_at_8_9_pct', False)):
            rules.append({'rule_id': 'R76', 'master': MASTER, 'action': '打板',
                          'position': 0.15, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '高位横盘换手板→筹码已充分换手→封板确定性高'})

    # R77: 主升才是王道→不参与调整段→看不清楚就卖
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('in_consolidation', False)):
            rules.append({'rule_id': 'R77', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '进入调整段→不参与调整→全清'})

    return rules
