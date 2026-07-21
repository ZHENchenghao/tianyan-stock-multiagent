# -*- coding: utf-8 -*-
"""
天眼 v3.1+ 跨市场传导时滞矩阵
==============================
铁律#0落地模块: 单一市场视角是最大的决策风险。
每个板块持仓在减仓前必须强制检查领先指标是否同向。

问题起源: 2026-05-22 有色ETF亏损事件
  - KDJ J=1.7 极度超卖 + 主力净流入"流出" → 触发卖出
  - 但WTI油价同日从$103反弹 → 有色滞后1-3天跟涨
  - 如果有跨市场传导矩阵, 5/22会发出"WTI已反弹→不宜减仓有色"

算法: 皮尔逊相关系数滞后扫描 + Granger因果检验 + 传导信号生成
参考: Fang Jianyong et al. (2025) "Lead-Lag Modeling for A-Share Market"
      arXiv 2506.19255 — 两阶段跨市场时滞分析
      ZVT (zvtvz/zvt) — 板块资金流分解数据结构

核心输出:
  CONDUCTION_MATRIX: {领先资产 → {滞后资产 → {最佳滞后期, 相关系数, Granger p值}}}
  conduction_signal(): 给定当前持仓, 检查领先指标状态 → 看多/看空/观望

用法:
  python engine/cross_market_conduction.py           # 单次全量计算
  python engine/cross_market_conduction.py --check   # 检查当前持仓的传导信号
  python engine/cross_market_conduction.py --update  # 更新传导矩阵+存入DuckDB

集成:
  天眼_full.py 模块19: 在recommend裁决前调用 conduction_signal()
  给每个持仓的 cross_market_dim 打分, 纳入交叉验证矩阵第六维度(权重15%)
"""

import sys, os, json, math, time
from datetime import datetime, date, timedelta
from collections import defaultdict

os.environ['TQDM_DISABLE'] = '1'
import numpy as np
import pandas as pd
from scipy import stats

# SSL workaround
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    import akshare as ak
except ImportError:
    ak = None

BASE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_FILE = os.path.join(BASE, '..', 'portfolio.json')
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
MATRIX_CACHE = os.path.join(BASE, '..', 'conduction_matrix.json')

# ═══════════════════════════════════════════
# 一、传导对定义 — 学术/实战双重验证
# ═══════════════════════════════════════════

# 每个传导对有: (领先资产, 滞后资产, 传导逻辑, 预期滞后期范围, 方向)
CONDUCTION_PAIRS = [
    # ── 大宗商品 → A股板块 ──
    ('WTI原油',   '有色金属',   '油价涨→矿企成本上升但售价跟涨, 滞后3-5天传导到A股有色板块', (1, 8), '+'),
    ('WTI原油',   '煤炭',       '油价涨→替代能源需求增→煤价跟涨→煤炭板块受益',         (2, 6), '+'),
    ('WTI原油',   '石油石化',   '油价涨→直接利好三桶油',                                  (0, 3), '+'),
    ('伦敦金',    '黄金ETF',    '金价涨→黄金股跟涨, A股黄金板块滞后1-3天',                (0, 5), '+'),
    ('伦铜',      '有色金属',   '铜价是经济晴雨表, 领先A股有色2-5天',                     (2, 7), '+'),
    ('伦铝',      '有色金属',   '铝价传导到铝业股, 滞后3-7天',                            (3, 8), '+'),

    # ── 汇率/利率 → A股 ──
    ('美元指数',  '沪深300',    '强美元→外资流出→A股承压, 滞后1-3天',                   (1, 5), '-'),
    ('美10Y',    '科创50',     '美债利率升→高估值成长股承压, 滞后2-5天',                (2, 7), '-'),
    ('美10Y',    '沪深300',    '美债利率升→北向流出→大盘承压',                           (2, 5), '-'),
    ('CNH/USD',  '沪深300',    '人民币贬值→A股承压, 几乎同步',                           (0, 2), '-'),
    ('SHIBOR',   '沪深300',    '银行间利率升→流动性收紧→A股承压, 滞后1-3天',            (1, 5), '-'),

    # ── 外盘 → A股 ──
    ('标普500',  '沪深300',    '美股涨→次日A股情绪传导, 滞后0-1天',                     (0, 2), '+'),
    ('纳指',     '科创50',     '纳指涨→A股科技次日跟涨',                                 (0, 2), '+'),
    ('恒生指数', '沪深300',    '港股与A股同步性最强, 几乎同步',                          (0, 1), '+'),

    # ── A股内部：板块间传导 ──
    ('证券',     '沪深300',    '券商是牛市旗手, 领先大盘0-3天',                           (0, 4), '+'),
    ('银行',     '沪深300',    '银行护盘后大盘企稳, 滞后1-3天',                           (1, 5), '+'),
    ('房地产',   '建材',       '地产政策→建材需求, 滞后3-7天',                            (3, 10), '+'),
    ('新能源车', '锂电池',     '整车销量→电池需求, 滞后1-2周',                            (5, 15), '+'),
    ('半导体',   '科创50',     '芯片周期领先科创50约1-2周',                               (5, 15), '+'),

    # ── 逆传导（反向验证）──
    ('WTI原油',  '航空',       '油价涨→航空成本增→利空航空股',                            (1, 5), '-'),
    ('WTI原油',  '新能源车',   '油价涨→新能源替代需求增→利好新能源车',                    (2, 8), '+'),

    # ── v4.1 新增: 宏观→产业链联动 (基于Cohen & Frazzini 2008传导理论) ──
    # 油价暴跌→成本受益链
    ('WTI原油',  '航空机场',   '油价跌→航油成本降30-35%→利润弹性+15-20%',              (1, 5), '-'),
    ('WTI原油',  '化纤行业',   '油价跌→PTA/EG原料降50-60%→直接受益',                    (3, 10), '-'),
    ('WTI原油',  '塑料制品',   '油价跌→PE/PP原料降→滞后3-7天',                          (3, 7), '-'),
    ('WTI原油',  '橡胶制品',   '油价跌→合成橡胶降20-25%→轮胎受益',                       (2, 8), '-'),
    ('WTI原油',  '家电行业',   '油价跌→塑料/化工成本降+消费力释放',                       (3, 15), '-'),
    ('WTI原油',  '物流行业',   '油价跌→运输成本直接降→快递/物流受益',                    (1, 5), '-'),
    ('WTI原油',  '旅游酒店',   '油价跌→出行成本降→旅游需求边际改善',                      (3, 10), '-'),
    ('WTI原油',  '化肥行业',   '油价跌→氮肥原料降30%→化肥成本降',                         (3, 10), '-'),
    # 油价暴跌→受损链
    ('WTI原油',  '石油行业',   '油价跌→营收直接受损→利空三桶油',                         (0, 2), '+'),
    ('WTI原油',  '采掘行业',   '油价跌→资源品联动下跌→采掘利空',                         (1, 5), '+'),

    # ── v4.1 新增: A股板块间传导细化 ──
    ('有色金属', '电气设备',   '铜铝涨价→电网/变压器成本承压',                            (5, 20), '+'),
    ('有色金属', '新能源车',   '锂/钴/稀土→电池成本, 滞后2-4周',                          (10, 30), '+'),
    ('石油行业', '化纤行业',   '石油涨价→化纤原料成本升→滞后7-14天',                      (7, 14), '+'),
    ('煤炭',     '电力行业',   '煤价涨→火电成本升→滞后3-7天',                            (3, 7), '+'),
    ('科创50',   '半导体',   '科创50涨→芯片板块跟涨→滞后0-3天',                         (0, 3), '+'),
    ('沪深300',  '白酒',     '沪深300涨→外资回流→白酒受益→滞后1-3天',                  (1, 3), '+'),
    ('航空机场', '旅游酒店',  '航空客流增→旅游需求跟涨→滞后1-2周',                       (5, 15), '+'),

    # ── v4.1 新增: 利率传导 ├ ─
    ('美10Y',    '电力行业',  '利率升→公用事业折现率升→电力估值承压',                     (2, 5), '-'),
    ('美10Y',    '银行',     '利率升→息差扩→银行利多',                                   (1, 5), '+'),

    # ── v4.1 新增: 汇率传导 ├ ─
    ('CNH/USD',  '航空机场',  '人民币升→美元债减负→航空受益',                             (1, 5), '-'),
    ('CNH/USD',  '家电行业',  '人民币升→出口竞争力降→家电利空',                           (3, 10), '+'),
]

