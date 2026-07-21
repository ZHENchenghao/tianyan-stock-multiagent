# -*- coding: utf-8 -*-
"""
天眼 v6.0 核心舱基本面引擎 — fundamental_engine.py
==================================================
定位: 场外基金(指数/增强型)核心舱动态打分
策略: "平时持稳、抗回撤、提供复利安全垫"

三因子模型:
  F1. 估值历史百分位 (Valuation Quantile) — 满分 40
  F2. 红利/股息率     (Dividend Yield)      — 满分 30
  F3. 盈利稳定性      (ROE Stability)       — 满分 30
                                            = 总分 100

数据流:
  AKShare (stock_index_pe_lg)        → PE 分位数
  AKShare (stock_zh_index_value_csindex) → 股息率
  DuckDB  financial_statements       → ROE / ROE 波动率
  DuckDB  valuation_daily            → PE/PB (个股, 兜底)

用法:
  from engine.fundamental_engine import score_core_fundamentals
  result = score_core_fundamentals('sh000300', '沪深300')
  # → {'score': 72, 'factors': {...}, 'verdict': '买入', ...}
"""

import json
import os
import ssl
import sys
import time
import warnings
from datetime import date, datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

# ── 环境 ────────────────────────────────────────────
ssl._create_default_https_context = ssl._create_unverified_context
os.environ["PYTHONHTTPSVERIFY"] = "0"
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = r"D:\FreeFinanceData\data\duckdb\finance.db"
TRACKED_FILE = os.path.join(os.path.dirname(BASE), "tracked_stocks.json")

# ── 指数代码 → AKShare 名称映射 ────────────────────
INDEX_AKSHARE_MAP = {
    "sh000016": {"name": "上证50",          "csindex": "000016", "pe_lg": "上证50"},
    "sh000300": {"name": "沪深300",         "csindex": "000300", "pe_lg": "沪深300"},
    "sh000688": {"name": "科创50",          "csindex": "000688", "pe_lg": "科创50"},
    "sh000819": {"name": "有色金属",        "csindex": "000819", "pe_lg": "有色金属"},
    "sh000905": {"name": "中证500",         "csindex": "000905", "pe_lg": "中证500"},
    "sz399001": {"name": "深证成指",        "csindex": "399001", "pe_lg": "深证成指"},
    "sz399006": {"name": "创业板指",        "csindex": "399006", "pe_lg": "创业板指"},
    "sz399160": {"name": "电力公用",        "csindex": "399160", "pe_lg": "电力公用事业"},
    "sz399261": {"name": "新能源电池",      "csindex": "399261", "pe_lg": "新能源车"},
    "sz399967": {"name": "军工",            "csindex": "399967", "pe_lg": "中证军工"},
    "sz399986": {"name": "银行",            "csindex": "399986", "pe_lg": "800银行"},
    "sz399997": {"name": "白酒",            "csindex": "399997", "pe_lg": "中证白酒"},
    "sz399438": {"name": "新能源车",        "csindex": "399438", "pe_lg": "新能源车"},
}

# ── AKShare 可用性 ─────────────────────────────────
_AKSHARE_AVAILABLE = None

def _has_akshare():
    global _AKSHARE_AVAILABLE
    if _AKSHARE_AVAILABLE is None:
        try:
            import akshare as _ak
            _AKSHARE_AVAILABLE = True
        except ImportError:
            _AKSHARE_AVAILABLE = False
    return _AKSHARE_AVAILABLE

# ── stock_index_pe_lg 支持的12个指数 ─────────────────
# 不在列表中的指数无法获取PE分位, 走降级路径
PE_LG_SUPPORTED = {
    "sh000016": "上证50",
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000009": "上证380",
    "sh000010": "上证180",
    "sh000903": "中证100",
    "sh000906": "中证800",
    "sh000852": "中证1000",
    "sh000015": "上证红利",
    "sz399673": "创业板50",
    "sz399324": "深证红利",
    "sz399330": "中证100",
}

