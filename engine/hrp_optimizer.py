# -*- coding: utf-8 -*-
"""
天眼 v7 → v8 升级 · HRP 组合优化器 v2.0
=========================================
数据适配: DuckDB kline_daily 实测 仅 OHLCV 五字段有效,
          change_pct / turnover_rate / pre_close 全为 NULL。
          所有衍生指标从 close 和 vol 自算。

四条防御线:
  防御线1: 僵尸股硬过滤
    - ActiveRatio: 60日 vol>0 且非涨跌停天数占比 (涨跌停从 close 自算)
    - ReturnDispersion: 60日收益率 IQR
  防御线2: 伪低波方差惩罚
    - Illiq: vol / 60日均量 → 惩罚低流动性标的
    - MonoRisk: 连续同号收益率检测 → 惩罚连续一字跌停
  防御线3: EMA 权重动量平滑 (η=0.3, 新资产 η=1.0)
  防御线4: 瀑布再分配 (下月迭代)

组合优化: PyPortfolioOpt HRPOpt (分层风险平价)
持久化: hrp_state.json (EMA平滑状态跨日保存)

用法:
  from engine.hrp_optimizer import HRPOptimizer
  opt = HRPOptimizer()
  result = opt.optimize(['sh600519', 'sz000858', '601398.SH', ...])
"""

import sys, os, io, json, math, warnings
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict

warnings.filterwarnings('ignore')

import duckdb
import numpy as np
import pandas as pd

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_PATH = r'D:\FreeFinanceData\data\duckdb\finance.db'
STATE_FILE = os.path.join(PROJECT_DIR, 'hrp_state.json')

# ════════════════════════════════════════════════════
# 超参数
# ════════════════════════════════════════════════════

LOOKBACK_DAYS = 252           # 协方差估计窗口
MIN_ACTIVE_RATIO = 0.50       # 60日有量天数阈值
MIN_RETURN_DISPERSION = 0.001 # 日收益IQR阈值 (0.1个百分点)
LAMBDA_ILLIQ = 2.0            # 流动性惩罚强度
LAMBDA_MONO = 3.0             # 单调下行惩罚强度
MAX_STOCK_WEIGHT = 0.15       # 单票上限
ETA_MOMENTUM = 0.30           # EMA平滑学习率
CRISIS_CORR_THRESHOLD = 0.85  # 系统性危机触发
HIGH_CORR_THRESHOLD = 0.70    # 高相关模式触发
ZOMBIE_WINDOW = 60            # 僵尸检测窗口 (交易日)
MIN_DATA_DAYS = 20            # 最短数据天数要求

# ════════════════════════════════════════════════════
# 工具: DuckDB 最新日期
# ════════════════════════════════════════════════════

def _get_latest_date() -> Optional[str]:
    try:
        conn = duckdb.connect(DB_PATH, read_only=True)
        row = conn.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()
        conn.close()
        return str(row[0])[:10] if row and row[0] else None
    except Exception:
        return None


def _get_date_n_days_ago(days: int) -> Optional[str]:
    """从最新日期往回推 days 个自然日 (粗略, 留给SQL取更多)"""
    latest = _get_latest_date()
    if not latest:
        return None
    try:
        dt = datetime.strptime(latest, '%Y-%m-%d') - timedelta(days=days + 30)
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return None


# ════════════════════════════════════════════════════
# 防御线1: 僵尸股过滤器 (仅用 close + vol)
# ════════════════════════════════════════════════════

