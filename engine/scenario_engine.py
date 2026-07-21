"""天眼推演引擎 — 六条法则落地
法则1 预期差 · 法则2 四象限 · 法则3 百分位
法则4 概率推演 · 法则5 压力测试 · 法则6 贝叶斯校准
"""
import duckdb, json, os, sys, math
from datetime import date, timedelta

DB = 'D:/FreeFinanceData/data/duckdb/finance.db'
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTFOLIO_FILE = os.path.join(BASE, 'portfolio.json')

def q(sql, params=None):
    return duckdb.connect(DB).execute(sql, params or []).fetchdf()

def ok(v):
    """值有效（非None非NaN）"""
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (TypeError, ValueError):
        return True

# ═══════════════════════════════════════════
# 法则1：预期差分析
# 比较市场共识预期 vs 实际数据，识别定价偏差
# ═══════════════════════════════════════════

def expectation_gap():
    """预期差：实际数据 vs 市场隐含预期"""
    gaps = []

    # 宏观预期差：美10Y vs 市场隐含利率预期
    macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
    if not macro.empty:
        m = macro.iloc[0]
        us10y = m.get('us10y')
        if ok(us10y):
            # 简单预期差：当前值 vs 20日均值（代表近期市场定价）
            hist = q("SELECT AVG(us10y) as avg_10y FROM macro_indicators WHERE trade_date >= CURRENT_DATE - 20")
            if not hist.empty and ok(hist.iloc[0]['avg_10y']):
                avg = hist.iloc[0]['avg_10y']
                gap_bp = (us10y - avg) * 100
                direction = '鹰派超预期' if gap_bp > 10 else ('鸽派超预期' if gap_bp < -10 else '符合预期')
                gaps.append({'指标': '美10Y预期差', '当前': f'{us10y:.3f}%', '20日均': f'{avg:.3f}%',
                              '偏差bp': round(gap_bp, 1), '判定': direction})

    # 情绪预期差
    sentiment = q("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT 1")
    if not sentiment.empty:
        s = sentiment.iloc[0]
        score = s.get('emotion_score', 50)
        hist_emo = q("SELECT AVG(emotion_score) as avg_score FROM market_sentiment WHERE trade_date >= CURRENT_DATE - 20")
        if not hist_emo.empty and ok(hist_emo.iloc[0]['avg_score']):
            avg_score = hist_emo.iloc[0]['avg_score']
            emo_gap = score - avg_score
            direction = '情绪超涨' if emo_gap > 15 else ('情绪超跌' if emo_gap < -15 else '在预期内')
            gaps.append({'指标': '情绪预期差', '当前': f'{score:.0f}', '20日均': f'{avg_score:.0f}',
                          '偏差': round(emo_gap, 1), '判定': direction})

    # 估值预期差：PE分位 vs 历史中位
    val = q("SELECT AVG(pe_ttm) as avg_pe FROM valuation_daily WHERE trade_date = (SELECT MAX(trade_date) FROM valuation_daily)")
    if not val.empty and ok(val.iloc[0]['avg_pe']):
        avg_pe = val.iloc[0]['avg_pe']
        pe_hist = q("SELECT AVG(pe_ttm) as hist_pe FROM kline_daily WHERE trade_date >= CURRENT_DATE - INTERVAL 1 YEAR")
        if not pe_hist.empty and ok(pe_hist.iloc[0]['hist_pe']):
            hist_pe = pe_hist.iloc[0]['hist_pe']
            gap_pct = (avg_pe - hist_pe) / hist_pe * 100 if hist_pe else 0
            direction = '估值偏高' if gap_pct > 20 else ('估值偏低' if gap_pct < -20 else '合理区间')
            gaps.append({'指标': '全市场PE预期差', '当前': f'{avg_pe:.1f}', '年均': f'{hist_pe:.1f}',
                          '偏差%': round(gap_pct, 1), '判定': direction})

    return {'count': len(gaps), 'gaps': gaps}

# ═══════════════════════════════════════════
# 法则2：四象限分类
# 成长-价值 × 动量-质量 → 四象限定位
# ═══════════════════════════════════════════

