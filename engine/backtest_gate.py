# -*- coding: utf-8 -*-
"""天眼v8 · 市场门禁对比: MA200 vs 市场宽度 (独立版, 不污染全局)"""
import sys, os, time, warnings
warnings.filterwarnings('ignore')
os.environ['TQDM_DISABLE'] = '1'
import numpy as np, pandas as pd
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
sys.path.insert(0, PROJECT_DIR)

# 不从 backtest_v8_atr_fast import, 直接引用所需函数
import duckdb
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'

# 常量 (与 backtest_v8_atr_fast 保持一致)
STOP_LOSS = -0.08; MAX_HOLD_DAYS = 20; MIN_HOLD_DAYS = 5
REBALANCE_INTERVAL = 5; MA20_TOP_N = 30; ETA = 0.30
SLIPPAGE = 0.001; BUY_FEE = 0.0001; SELL_FEE = 0.0011
LOT_SIZE = 100; MIN_TRADE_AMOUNT = 500

# 直接导入纯函数 (不触发 backtest_v8_atr_fast 的全局初始化)
from engine.backtest_v8_atr_fast import (preload_klines, scan_ma20_vectorized,
    get_price_slice, execute_rebalance, calc_nav)
from engine.atr_sizer import compute_atr_weights

GATES = {
    'ma200': 'CSI300在MA200上方才开仓',
    'breadth': '>40%站上MA20才开仓',
    'none': '无门禁 (基准)',
}

