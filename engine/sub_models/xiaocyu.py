"""子模型④ 小鳄鱼 — 隔日交易 + 四合一手法 + 三热度选股
推背图规则 R21-R27
⚠️ 全部冻结 — 盈亏比1.12:1 < 3:1 宪法C1
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '小鳄鱼'
PARAMS = MODEL_PARAMS[MASTER]
FROZEN = True  # 宪法C1冻结

def scan(market_data, candidates, positions):
    """扫描小鳄鱼7条规则（买入指令全部冻结，仅允许卖出）"""
    rules = []
    score = priority_score(MASTER)

    # R23: 次日低开 → 开盘卖出（卖出指令不受冻结影响）
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('next_day_open_chg', 0) < -1.5):
            rules.append({
                'rule_id': 'R23', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'entry_time': '开盘秒清',
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"低开{p['next_day_open_chg']}%→开盘秒清"
            })

    # R26: 低吸后当日浮亏5% → 次日止损
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('intraday_loss', 0) <= -5):
            rules.append({
                'rule_id': 'R26', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"低吸浮亏{p['intraday_loss']}%→次日止损"
            })

    # 以下买入规则在宪法层被冻结，此处仅记录条件满足情况
    # R21: 龙头首阴+换手≥25% → (冻结)
    # R22: 次日高开3%+ → (冻结)
    # R24: 反包板封单≥5万手 → (冻结)
    # R25: 点火30分钟不涨停 → (冻结，但允许卖出)
    # R27: 每日交易上限2次 → (冻结)

    return rules
