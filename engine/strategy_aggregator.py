# -*- coding: utf-8 -*-
"""
天眼战法系统 · 动态裁决聚合器 v1.0
============================================
功能: 加载全量回测筛选出的存活战法, 以历史全量回测得分为权重,
      对实时/指定日期的信号进行加权投票聚合, 输出 trend_score 和 trend_direction。

核心设计:
  1. 权重 = 存活战法的夏普比率归一化值 (保底0.05, 避免零权重)
  2. 信号聚合 = Σ(w_i × s_i) / Σ(w_i), s_i ∈ {0(空仓), 1(做多), -1(做空)}
  3. 结果映射到 0-100 区间: 50为中性, >55偏多, <45偏空

防未来函数:
  - 实时模式下, 信号用最新收盘价计算 (代表当日判断)
  - 回测模式下 (as_of_date 指定), 只使用该日期及之前的数据

用法:
  from engine.strategy_aggregator import StrategyAggregator
  agg = StrategyAggregator(DB_PATH)
  agg.load_survivors()                 # 加载存活战法
  result = agg.aggregate('sh000300')   # 实时聚合
  result = agg.aggregate('sh000300', '2026-06-10')  # 历史日期聚合
"""

import sys, os, io, json, warnings, math
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

warnings.filterwarnings('ignore')
import duckdb
import pandas as pd
import numpy as np

# ── 路径 & DB ──────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'
REPORTS_DIR = os.path.join(PROJECT_DIR, 'reports')
SURVIVORS_FILE = os.path.join(REPORTS_DIR, 'strategy_survivors.json')

# ── 聚合常量 ───────────────────────────────────────────
MIN_WEIGHT       = 0.05    # 最小权重保底, 避免零权重规则被完全忽略
NEUTRAL_SCORE    = 50.0    # 中性基准分
BULLISH_THRESHOLD = 55.0   # 偏多阈值
BEARISH_THRESHOLD = 45.0   # 偏空阈值


