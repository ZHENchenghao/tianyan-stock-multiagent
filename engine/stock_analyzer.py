"""天眼三板斧 · 个股综合分析引擎
流程: 看天(market) → 看地(tech+fund) → 算账(plan)
输出: 加/减/观望 + 点位 + 概率分布
"""
import sys, os, json, time, math, duckdb
from datetime import date, timedelta
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = 'D:/FreeFinanceData/data/duckdb/finance.db'

def q(sql, params=None):
    return duckdb.connect(DB).execute(sql, params or []).fetchdf()

def ok(v):
    if v is None: return False
    try: return not math.isnan(float(v))
    except: return True

# ═══════════════════════════════════════════
# 第一板斧：看天 — 宏观+情绪+派发日
# ═══════════════════════════════════════════

def sky_check():
    """市场环境判定 — O'Neil状态机 + 养家情绪 + 宏观 (Context Builder DB优先)"""
    from market_state import oneil_state_machine, yangjia_emotion_cycle, calc_win_rate

    oneil = oneil_state_machine()
    yang = yangjia_emotion_cycle()
    win_rate = calc_win_rate()

    # 宏观 — Context Builder DB优先
    us10y = None; wti = None; shibor = None
    try:
        from context_builder import build_sky_context
        sky_ctx = build_sky_context()
        us10y = sky_ctx.get('us10y')
        wti = sky_ctx.get('wti')
        shibor = sky_ctx.get('shibor_on')
    except Exception:
        macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
        if not macro.empty:
            m = macro.iloc[0]
            us10y = m.get('us10y')
            wti = m.get('wti')
            shibor = m.get('shibor_on')

    # 风险列表
    risks = []
    if oneil['state'] == 'correction':
        risks.append(f"O'Neil: 下跌修正 — {oneil['action']}")
    elif oneil['state'] == 'uptrend_pressure':
        risks.append(f"O'Neil: 上升承压({oneil['distribution_days']}个派发日)")
    if oneil['below_ma60']:
        risks.append('沪深300 < 60日线 → 中期偏弱')
    if yang['stage'] in ('退潮', '冰点'):
        risks.append(f"养家: 情绪{yang['stage']} → {yang['action']}")
    if us10y and us10y > 4.5:
        risks.append(f'美10Y={us10y:.2f}% 高利率压制估值')
    if wti and wti > 108:
        risks.append(f'WTI=${wti:.0f} 突破琼斯警戒线$108')
    elif wti and wti > 100:
        risks.append(f'WTI=${wti:.0f} 接近琼斯警戒线$108')

    # 判定
    if oneil['state'] == 'confirmed_uptrend' and not risks:
        sky_level = '✅ 可以进场'
    elif len(risks) <= 2:
        sky_level = '⚠️ 谨慎进场'
    else:
        sky_level = '🔴 不宜进场'

    return {
        'oneil_state': oneil['label'],
        'oneil_action': oneil['action'],
        'yang_stage': yang['stage'],
        'yang_action': yang['action'],
        'win_rate': win_rate,
        'distribution_days': oneil['distribution_days'],
        'rally_day': oneil['rally_day'],
        'us10y': us10y, 'wti': wti, 'shibor_on': shibor,
        'emotion': yang['stage'],
        'emotion_score': yang['score'],
        'limit_up': yang['limit_up'],
        'limit_down': yang['limit_down'],
        'bomb_rate': yang['bomb_rate'],
        'sky_level': sky_level,
        'sky_risks': risks
    }

# ═══════════════════════════════════════════
# 第二板斧：看地 — 技术面+基本面+估值分位
# ═══════════════════════════════════════════

