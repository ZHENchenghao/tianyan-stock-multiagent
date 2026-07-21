# -*- coding: utf-8 -*-
"""
天眼 v9 · 新闻接线中枢 (News Hub)
==================================
串联5个现有新闻分析模块，产出统一 NewsVerdict 字典，供9维矩阵消费。

调用链:
  news_articles (DuckDB)
    ├── news_energy.py      → 消息能量模型 (E_total / 定价检测)
    ├── event_calendar.py   → 宏观事件扫描 + 情绪引擎 + 冲突溢价
    ├── nlp_surprise.py     → NLP预期差分析 (噪音过滤 + 超预期检测)
    ├── sentiment_gate.py   → 板块情绪风险 + 三重验证
    └── capital_flow_fingerprint.py → 资金流微观结构 (辅助flow_alert)

防御机制:
  - 今日新闻=0 → 返回安全中性默认值
  - 任一模块报错 → 该模块降级为None, 不影响其他模块
  - 所有异常不抛出, 记录到 degradations[]

用法:
  from engine.news_hub import daily_verdict
  nv = daily_verdict()
  # nv['dimension_score'] → 50±25, 直接注入 cross_validation_matrix
"""

import sys, os, json, math
from datetime import date, datetime, timedelta
from collections import defaultdict, Counter
from typing import Optional, Dict, List, Any

BASE = os.path.dirname(os.path.abspath(__file__))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'

# ── 持仓板块 → 代表指数映射 ──
SECTOR_INDEX_MAP = {
    '有色金属': 'sh000819',  '有色': 'sh000819',   '有色/商品': 'sh000819',
    '沪深300': 'sh000300',   '金融/地产': 'sh000300', '消费': 'sh000300',
    '电力指数': 'sz399438',  '电力': 'sz399438',    '新能源/电力': 'sz399438',
    '科创50': 'sh000688',   'AI/科技': 'sh000688',  '半导体': 'sh000688',
    '锂电池': 'sz399261',   '新能源车': 'sz399261',
    '中证电池': 'sh000849',
    '医药': 'sh000300',
    '宏观/政策': 'sh000300',
}

# ── 默认中性裁定 ──
NEUTRAL_VERDICT = {
    'date': '',
    'news_count': 0,
    'status': 'empty',
    'macro_shock': {'score': 0, 'level': 'normal', 'triggers': [], 'note': '今日无新闻'},
    'sector_energy': {'headlines': [], 'bullish_sectors': [], 'bearish_sectors': [],
                      'energy_map': {}, 'E_total_net': 0},
    'flow_alert': {'active': False, 'alerts': [], 'sectors_at_risk': []},
    'nlp_surprise': {'surprise_count': 0, 'bearish_surprises': 0, 'bullish_surprises': 0,
                     'flagged_events': []},
    'dimension_score': 50.0,
    'dimension_direction': 'neutral',
    'degradations': [],
    'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
}


# ═══════════════════════════════════════════
# 防御层: 安全包装器
# ═══════════════════════════════════════════

def _safe_call(module_name: str, fn, *args, **kwargs) -> tuple:
    """
    包装模块调用, 任何异常→返回(None, error_msg)。
    绝不抛异常到调用方。
    """
    try:
        result = fn(*args, **kwargs)
        return (result, None)
    except Exception as e:
        return (None, f'{module_name}.{fn.__name__} 失败: {str(e)[:120]}')


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