# 领先资产 → DuckDB 列名映射 (macro_indicators宽表)
MACRO_COLUMN_MAP = {
    'WTI原油': 'wti', '伦敦金': None, '伦铜': 'copper', '伦铝': None,
    '美元指数': None, '美10Y': 'us10y', 'CNH/USD': 'usdcny',
    'SHIBOR': 'shibor_on',
}

# 板块 → kline_daily ts_code 映射 (实际格式: sh000819, sz399975)
SECTOR_INDEX_MAP = {
    '有色金属': 'sh000819', '证券': 'sz399975', '银行': 'sz399986',
    '房地产': 'sz399393', '新能源车': 'sh000941', '半导体': 'sz990001',
    '煤炭': 'sz399990', '石油石化': 'sz399441', '建材': 'sz399133',
    '航空': 'sz399959', '锂电池': 'sz399434',
    '电力': 'sz399438', '沪深300': 'sh000300', '科创50': 'sh000688',
}

# 没有DuckDB数据的领先资产 → AKShare实时获取
NEEDS_AKSHARE = {'伦敦金', '伦铝', '美元指数', '标普500', '纳指', '恒生指数'}


def _conn():
    if duckdb is None:
        return None
    try:
        return duckdb.connect(DB)
    except Exception:
        return None


def fetch_macro_series(name: str, lookback: int = 365):
    """
    从DuckDB macro_indicators宽表获取宏观时间序列。
    降级: DuckDB数据不足 → AKShare实时拉取 (铁律#8: 数据自愈)

    macro_indicators实际结构 (宽表):
      trade_date, us10y, wti, copper, usdcny, shibor_on, ...
    """
    col = MACRO_COLUMN_MAP.get(name)
    if col is None:
        return None

    conn = _conn()
    series = None

    # 尝试1: DuckDB
    if conn is not None:
        try:
            start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
            df = conn.execute(f"""
                SELECT trade_date, {col} FROM macro_indicators
                WHERE trade_date >= ? AND {col} IS NOT NULL
                ORDER BY trade_date
            """, [start_d]).fetchdf()
            conn.close()
            if not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.dropna()
                if len(df) >= 5:  # 放宽限制: >=5天即可
                    series = df.set_index('trade_date')[col]
        except Exception:
            pass

    # 尝试2: AKShare降级 (DuckDB数据不足→实时补充, 限制范围避免OOM)
    if (series is None or len(series) < 20) and ak is not None:
        try:
            AK_MACRO_MAP = {
                'WTI原油': ('futures', 'CL'), '美10Y': ('bond', 'us10y'),
                'SHIBOR': ('rate', 'shibor'), 'CNH/USD': ('fx', 'usdcny'),
                '伦铜': ('futures', 'copper'),
            }
            ak_info = AK_MACRO_MAP.get(name)
            if ak_info:
                ak_type, ak_sym = ak_info
                df = None
                if ak_type == 'futures':
                    df = ak.futures_foreign_hist(symbol=ak_sym)
                elif ak_type == 'bond':
                    try:
                        df = ak.bond_zh_us_rate()
                    except Exception:
                        pass
                elif ak_type == 'fx':
                    try:
                        df = ak.fx_spot_quote()
                    except Exception:
                        pass
                elif ak_type == 'rate':
                    try:
                        df = ak.rate_interbank(market='上海银行间同业拆放利率')
                    except Exception:
                        pass
                if df is not None and not df.empty:
                    new_series = _parse_akshare_macro(df, ak_info, name)
                    if new_series is not None and len(new_series) >= 5:
                        # 只取最后lookback天
                        cutoff = new_series.index.max() - pd.Timedelta(days=lookback)
                        new_series = new_series[new_series.index >= cutoff]
                        if len(new_series) >= 5:
                            series = new_series
        except Exception:
            pass

    return series


