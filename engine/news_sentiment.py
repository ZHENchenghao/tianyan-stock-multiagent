# -*- coding: utf-8 -*-
"""
天眼新闻期望模块 · News Impact Sentiment
=========================================
每条重大新闻 → 方向(利多/利空) + 期望幅度 + 置信度 + 影响标的/板块 + 衰减曲线

不依赖个股匹配。映射到板块/指数/ETF层。
输出到 news_sentiment 表, 侦探/日报/纸交直接读。
"""
import re, json, os, sys
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict
import numpy as np

try:
    import duckdb
except: pass

DB = 'D:/FreeFinanceData/data/duckdb/finance.db'
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ═══════════════════════════════════════
# 新闻→板块→影响映射规则
# ═══════════════════════════════════════

IMPACT_RULES = [
    # (关键词正则, 方向, 幅度%, 置信度, 影响板块, 衰减天数, 机制说明)
    # IPO/供给冲击
    ('长鑫|CXMT|存储.*IPO|DRAM.*上市', 'bearish', -3.0, 0.75, '科创50/半导体', 30,
     '存储龙头上市→科创板供给冲击→科创50成分股资金分流'),
    ('DeepSeek|深度求索.*上市|深度求索.*IPO|AI.*龙头.*IPO', 'bearish', -2.5, 0.65, '科创50/AI', 30,
     'AI巨头IPO→科创板资金虹吸→短期抽血但长期提升指数质量'),
    ('IPO.*重启|巨无霸.*上市|募资.*百亿|首发.*受理|上市委.*通过', 'bearish', -1.5, 0.55, '全市场', 15,
     '新股供给增加→二级市场流动性承压'),

    # 金融工程事故
    ('杠杆.*爆仓|强制平仓|margin call|死亡螺旋|杠杆ETF.*踩踏', 'bearish', -5.0, 0.80, '全市场/科创50', 7,
     '杠杆资金强制出清→踩踏式下跌→短期恐慌但快速修复'),
    ('熔断|暴跌.*熔断|KOSPI.*熔断|韩国.*熔断', 'bearish', -3.0, 0.70, '半导体/科创50', 5,
     '海外熔断→A股量化因子触发→被动砸盘→次日大概率V反'),

    # 国家队/政策
    ('汇金.*增持|国家队.*入市|平准基金|央企.*回购.*百亿', 'bullish', +2.5, 0.75, '全市场/金融', 20,
     '国家队入场→政策底确认→市场信心修复→蓝筹领涨'),
    ('降息|降准|LPR.*下调|MLF.*下调|逆回购.*利率.*下调', 'bullish', +1.5, 0.65, '全市场/地产', 15,
     '货币宽松→流动性改善→估值提升→利率敏感板块最先受益'),
    ('消费.*规划|刺激.*消费|补贴.*消费|消费券', 'bullish', +1.5, 0.60, '消费/汽车/家电', 20,
     '消费刺激政策→需求端利好→消费板块估值修复'),

    # 地缘/制裁
    ('关税.*新增|出口管制.*半导体|实体清单.*新增|制裁.*中国', 'bearish', -2.0, 0.65, '半导体/出口', 15,
     '地缘政治升级→供应链脱钩→出口板块重定价→国产替代利好对冲'),
    ('海峡.*关闭|霍尔木兹.*袭击|原油.*暴涨.*地缘', 'bearish', -2.0, 0.70, '全市场/航空/化工', 10,
     '地缘冲突→油价急涨→通胀预期→新兴市场资金外流'),

    # 半导体周期
    ('存储.*涨价|DRAM.*涨价|NAND.*涨价|HBM.*涨价', 'bullish', +2.0, 0.60, '半导体/科创50', 15,
     '存储涨价→半导体周期上行→芯片板块盈利改善'),
    ('芯片.*过剩|存储.*过剩|半导体.*库存.*高', 'bearish', -2.0, 0.60, '半导体/科创50', 20,
     '产能过剩→价格下行→半导体周期见顶'),

    # 监管
    ('监管.*约谈|立案.*调查|处罚.*信息披露|暂停.*上市', 'bearish', -3.0, 0.55, '被监管标的/同行业', 10,
     '监管事件→行业风险重定价→合规成本上升'),
]