def detect_zombies(codes: List[str]) -> Tuple[Set[str], Dict[str, dict]]:
    """
    硬前置过滤器: 仅用 close 和 vol 检测僵尸股。

    (a) ActiveRatio: 过去 ZOMBIE_WINDOW 日中 vol>0 且非涨跌停天数占比
    (b) ReturnDispersion: 过去 ZOMBIE_WINDOW 日收益率的四分位距
    """
    if not codes:
        return set(), {}

    conn = duckdb.connect(DB_PATH, read_only=True)
    zombies = set()
    diagnostics = {}

    # 批量拉取 (一次查询, 分组处理)
    code_list = "', '".join(codes)
    cutoff = _get_date_n_days_ago(120) or '2020-01-01'

    raw = conn.execute(f"""
        SELECT ts_code, trade_date, close, vol
        FROM kline_daily
        WHERE ts_code IN ('{code_list}')
          AND close > 0
          AND trade_date >= '{cutoff}'
        ORDER BY ts_code, trade_date
    """).fetchdf()
    conn.close()

    if raw.empty:
        return set(codes), {c: {'zombie': True, 'reason': '无K线数据'} for c in codes}

    raw['trade_date'] = pd.to_datetime(raw['trade_date'])

    for code in codes:
        df = raw[raw['ts_code'] == code].copy()
        if len(df) < MIN_DATA_DAYS:
            zombies.add(code)
            diagnostics[code] = {
                'zombie': True,
                'reason': f'数据不足 (K线 {len(df)} < {MIN_DATA_DAYS} 日)',
                'active_ratio': round(len(df) / ZOMBIE_WINDOW, 3),
                'return_iqr': None,
            }
            continue

        # 只取最近 ZOMBIE_WINDOW 日
        df = df.sort_values('trade_date').tail(ZOMBIE_WINDOW)
        n = len(df)

        # (a) ActiveRatio: vol>0 且非涨跌停
        close = df['close'].values.astype(float)
        vol = df['vol'].values.astype(float)

        # 日收益率 (从close自算)
        ret = np.full(n, np.nan)
        ret[1:] = (close[1:] - close[:-1]) / close[:-1]

        # 涨跌停: abs(ret) > 9.8%
        is_limit = np.abs(ret) >= 0.098
        has_vol = (~np.isnan(vol)) & (vol > 0)
        active_days = ((~is_limit) & has_vol).sum()
        active_ratio = active_days / n if n > 0 else 0.0

        # (b) ReturnDispersion: IQR
        clean_ret = ret[~np.isnan(ret)]
        if len(clean_ret) >= 5:
            q75, q25 = np.percentile(clean_ret, [75, 25])
            return_iqr = q75 - q25
        else:
            return_iqr = 0.0

        is_zombie = (active_ratio < MIN_ACTIVE_RATIO) or (return_iqr < MIN_RETURN_DISPERSION)

        if is_zombie:
            zombies.add(code)
            reasons = []
            if active_ratio < MIN_ACTIVE_RATIO:
                reasons.append(f'ActiveRatio={active_ratio:.2f}<{MIN_ACTIVE_RATIO}')
            if return_iqr < MIN_RETURN_DISPERSION:
                reasons.append(f'ReturnIQR={return_iqr:.5f}<{MIN_RETURN_DISPERSION}')
            diagnostics[code] = {
                'zombie': True,
                'reason': ' | '.join(reasons),
                'active_ratio': round(active_ratio, 3),
                'return_iqr': round(return_iqr, 5),
            }
        else:
            diagnostics[code] = {
                'zombie': False,
                'reason': 'OK',
                'active_ratio': round(active_ratio, 3),
                'return_iqr': round(return_iqr, 5),
            }

    return zombies, diagnostics


# ════════════════════════════════════════════════════
# 防御线1.5: 系统性危机检测
# ════════════════════════════════════════════════════

def check_crisis_mode(price_df: pd.DataFrame) -> Tuple[bool, str]:
    """检测相关性矩阵是否坍缩"""
    if price_df.shape[1] < 2:
        return (True, 'CRISIS_EQUAL_WEIGHT')

    returns = price_df.pct_change().dropna()
    if returns.shape[0] < 20:
        return (True, 'CRISIS_EQUAL_WEIGHT')

    corr = returns.corr().values
    n = corr.shape[0]
    mask = ~np.eye(n, dtype=bool)
    avg_abs_corr = float(np.abs(corr[mask]).mean()) if mask.sum() > 0 else 0.0

    if avg_abs_corr > CRISIS_CORR_THRESHOLD:
        return (True, 'CRISIS_EQUAL_WEIGHT')
    elif avg_abs_corr > HIGH_CORR_THRESHOLD:
        return (False, 'HIGH_CORR_STRETCH')
    else:
        return (False, 'NORMAL')


