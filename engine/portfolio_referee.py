# -*- coding: utf-8 -*-
"""
天眼 v7 → v8 升级 · 组合统一裁判官 (Portfolio Referee)
=========================================================
中央拦截器: 缝合 [当前持仓] + [战法新信号] → HRP全资产联合优化 → 调仓调配单

核心纠错 (10年老兵修正):
  旧方案: 只把战法新信号喂给HRP → 老持仓被"吃掉", 银行板块过载75%
  新方案: 老持仓 ∪ 新信号 → 全资产在同一棵树上博弈 → 动态再平衡

工程点:
  1. EMA状态从 hrp_state.json 持久化 (跨重启存活)
  2. 碎股处理: 权重→目标金额→100股整手取整→BUY/SELL净订单
  3. 先卖后买: SELL优先执行释放现金, 再BUY

用法:
  from engine.portfolio_referee import PortfolioReferee
  ref = PortfolioReferee()
  orders = ref.run_interceptor(survivor_pool, paper_portfolio_path)
  # → [{'code':'sh600519','action':'SELL','shares':200,'price':1326.0}, ...]
"""

import sys, os, io, json, warnings
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

warnings.filterwarnings('ignore')
os.environ['TQDM_DISABLE'] = '1'

import duckdb
import numpy as np
import pandas as pd

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
# 确保项目根目录在 sys.path (解决 engine 模块导入)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'
HRP_STATE_FILE = os.path.join(PROJECT_DIR, 'hrp_state.json')

# ════════════════════════════════════════════════════
# 交易参数
# ════════════════════════════════════════════════════

BUY_FEE = 0.0001
SELL_FEE = 0.0011
SLIPPAGE = 0.001
MAX_STOCK_WEIGHT = 0.15
ETA_MOMENTUM = 0.30
MIN_TRADE_AMOUNT = 500      # 最低交易金额 (元)
LOT_SIZE = 100              # A股最小交易单位


# ════════════════════════════════════════════════════
# PortfolioReferee
# ════════════════════════════════════════════════════

