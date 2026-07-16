"""天眼策略引擎 — 风控裁决器（回撤控制系统落地）

铁律#1-#5集成: 所有风控建议必须经过 iron_law 验证
"""
import json, os, sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from constitution import min_required_rr

# ============ 配置 ============
RISK_FILE = os.path.join(os.path.dirname(__file__), '..', 'risk_state.json')

RISK_CONFIG = {
    'stop_loss': {
        # 个股止损5-7% [O'Neil + Loeb + 徐翔共识]
        'confirmed_uptrend': {'floor': 0.05, 'cap': 0.07},
        'uptrend_pressure': {'floor': 0.04, 'cap': 0.07},
        'correction': {'floor': 0.03, 'cap': 0.07},
    },
    'portfolio_drawdown': {
        # 组合回撤级别 [用户定义硬约束]
        'yellow': 0.05,    # 黄警：仓位≤50%
        'orange': 0.07,    # 橙警：仓位≤30%
        'red': 0.10,       # 红警：停开仓+减半
    },
    'monthly_loss': {
        # 月度亏损线 [PTJ铁律: 3.5%预警/5%停手]
        'warn': 0.035,
        'stop': 0.05,
    },
    'consecutive_losses': 3,  # [徐翔+小鳄鱼连亏保护]
    # 盈亏比最低阈值改用动态公式: min_required_rr(model) from constitution.py
    # 公式: (1/胜率-1)×1.5安全边际 [Kelly 1956]
}

def load_risk_state():
    if os.path.exists(RISK_FILE):
        with open(RISK_FILE, 'r') as f:
            return json.load(f)
    return {
        'portfolio_peak': 10000,
        'current_value': 10000,
        'month_start': 10000,
        'drawdown': 0,
        'monthly_pnl': 0,
        'consecutive_losses': 0,
        'market_state': 'confirmed_uptrend',
        'is_paused': False,
        'monthly_stopped': False,
        'recovery_week': 0,
        'last_update': str(date.today())
    }

def save_risk_state(state):
    state['last_update'] = str(date.today())
    with open(RISK_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def calc_stop_loss(entry_price, market_state, atr_pct=0.03):
    """个股动态止损计算"""
    cfg = RISK_CONFIG['stop_loss'].get(market_state, RISK_CONFIG['stop_loss']['confirmed_uptrend'])
    atr_stop = atr_pct * 1.5
    stop_pct = max(cfg['floor'], min(atr_stop, cfg['cap']))
    return entry_price * (1 - stop_pct), stop_pct

def check_portfolio_risk(current_value, peak_value):
    """组合回撤检查"""
    drawdown = (current_value - peak_value) / peak_value
    cfg = RISK_CONFIG['portfolio_drawdown']

    if drawdown <= -cfg['red']:
        return 'red', abs(drawdown), '停止新开仓 + 减半仓 + 启动恢复协议', 0.30
    elif drawdown <= -cfg['orange']:
        return 'orange', abs(drawdown), '仓位≤30% + 不加仓', 0.30
    elif drawdown <= -cfg['yellow']:
        return 'yellow', abs(drawdown), '仓位≤50% + 不加仓', 0.50
    return 'green', abs(drawdown), '正常', 1.0

def check_monthly_pnl(current_value, month_start):
    """月度亏损线检查"""
    pnl = (current_value - month_start) / month_start
    cfg = RISK_CONFIG['monthly_loss']

    if pnl <= -cfg['stop']:
        return 'stop', pnl, '当月强制停手，不开新仓', 0.0
    elif pnl <= -cfg['warn']:
        return 'warn', pnl, '仓位≤30%预警', 0.30
    return 'green', pnl, '正常', 1.0

def check_consecutive_losses(loss_list):
    """连续亏损检查"""
    cfg = RISK_CONFIG['consecutive_losses']
    consecutive = 0
    for loss in loss_list:
        if loss:
            consecutive += 1
        else:
            break
    if consecutive >= cfg:
        return True, consecutive, f'连亏{consecutive}笔 → 暂停新开仓'
    return False, consecutive, '正常'

def jones_filter(risk_reward_ratio, master=None):
    """琼斯盈亏比过滤 v2.0 — 动态最低盈亏比
    Source: Kelly(1956) → min_rr = (1/win_rate-1)×1.5安全边际
    未指定master时使用保守默认3.0
    """
    if master:
        return risk_reward_ratio >= min_required_rr(master)
    return risk_reward_ratio >= 3.0  # 保守默认值

def risk_report(portfolio_file=None):
    """生成完整风控报告"""
    state = load_risk_state()

    print("=" * 60)
    print("  天眼风控裁决器")
    print("=" * 60)

    # 1. 组合回撤
    level, dd, msg, cap = check_portfolio_risk(
        state['current_value'], state['portfolio_peak'])
    print(f"\n[组合回撤] {level.upper()} 回撤{dd:.1%} → {msg}")

    # 2. 月度亏损
    mlvl, mpnl, mmsg, mcap = check_monthly_pnl(
        state['current_value'], state['month_start'])
    print(f"[月度亏损] {mlvl.upper()} {mpnl:+.1%} → {mmsg}")

    # 3. 连续亏损
    triggered, count, cmsg = check_consecutive_losses(
        [False, False, False])  # 简化，需接入真实交易记录
    print(f"[连续亏损] {count}笔 → {cmsg}")

    # 4. 个股止损示例
    stop_price, stop_pct = calc_stop_loss(10.0, state['market_state'], 0.03)
    print(f"[个股止损] 买入价10.0 → 止损{stop_price:.2f} ({stop_pct:.1%})")

    # 综合仓位天花板
    final_cap = min(cap, mcap)
    if triggered:
        final_cap = min(final_cap, 0.30)

    # ====== 铁律: 裁决链优先 ======
    print(f"\n  裁决链仓位上限: {final_cap:.0%}")
    print(f"  此为最终答案。芭菲/O'Neil/养家仓位若与此冲突，以此为准。")

    print(f"\n>>> 风控仓位天花板: {final_cap:.0%}")

    return {
        'portfolio_risk': {'level': level, 'drawdown': dd, 'cap': cap},
        'monthly_pnl': {'level': mlvl, 'pnl': mpnl, 'cap': mcap},
        'consecutive': {'triggered': triggered, 'count': count},
        'stop_loss_example': {'entry': 10.0, 'stop': stop_price, 'pct': stop_pct},
        'final_cap': final_cap,
        'finality': '风控裁决链为最终答案'
    }

if __name__ == '__main__':
    result = risk_report()
    print(f"\n{json.dumps(result, indent=2, ensure_ascii=False, default=str)}")
