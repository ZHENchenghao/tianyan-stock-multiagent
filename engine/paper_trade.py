# -*- coding: utf-8 -*-
"""
天眼 · 纸交引擎 (Paper Trade Engine)
======================================
每天收盘后跑一次: 刷新数据 → v9门禁 → MA20扫描 → 输出次日交易清单
零外部API依赖, 完全基于DuckDB + 天眼回测底座。

用法:
  python engine/paper_trade.py           # 生成明日交易指令
  python engine/paper_trade.py --history # 查看纸交历史P&L
"""
import sys, os, json, warnings, time
from datetime import datetime, date
from typing import Dict, List, Optional
from collections import defaultdict

warnings.filterwarnings('ignore')
os.environ['TQDM_DISABLE'] = '1'

import duckdb
import pandas as pd
import numpy as np

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
# 确保项目根目录在sys.path (解决engine模块导入问题)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'
PAPER_PORTFOLIO = os.path.join(PROJECT_DIR, 'paper_portfolio.json')
PAPER_LOG = os.path.join(PROJECT_DIR, 'reports', 'paper_trade_log.csv')

# ── 交易参数 ──
INITIAL_CAPITAL = 10_000      # 纸交本金1万，和实盘对齐
MAX_POSITIONS = 5             # 最多持有5只
MAX_POSITION_PCT = 0.20       # 单只上限20% (小资金必须集中)
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20
BUY_FEE = 0.0001
SELL_FEE = 0.0011
SLIPPAGE = 0.001


# ══════════════════════════════════════════════════════
# 1. 数据刷新
# ══════════════════════════════════════════════════════

def refresh_data():
    """调用天眼日常数据刷新"""
    import subprocess
    tianyan = os.path.join(PROJECT_DIR, 'tianyan.py')
    print('[1/5] 刷新日线数据...')
    result = subprocess.run(
        [sys.executable, tianyan, 'daily'],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f'  ⚠ 刷新可能部分失败, 继续...')
    else:
        print(f'  ✓ 数据刷新完成')


# ══════════════════════════════════════════════════════
# 2. v9门禁判断
# ══════════════════════════════════════════════════════

def check_v9_gate():
    """查询今天是否处于v9防御状态"""
    # 确保路径正确
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(engine_dir)
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)

    from engine.unified_verdict import layer1_macro_regime

    print('[2/5] v9门禁判断...')
    regime = layer1_macro_regime()
    regime_name = regime.get('regime', 'UNKNOWN')
    wti = regime.get('wti', 0)
    us10y = regime.get('us10y', 0)

    if regime_name.startswith('DEFENSE'):
        in_defense = True
        print(f'  🔴 防御中: {regime_name} (WTI=${wti:.0f}, US10Y={us10y:.2f}%)')
        print(f'  → 禁止新开仓, 只处理已有持仓的卖出')
    else:
        in_defense = False
        print(f'  🟢 正常: {regime_name} (WTI=${wti:.0f}, US10Y={us10y:.2f}%)')

    return in_defense, regime


# ══════════════════════════════════════════════════════
# 3. MA20全市场扫描
# ══════════════════════════════════════════════════════

def scan_ma20_signals(in_defense: bool):
    """扫描全市场MA20突破信号"""
    print('[3/5] MA20全市场扫描...')

    conn = duckdb.connect(DB_PATH, read_only=True)

    # 获取股票池
    codes = conn.execute("""
        SELECT DISTINCT ts_code FROM kline_daily
        WHERE ts_code NOT LIKE 'sh000%' AND ts_code NOT LIKE 'sz399%'
          AND ts_code NOT LIKE 'sh688%' AND ts_code NOT LIKE 'sz300%'
          AND ts_code NOT LIKE 'sz301%'
        GROUP BY ts_code HAVING COUNT(*) >= 250
    """).fetchall()

    candidates = []
    for (code,) in codes:
        # 过滤688/300
        numeric = code.replace('sh','').replace('sz','').replace('.SH','').replace('.SZ','')
        if numeric.startswith('688') or numeric.startswith('300') or numeric.startswith('301'):
            continue

        # 取最近25日数据
        df = conn.execute(f"""
            SELECT trade_date, close, amount, open
            FROM kline_daily WHERE ts_code='{code}' AND close>0
            ORDER BY trade_date DESC LIMIT 25
        """).fetchdf()

        if len(df) < 22:
            continue

        df = df.sort_values('trade_date').reset_index(drop=True)
        close = df['close'].values.astype(float)
        ma20 = pd.Series(close).rolling(20).mean().values

        # MA20上穿: 昨天收盘站上MA20, 前天还在MA20下方
        if len(close) >= 2 and len(ma20) >= 2:
            today_close = close[-1]
            today_ma20 = ma20[-1]
            yesterday_close = close[-2]
            yesterday_ma20 = ma20[-2]

            if (not np.isnan(today_ma20) and not np.isnan(yesterday_ma20) and
                today_close > today_ma20 and yesterday_close <= yesterday_ma20):
                strength = today_close / today_ma20
                # 成交量
                amount = float(df['amount'].iloc[-1]) if 'amount' in df.columns else 0
                candidates.append({
                    'code': code,
                    'name': code,
                    'close': today_close,
                    'ma20': today_ma20,
                    'strength': strength,
                    'amount': amount,
                })

    conn.close()

    # 按strength排序
    candidates.sort(key=lambda x: x['strength'], reverse=True)
    print(f'  MA20突破: {len(candidates)} 只股票')

    if in_defense:
        print(f'  → 门禁锁定, 候选仅展示, 不执行买入')

    return candidates