def _build_rules() -> List[dict]:
    """
    构建25条规则定义 (与 strategy_backtest.build_rules() 完全一致)。

    每条规则含: rule_id, name, category, master, signal(lambda), direction。
    signal lambda 接受含技术指标的 DataFrame, 返回 bool Series。
    """
    rules = []

    # ── 均线类 ──
    rules.append({
        'rule_id': 'R08', 'name': '利弗莫尔: 关键点突破(突破MA60)',
        'category': '趋势', 'master': '利弗莫尔',
        'signal': lambda d: (d['close'] > d['ma60']) & (d['close'].shift(1) <= d['ma60'].shift(1)),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R10', 'name': '利弗莫尔: 跌破MA60清仓',
        'category': '趋势', 'master': '利弗莫尔',
        'signal': lambda d: (d['close'] < d['ma60']) & (d['close'].shift(1) >= d['ma60'].shift(1)),
        'direction': -1
    })
    rules.append({
        'rule_id': 'R35', 'name': 'PTJ: 站上MA200做多',
        'category': '趋势', 'master': 'PTJ',
        'signal': lambda d: (d['close'] > d['ma200']) & (d['close'].shift(1) <= d['ma200'].shift(1)),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R36', 'name': 'PTJ: 跌破MA200做空/清仓',
        'category': '趋势', 'master': 'PTJ',
        'signal': lambda d: (d['close'] < d['ma200']) & (d['close'].shift(1) >= d['ma200'].shift(1)),
        'direction': -1
    })
    rules.append({
        'rule_id': 'R53', 'name': 'Darvas: 箱体突破(新高+放量)',
        'category': '趋势', 'master': 'Darvas',
        'signal': lambda d: (d['close'] > d['high'].rolling(20).max().shift(1)) &
                            (d['vol'] > d['vol'].rolling(20).mean() * 1.5),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R56', 'name': 'Darvas: 跌破箱底(20日低点)',
        'category': '趋势', 'master': 'Darvas',
        'signal': lambda d: (d['close'] < d['low'].rolling(20).min().shift(1)),
        'direction': -1
    })

    # ── MACD类 ──
    rules.append({
        'rule_id': 'R14', 'name': '赵老哥: MACD金叉买入',
        'category': '技术', 'master': '赵老哥',
        'signal': lambda d: (d['macd_dif'] > d['macd_dea']) &
                            (d['macd_dif'].shift(1) <= d['macd_dea'].shift(1)),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R19', 'name': '赵老哥: MACD死叉卖出',
        'category': '技术', 'master': '赵老哥',
        'signal': lambda d: (d['macd_dif'] < d['macd_dea']) &
                            (d['macd_dif'].shift(1) >= d['macd_dea'].shift(1)),
        'direction': -1
    })

    # ── KDJ类 ──
    rules.append({
        'rule_id': 'R21', 'name': '小鳄鱼: KDJ-J<0超卖抄底',
        'category': '反转', 'master': '小鳄鱼',
        'signal': lambda d: (d['kdj_j'] < 0),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R24', 'name': '小鳄鱼: KDJ-J>100超买卖出',
        'category': '反转', 'master': '小鳄鱼',
        'signal': lambda d: (d['kdj_j'] > 100),
        'direction': -1
    })

    # ── RSI类 ──
    rules.append({
        'rule_id': 'R25', 'name': '小鳄鱼: RSI<30超卖买入',
        'category': '反转', 'master': '小鳄鱼',
        'signal': lambda d: (d['rsi14'] < 30),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R26', 'name': '小鳄鱼: RSI>70超买卖出',
        'category': '反转', 'master': '小鳄鱼',
        'signal': lambda d: (d['rsi14'] > 70),
        'direction': -1
    })

    # ── BOLL类 ──
    rules.append({
        'rule_id': 'R78', 'name': 'Wyckoff: 触及BOLL下轨反弹',
        'category': '反转', 'master': 'Wyckoff',
        'signal': lambda d: (d['close'] <= d['boll_lo'] * 1.02),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R80', 'name': 'Wyckoff: 突破BOLL上轨强势',
        'category': '趋势', 'master': 'Wyckoff',
        'signal': lambda d: (d['close'] > d['boll_up']) &
                            (d['vol'] > d['vol'].rolling(20).mean()),
        'direction': 1
    })

    # ── 均线排列类 ──
    rules.append({
        'rule_id': 'R30', 'name': '炒股养家: MA5>MA10>MA20多头排列买入',
        'category': '趋势', 'master': '炒股养家',
        'signal': lambda d: (d['ma5'] > d['ma10']) & (d['ma10'] > d['ma20']) &
                            ((d['ma5'].shift(1) <= d['ma10'].shift(1)) |
                             (d['ma10'].shift(1) <= d['ma20'].shift(1))),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R33', 'name': '炒股养家: MA5<MA10<MA20空头排列卖出',
        'category': '趋势', 'master': '炒股养家',
        'signal': lambda d: (d['ma5'] < d['ma10']) & (d['ma10'] < d['ma20']) &
                            ((d['ma5'].shift(1) >= d['ma10'].shift(1)) |
                             (d['ma10'].shift(1) >= d['ma20'].shift(1))),
        'direction': -1
    })

    # ── 量价类 ──
    rules.append({
        'rule_id': 'R40', 'name': 'PTJ: 缩量回调到MA20买入',
        'category': '趋势', 'master': 'PTJ',
        'signal': lambda d: (d['close'] > d['ma20'] * 0.98) &
                            (d['close'] < d['ma20'] * 1.02) &
                            (d['vol'] < d['vol'].rolling(20).mean() * 0.7),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R83', 'name': '逻辑哥: 放量突破20日高点',
        'category': '趋势', 'master': '逻辑哥',
        'signal': lambda d: (d['close'] > d['high'].rolling(20).max().shift(1)) &
                            (d['vol'] > d['vol'].rolling(20).mean() * 2),
        'direction': 1
    })

    # ── 均线回踩类 ──
    rules.append({
        'rule_id': 'R41', 'name': 'Minervini: 回踩MA10反弹买入',
        'category': '趋势', 'master': 'Minervini',
        'signal': lambda d: (d['close'] > d['ma10']) & (d['low'] < d['ma10'] * 1.01) &
                            (d['close'] > d['open']),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R47', 'name': 'Minervini: 跌破MA20趋势结束卖出',
        'category': '趋势', 'master': 'Minervini',
        'signal': lambda d: (d['close'] < d['ma20']) & (d['close'].shift(1) >= d['ma20'].shift(1)),
        'direction': -1
    })

    # ── 突破回踩类 ──
    rules.append({
        'rule_id': 'R48', 'name': 'Druckenmiller: 突破后回踩MA5加仓',
        'category': '趋势', 'master': 'Druckenmiller',
        'signal': lambda d: (d['close'] > d['ma5']) & (d['low'] < d['ma5'] * 1.015) &
                            (d['close'].shift(5) < d['ma20'].shift(5)) & (d['close'] > d['ma20']),
        'direction': 1
    })

    # ── 连续涨跌类 ──
    rules.append({
        'rule_id': 'R58', 'name': 'Loeb: 连跌3日+缩量→反弹买入',
        'category': '反转', 'master': 'Loeb',
        'signal': lambda d: (d['close'] < d['close'].shift(1)) &
                            (d['close'].shift(1) < d['close'].shift(2)) &
                            (d['close'].shift(2) < d['close'].shift(3)) &
                            (d['vol'] < d['vol'].rolling(20).mean() * 0.5),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R60', 'name': 'Loeb: 连涨3日+放量→止盈卖出',
        'category': '反转', 'master': 'Loeb',
        'signal': lambda d: (d['close'] > d['close'].shift(1)) &
                            (d['close'].shift(1) > d['close'].shift(2)) &
                            (d['close'].shift(2) > d['close'].shift(3)) &
                            (d['vol'] > d['vol'].rolling(20).mean() * 1.5),
        'direction': -1
    })

    # ── 综合评分类 ──
    rules.append({
        'rule_id': 'R65', 'name': '北京炒家: 技术评分>70+放量买入',
        'category': '综合', 'master': '北京炒家',
        'signal': lambda d: (d['tech_score'] > 70) & (d['vol'] > d['vol'].rolling(20).mean()),
        'direction': 1
    })
    rules.append({
        'rule_id': 'R68', 'name': '北京炒家: 技术评分<30卖出',
        'category': '综合', 'master': '北京炒家',
        'signal': lambda d: (d['tech_score'] < 30),
        'direction': -1
    })
    return rules


