"""子模型⑥ PTJ(保罗·都铎·琼斯) — 200日线铁律 + 5:1盈亏比 + 月度硬止损
40年0亏损年，仅5个亏损季度。1987黑色星期一当月大赚62%。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'PTJ'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R35: 价格>200日线→做多/持有；价格<200日线→清仓走人
    if market_data.get('below_ma200', False):
        for p in positions:
            rules.append({'rule_id': 'R35', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '价格<200日线→清仓转入防守'})

    # R36: 月度亏损≥5%→当月停止交易
    if market_data.get('monthly_pnl', 0) <= -0.05:
        rules.append({'rule_id': 'R36', 'master': MASTER, 'action': '空仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f"月亏损{market_data['monthly_pnl']:.1%}≥5%→当月强制停手"})

    # R37: 亏损时缩仓（连续亏损→缩小仓位）
    if market_data.get('consecutive_losses', 0) >= 2:
        rules.append({'rule_id': 'R37', 'master': MASTER, 'action': '减仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f"连亏{market_data['consecutive_losses']}笔→强制缩仓"})

    # R38: 流动性收紧→全线降仓（监控MLF/LPR/降准/社融/Shibor）
    if market_data.get('liquidity_tightening', False):
        for p in positions:
            rules.append({'rule_id': 'R38', 'master': MASTER, 'action': '减半仓',
                          'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '央行收水/加息/缩表→全线降仓'})

    # R39: 宏大失衡机会→重押（多条件共振）
    if (market_data.get('macro_imbalance', False) and
        market_data.get('technical_confirmation', False) and
        not market_data.get('below_ma200', False)):
        rules.append({'rule_id': 'R39', 'master': MASTER, 'action': '买入',
                      'position': 0.30,  # 重押
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '宏观失衡+技术确认+200日线上→重押机会'})

    # R40: 浮盈30-40%后→更激进（Druck浮盈安全垫）
    for p in positions:
        if p.get('floating_pnl', 0) >= 0.30:
            rules.append({'rule_id': 'R40', 'master': MASTER, 'action': '可加仓',
                          'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"浮盈{p['floating_pnl']:.0%}→可激进加仓"})

    return rules
