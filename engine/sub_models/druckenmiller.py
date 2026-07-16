"""子模型⑧ Druckenmiller(德鲁肯米勒) — 流动性驱动 + 集中重押 + 保本第一
索罗斯左右手，量子基金首席。30年0亏损年，仅5个亏损季度。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'Druckenmiller'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R48: 央行放水/降息/降准→进攻仓位；收水→降仓
    if market_data.get('cb_easing', False):
        rules.append({'rule_id': 'R48', 'master': MASTER, 'action': '可加仓',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '央行放水/降息/降准→进攻仓位'})
    if market_data.get('cb_tightening', False):
        for p in positions:
            rules.append({'rule_id': 'R48b', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '央行收水/加息/缩表→全线降仓'})

    # R49: 浮盈30-40%后才能激进（先打安全垫）
    floating = market_data.get('total_floating_pnl', 0)
    if floating >= 0.30:
        rules.append({'rule_id': 'R49', 'master': MASTER, 'action': '可激进',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f'浮盈{floating:.0%}→安全垫已厚，可激进'})
    elif floating < 0:
        rules.append({'rule_id': 'R49b', 'master': MASTER, 'action': '保守',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '浮亏状态→先保本，不冒进'})

    # R50: 集中1-2个高确信机会（不分散）
    if len(positions) > 4:
        # 保留得分最高的1-2个，其余清仓
        sorted_pos = sorted(positions, key=lambda p: p.get('priority_score', 0), reverse=True)
        for p in sorted_pos[2:]:
            rules.append({'rule_id': 'R50', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'持仓>{2}只→集中1-2高确信机会，减仓{p["symbol"]}'})

    # R51: 流动性拐点是最重要信号
    if market_data.get('liquidity_inflection', False):
        rules.append({'rule_id': 'R51', 'master': MASTER, 'action': '关注',
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': '流动性拐点出现→最重要的择时信号！'})

    # R52: 确信度+宏观+技术面共振→重仓出击
    conviction = market_data.get('conviction_score', 0)
    if conviction >= 8 and not market_data.get('bear_market', False):
        rules.append({'rule_id': 'R52', 'master': MASTER, 'action': '重仓',
                      'position': 0.50,
                      'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                      'desc': f'确信度{conviction}/10+宏观OK+技术确认→重仓出击50%'})

    # === v3.1 新增: Druckenmiller卖出/止损规则 (填补盲区) ===
    # Druckenmiller以"保本第一"著称, 30年仅5个亏损季度, 但原系统无任何止损规则

    # R52a: 保本止损 — 任何持仓亏损≥3%→减半仓, 亏损≥5%→全清
    # 来源: Druckenmiller "第一原则: 绝不亏钱" + Soros "先保本再谈收益"
    for p in positions:
        pnl = p.get('pnl_pct', 0)
        if pnl <= -0.05:
            rules.append({'rule_id': 'R52a', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'亏损{pnl:.0%}≤-5%→保本止损全清(Druckenmiller铁律: 不亏钱)'})
        elif pnl <= -0.03:
            rules.append({'rule_id': 'R52a', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'亏损{pnl:.0%}≤-3%→减半仓(Druckenmiller: 小亏止损, 不等大亏)'})

    # R52b: 宏观恶化信号→一键清仓 (Druckenmiller最核心的择时)
    # 来源: Druckenmiller "流动性拐点是最重要的信号" + 1992做空英镑
    macro_danger = (market_data.get('us10y_change_4w', 0) >= 0.30 or  # 美10Y四周急升30bp
                    market_data.get('wti_change_4w', 0) >= 15 or       # 油价四周涨15%
                    market_data.get('vix', 0) >= 30 or                 # VIX>30恐慌
                    market_data.get('cb_tightening', False))           # 央行收水
    if macro_danger:
        for p in positions:
            rules.append({'rule_id': 'R52b', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '宏观恶化信号触发→一键清仓(Druckenmiller: 流动性第一)'})

    # R52c: 集中持仓的止损 — 确信度跌破5分→全部清仓
    # 来源: Druckenmiller "如果我不再确信, 我就立刻出场"
    for p in positions:
        if p.get('master') == MASTER and conviction < 5 and p.get('pnl_pct', 0) < 0:
            rules.append({'rule_id': 'R52c', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'确信度降至{conviction}/10+浮亏→不再确信, 清仓(Druckenmiller)'})

    return rules