# ══════════════════════════════════════════════════════
# 4. 虚拟持仓管理
# ══════════════════════════════════════════════════════

def load_portfolio() -> dict:
    """加载纸交虚拟账户"""
    if os.path.exists(PAPER_PORTFOLIO):
        with open(PAPER_PORTFOLIO, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'cash': INITIAL_CAPITAL,
        'initial_capital': INITIAL_CAPITAL,
        'positions': {},
        'trade_history': [],
        'created': date.today().isoformat(),
        'updated': date.today().isoformat(),
    }


def save_portfolio(pf: dict):
    pf['updated'] = date.today().isoformat()
    with open(PAPER_PORTFOLIO, 'w', encoding='utf-8') as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)


def process_exits(pf: dict) -> list:
    """处理持仓卖出信号"""
    print('[4/5] 检查持仓出场...')
    conn = duckdb.connect(DB_PATH, read_only=True)
    sells = []

    for code, pos in list(pf['positions'].items()):
        # 取最近数据
        df = conn.execute(f"""
            SELECT trade_date, close, low, open
            FROM kline_daily WHERE ts_code='{code}' AND close>0
            ORDER BY trade_date DESC LIMIT 25
        """).fetchdf()

        if len(df) < 2:
            continue

        df = df.sort_values('trade_date').reset_index(drop=True)
        today_close = float(df['close'].iloc[-1])
        today_low = float(df['low'].iloc[-1])
        today_open = float(df['open'].iloc[-1])
        latest_date = str(df['trade_date'].iloc[-1])[:10]

        close_arr = df['close'].values.astype(float)
        ma20_arr = pd.Series(close_arr).rolling(20).mean().values

        exit_signal = None
        exit_price = 0.0

        # 止损
        stop_price = pos['entry_price'] * (1 + STOP_LOSS)
        if today_low <= stop_price:
            exit_price = min(today_open, stop_price) * (1 - SLIPPAGE)
            exit_signal = 'stop_loss'

        # MA20下穿
        elif (len(close_arr) >= 2 and len(ma20_arr) >= 2 and
              close_arr[-1] < ma20_arr[-1] and close_arr[-2] >= ma20_arr[-2]):
            exit_price = today_close * (1 - SLIPPAGE)
            exit_signal = 'ma20_sell'

        # 到期
        elif pos['hold_days'] <= 1:
            exit_price = today_close * (1 - SLIPPAGE)
            exit_signal = 'time_exit'

        # 更新持仓天数
        pf['positions'][code]['hold_days'] = pos['hold_days'] - 1

        if exit_signal:
            shares = pos['shares']
            proceeds = shares * exit_price * (1 - SELL_FEE)
            cost_basis = shares * pos['entry_price'] * (1 + BUY_FEE)
            pnl = proceeds - cost_basis
            pnl_pct = pnl / cost_basis if cost_basis > 0 else 0

            sells.append({
                'code': code,
                'exit_price': exit_price,
                'reason': exit_signal,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'date': latest_date,
            })

            pf['cash'] += proceeds
            del pf['positions'][code]

            print(f'  卖出 {code}: @{exit_price:.2f} {exit_signal} P&L={pnl:+,.0f} ({pnl_pct:+.1%})')

    conn.close()

    if not sells:
        print(f'  无卖出信号, 当前持仓 {len(pf["positions"])} 只')
    return sells