def four_quadrant():
    """四象限：市场风格定位 + 板块分布"""
    # 取市场数据判断当前处于哪个象限
    macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
    sentiment = q("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT 1")

    # 维度1：宏观环境 → 成长/价值倾向
    # 美10Y下行 + 流动性宽松 → 成长；美10Y上行 + 流动性收紧 → 价值
    macro_axis = 'neutral'
    if not macro.empty:
        m = macro.iloc[0]
        us10y = m.get('us10y') or 4.0
        us10y_hist = q("SELECT AVG(us10y) as avg FROM macro_indicators WHERE trade_date >= CURRENT_DATE - 60")
        avg = us10y_hist.iloc[0]['avg'] if not us10y_hist.empty and us10y_hist.iloc[0]['avg'] else us10y
        if us10y < avg - 0.2:
            macro_axis = 'growth'  # 利率下行，利好成长
        elif us10y > avg + 0.2:
            macro_axis = 'value'   # 利率上行，利好价值

    # 维度2：市场情绪 → 动量/质量倾向
    # 主升/高潮 → 动量；退潮/冰点 → 质量防御
    sentiment_axis = 'neutral'
    if not sentiment.empty:
        s = sentiment.iloc[0]
        emo = s.get('market_emotion', '')
        if emo in ('主升', '高潮'):
            sentiment_axis = 'momentum'
        elif emo in ('退潮', '冰点'):
            sentiment_axis = 'quality'

    # 四象限判定
    quadrant_map = {
        ('growth', 'momentum'):  {'name': '第一象限 · 成长动量',  'style': '进攻', 'desc': '高成长+强趋势，适用动能策略'},
        ('growth', 'quality'):   {'name': '第二象限 · 成长质量',  'style': '攻守兼备', 'desc': '成长股中选质量，GARP策略'},
        ('value', 'momentum'):   {'name': '第三象限 · 价值动量',  'style': '反转进攻', 'desc': '价值修复+趋势确认，反转策略'},
        ('value', 'quality'):    {'name': '第四象限 · 价值质量',  'style': '防御', 'desc': '低估值+高质量，深度价值策略'},
        ('growth', 'neutral'):   {'name': '成长偏中', 'style': '偏进攻', 'desc': '宏观支持成长，情绪中性'},
        ('value', 'neutral'):    {'name': '价值偏中', 'style': '偏防御', 'desc': '宏观压制成长，情绪中性'},
        ('neutral', 'momentum'): {'name': '中性与动量', 'style': '跟趋势', 'desc': '宏观中性，情绪驱动动量'},
        ('neutral', 'quality'):  {'name': '中性与质量', 'style': '守势', 'desc': '宏观中性，情绪偏防御'},
        ('neutral', 'neutral'):  {'name': '四象限均衡', 'style': '观望', 'desc': '两维度均无明确方向'},
    }
    result = quadrant_map.get((macro_axis, sentiment_axis), quadrant_map[('neutral', 'neutral')])

    return {
        'macro_axis': macro_axis,
        'sentiment_axis': sentiment_axis,
        'quadrant': result['name'],
        'style': result['style'],
        'desc': result['desc']
    }

# ═══════════════════════════════════════════
# 法则3：百分位排名
# 关键指标当前值在历史分布中的位置
# ═══════════════════════════════════════════