# ══════════════════════════════════════════════════════════
# 技术指标计算 (与 strategy_backtest.calc_all_indicators 一致)
# ══════════════════════════════════════════════════════════

def _calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """预计算所有技术指标"""
    d = df.copy()
    close = d['close'].values.astype(float)
    high  = d['high'].values.astype(float)
    low   = d['low'].values.astype(float)
    vol   = d['vol'].values.astype(float)

    d['ma5']   = pd.Series(close).rolling(5).mean()
    d['ma10']  = pd.Series(close).rolling(10).mean()
    d['ma20']  = pd.Series(close).rolling(20).mean()
    d['ma60']  = pd.Series(close).rolling(60).mean()
    d['ma200'] = pd.Series(close).rolling(200).mean()

    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean()
    d['macd_dif'] = ema12 - ema26
    d['macd_dea'] = d['macd_dif'].ewm(span=9, adjust=False).mean()

    low9  = pd.Series(low).rolling(9).min()
    high9 = pd.Series(high).rolling(9).max()
    rsv = (pd.Series(close) - low9) / (high9 - low9).replace(0, np.nan) * 100
    d['kdj_k'] = rsv.ewm(alpha=1/3, adjust=False).mean()
    d['kdj_d'] = d['kdj_k'].ewm(alpha=1/3, adjust=False).mean()
    d['kdj_j'] = 3 * d['kdj_k'] - 2 * d['kdj_d']

    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    d['rsi14'] = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, np.nan)))

    boll_mid = pd.Series(close).rolling(20).mean()
    boll_std = pd.Series(close).rolling(20).std()
    d['boll_up'] = boll_mid + 2 * boll_std
    d['boll_lo'] = boll_mid - 2 * boll_std

    # 综合技术评分
    score = pd.Series(50.0, index=d.index)
    ma_bull  = (d['ma5'] > d['ma10']) & (d['ma10'] > d['ma20'])
    ma_bear  = (d['ma5'] < d['ma10']) & (d['ma10'] < d['ma20'])
    macd_bull = d['macd_dif'] > d['macd_dea']
    rsi_ok    = (d['rsi14'] >= 30) & (d['rsi14'] <= 70)
    rsi_oversold = d['rsi14'] < 30
    kdj_oversold = d['kdj_j'] < 0
    vol_low   = vol < pd.Series(vol).rolling(20).mean() * 0.8
    above_ma5  = close > d['ma5'].values
    above_ma20 = close > d['ma20'].values

    score = score + ma_bull.astype(float) * 15 - ma_bear.astype(float) * 15
    score = score + above_ma5.astype(float) * 5 + above_ma20.astype(float) * 5
    score = score + macd_bull.astype(float) * 10 + rsi_ok.astype(float) * 5
    score = score + rsi_oversold.astype(float) * 5 + kdj_oversold.astype(float) * 5
    score = score + vol_low.astype(float) * 5
    d['tech_score'] = score.clip(0, 100)

    return d