def process_entries(pf: dict, candidates: list, in_defense: bool):
    """[DEPRECATED v7→v8] 生成买入指令 — 固定20%仓位, 已被 execute_rebalance_orders + PortfolioReferee 替代"""
    print('[5/5] 生成买入清单...')

    if in_defense:
        print(f'  🔴 v9防御中 — 今日禁止新开仓')
        print(f'\n  {"─"*60}')
        print(f'  明日操作: 无 (防御锁仓)')
        print(f'  MA20突破候选 (仅供参考):')
        for c in candidates[:10]:
            print(f'    {c["code"]:<12} 收盘{c["close"]:.2f} strength={c["strength"]:.3f}')
        return []

    slots = MAX_POSITIONS - len(pf['positions'])
    if slots <= 0:
        print(f'  仓位已满 ({len(pf["positions"])}/{MAX_POSITIONS})')
        return []

    # 计算当前NAV
    conn = duckdb.connect(DB_PATH, read_only=True)
    nav = pf['cash']
    for code, pos in pf['positions'].items():
        row = conn.execute(f"""
            SELECT close FROM kline_daily WHERE ts_code='{code}' AND close>0
            ORDER BY trade_date DESC LIMIT 1
        """).fetchone()
        if row:
            nav += pos['shares'] * row[0] * (1 - SELL_FEE)
    conn.close()

    orders = []
    for cand in candidates[:slots * 2]:  # 多取一些候选
        if len(orders) >= slots:
            break

        max_pos_val = nav * MAX_POSITION_PCT
        buy_amount = min(pf['cash'] * 0.95, max_pos_val)

        entry_price = cand['close'] * (1 + SLIPPAGE)
        cost_per_share = entry_price * (1 + BUY_FEE)
        shares = int(buy_amount / cost_per_share / 100) * 100

        if shares < 100 or buy_amount < 500:
            continue

        total_cost = shares * cost_per_share
        if total_cost > pf['cash']:
            shares = int(pf['cash'] * 0.95 / cost_per_share / 100) * 100
            if shares < 100:
                continue
            total_cost = shares * cost_per_share

        orders.append({
            'code': cand['code'],
            'name': cand['name'],
            'action': 'BUY',
            'shares': shares,
            'entry_price': round(entry_price, 2),
            'total_cost': round(total_cost, 2),
            'strength': round(cand['strength'], 3),
        })

        # 预扣现金
        pf['cash'] -= total_cost
        pf['positions'][cand['code']] = {
            'shares': shares,
            'entry_price': entry_price,
            'entry_date': date.today().isoformat(),
            'hold_days': MAX_HOLD_DAYS,
        }

    if orders:
        print(f'  生成 {len(orders)} 笔买入指令')
    else:
        print(f'  无符合条件的买入候选')

    return orders


# ══════════════════════════════════════════════════════
# 4b. [v8新增] 统一调仓执行引擎
# ══════════════════════════════════════════════════════