# ════════════════════════════════════════════════════
# 防御线2: 伪低波方差惩罚 (vol-based)
# ════════════════════════════════════════════════════

def compute_penalized_covariance(
    price_df: pd.DataFrame,
    codes: List[str],
) -> np.ndarray:
    """
    对每个资产 i: σ̃²_i = σ̂²_i × (1 + λ₁·Illiq_i) × (1 + λ₂·MonoRisk_i)
    保持相关性结构: Σ̃[i,j] = ρ[i,j] × σ̃_i × σ̃_j

    指标均从 price_df 自算, 不需额外DB查询。
    """
    returns = price_df.pct_change().dropna()
    n = len(codes)
    T = len(returns)

    if T < 20 or n < 2:
        return returns.cov().values

    sample_cov = returns.cov().values
    sample_std = np.sqrt(np.maximum(np.diag(sample_cov), 1e-12))
    corr = returns.corr().values

    penalty = np.ones(n)

    # ── 批量读取 vol 数据 ──
    conn = duckdb.connect(DB_PATH, read_only=True)
    code_list = "', '".join(codes)
    cutoff = _get_date_n_days_ago(120) or '2020-01-01'

    vol_raw = conn.execute(f"""
        SELECT ts_code, trade_date, vol
        FROM kline_daily
        WHERE ts_code IN ('{code_list}')
          AND trade_date >= '{cutoff}'
        ORDER BY ts_code, trade_date
    """).fetchdf()
    conn.close()

    if not vol_raw.empty:
        vol_raw['trade_date'] = pd.to_datetime(vol_raw['trade_date'])

    for i, code in enumerate(codes):
        if sample_std[i] < 1e-10:
            penalty[i] = 10.0
            continue

        try:
            # (a) Illiq: 基于 vol 的流动性惩罚
            if not vol_raw.empty:
                vdf = vol_raw[vol_raw['ts_code'] == code]
                if len(vdf) >= 20:
                    recent_vol = vdf.sort_values('trade_date').tail(60)['vol'].values.astype(float)
                    recent_vol = recent_vol[~np.isnan(recent_vol)]
                    if len(recent_vol) >= 10:
                        avg_vol = float(np.median(recent_vol))
                        # 用自身60日均量的衰减作为流动性度量
                        # avg_vol < 100万股 视为极不活跃
                        vol_ref = max(avg_vol, 100_000)  # 地板: 10万股
                        vol_score = np.log10(vol_ref / 100_000)
                        illiq = max(0.0, 1.0 - vol_score / 4.0)  # log10(1e7/1e5)=2, log10(1e9/1e5)=4
                    else:
                        illiq = 1.0
                else:
                    illiq = 1.0
            else:
                illiq = 1.0

            # (b) MonoRisk: 连续同号下行检测 (从 price_df 自算)
            col_ret = returns.iloc[:, i].values if i < returns.shape[1] else np.array([])
            if len(col_ret) >= 10:
                col_ret = col_ret[~np.isnan(col_ret)]
                max_run = 0
                cur_run = 1
                for j in range(1, len(col_ret)):
                    if np.sign(col_ret[j]) == np.sign(col_ret[j-1]) and col_ret[j] != 0:
                        cur_run += 1
                    else:
                        cur_run = 1
                    max_run = max(max_run, cur_run)
                is_down = float(col_ret[:max(max_run, 1)].mean()) < 0 if max_run > 0 else False
                mono_risk = (max_run / ZOMBIE_WINDOW) * (1.0 if is_down else 0.0)
            else:
                mono_risk = 0.0

            penalty[i] = (1.0 + LAMBDA_ILLIQ * illiq) * (1.0 + LAMBDA_MONO * mono_risk)

        except Exception:
            penalty[i] = 1.0

    # 重建协方差矩阵
    penalized_std = sample_std * np.sqrt(np.maximum(penalty, 1.0))
    penalized_cov = corr * penalized_std[:, None] * penalized_std[None, :]

    return penalized_cov


