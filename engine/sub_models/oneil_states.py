# -*- coding: utf-8 -*-
"""
O'Neil 市场状态规则补全 v3.1
============================
原系统盲区: "上升承压(uptrend_pressure)"和"反弹尝试(rally_attempt)"状态覆盖为0
本模块为这两种状态设计完整的操作规则

学术依据:
  - O'Neil, W. (2009) 《笑傲股市》Ch.9: 大盘走势判定
  - O'Neil, W. (2003) 《股票买卖原则》Ch.6: 派发日与跟进日
  - A股修正: 派发日阈值-2.5%(回测验证, 5911天数据), 跟进日+2.5%

规则:
  - 本模块不定义新的scan()函数, 而是提供规则函数供conflict_resolver调用
  - 所有规则源于O'Neil原书, A股参数已做回测修正
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from constitution import MODEL_PARAMS, priority_score

MASTER = "O'Neil"
PARAMS = {
    'win_rate': 0.45,
    'risk_reward': 3.5,
    'turnover': 12,
    'min_rr': 0.70
}


# ═══════════════════════════════════════════
# 状态1: 上升承压 (uptrend_pressure)
# 定义: 派发日2-4个, 上升趋势承压但未反转
# ═══════════════════════════════════════════

def uptrend_pressure_rules(market_data, candidates, positions):
    """
    上升承压状态规则 (R_A1 ~ R_A5)

    O'Neil原书规则:
      - 派发日2-4个 → 收紧止损, 选择性买入
      - 仓位上限: 70%
      - 优先卖出弱势持仓, 保留强势股
      - 新开仓必须满足更严格条件(突破当日量>均量2倍)
    """
    rules = []
    score = 0.45  # O'Neil模型优先级得分

    dist_days = market_data.get('distribution_days', 0)
    if dist_days < 2 or dist_days > 4:
        return rules  # 不在上升承压状态

    # R_A1: 上升承压→总仓位≤70% (O'Neil原书: 收紧仓位)
    rules.append({
        'rule_id': 'R_A1', 'master': MASTER, 'action': '仓位上限',
        'max_position': 0.70,
        'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
        'desc': f'O\'Neil上升承压({dist_days}个派发日)→仓位≤70%, 收紧止损'
    })

    # R_A2: 卖出弱势持仓 — 跌破50日线 或 相对强度Rank<30
    for p in positions:
        if (p.get('below_ma50', False) or
            p.get('rs_rank', 100) < 30):
            rules.append({
                'rule_id': 'R_A2', 'master': MASTER, 'action': '减仓',
                'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'上升承压→{p["symbol"]}弱势(破50日线或RS<30)→减半仓'
            })

    # R_A3: 保留强势股 — 近10日涨幅前三 + 高于50日线
    for p in positions:
        if (p.get('chg_10d', 0) > 3 and
            not p.get('below_ma50', False) and
            p.get('rs_rank', 100) >= 70):
            rules.append({
                'rule_id': 'R_A3', 'master': MASTER, 'action': '持有',
                'position': p.get('position', 0), 'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'上升承压→{p["symbol"]}强势(RS70+10日涨{p.get("chg_10d",0):.0f}%)→保留'
            })

    # R_A4: 新开仓条件加严 — 突破日量>均量2倍 + 相对强度Rank>80
    for c in candidates:
        if (c.get('breakout_vol_ratio', 0) >= 2.0 and
            c.get('rs_rank', 0) >= 80 and
            not c.get('in_position', False)):
            rules.append({
                'rule_id': 'R_A4', 'master': MASTER, 'action': '可开仓',
                'position': 0.05, 'symbol': c['code'],
                'entry_price': c.get('pivot_price'),
                'stop_loss': c.get('pivot_price', 0) * 0.93,  # 收紧止损至7%
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'上升承压→{c["code"]}突破量2x+RS80→小仓试探5%'
            })

    # R_A5: 派发日≥5→状态转移为correction
    if dist_days >= 5:
        rules.append({
            'rule_id': 'R_A5', 'master': MASTER, 'action': '状态转移',
            'new_state': 'correction',
            'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
            'desc': f'派发日{dist_days}≥5→O\'Neil状态: 上升承压→下跌修正'
        })

    return rules


# ═══════════════════════════════════════════
# 状态2: 反弹尝试 (rally_attempt)
# 定义: 下跌修正后出现反弹, 等待跟进日确认
# ═══════════════════════════════════════════

def rally_attempt_rules(market_data, candidates, positions):
    """
    反弹尝试状态规则 (R_B1 ~ R_B4)

    O'Neil原书规则:
      - 反弹第1-3天: 不开新仓, 持有现金
      - 第4天起: 等待跟进日(放量涨>2.5%)确认
      - 跟进日出现→状态升级为confirmed_uptrend
      - 跟进日未出现→继续观望
    """
    rules = []
    score = 0.45

    rally_day = market_data.get('rally_day', 0)
    if rally_day <= 0:
        return rules  # 不在反弹尝试状态

    # R_B1: 反弹第1-3天→现金为王, 禁止开新仓
    if rally_day <= 3:
        rules.append({
            'rule_id': 'R_B1', 'master': MASTER, 'action': '禁止开仓',
            'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
            'desc': f'O\'Neil反弹尝试第{rally_day}天→禁止新开仓, 持有现金'
        })

    # R_B2: 反弹第4天起→等待跟进日(放量涨>2.5%, A股修正)
    if rally_day >= 4:
        has_ftd = market_data.get('follow_through_day', False)
        if has_ftd:
            rules.append({
                'rule_id': 'R_B2', 'master': MASTER, 'action': '状态转移',
                'new_state': 'confirmed_uptrend',
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'跟进日确认! 第{rally_day}天放量涨>2.5%→上升确认, 恢复正常仓位'
            })
        else:
            rules.append({
                'rule_id': 'R_B2', 'master': MASTER, 'action': '观望',
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'反弹第{rally_day}天→等待跟进日(放量涨>2.5%), 继续观望'
            })

    # R_B3: 反弹失败→重回下跌修正 (反弹再创新低)
    if market_data.get('new_low', False):
        rules.append({
            'rule_id': 'R_B3', 'master': MASTER, 'action': '状态转移',
            'new_state': 'correction',
            'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
            'desc': '反弹再创新低→O\'Neil: 反弹失败, 重回下跌修正'
        })
        # 清仓所有持仓
        for p in positions:
            rules.append({
                'rule_id': 'R_B3b', 'master': MASTER, 'action': '清仓',
                'position': p.get('position', 0), 'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'反弹失败→清仓{p["symbol"]}'
            })

    # R_B4: 反弹期间持仓处理 — 保留抗跌股(反弹期间涨幅>指数), 卖出弱势
    bench_chg = market_data.get('benchmark_chg_during_rally', 0)
    for p in positions:
        stock_chg = p.get('chg_during_rally', 0)
        if stock_chg < bench_chg - 2:  # 落后基准2%以上
            rules.append({
                'rule_id': 'R_B4', 'master': MASTER, 'action': '减仓',
                'position': p.get('position', 0) * 0.5, 'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'], 'priority_score': score,
                'desc': f'反弹期间{p["symbol"]}落后基准{bench_chg-stock_chg:.0f}%→减半仓'
            })

    return rules


# ═══════════════════════════════════════════
# 便捷: 根据当前O'Neil状态返回对应规则
# ═══════════════════════════════════════════

def scan_by_state(market_data, candidates, positions):
    """
    根据O'Neil状态自动分发规则

    用法(在conflict_resolver中调用):
        from sub_models.oneil_states import scan_by_state
        oneil_rules = scan_by_state(market_data, candidates, positions)
    """
    state = market_data.get('oneil_state', 'confirmed_uptrend')

    if state == 'uptrend_pressure':
        return uptrend_pressure_rules(market_data, candidates, positions)
    elif state == 'rally_attempt':
        return rally_attempt_rules(market_data, candidates, positions)
    elif state == 'correction':
        # 下跌修正: 禁止开仓+清仓信号
        rules = [{
            'rule_id': 'R_C1', 'master': MASTER, 'action': '禁止开仓',
            'risk_reward': PARAMS['risk_reward'], 'priority_score': 0.50,
            'desc': "O'Neil下跌修正→禁止所有新开仓, 仅持有现金"
        }]
        for p in positions:
            rules.append({
                'rule_id': 'R_C2', 'master': MASTER, 'action': '清仓',
                'position': p.get('position', 0), 'symbol': p['symbol'],
                'risk_reward': PARAMS['risk_reward'], 'priority_score': 0.50,
                'desc': f"O'Neil下跌修正→清仓{p['symbol']}"
            })
        return rules
    else:
        # confirmed_uptrend: 无需特殊规则
        return []