def execute_rebalance_orders(pf: dict, orders: list, date_str: str = None):
    """
    天眼 v8 统一调仓执行引擎: 替代原 process_entries().
    接管 BUY 与 SELL 双向指令, 动态更新账户 Cash 和 Positions.

    工程铁律:
      - 先卖后买: SELL 优先执行 → 释放现金 → BUY 使用释放后的现金
      - 现金不足时拒绝买入 (不部分成交, 避免碎片化)
      - 卖出时检查持仓是否足额
      - 零股持仓自动清理
    """
    if date_str is None:
        date_str = date.today().isoformat()

    if not orders:
        return pf

    # 先卖后买
    sorted_orders = sorted(orders, key=lambda x: 0 if x['action'] == 'SELL' else 1)

    for ord in sorted_orders:
        code = ord['code']
        action = ord['action']
        shares = ord['shares']
        price = ord['price']

        if shares <= 0:
            continue

        if action == 'BUY':
            slippage = price * SLIPPAGE
            execution_price = price + slippage
            total_cost = shares * execution_price * (1 + BUY_FEE)

            if pf['cash'] >= total_cost:
                pf['cash'] -= total_cost
                if code in pf['positions']:
                    # 加仓: 更新平均成本
                    old_shares = pf['positions'][code]['shares']
                    old_cost = pf['positions'][code]['entry_price']
                    total_shares = old_shares + shares
                    pf['positions'][code]['shares'] = total_shares
                    pf['positions'][code]['entry_price'] = round(
                        (old_shares * old_cost + shares * execution_price) / total_shares, 2
                    )
                    pf['positions'][code]['entry_date'] = date_str
                else:
                    pf['positions'][code] = {
                        'shares': shares,
                        'entry_price': round(execution_price, 2),
                        'entry_date': date_str,
                        'hold_days': MAX_HOLD_DAYS,
                    }
                print(f'  [BUY]  {code:16s} +{shares:>6}股 @{execution_price:>10.2f} 成本CNY{total_cost:>10,.0f}')
            else:
                print(f'  [风控] {code:16s} 现金不足 缺CNY{total_cost - pf["cash"]:,.0f} → 跳过')

        elif action == 'SELL':
            if code not in pf['positions']:
                print(f'  [异常] {code:16s} 无持仓可卖 → 跳过')
                continue

            current_shares = pf['positions'][code]['shares']
            if current_shares < shares:
                print(f'  [异常] {code:16s} 持仓{current_shares}股 < 计划{shares}股 → 只卖{current_shares}股')
                shares = current_shares

            slippage = price * SLIPPAGE
            execution_price = price - slippage
            proceeds = shares * execution_price * (1 - SELL_FEE)

            pf['positions'][code]['shares'] -= shares
            pf['cash'] += proceeds

            pnl_pct = (execution_price - pf['positions'][code]['entry_price']) / pf['positions'][code]['entry_price']

            print(f'  [SELL] {code:16s} -{shares:>6}股 @{execution_price:>10.2f} 回笼CNY{proceeds:>10,.0f} ({pnl_pct:+.1%})')

            # 清零清理
            if pf['positions'][code]['shares'] == 0:
                del pf['positions'][code]

    return pf


# ══════════════════════════════════════════════════════
# 5. 输出报告
# ══════════════════════════════════════════════════════