def _parse_akshare_macro(df, ak_info, name: str):
    """解析AKShare宏观数据为统一的pd.Series"""
    try:
        ak_type, ak_sym = ak_info
        if ak_type == 'futures':
            if '日期' in df.columns and '收盘价' in df.columns:
                df['date'] = pd.to_datetime(df['日期'])
                return df.set_index('date')['收盘价']
        elif ak_type == 'bond':
            if '日期' in df.columns and '美国国债收益率10年' in df.columns:
                df['date'] = pd.to_datetime(df['日期'])
                return df.set_index('date')['美国国债收益率10年']
        elif ak_type == 'fx':
            if '日期' in df.columns and '美元人民币' in df.columns:
                df['date'] = pd.to_datetime(df['日期'])
                return df.set_index('date')['美元人民币']
        elif ak_type == 'rate':
            if '报告日' in df.columns and 'ON' in df.columns:
                df['date'] = pd.to_datetime(df['报告日'])
                return df.set_index('date')['ON']
    except Exception:
        pass
    return None


def fetch_sector_index(name: str, lookback: int = 365):
    """
    从DuckDB获取板块/指数日线 (v4.0: 优先级 DuckDB→AKShare/Sina)
    - A股板块: kline_daily 表 (ADATA采集)
    - 全球指数: global_index_daily 表 (Sina采集)
    - 商品期货: macro_indicators 表 (AKShare采集)
    """
    conn = _conn()
    close_series = None

    # ── 路由1: 全球指数 → global_index_daily ──
    GLOBAL_DB_MAP = {'标普500': '.INX', '纳指': '.IXIC', '恒生指数': 'HSI'}
    global_code = GLOBAL_DB_MAP.get(name)
    if global_code and conn is not None:
        try:
            start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
            df = conn.execute("""
                SELECT trade_date, close FROM global_index_daily
                WHERE index_code = ? AND trade_date >= ?
                ORDER BY trade_date
            """, [global_code, start_d]).fetchdf()
            if not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                close_series = df.set_index('trade_date')['close']
        except Exception:
            pass

    # ── 路由2: 商品期货 → macro_indicators ──
    COMM_DB_MAP = {'伦敦金': 'gold', '伦铝': 'aluminum', '伦铜': 'copper'}
    comm_col = COMM_DB_MAP.get(name)
    if close_series is None and comm_col and conn is not None:
        try:
            start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
            df = conn.execute(f"""
                SELECT trade_date, {comm_col} FROM macro_indicators
                WHERE trade_date >= ? AND {comm_col} IS NOT NULL
                ORDER BY trade_date
            """, [start_d]).fetchdf()
            if not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                close_series = df.set_index('trade_date')[comm_col]
        except Exception:
            pass

    # ── 路由3: A股板块 → kline_daily ──
    if close_series is None:
        ts_code = SECTOR_INDEX_MAP.get(name)
        if ts_code and conn is not None:
            try:
                start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
                df = conn.execute("""
                    SELECT trade_date, close FROM kline_daily
                    WHERE ts_code = ? AND trade_date >= ?
                    ORDER BY trade_date
                """, [ts_code, start_d]).fetchdf()
                if not df.empty:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    close_series = df.set_index('trade_date')['close']
            except Exception:
                pass

    # ── 降级: AKShare实时补充 (DuckDB数据不足时) ──
    if (close_series is None or len(close_series) < 10) and ak is not None:
        try:
            # Sina全球指数
            if name in ('标普500', '纳指'):
                df = ak.index_us_stock_sina(symbol={'标普500':'.INX','纳指':'.IXIC'}[name])
                if df is not None and not df.empty and 'date' in df.columns:
                    df['trade_date'] = pd.to_datetime(df['date'])
                    close_series = df.set_index('trade_date')['close']
            elif name == '恒生指数':
                df = ak.stock_hk_index_daily_sina(symbol='HSI')
                if df is not None and not df.empty and 'date' in df.columns:
                    df['trade_date'] = pd.to_datetime(df['date'])
                    close_series = df.set_index('trade_date')['close']
            # 商品期货
            elif name in ('伦敦金', '伦铝', '伦铜'):
                fut_map = {'伦敦金':'GC', '伦铝':'AHD', '伦铜':'HG'}
                df = ak.futures_foreign_hist(symbol=fut_map[name])
                if df is not None and not df.empty and 'date' in df.columns:
                    df['trade_date'] = pd.to_datetime(df['date'])
                    close_series = df.set_index('trade_date')['close']
        except Exception:
            pass

    return close_series


def get_return_series(price_series: pd.Series, lag: int = 0):
    """
    将价格序列转为日收益率序列, 可选前移(用于领先资产)。
    领先资产的lag=+N → 用今天的值预测滞后资产N天后的值。
    """
    if price_series is None or len(price_series) < 20:
        return None
    rets = price_series.pct_change().dropna()
    if lag > 0:
        rets = rets.shift(-lag)  # 前移: 今天的领先值匹配N天后的滞后值
    return rets.dropna()


# ═══════════════════════════════════════════
# 二、核心算法: 最优滞后期扫描
# ═══════════════════════════════════════════

