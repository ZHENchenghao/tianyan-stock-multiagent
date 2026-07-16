# -*- coding: utf-8 -*-
"""
天眼 v8 路线A · ATR定仓回测 (极速版)
======================================
优化: 预加载全量K线到内存 → 向量化扫描 → 5分钟跑完10年
"""

import sys, os, time, warnings
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict

warnings.filterwarnings('ignore')
os.environ['TQDM_DISABLE'] = '1'

import duckdb, numpy as np, pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'
REPORTS_DIR = os.path.join(PROJECT_DIR, 'reports')

INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 15
MAX_STOCK_WEIGHT = 0.15
STOP_TYPE = 'hard8'   # hard8 | hard15 | atr3
STOP_LOSS = -0.08       # hard8用
STOP_HARD15 = -0.15     # hard15用
ATR_STOP_MULT = 3.0     # atr3用
MAX_HOLD_DAYS = 20
MIN_HOLD_DAYS = 5
REBALANCE_INTERVAL = 5   # 周频再平衡
BUY_FEE = 0.0001
SELL_FEE = 0.0011
SLIPPAGE = 0.001
LOT_SIZE = 100
MA20_TOP_N = 30
ETA = 0.30
IN_SAMPLE_END = '2022-12-31'
MIN_TRADE_AMOUNT = 500


def preload_klines():
    """一次性加载全量K线到内存 → {code: DataFrame}, 价格矩阵, 交易日历"""
    print('[0/4] 预加载全量K线到内存...')
    conn = duckdb.connect(DB_PATH, read_only=True)
    df = conn.execute("""
        SELECT ts_code, trade_date, close, vol, change_pct, is_st
        FROM kline_daily
        WHERE trade_date >= '2015-01-01'
          AND close > 0 AND vol > 0 AND is_st = false
          AND ts_code NOT LIKE 'sh000%' AND ts_code NOT LIKE 'sz399%' AND ts_code NOT LIKE 'bj%'
        ORDER BY ts_code, trade_date
    """).fetchdf()
    conn.close()

    df['trade_date'] = pd.to_datetime(df['trade_date'])

    # 价格矩阵: dates × codes
    price_matrix = df.pivot(index='trade_date', columns='ts_code', values='close')
    price_matrix = price_matrix.ffill()  # 前向填充停牌日

    # 成交量矩阵
    vol_matrix = df.pivot(index='trade_date', columns='ts_code', values='vol')
    vol_matrix = vol_matrix.fillna(0)

    # 交易日历
    all_dates = sorted(price_matrix.index.tolist())

    # 每只股票的DataFrame (供ATR等使用)
    code_dfs = {}
    for code, group in df.groupby('ts_code'):
        code_dfs[code] = group.set_index('trade_date').sort_index()

    n_stocks = price_matrix.shape[1]
    n_dates = len(all_dates)
    print(f'  加载完成: {n_stocks}只 × {n_dates}天 = {n_stocks * n_dates / 1e6:.1f}M 数据点')
    print(f'  日期: {all_dates[0].date()} ~ {all_dates[-1].date()}')

    return price_matrix, vol_matrix, code_dfs, all_dates


def scan_ma20_vectorized(price_matrix: pd.DataFrame, date_idx: int,
                         codes_active: List[str]) -> List[dict]:
    """
    向量化MA20扫描: 对价格矩阵切片直接做pandas rolling计算。
    比逐只查DuckDB快 100x+。
    """
    if date_idx < 21:
        return []

    # 取到当天的价格窗口 (最近25天)
    window = price_matrix.iloc[max(0, date_idx - 24):date_idx + 1]
    if window.shape[0] < 22:
        return []

    ma20 = window.rolling(20).mean()
    close_today = window.iloc[-1]
    close_yesterday = window.iloc[-2]
    ma20_today = ma20.iloc[-1]
    ma20_yesterday = ma20.iloc[-2]

    # MA20金叉: today close > ma20 AND yesterday close <= ma20
    cross_up = (close_today > ma20_today) & (close_yesterday <= ma20_yesterday)

    candidates = []
    for code in codes_active:
        if code not in cross_up.index:
            continue
        if not cross_up[code]:
            continue
        if pd.isna(close_today[code]) or pd.isna(ma20_today[code]):
            continue
        strength = float(close_today[code] / ma20_today[code])
        candidates.append({
            'code': code,
            'close': float(close_today[code]),
            'strength': round(strength, 4),
        })

    candidates.sort(key=lambda x: -x['strength'])
    return candidates