def print_report(orders: list, sells: list, pf: dict, in_defense: bool, candidates: list):
    """打印纸交日报"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = duckdb.connect(DB_PATH, read_only=True)
    latest_date = conn.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
    conn.close()

    # 计算NAV
    conn = duckdb.connect(DB_PATH, read_only=True)
    nav = pf['cash']
    for code, pos in pf['positions'].items():
        row = conn.execute(f"""
            SELECT close FROM kline_daily WHERE ts_code='{code}' AND close>0
            ORDER BY trade_date DESC LIMIT 1
        """).fetchone()
        if row:
            nav += pos['shares'] * row[0] * (1 - SELL_FEE)
    conn.close()

    total_return = (nav - pf['initial_capital']) / pf['initial_capital']

    print(f'\n{"="*60}')
    print(f'  天眼纸交日报')
    print(f'{"="*60}')
    print(f'  生成时间: {now}')
    print(f'  最新K线: {latest_date}')
    print(f'  v9状态:  {"🔴 防御锁仓" if in_defense else "🟢 正常交易"}')
    print(f'  账户NAV: CNY{nav:,.0f} ({total_return:+.2%})')
    print(f'  现金:    CNY{pf["cash"]:,.0f}')
    print(f'  持仓:    {len(pf["positions"])}/{MAX_POSITIONS} 只')

    if pf['positions']:
        print(f'\n  当前持仓:')
        print(f'  {"代码":<12} {"股数":>6} {"入场价":>10} {"现价":>10} {"盈亏":>10} {"持日":>4}')
        conn = duckdb.connect(DB_PATH, read_only=True)
        for code, pos in pf['positions'].items():
            row = conn.execute(f"""
                SELECT close FROM kline_daily WHERE ts_code='{code}' AND close>0
                ORDER BY trade_date DESC LIMIT 1
            """).fetchone()
            cur_price = row[0] if row else 0
            pnl = (cur_price - pos['entry_price']) / pos['entry_price']
            print(f'  {code:<12} {pos["shares"]:>6} {pos["entry_price"]:>10.2f} '
                  f'{cur_price:>10.2f} {pnl:>+9.1%} {pos["hold_days"]:>4}')
        conn.close()

    if sells:
        print(f'\n  今日卖出:')
        for s in sells:
            print(f'  {s["code"]} @{s["exit_price"]:.2f} [{s["reason"]}] P&L={s["pnl"]:+,.0f} ({s["pnl_pct"]:+.1%})')

    if orders:
        print(f'\n  ╔{"═"*56}╗')
        print(f'  ║  📋 明日早盘买入清单 (T+1开盘价+千二滑点){" "*(24-len("明日早盘买入清单"))} ║')
        print(f'  ╠{"═"*56}╣')
        print(f'  ║ {"代码":<12} {"股数":>6} {"预估成本":>12} {"强度":>8} {"占比":>8} ║')
        for o in orders:
            pct = o['total_cost'] / nav * 100
            print(f'  ║ {o["code"]:<12} {o["shares"]:>6} CNY{o["total_cost"]:>10,.0f} {o["strength"]:>8.3f} {pct:>7.1f}% ║')
        print(f'  ╚{"═"*56}╝')
    elif not in_defense:
        print(f'\n  明日无买入指令')

    if in_defense and candidates:
        print(f'\n  ⚠ 门禁锁仓中, 以下MA20突破候选暂不买入:')
        for c in candidates[:5]:
            print(f'    {c["code"]:<12} strength={c["strength"]:.3f} close={c["close"]:.2f}')


# ══════════════════════════════════════════════════════
# 5b. [v8新增] 调仓报告
# ══════════════════════════════════════════════════════

def print_report_v8(orders: list, sells: list, pf: dict, in_defense: bool, candidates: list):
    """v8 调仓日报 — 订单格式: {code, action, shares, price}"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = duckdb.connect(DB_PATH, read_only=True)
    latest_date = conn.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
    conn.close()

    # 计算NAV
    conn = duckdb.connect(DB_PATH, read_only=True)
    nav = pf['cash']
    for code, pos in pf['positions'].items():
        row = conn.execute(f"""
            SELECT close FROM kline_daily WHERE ts_code='{code}' AND close>0
            ORDER BY trade_date DESC LIMIT 1
        """).fetchone()
        if row:
            nav += pos['shares'] * row[0] * (1 - SELL_FEE)
    conn.close()

    total_return = (nav - pf['initial_capital']) / pf['initial_capital'] if pf['initial_capital'] > 0 else 0.0

    print(f'\n{"="*60}')
    print(f'  天眼纸交日报 v8 (HRP动态仓位)')
    print(f'{"="*60}')
    print(f'  生成时间: {now}')
    print(f'  最新K线: {latest_date}')
    print(f'  v9状态:  {"🔴 防御锁仓" if in_defense else "🟢 正常交易"}')
    print(f'  账户NAV: CNY{nav:,.0f} ({total_return:+.2%})')
    print(f'  现金:    CNY{pf["cash"]:,.0f}')
    print(f'  持仓:    {len(pf["positions"])} 只')

    if pf['positions']:
        print(f'\n  当前持仓:')
        print(f'  {"代码":<16} {"股数":>6} {"入场价":>10} {"现价":>10} {"盈亏":>10} {"占比":>8}')
        conn = duckdb.connect(DB_PATH, read_only=True)
        for code, pos in pf['positions'].items():
            row = conn.execute(f"""
                SELECT close FROM kline_daily WHERE ts_code='{code}' AND close>0
                ORDER BY trade_date DESC LIMIT 1
            """).fetchone()
            cur_price = row[0] if row else 0
            pnl = (cur_price - pos['entry_price']) / pos['entry_price'] if pos['entry_price'] > 0 else 0
            weight = (pos['shares'] * cur_price) / nav * 100 if nav > 0 else 0
            print(f'  {code:<16} {pos["shares"]:>6} {pos["entry_price"]:>10.2f} '
                  f'{cur_price:>10.2f} {pnl:>+9.1%} {weight:>7.1f}%')
        conn.close()

    if sells:
        print(f'\n  今日硬性出场 (止损/MA20/到期):')
        for s in sells:
            print(f'  {s["code"]} @{s["exit_price"]:.2f} [{s["reason"]}] P&L={s["pnl"]:+,.0f} ({s["pnl_pct"]:+.1%})')

    if orders:
        buys = [o for o in orders if o['action'] == 'BUY']
        sells_adj = [o for o in orders if o['action'] == 'SELL']
        print(f'\n  今日主动调仓 ({len(orders)} 笔: {len(buys)}买 {len(sells_adj)}卖):')
        for o in orders:
            direction = '▲' if o['action'] == 'BUY' else '▼'
            print(f'  {direction} {o["action"]:4s} {o["code"]:16s} {o["shares"]:>6}股 @{o["price"]:>10.2f}')
    elif not in_defense:
        print(f'\n  今日无需调仓 (HRP目标与当前持仓一致)')

    if in_defense and candidates:
        print(f'\n  ⚠ 门禁锁仓中, MA20突破候选暂不买入:')
        for c in candidates[:5]:
            print(f'    {c["code"]:<12} strength={c["strength"]:.3f} close={c["close"]:.2f}')