def earth_check(code):
    """个股技术+基本面 — DB优先 + AKShare降级"""
    from context_builder import build_earth_context
    from code_mapper import normalize_ts_code

    code = normalize_ts_code(code)
    # 优先从DB读取
    ctx = build_earth_context(code)
    result = {'name': code, 'code': code}

    if ctx.get('kline_count', 0) > 0:
        # DB有数据，直接使用
        for key in ['price', 'change_pct', 'vol_ratio', 'ma5', 'ma10', 'ma20', 'ma60',
                     'macd_dif', 'macd_dea', 'macd_hist', 'kdj_k', 'kdj_d', 'kdj_j',
                     'rsi6', 'rsi14', 'boll_upper', 'boll_mid', 'boll_lower',
                     'boll_position', 'ma_alignment', 'tech_score', 'tech_signals',
                     'roe', 'npm', 'gm', 'eps', 'debt_ratio', 'pe', 'pb',
                     'fund_score']:
            if key in ctx:
                result[key] = ctx[key]
        # freshness
        kline_fresh = ctx.get('fields', {}).get('kline', {})
        if kline_fresh.get('level') == 'expired':
            result['data_note'] = f"DB数据滞后{kline_fresh.get('lag_days')}天"
    else:
        # DB无数据，降级到AKShare实时查询
        result = _earth_fallback_akshare(code, result)

    # PE分位 — v8 σ标尺 9档分类 + 价值陷阱检测 + 历史回测胜率
    try:
        val = _q("SELECT pe_percentile_5y, pe_5y_min, pe_5y_max, pe_ttm FROM valuation_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", [code])
        if not val.empty:
            pct_raw = val.iloc[0].get('pe_percentile_5y')
            if pct_raw is not None and not (isinstance(pct_raw, float) and math.isnan(pct_raw)):
                pct = round(float(pct_raw), 1)
                result['pe_percentile'] = pct

                # v8: 9档σ标尺分类 (替代旧3档硬切)
                cls = _pe_sigma_classify(pct)
                result['pe_level'] = cls['标签']        # 向后兼容: 9档标签
                result['pe_sigma'] = cls['σ']           # z-score 字符串, 如 "+0.68σ"
                result['pe_z'] = cls['z_score']         # z-score 数值
                result['pe_suggestion'] = cls['建议']    # 档位操作建议

                # 历史60日胜率 — 联动DuckDB回测, N<30→null
                lo, hi = _pe_bucket_range(pct)
                result['pe_winrate_60d'] = _pe_backtest_winrate(code, lo, hi)

                # 价值陷阱检测 — PE<10%分位 + 盈利恶化 → 告警
                result['pe_trap'] = _pe_value_trap_check(code, pct)

                # 旧字段保留 (向后兼容)
                result['pe_1y_low'] = val.iloc[0].get('pe_5y_min')
                result['pe_1y_high'] = val.iloc[0].get('pe_5y_max')
                result['pe_ttm'] = val.iloc[0].get('pe_ttm')
    except Exception:
        pass

    return result


def _q(sql, params=None):
    return duckdb.connect(DB).execute(sql, params or []).fetchdf()


# ═══════════════════════════════════════════
# PE估值 σ标尺 9档分类 (v8 状态词分级改造)
# ═══════════════════════════════════════════