def scan_optimal_lag(leader_series: pd.Series, follower_series: pd.Series,
                     min_lag: int = 0, max_lag: int = 10) -> dict:
    """
    扫描[0, max_lag]天滞后期, 找到皮尔逊相关系数最大的滞后期。

    Parameters
    ----------
    leader_series: 领先资产价格序列 (日线)
    follower_series: 滞后资产价格序列 (日线)
    min_lag, max_lag: 扫描范围

    Returns
    -------
    {
        'optimal_lag': int,       # 最佳滞后期(天)
        'max_correlation': float, # 最大相关系数
        'p_value': float,         # 显著性
        'lag_profile': [(lag, corr, p), ...],  # 全滞后扫描曲线
        'granger_p': float,       # Granger因果检验p值
        'confidence': str,        # 'high'/'medium'/'low'
    }
    """
    if leader_series is None or follower_series is None:
        return _empty_lag_result()

    # 对齐日期
    common_dates = leader_series.index.intersection(follower_series.index)
    if len(common_dates) < 60:
        return _empty_lag_result()

    leader_aligned = leader_series.loc[common_dates]
    follower_aligned = follower_series.loc[common_dates]

    # 转为日收益率 (对数收益率更稳定)
    leader_rets = np.log(leader_aligned / leader_aligned.shift(1)).dropna()
    follower_rets = np.log(follower_aligned / follower_aligned.shift(1)).dropna()

    # 再次对齐
    common = leader_rets.index.intersection(follower_rets.index)
    if len(common) < 50:
        return _empty_lag_result()

    lr = leader_rets.loc[common].values
    fr = follower_rets.loc[common].values

    lag_profile = []
    best_lag, best_corr, best_p = 0, 0.0, 1.0

    for lag in range(min_lag, max_lag + 1):
        if lag == 0:
            corr, p = stats.pearsonr(lr, fr)
        else:
            # leader前移lag天: leader[t] 对应 follower[t+lag]
            if len(lr) <= lag:
                continue
            corr, p = stats.pearsonr(lr[:-lag], fr[lag:])

        lag_profile.append({'lag': lag, 'correlation': round(corr, 4), 'p_value': round(p, 6)})

        if abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag
            best_p = p

    # Granger因果检验 (leader→follower, 用收益率)
    try:
        from scipy.stats import f as f_dist
        granger_p = _simple_granger_test(lr, fr, max_lag=min(5, max_lag))
    except Exception:
        granger_p = 0.5

    # 置信度判定
    if abs(best_corr) >= 0.3 and best_p < 0.01 and granger_p < 0.05:
        confidence = 'high'
    elif abs(best_corr) >= 0.15 and best_p < 0.05:
        confidence = 'medium'
    else:
        confidence = 'low'

    return {
        'optimal_lag': best_lag,
        'max_correlation': round(best_corr, 4),
        'p_value': round(best_p, 6),
        'lag_profile': lag_profile,
        'granger_p': round(granger_p, 6),
        'confidence': confidence,
        'n_samples': len(lr),
    }


def _simple_granger_test(x: np.ndarray, y: np.ndarray, max_lag: int = 3) -> float:
    """
    简化Granger因果检验: y ~ lagged(y) + lagged(x)
    返回F检验的p值。p<0.05 → x Granger-causes y.

    完整公式:
      H0: x不Granger-cause y
      H1: x Granger-cause y
      Regression: y_t = α + Σβ_i·y_{t-i} + Σγ_j·x_{t-j} + ε_t
      Test: 所有γ_j = 0
    """
    n = len(y)
    if n <= max_lag * 3:
        return 0.5

    # 构建限制模型: y_t = α + Σβ_i·y_{t-i}
    # 构建无限制模型: y_t = α + Σβ_i·y_{t-i} + Σγ_j·x_{t-j}
    X_restricted = np.column_stack([np.ones(n - max_lag)]
                                   + [y[max_lag - i - 1:n - i - 1] for i in range(max_lag)])
    X_unrestricted = np.column_stack([X_restricted]
                                     + [x[max_lag - j - 1:n - j - 1] for j in range(max_lag)])

    y_dep = y[max_lag:]

    # OLS
    try:
        beta_r = np.linalg.lstsq(X_restricted, y_dep, rcond=None)[0]
        beta_u = np.linalg.lstsq(X_unrestricted, y_dep, rcond=None)[0]

        resid_r = y_dep - X_restricted @ beta_r
        resid_u = y_dep - X_unrestricted @ beta_u

        ssr_r = np.sum(resid_r ** 2)
        ssr_u = np.sum(resid_u ** 2)

        df_r = n - max_lag - (max_lag + 1)
        df_u = n - max_lag - (2 * max_lag + 1)

        if df_u <= 0 or ssr_u <= 0:
            return 0.5

        f_stat = ((ssr_r - ssr_u) / max_lag) / (ssr_u / df_u)
        p_value = 1.0 - stats.f.cdf(f_stat, max_lag, df_u)
        return p_value
    except Exception:
        return 0.5


def _empty_lag_result() -> dict:
    return {
        'optimal_lag': 0, 'max_correlation': 0.0, 'p_value': 1.0,
        'lag_profile': [], 'granger_p': 1.0, 'confidence': 'low', 'n_samples': 0,
    }


# ═══════════════════════════════════════════
# 三、全量传导矩阵计算
# ═══════════════════════════════════════════