# ══════════════════════════════════════════════════════════
# 核心: 动态裁决聚合器
# ══════════════════════════════════════════════════════════

class StrategyAggregator:
    """
    动态裁决聚合器。

    工作流:
      1. load_survivors() — 从JSON加载存活战法及其回测得分
      2. aggregate() — 对指定标的/日期, 运行存活战法信号并加权聚合
      3. 输出 trend_score (0-100) 和 trend_direction

    权重方案 (v1.0):
      - 主权重 = 夏普比率归一化 (最大夏普→1.0, 最小→min_weight)
      - 最终信号 = Σ(w_i × direction_i × signal_active_i) / Σ(w_i)
      - 无信号(所有战法都空仓) → score=50, direction=neutral

    降级策略:
      - 回测结果文件不存在 → 使用等权全25条规则 (不淘汰)
      - 存活战法<3条 → 自动降级到全25条等权
    """

    def __init__(self, db_path: str = DB_PATH,
                 survivors_file: str = SURVIVORS_FILE):
        self.db_path = db_path
        self.survivors_file = survivors_file
        self.all_rules = _build_rules()
        self._rule_map = {r['rule_id']: r for r in self.all_rules}

        # 存活战法信息
        self.survivors: Dict[str, dict] = {}       # rule_id → {sharpe, weight, ...}
        self.normalized_weights: Dict[str, float] = {}  # rule_id → 归一化权重
        self.is_loaded = False
        self.degraded = False  # 是否已降级到等权模式

        # 动态风控 (v3.0 软性指数降权, 废除一票否决)
        self.penalty_factors: Dict[str, float] = {}    # rule_id → 惩罚因子 (1.0=无惩罚, <1.0=降权)
        self.penalty_details: Dict[str, dict] = {}     # rule_id → {max_loss, bsf, es, status}
        self.bs_audit_loaded = False

    # ── 加载存活战法 ──────────────────────────────────

    def load_survivors(self, backtest_results: dict = None) -> bool:
        """
        加载存活战法名单及其回测得分。

        优先级:
          1. backtest_results: StrategyBacktester.run_all() 的返回值 (内存传递)
          2. survivors_file:   磁盘JSON文件 (跨进程)

        归一化夏普比率为权重: w_i = max(min_weight, sharpe_i / max_sharpe)

        返回: True=加载成功, False=降级到等权模式
        """
        survivor_data = None

        # ── 来源1: 内存传递 ──
        if backtest_results:
            survivors_list = backtest_results.get('survivors', [])
            composite_scores = backtest_results.get('composite_scores', {})
            rule_metrics = backtest_results.get('rule_metrics', {})
            survivor_data = {
                'survivors': {}
            }
            for rid in survivors_list:
                m = rule_metrics.get(rid, {})
                survivor_data['survivors'][rid] = {
                    'rule_id': rid,
                    'rule_name': m.get('rule_name', rid),
                    'sharpe_ratio': m.get('sharpe_ratio', 0),
                    'win_rate': m.get('win_rate', 0),
                    'total_return': m.get('total_return', 0),
                    'profit_factor': m.get('profit_factor', 0),
                    'calmar_ratio': m.get('calmar_ratio', 0),
                    'composite_score': composite_scores.get(rid, 0),
                    'n_trades': m.get('n_trades', 0),
                }

        # ── 来源2: JSON文件 ──
        if survivor_data is None:
            if os.path.exists(self.survivors_file):
                try:
                    with open(self.survivors_file, 'r', encoding='utf-8') as f:
                        survivor_data = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass

        # ── 降级: 无数据则等权全25条 ──
        if survivor_data is None or not survivor_data.get('survivors'):
            print("[!] StrategyAggregator: no backtest results, fallback to equal weight all 25 rules")
            self._degrade_to_equal_weight()
            return False

        # ── 解析存活战法 ──
        self.survivors = survivor_data['survivors']
        n_survivors = len(self.survivors)

        if n_survivors < 3:
            print(f"[!] Only {n_survivors} survivors (<3), fallback to equal weight")
            self._degrade_to_equal_weight()
            return False

        # ── 归一化夏普为权重 ──
        sharpe_values = [s.get('sharpe_ratio', 0) for s in self.survivors.values()]
        max_sharpe = max(sharpe_values) if sharpe_values else 1.0
        min_sharpe = min(sharpe_values) if sharpe_values else 0.0
        sharpe_range = max_sharpe - min_sharpe

        for rid, s in self.survivors.items():
            sr = s.get('sharpe_ratio', 0)
            if sharpe_range > 0:
                raw_weight = (sr - min_sharpe) / sharpe_range
            else:
                raw_weight = 1.0
            # 保底MIN_WEIGHT, 确保没有规则被完全忽略
            self.normalized_weights[rid] = max(MIN_WEIGHT, raw_weight)

        self.is_loaded = True
        self.degraded = False
        return True

    # ── 软性风险降权 (v3.0 指数衰减) ──────────────────────

    def apply_soft_penalty(self, audit_path: str = None) -> int:
        """加载BS审计, 用指数衰减降权。max_loss>30%时penalty=exp(-2*excess)"""
        import math as _m
        if audit_path is None:
            audit_path = os.path.join(PROJECT_DIR, 'reports', 'gt', 'black_swan_audit.json')
        if not os.path.exists(audit_path):
            for rid in self.survivors:
                self.penalty_factors[rid] = 1.0
            self.bs_audit_loaded = True
            return 0
        try:
            with open(audit_path, 'r', encoding='utf-8') as f:
                audit = json.load(f)
        except:
            return 0
        rule_risk = audit.get('rule_risk', {})
        self.penalty_factors.clear()
        self.penalty_details.clear()
        n_pen = 0
        for rid in self.survivors:
            risk = rule_risk.get(rid, {})
            max_loss = abs(risk.get('max_single_loss', 0))
            if max_loss >= 0.30:
                penalty = _m.exp(-2.0 * (max_loss - 0.30))
                status = 'WARNING'
                n_pen += 1
            else:
                penalty = 1.0
                status = 'ACTIVE'
            self.penalty_factors[rid] = round(penalty, 4)
            self.penalty_details[rid] = {
                'max_loss': round(max_loss, 4), 'penalty': round(penalty, 4),
                'status': status, 'risk_level': risk.get('risk_level', '?'),
                'bsf': risk.get('bsf', 0), 'expected_shortfall': risk.get('expected_shortfall', 0),
            }
            if rid in self.normalized_weights:
                self.normalized_weights[rid] = max(MIN_WEIGHT, self.normalized_weights[rid] * penalty)
        self.bs_audit_loaded = True
        if n_pen > 0:
            print(f"  [SOFT] {n_pen} rules penalized (exp decay, never zero)")
        return n_pen

    # backward compat
    load_black_swan_audit = apply_soft_penalty

    def _degrade_to_equal_weight(self):
        """降级模式: 所有25条规则等权"""
        self.survivors = {}
        for r in self.all_rules:
            rid = r['rule_id']
            self.survivors[rid] = {
                'rule_id': rid,
                'rule_name': r['name'],
                'sharpe_ratio': 0.0,
                'composite_score': 0.0,
            }
            self.normalized_weights[rid] = 1.0  # 等权
        self.is_loaded = True
        self.degraded = True

    # ── 数据获取 ──────────────────────────────────────

    def _get_kline_with_indicators(self, ts_code: str,
                                    as_of_date: str = None) -> Optional[pd.DataFrame]:
        """
        获取标的K线数据并计算技术指标。

        参数:
          ts_code:    标的代码 (如 'sh000300')
          as_of_date: 截止日期 (None = 最新)

        ⚠️ 回测模式下 (as_of_date 指定), 严格只取该日期及之前的数据,
           确保信号计算不偷看未来。
        """
        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            if as_of_date:
                df = conn.execute(f"""
                    SELECT trade_date, open, high, low, close, vol
                    FROM kline_daily
                    WHERE ts_code = '{ts_code}'
                      AND trade_date <= '{as_of_date}'
                    ORDER BY trade_date
                """).fetchdf()
            else:
                df = conn.execute(f"""
                    SELECT trade_date, open, high, low, close, vol
                    FROM kline_daily
                    WHERE ts_code = '{ts_code}'
                    ORDER BY trade_date
                """).fetchdf()
            conn.close()

            if len(df) < 250:
                return None

            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df = df.reset_index(drop=True)
            df = _calc_indicators(df)
            return df
        except Exception as e:
            return None

    def _get_single_signal(self, df: pd.DataFrame, rule: dict,
                           as_of_date: str = None) -> int:
        """
        获取单条规则在最新日期的信号值。

        返回:
           1:  做多信号
          -1:  做空/卖出信号
           0:  无信号 (空仓)

        ⚠️ 防未来函数: 信号在最新日期上计算, 不使用shift后的值。
           因为聚合器输出的是"当前判断", 不是交易执行信号。
           交易执行时的shift由 backtester 处理。
        """
        try:
            raw_signal = rule['signal'](df).fillna(False)
            if as_of_date:
                # 回测模式: 取指定日期当天的信号
                target_date = pd.to_datetime(as_of_date)
                mask = df['trade_date'] <= target_date
                if mask.any():
                    # 取最后一天的信号 (即 as_of_date 当天的判断)
                    last_idx = df.index[mask][-1]
                    if raw_signal.iloc[last_idx]:
                        return rule['direction']
            else:
                # 实时模式: 取最新日期的信号
                if len(raw_signal) > 0 and raw_signal.iloc[-1]:
                    return rule['direction']
        except Exception:
            pass
        return 0

    # ── 核心聚合 ──────────────────────────────────────

    def aggregate(self, idx_code: str, as_of_date: str = None) -> Dict[str, Any]:
        """
        核心聚合方法: 获取存活战法的当日信号, 以回测得分为权重聚合。

        参数:
          idx_code:   指数代码 (如 'sh000300', 'sh000688', 'sz399006')
          as_of_date: 历史日期 (YYYY-MM-DD), None=最新交易日

        返回:
          {
            'trend_score':     float,  # 0-100, 50=中性, >55偏多, <45偏空
            'trend_direction': str,    # 'bullish' | 'bearish' | 'neutral'
            'weighted_signal': float,  # 加权原始信号 (-1 ~ 1)
            'n_active':        int,    # 发出信号的战法数量
            'n_survivors':     int,    # 存活战法总数
            'vote_details':    list,   # 每条战法的投票明细
            'degraded':        bool,   # 是否降级模式
            'as_of_date':      str,    # 实际使用日期
          }
        """
        if not self.is_loaded:
            self.load_survivors()

        # ── 获取数据 ──
        df = self._get_kline_with_indicators(idx_code, as_of_date)
        if df is None:
            return self._empty_result(idx_code, as_of_date, 'data_unavailable')

        # 确认实际使用的日期
        actual_date = str(df['trade_date'].iloc[-1])[:10]

        # ── 遍历存活战法, 收集信号 ──
        votes = []       # (rule_id, direction, weight, signal_active)
        total_weight = 0.0
        weighted_sum  = 0.0
        n_active = 0

        for rid in self.survivors:
            rule = self._rule_map.get(rid)
            if rule is None:
                continue

            # ── v3.0软性降权: 用惩罚因子打折, 永不清零 ──
            penalty = self.penalty_factors.get(rid, 1.0)
            weight = self.normalized_weights.get(rid, MIN_WEIGHT)
            is_penalized = penalty < 1.0

            if weight <= 0:
                signal = 0
            else:
                signal = self._get_single_signal(df, rule, as_of_date)

            if signal != 0:
                n_active += 1
                weighted_sum += weight * signal
            total_weight += weight

            votes.append({
                'rule_id': rid,
                'rule_name': rule['name'][:40],
                'direction': rule['direction'],
                'signal': signal,
                'weight': round(weight, 4),
                'penalized': is_penalized,
            })

        # ── 计算加权信号 ──
        if total_weight > 0:
            raw_weighted = weighted_sum / total_weight  # 范围: [-1, 1]
        else:
            raw_weighted = 0.0

        # ── 映射到 0-100 分 ──
        # raw_weighted ∈ [-1, 1] → score ∈ [0, 100]
        # 转换公式: score = 50 + raw_weighted * 50
        trend_score = round(NEUTRAL_SCORE + raw_weighted * 50.0, 1)
        trend_score = max(0.0, min(100.0, trend_score))

        # ── 方向判断 ──
        if trend_score >= BULLISH_THRESHOLD:
            trend_direction = 'bullish'
        elif trend_score <= BEARISH_THRESHOLD:
            trend_direction = 'bearish'
        else:
            trend_direction = 'neutral'

        # ── 构建信号描述 ──
        vote_summary_parts = []
        for v in votes:
            if v.get('penalized'):
                marker = '[~]'  # soft penalty
            elif v['signal'] == 1:
                marker = '[+]'
            elif v['signal'] == -1:
                marker = '[-]'
            else:
                marker = '[ ]'
            vote_summary_parts.append(f"{v['rule_id']}{marker}")
        vote_summary = ' '.join(vote_summary_parts)

        penalized_in_survivors = [rid for rid in self.penalty_factors
                                   if rid in self.survivors and self.penalty_factors[rid] < 1.0]
        active_survivors = len(self.survivors) - len(penalized_in_survivors)
        n_broken_in_survivors = len(penalized_in_survivors)
        return {
            'trend_score':      trend_score,
            'trend_direction':  trend_direction,
            'weighted_signal':  round(raw_weighted, 4),
            'n_active':         n_active,
            'n_survivors':      max(0, active_survivors),
            'n_total_loaded':   len(self.survivors),
            'n_penalized': n_broken_in_survivors,
            'vote_details':     votes,
            'degraded':         self.degraded,
            'as_of_date':       actual_date,
            'vote_summary':     vote_summary,
            'idx_code':         idx_code,
        }

    def _empty_result(self, idx_code: str, as_of_date: str,
                      reason: str = 'unknown') -> Dict[str, Any]:
        """返回空结果 (数据不可用时的降级输出)"""
        return {
            'trend_score':      50.0,
            'trend_direction':  'neutral',
            'weighted_signal':  0.0,
            'n_active':         0,
            'n_survivors':      len(self.survivors) if self.survivors else 0,
            'vote_details':     [],
            'degraded':         True,
            'as_of_date':       as_of_date or 'unknown',
            'vote_summary':     '',
            'idx_code':         idx_code,
            'error':            reason,
        }

    # ── 辅助: 获取单个战法的权重 ─────────────────────

    def get_weights_summary(self) -> Dict[str, float]:
        """返回各存活战法的归一化权重"""
        return dict(self.normalized_weights)

    def get_top_rules(self, n: int = 5) -> List[Tuple[str, float, str]]:
        """返回权重最高的前N条规则 (rule_id, weight, rule_name)"""
        sorted_rules = sorted(self.normalized_weights.items(),
                              key=lambda x: x[1], reverse=True)
        return [(rid, w, self._rule_map[rid]['name'][:40])
                for rid, w in sorted_rules[:n] if rid in self._rule_map]