def _norm_ppf(p):
    """
    逆正态CDF (分位数函数) — 将百分位映射为 z-score。
    优先 scipy.stats.norm.ppf，不可用时降级为 Acklam 有理近似
    (最大绝对误差 < 1.5e-9, 来源: Peter Acklam, 2001)。

    输入 p ∈ (0, 1), 输出 z ∈ (-∞, +∞)。
    PE分位 / 100 → z-score, 表达"偏离历史中枢多少个标准差"。
    """
    if p <= 0.0 or p >= 1.0:
        return 0.0
    try:
        from scipy.stats import norm
        return norm.ppf(p)
    except ImportError:
        pass

    # Acklam 有理近似 — 两级: 中心区 (|q|≤0.425) 用低阶, 尾部用高阶
    a = [2.506628277459239, -18.61500062529, 41.39119773534, -25.44106049637]
    b = [-8.4735109309, 23.08336743743, -21.06224101826, 3.13082909833]
    c = [0.337475482272615, 0.976169019091719, 0.160797971491821,
         0.027643881033386, 0.003840572937361, 0.000395189651191, 0.000032176788177]
    d = [0.012774751053792, 0.028248906998931, 0.002752155235973,
         0.000140855840673, 0.000003399245587, 0.000000032560023]

    q = p - 0.5
    if abs(q) <= 0.425:
        # 中心区: 低阶多项式, R = 0.180625 - q²
        r = 0.180625 - q * q
        # num = a0 + a1*R + a2*R² + a3*R³ = (((a3*R + a2)*R + a1)*R + a0)
        num = ((a[3] * r + a[2]) * r + a[1]) * r + a[0]
        # den = 1 + b0*R + b1*R² + b2*R³ + b3*R⁴ = (((b3*R + b2)*R + b1)*R + b0)*R + 1
        den = (((b[3] * r + b[2]) * r + b[1]) * r + b[0]) * r + 1.0
        val = q * num / den
    else:
        # 尾部: 高阶多项式, R = sqrt(-ln(min(p, 1-p)))
        r = math.sqrt(-math.log(min(p, 1.0 - p)))
        # P(R) = c0 + c1*R + ... + c6*R⁶ = ((((((c6*R + c5)*R + c4)*R + c3)*R + c2)*R + c1)*R + c0)
        num = (((((c[6] * r + c[5]) * r + c[4]) * r + c[3]) * r + c[2]) * r + c[1]) * r + c[0]
        # Q(R) = 1 + d0*R + ... + d5*R⁶ = (((((d5*R + d4)*R + d3)*R + d2)*R + d1)*R + d0)*R + 1
        den = (((((d[5] * r + d[4]) * r + d[3]) * r + d[2]) * r + d[1]) * r + d[0]) * r + 1.0
        val = num / den
        if q < 0:
            val = -val
    return val


# σ → 分位区间 映射表
# 格式: (分位上限, σ下限, σ上限, 标签, 操作建议)
# 尾部加密 (3%/7%/15%/15%/20%/15%/15%/7%/3%) → 边际信息量大的区间更细
_PE_BUCKETS = [
    (3,    None, -2.0, '极端低估', '高胜率但罕见，需确认非价值陷阱'),
    (10,   -2.0, -1.0, '显著低估', '左侧布局区间，历史胜率较高'),
    (25,   -1.0, -0.5, '偏低估',   '适合定投/分批'),
    (40,   -0.5,  0.0, '合理偏低', '估值舒适区'),
    (60,    0.0,  0.0, '合理中枢', '估值中性，看其他维度'),
    (75,    0.0, +0.5, '合理偏高', '注意风险积累'),
    (90,   +0.5, +1.0, '偏高估',   '减仓预警区'),
    (97,   +1.0, +2.0, '显著高估', '强烈减仓信号'),
    (100,  +2.0, None, '极端高估', '泡沫区，清仓'),
]


def _pe_sigma_classify(pct):
    """
    PE分位 → 9档σ标尺分类。

    Args:
        pct: float, PE 5年分位 (0-100)

    Returns:
        dict: {'标签': str, 'σ': str, 'z_score': float, '建议': str, '分位': float}
    """
    z = _norm_ppf(pct / 100.0)

    for limit, lo, hi, label, suggestion in _PE_BUCKETS:
        if pct < limit or (limit == 100 and pct <= 100):
            return {
                '标签': label,
                'σ': f"{z:+.2f}σ",
                'z_score': round(z, 4),
                '建议': suggestion,
                '分位': pct,
            }

    # 防御值 (理论上不可达)
    return {
        '标签': '合理中枢', 'σ': f"{z:+.2f}σ",
        'z_score': round(z, 4), '建议': '估值中性', '分位': pct,
    }