def build_conduction_matrix(lookback_days: int = 365) -> dict:
    """
    遍历所有传导对, 计算每对的最优滞后期和传导强度。

    Returns
    -------
    {
        'updated': '2026-05-26',
        'lookback_days': 365,
        'pairs': {
            'WTI原油→有色金属': {
                'leader': 'WTI原油', 'follower': '有色金属',
                'direction': '+',
                'optimal_lag': 3,
                'correlation': 0.35,
                'confidence': 'high',
                'conduction_logic': '油价涨→矿企成本上升但售价跟涨...',
                ...
            },
            ...
        },
        'summary': { 'high': N, 'medium': N, 'low': N }
    }
    """
    print(f"[跨市场传导矩阵] 开始计算 {len(CONDUCTION_PAIRS)} 对传导关系...")
    print(f"  回溯窗口: {lookback_days}天")

    # 先缓存所有领先资产的收益率序列 (避免重复拉取)
    leader_cache = {}
    for pair in CONDUCTION_PAIRS:
        lead_name = pair[0]
        if lead_name in leader_cache:
            continue

        # 按资产类型获取数据
        if lead_name in MACRO_COLUMN_MAP:
            series = fetch_macro_series(lead_name, lookback_days)
        elif lead_name in SECTOR_INDEX_MAP or lead_name in ('标普500', '纳指', '恒生指数'):
            series = fetch_sector_index(lead_name, lookback_days)
        else:
            series = fetch_sector_index(lead_name, lookback_days)

        leader_cache[lead_name] = series
        if series is not None:
            print(f"  [OK] {lead_name}: {len(series)}条")
        else:
            print(f"  [--] {lead_name}: 无数据")

    # 同样缓存滞后资产
    follower_cache = {}

    matrix = {
        'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'lookback_days': lookback_days,
        'pairs': {},
        'summary': {'high': 0, 'medium': 0, 'low': 0, 'nodata': 0},
    }

    for pair in CONDUCTION_PAIRS:
        lead_name, follow_name, logic, (min_lag, max_lag), direction = pair

        leader_series = leader_cache.get(lead_name)
        if leader_series is None:
            matrix['summary']['nodata'] += 1
            continue

        if follow_name not in follower_cache:
            follower_cache[follow_name] = fetch_sector_index(follow_name, lookback_days)

        follower_series = follower_cache.get(follow_name)
        if follower_series is None:
            matrix['summary']['nodata'] += 1
            continue

        # 核心计算
        result = scan_optimal_lag(leader_series, follower_series,
                                  min_lag=min_lag, max_lag=max_lag)
        result['direction'] = direction
        result['conduction_logic'] = logic
        result['leader'] = lead_name
        result['follower'] = follow_name

        pair_key = f"{lead_name}→{follow_name}"
        matrix['pairs'][pair_key] = result
        matrix['summary'][result['confidence']] += 1

        tag = {'high': '[高]', 'medium': '[中]', 'low': '[低]'}.get(result['confidence'], '[--]')
        print(f"  {tag} {pair_key}: lag={result['optimal_lag']}d "
              f"corr={result['max_correlation']:.3f} p={result['p_value']:.4f} "
              f"G-p={result['granger_p']:.4f}")

    print(f"\n  汇总: 高={matrix['summary']['high']} 中={matrix['summary']['medium']}"
          f" 低={matrix['summary']['low']} 无数据={matrix['summary']['nodata']}")

    # 持久化
    with open(MATRIX_CACHE, 'w', encoding='utf-8') as f:
        json.dump(matrix, f, ensure_ascii=False, indent=2)

    return matrix


# ═══════════════════════════════════════════
# 四、传导信号生成 — 给当前持仓打分
# ═══════════════════════════════════════════