def daily_verdict(today_str: str = None, holdings: list = None) -> dict:
    """
    新闻接线中枢 · 每日统一裁定。

    Args:
        today_str: 日期 YYYY-MM-DD, 默认今天
        holdings: 持仓板块列表, e.g. ['有色金属','电力指数','科创50']
                  默认从 SECTOR_INDEX_MAP 取全部板块

    Returns:
        NewsVerdict dict, 始终包含 dimension_score 和 dimension_direction
    """
    if today_str is None:
        today_str = date.today().isoformat()
    if holdings is None:
        holdings = ['有色金属', '电力指数', '科创50', '沪深300', '锂电池']

    verdict = dict(NEUTRAL_VERDICT)
    verdict['date'] = today_str
    verdict['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    degradations = []

    # ━━━ 第0步: 读今日新闻 ━━━
    articles, db_err = _safe_call('DuckDB', _fetch_today_news, today_str)
    if db_err:
        degradations.append(db_err)
        verdict['degradations'] = degradations
        verdict['status'] = 'degraded'
        verdict['macro_shock']['note'] = f'新闻DB不可用: {db_err[:60]}'
        return verdict

    if not articles:
        verdict['status'] = 'empty'
        verdict['degradations'] = degradations
        return verdict

    verdict['news_count'] = len(articles)
    verdict['status'] = 'active'

    # ━━━ 第1步: 消息能量 (news_energy) ━━━
    energy_results, en_err = _safe_call('news_energy', _run_news_energy, articles, today_str)
    if en_err:
        degradations.append(en_err)
    else:
        verdict['sector_energy'] = energy_results

    # ━━━ 第2步: 宏观事件 + 情绪 (event_calendar) ━━━
    macro_result, ev_err = _safe_call('event_calendar', _run_event_calendar)
    if ev_err:
        degradations.append(ev_err)
    else:
        verdict['macro_shock'] = macro_result

    # ━━━ 第3步: NLP预期差 (nlp_surprise) ━━━
    nlp_result, nl_err = _safe_call('nlp_surprise', _run_nlp_surprise, articles)
    if nl_err:
        degradations.append(nl_err)
    else:
        verdict['nlp_surprise'] = nlp_result

    # ━━━ 第4步: 板块情绪风险 (sentiment_gate) ━━━
    flow_result, sg_err = _safe_call('sentiment_gate', _run_sentiment_gate, holdings)
    if sg_err:
        degradations.append(sg_err)
    else:
        verdict['flow_alert'] = flow_result

    # ━━━ 第5步: 综合 → dimension_score ━━━
    verdict['dimension_score'], verdict['dimension_direction'] = _compute_dimension_score(verdict)
    verdict['degradations'] = degradations

    # 降级标记
    if len(degradations) >= 3:
        verdict['status'] = 'heavily_degraded'
    elif degradations:
        verdict['status'] = 'partial'

    return verdict


# ═══════════════════════════════════════════
# 第0步: 数据源
# ═══════════════════════════════════════════

def _fetch_today_news(today_str: str) -> list:
    """从DuckDB读取今日新闻, 返回 [{title, content, source, sector_tags, ...}]"""
    import duckdb
    conn = duckdb.connect(DB, read_only=True)
    try:
        rows = conn.execute("""
            SELECT id, title, content, source, sector_tags, publish_date
            FROM news_articles
            WHERE publish_date = ?
            ORDER BY id DESC
        """, [today_str]).fetchall()
    finally:
        conn.close()

    articles = []
    for row in rows:
        articles.append({
            'id': row[0],
            'title': row[1] or '',
            'content': row[2] or '',
            'source': row[3] or '未知',
            'sector_tags': row[4] or '',
            'publish_date': str(row[5])[:10] if row[5] else today_str,
        })
    return articles


# ═══════════════════════════════════════════
# 第1步: 消息能量模型
# ═══════════════════════════════════════════

def _run_news_energy(articles: list, today_str: str) -> dict:
    """
    调用 news_energy.NewsEnergyCalculator 逐条分析,
    按板块聚合 E_total, 区分多空方向。
    """
    from engine.news_energy import NewsEnergyCalculator, _event_type_from_tags

    calc = NewsEnergyCalculator()

    # 板块聚合: {sector: {'bullish_E': 0, 'bearish_E': 0, 'headlines': []}}
    sector_acc = defaultdict(lambda: {'bullish_E': 0, 'bearish_E': 0,
                                        'neutral_E': 0, 'headlines': [], 'count': 0})
    all_headlines = []
    bullish_total = 0.0
    bearish_total = 0.0

    for art in articles:
        title = art['title']
        tags = art.get('sector_tags', '')

        # 能量分析
        try:
            energy = calc.analyze({
                'event_type': _event_type_from_tags(tags),
                'title': title,
                'content': art['content'][:1000],
                'source': art['source'],
                'publish_date': art['publish_date'],
            })
        except Exception:
            energy = {'E_total': 0, 'E_residual_adjusted': 0, 'reliability': 0.5,
                      'judgment': '未知', 'benchmark_desc': '未知'}

        E = energy.get('E_residual_adjusted', energy.get('E_total', 0))
        if isinstance(E, (int, float)):
            e_val = float(E)
        else:
            e_val = 0.0

        # 方向判定: 用标题关键词+事件类型
        direction = _detect_direction(title, tags)
        if direction == 'bullish':
            bullish_total += e_val
        elif direction == 'bearish':
            bearish_total += e_val

        # 按板块聚合
        if tags:
            for sector in str(tags).split(','):
                sector = sector.strip()
                if not sector:
                    continue
                acc = sector_acc[sector]
                acc['count'] += 1
                if direction == 'bullish':
                    acc['bullish_E'] += e_val
                elif direction == 'bearish':
                    acc['bearish_E'] += e_val
                else:
                    acc['neutral_E'] += e_val
                if len(acc['headlines']) < 5:
                    acc['headlines'].append(f'[{direction}] {title[:50]}')

        if len(all_headlines) < 10:
            all_headlines.append({
                'title': title[:60],
                'direction': direction,
                'E': round(e_val, 3),
                'source': art['source'],
            })

    # 板块偏多/偏空判定
    bullish_sectors = []
    bearish_sectors = []
    energy_map = {}
    for sector, acc in sector_acc.items():
        net = acc['bullish_E'] - acc['bearish_E']
        energy_map[sector] = round(net, 3)
        if net > 0.1:
            bullish_sectors.append(sector)
        elif net < -0.1:
            bearish_sectors.append(sector)

    E_total_net = bullish_total - bearish_total

    return {
        'headlines': all_headlines,
        'bullish_sectors': bullish_sectors,
        'bearish_sectors': bearish_sectors,
        'energy_map': energy_map,
        'E_total_net': round(E_total_net, 3),
        'bullish_E_total': round(bullish_total, 3),
        'bearish_E_total': round(bearish_total, 3),
        'sector_count': len(sector_acc),
        'source': 'news_energy.NewsEnergyCalculator',
    }


def _detect_direction(title: str, tags: str) -> str:
    """简易方向检测 — 标题关键词 + 板块标签"""
    t = (title + ' ' + tags).lower()

    bearish_words = ['跌', '暴跌', '崩', '危机', '暴雷', '违约', '制裁', '利空',
                     '减持', '处罚', '调查', '退市', '亏损', '暴亏', '造假',
                     '崩盘', '踩踏', '恐慌', '抛售', '跑路', '倒闭']
    bullish_words = ['涨', '暴涨', '突破', '利好', '增持', '回购', '买入',
                     '超预期', '放量', '新高', '翻倍', '涨停', '净买入',
                     '政策支持', '补贴', '放水', '降息', '复苏']

    bearish_hits = sum(1 for w in bearish_words if w in t)
    bullish_hits = sum(1 for w in bullish_words if w in t)

    if bearish_hits > bullish_hits:
        return 'bearish'
    elif bullish_hits > bearish_hits:
        return 'bullish'
    return 'neutral'


# ═══════════════════════════════════════════
# 第2步: 宏观事件 + 情绪引擎
# ═══════════════════════════════════════════

def _run_event_calendar() -> dict:
    """
    调用 event_calendar.EventScanner + SentimentEngine,
    产出宏观冲击分 (0-100) + 触发器列表。
    """
    from engine.event_calendar import EventScanner, SentimentEngine

    # 扫描未来事件
    scanner = EventScanner()
    events = scanner.scan()

    # 情绪评估
    sentiment = SentimentEngine()
    sent_result = sentiment.assess()

    # 统计高危事件
    triggers = []
    macro_score = 0

    for evt in events:
        direction = evt.get('direction_rules', {})
        impact = direction.get('impact', 'neutral')
        a_share = direction.get('a_share', 'neutral')

        if impact in ('bearish', 'high_volatility') or a_share in ('bearish', 'risk_off'):
            triggers.append({
                'date': str(evt.get('date', ''))[:10],
                'name': evt.get('name', evt.get('event_name', '?'))[:50],
                'impact': impact,
                'a_share': a_share,
            })
            macro_score += 15 if impact == 'high_volatility' else 10

    # VIX修正
    vix = sent_result.get('vix', 18)
    if vix and vix > 25:
        macro_score += 20
        triggers.append({'date': 'today', 'name': f'VIX={vix:.0f}(高度恐慌)',
                         'impact': 'bearish', 'a_share': 'risk_off'})
    elif vix and vix > 20:
        macro_score += 10

    macro_score = min(100, macro_score)

    level = 'normal'
    if macro_score >= 60:
        level = 'crisis'
    elif macro_score >= 30:
        level = 'elevated'

    return {
        'score': macro_score,
        'level': level,
        'triggers': triggers[:8],
        'event_count': len(events),
        'vix': vix,
        'vix_level': sent_result.get('vix_level', 'unknown'),
        'garch_sticky': sent_result.get('garch_sticky', False),
        'source': 'event_calendar.EventScanner + SentimentEngine',
    }


# ═══════════════════════════════════════════
# 第3步: NLP预期差
# ═══════════════════════════════════════════

def _run_nlp_surprise(articles: list) -> dict:
    """
    调用 nlp_surprise.analyze_surprise 逐条检测预期差。
    噪音过滤自动处理, 只统计 non-noise 的超预期事件。
    """
    from engine.nlp_surprise import analyze_surprise

    bullish_surprises = 0
    bearish_surprises = 0
    flagged = []

    # 取前20条做NLP分析 (控制计算量)
    for art in articles[:20]:
        # 使用板块代表代码
        tags = art.get('sector_tags', '')
        code = _sector_to_code(tags)

        try:
            result = analyze_surprise(
                code=code,
                title=art['title'],
                content=art['content'][:2000],
                source=art['source'],
            )
        except Exception:
            continue

        # 只关注有效信号 (噪音已自动过滤)
        if result.get('is_useful_lead_signal', 0) == 0:
            continue

        mag = result.get('surprise_magnitude', 0)
        direction = result.get('surprise_direction', 0)  # -1/0/+1

        if direction > 0:
            bullish_surprises += 1
        elif direction < 0:
            bearish_surprises += 1

        if abs(mag) > 0.5:
            flagged.append({
                'title': art['title'][:60],
                'direction': 'bullish' if direction > 0 else 'bearish',
                'magnitude': round(mag, 2),
                'event_type': result.get('event_type', '?')[:30],
                'note': result.get('analysis_note', '')[:80],
            })

    return {
        'surprise_count': bullish_surprises + bearish_surprises,
        'bearish_surprises': bearish_surprises,
        'bullish_surprises': bullish_surprises,
        'flagged_events': flagged[:8],
        'source': 'nlp_surprise.analyze_surprise',
    }


def _sector_to_code(tags: str) -> str:
    """板块标签→代表股票代码 (供NLP分析用)"""
    if not tags:
        return '000300'
    tag_list = str(tags).split(',')
    for t in tag_list:
        t = t.strip()
        if t in SECTOR_INDEX_MAP:
            idx = SECTOR_INDEX_MAP[t]
            # 指数→个股映射
            stock_map = {'sh000819': '000819', 'sh000300': '000300',
                         'sz399438': '399438', 'sh000688': '000688',
                         'sz399261': '399261', 'sh000849': '000849'}
            return stock_map.get(idx, '000300')
    return '000300'


# ═══════════════════════════════════════════
# 第4步: 板块情绪 + 资金流预警
# ═══════════════════════════════════════════

def _run_sentiment_gate(holdings: list) -> dict:
    """
    调用 sentiment_gate.sector_emotional_check 逐板块检测,
    产出 flow_alert (是否情绪化、是否过热/恐慌)。
    """
    from engine.sentiment_gate import sector_emotional_check

    alerts = []
    sectors_at_risk = []

    for sector in holdings:
        idx_code = SECTOR_INDEX_MAP.get(sector, 'sh000300')
        try:
            check = sector_emotional_check(sector, idx_code)
        except Exception:
            continue

        if check.get('is_emotional'):
            sectors_at_risk.append(sector)
            flags = check.get('flags', [])
            for flag in flags:
                alerts.append({
                    'sector': sector,
                    'alert': flag,
                    'action': check.get('action', '观望'),
                })

    return {
        'active': len(alerts) > 0,
        'alerts': alerts[:10],
        'sectors_at_risk': sectors_at_risk,
        'source': 'sentiment_gate.sector_emotional_check',
    }


# ═══════════════════════════════════════════
# 第5步: 综合 → dimension_score
# ═══════════════════════════════════════════

def _compute_dimension_score(verdict: dict) -> tuple:
    """
    将 macro_shock + sector_energy + nlp_surprise + flow_alert
    综合为一个 0-100 的维度分数, 用于注入9维矩阵。

    权重:
      macro_shock:  40%  (宏观冲击→扣分)
      sector_energy: 30%  (板块多空→加分/扣分)
      nlp_surprise: 20%  (预期差→扣分/加分)
      flow_alert:   10%  (情绪化→扣分)

    → 50=中性, >50=偏多(无利空冲击), <50=偏空(有利空堆积)
    """
    # 1. macro_shock: score越高越差 → 扣分
    ms = verdict['macro_shock'].get('score', 0)
    macro_component = -ms * 0.4  # score=50 → -20

    # 2. sector_energy: E_total_net正=偏多
    se = verdict['sector_energy']
    E_net = se.get('E_total_net', 0)
    # 映射: E_net ∈ [-2, +2] → [-30, +30]
    energy_component = max(-30, min(30, E_net * 15))

    # 3. nlp_surprise: 超预期偏多 vs 不及预期偏空
    nl = verdict['nlp_surprise']
    bull = nl.get('bullish_surprises', 0)
    bear = nl.get('bearish_surprises', 0)
    surprise_net = bull - bear
    nl_component = max(-20, min(20, surprise_net * 5))

    # 4. flow_alert: 有情绪化→扣分
    fa = verdict['flow_alert']
    flow_penalty = -10 if fa.get('active') else 0

    total = 50.0 + macro_component + energy_component + nl_component + flow_penalty
    total = max(5.0, min(95.0, total))

    direction = 'bullish' if total > 55 else ('bearish' if total < 45 else 'neutral')

    return round(total, 1), direction


# ═══════════════════════════════════════════
# CLI 自检
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    print('=' * 60)
    print('  天眼 v9 · 新闻接线中枢 自检')
    print('=' * 60)

    nv = daily_verdict()

    print(f"\n状态: {nv['status']}")
    print(f"日期: {nv['date']}  新闻条数: {nv['news_count']}")
    print(f"降级模块: {len(nv['degradations'])}")
    for d in nv['degradations']:
        print(f"  ⚠ {d}")

    print(f"\n── 宏观冲击 ──")
    ms = nv['macro_shock']
    print(f"  分数: {ms['score']}  等级: {ms['level']}  触发事件: {len(ms.get('triggers',[]))}")

    print(f"\n── 板块能量 ──")
    se = nv['sector_energy']
    print(f"  E_total_net: {se['E_total_net']}")
    print(f"  偏多板块: {se['bullish_sectors']}")
    print(f"  偏空板块: {se['bearish_sectors']}")

    print(f"\n── NLP预期差 ──")
    nl = nv['nlp_surprise']
    print(f"  超预期: 多{nl['bullish_surprises']} 空{nl['bearish_surprises']}")
    print(f"  高置信事件: {len(nl['flagged_events'])}")

    print(f"\n── 资金流预警 ──")
    fa = nv['flow_alert']
    print(f"  预警: {'是' if fa['active'] else '否'}  风险板块: {fa['sectors_at_risk']}")

    print(f"\n── 综合 ──")
    print(f"  dimension_score: {nv['dimension_score']}")
    print(f"  dimension_direction: {nv['dimension_direction']}")
    print(f"  完整keys: {list(nv.keys())}")