for gate_name, gate_desc in GATES.items():
    t0 = time.time()
    print(f'\n{"="*60}\n  {gate_name}: {gate_desc}\n{"="*60}')

    price_matrix, vol_matrix, code_dfs, all_dates = preload_klines()
    dates = [d for d in all_dates if d >= pd.Timestamp('2016-01-04')]
    n_days = len(dates)
    all_stocks = list(price_matrix.columns)

    cash = 1_000_000.0; positions = {}; trade_log = []; nav_series = []

    # 预计算门禁
    gate_on = np.ones(n_days, dtype=bool)

    if gate_name == 'ma200':
        # 找CSI300列
        csi300 = None
        for c in all_stocks:
            if '000300' in c: csi300 = c; break
        if not csi300:
            for c in all_stocks:
                if '399300' in c or 'sh000300' in c: csi300 = c; break
        if csi300 and csi300 in price_matrix.columns:
            csi = price_matrix[csi300]
            ma200 = csi.rolling(200).mean()
            for di in range(n_days):
                d = dates[di]
                if d in ma200.index and d in csi.index:
                    if pd.notna(csi[d]) and pd.notna(ma200[d]):
                        if csi[d] < ma200[d]:
                            gate_on[di] = False
            closed_days = (~gate_on).sum()
            print(f'  CSI300={csi300}  门禁关闭: {closed_days}天 ({closed_days/n_days*100:.0f}%)')

    elif gate_name == 'breadth':
        for di in range(n_days):
            if di < 21: continue
            window = price_matrix.iloc[max(0, di-24):di+1]
            if window.shape[0] < 21: continue
            ma20 = window.rolling(20).mean().iloc[-1]
            close_t = window.iloc[-1]
            above = (close_t > ma20).sum()
            total = close_t.notna().sum()
            if total > 0 and above / total < 0.40:
                gate_on[di] = False
        closed_days = (~gate_on).sum()
        print(f'  门禁关闭: {closed_days}天 ({closed_days/n_days*100:.0f}%)')

    start_time = time.time(); last_pct = -1

    for di, trade_date in enumerate(dates):
        pct = (di+1)*100//n_days
        if pct != last_pct:
            elapsed = time.time()-start_time
            rate = (di+1)/elapsed*60 if elapsed>0 else 0
            eta = (n_days-di-1)/rate if rate>0 else 0
            nav_now = calc_nav(cash, positions, {})
            print(f'  [{di+1:>5}/{n_days} {pct:>3}%] NAV={nav_now:,.0f} | {rate:.0f}天/分 | ETA {eta:.0f}分', flush=True)
            last_pct = pct

        date_idx = all_dates.index(trade_date)
        today_prices = price_matrix.iloc[date_idx]
        today_vols = vol_matrix.iloc[date_idx]
        active_mask = (today_prices > 0) & (today_vols > 0)
        codes_active = today_prices[active_mask].index.tolist()
        price_dict = today_prices[active_mask].to_dict()
        if not codes_active:
            nav_series.append({'date': str(trade_date)[:10], 'nav': calc_nav(cash, positions, {})})
            continue

        # 硬出场
        to_remove = []
        for code, pos in list(positions.items()):
            if code not in price_dict: continue
            cur_price = price_dict[code]
            pnl_pct = (cur_price - pos['entry_price']) / pos['entry_price']
            hold_elapsed = (dates[di] - pd.Timestamp(pos['entry_date'])).days
            if pnl_pct <= STOP_LOSS:
                ep = cur_price * (1 - SLIPPAGE)
                cash += pos['shares'] * ep * (1 - SELL_FEE)
                trade_log.append({'date': str(trade_date)[:10], 'code': code, 'action': 'SELL',
                    'shares': pos['shares'], 'price': round(ep, 2), 'reason': 'stop_loss'})
                to_remove.append(code)
            elif hold_elapsed >= MAX_HOLD_DAYS:
                ep = cur_price * (1 - SLIPPAGE)
                cash += pos['shares'] * ep * (1 - SELL_FEE)
                trade_log.append({'date': str(trade_date)[:10], 'code': code, 'action': 'SELL',
                    'shares': pos['shares'], 'price': round(ep, 2), 'reason': 'time_exit'})
                to_remove.append(code)
        for code in to_remove: del positions[code]

        # 周频再平衡
        should_rebalance = (di % REBALANCE_INTERVAL == 0)
        orders = []; rejected = []

        if should_rebalance and gate_on[di]:
            ma20_signals = scan_ma20_vectorized(price_matrix, date_idx, codes_active)
            survivor_pool = [s['code'] for s in ma20_signals[:MA20_TOP_N]]
            existing_codes = list(positions.keys())
            total_pool = list(dict.fromkeys(existing_codes + survivor_pool))
            target_weights = {}
            if len(total_pool) >= 2:
                ps = get_price_slice(price_matrix, total_pool, date_idx)
                if ps.shape[1] >= 2:
                    try:
                        valid = [c for c in total_pool if c in ps.columns]
                        target_weights = compute_atr_weights(valid, ps)
                    except: pass
            for c in total_pool:
                if c not in target_weights: target_weights[c] = 0.0
            cn = calc_nav(cash, positions, price_dict)
            lew = {}
            for code, pos in positions.items():
                if code in price_dict and cn > 0:
                    lew[code] = (pos['shares'] * price_dict[code]) / cn
            ew = {}
            for code, tgt in target_weights.items():
                if code in lew and lew[code] > 0:
                    ew[code] = ETA * tgt + (1 - ETA) * lew[code]
                else: ew[code] = tgt
            tw = sum(ew.values())
            if tw > 1e-10: ew = {k: v/tw for k, v in ew.items()}
            for code, exe_w in ew.items():
                price = price_dict.get(code, 0)
                if price <= 0: continue
                tv = cn * exe_w
                ts = int(tv / price / LOT_SIZE) * LOT_SIZE
                cs = positions.get(code, {}).get('shares', 0)
                diff = ts - cs
                if diff == 0: continue
                if diff < 0 and code in positions:
                    if (trade_date - pd.Timestamp(positions[code]['entry_date'])).days < MIN_HOLD_DAYS: continue
                if diff > 0:
                    if diff * price < MIN_TRADE_AMOUNT:
                        rejected.append({'code': code, 'action': 'BUY', 'shares': diff, 'amount': diff*price, 'reason': 'BELOW_MIN'})
                        continue
                    orders.append({'code': code, 'action': 'BUY', 'shares': diff, 'price': price})
                else:
                    ss = int(min(abs(diff), cs) / LOT_SIZE) * LOT_SIZE
                    if ss <= 0 or ss * price < MIN_TRADE_AMOUNT:
                        rejected.append({'code': code, 'action': 'SELL', 'shares': ss, 'amount': ss*price, 'reason': 'BELOW_MIN'})
                        continue
                    orders.append({'code': code, 'action': 'SELL', 'shares': ss, 'price': price})
            if orders:
                cash, positions, dt = execute_rebalance(cash, positions, orders, price_dict, str(trade_date)[:10])
                trade_log.extend(dt)

        elif should_rebalance and not gate_on[di]:
            # 门禁关闭: 清仓
            for code, pos in list(positions.items()):
                if code in price_dict:
                    ep = price_dict[code] * (1 - SLIPPAGE)
                    proceeds = pos['shares'] * ep * (1 - SELL_FEE)
                    cash += proceeds
                    trade_log.append({'date': str(trade_date)[:10], 'code': code, 'action': 'SELL',
                        'shares': pos['shares'], 'price': round(ep, 2), 'reason': 'gate_close'})
            positions.clear()

        nav = calc_nav(cash, positions, price_dict)
        nav_series.append({'date': str(trade_date)[:10], 'nav': nav})

    # 汇总
    elapsed = time.time() - start_time
    navs = np.array([r['nav'] for r in nav_series])
    total_return = (navs[-1] - 1_000_000) / 1_000_000
    peak = navs[0]; max_dd = 0.0
    for v in navs: peak = max(peak, v); max_dd = max(max_dd, (peak - v) / peak)
    n_years = n_days / 242
    ann_return = (1 + total_return) ** (1 / max(n_years, 0.5)) - 1
    dr = np.diff(navs) / navs[:-1]
    sharpe = float(np.mean(dr) / np.std(dr) * np.sqrt(242)) if np.std(dr) > 0 else 0
    in_samp = sum(1 for r in nav_series if r['date'] <= '2022-12-31')
    is_ret = (navs[in_samp-1] - 1_000_000) / 1_000_000 if in_samp > 0 else 0
    oos_ret = (navs[-1] - navs[in_samp-1]) / navs[in_samp-1] if 0 < in_samp < len(navs) else 0
    trade_count = len(trade_log)

    # 逐年
    nav_df = pd.DataFrame(nav_series); nav_df['date'] = pd.to_datetime(nav_df['date']); nav_df['year'] = nav_df['date'].dt.year
    yr_str = ''
    for yr in range(2016, 2027):
        ydf = nav_df[nav_df['year'] == yr]
        if len(ydf) > 0:
            yr_ret = (ydf['nav'].iloc[-1] - ydf['nav'].iloc[0]) / ydf['nav'].iloc[0]
            yr_str += f'{yr}:{yr_ret:+.1%} '

    print(f'\n  {gate_name}: 收益{total_return:+.1%} 年化{ann_return:+.1%} 回撤{max_dd:.1%} 夏普{sharpe:+.2f} 交易{trade_count:,}')
    print(f'  样本内{is_ret:+.1%} 样本外{oos_ret:+.1%}')
    print(f'  {yr_str}')

print(f'\n{"="*60}')
print(f'  三方对比')
print(f'  无门禁:  -41.4%  夏普-0.18')
print(f'{"="*60}')