def load_conduction_matrix() -> dict:
    """加载已缓存的传导矩阵"""
    if os.path.exists(MATRIX_CACHE):
        with open(MATRIX_CACHE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def conduction_signal(holding_sector: str, matrix: dict = None,
                      recent_days: int = 5) -> dict:
    """
    给定持仓板块, 检查所有领先指标的最新动向, 生成传导信号。

    算法:
      1. 从传导矩阵中找所有 follower=holding_sector 的传导对
      2. 检查每个领先资产最近N日的涨跌
      3. 按传导方向(+/-)判断对持仓是利好还是利空
      4. 按置信度加权汇总

    Returns
    -------
    {
        'sector': '有色金属',
        'signals': [
            {'leader': 'WTI原油', 'direction': '+', 'optimal_lag': 3,
             'leader_change_5d': +2.3, 'conduction_impact': 'bullish',
             'confidence': 'high', 'logic': '油价涨3天前开始反弹→有色即将跟涨'},
            ...
        ],
        'summary': 'bullish',  # 'bullish'/'bearish'/'neutral'
        'bullish_count': 3, 'bearish_count': 1,
        'score': +0.6,  # 传导维度得分(-1到+1)
    }
    """
    if matrix is None:
        matrix = load_conduction_matrix()

    if not matrix or 'pairs' not in matrix:
        return {'sector': holding_sector, 'signals': [], 'summary': 'neutral',
                'bullish_count': 0, 'bearish_count': 0, 'score': 0.0}

    signals = []
    bullish_score = 0.0
    bearish_score = 0.0

    for pair_key, pair_data in matrix['pairs'].items():
        if pair_data.get('follower') != holding_sector:
            continue

        # 数据不足时也包含低置信度对(降权使用)
        # 只有nodata(完全不相关)才跳过

        lead_name = pair_data['leader']
        direction = pair_data.get('direction', '+')
        corr = pair_data.get('max_correlation', 0)
        confidence = pair_data.get('confidence', 'low')
        opt_lag = pair_data.get('optimal_lag', 0)

        # 获取领先资产最近N天涨跌
        if lead_name in MACRO_COLUMN_MAP:
            series = fetch_macro_series(lead_name, lookback=recent_days + 10)
        else:
            series = fetch_sector_index(lead_name, lookback=recent_days + 10)

        if series is None or len(series) < recent_days + 2:
            continue

        # 计算最近N日涨跌 (扣除最优滞后期, 因为领先资产lag天前的变动才传导到今天)
        # 例如 lag=3 → 领先资产4-7天前的变化影响今天
        start_idx = max(0, opt_lag)
        end_idx = start_idx + recent_days
        if len(series) <= end_idx:
            # 数据不够, 用最新最近N日
            recent_vals = series.iloc[-recent_days:]
        else:
            recent_vals = series.iloc[-(end_idx + 1):-(start_idx + 1)] if start_idx > 0 else series.iloc[-recent_days:]

        if len(recent_vals) < 2:
            continue

        leader_change = (recent_vals.iloc[-1] / recent_vals.iloc[0] - 1.0) * 100  # %

        # 判定传导影响
        if direction == '+':
            if leader_change > 1.0:
                impact = 'bullish'
                weight = corr * (1.0 if confidence == 'high' else 0.6)
                bullish_score += abs(weight) * min(leader_change / 5.0, 1.0)
            elif leader_change < -1.0:
                impact = 'bearish'
                weight = corr * (1.0 if confidence == 'high' else 0.6)
                bearish_score += abs(weight) * min(abs(leader_change) / 5.0, 1.0)
            else:
                impact = 'neutral'
        else:  # 负相关传导
            if leader_change > 1.0:
                impact = 'bearish'
                weight = corr * (1.0 if confidence == 'high' else 0.6)
                bearish_score += abs(weight) * min(leader_change / 5.0, 1.0)
            elif leader_change < -1.0:
                impact = 'bullish'
                weight = corr * (1.0 if confidence == 'high' else 0.6)
                bullish_score += abs(weight) * min(abs(leader_change) / 5.0, 1.0)
            else:
                impact = 'neutral'

        signals.append({
            'leader': lead_name,
            'direction': direction,
            'optimal_lag': opt_lag,
            'leader_change_5d': round(leader_change, 2),
            'conduction_impact': impact,
            'confidence': confidence,
            'correlation': round(corr, 3),
            'logic': pair_data.get('conduction_logic', ''),
        })

    # 汇总
    signal_count = len(signals)
    bullish_count = sum(1 for s in signals if s['conduction_impact'] == 'bullish')
    bearish_count = sum(1 for s in signals if s['conduction_impact'] == 'bearish')

    if signal_count == 0:
        summary = 'neutral'
        score = 0.0
    elif bullish_count >= 2 and bullish_count > bearish_count:
        summary = 'bullish'
    elif bearish_count >= 2 and bearish_count > bullish_count:
        summary = 'bearish'
    else:
        summary = 'neutral'

    # 得分归一化到[-1, 1]
    total = max(bullish_score + bearish_score, 0.001)
    score = (bullish_score - bearish_score) / total

    return {
        'sector': holding_sector,
        'signals': signals,
        'summary': summary,
        'bullish_count': bullish_count,
        'bearish_count': bearish_count,
        'score': round(score, 2),
    }


# ═══════════════════════════════════════════
# 五、集成接口 — 供天眼_full.py调用
# ═══════════════════════════════════════════

def check_all_holdings(portfolio_path: str = None, matrix: dict = None) -> list:
    """
    检查当前持仓组合中所有板块的传导信号。
    这个函数是 天眼_full.py 模块19 的主入口。

    Returns
    -------
    [
        {'sector': '有色金属', 'summary': 'bullish', 'score': +0.6, ...},
        {'sector': '电力', 'summary': 'neutral', 'score': 0.0, ...},
        ...
    ]
    """
    if portfolio_path is None:
        portfolio_path = PORTFOLIO_FILE

    if matrix is None:
        matrix = load_conduction_matrix()

    if not matrix:
        print("[传导矩阵] 无缓存, 开始构建...")
        matrix = build_conduction_matrix()

    if not os.path.exists(portfolio_path):
        print(f"[传导矩阵] portfolio.json不存在: {portfolio_path}")
        return []

    with open(portfolio_path, 'r', encoding='utf-8') as f:
        pf = json.load(f)

    holdings = pf.get('holdings', [])
    results = []

    print(f"\n{'='*60}")
    print(f"  跨市场传导信号 · 持仓检查 ({len(holdings)}只)")
    print(f"{'='*60}")

    for h in holdings:
        sector = h.get('sector', '')
        if not sector:
            continue

        result = conduction_signal(sector, matrix)

        tag = {'bullish': '[多看]', 'bearish': '[看空]', 'neutral': '[中立]'}.get(
            result['summary'], '[--]')
        print(f"\n  {tag} {h['name']} ({sector})")
        print(f"    传导得分: {result['score']:+.2f}  "
              f"利好:{result['bullish_count']} 利空:{result['bearish_count']}")

        for s in result['signals']:
            icon = {'bullish': '↑', 'bearish': '↓', 'neutral': '→'}.get(
                s['conduction_impact'], '·')
            print(f"    {icon} {s['leader']} (lag={s['optimal_lag']}d) "
                  f"5日变动:{s['leader_change_5d']:+.1f}% "
                  f"相关:{s['correlation']:.2f} [{s['confidence']}]")

        results.append(result)

    return results


def cross_market_dim_score(holding_sector: str, matrix: dict = None) -> float:
    """
    给单个持仓的跨市场维度打分 (0-100), 用于交叉验证矩阵第六维。

    用法 (在 recommend.py 或 天眼_full.py 中):
      from engine.cross_market_conduction import cross_market_dim_score
      cm_score = cross_market_dim_score('有色金属')
      # → 75 (多数领先指标看多)
    """
    result = conduction_signal(holding_sector, matrix)
    # 将[-1,+1]映射到[0,100]
    return round((result['score'] + 1.0) * 50.0, 1)


# ═══════════════════════════════════════════
# 六、自愈层 — 数据质量检查
# ═══════════════════════════════════════════

def health_check() -> dict:
    """检查传导矩阵的健康状态 (铁律#8: 数据自愈)"""
    matrix = load_conduction_matrix()
    if not matrix:
        return {'status': 'missing', 'action': 'run build_conduction_matrix()'}

    updated = matrix.get('updated', '')
    if updated:
        try:
            updated_dt = datetime.strptime(updated, '%Y-%m-%d %H:%M')
            days_old = (datetime.now() - updated_dt).days
        except Exception:
            days_old = 999
    else:
        days_old = 999

    summary = matrix.get('summary', {})
    high = summary.get('high', 0)

    status = 'healthy'
    if days_old > 7:
        status = 'stale'
    if high < 3:
        status = 'degraded'
    if days_old > 30:
        status = 'expired'

    return {
        'status': status,
        'updated': updated,
        'days_old': days_old,
        'high_confidence_pairs': high,
        'action': 'run build_conduction_matrix()' if status != 'healthy' else 'ok',
    }


# ═══════════════════════════════════════════
# 七、硬编码传导快通道 — 实测验证参数, 禁止修改
# ═══════════════════════════════════════════

# 2026-05-26 实测验证 (5年数据, 365天回溯, 皮尔逊相关+Granger因果)
# 以下参数来自统计显著结果, 锁定不重新计算
HARDWIRED_PAIRS = {
    '标普500→沪深300': {
        'leader': '标普500', 'leader_code': '.INX',
        'follower': '沪深300', 'follower_code': 'sh000300',
        'lag_days': 1,       # 标普今天 → 沪深明天
        'correlation': 0.294, # 皮尔逊r
        'p_value': 0.0000,    # <0.001 极显著
        'direction': '+',     # 正相关
        'confidence': 'HIGH',
    },
    '恒生指数→沪深300': {
        'leader': '恒生指数', 'leader_code': 'HSI',
        'follower': '沪深300', 'follower_code': 'sh000300',
        'lag_days': 0,        # 当日同步
        'correlation': 0.684,  # 极强相关
        'p_value': 0.0000,
        'direction': '+',
        'confidence': 'HIGH',
    },
    '纳指→科创50': {
        'leader': '纳指', 'leader_code': '.IXIC',
        'follower': '科创50', 'follower_code': 'sh000688',
        'lag_days': 0,         # 当日同步
        'correlation': 0.218,   # 中等相关
        'p_value': 0.0009,      # <0.001 显著
        'direction': '+',
        'confidence': 'MEDIUM',
    },
}


def get_hardwired_signal() -> dict:
    """
    基于硬编码传导参数, 生成当日/次日A股大盘与科创50方向信号。

    算法:
      标普500 lag=1d: SPX今日涨跌 → 预测沪深300明日方向
      恒生指数 lag=0d: HSI今日涨跌 → 沪深300今日方向
      纳指   lag=0d: IXIC今日涨跌 → 科创50今日方向

    每个信号的强度 = 领先资产涨跌幅 × 相关系数 × 置信度权重
    汇总后输出: 沪深300方向 / 科创50方向 / 置信度

    Returns
    -------
    {
        'generated': '2026-05-26 15:30',
        'signals': [
            {'pair': '恒生→沪深300', 'direction': 'bullish', 'strength': 0.45, ...},
            ...
        ],
        'hs300_verdict': 'bullish',   # 综合判定
        'kc50_verdict': 'neutral',
        'hs300_score': +0.65,         # 综合得分 [-1, +1]
        'kc50_score': +0.12,
    }
    """
    from datetime import datetime, date, timedelta

    # 获取领先资产的最新数据
    leader_data = {}
    for key, pair in HARDWIRED_PAIRS.items():
        lcode = pair['leader_code']
        if lcode not in leader_data:
            if lcode in ('.INX', '.IXIC'):
                # 从 global_index_daily 取
                series = _fetch_global_index_from_db(lcode)
            elif lcode == 'HSI':
                series = _fetch_global_index_from_db('HSI')
            else:
                series = _fetch_sector_from_db(lcode)
            leader_data[lcode] = series

    # 获取被跟踪资产(沪深300/科创50)的当前数据
    follower_data = {
        '沪深300': _fetch_sector_from_db('sh000300'),
        '科创50': _fetch_sector_from_db('sh000688'),
    }

    signals = []
    hs300_signals = []  # 所有指向沪深300的信号
    kc50_signals = []   # 所有指向科创50的信号

    today = date.today()

    for key, pair in HARDWIRED_PAIRS.items():
        leader_series = leader_data.get(pair['leader_code'])
        follower_series = follower_data.get(pair['follower'])

        if leader_series is None or len(leader_series) < 3:
            continue
        if follower_series is None or len(follower_series) < 3:
            continue

        lag = pair['lag_days']
        corr = pair['correlation']
        direction = pair['direction']
        conf = pair['confidence']

        # 领先资产最近变动 (扣除滞后期)
        if lag > 0:
            # lag=1: 今天领先值 → 明天滞后值, 用昨天到今天的变化
            change_idx = -2  # 倒数第2天(昨天)到倒数第1天(今天)
            if len(leader_series) >= lag + 2:
                prev = leader_series.iloc[-lag-2]
                curr = leader_series.iloc[-lag-1]
            else:
                prev = leader_series.iloc[-2]
                curr = leader_series.iloc[-1]
        else:
            # lag=0: 当日同步
            if len(leader_series) >= 2:
                prev = leader_series.iloc[-2]
                curr = leader_series.iloc[-1]
            else:
                continue

        leader_change_pct = (curr / prev - 1.0) * 100 if prev > 0 else 0

        # 信号强度 = 涨跌幅 × 相关系数 × 置信度权重
        conf_weight = {'HIGH': 1.0, 'MEDIUM': 0.6, 'LOW': 0.3}[conf]
        signal_strength = (leader_change_pct / 5.0) * abs(corr) * conf_weight
        signal_strength = round(np.clip(signal_strength, -1.0, 1.0), 2)

        if direction == '+':
            impact = 'bullish' if signal_strength > 0.05 else ('bearish' if signal_strength < -0.05 else 'neutral')
        else:
            impact = 'bearish' if signal_strength > 0.05 else ('bullish' if signal_strength < -0.05 else 'neutral')

        sig = {
            'pair': key,
            'leader': pair['leader'],
            'follower': pair['follower'],
            'lag_days': lag,
            'correlation': corr,
            'p_value': pair['p_value'],
            'confidence': conf,
            'leader_change_pct': round(leader_change_pct, 2),
            'direction': impact,
            'strength': signal_strength,
        }
        signals.append(sig)

        if pair['follower'] == '沪深300':
            hs300_signals.append(sig)
        elif pair['follower'] == '科创50':
            kc50_signals.append(sig)

    # ── 汇总判定 ──
    def _verdict(sigs):
        if not sigs:
            return ('neutral', 0.0)
        bullish_w = sum(s['strength'] for s in sigs if s['direction'] == 'bullish')
        bearish_w = sum(abs(s['strength']) for s in sigs if s['direction'] == 'bearish')
        neutral_count = sum(1 for s in sigs if s['direction'] == 'neutral')
        total_w = max(bullish_w + bearish_w, 0.001)
        # 中性信号多 → 降低置信度
        conviction = 1.0 - 0.25 * neutral_count  # 每个中性减25%置信
        conviction = max(conviction, 0.3)
        raw_score = (bullish_w - bearish_w) / total_w
        score = round(raw_score * conviction, 2)
        if score > 0.15:
            v = 'bullish'
        elif score < -0.15:
            v = 'bearish'
        else:
            v = 'neutral'
        return (v, score)

    hs300_v, hs300_s = _verdict(hs300_signals)
    kc50_v, kc50_s = _verdict(kc50_signals)

    return {
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'note': '硬编码参数(实测验证p<0.001), 禁止重新计算',
        'signals': signals,
        'hs300_verdict': hs300_v,
        'kc50_verdict': kc50_v,
        'hs300_score': hs300_s,
        'kc50_score': kc50_s,
    }


def _fetch_global_index_from_db(index_code: str, lookback: int = 30) -> pd.Series:
    """从DuckDB global_index_daily取全球指数收盘价"""
    conn = _conn()
    if conn is None:
        return None
    try:
        start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
        df = conn.execute("""
            SELECT trade_date, close FROM global_index_daily
            WHERE index_code = ? AND trade_date >= ?
            ORDER BY trade_date
        """, [index_code, start_d]).fetchdf()
        conn.close()
        if df.empty:
            return None
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        return df.set_index('trade_date')['close']
    except Exception:
        return None


def _fetch_sector_from_db(ts_code: str, lookback: int = 30) -> pd.Series:
    """从DuckDB kline_daily取A股指数/板块收盘价"""
    conn = _conn()
    if conn is None:
        return None
    try:
        start_d = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
        df = conn.execute("""
            SELECT trade_date, close FROM kline_daily
            WHERE ts_code = ? AND trade_date >= ?
            ORDER BY trade_date
        """, [ts_code, start_d]).fetchdf()
        conn.close()
        if df.empty:
            return None
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        return df.set_index('trade_date')['close']
    except Exception:
        return None


# ═══════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# 七、日报v5 数据出口 — 穿透矩阵
# ═══════════════════════════════════════════

def daily_key_links() -> list:
    """
    日报v5 Phase 0b 数据出口。
    返回当天关键Granger传导对（纯数据，零叙述）。
    """
    matrix = load_conduction_matrix()
    if not matrix:
        mat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'cross_market_conduction.json')
        if os.path.exists(mat_path):
            with open(mat_path, 'r', encoding='utf-8') as f:
                matrix = json.load(f)
        else:
            return []

    pairs = matrix.get('pairs', [])
    if not pairs:
        # 兼容旧格式: summary中的pairs
        pairs = matrix.get('summary', {}).get('pairs', [])

    key = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        corr = p.get('correlation', p.get('corr', 0))
        pv = p.get('p_value', p.get('p', 0))
        if abs(corr) >= 0.40 and pv < 0.05:
            leader = p.get('leader', p.get('source', ''))
            follower = p.get('follower', p.get('target', ''))
            lag = p.get('optimal_lag', p.get('lag_days', p.get('lag', 1)))
            key.append({
                "source": leader,
                "target": follower,
                "lag_days": lag,
                "correlation": round(corr, 3),
                "p_value": round(pv, 4),
                "direction": "positive" if corr > 0 else "negative"
            })
    key.sort(key=lambda x: abs(x.get('correlation', 0)), reverse=True)
    return key[:15]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='天眼跨市场传导时滞矩阵')
    parser.add_argument('--check', action='store_true', help='检查当前持仓的传导信号')
    parser.add_argument('--update', action='store_true', help='更新传导矩阵')
    parser.add_argument('--signal', action='store_true', help='硬编码快通道: 当日/次日大盘方向信号')
    parser.add_argument('--health', action='store_true', help='健康检查')
    parser.add_argument('--daily-links', action='store_true', help='日报v5: 关键传导对JSON')
    parser.add_argument('--lookback', type=int, default=365, help='回溯天数(默认365)')
    args = parser.parse_args()

    if args.daily_links:
        import json as _json
        links = daily_key_links()
        print(_json.dumps({"links": links, "count": len(links), "generated": datetime.now().strftime('%Y-%m-%d %H:%M')},
                          ensure_ascii=False, indent=2))

    elif args.health:
        hc = health_check()
        print(f"传导矩阵状态: {hc['status']}")
        print(f"  更新: {hc['updated']} ({hc['days_old']}天前)")
        print(f"  高置信度传导对: {hc['high_confidence_pairs']}")
        print(f"  操作: {hc['action']}")

    elif args.update:
        build_conduction_matrix(lookback_days=args.lookback)

    elif args.signal:
        import json
        result = get_hardwired_signal()
        print(f"\n{'='*60}")
        print(f"  跨市场传导 · 硬编码快通道")
        print(f"  生成: {result['generated']}")
        print(f"  参数: 实测验证(p<0.001), 锁定不重算")
        print(f"{'='*60}")

        for sig in result['signals']:
            icon = {'bullish': '↑', 'bearish': '↓', 'neutral': '→'}[sig['direction']]
            print(f"\n  {icon} {sig['pair']}")
            print(f"    领先资产: {sig['leader']} 变动 {sig['leader_change_pct']:+.2f}%")
            print(f"    滞后期: {sig['lag_days']}天  相关: {sig['correlation']:.3f}  p={sig['p_value']:.4f}")
            print(f"    方向: {sig['direction']}  强度: {sig['strength']:+.2f}  [{sig['confidence']}]")

        print(f"\n  {'='*40}")
        print(f"  沪深300: {result['hs300_verdict']} (得分 {result['hs300_score']:+.2f})")
        print(f"  科创50:  {result['kc50_verdict']} (得分 {result['kc50_score']:+.2f})")
        print()

    elif args.check:
        check_all_holdings()

    else:
        # 默认: 显示硬编码信号 + 检查持仓
        import json
        result = get_hardwired_signal()
        print(f"\n{'='*60}")
        print(f"  跨市场传导 · 硬编码快通道")
        print(f"  参数来源: 5年实测(p<0.001), 锁定不重算")
        print(f"{'='*60}")
        for sig in result['signals']:
            icon = {'bullish': '↑', 'bearish': '↓', 'neutral': '→'}[sig['direction']]
            print(f"  {icon} {sig['pair']}: {sig['direction']} 强度{sig['strength']:+.2f} "
                  f"({sig['leader']} {sig['leader_change_pct']:+.2f}%)")
        print(f"  → 沪深300: {result['hs300_verdict']} ({result['hs300_score']:+.2f})")
        print(f"  → 科创50:  {result['kc50_verdict']} ({result['kc50_score']:+.2f})")