def _pe_bucket_range(pct):
    """
    返回PE分位所在σ区间的分位下界和上界 [lower, upper)，
    用于回测时在 valuation_daily 中查询该区间的历史命中记录。
    """
    prev = 0.0
    for limit, lo, hi, label, suggestion in _PE_BUCKETS:
        if pct < limit or (limit == 100 and pct <= 100):
            return (prev, float(limit))
        prev = float(limit)
    return (0.0, 100.0)


def _pe_backtest_winrate(code, pct_lower, pct_upper):
    """
    回测: PE分位在 [pct_lower, pct_upper) 区间时，买入后60个交易日胜率。

    算法:
      1. valuation_daily 找出该股票PE分位落在目标区间的所有历史日期
      2. kline_daily 用 ROW_NUMBER 窗口函数前跳60日取退出价
      3. 统计正收益占比

    防御:
      - 命中次数 N < 30 → 返回 None (样本不足, 不可信)
      - N ≥ 30 → 返回 "XX% (N=YYY)"
      - DB异常 → 返回 None

    Returns:
        str | None
    """
    try:
        conn = duckdb.connect(DB)
        df = conn.execute("""
            WITH numbered AS (
                SELECT trade_date, close,
                       ROW_NUMBER() OVER (ORDER BY trade_date) AS rn
                FROM kline_daily
                WHERE ts_code = ?
            ),
            pe_dates AS (
                SELECT trade_date, pe_percentile_5y
                FROM valuation_daily
                WHERE ts_code = ?
                  AND pe_percentile_5y >= ?
                  AND pe_percentile_5y < ?
            )
            SELECT
                p.trade_date AS entry_date,
                k.close AS entry_price,
                f.close AS exit_price
            FROM pe_dates p
            JOIN numbered k ON k.trade_date = p.trade_date
            LEFT JOIN numbered f ON f.rn = k.rn + 60
            WHERE f.close IS NOT NULL
            ORDER BY p.trade_date
        """, [code, code, pct_lower, pct_upper]).fetchdf()
        conn.close()

        n = len(df)
        if n < 30:
            return None

        wins = int((df['exit_price'] > df['entry_price']).sum())
        wr = round(wins / n * 100)
        return f"{wr}% (N={n})"

    except Exception:
        return None


def _pe_value_trap_check(code, pct):
    """
    价值陷阱检测 — 双条件 AND 逻辑。

    前置条件: PE分位 < 10% (极端/显著低估区)
    触发条件 (任一满足即告警):
      (a) 最近两个季度归母净利润连续环比下滑 (Q0<Q1<Q2)
      (b) 最新季度 EPS 同比下修 >= 10%

    防御: 仅当 financial_statements 有 ≥3 条记录且字段非空时检测，
          数据不足 → 返回 None (不误报)。

    Returns:
        '可能为价值陷阱' | None
    """
    if pct >= 10.0:
        return None

    try:
        conn = duckdb.connect(DB)
        fin = conn.execute("""
            SELECT report_date, net_profit, eps, report_type
            FROM financial_statements
            WHERE ts_code = ?
              AND net_profit IS NOT NULL
            ORDER BY report_date DESC
            LIMIT 4
        """, [code]).fetchdf()
        conn.close()

        if fin.empty or len(fin) < 3:
            return None

        # (a) 连续两季净利润环比下滑
        np_vals = fin['net_profit'].dropna().tolist()
        if len(np_vals) >= 3:
            if np_vals[0] < np_vals[1] < np_vals[2]:
                return '可能为价值陷阱'

        # (b) EPS 同比下修 >= 10% (最近季度 vs 去年同期)
        eps_vals = fin['eps'].dropna().tolist()
        if len(eps_vals) >= 2 and eps_vals[1] > 0:
            eps_drop = (eps_vals[1] - eps_vals[0]) / eps_vals[1]
            if eps_drop >= 0.10:
                return '可能为价值陷阱'

        return None

    except Exception:
        return None


