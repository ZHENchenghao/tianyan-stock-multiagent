"""子模型⑦ Minervini(马克·米内尔维尼) — SEPA + VCP + Stage分析 + 8条件趋势模板
两届全美投资冠军，1994-2000年均回报+220%。提前逃顶8次熊市。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = 'Minervini'
PARAMS = MODEL_PARAMS[MASTER]

def scan(market_data, candidates, positions):
    rules = []
    score = priority_score(MASTER)

    # R41: 8条件趋势模板≥7/8→可交易；≤6/8→回避
    for c in candidates:
        trend_score = c.get('minervini_score', 0)  # 0-8
        if trend_score >= 7:
            # Stage2确认，找买点
            pass  # 由R42-R43决定具体操作
        elif trend_score <= 6 and c.get('in_position', False):
            rules.append({'rule_id': 'R41', 'master': MASTER, 'action': '清仓',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"趋势模板{c.get('minervini_score',0)}/8≤6→回避/清仓"})

    # R42: VCP收缩≥33%+量缩至均量70%以下+放量突破Pivot→买点
    for c in candidates:
        if (c.get('vcp_contraction', 0) >= 33 and
            c.get('vcp_vol_shrink', 1.0) <= 0.70 and
            c.get('pivot_breakout', False) and
            c.get('breakout_vol_ratio', 0) >= 1.5):
            rules.append({'rule_id': 'R42', 'master': MASTER, 'action': '买入',
                          'position': 0.125, 'symbol': c['code'],
                          'entry_price': c.get('pivot_price'),
                          'stop_loss': c.get('pivot_price', 0) * 0.95,
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"VCP收缩{c['vcp_contraction']}%+量缩+放量突破→买入"})

    # R43: 距50日线>25%→不买（延伸过高）
    for c in candidates:
        if c.get('dist_from_ma50', 0) > 25:
            rules.append({'rule_id': 'R43', 'master': MASTER, 'action': '不买',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f"距50日线{c['dist_from_ma50']}%>25%→延伸过高不买"})

    # R44: 放量跌破50日线或150日线→硬出场
    for p in positions:
        if (p.get('master') == MASTER and
            p.get('below_ma50_heavy_vol', False)):
            rules.append({'rule_id': 'R44', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '放量跌破50/150日线→硬出场'})

    # R45: 涨20-25%→减仓25-33%，剩余跟踪止盈
    for p in positions:
        if p.get('pnl_pct', 0) >= 20:
            rules.append({'rule_id': 'R45', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0) * 0.30,
                          'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'涨{p["pnl_pct"]:.0f}%→减仓30%，剩余跟踪止盈'})

    # R46: 盈亏比<2:1→不开仓 (Minervini明确标准: 潜在收益/风险至少2:1)
    for c in candidates:
        pot_rr = c.get('potential_rr', 0)
        if pot_rr > 0 and pot_rr < 2.0 and not c.get('in_position', False):
            rules.append({'rule_id': 'R46', 'master': MASTER, 'action': '不买',
                          'symbol': c['code'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'潜在盈亏比{pot_rr:.1f}:1<2:1→不开仓(Minervini硬标准)'})

    # R47: 熊市→100%现金，不做任何交易
    if market_data.get('bear_market', False):
        for p in positions:
            rules.append({'rule_id': 'R47', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': '熊市→全清→100%现金'})

    # === v3.1 新增: Minervini卖出/止损规则 (填补盲区) ===

    # R47a: 追踪止盈 — 从最高点回撤≥10%→全卖 (Minervini核心出场策略)
    # 来源: Minervini《股票魔法师》Ch.9, "让赢家奔跑,但设追踪止盈"
    for p in positions:
        pnl = p.get('pnl_pct', 0)
        drawdown_from_peak = p.get('drawdown_from_peak', 0)
        if pnl > 0.10 and drawdown_from_peak >= 10:
            rules.append({'rule_id': 'R47a', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'浮盈{pnl:.0%}, 从高点回撤{drawdown_from_peak:.0%}≥10%→追踪止盈'})

    # R47b: 个股止损 — 亏损5-7%无条件清仓 (Minervini硬止损)
    # 来源: Minervini《股票魔法师》Ch.5, "亏损是交易的一部分,但必须控制"
    for p in positions:
        pnl = p.get('pnl_pct', 0)
        if pnl <= -0.07:
            rules.append({'rule_id': 'R47b', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'亏损{pnl:.0%}≤-7%→硬止损清仓(Minervini 5-7%规则)'})
        elif pnl <= -0.05 and p.get('below_ma50', False):
            rules.append({'rule_id': 'R47b', 'master': MASTER, 'action': '清仓',
                          'position': p.get('position', 0), 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'亏损{pnl:.0%}≤-5%且跌破50日线→双重确认止损'})

    # R47c: 放量长阴线(单日跌>4%+量>均量2倍)→减半仓
    # 来源: Minervini对"机构出货日"的判断标准
    for p in positions:
        if (p.get('daily_chg_pct', 0) <= -4.0 and
            p.get('vol_ratio', 1) >= 2.0):
            rules.append({'rule_id': 'R47c', 'master': MASTER, 'action': '减仓',
                          'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                          'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                          'desc': f'单日跌{p.get("daily_chg_pct",0):.0f}%+放量→机构出货信号, 减半仓'})

    return rules