# 板块→ETF/指数映射
SECTOR_MAP = {
    '科创50': ['sh000688', '588000'],
    '半导体': ['sh000685', '512480'],
    '全市场': ['sh000300', '510300'],
    '金融': ['sz399975', '510050'],
    '消费': ['sh000814', '159928'],
    '地产': ['sz399967', '512200'],
    'AI': ['sz399967', '159819'],
    '航空': ['sz399967', '159766'],
    '出口': ['sh000300', '510300'],
    '汽车': ['sh000941', '516110'],
    '家电': ['sh000990', '159996'],
}


def score_news_impact(title, content='', source=''):
    """评分单条新闻 → (direction, magnitude_pct, confidence, sectors, decay_days, mechanism)"""
    text = f'{title} {content or ""}'[:2000]

    for kw_pattern, direction, magnitude, confidence, sectors, decay, mechanism in IMPACT_RULES:
        try:
            if re.search(kw_pattern, text, re.IGNORECASE):
                return direction, magnitude, confidence, sectors, decay, mechanism
        except re.error:
            # 降级为简单字符串匹配
            for kw in kw_pattern.split('|'):
                kw_clean = kw.replace('.*', '')
                if kw_clean in text:
                    return direction, magnitude, confidence, sectors, decay, mechanism
    return None