def _earth_fallback_akshare(code, result):
    """AKShare降级 — DB无数据时使用"""
    import akshare as ak
    end_d = date.today().strftime('%Y%m%d')
    start_d = (date.today() - timedelta(days=300)).strftime('%Y%m%d')
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start_d, end_date=end_d, adjust='qfq')
            break
        except:
            if attempt < 2: time.sleep(2.0)
            else:
                try: df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start_d, end_date=end_d, adjust='')
                except: pass

    if df is not None and not df.empty:
        close = df['收盘'].values; cur = close[-1]
        result['price'] = cur
        result['change_pct'] = df['涨跌幅'].iloc[-1] if '涨跌幅' in df.columns else 0
        # 自算基本指标
        result['ma5'] = float(np.mean(close[-5:])) if len(close)>=5 else None
        result['ma20'] = float(np.mean(close[-20:])) if len(close)>=20 else None
        ema12 = pd.Series(close).ewm(span=12).mean(); ema26 = pd.Series(close).ewm(span=26).mean()
        dif = (ema12-ema26).values; dea = pd.Series(dif).ewm(span=9).mean().values
        result['macd_dif'] = round(float(dif[-1]),4)
        result['macd_dea'] = round(float(dea[-1]),4)
        delta = pd.Series(close).diff()
        gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        ag14 = gain.rolling(14).mean().iloc[-1]; al14 = loss.rolling(14).mean().iloc[-1]
        result['rsi14'] = round(float(100-100/(1+ag14/al14)),1) if al14>0 else 100
        result['tech_score'] = 50
        result['tech_signals'] = ['AKShare实时']
        result['fund_score'] = 50
        result['data_note'] = 'DB无数据，AKShare实时查询'
    else:
        result['tech_score'] = 0
        result['tech_signals'] = ['数据获取失败(接口限流/非交易日)']
        result['fund_score'] = 50
        result['data_note'] = 'DB+AKShare均无数据'
    return result

# ═══════════════════════════════════════════
# 第三板斧：算账 — 压力测试+概率+点位
# ═══════════════════════════════════════════