# ── PE分位静态估算 (非PE_LG指数降级用) ──────────────
# 基于防御型/价值型指数天然低估的特征, 给予合理的PE分位估算
PE_FALLBACK = {
    "sz399986": 25.0,  # 银行 — 长期低估, PE分位约25%
    "sz399160": 30.0,  # 电力公用 — 稳定防御, PE分位约30%
    "sz399997": 55.0,  # 白酒 — 消费品适中
    "sh000819": 35.0,  # 有色金属 — 周期, 偏低估
    "sz399006": 40.0,  # 创业板 — 成长型
    "sh000688": 60.0,  # 科创50 — 高估值成长
    "sz399967": 45.0,  # 军工
    "sz399261": 50.0,  # 新能源电池
    "sz399001": 42.0,  # 深证成指
    "sz399438": 48.0,  # 新能源车
}

# ── CORE 标签默认列表 ──────────────────────────────
DEFAULT_CORE = [
    "sh000300", "sh000016", "sh000905", "sz399986", "sz399160",
]


def _load_core_list():
    """从 tracked_stocks.json 读取 CORE 标记, 兜底用默认列表"""
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            core = data.get("core_indices", [])
            if core:
                return core
        except Exception:
            pass
    return DEFAULT_CORE


# ═══════════════════════════════════════════════════════
# F1. 估值历史百分位 (满分 40)
# ═══════════════════════════════════════════════════════

def _fetch_pe_percentile_akshare(ts_code):
    """从 AKShare stock_index_pe_lg 获取滚动市盈率分位数

    仅 PE_LG_SUPPORTED 中的12个指数有数据, 其余返回 None.
    """
    if not _has_akshare():
        return None

    pe_lg_name = PE_LG_SUPPORTED.get(ts_code)
    if not pe_lg_name:
        return None

    try:
        import akshare as ak

        df = ak.stock_index_pe_lg(symbol=pe_lg_name)
        if df is None or df.empty:
            return None

        # 列名可能是「分位数」或「中位数」, 搜「位」字兜底
        pct_col = None
        for c in df.columns:
            cstr = str(c)
            if "位" in cstr:
                pct_col = c
                break

        if pct_col is None:
            return None

        latest = df.iloc[-1]
        val = float(latest[pct_col])
        return val

    except Exception as e:
        print(f"  [F1] AKShare PE分位获取失败 ({ts_code}/{pe_lg_name}): {e}")
        return None