# ══════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Paper Trade Engine')
    parser.add_argument('--skip-refresh', action='store_true', help='跳过数据刷新(已手动刷新过)')
    parser.add_argument('--history', action='store_true', help='查看纸交历史')
    parser.add_argument('--legacy', action='store_true', help='回退旧版固定20%仓位模式')
    args = parser.parse_args()

    if args.history:
        show_history()
        return

    # ── v8模式切换 ──
    use_v8 = not getattr(args, 'legacy', False)  # 默认v8, --legacy回退旧版

    print('='*60)
    if use_v8:
        print('  天眼纸交引擎 v8 · HRP动态仓位 + 全资产联合优化')
    else:
        print('  天眼纸交引擎 · Paper Trade Engine (旧版固定20%)')
    print(f'  本金: CNY{INITIAL_CAPITAL:,} | 持仓上限: {MAX_POSITIONS}只 | 单只上限: {MAX_POSITION_PCT:.0%}')
    print('='*60)

    # Step 1: 刷新数据
    if not args.skip_refresh:
        refresh_data()
    else:
        print('[1/5] 跳过数据刷新 (--skip-refresh)')

    # Step 2: v9门禁
    in_defense, regime = check_v9_gate()

    # Step 3: MA20扫描
    candidates = scan_ma20_signals(in_defense)

    # Step 4: 持仓管理 (硬性出场: 止损/MA20下穿/到期)
    pf = load_portfolio()
    sells = process_exits(pf)

    # Step 5: 买入指令 — v8链路 vs 旧版链路
    if use_v8:
        print('[5/5] PortfolioReferee 全资产联合优化...')

        # 先落盘当前账户状态 (确保Referee从磁盘读到最新)
        save_portfolio(pf)

        # 提取候选池代码列表
        survivor_pool = [c['code'] for c in candidates[:MAX_POSITIONS * 3]]

        if survivor_pool or pf.get('positions'):
            from engine.portfolio_referee import PortfolioReferee
            ref = PortfolioReferee(max_stock_weight=0.15, eta=0.30)
            rebalance_orders = ref.run_interceptor(survivor_pool, PAPER_PORTFOLIO)

            # 防御模式下过滤BUY: 只执行SELL(调降), 禁止新开仓
            if in_defense:
                filtered = [o for o in rebalance_orders if o['action'] == 'SELL']
                blocked = len(rebalance_orders) - len(filtered)
                if blocked > 0:
                    print(f'  🔴 v9防御: 拦截 {blocked} 笔BUY指令')
                rebalance_orders = filtered

            if rebalance_orders:
                n_buy = sum(1 for o in rebalance_orders if o['action'] == 'BUY')
                n_sell = sum(1 for o in rebalance_orders if o['action'] == 'SELL')
                print(f'  中央裁判官输出 {len(rebalance_orders)} 笔调仓指令 ({n_buy}买 {n_sell}卖)')
                today_str = date.today().isoformat()
                pf = execute_rebalance_orders(pf, rebalance_orders, date_str=today_str)
                orders = rebalance_orders
            else:
                print(f'  中央裁判官: 无需调仓')
                orders = []
        else:
            print(f'  无持仓无信号, 跳过')
            orders = []

        # 保存 (v8链路)
        save_portfolio(pf)
        # v8 报告
        print_report_v8(orders, sells, pf, in_defense, candidates)
    else:
        orders = process_entries(pf, candidates, in_defense)
        # 保存
        save_portfolio(pf)
        # 报告
        print_report(orders, sells, pf, in_defense, candidates)


def show_history():
    """查看纸交历史"""
    if not os.path.exists(PAPER_PORTFOLIO):
        print('暂无纸交记录')
        return

    pf = load_portfolio()
    print(f'\n  纸交账户')
    print(f'  创建日期: {pf.get("created", "?")}')
    print(f'  最后更新: {pf.get("updated", "?")}')
    print(f'  初始本金: CNY{pf["initial_capital"]:,.0f}')
    print(f'  当前现金: CNY{pf["cash"]:,.0f}')
    print(f'  当前持仓: {len(pf.get("positions", {}))} 只')


if __name__ == '__main__':
    main()
