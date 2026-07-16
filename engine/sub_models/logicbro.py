"""子模型⑮ 逻辑哥 — 量价+资金流 + 启动突破战法 + 波段
B站/公众号财经创作者。三维分析框架: 技术面+逻辑面+资金面。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = '逻辑哥'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R83: 平台整理≥10日+放量突破平台高×1.02+MACD金叉→买入
    for c in candidates:
        if (c.get('platform_days', 0) >= 10 and
            c.get('platform_breakout', False) and
            c.get('breakout_vol_ratio', 0) >= 1.5 and
            c.get('macd_golden_cross', False)):
            rules.append({'rule_id': 'R83', 'master': MASTER, 'action': '买入',
                          'position': 0.15, 'symbol': c['code'],
                          'stop_loss': c.get('platform_high', 0) * 0.95,
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"平台整理{c['platform_days']}日+突破+MACD金叉→买入"})

    # R84: 三维共振确认（技术面+资金面+逻辑面）
    for c in candidates:
        signals = sum([
            c.get('tech_signal', False),
            c.get('capital_signal', False),
            c.get('logic_signal', False)
        ])
        if signals >= 3:
            rules.append({'rule_id': 'R84', 'master': MASTER, 'action': '加仓',
                          'position': 0.10, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'三维共振({signals}/3)→加仓'})

    # R85: 固定仓位分批建仓（不是一次满上）
    for c in candidates:
        if c.get('ready_to_enter', False) and not c.get('full_position', False):
            rules.append({'rule_id': 'R85', 'master': MASTER, 'action': '买入',
                          'position': 0.10, 'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '分批建仓→首仓10%，确认后再加'})

    # R86: 趋势走弱→破位关键支撑→止损
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('trend_weakening', False) and
            p.get('broke_key_support', False)):
            rules.append({'rule_id': 'R86', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '趋势走弱+破位关键支撑→纪律止损'})

    return rules