def get_price_slice(price_matrix: pd.DataFrame, codes: List[str],
                    date_idx: int, lookback: int = 252) -> pd.DataFrame:
    """从预加载矩阵切片 → ATR定仓用"""
    start = max(0, date_idx - lookback - 1)
    end = date_idx + 1
    valid = [c for c in codes if c in price_matrix.columns]
    return price_matrix.iloc[start:end][valid]


def execute_rebalance(cash: float, positions: dict, orders: List[dict],
                      prices: Dict[str, float], date_str: str
                      ) -> Tuple[float, dict, List[dict]]:
    """先卖后买, 含费率+滑点"""
    trade_log = []
    if not orders:
        return cash, positions, trade_log

    sorted_orders = sorted(orders, key=lambda x: 0 if x['action'] == 'SELL' else 1)

    for ord in sorted_orders:
        code = ord['code']
        action = ord['action']
        target_shares = ord['shares']
        price = prices.get(code, 0.0)
        if price <= 0 or target_shares <= 0:
            continue

        if action == 'SELL':
            if code not in positions:
                continue
            cur = positions[code]['shares']
            sell_shares = min(target_shares, cur)
            sell_shares = int(sell_shares / LOT_SIZE) * LOT_SIZE
            if sell_shares <= 0:
                continue
            exec_price = price * (1 - SLIPPAGE)
            proceeds = sell_shares * exec_price * (1 - SELL_FEE)
            cash += proceeds
            positions[code]['shares'] -= sell_shares
            if positions[code]['shares'] <= 0:
                del positions[code]
            trade_log.append({'date': date_str, 'code': code, 'action': 'SELL',
                'shares': sell_shares, 'price': round(exec_price, 2)})

        elif action == 'BUY':
            exec_price = price * (1 + SLIPPAGE)
            cost_per_share = exec_price * (1 + BUY_FEE)
            total_cost = target_shares * cost_per_share
            if total_cost > cash * 0.95:
                affordable = int(cash * 0.95 / cost_per_share / LOT_SIZE) * LOT_SIZE
                if affordable < LOT_SIZE:
                    continue
                target_shares = affordable
                total_cost = target_shares * cost_per_share
            if total_cost < MIN_TRADE_AMOUNT:
                continue
            cash -= total_cost
            if code in positions:
                old_s = positions[code]['shares']
                old_p = positions[code]['entry_price']
                total_s = old_s + target_shares
                positions[code]['shares'] = total_s
                positions[code]['entry_price'] = round(
                    (old_s * old_p + target_shares * exec_price) / total_s, 2)
                positions[code]['highest_close'] = max(
                    positions[code].get('highest_close', exec_price), exec_price)
            else:
                positions[code] = {
                    'shares': target_shares,
                    'entry_price': round(exec_price, 2),
                    'entry_date': date_str,
                    'highest_close': round(exec_price, 2),  # ATR追踪用
                }
            trade_log.append({'date': date_str, 'code': code, 'action': 'BUY',
                'shares': target_shares, 'price': round(exec_price, 2)})

    return cash, positions, trade_log


def calc_nav(cash, positions, prices):
    nav = cash
    for code, pos in positions.items():
        nav += pos['shares'] * prices.get(code, pos['entry_price'])
    return nav