def calc_verdict(sky, earth, plan_ctx=None):
    """综合判定：加/减/观望 + 点位 + 概率 + 数据质量"""

    # 1. 市场环境得分
    sky_ok = sky.get('sky_level', '').startswith('✅')
    sky_warn = sky.get('sky_level', '').startswith('⚠')

    # 数据质量检查 (Context Builder)
    if plan_ctx:
        dq = plan_ctx.get('data_quality', {})
    else:
        dq = {}

    # 2. 技术面得分
    tech_score = earth.get('tech_score', 50)

    # 3. 基本面得分
    fund_score = earth.get('fund_score', 50)

    # 4. PE分位 (v8: 连续z-score替代离散标签匹配)
    pe_level = earth.get('pe_level', '?')
    pe_z = earth.get('pe_z', 0.0)
    # 连续偏向判断 — 无硬切, 丝滑过渡
    pe_cheap = pe_z < -0.5       # z<-0.5σ → 分位 < ~31% → 偏低估方向
    pe_expensive = pe_z > 0.5    # z>+0.5σ → 分位 > ~69% → 偏高估方向
    pe_fair = not pe_cheap and not pe_expensive

    # 数据完整性检查
    data_missing = []
    if tech_score == 0:
        data_missing.append('技术面数据获取失败(接口限流/非交易日)')
    if fund_score == 50:
        data_missing.append('基本面数据不完整')

    # === 决策 ===
    action = '观望'
    reasons = []

    if data_missing:
        action = '数据不足'
        reasons = data_missing
        reasons.append('建议稍后重试或手动查看')
    elif sky_ok and tech_score >= 65 and fund_score >= 65:
        if pe_cheap or pe_fair:
            action = '加仓'
            reasons.append('市场OK + 技术偏强 + 基本面好 + 估值合理偏低')
        elif pe_expensive:
            action = '观望'
            reasons.append(f'基本面好但估值偏高({pe_level}, z={pe_z:+.2f})，等回调')
    elif not sky_ok and tech_score < 45:
        action = '减仓'
        reasons.append('市场不配合 + 技术偏弱')
    elif tech_score < 40:
        action = '减仓'
        reasons.append('技术面显著走弱')
    else:
        data_points = sum([sky_ok, tech_score >= 55, fund_score >= 55, pe_cheap or pe_fair])
        if data_points >= 3:
            action = '轻仓试多'
            reasons.append(f'{data_points}/4 条件满足，可小仓位试探')
        else:
            action = '观望'
            reasons.append(f'仅{data_points}/4 条件满足，等信号')

    # === 点位 ===
    price = earth.get('price', 0)
    levels = {}
    if price > 0:
        ma5 = earth.get('ma5') or price
        ma20 = earth.get('ma20') or price
        ma60 = earth.get('ma60') or price
        levels['建议买入'] = round(min(ma20, ma5), 2)  # 均线支撑位
        levels['止损价'] = round(price * 0.93, 2)  # -7%
        levels['加仓触发'] = round(price * 1.03, 2)  # 突破确认
        levels['阻力位'] = round(price * 1.08, 2)  # +8%
        if earth.get('boll_position') and earth['boll_position'] < 15:
            levels['提示'] = '目前在BOLL下轨，超跌区域，不宜在此处止损'

    # === 概率分布 ===
    # 基于情绪+技术综合估算
    emo_score = sky.get('emotion_score', 50)
    tech_sc = earth.get('tech_score', 50)

    # 简化贝叶斯
    bull = 0.30
    bear = 0.30
    if sky_ok: bull += 0.10
    if sky.get('dist_days', 0) == 0: bull += 0.05
    if tech_sc >= 65: bull += 0.10
    # v8: PE用连续z-score分梯度加分 (越便宜加越多, 替代旧单点+0.10)
    if pe_cheap: bull += 0.15
    elif pe_z < 0: bull += max(0, -pe_z * 0.10)  # z=-0.3→+0.03, z=-0.5→+0.05
    if tech_sc < 40: bear += 0.15
    if not sky_ok: bear += 0.10
    if pe_expensive: bear += 0.10                 # 高估增加看空权重

    total = bull + 0.35 + bear
    bull_p = round(bull / total * 100)
    neutral_p = round(0.35 / total * 100)
    bear_p = round(bear / total * 100)

    max_loss = round(price * (1 - 0.93) * 100, 0) if price > 0 else 0  # 单股最大亏损

    return {
        'action': action,
        'reasons': reasons,
        'levels': levels,
        'data_quality': dq,
        'probability': {
            '赚(>5%)': f'{bull_p}%',
            '平(±5%)': f'{neutral_p}%',
            '亏(>5%)': f'{bear_p}%',
            '单次最坏亏损': f'¥{max_loss}/股 ≈ 7%（止损线）'
        }
    }

# ═══════════════════════════════════════════
# 综合输出
# ═══════════════════════════════════════════