# ══════════════════════════════════════════════════════════
# 便捷函数 — 供 unified_verdict.py 直接调用
# ══════════════════════════════════════════════════════════

# 全局单例 (惰性初始化)
_aggregator = None

def get_aggregator(db_path: str = DB_PATH,
                   survivors_file: str = SURVIVORS_FILE,
                   force_reload: bool = False) -> StrategyAggregator:
    """
    获取全局 StrategyAggregator 单例。

    首次调用时自动加载存活战法。
    force_reload=True 时强制重新加载。
    """
    global _aggregator
    if _aggregator is None or force_reload:
        _aggregator = StrategyAggregator(db_path, survivors_file)
        _aggregator.load_survivors()
    return _aggregator


def quick_aggregate(idx_code: str, as_of_date: str = None) -> Dict[str, Any]:
    """
    一行聚合: 自动初始化 + 聚合。

    用法:
      from engine.strategy_aggregator import quick_aggregate
      result = quick_aggregate('sh000300')
      print(result['trend_score'], result['trend_direction'])
    """
    agg = get_aggregator()
    return agg.aggregate(idx_code, as_of_date)


# ══════════════════════════════════════════════════════════
# 自测 & 演示
# ══════════════════════════════════════════════════════════

def main():
    """命令行入口: python engine/strategy_aggregator.py [idx_code] [--date YYYY-MM-DD]"""
    import argparse
    parser = argparse.ArgumentParser(description='天眼战法动态聚合器')
    parser.add_argument('idx_code', nargs='?', default='sh000300',
                        help='指数代码 (默认 sh000300)')
    parser.add_argument('--date', '-d', default=None,
                        help='指定日期 YYYY-MM-DD (默认最新)')
    parser.add_argument('--reload', action='store_true',
                        help='强制重新加载存活战法')
    parser.add_argument('--top', type=int, default=5,
                        help='显示前N条规则权重')
    args = parser.parse_args()

    agg = get_aggregator(force_reload=args.reload)

    # 显示权重
    print(f"\n{'='*60}")
    print(f"  天眼战法动态聚合器")
    print(f"  模式: {'降级-等权' if agg.degraded else '回测加权'} | "
          f"存活战法: {len(agg.survivors)}条")
    print(f"{'='*60}")

    if not agg.degraded:
        print(f"\n  Top {args.top} 战法 (按权重):")
        for rid, w, name in agg.get_top_rules(args.top):
            print(f"    {rid}: {name} — 权重={w:.4f}")

    # 运行聚合
    result = agg.aggregate(args.idx_code, args.date)
    print(f"\n{'='*60}")
    print(f"  聚合结果: {args.idx_code} @ {result['as_of_date']}")
    print(f"{'='*60}")
    print(f"  Trend Score:    {result['trend_score']}/100")
    print(f"  Trend Direction: {result['trend_direction']}")
    print(f"  Weighted Signal: {result['weighted_signal']:.4f}")
    print(f"  Active Rules:    {result['n_active']}/{result['n_survivors']}")
    print(f"  Degraded:        {result['degraded']}")
    print(f"\n  投票明细:")
    for v in result['vote_details']:
        signal_marker = '▲多' if v['signal'] == 1 else ('▼空' if v['signal'] == -1 else '─')
        print(f"    {v['rule_id']} {signal_marker} w={v['weight']:.3f} | {v['rule_name']}")
    print(f"\n  {result.get('vote_summary', '')}")


if __name__ == '__main__':
    main()