def run():
    print('=' * 60)
    print('  天眼 v8 路线A · ATR定仓 极速回测 (全量日频)')
    print(f'  优化: 内存预加载 + 向量化MA20扫描')
    print('=' * 60)

    # ── 预加载 ──
    t0 = time.time()
    price_matrix, vol_matrix, code_dfs, all_dates = preload_klines()
    dates = [d for d in all_dates if d >= pd.Timestamp('2016-01-04')]
    n_days = len(dates)
    print(f'\n[1/4] 回测天数: {n_days} (已预加载, {time.time()-t0:.1f}秒)')

    # ── 初始化 ──
    cash = float(INITIAL_CAPITAL)
    positions = {}
    trade_log = []
    nav_series = []
    all_stocks = list(price_matrix.columns)

    from engine.atr_sizer import compute_atr_weights
    from engine.backtest_monitor import BacktestMonitor
    monitor = BacktestMonitor()

    start_time = time.time()
    last_pct = -1

    print('[2/4] 向量化逐日回测...')

    for di, trade_date in enumerate(dates):
        pct = (di + 1) * 100 // n_days
        if pct != last_pct:
            elapsed = time.time() - start_time
            rate = (di + 1) / elapsed * 60 if elapsed > 0 else 0
            eta = (n_days - di - 1) / rate if rate > 0 else 0
            nav_now = calc_nav(cash, positions, {})
            print(f'  [{di+1:>5}/{n_days} {pct:>3}%] '
                  f'NAV={nav_now:,.0f} | {rate:.0f}天/分 | ETA {eta:.0f}分', flush=True)
            last_pct = pct

        date_idx = all_dates.index(trade_date)

        # 当天快照 (从预加载矩阵取)
        today_prices = price_matrix.iloc[date_idx]
        today_vols = vol_matrix.iloc[date_idx]

        active_mask = (today_prices > 0) & (today_vols > 0)
        codes_active = today_prices[active_mask].index.tolist()
        price_dict = today_prices[active_mask].to_dict()

        if not codes_active:
            nav_series.append({'date': str(trade_date)[:10], 'nav': calc_nav(cash, positions, {})})
            continue

        # 硬出场 (每日执行, 支持 hard8/hard15/atr3)
        to_remove = []
        for code, pos in list(positions.items()):
            if code not in price_dict:
                continue
            cur_price = price_dict[code]
            pnl_pct = (cur_price - pos['entry_price']) / pos['entry_price']
            hold_days_elapsed = (dates[di] - pd.Timestamp(pos['entry_date'])).days

            # 止损判断
            exit_triggered = False
            exit_reason = ''

            if STOP_TYPE in ('hard8', 'hard15'):
                threshold = STOP_LOSS if STOP_TYPE == 'hard8' else STOP_HARD15
                if pnl_pct <= threshold:
                    exit_triggered = True
                    exit_reason = f'stop_{STOP_TYPE}'

            if not exit_triggered and STOP_TYPE == 'atr3':
                # ATR追踪止损: 止损价 = 入场后最高收盘价 - 3×ATR
                highest_since_entry = pos.get('highest_close', pos['entry_price'])
                if cur_price > highest_since_entry:
                    highest_since_entry = cur_price
                    positions[code]['highest_close'] = highest_since_entry

                # 计算ATR (从价格矩阵取最近20天)
                if code in price_matrix.columns:
                    col_idx = list(price_matrix.columns).index(code)
                    end_idx = date_idx + 1
                    start_idx = max(0, end_idx - 21)
                    close_slice = price_matrix.iloc[start_idx:end_idx, col_idx].dropna()
                    if len(close_slice) >= 15:
                        high_slice = close_slice.values
                        low_slice = close_slice.values
                        tr = np.maximum(
                            high_slice[1:] - low_slice[1:],
                            np.abs(high_slice[1:] - close_slice.values[:-1])
                        )
                        tr = np.maximum(tr, np.abs(low_slice[1:] - close_slice.values[:-1]))
                        atr_val = np.mean(tr[-14:]) if len(tr) >= 14 else np.std(close_slice) * 0.5
                    else:
                        atr_val = cur_price * 0.03  # 兜底: 3%
                else:
                    atr_val = cur_price * 0.03

                stop_price = highest_since_entry - ATR_STOP_MULT * atr_val
                if cur_price <= stop_price:
                    exit_triggered = True
                    exit_reason = f'stop_atr3'

            if not exit_triggered and hold_days_elapsed >= MAX_HOLD_DAYS:
                exit_triggered = True
                exit_reason = 'time_exit'

            if exit_triggered:
                exec_price = cur_price * (1 - SLIPPAGE)
                proceeds = pos['shares'] * exec_price * (1 - SELL_FEE)
                cash += proceeds
                trade_log.append({'date': str(trade_date)[:10], 'code': code, 'action': 'SELL',
                    'shares': pos['shares'], 'price': round(exec_price, 2), 'reason': exit_reason})
                to_remove.append(code)

        for code in to_remove:
            del positions[code]

        # ★ 周频再平衡: 只在 rebalance 日跑完整管线
        should_rebalance = (di % REBALANCE_INTERVAL == 0)
        orders = []
        rejected = []

        if should_rebalance:
            ma20_signals = scan_ma20_vectorized(price_matrix, date_idx, codes_active)
            survivor_pool = [s['code'] for s in ma20_signals[:MA20_TOP_N]]

            existing_codes = list(positions.keys())
            total_pool = list(dict.fromkeys(existing_codes + survivor_pool))

            target_weights = {}
            if len(total_pool) >= 2:
                price_slice = get_price_slice(price_matrix, total_pool, date_idx)
                if price_slice.shape[1] >= 2:
                    try:
                        valid = [c for c in total_pool if c in price_slice.columns]
                        target_weights = compute_atr_weights(valid, price_slice)
                    except Exception:
                        pass

            for c in total_pool:
                if c not in target_weights:
                    target_weights[c] = 0.0

            current_nav = calc_nav(cash, positions, price_dict)
            last_exec_w = {}
            for code, pos in positions.items():
                if code in price_dict and current_nav > 0:
                    last_exec_w[code] = (pos['shares'] * price_dict[code]) / current_nav

            executed_w = {}
            for code, tgt_w in target_weights.items():
                if code in last_exec_w and last_exec_w[code] > 0:
                    executed_w[code] = ETA * tgt_w + (1 - ETA) * last_exec_w[code]
                else:
                    executed_w[code] = tgt_w

            total_w = sum(executed_w.values())
            if total_w > 1e-10:
                executed_w = {k: v / total_w for k, v in executed_w.items()}

            for code, exe_w in executed_w.items():
                price = price_dict.get(code, 0)
                if price <= 0:
                    continue
                target_value = current_nav * exe_w
                target_shares = int(target_value / price / LOT_SIZE) * LOT_SIZE
                current_shares = positions.get(code, {}).get('shares', 0)
                diff = target_shares - current_shares
                if diff == 0:
                    continue
                # 最小持仓保护: 不满MIN_HOLD_DAYS不卖
                if diff < 0 and code in positions:
                    entry_dt = pd.Timestamp(positions[code]['entry_date'])
                    if (trade_date - entry_dt).days < MIN_HOLD_DAYS:
                        continue
                if diff > 0:
                    amount = diff * price
                    if amount < MIN_TRADE_AMOUNT:
                        rejected.append({'code': code, 'action': 'BUY', 'shares': diff, 'amount': amount, 'reason': 'BELOW_MIN'})
                        continue
                    orders.append({'code': code, 'action': 'BUY', 'shares': diff, 'price': price})
                else:
                    sell_shares = int(min(abs(diff), current_shares) / LOT_SIZE) * LOT_SIZE
                    if sell_shares <= 0:
                        continue
                    amount = sell_shares * price
                    if amount < MIN_TRADE_AMOUNT:
                        rejected.append({'code': code, 'action': 'SELL', 'shares': sell_shares, 'amount': amount, 'reason': 'BELOW_MIN'})
                        continue
                    orders.append({'code': code, 'action': 'SELL', 'shares': sell_shares, 'price': price})

            if orders:
                cash, positions, day_trades = execute_rebalance(
                    cash, positions, orders, price_dict, str(trade_date)[:10])
                trade_log.extend(day_trades)

        nav = calc_nav(cash, positions, price_dict)
        nav_series.append({'date': str(trade_date)[:10], 'nav': nav})
        monitor.record_nav(str(trade_date)[:10], nav)

        if rejected:
            monitor.record_trap2(str(trade_date)[:10], rejected_orders=rejected, executed_orders=orders)

    # ── 汇总 ──
    elapsed = time.time() - start_time
    print(f'\n[3/4] 回测完成 ({elapsed/60:.1f}分 | 速率 {n_days/elapsed*60:.0f}天/分)')

    navs = [r['nav'] for r in nav_series]
    navs_arr = np.array(navs)
    total_return = (navs_arr[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL
    peak = navs_arr[0]
    max_dd = 0.0
    dd_start = 0
    max_dd_dur = 0
    current_dd_dur = 0
    for i, v in enumerate(navs_arr):
        if v > peak:
            peak = v
            current_dd_dur = 0
        else:
            current_dd_dur += 1
            max_dd_dur = max(max_dd_dur, current_dd_dur)
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)

    n_years = n_days / 242
    ann_return = (1 + total_return) ** (1 / max(n_years, 0.5)) - 1
    daily_rets = np.diff(navs_arr) / navs_arr[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(242)) if np.std(daily_rets) > 0 else 0

    in_sample_end_idx = sum(1 for r in nav_series if r['date'] <= IN_SAMPLE_END)

    # NAV逐年
    nav_df = pd.DataFrame(nav_series)
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    nav_df['year'] = nav_df['date'].dt.year

    print(f'\n[4/4] 业绩报告')
    print(f'  总收益:   {total_return:+.2%}')
    print(f'  年化:     {ann_return:+.2%}')
    print(f'  最大回撤: {max_dd:.2%} (持续{max_dd_dur}天={max_dd_dur/21:.0f}月)')
    print(f'  夏普:     {sharpe:.2f}')
    print(f'  交易次数: {len(trade_log):,}')
    if in_sample_end_idx > 0 and in_sample_end_idx < len(navs):
        is_ret = (navs[in_sample_end_idx-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL
        oos_ret = (navs[-1] - navs[in_sample_end_idx-1]) / navs[in_sample_end_idx-1]
        print(f'  样本内:   {is_ret:+.2%}')
        print(f'  样本外:   {oos_ret:+.2%}')

    # 逐年
    print(f'\n  逐年收益:')
    for yr in range(2016, 2027):
        ydf = nav_df[nav_df['year'] == yr]
        if len(ydf) > 0:
            yr_ret = (ydf['nav'].iloc[-1] - ydf['nav'].iloc[0]) / ydf['nav'].iloc[0]
            yr_peak = ydf['nav'].max()
            yr_dd = (ydf['nav'].min() - yr_peak) / yr_peak
            print(f'    {yr}: {yr_ret:+.1%}  maxDD={yr_dd:.1%}')

    # 对比
    print(f'\n  {"="*50}')
    print(f'  终极对比')
    print(f'  {"指标":10s} {"HRP版":>12s} {"ATR月频":>12s} {"ATR全日频":>12s}')
    print(f'  {"收益":10s} {"-63.1%":>12s} {"+11.5%":>12s} {total_return:>+12.1%}')
    print(f'  {"回撤":10s} {"-75.1%":>12s} {"-46.9%":>12s} {max_dd:>12.1%}')
    print(f'  {"夏普":10s} {"-0.51":>12s} {"+0.68":>12s} {sharpe:>+12.2f}')
    print(f'  {"交易":10s} {"146,769":>12s} {"10,981":>12s} {len(trade_log):>12,}')
    print(f'  {"="*50}')

    mon_report = monitor.summary()
    os.makedirs(REPORTS_DIR, exist_ok=True)
    monitor.save(os.path.join(REPORTS_DIR, 'backtest_v8_atr_fast_monitor.json'))
    nav_df.to_csv(os.path.join(REPORTS_DIR, 'backtest_v8_atr_fast_nav.csv'), index=False)

    return {
        'nav_series': nav_series,
        'trades': trade_log,
        'performance': {
            'total_return': total_return,
            'ann_return': ann_return,
            'max_dd': max_dd,
            'sharpe': sharpe,
            'n_trades': len(trade_log),
        },
    }


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--stop', type=str, default='hard8',
                   choices=['hard8','hard15','atr3'])
    p.add_argument('--all', action='store_true', help='三版全跑')
    args = p.parse_args()

    if args.all:
        for st in ['hard8', 'hard15', 'atr3']:
            sep = '=' * 60
            print(f'\n{sep}')
            print(f'  止损模式: {st}')
            print(f'{sep}')
            globals()['STOP_TYPE'] = st
            run()
    else:
        globals()['STOP_TYPE'] = args.stop
        run()