# ════════════════════════════════════════════════════
# 核心: HRP 优化 (惩罚协方差注入)
# ════════════════════════════════════════════════════

def run_hrp(
    price_df: pd.DataFrame,
    codes: List[str],
    crisis_mode: str,
    penalized_cov: np.ndarray,
) -> Dict[str, float]:
    """执行 HRP 优化, 含危机降级 + 硬截断"""
    n = len(codes)

    if crisis_mode == 'CRISIS_EQUAL_WEIGHT' or n < 2:
        w = 1.0 / max(n, 1)
        return {c: w for c in codes}

    try:
        from pypfopt import HRPOpt

        returns_raw = price_df.pct_change().dropna().values
        cov_raw = np.cov(returns_raw, rowvar=False)

        try:
            L_raw = np.linalg.cholesky(cov_raw)
            L_pen = np.linalg.cholesky(penalized_cov)
        except np.linalg.LinAlgError:
            # 非正定回退
            from pypfopt import expected_returns, risk_models
            hrp = HRPOpt(price_df.pct_change().dropna())
            weights = hrp.optimize()
            cleaned = hrp.clean_weights()
            return _postprocess_weights(cleaned, codes)

        # 白化 + 重新染色
        Z = np.linalg.solve(L_raw.T, returns_raw.T).T
        R_pen = Z @ L_pen.T
        returns_pen_df = pd.DataFrame(R_pen, columns=price_df.columns)

        hrp = HRPOpt(returns_pen_df)
        weights = hrp.optimize()
        cleaned = hrp.clean_weights()

    except Exception:
        w = 1.0 / n
        return {c: w for c in codes}

    return _postprocess_weights(cleaned, codes)


def _apply_cap_iterative(weights: Dict[str, float], cap: float = MAX_STOCK_WEIGHT,
                          max_iter: int = 50) -> Dict[str, float]:
    """
    迭代截断再分配: 超额只流向未触顶的资产, 不回流到已截断资产。
    保证最终结果 ∑w=1, 0≤w≤cap, 且只收敛于"所有资产都触顶"或"没有资产触顶"。
    """
    w = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(w.values())
    if total < 1e-10:
        n = max(len(w), 1)
        return {k: 1.0 / n for k in w}

    # 初始归一化
    w = {k: v / total for k, v in w.items()}

    for _ in range(max_iter):
        over = {k: v for k, v in w.items() if v > cap}
        if not over:
            break

        # 截断超额部分
        excess = sum(v - cap for v in over.values())
        for k in over:
            w[k] = cap

        # 超额只分配给未触顶资产 (按当前权重比例)
        under = {k: v for k, v in w.items() if v < cap - 1e-10}
        total_under = sum(under.values())

        if total_under < 1e-10:
            # 全部触顶 → 等权
            n = max(len(w), 1)
            return {k: 1.0 / n for k in w}

        for k in under:
            w[k] += excess * (under[k] / total_under)

    return w


def _postprocess_weights(cleaned: dict, codes: List[str]) -> Dict[str, float]:
    """硬截断 15% + 迭代再分配。cleaned keys 是列名(code), 不是整数索引。"""
    result = {}
    for code in codes:
        result[code] = float(cleaned.get(code, 0.0))

    total = sum(result.values())
    if total < 1e-10:
        w = 1.0 / max(len(codes), 1)
        return {c: w for c in codes}

    return _apply_cap_iterative(result, MAX_STOCK_WEIGHT)


# ════════════════════════════════════════════════════
# 防御线3: EMA 权重动量平滑
# ════════════════════════════════════════════════════