def process_daily_news(date_str=None):
    """处理当日新闻 → 聚合板块级利多利空期望"""
    if date_str is None:
        date_str = str(date.today())

    conn = duckdb.connect(DB)

    # 确保sentiment表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_sentiment (
            id INTEGER PRIMARY KEY,
            news_id INTEGER,
            date TEXT,
            direction TEXT,
            magnitude REAL,
            confidence REAL,
            sectors TEXT,
            decay_days INTEGER,
            mechanism TEXT,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 读取今日高优先级新闻
    try:
        rows = conn.execute("""
            SELECT n.id, n.title, n.content, n.source
            FROM news_articles n
            INNER JOIN news_impacts i ON n.id = i.news_id
            WHERE n.publish_date = ? AND i.priority <= 2
            ORDER BY i.priority
        """, [date_str]).fetchall()
    except:
        # news_impacts表可能不存在, 降级到直接扫新闻
        rows = conn.execute("""
            SELECT id, title, content, source
            FROM news_articles WHERE publish_date = ?
            ORDER BY id DESC LIMIT 200
        """, [date_str]).fetchall()

    sentiments = []
    for news_id, title, content, source in rows:
        result = score_news_impact(title, content or '', source)
        if result:
            direction, magnitude, confidence, sectors, decay, mechanism = result
            # 查重
            exists = conn.execute(
                "SELECT COUNT(*) FROM news_sentiment WHERE news_id=? AND date=?",
                [news_id, date_str]
            ).fetchone()[0]
            if not exists:
                conn.execute("""
                    INSERT INTO news_sentiment (news_id, date, direction, magnitude, confidence, sectors, decay_days, mechanism, title)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, [news_id, date_str, direction, magnitude, confidence, sectors, decay, mechanism, title[:200]])
                sentiments.append({
                    'news_id': news_id, 'direction': direction,
                    'magnitude': magnitude, 'confidence': confidence,
                    'sectors': sectors, 'decay': decay, 'mechanism': mechanism,
                    'title': title[:100]
                })

    conn.close()
    return sentiments


def aggregate_sentiment(date_str=None):
    """聚合当日情绪 → 板块级多空期望"""
    if date_str is None:
        date_str = str(date.today())

    conn = duckdb.connect(DB, read_only=True)
    try:
        rows = conn.execute("""
            SELECT direction, magnitude, confidence, sectors, mechanism, title
            FROM news_sentiment WHERE date = ?
        """, [date_str]).fetchall()
    except:
        conn.close()
        return None
    conn.close()

    if not rows:
        return None

    # 按板块聚合
    sector_impact = defaultdict(lambda: {'bullish': 0, 'bearish': 0, 'details': []})
    for direction, magnitude, confidence, sectors, mechanism, title in rows:
        for sec in sectors.split('/'):
            sec = sec.strip()
            impact = abs(magnitude) * confidence
            if direction == 'bullish':
                sector_impact[sec]['bullish'] += impact
            else:
                sector_impact[sec]['bearish'] += impact
            sector_impact[sec]['details'].append({
                'direction': direction, 'magnitude': magnitude,
                'confidence': confidence, 'mechanism': mechanism[:80],
                'title': title[:60] if title else ''
            })

    # Overall market sentiment
    total_bull = sum(v['bullish'] for v in sector_impact.values())
    total_bear = sum(v['bearish'] for v in sector_impact.values())
    net_score = total_bull - total_bear
    if net_score > 0.5:
        overall = 'bullish'
    elif net_score < -0.5:
        overall = 'bearish'
    else:
        overall = 'neutral'

    return {
        'date': date_str,
        'overall': overall,
        'net_score': round(net_score, 2),
        'total_bull': round(total_bull, 2),
        'total_bear': round(total_bear, 2),
        'n_events': len(rows),
        'sector_impact': {k: {
            'bull': round(v['bullish'], 2),
            'bear': round(v['bearish'], 2),
            'net': round(v['bullish'] - v['bearish'], 2),
            'details': v['details'][:3]
        } for k, v in sorted(sector_impact.items(), key=lambda x: abs(x[1]['bullish']-x[1]['bearish']), reverse=True)}
    }


def sentiment_report_section(date_str=None):
    """生成新闻情绪段 → 日报渲染"""
    if date_str is None:
        date_str = str(date.today())

    agg = aggregate_sentiment(date_str)
    if not agg:
        return '> ⚠️ 新闻情绪: 今日无重大事件或news_sentiment表为空(需先跑新闻采集)'

    lines = ['## 📰 新闻情绪期望 · News Impact Sentiment']
    overall = agg['overall']
    emoji = {'bullish': '🟢 偏多', 'bearish': '🔴 偏空', 'neutral': '⚪ 中性'}.get(overall, '?')
    lines.append(f'> 总体: {emoji} | 净期望+{agg[\"net_score\"]:.1f} | {agg[\"n_events\"]}个重大事件')
    lines.append('')

    lines.append('| 板块 | 利多期望 | 利空期望 | 净期望 | 关键事件 |')
    lines.append('|------|:--:|:--:|:--:|------|')
    for sec, imp in agg['sector_impact'].items():
        b = imp['bull']; be = imp['bear']; net = imp['net']
        tag = '🟢' if net > 0.3 else ('🔴' if net < -0.3 else '⚪')
        events = '; '.join([f'{d["direction"][:4]}: {d["title"][:40]}' for d in imp['details'][:2]])
        lines.append(f'| {sec} | +{b:.1f} | -{be:.1f} | {tag} {net:+.1f} | {events[:80]} |')

    lines.append('')
    lines.append(f'> 算法: 每条新闻→方向(利多/利空)×期望幅度×置信度→按板块聚合→净期望。正向=板块受益, 负向=板块承压。')

    return '\n'.join(lines)


if __name__ == '__main__':
    import sys, io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except: pass

    # 处理今日新闻
    sentiments = process_daily_news()
    print(f'处理完成: {len(sentiments)}条情绪标记')
    for s in sentiments[:8]:
        dr = s['direction']; mag = s['magnitude']; conf = s['confidence']
        mech = s['mechanism'][:60]
        print(f'  [{dr}] {mag:+.1f}% conf={conf:.0%} | {mech}')

    # 聚合
    agg = aggregate_sentiment()
    if agg:
        print(f'\n情绪聚合: {agg[\"overall\"]} net={agg[\"net_score\"]:.1f}')
        for sec, imp in agg['sector_impact'].items():
            print(f'  {sec}: +{imp[\"bull\"]:.1f}/-{imp[\"bear\"]:.1f} net={imp[\"net\"]:+.1f}')

    print('\n' + sentiment_report_section())