def _fetch_pe_percentile_duckdb(ts_code):
    """从 DuckDB valuation_daily 尝试获取 PE 分位 (兜底)"""
    try:
        conn = duckdb.connect(DB_PATH)
        row = conn.execute(
            "SELECT pe_percentile_5y FROM valuation_daily "
            "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
            [ts_code],
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return None


def score_valuation(ts_code):
    """估值百分位因子 (0-40)

    数据源优先级: AKShare(PE_LG) > DuckDB > 静态映射

    PE分位越低 → 估值越低 → 得分越高
    """
    pct = _fetch_pe_percentile_akshare(ts_code)
    if pct is None:
        pct = _fetch_pe_percentile_duckdb(ts_code)
    if pct is None:
        pct = PE_FALLBACK.get(ts_code)

    if pct is None:
        return {"score": 15, "pct": None, "level": "数据缺失", "detail": "PE分位无数据, 给基准分15"}

    # 映射: 分位数 → 得分 (六段)
    if pct <= 10:
        score = 40
        level = "极度低估"
    elif pct <= 25:
        score = 35 + (25 - pct) / 15 * 5  # 35~40
        level = "显著低估"
    elif pct <= 40:
        score = 25 + (40 - pct) / 15 * 10  # 25~35
        level = "低估"
    elif pct <= 60:
        score = 15 + (60 - pct) / 20 * 10  # 15~25
        level = "合理"
    elif pct <= 80:
        score = 5 + (80 - pct) / 20 * 10  # 5~15
        level = "偏贵"
    else:
        score = max(0, (100 - pct) / 20 * 5)  # 0~5
        level = "泡沫"

    score = round(score, 1)

    return {
        "score": score,
        "pct": round(pct, 1),
        "level": level,
        "detail": f"PE分位{pct:.1f}%→{level}({score:.0f}/40)",
    }


# ═══════════════════════════════════════════════════════
# F2. 股息率因子 (满分 30)
# ═══════════════════════════════════════════════════════

def _fetch_dividend_yield_akshare(ts_code):
    """从 AKShare stock_zh_index_value_csindex 获取股息率"""
    if not _has_akshare():
        return None

    info = INDEX_AKSHARE_MAP.get(ts_code)
    if not info:
        return None

    csindex_code = info.get("csindex")
    if not csindex_code:
        return None

    try:
        import akshare as ak

        df = ak.stock_zh_index_value_csindex(symbol=csindex_code)
        if df is None or df.empty:
            return None

        div_col = None
        for c in df.columns:
            if "股息" in str(c):
                div_col = c
                break

        if div_col is None:
            return None

        latest = df.iloc[-1]
        val = float(latest[div_col])
        return val

    except Exception as e:
        print(f"  [F2] AKShare 股息率获取失败 ({ts_code}/{csindex_code}): {e}")
        return None


# ── 静态股息率映射 (AKShare 不可用时的兜底) ────────
FALLBACK_DIVIDEND = {
    "sh000016": 3.8,   # 上证50
    "sh000300": 2.5,   # 沪深300
    "sh000905": 1.8,   # 中证500
    "sz399986": 5.2,   # 中证银行
    "sz399160": 3.2,   # 电力公用
    "sh000819": 2.0,   # 有色金属
    "sz399006": 0.8,   # 创业板
    "sh000688": 0.5,   # 科创50
    "sz399997": 2.8,   # 白酒
    "sz399967": 1.2,   # 军工
    "sz399261": 1.0,   # 新能源电池
}


def score_dividend(ts_code):
    """股息率因子 (0-30)

    规则:
      高股息 = 抗跌性强, 得分高
      股息率 >= 4.5%  → 30 分 (满分)
      股息率 >= 3.0%  → 20~30 分
      股息率 >= 2.0%  → 10~20 分
      股息率 >= 1.0%  → 3~10 分
      股息率 < 1.0%   → 0~3 分
    """
    div_yield = _fetch_dividend_yield_akshare(ts_code)
    source = "AKShare"

    if div_yield is None:
        div_yield = FALLBACK_DIVIDEND.get(ts_code)
        source = "静态映射"

    if div_yield is None:
        return {"score": 8, "yield": None, "level": "数据缺失", "detail": "股息率无数据, 给基准分8"}

    if div_yield >= 4.5:
        score = 30
        level = "高股息"
    elif div_yield >= 3.0:
        score = 20 + (div_yield - 3.0) / 1.5 * 10
        level = "稳健股息"
    elif div_yield >= 2.0:
        score = 10 + (div_yield - 2.0) / 1.0 * 10
        level = "中等股息"
    elif div_yield >= 1.0:
        score = 3 + (div_yield - 1.0) / 1.0 * 7
        level = "低股息"
    else:
        score = max(0, div_yield / 1.0 * 3)
        level = "微股息"

    score = round(score, 1)

    return {
        "score": score,
        "yield": round(div_yield, 2),
        "level": level,
        "source": source,
        "detail": f"股息率{div_yield:.2f}%→{level}({score:.0f}/30)",
    }


# ═══════════════════════════════════════════════════════
# F3. ROE 稳定性因子 (满分 30)
# ═══════════════════════════════════════════════════════

def _fetch_roe_series_duckdb(ts_code):
    """从 DuckDB financial_statements 获取 ROE 时间序列 (近3年季度)"""
    try:
        conn = duckdb.connect(DB_PATH)
        df = conn.execute(
            "SELECT report_date, roe FROM financial_statements "
            "WHERE ts_code=? AND report_date >= DATE ? - INTERVAL 3 YEAR "
            "ORDER BY report_date",
            [ts_code, str(date.today())],
        ).fetchdf()
        conn.close()
        if df.empty:
            return None
        return df
    except Exception:
        return None


def _roe_stability_from_peers(ts_code):
    """从同类标的的 financial_statements 估算 ROE 稳定性

    对于没有个股财报的指数, 读取其成分股或相近指数的 ROE 数据.
    """
    # 指数 → 代表性成分股映射 (从 stock_basic 中挑有财报数据的)
    INDEX_CONSTITUENT_MAP = {
        "sh000016": ["600036", "601318", "600519", "600276", "601398"],  # 上证50
        "sh000300": ["600519", "600036", "000858", "000333", "601318"],  # 沪深300
        "sh000905": ["002415", "002142", "300750", "000333", "002027"],  # 中证500
        "sh000688": ["688981", "688012", "688111", "688036", "688008"],  # 科创50
        "sz399006": ["300750", "300059", "300015", "300124", "300274"],  # 创业板
        "sz399160": ["600900", "601088", "600886", "600025", "600011"],  # 电力
        "sz399986": ["600036", "601398", "601288", "601939", "600016"],  # 银行
    }

    peers = INDEX_CONSTITUENT_MAP.get(ts_code)
    if not peers:
        return None

    # 从 stock_basic 找到这些股票的实际 ts_code
    all_roe = []

    try:
        conn = duckdb.connect(DB_PATH)
        for symbol in peers:
            # 查 stock_basic 获取完整 ts_code
            row = conn.execute(
                "SELECT ts_code FROM stock_basic WHERE symbol=? LIMIT 1",
                [symbol],
            ).fetchone()
            if row is None:
                continue

            stock_code = row[0]
            df = conn.execute(
                "SELECT report_date, roe FROM financial_statements "
                "WHERE ts_code=? AND roe IS NOT NULL "
                "AND report_date >= DATE ? - INTERVAL 3 YEAR "
                "ORDER BY report_date",
                [stock_code, str(date.today())],
            ).fetchdf()

            if not df.empty:
                all_roe.append(df["roe"].dropna().values)

        conn.close()
    except Exception:
        pass

    if not all_roe:
        return None

    # 汇总所有成分股的 ROE 序列 (取均值后计算波动率)
    combined = []
    for arr in all_roe:
        combined.extend(arr.tolist())

    if len(combined) < 4:
        return None

    return np.array(combined)


def score_roe_stability(ts_code, sector_name=""):
    """ROE 稳定性因子 (0-30)

    计算 ROE 变异系数 CV = std(ROE) / mean(ROE)
    CV 越小 → ROE 越稳定 → 得分越高

    数据源: DuckDB financial_statements → 近3年季报ROE序列
    指数级: 读取成分股聚合ROE
    """
    # 先用自身代码
    roe_df = _fetch_roe_series_duckdb(ts_code)

    if roe_df is not None and len(roe_df) >= 4:
        roe_vals = roe_df["roe"].dropna().values
    else:
        # 用成分股聚合
        roe_vals = _roe_stability_from_peers(ts_code)
        if roe_vals is None:
            # 主题型指数 (如电力CORE) — 适配红利低波因子: 给较高稳定性分
            stable_sectors = {"电力公用", "银行", "上证50", "沪深300"}
            if sector_name in stable_sectors or any(
                kw in (sector_name or "") for kw in ["电力", "银行", "红利", "公用"]
            ):
                return {
                    "score": 24,
                    "roe_mean": None,
                    "roe_cv": None,
                    "level": "稳定(适配)",
                    "detail": f"{sector_name or ts_code}属红利低波型, 适配稳健分24/30",
                }
            return {
                "score": 15,
                "roe_mean": None,
                "roe_cv": None,
                "level": "数据不足",
                "detail": "ROE数据不足, 给基准分15",
            }

    if len(roe_vals) < 4:
        return {"score": 15, "roe_mean": None, "roe_cv": None, "level": "数据不足", "detail": "ROE数据<4期"}

    roe_mean = float(np.mean(roe_vals))
    roe_std = float(np.std(roe_vals, ddof=1))

    # 变异系数
    cv = (roe_std / abs(roe_mean)) if abs(roe_mean) > 0.001 else 2.0

    # CV 映射到得分
    if cv <= 0.15:
        score = 30
        level = "极稳定"
    elif cv <= 0.25:
        score = 24 + (0.25 - cv) / 0.10 * 6
        level = "稳定"
    elif cv <= 0.40:
        score = 15 + (0.40 - cv) / 0.15 * 9
        level = "中等波动"
    elif cv <= 0.60:
        score = 8 + (0.60 - cv) / 0.20 * 7
        level = "偏高波动"
    else:
        score = max(0, (1.0 - cv) * 8)
        level = "高波动"

    score = round(min(30, max(0, score)), 1)

    return {
        "score": score,
        "roe_mean": round(roe_mean * 100, 1),
        "roe_cv": round(cv, 3),
        "level": level,
        "detail": f"ROE均值{roe_mean*100:.1f}% CV={cv:.3f}→{level}({score:.0f}/30)",
    }


# ═══════════════════════════════════════════════════════
# 综合打分入口
# ═══════════════════════════════════════════════════════

def score_core_fundamentals(ts_code, sector_name=""):
    """核心舱基本面三因子打分

    Args:
      ts_code:     指数代码, 如 'sh000300'
      sector_name: 板块名称, 如 '沪深300'

    Returns:
      dict: {
        'ts_code': str,
        'sector': str,
        'total_score': float,     # 0-100
        'verdict': str,           # 强烈买入/买入/持有/回避
        'factors': {
            'valuation':  {...},  # F1
            'dividend':   {...},  # F2
            'roe_stability': {...},  # F3
        },
        'detail': str,
        'timestamp': str,
      }
    """
    if not sector_name:
        info = INDEX_AKSHARE_MAP.get(ts_code, {})
        sector_name = info.get("name", ts_code)

    # ── F1: 估值 ──
    f1 = score_valuation(ts_code)

    # ── F2: 股息 ──
    f2 = score_dividend(ts_code)

    # ── F3: ROE 稳定性 ──
    f3 = score_roe_stability(ts_code, sector_name)

    total = f1["score"] + f2["score"] + f3["score"]
    total = round(min(100, total), 1)

    # ── 裁决 ──
    if total >= 75:
        verdict = "强烈买入"
        emoji = "[G]"
    elif total >= 60:
        verdict = "买入"
        emoji = "[G]"
    elif total >= 45:
        verdict = "持有"
        emoji = "[Y]"
    elif total >= 30:
        verdict = "观望"
        emoji = "[Y]"
    else:
        verdict = "回避"
        emoji = "[R]"

    detail = (
        f"{emoji} {sector_name}({ts_code}) 基本面{total:.0f}/100 {verdict} | "
        f"估值{f1['score']:.0f}/40({f1['level']}) "
        f"股息{f2['score']:.0f}/30({f2['level']}) "
        f"ROE{f3['score']:.0f}/30({f3['level']})"
    )

    return {
        "ts_code": ts_code,
        "sector": sector_name,
        "total_score": total,
        "verdict": verdict,
        "factors": {
            "valuation": f1,
            "dividend": f2,
            "roe_stability": f3,
        },
        "detail": detail,
        "timestamp": datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════
# 批量打分: 核心舱全部标的
# ═══════════════════════════════════════════════════════

def score_all_core():
    """对 CORE 列表全部标的打分并排名

    Returns:
      list[dict]: 按 total_score 降序排列
    """
    core_list = _load_core_list()
    results = []

    print(f"[fundamental_engine] 核心舱三因子打分 ({len(core_list)}个标的)")
    print(f"{'='*70}")

    for ts_code in core_list:
        info = INDEX_AKSHARE_MAP.get(ts_code, {})
        name = info.get("name", ts_code)

        r = score_core_fundamentals(ts_code, name)
        results.append(r)

        # 打印分行报告
        print(f"\n  {r['detail']}")
        f = r["factors"]
        print(f"    F1估值: {f['valuation']['detail']}")
        print(f"    F2股息: {f['dividend']['detail']}")
        print(f"    F3 ROE: {f['roe_stability']['detail']}")

        # AKShare 限流间隔
        if len(results) < len(core_list):
            time.sleep(1.5)

    results.sort(key=lambda x: x["total_score"], reverse=True)

    print(f"\n{'='*70}")
    print(f"  核心舱排名:")
    for i, r in enumerate(results):
        print(f"  #{i+1} {r['sector']:10s} {r['total_score']:5.1f}分 {r['verdict']}")

    return results


# ═══════════════════════════════════════════════════════
# 与 v6.0 技术打分的对接桥接
# ═══════════════════════════════════════════════════════

def get_fundamental_bridge(ts_code, sector_name=""):
    """返回与技术打分模块兼容的基本面得分 (0-20)

    screening_engine._score_sector() 期望一个 prosperity_score (0-20),
    这里把三因子 0-100 总分映射到 0-20.
    """
    result = score_core_fundamentals(ts_code, sector_name)
    normalized = round(result["total_score"] / 5.0, 1)  # 100→20
    return {
        "prosperity_score": normalized,
        "fundamental_total": result["total_score"],
        "verdict": result["verdict"],
        "detail": result["detail"],
    }


# ═══════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "all":
        score_all_core()
    elif len(sys.argv) > 1:
        ts_code = sys.argv[1]
        sector = sys.argv[2] if len(sys.argv) > 2 else ""
        r = score_core_fundamentals(ts_code, sector)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        # 默认: 跑全部 CORE
        score_all_core()