class WeightMomentum:
    """
    w_exec[t] = η · w_target[t] + (1-η) · w_exec[t-1]
    新资产 η=1.0, 退出资产自然衰减, 留存资产 EMA 平滑
    """

    def __init__(self, eta: float = ETA_MOMENTUM, state_file: str = STATE_FILE):
        self.eta = eta
        self.state_file = state_file
        self.last_executed: Dict[str, float] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.last_executed = data.get('last_executed', {})
            except Exception:
                self.last_executed = {}

    def save(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump({
                'last_executed': self.last_executed,
                'updated_at': datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    def smooth(self, target_weights: Dict[str, float]) -> Dict[str, float]:
        executed = {}
        all_codes = set(target_weights.keys()) | set(self.last_executed.keys())

        for code in all_codes:
            w_target = target_weights.get(code, 0.0)
            w_last = self.last_executed.get(code, 0.0)

            if w_last == 0.0 and w_target > 0:
                executed[code] = w_target
            elif w_target == 0.0 and w_last > 0:
                executed[code] = (1.0 - self.eta) * w_last
            else:
                executed[code] = self.eta * w_target + (1.0 - self.eta) * w_last

        total = sum(executed.values())
        if total > 1e-10:
            executed = {k: v / total for k, v in executed.items()}

        executed = {k: v for k, v in executed.items() if v > 0.001}

        self.last_executed = executed
        self.save()
        return executed


# ════════════════════════════════════════════════════
# 顶层接口: HRPOptimizer
# ════════════════════════════════════════════════════

def _calc_turnover(old_weights: Dict[str, float], new_weights: Dict[str, float]) -> float:
    """计算单边换手率 = 0.5 × Σ|w_new - w_old|"""
    if not old_weights:
        return round(sum(new_weights.values()), 4)  # 首次建仓
    all_codes = set(new_weights.keys()) | set(old_weights.keys())
    turnover = sum(
        abs(new_weights.get(c, 0.0) - old_weights.get(c, 0.0))
        for c in all_codes
    )
    return round(turnover / 2.0, 4)


class HRPOptimizer:
    """天眼 HRP 组合优化器 — 一站式接口"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.momentum = WeightMomentum()
        self._latest_date: Optional[str] = None

    @property
    def latest_date(self) -> str:
        if self._latest_date is None:
            self._latest_date = _get_latest_date() or date.today().isoformat()
        return self._latest_date

    def optimize(self, codes: List[str], apply_momentum: bool = True) -> dict:
        """
        主入口: candidate codes → HRP weights.

        Args:
          codes:          候选股票代码列表 (5-30只)
          apply_momentum: 是否启用 EMA 动量平滑

        Returns:
          dict 含 weights, target_weights, zombies, crisis_mode, diagnostics, turnover_est
        """
        empty_result = {
            'weights': {},
            'target_weights': {},
            'zombies': set(),
            'crisis_mode': 'CRISIS_EQUAL_WEIGHT',
            'diagnostics': {},
            'turnover_est': 0.0,
        }

        if not codes:
            return empty_result

        codes = list(dict.fromkeys(codes))  # 去重保序

        # ── Step 1: 僵尸股过滤 ──
        zombies, diagnostics = detect_zombies(codes)
        clean_codes = [c for c in codes if c not in zombies]

        if len(clean_codes) < 2:
            weights = _equal_weight(clean_codes)
            if apply_momentum:
                weights = self.momentum.smooth(weights)
            return {
                'weights': weights,
                'target_weights': weights,
                'zombies': zombies,
                'crisis_mode': 'CRISIS_EQUAL_WEIGHT',
                'diagnostics': diagnostics,
                'turnover_est': 0.0,
            }

        # ── Step 2: 拉取价格透视表 ──
        price_df = self._fetch_prices(clean_codes)

        if price_df is None or price_df.shape[1] < 2:
            weights = _equal_weight(clean_codes)
            if apply_momentum:
                weights = self.momentum.smooth(weights)
            return {
                'weights': weights,
                'target_weights': weights,
                'zombies': zombies,
                'crisis_mode': 'CRISIS_EQUAL_WEIGHT',
                'diagnostics': diagnostics,
                'turnover_est': 0.0,
            }

        # ── Step 3: 危机模式检测 ──
        is_crisis, crisis_mode = check_crisis_mode(price_df)

        if is_crisis:
            weights = _equal_weight(clean_codes)
            if apply_momentum:
                weights = self.momentum.smooth(weights)
            return {
                'weights': weights,
                'target_weights': weights,
                'zombies': zombies,
                'crisis_mode': crisis_mode,
                'diagnostics': diagnostics,
                'turnover_est': 0.0,
            }

        # ── Step 4: 惩罚协方差 ──
        penalized_cov = compute_penalized_covariance(price_df, clean_codes)

        # ── Step 5: HRP 优化 ──
        target_weights = run_hrp(price_df, clean_codes, crisis_mode, penalized_cov)

        # ── Step 6: EMA 动量平滑 ──
        old_weights = dict(self.momentum.last_executed)  # 快照 → 用于换手率计算
        if apply_momentum:
            executed_weights = self.momentum.smooth(target_weights)
        else:
            executed_weights = target_weights

        # ── Step 7: 估算换手率 (对比平滑前后的权重) ──
        turnover_est = _calc_turnover(old_weights, executed_weights)

        return {
            'weights': executed_weights,
            'target_weights': target_weights,
            'zombies': zombies,
            'crisis_mode': crisis_mode,
            'diagnostics': diagnostics,
            'turnover_est': turnover_est,
        }

    def _fetch_prices(self, codes: List[str]) -> Optional[pd.DataFrame]:
        """批量拉取 close 价格, 透视为 date × code 表"""
        conn = duckdb.connect(self.db_path, read_only=True)
        try:
            code_list = "', '".join(codes)
            cutoff = _get_date_n_days_ago(LOOKBACK_DAYS + 60) or '2020-01-01'
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
        pivot = pivot.tail(LOOKBACK_DAYS)

        valid_cols = [c for c in codes if c in pivot.columns and pivot[c].notna().sum() >= MIN_DATA_DAYS]
        if len(valid_cols) < 2:
            return None

        return pivot[valid_cols]

    def get_state(self) -> dict:
        """返回当前EMA状态 (供外部监控)"""
        return {
            'last_executed': self.momentum.last_executed,
            'eta': self.momentum.eta,
            'latest_date': self.latest_date,
        }


def _equal_weight(codes: List[str]) -> Dict[str, float]:
    n = max(len(codes), 1)
    w = 1.0 / n
    return {c: w for c in codes}


# ════════════════════════════════════════════════════
# CLI 自检
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    print('=' * 60)
    print('天眼 HRP 优化器 v2.0 · 自检')
    print(f'最新数据日期: {_get_latest_date()}')
    print('=' * 60)

    opt = HRPOptimizer()

    # 用真实存在且数据充足的代码 (混合两种格式)
    test_codes = [
        'sh600519',   # 贵州茅台 (旧格式)
        'sz000858',   # 五粮液
        'sh601398',   # 工商银行
        'sh600036',   # 招商银行
        'sh600900',   # 长江电力
        'sz300750',   # 宁德时代
        'sz002594',   # 比亚迪
        'sh600809',   # 山西汾酒
    ]

    result = opt.optimize(test_codes, apply_momentum=False)

    print(f'\n危机模式: {result["crisis_mode"]}')
    print(f'候选池: {len(test_codes)} → 过滤后 {len(result["weights"])} + 僵尸 {len(result["zombies"])}')
    print(f'换手率估算: {result.get("turnover_est", "N/A")}')

    print('\n── HRP 权重 (目标) ──')
    for code, w in sorted(result.get('target_weights', result['weights']).items(), key=lambda x: -x[1]):
        diag = result['diagnostics'].get(code, {})
        print(f'  {code:16s} {w:6.2%}  | 活跃率={diag.get("active_ratio","?"):.2f}  '
              f'IQR={diag.get("return_iqr","?"):.5f}  {diag.get("reason","?")}')

    if result['zombies']:
        print('\n── 僵尸股 (已踢出) ──')
        for code in sorted(result['zombies']):
            diag = result['diagnostics'].get(code, {})
            print(f'  {code:16s} {diag.get("reason","?")}')

    # 总权重检查
    total = sum(result['weights'].values())
    print(f'\n总权重: {total:.4f} ({len(result["weights"])} 只)')
    print('自检完成.')