def percentile_rank():
    """百分位：关键指标在历史中的位置"""
    rankings = []

    # 1. 全市场PE分位
    pe = q("""
        SELECT AVG(pe_ttm) as pe FROM valuation_daily
        WHERE trade_date = (SELECT MAX(trade_date) FROM valuation_daily)
    """)
    if not pe.empty and ok(pe.iloc[0]['pe']):
        cur_pe = pe.iloc[0]['pe']
        pe_hist = q(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pe_ttm <= {cur_pe} THEN 1 ELSE 0 END) as below
            FROM kline_daily
            WHERE trade_date >= CURRENT_DATE - INTERVAL 3 YEAR AND pe_ttm > 0
        """)
        if not pe_hist.empty:
            t, b = pe_hist.iloc[0]['total'], pe_hist.iloc[0]['below']
            pct = round(b / t * 100, 1) if t > 0 else 50
            level = '低估' if pct < 25 else ('高估' if pct > 75 else '合理')
            rankings.append({'指标': '全市场PE', '当前': f'{cur_pe:.1f}', '分位': f'{pct}%', '水位': level})

    # 2. 情绪分位
    emo = q("SELECT emotion_score FROM market_sentiment ORDER BY trade_date DESC LIMIT 1")
    if not emo.empty:
        cur_emo = emo.iloc[0]['emotion_score']
        if ok(cur_emo):
            emo_hist = q(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN emotion_score <= {cur_emo} THEN 1 ELSE 0 END) as below
                FROM market_sentiment WHERE trade_date >= CURRENT_DATE - INTERVAL 1 YEAR
            """)
            if not emo_hist.empty:
                t, b = emo_hist.iloc[0]['total'], emo_hist.iloc[0]['below']
                pct = round(b / t * 100, 1) if t > 0 else 50
                level = '亢奋区' if pct > 80 else ('冰点区' if pct < 20 else '中性')
                rankings.append({'指标': '市场情绪', '当前': f'{cur_emo:.0f}', '分位': f'{pct}%', '水位': level})

    # 3. 美10Y分位
    macro = q("SELECT us10y FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
    if not macro.empty and ok(macro.iloc[0]['us10y']):
        cur_y = macro.iloc[0]['us10y']
        y_hist = q(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN us10y <= {cur_y} THEN 1 ELSE 0 END) as below
            FROM macro_indicators WHERE us10y IS NOT NULL
        """)
        if not y_hist.empty:
            t, b = y_hist.iloc[0]['total'], y_hist.iloc[0]['below']
            pct = round(b / t * 100, 1) if t > 0 else 50
            level = '高利率环境' if pct > 75 else ('低利率环境' if pct < 25 else '正常')
            rankings.append({'指标': '美10Y', '当前': f'{cur_y:.3f}%', '分位': f'{pct}%', '水位': level})

    # 4. 派发日分位
    dist = q("""
        SELECT COUNT(*) as cnt FROM kline_daily
        WHERE ts_code='000300.SH' AND trade_date >= CURRENT_DATE - INTERVAL 28 DAY AND change_pct < -1.5
    """)
    if not dist.empty:
        cur_dist = int(dist.iloc[0]['cnt'])
        dist_hist = q("""
            WITH weekly AS (
                SELECT DATE_TRUNC('week', trade_date) as wk,
                       SUM(CASE WHEN change_pct < -1.5 THEN 1 ELSE 0 END) as dist
                FROM kline_daily WHERE ts_code='000300.SH'
                GROUP BY wk
            )
            SELECT COUNT(*) as total, SUM(CASE WHEN dist <= {} THEN 1 ELSE 0 END) as below FROM weekly
        """.format(cur_dist))
        if not dist_hist.empty:
            t, b = dist_hist.iloc[0]['total'], dist_hist.iloc[0]['below']
            pct = round(b / t * 100, 1) if t > 0 else 50
            level = '高压区' if pct > 70 else ('安全区' if pct < 30 else '正常')
            rankings.append({'指标': '派发日(4周)', '当前': f'{cur_dist}个', '分位': f'{pct}%', '水位': level})

    return {'count': len(rankings), 'rankings': rankings}

# ═══════════════════════════════════════════
# 法则4：概率推演
# 基于当前状态 → 三种情景 + 概率权重
# ═══════════════════════════════════════════

def scenario_probability():
    """概率推演：三种情景的概率估计"""
    sentiment = q("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT 1")
    macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
    perc = percentile_rank()

    # 基础概率：基于情绪状态
    base_bull, base_neutral, base_bear = 0.33, 0.34, 0.33
    if not sentiment.empty:
        s = sentiment.iloc[0]
        emo = s.get('market_emotion', '')
        score = s.get('emotion_score', 50)
        if emo == '主升':
            base_bull, base_neutral, base_bear = 0.50, 0.30, 0.20
        elif emo == '高潮':
            base_bull, base_neutral, base_bear = 0.35, 0.30, 0.35  # 高潮后易反转
        elif emo == '启动':
            base_bull, base_neutral, base_bear = 0.45, 0.35, 0.20
        elif emo == '退潮':
            base_bull, base_neutral, base_bear = 0.15, 0.35, 0.50
        elif emo == '冰点':
            base_bull, base_neutral, base_bear = 0.30, 0.30, 0.40  # 冰点后反弹概率不低

    # 宏观修正
    if not macro.empty:
        m = macro.iloc[0]
        wti = m.get('wti')
        if ok(wti) and wti > 108:
            base_bear += 0.15
            base_bull -= 0.10
        elif wti < 70:
            base_bull += 0.10

    # 百分位修正
    for r in perc.get('rankings', []):
        if r['指标'] == '市场情绪' and r['水位'] == '亢奋区':
            base_bear += 0.05
            base_bull -= 0.05
        if r['指标'] == '派发日(4周)' and r['水位'] == '高压区':
            base_bear += 0.10
            base_bull -= 0.05

    # 归一化
    total = base_bull + base_neutral + base_bear
    bull_p = round(base_bull / total * 100, 1)
    neutral_p = round(base_neutral / total * 100, 1)
    bear_p = round(base_bear / total * 100, 1)

    return {
        'bull': {'概率': f'{bull_p}%', '触发条件': '成交量放大+涨停>100+主线板块领涨',
                  '仓位': '70-100%', '标的': '主线龙头+趋势模板通过股'},
        'neutral': {'概率': f'{neutral_p}%', '触发条件': '缩量震荡+板块轮动+无明确方向',
                     '仓位': '30-50%', '标的': '低位质优股+高股息防御'},
        'bear': {'概率': f'{bear_p}%', '触发条件': '派发日>5或跌停>80或WTI急升',
                  '仓位': '0-30%', '标的': '现金+国债逆回购+黄金ETF'}
    }

# ═══════════════════════════════════════════
# 法则5：压力测试
# 极端情景下的组合损失测算
# ═══════════════════════════════════════════

def stress_test(portfolio_file=None):
    """压力测试：极端情景组合损失测算"""
    pf_file = portfolio_file or PORTFOLIO_FILE
    portfolio = {'total_capital': 100000, 'positions': []}
    if os.path.exists(pf_file):
        try:
            with open(pf_file, 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
        except:
            pass

    total_cap = portfolio.get('total_capital', 100000)
    positions = portfolio.get('positions', [])

    # 五个极端场景
    scenarios = [
        {'name': '流动性冲击（升息100bp）', 'market_drop': -0.20, 'duration_days': 30,
         'desc': '美10Y急升100bp，全球资产重定价，新兴市场资金外流'},
        {'name': '信用事件（地产/城投爆雷）', 'market_drop': -0.25, 'duration_days': 15,
         'desc': '信用违约引发流动性危机，金融板块领跌，波及全市场'},
        {'name': '外部冲击（地缘/脱钩升级）', 'market_drop': -0.18, 'duration_days': 20,
         'desc': '关税/制裁升级，出口链重创，北向资金大幅流出'},
        {'name': '系统性熊市（指数跌30%）', 'market_drop': -0.30, 'duration_days': 90,
         'desc': '全面熊市，所有板块下跌，仅有现金和债券正收益'},
        {'name': '流动性枯竭（千股跌停）', 'market_drop': -0.35, 'duration_days': 5,
         'desc': '极端流动性危机，涨跌停限制导致无法平仓，实际损失可能更大'},
    ]

    results = []
    max_dd = -0.10  # 硬约束
    for sc in scenarios:
        # 简化的组合损失 = 市值仓位 × 市场跌幅 × β
        equity_exposure = 0.60  # 假设60%权益仓位
        if positions:
            invested = sum(p.get('market_value', 0) for p in positions)
            equity_exposure = invested / total_cap if total_cap > 0 else 0.60

        loss_pct = equity_exposure * abs(sc['market_drop']) * 1.1  # β≈1.1
        loss_amount = total_cap * loss_pct
        within_limit = loss_pct <= abs(max_dd)
        action = '风控范围内' if within_limit else '⚠ 触发组合回撤上限!需减仓/对冲'

        # 测试个股止损
        stop_hits = 0
        for p in positions:
            entry = p.get('entry_price', 0)
            current = p.get('current_price', entry)
            if entry > 0:
                crash_price = current * (1 + sc['market_drop'])  # 注意market_drop为负
                if crash_price < entry * 0.93:  # 跌破止损位
                    stop_hits += 1

        results.append({
            '场景': sc['name'], '市场跌幅': f'{sc["market_drop"]:.0%}',
            '组合损失': f'{loss_pct:.1%}', '损失金额': f'¥{loss_amount:,.0f}',
            '是否超标': '✅安全' if within_limit else '🔴超标',
            '止损触发': f'{stop_hits}/{len(positions)}只' if positions else '无持仓',
            '应对': action, '场景描述': sc['desc'],
            '恢复时间': f'{sc["duration_days"]}天'
        })

    return {
        '组合规模': f'¥{total_cap:,.0f}',
        '假设权益仓位': f'{equity_exposure:.0%}',
        '组合回撤硬约束': f'{max_dd:.0%}',
        '场景数': len(results),
        'scenarios': results
    }

# ═══════════════════════════════════════════
# 法则6：贝叶斯校准
# 先验概率 → 新证据 → 后验概率（持续更新）
# ═══════════════════════════════════════════

def bayesian_update(prior_bull=None, prior_bear=None):
    """贝叶斯校准：用新证据更新情景概率"""
    # 先验：从概率推演获取
    if prior_bull is None:
        probs = scenario_probability()
        prior_bull = float(probs['bull']['概率'].rstrip('%')) / 100
        prior_bear = float(probs['bear']['概率'].rstrip('%')) / 100

    # 新证据：从最新市场数据获取
    sentiment = q("SELECT * FROM market_sentiment ORDER BY trade_date DESC LIMIT 1")
    macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")

    evidence_bull = 1.0
    evidence_bear = 1.0
    items = []

    # 证据1：情绪状态
    if not sentiment.empty:
        s = sentiment.iloc[0]
        emo = s.get('market_emotion', '')
        # P(E|Bull) / P(E|Bear)：主升在牛市中更常见，冰点在熊市中更常见
        likelihood = {
            '主升': (3.0, 0.3),
            '高潮': (1.5, 0.5),
            '启动': (2.5, 0.4),
            '退潮': (0.3, 3.0),
            '冰点': (0.5, 2.0),
        }
        l_bull, l_bear = likelihood.get(emo, (1.0, 1.0))
        evidence_bull *= l_bull
        evidence_bear *= l_bear
        items.append({'证据': f'情绪={emo}', '牛市似然': l_bull, '熊市似然': l_bear})

    # 证据2：派发日
    dist = q("""
        SELECT COUNT(*) as cnt FROM kline_daily
        WHERE ts_code='000300.SH' AND trade_date >= CURRENT_DATE - INTERVAL 28 DAY AND change_pct < -1.5
    """)
    if not dist.empty:
        d = int(dist.iloc[0]['cnt'])
        if d >= 5:
            evidence_bull *= 0.3
            evidence_bear *= 2.5
            items.append({'证据': f'派发日={d}(≥5)', '牛市似然': 0.3, '熊市似然': 2.5})
        elif d >= 3:
            evidence_bull *= 0.6
            evidence_bear *= 1.5
            items.append({'证据': f'派发日={d}(3-4)', '牛市似然': 0.6, '熊市似然': 1.5})

    # 证据3：宏观
    if not macro.empty:
        m = macro.iloc[0]
        wti = m.get('wti')
        if wti and wti > 108:
            evidence_bull *= 0.4
            evidence_bear *= 2.0
            items.append({'证据': f'WTI=${wti:.0f}>$108', '牛市似然': 0.4, '熊市似然': 2.0})

    # 贝叶斯更新公式: P(H|E) = P(E|H) * P(H) / P(E)
    posterior_bull = evidence_bull * prior_bull
    posterior_bear = evidence_bear * prior_bear
    posterior_neutral = 1.0 * (1 - prior_bull - prior_bear)  # 中性似然=1.0
    total = posterior_bull + posterior_bear + posterior_neutral

    post_bull = posterior_bull / total
    post_bear = posterior_bear / total
    post_neutral = posterior_neutral / total

    # 更新方向
    b_delta = post_bull - prior_bull
    direction = '↑ 看多信心增强' if b_delta > 0.03 else ('↓ 看空信心增强' if b_delta < -0.03 else '→ 维持先验判断')

    return {
        'prior': {'bull': f'{prior_bull:.1%}', 'neutral': f'{1-prior_bull-prior_bear:.1%}', 'bear': f'{prior_bear:.1%}'},
        'evidence': items,
        'posterior': {'bull': f'{post_bull:.1%}', 'neutral': f'{post_neutral:.1%}', 'bear': f'{post_bear:.1%}'},
        'delta_bull': f'{b_delta:+.1%}',
        'direction': direction
    }

# ═══════════════════════════════════════════
# 综合输出：情景推演报告 cmd_plan()
# ═══════════════════════════════════════════

def generate_plan_report():
    """情景推演完整报告"""
    print("=" * 64)
    print("  天眼推演引擎 · 六大法则全景")
    print("=" * 64)

    # —— 法则3 百分位 ——
    perc = percentile_rank()
    print("\n📊 【法则3 · 百分位排名】")
    for r in perc['rankings']:
        print(f"  {r['指标']}: {r['当前']} → 分位 {r['分位']} [{r['水位']}]")

    # —— 法则2 四象限 ——
    quad = four_quadrant()
    print(f"\n🧭 【法则2 · 四象限】")
    print(f"  宏观轴: {quad['macro_axis']}  情绪轴: {quad['sentiment_axis']}")
    print(f"  当前象限: {quad['quadrant']}")
    print(f"  风格倾向: {quad['style']} — {quad['desc']}")

    # —— 法则1 预期差 ——
    gap = expectation_gap()
    print(f"\n🎯 【法则1 · 预期差】({gap['count']}项)")
    for g in gap['gaps']:
        print(f"  {g['指标']}: {g.get('判定','')}")

    # —— 法则4 概率推演 ——
    prob = scenario_probability()
    print(f"\n🎲 【法则4 · 概率推演】")
    for key, label in [('bull','情景A·乐观'), ('neutral','情景B·中性'), ('bear','情景C·悲观')]:
        p = prob[key]
        print(f"  {label}: {p['概率']} → 仓位{p['仓位']}, {p['标的']}")
        print(f"    触发: {p['触发条件']}")

    # —— 法则5 压力测试 ——
    stress = stress_test()
    print(f"\n💥 【法则5 · 压力测试】组合{stress['组合规模']} 权益仓位{stress['假设权益仓位']}")
    for s in stress['scenarios']:
        print(f"  {s['场景']}: 亏{s['组合损失']}({s['损失金额']}) {s['是否超标']} | {s['应对']}")

    # —— 法则6 贝叶斯校准 ——
    bayes = bayesian_update()
    print(f"\n📐 【法则6 · 贝叶斯校准】")
    print(f"  先验: 牛{bayes['prior']['bull']} 中{bayes['prior']['neutral']} 熊{bayes['prior']['bear']}")
    print(f"  后验: 牛{bayes['posterior']['bull']} 中{bayes['posterior']['neutral']} 熊{bayes['posterior']['bear']}")
    print(f"  更新: Δ{bayes['delta_bull']} {bayes['direction']}")
    if bayes['evidence']:
        for e in bayes['evidence']:
            print(f"    {e['证据']}: L(牛)={e['牛市似然']} L(熊)={e['熊市似然']}")

    # —— 综合 ——
    print(f"\n{'='*64}")
    print("  综合判定")
    print(f"{'='*64}")
    print(f"  象限定位: {quad['quadrant']} → {quad['style']}")
    print(f"  乐观概率: {prob['bull']['概率']}  悲观概率: {prob['bear']['概率']}")
    print(f"  贝叶斯方向: {bayes['direction']}")

    return {
        'quadrant': quad, 'percentile': perc, 'expectation_gap': gap,
        'scenario': prob, 'stress_test': stress, 'bayesian': bayes
    }

# ═══════════════════════════════════════════
# 综合输出：组合追踪报告 cmd_trace()
# ═══════════════════════════════════════════

def generate_trace_report(portfolio_file=None):
    """组合追踪完整报告"""
    pf_file = portfolio_file or PORTFOLIO_FILE
    print("=" * 64)
    print("  天眼组合追踪 · 贝叶斯动态校准")
    print("=" * 64)

    # 读取组合
    portfolio = {'total_capital': 100000, 'positions': [], 'watchlist': []}
    if os.path.exists(pf_file):
        try:
            with open(pf_file, 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
        except:
            print("  ⚠ portfolio.json 读取失败，使用默认值")

    total_cap = portfolio.get('total_capital', 100000)
    positions = portfolio.get('positions', [])

    # 持仓明细
    print(f"\n📌 持仓明细 (总资金 ¥{total_cap:,.0f})")
    if positions:
        print(f"  {'代码':<10s} {'名称':<8s} {'现价':>8s} {'成本':>8s} {'盈亏%':>8s} {'市值':>10s} {'信号':<8s}")
        print(f"  {'-'*62}")
        invested = 0
        for p in positions:
            ts = p.get('ts_code', '?')
            name = p.get('name', '?')
            cur = p.get('current_price', 0)
            entry = p.get('entry_price', cur)
            shares = p.get('shares', 0)
            mv = cur * shares
            pnl = (cur - entry) / entry * 100 if entry > 0 else 0
            invested += mv
            signal = '持有' if pnl > -3 else ('⚠ 近止损' if pnl > -7 else '🔴 止损!')
            print(f"  {ts:<10s} {name:<8s} ¥{cur:>7.2f} ¥{entry:>7.2f} {pnl:>+7.1f}% ¥{mv:>9,.0f} {signal}")
        cash = total_cap - invested
        print(f"\n  已投资: ¥{invested:,.0f} ({invested/total_cap*100:.1f}%)  现金: ¥{cash:,.0f} ({cash/total_cap*100:.1f}%)")
    else:
        print("  (暂无持仓)")

    # 贝叶斯校准
    bayes = bayesian_update()
    print(f"\n📐 【贝叶斯动态校准】")
    print(f"  先验 → 后验: 牛{bayes['prior']['bull']}→{bayes['posterior']['bull']}  熊{bayes['prior']['bear']}→{bayes['posterior']['bear']}")
    print(f"  方向: {bayes['direction']}")

    # 仓位建议（调用五级裁决链）
    print(f"\n⚖️ 【仓位建议】")
    try:
        sys.path.insert(0, os.path.join(BASE, 'engine'))
        from position_calculator import calculate_position
        pos = calculate_position(pf_file)
    except:
        # 内联简化仓位计算
        print("  (使用简化仓位计算)")
        bp = float(bayes['posterior']['bull'].rstrip('%')) / 100
        cap = 1.0 if bp > 0.5 else (0.5 if bp > 0.3 else 0.3)
        print(f"  基于后验看多概率 {bp:.1%} → 仓位上限 {cap:.0%}")

    # 异动提醒
    print(f"\n⚠️ 【异动提醒】")
    alerts = []
    if positions:
        for p in positions:
            entry = p.get('entry_price', 0)
            cur = p.get('current_price', 0)
            if entry > 0 and cur < entry * 0.95:
                alerts.append(f"🔴 {p.get('ts_code','?')} 距止损仅{(cur/entry-1)*100:+.1f}%")
            if entry > 0 and cur < entry * 0.97:
                alerts.append(f"🟡 {p.get('ts_code','?')} 浮亏{(cur/entry-1)*100:+.1f}% 关注")
    # 宏观预警
    macro = q("SELECT * FROM macro_indicators ORDER BY trade_date DESC LIMIT 1")
    if not macro.empty:
        m = macro.iloc[0]
        if m.get('wti') and m['wti'] > 100:
            alerts.append(f'⚠ WTI=${m["wti"]:.0f} 接近琼斯警戒线$108')
    if not alerts:
        alerts.append('✅ 无异常信号')
    for a in alerts:
        print(f"  {a}")

    print(f"\n{'='*64}")
    return {'portfolio': portfolio, 'bayesian': bayes, 'alerts': alerts}


# ═══════════════════════════════════════════
# 第四刀: 贝叶斯学习回路
# 每日预测 → 实际验证 → 修正先验
# ═══════════════════════════════════════════

BAYES_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bayesian_state.json')


def bayesian_loop_update():
    """
    贝叶斯学习回路 (第四刀)。

    每天运行:
      1. 读取昨天的后验概率
      2. 对比今天实际市场方向
      3. 计算预测误差
      4. 修正今天的先验
      5. 写入状态文件供明天使用
      6. 连续5天误差>30% → 标记模型失准

    Returns:
        dict with prior, posterior, error, alert
    """
    today = date.today()

    # ── 1. 读取昨日状态 ──
    state = {'last_date': None, 'posterior_bull': 0.35, 'posterior_neutral': 0.30,
             'posterior_bear': 0.35, 'error_streak': 0, 'history': []}
    if os.path.exists(BAYES_STATE_FILE):
        try:
            with open(BAYES_STATE_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                state.update(saved)
        except:
            pass

    # ── 2. 获取今天实际市场方向 ──
    actual_direction = 'neutral'
    actual_return = 0.0
    try:
        hs300 = q("SELECT close, change_pct FROM kline_daily WHERE ts_code='sh000300' "
                  "ORDER BY trade_date DESC LIMIT 2").fetchall() if duckdb else None
        # 降级: 用现有q函数
        rows = q("SELECT close FROM kline_daily WHERE ts_code='sh000300' "
                 "ORDER BY trade_date DESC LIMIT 2")
        if rows is not None and len(rows) >= 2:
            today_close = rows.iloc[0]['close'] if hasattr(rows, 'iloc') else rows[0][0]
            yesterday_close = rows.iloc[1]['close'] if hasattr(rows, 'iloc') else rows[1][0]
            if today_close and yesterday_close:
                actual_return = (float(today_close) / float(yesterday_close) - 1)
                if actual_return > 0.005:
                    actual_direction = 'bull'
                elif actual_return < -0.005:
                    actual_direction = 'bear'
                else:
                    actual_direction = 'neutral'
    except:
        pass

    # ── 3. 运行今日贝叶斯更新 ──
    yesterday_state = state
    prior_bull = yesterday_state.get('posterior_bull', 0.35)
    prior_bear = yesterday_state.get('posterior_bear', 0.35)
    prior_neutral = 1.0 - prior_bull - prior_bear

    try:
        today_bayes = bayesian_update(prior_bull=prior_bull, prior_bear=prior_bear)
    except:
        today_bayes = bayesian_update()

    # ── 4. 计算预测误差 ──
    if yesterday_state.get('last_date'):
        yesterday_bull = yesterday_state.get('posterior_bull', 0.35)
        yesterday_bear = yesterday_state.get('posterior_bear', 0.35)
        if yesterday_bull > yesterday_bear + 0.10:
            predicted_direction = 'bull'
        elif yesterday_bear > yesterday_bull + 0.10:
            predicted_direction = 'bear'
        else:
            predicted_direction = 'neutral'

        if predicted_direction == actual_direction:
            error = 0.0
            state['error_streak'] = 0
        elif predicted_direction == 'neutral':
            error = 0.5
        else:
            error = 1.0
            state['error_streak'] = state.get('error_streak', 0) + 1
    else:
        error = 0.0
        predicted_direction = 'unknown'

    # ── 5. 认知熔断 (v2.0): 一次打脸 → 信用清零 → 后天50/50 ──
    posterior = today_bayes.get('posterior', {})
    post_bull = float(str(posterior.get('bull', '35%')).rstrip('%')) / 100
    post_bear = float(str(posterior.get('bear', '35%')).rstrip('%')) / 100

    if error == 1.0:
        # 方向完全预测反了 → 认知熔断: 后验崩塌
        shame_factor = 0.5 / max(post_bull, post_bear, 0.3)
        post_bull = max(0.33, post_bull * shame_factor)
        post_bear = max(0.33, post_bear * shame_factor)
        total = post_bull + post_bear
        post_bull = round(post_bull / total * 0.5, 3)
        post_bear = round(post_bear / total * 0.5, 3)
        meltdown = True
    elif state.get('error_streak', 0) >= 3 and error > 0:
        post_bull = 0.33; post_bear = 0.33
        meltdown = True
    elif error == 0.0 and (state.get('meltdown') or state.get('posterior_neutral', 0) > 0.40):
        # 恢复: 错误归零 → 每天释放20%neutral到有方向的后验
        prev_neutral = state.get('posterior_neutral', 0.45)
        release = min(0.20, prev_neutral - 0.20)  # 释放量: 最多到20%底线
        if release > 0:
            post_bull = round(post_bull + release * (post_bull / max(post_bull + post_bear, 0.01)), 3)
            post_bear = round(post_bear + release * (post_bear / max(post_bull + post_bear, 0.01)), 3)
        meltdown = False
        state['error_streak'] = max(0, state.get('error_streak', 0) - 1)
    else:
        meltdown = state.get('meltdown', False)

    post_neutral = round(1.0 - post_bull - post_bear, 3)

    state['last_date'] = today.isoformat()
    state['prior_bull'] = round(prior_bull, 3)
    state['prior_bear'] = round(prior_bear, 3)
    state['posterior_bull'] = round(post_bull, 3)
    state['posterior_bear'] = round(post_bear, 3)
    state['posterior_neutral'] = post_neutral
    state['predicted_direction'] = predicted_direction
    state['actual_direction'] = actual_direction
    state['actual_return'] = round(actual_return, 4)
    state['error'] = error
    state['meltdown'] = meltdown
    state['bayes_direction'] = today_bayes.get('direction', '?')

    # 历史记录 (保留最近60条)
    state['history'].append({
        'date': today.isoformat(),
        'prior_bull': round(prior_bull, 3),
        'posterior_bull': round(post_bull, 3),
        'predicted': predicted_direction,
        'actual': actual_direction,
        'error': error,
    })
    if len(state['history']) > 60:
        state['history'] = state['history'][-60:]

    # 保存
    with open(BAYES_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 6. 告警 ──
    alert = None
    if state['error_streak'] >= 5:
        alert = {
            'level': '🔴',
            'msg': f'贝叶斯模型连续{state["error_streak"]}天预测错误 → 模型可能失准, 暂停依赖概率推演',
            'action': '概率推演降级为"方向参考", 不给精确百分比'
        }
    elif state['error_streak'] >= 3:
        alert = {
            'level': '🟡',
            'msg': f'贝叶斯连续{state["error_streak"]}天预测错误 → 关注',
            'action': '暂维持, 但提高人工审查频率'
        }

    return {
        'date': today.isoformat(),
        'prior': {'bull': f'{prior_bull:.1%}', 'neutral': f'{prior_neutral:.1%}', 'bear': f'{prior_bear:.1%}'},
        'bayes_output': today_bayes,
        'posterior': state['posterior_bull'],
        'predicted': predicted_direction,
        'actual': actual_direction,
        'actual_return': f'{actual_return:+.2%}',
        'error': error,
        'error_streak': state['error_streak'],
        'alert': alert,
        'state_file': BAYES_STATE_FILE,
    }


# ═══════════════════════════════════════════
# CLI入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'plan'
    if cmd == 'plan':
        result = generate_plan_report()
    elif cmd == 'trace':
        pf = sys.argv[2] if len(sys.argv) > 2 else None
        result = generate_trace_report(pf)
    elif cmd == 'stress':
        result = stress_test()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'bayes':
        result = bayesian_update()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'percentile':
        result = percentile_rank()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'quadrant':
        result = four_quadrant()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'gap':
        result = expectation_gap()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'json':
        # 全量JSON输出（供外部调用）
        result = {
            'percentile': percentile_rank(),
            'quadrant': four_quadrant(),
            'expectation_gap': expectation_gap(),
            'scenario_probability': scenario_probability(),
            'stress_test': stress_test(),
            'bayesian_update': bayesian_update()
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("用法: scenario_engine.py [plan|trace|stress|bayes|percentile|quadrant|gap|json|vpin|crowding]")


# ═══════════════════════════════════════════
# v1.1 增强 (2026-07-17): VPIN闸门 + 机构拥挤
# 从AgentQuant集成 — 多情景概率的前置过滤器
# ═══════════════════════════════════════════

def vpin_gate():
    """
    VPIN闸门 — 知情交易概率过滤。
    高VPIN→知情交易活跃→市场方向被少数参与者主导→情景推演需降权。
    返回: {vpin_est, gate, note}
    """
    con = duckdb.connect(DB, read_only=True)
    today = con.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
    rows = con.execute("""
        SELECT AVG(ABS(pct_chg)/NULLIF(turnover_rate,0)) FROM kline_daily
        WHERE trade_date=? AND turnover_rate>0
    """, [str(today)]).fetchone()
    con.close()

    vpin = float(rows[0]) if rows and rows[0] else 0
    if vpin > 5:
        gate = 'RED'
        note = f'VPIN={vpin:.1f}(>5→高度知情交易)→多情景概率置信度打五折, 情景推演标注"高不确定性"'
    elif vpin > 2:
        gate = 'YELLOW'
        note = f'VPIN={vpin:.1f}(2-5→知情交易偏高)→情景推演标注"注意信息不对称"'
    else:
        gate = 'GREEN'
        note = f'VPIN={vpin:.1f}(<2→噪音交易为主)→情景推演正常权重'

    return {'date': str(today), 'vpin_estimate': round(vpin, 2), 'gate': gate, 'note': note}


def institutional_crowding():
    """
    机构拥挤度 — 前50大市值集中度 + 北向/融资同步性。
    同向操作N周以上→拥挤瓦解风险上升→熊市场景概率加权。
    """
    con = duckdb.connect(DB, read_only=True)
    today = con.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
    today = str(today)

    # 市值集中度
    try:
        top50 = con.execute("""
            SELECT SUM(total_mv) FROM (SELECT total_mv FROM kline_daily
            WHERE trade_date=? ORDER BY total_mv DESC LIMIT 50)
        """, [today]).fetchone()
        total = con.execute("SELECT SUM(total_mv) FROM kline_daily WHERE trade_date=?", [today]).fetchone()
        conc = float(top50[0]) / float(total[0]) if top50 and total and top50[0] and total[0] else 0
    except:
        conc = 0

    # 北向/融资同步周数
    try:
        nb = con.execute("""
            SELECT trade_date, net_flow FROM lab_northbound_daily
            WHERE trade_date <= ? AND net_flow IS NOT NULL ORDER BY trade_date DESC LIMIT 20
        """, [today]).fetchall()
        mg = con.execute("""
            SELECT trade_date, margin_balance FROM margin_trading
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 20
        """, [today]).fetchall()
        nb_dates = {str(r[0]): float(r[1]) for r in nb}
        mg_dates = {str(r[0]): float(r[1]) for r in mg}

        sync_weeks = 0
        for i in range(min(len(nb), len(mg))):
            nb_v = float(nb[i][1]) if i < len(nb) else 0
            mg_v = float(mg[i][1]) if i < len(mg) else 0
            mg_prev = float(mg[i+1][1]) if i+1 < len(mg) and mg[i+1][1] else mg_v
            nb_dir = 1 if nb_v > 0 else -1
            mg_dir = 1 if mg_v > mg_prev else -1
            if nb_dir == mg_dir:
                sync_weeks += 1
            else:
                break
    except:
        sync_weeks = 0

    con.close()

    crowding_level = 'LOW'
    if conc > 0.45 and sync_weeks >= 3:
        crowding_level = 'HIGH'
        note = f'前50占{conc:.0%}+北向融资同步{sync_weeks}天→高拥挤警惕瓦解; 熊市/震荡场景概率+15%'
    elif conc > 0.40:
        crowding_level = 'MEDIUM'
        note = f'前50占{conc:.0%}(偏高)→中性场景降权, 尾部风险升'
    else:
        note = f'集中度{conc:.0%}(正常)+同步{sync_weeks}天→无拥挤警报'

    return {'date': today, 'top50_concentration': round(conc, 2), 'nb_margin_sync_days': sync_weeks,
            'crowding_level': crowding_level, 'note': note}
