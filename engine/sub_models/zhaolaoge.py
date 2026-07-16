"""子模型③ 赵老哥 — 二板定龙头 + 万手封板 + 核按钮止损
推背图规则 R14-R20
优先级得分840.0 — 所有子模型中最高
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '赵老哥'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    """扫描赵老哥7条规则"""
    rules = []
    score = priority_score(MASTER)  # 840.0

    # R14: 新题材首板≥5只 → 关注次日二板
    if market_data.get('new_theme_first_board_count', 0) >= 5:
        for c in candidates:
            if (c.get('is_first_board', False) and
                c.get('board_time', '99:99') < '10:30' and
                c.get('turnover_amount', 0) > 5e8):  # >5亿
                rules.append({
                    'rule_id': 'R14', 'master': MASTER,
                    'action': '关注',
                    'symbol': c['code'],
                    'risk_reward': PARAMS['risk_reward'],
                    'priority_score': score,
                    'desc': f"首板入库: 封板{c['board_time']}+成交{c['turnover_amount']/1e8:.1f}亿"
                })

    # R15: 二板封单≥10万手 + 换手10-20% + 炸板≥5分钟 + 回封<14:00 → 打板买入25%
    for c in candidates:
        if (c.get('is_second_board', False) and
            c.get('seal_volume', 0) >= 100000 and  # ≥10万手
            10 <= c.get('turnover_rate', 0) <= 20 and
            c.get('blow_open_minutes', 0) >= 5 and
            c.get('re_seal_time', '99:99') < '14:00' and
            c.get('blow_low_pct', 0) >= 3):  # 炸板低点≥+3%
            rules.append({
                'rule_id': 'R15', 'master': MASTER,
                'action': '打板', 'position': 0.25,
                'symbol': c['code'],
                'entry_time': '次日开盘15分钟',
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"二板龙头: 封单{c['seal_volume']/10000:.0f}万手+换手{c['turnover_rate']}%"
            })

    # R16: 二板炸板回封 → 加仓10%
    for c in candidates:
        if (c.get('is_second_board', False) and
            c.get('re_sealed_after_blow', False)):
            rules.append({
                'rule_id': 'R16', 'master': MASTER,
                'action': '加仓', 'position': 0.10,
                'symbol': c['code'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': '二板炸板回封→加仓10%'
            })

    # R18: 次日低开3% → 核按钮止损
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('next_day_open_chg', 0) <= -3):
            rules.append({
                'rule_id': 'R18', 'master': MASTER,
                'action': '核按钮', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'entry_price': '跌停价',
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"低开{p['next_day_open_chg']}%→核按钮止损"
            })

    # R19: 主升浪中不做反抽（监控） / R19b: 狂热时减仓30%
    for p in positions:
        if p.get('master') == MASTER:
            mania = market_data.get('mania', False)
            if mania:
                rules.append({
                    'rule_id': 'R19b', 'master': MASTER,
                    'action': '减仓', 'position': p.get('position', 0) * 0.30,
                    'symbol': p['symbol'],
                    'risk_reward': PARAMS['risk_reward'],
                    'priority_score': score,
                    'desc': '市场狂热(涨停跌停>8:1)→减仓30%'
                })

    # R20: 持股满3天 → 全部卖出
    for p in positions:
        if p.get('master') == MASTER and p.get('hold_days', 0) >= 3:
            rules.append({
                'rule_id': 'R20', 'master': MASTER,
                'action': '清仓', 'position': p.get('position', 0),
                'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'],
                'priority_score': score,
                'desc': f"持股{p['hold_days']}天≥3→全部卖出"
            })

    return rules