class PortfolioReferee:
    """
    天眼 v7 中央拦截器 — 全资产联合优化 + 动态调仓。

    链路:
      [当前持仓] ∪ [战法新信号] → HRP → EMA平滑 → 碎股取整 → 调仓调配单
    """

    def __init__(self, db_path: str = DB_PATH,
                 max_stock_weight: float = MAX_STOCK_WEIGHT,
                 eta: float = ETA_MOMENTUM):
        self.db_path = db_path
        self.max_stock_weight = max_stock_weight
        self.eta = eta
        self._latest_date: Optional[str] = None

    @property
    def latest_date(self) -> str:
        if self._latest_date is None:
            try:
                conn = duckdb.connect(self.db_path, read_only=True)
                row = conn.execute(
                    "SELECT MAX(trade_date) FROM kline_daily"
                ).fetchone()
                conn.close()
                self._latest_date = str(row[0])[:10] if row and row[0] else date.today().isoformat()
            except Exception:
                self._latest_date = date.today().isoformat()
        return self._latest_date

    # ── 主入口 ──────────────────────────────────

    def run_interceptor(self, survivor_pool: List[str],
                        portfolio_json_path: str) -> List[dict]:
        """
        中央拦截器核心链路。

        Args:
          survivor_pool:        战法层今日存活信号 (List[code])
          portfolio_json_path:  paper_portfolio.json 路径

        Returns:
          trade_orders: [{'code','action','shares','price'}, ...]
        """
        # ── Step 1: 读取当前账户状态 ──
        pf = self._load_portfolio(portfolio_json_path)
        current_cash = pf.get('cash', 0.0)
        existing_positions = pf.get('positions', {})

        existing_codes = list(existing_positions.keys()) if existing_positions else []

        # ── Step 2: 构建全资产联合优化池 (去重并集) ──
        total_pool = list(dict.fromkeys(existing_codes + survivor_pool))

        if not total_pool:
            return []

        # ── Step 3: 批量拉取最新收盘价 + 计算NAV ──
        price_dict = self._fetch_latest_prices(total_pool)

        pos_value = 0.0
        for code, pos_data in existing_positions.items():
            cur_price = price_dict.get(code, pos_data.get('entry_price', 0.0))
            pos_value += pos_data.get('shares', 0) * cur_price

        current_nav = current_cash + pos_value

        if current_nav <= 0:
            return []

        # ── Step 4: 拉取历史价格矩阵 → HRP ──
        price_df = self._fetch_price_history(total_pool)

        if price_df is None or price_df.shape[1] < 2:
            # 候选池太小 → 等权 + 只做新买入
            return self._fallback_orders(total_pool, existing_positions,
                                         current_nav, price_dict)

        # ── Step 5: HRP 全资产联合优化 ──
        from engine.hrp_optimizer import (
            detect_zombies, check_crisis_mode,
            compute_penalized_covariance, run_hrp,
        )

        zombies, diagnostics = detect_zombies(total_pool)
        clean_codes = [c for c in total_pool if c not in zombies]

        if len(clean_codes) < 2:
            return self._fallback_orders(clean_codes, existing_positions,
                                         current_nav, price_dict)

        # 只保留 clean_codes 的价格列
        valid_cols = [c for c in clean_codes if c in price_df.columns]
        if len(valid_cols) < 2:
            return self._fallback_orders(valid_cols, existing_positions,
                                         current_nav, price_dict)

        clean_price_df = price_df[valid_cols]

        is_crisis, crisis_mode = check_crisis_mode(clean_price_df)
        penalized_cov = compute_penalized_covariance(clean_price_df, valid_cols)
        target_weights = run_hrp(clean_price_df, valid_cols, crisis_mode, penalized_cov)

        # 确保池子里每个标的都有权重
        for code in total_pool:
            if code not in target_weights:
                target_weights[code] = 0.0

        # ── Step 6: EMA 动量平滑 ──
        executed_weights = self._apply_momentum(
            target_weights, existing_positions, current_nav, price_dict
        )

        # ── Step 7: 权重 → 订单 (碎股处理) ──
        trade_orders = self._weights_to_orders(
            executed_weights, existing_positions, current_nav, price_dict
        )

        return trade_orders

    # ── 数据获取 ──────────────────────────────────

    def _load_portfolio(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            'cash': 0.0,
            'initial_capital': 0.0,
            'positions': {},
        }

    def _fetch_latest_prices(self, codes: List[str]) -> Dict[str, float]:
        """批量获取各股票的最新收盘价 (每个code取各自最新的trade_date)"""
        if not codes:
            return {}
        conn = duckdb.connect(self.db_path, read_only=True)
        price_dict = {}
        try:
            code_list = "', '".join(codes)
            # 每个 code 取各自最新的 close (用 ROW_NUMBER 窗口函数)
            df = conn.execute(f"""
                SELECT ts_code, close FROM (
                    SELECT ts_code, close, trade_date,
                        ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                    FROM kline_daily
                    WHERE ts_code IN ('{code_list}') AND close > 0
                ) sub WHERE rn = 1
            """).fetchdf()
            for _, row in df.iterrows():
                price_dict[row['ts_code']] = float(row['close'])
        finally:
            conn.close()
        return price_dict

    def _fetch_price_history(self, codes: List[str]) -> Optional[pd.DataFrame]:
        """拉取 252 日收盘价透视表"""
        if not codes:
            return None

        conn = duckdb.connect(self.db_path, read_only=True)
        try:
            code_list = "', '".join(codes)
            # 从最新日期往前推 400 自然日
            cutoff_dt = datetime.strptime(self.latest_date, '%Y-%m-%d') - timedelta(days=400)
            cutoff = cutoff_dt.strftime('%Y-%m-%d')

            df = conn.execute(f"""
                SELECT ts_code, trade_date, close
                FROM kline_daily
                WHERE ts_code IN ('{code_list}')
                  AND close > 0
                  AND trade_date >= '{cutoff}'
                ORDER BY ts_code, trade_date
            """).fetchdf()
        finally:
            conn.close()

        if df.empty:
            return None

        df['trade_date'] = pd.to_datetime(df['trade_date'])
        pivot = df.pivot(index='trade_date', columns='ts_code', values='close')
        pivot = pivot.ffill().bfill()
        pivot = pivot.tail(252)

        valid_cols = [c for c in codes if c in pivot.columns and pivot[c].notna().sum() >= 20]
        if len(valid_cols) < 2:
            return None

        return pivot[valid_cols]

    # ── EMA 动量平滑 ──────────────────────────────

    def _apply_momentum(self, target_weights: Dict[str, float],
                        current_positions: dict, current_nav: float,
                        price_dict: Dict[str, float]) -> Dict[str, float]:
        """
        防线3落地: 从当前真实持仓推算昨天实际权重 → EMA平滑。

        工程鲁棒性: 不依赖内存变量。即使服务器重启、代码中断，
        从 paper_portfolio.json 的 shares × price / NAV 倒推真实权重。
        """
        # 从当前真实持仓倒推"昨天实际权重"
        last_executed = {}
        for code, pos_data in current_positions.items():
            cur_price = price_dict.get(code, pos_data.get('entry_price', 0.0))
            if cur_price > 0 and current_nav > 0:
                last_executed[code] = (pos_data.get('shares', 0) * cur_price) / current_nav
            else:
                last_executed[code] = 0.0

        # 尝试从 hrp_state.json 读取 (跨日持久化, 更精确)
        hrp_state = self._load_hrp_state()
        if hrp_state and hrp_state.get('last_executed'):
            # 优先用持久化状态 (含EMA衰减记忆)
            for code, w in hrp_state['last_executed'].items():
                if code not in last_executed or last_executed[code] == 0.0:
                    last_executed[code] = w

        executed = {}
        for code, tgt_w in target_weights.items():
            if code in last_executed and last_executed[code] > 0:
                # 留存资产: EMA平滑平抑换手
                executed[code] = self.eta * tgt_w + (1 - self.eta) * last_executed[code]
            else:
                # 全新资产: 一步到位
                executed[code] = tgt_w

        # 归一化
        total = sum(executed.values())
        if total > 1e-10:
            executed = {k: v / total for k, v in executed.items()}

        # 持久化到 hrp_state.json
        self._save_hrp_state(executed)

        return executed

    def _load_hrp_state(self) -> dict:
        if os.path.exists(HRP_STATE_FILE):
            try:
                with open(HRP_STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_hrp_state(self, weights: Dict[str, float]):
        os.makedirs(os.path.dirname(HRP_STATE_FILE), exist_ok=True)
        with open(HRP_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'last_executed': weights,
                'updated_at': datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    # ── 碎股处理: 权重 → 订单 ──────────────────────

    def _weights_to_orders(self, executed_weights: Dict[str, float],
                           current_positions: dict, current_nav: float,
                           price_dict: Dict[str, float]) -> List[dict]:
        """
        工程点2落地: 权重 → 目标金额 → 100股整手向下取整 → BUY/SELL 净订单。

        逻辑:
          target_shares = floor(target_value / price / 100) × 100
          diff = target_shares - current_shares
          正差 → BUY, 负差 → SELL
        """
        orders = []

        for code, exe_w in executed_weights.items():
            price = price_dict.get(code)
            if price is None or price <= 0:
                continue

            target_value = current_nav * exe_w
            target_shares = int(target_value / price / LOT_SIZE) * LOT_SIZE

            current_shares = 0
            if code in current_positions:
                current_shares = current_positions[code].get('shares', 0)

            share_diff = target_shares - current_shares

            if share_diff == 0:
                continue

            if share_diff > 0:
                # 买入: 检查最低交易金额
                buy_amount = share_diff * price
                if buy_amount < MIN_TRADE_AMOUNT:
                    continue
                orders.append({
                    'code': code,
                    'action': 'BUY',
                    'shares': share_diff,
                    'price': price,
                })
            else:
                # 卖出: 不能卖超过持仓
                sell_shares = min(abs(share_diff), current_shares)
                sell_shares = int(sell_shares / LOT_SIZE) * LOT_SIZE
                if sell_shares <= 0:
                    continue
                sell_amount = sell_shares * price
                if sell_amount < MIN_TRADE_AMOUNT:
                    continue
                orders.append({
                    'code': code,
                    'action': 'SELL',
                    'shares': sell_shares,
                    'price': price,
                })

        # 先卖后买排序
        orders.sort(key=lambda x: 0 if x['action'] == 'SELL' else 1)
        return orders

    # ── 降级兜底 ──────────────────────────────────

    def _fallback_orders(self, codes: List[str],
                         existing_positions: dict,
                         current_nav: float,
                         price_dict: Dict[str, float]) -> List[dict]:
        """
        HRP 不可用时降级: 等权分配 + 只做买入 (不强制卖出)
        """
        if not codes:
            return []

        n = len(codes)
        w = 1.0 / n
        eq_weights = {c: w for c in codes}

        return self._weights_to_orders(eq_weights, existing_positions,
                                       current_nav, price_dict)


# ════════════════════════════════════════════════════
# CLI 自检
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    print('=' * 60)
    print('天眼 PortfolioReferee · 自检')
    print('=' * 60)

    ref = PortfolioReferee()
    print(f'最新数据日期: {ref.latest_date}')

    # 模拟场景: 已有3只持仓 + 战法新出2只
    import tempfile, os as _os

    # 创建临时 paper_portfolio.json
    tmpdir = tempfile.mkdtemp()
    tmp_pf = _os.path.join(tmpdir, 'paper_portfolio.json')

    mock_pf = {
        'cash': 3000.0,
        'initial_capital': 10000.0,
        'positions': {
            'sh600519': {'shares': 100, 'entry_price': 1300.0, 'entry_date': '2026-06-01', 'hold_days': 10},
            'sh600036': {'shares': 300, 'entry_price': 42.0, 'entry_date': '2026-06-05', 'hold_days': 6},
            'sh600900': {'shares': 500, 'entry_price': 28.0, 'entry_date': '2026-06-03', 'hold_days': 8},
        },
        'updated': '2026-06-12',
    }
    with open(tmp_pf, 'w', encoding='utf-8') as f:
        json.dump(mock_pf, f, ensure_ascii=False, indent=2)

    # 战法层今日新信号
    survivor_pool = ['sh600519', 'sh600036', 'sh600900',  # 已有
                     'sz000858', 'sz300750']               # 新增

    print(f'\n已有持仓: {list(mock_pf["positions"].keys())}')
    print(f'战法新信号: {survivor_pool}')
    print(f'联合池: {list(dict.fromkeys(list(mock_pf["positions"].keys()) + survivor_pool))}')

    orders = ref.run_interceptor(survivor_pool, tmp_pf)

    print(f'\n── 调仓调配单 ({len(orders)} 笔) ──')
    if orders:
        for o in orders:
            print(f'  {o["action"]:4s} {o["code"]:16s} {o["shares"]:>6}股 @ {o["price"]:>10.2f}')
    else:
        print('  无调仓指令 (可能所有标的触发截断/僵尸过滤/数据不足)')

    # 清理
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    if _os.path.exists(HRP_STATE_FILE):
        _os.remove(HRP_STATE_FILE)

    print('\n自检完成.')