def analyze_stock(code):
    print("=" * 64)
    print(f"  天眼三板斧 · {code}")
    print("=" * 64)

    # 第一板斧
    print("\n🪓 【第一斧 · 看天】")
    sky = sky_check()
    print(f"  O'Neil: {sky.get('oneil_state','?')}  |  {sky.get('oneil_action','?')}")
    print(f"  养家情绪: {sky.get('yang_stage','?')}({sky.get('emotion_score','?')}分)  |  {sky.get('yang_action','?')}")
    print(f"  涨停{sky.get('limit_up','?')} 跌停{sky.get('limit_down','?')}  "
          f"派发日{sky.get('distribution_days','?')}个  "
          f"炸板率{sky.get('bomb_rate','?'):.0%}" if isinstance(sky.get('bomb_rate'), float) else f"炸板率{sky.get('bomb_rate','?')}")
    print(f"  美10Y: {sky.get('us10y','?')}%  WTI: ${sky.get('wti','?')}  SHIBOR: {sky.get('shibor_on','?')}%")
    print(f"  综合赢面: {sky.get('win_rate','?')}%")
    print(f"  判定: {sky['sky_level']}")
    if sky.get('sky_risks'):
        for r in sky['sky_risks']:
            print(f"    ⚠ {r}")

    # 第二板斧
    print("\n🪓 【第二斧 · 看地】")
    print(f"  实时查询 {code} ...")
    earth = earth_check(code)
    print(f"  价格: {earth.get('price','?')}  涨跌: {earth.get('change_pct','?')}%  量比: {earth.get('vol_ratio','?')}")
    print(f"  技术({earth.get('tech_score','?')}分): {', '.join(earth.get('tech_signals',[]))}")
    print(f"  均线: MA5={earth.get('ma5','?')} MA20={earth.get('ma20','?')} MA60={earth.get('ma60','?')}")
    print(f"  MACD: DIF={earth.get('macd_dif','?')} DEA={earth.get('macd_dea','?')}  RSI14={earth.get('rsi14','?')}")
    print(f"  BOLL位置: {earth.get('boll_position','?')}%")

    print(f"\n  基本面({earth.get('fund_score','?')}分):")
    print(f"  ROE={earth.get('roe','?')}%  净利率={earth.get('npm','?')}%  毛利率={earth.get('gm','?')}%")
    print(f"  PE={earth.get('pe','?')}  PB={earth.get('pb','?')}  资产负债率={earth.get('debt_ratio','?')}%")
    print(f"  营收YoY={earth.get('rev_growth','?')}%  利润YoY={earth.get('np_growth','?')}%")
    if earth.get('pe_level'):
        sigma_str = earth.get('pe_sigma', '')
        wr = earth.get('pe_winrate_60d')
        wr_str = f"  历史60日胜率: {wr}" if wr else "  历史60日胜率: 样本不足"
        trap = earth.get('pe_trap')
        trap_str = f"  ⚠️{trap}" if trap else ""
        print(f"  估值水位: {earth['pe_level']}({earth.get('pe_percentile','?')}%分位, {sigma_str})  PE={earth.get('pe_ttm','?')}  1年PE区间: {earth.get('pe_1y_low','?')}~{earth.get('pe_1y_high','?')}")
        print(f"  {wr_str}{trap_str}")
        if earth.get('pe_suggestion'):
            print(f"  档位建议: {earth['pe_suggestion']}")

    # 第三板斧
    print("\n🪓 【第三斧 · 算账】")
    try:
        from context_builder import build_plan_context
        plan_ctx = build_plan_context(sky, earth)
        verdict = calc_verdict(sky, earth, plan_ctx)
    except Exception:
        verdict = calc_verdict(sky, earth)
    if verdict.get('data_quality'):
        dq = verdict['data_quality']
        print(f"  数据质量: {dq.get('grade','?')} {len(dq.get('missing',[]))}缺失 {len(dq.get('expired',[]))}过期")
    action_map = {'加仓': '🟢', '减仓': '🔴', '数据不足': '🔵'}
    action_icon = action_map.get(verdict['action'],
        '🟡' if '试' in verdict['action'] else '⚪')
    print(f"\n  >>> 综合判定: {action_icon} {verdict['action']}")
    for r in verdict['reasons']:
        print(f"      {r}")

    print(f"\n  📍 点位建议:")
    for k, v in verdict['levels'].items():
        print(f"    {k}: {v}")

    print(f"\n  🎲 概率分布:")
    for k, v in verdict['probability'].items():
        print(f"    {k}: {v}")

    print(f"\n{'='*64}")
    return {'sky': sky, 'earth': earth, 'verdict': verdict}


if __name__ == '__main__':
    code = sys.argv[1] if len(sys.argv) > 1 else '600900'
    result = analyze_stock(code)
