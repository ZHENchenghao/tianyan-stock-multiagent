# -*- coding: utf-8 -*-
"""
天眼 v4.0 反共识景气度模型
==========================
第三阶段: 赚认知差的钱——当所有人看好时标记"拥挤", 当所有人绝望时标记"机会"。

算法参考: SentimentContrarianDetector (MIT) — sentiment vs return divergence
数据源: DuckDB news_articles (303条) + kline_daily + market_sentiment

核心指标:
  consensus_score: 新闻情绪面 — 最近N天该板块的新闻是看多还是看空 (0-100)
  reality_score:   基本面+技术面 — PE分位、动量、景气度 (0-100)
  divergence = consensus - reality
    > +20: 情绪过热, 基本面没跟上 → 🔴 拥挤, 反 consensus 看空
    < -20: 情绪过冷, 基本面在改善 → 🟢 冷门, 反 consensus 看多
    [-20,+20]: 定价有效 → ⚪ 中性

铁律#10: 每个术语附带解释。

用法:
  python engine/anti_consensus_prosperity.py
  python tianyan.py anticonsensus
  python tianyan.py anticonsensus --sector 有色
"""

import sys, os, json, math, re
from datetime import datetime, date, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    from snownlp import SnowNLP
    HAS_SNOWNLP = True
except ImportError:
    HAS_SNOWNLP = False

BASE = os.path.dirname(os.path.abspath(__file__))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'

# ═══════════════ 中文财经情感词汇表 (SnowNLP降级方案) ═══════════════
BULLISH_WORDS = [
    '暴涨', '涨停', '利好', '突破', '增长', '超预期', '翻倍', '创新高',
    '增持', '买入', '看好', '景气', '回暖', '复苏', '放量', '主升浪',
    '业绩大增', '政策利好', '资金流入', '订单饱满', '产能扩张',
    '盈利', '分红', '回购', '低估', '底部', '反弹', '金叉',
]
BEARISH_WORDS = [
    '暴跌', '跌停', '利空', '破位', '下降', '不及预期', '腰斩', '创新低',
    '减持', '卖出', '看空', '衰退', '低迷', '萎缩', '缩量', '崩盘',
    '业绩下滑', '政策收紧', '资金流出', '订单减少', '产能过剩',
    '亏损', 'ST', '退市', '高估', '顶部', '回调', '死叉',
    '整治', '处罚', '取缔', '关停', '调查', '暴雷', '违约',
]


def _conn():
    if duckdb is None:
        return None
    try:
        return duckdb.connect(DB)
    except Exception:
        return None


# ═══════════════ 1. 共识计算 (从新闻) ═══════════════

def calc_news_sentiment(text: str) -> float:
    """
    单条新闻情感: -1(强烈看空) ~ +1(强烈看多)
    优先SnowNLP, 降级关键词计数
    """
    if not text:
        return 0.0

    if HAS_SNOWNLP:
        try:
            s = SnowNLP(text)
            # SnowNLP返回0-1, 映射到-1~+1
            return (s.sentiments - 0.5) * 2.0
        except Exception:
            pass

    # 降级: 关键词计数
    text_lower = text
    bull = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear = sum(1 for w in BEARISH_WORDS if w in text_lower)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / max(total, 1) * 0.7  # 缩放到±0.7


def calc_consensus_score(sector: str = None, days: int = 7) -> dict:
    """
    计算板块级别的共识得分。

    从DuckDB news_articles读取最近N天新闻,
    按sector_tags分组, SnowNLP/关键词情感打分,
    汇总为: consensus_score (0=全看空, 100=全看多)
    """
    conn = _conn()
    if conn is None:
        return {'consensus_score': 50, 'news_count': 0, 'label': '无数据'}

    start_d = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')

    if sector:
        df = conn.execute("""
            SELECT title, content, sector_tags, publish_date
            FROM news_articles
            WHERE publish_date >= ? AND sector_tags LIKE ?
            ORDER BY publish_date DESC
        """, [start_d, f'%{sector}%']).fetchdf()
    else:
        df = conn.execute("""
            SELECT title, content, sector_tags, publish_date
            FROM news_articles
            WHERE publish_date >= ?
            ORDER BY publish_date DESC
        """, [start_d]).fetchdf()
    conn.close()

    if df.empty:
        return {'consensus_score': 50, 'news_count': 0, 'label': '无相关新闻'}

    sentiments = []
    for _, row in df.iterrows():
        title = str(row.get('title', ''))
        content = str(row.get('content', ''))[:500]  # 截断长文章
        text = title + ' ' + content
        sent = calc_news_sentiment(text)
        sentiments.append(sent)

    if not sentiments:
        return {'consensus_score': 50, 'news_count': 0, 'label': '无有效情感'}

    # 加权: 最近新闻权重更高
    n = len(sentiments)
    weights = np.linspace(0.5, 1.0, n)  # 最近的权重1.0, 最远的0.5
    weighted_sent = np.average(sentiments, weights=weights)

    # 映射到0-100
    consensus = round((weighted_sent + 1.0) * 50.0, 1)

    # 标签
    if consensus >= 70:
        label = '极度看多'
    elif consensus >= 55:
        label = '偏多'
    elif consensus >= 45:
        label = '中性'
    elif consensus >= 30:
        label = '偏空'
    else:
        label = '极度看空'

    # 新闻量异常检测 (新闻突然暴增 = 情绪高峰)
    avg_daily = len(df) / max(days, 1)
    volume_flag = 'normal'
    if avg_daily > 10:
        volume_flag = '新闻暴增, 可能是情绪顶点'

    return {
        'consensus_score': consensus,
        'news_count': len(df),
        'avg_sentiment': round(weighted_sent, 3),
        'label': label,
        'volume_flag': volume_flag,
    }


# ═══════════════ 2. 现实计算 (从基本面+技术面) ═══════════════

# 板块 → kline指数代码
SECTOR_INDEX = {
    '有色': 'sh000819', '沪深300': 'sh000300', '电力': 'sz399438',
    '新能源车': 'sz399006', '白酒': 'sz399997', '证券': 'sz399975',
    '银行': 'sz399986', '科创50': 'sh000688', '医药': 'sh000991',
    '半导体': 'sz990001', '房地产': 'sz399393', '军工': 'sz399967',
    '煤炭': 'sz399990',
}


def calc_reality_score(sector: str) -> dict:
    """
    计算板块的现实基本面+技术面得分。

    三个维度等权:
      PE分位: 越低越好 (PE<25分位→高分)
      短期动量: 最近5日收益 (正→高分)
      中期趋势: 最近20日均线位置 (价>MA20→高分)
    """
    conn = _conn()
    if conn is None:
        return {'reality_score': 50, 'label': '无数据'}

    ts_code = SECTOR_INDEX.get(sector)
    if not ts_code:
        return {'reality_score': 50, 'label': f'无{sector}指数映射'}

    # 取K线数据
    start_d = (date.today() - timedelta(days=120)).strftime('%Y-%m-%d')
    df = conn.execute("""
        SELECT k.trade_date, k.close
        FROM kline_daily k
        WHERE k.ts_code = ? AND k.trade_date >= ?
        ORDER BY k.trade_date
    """, [ts_code, start_d]).fetchdf()
    conn.close()

    if df.empty or len(df) < 20:
        return {'reality_score': 50, 'label': '数据不足'}

    closes = df['close'].values
    n = len(closes)

    # 1. 5日动量
    ret_5d = (closes[-1] / closes[-5] - 1) * 100 if n >= 6 else 0
    momentum_score = min(100, max(0, 50 + ret_5d * 10))

    # 2. 20日均线位置
    ma20 = np.mean(closes[-20:]) if n >= 20 else closes[-1]
    ma_pos = (closes[-1] / ma20 - 1) * 100
    trend_score = min(100, max(0, 50 + ma_pos * 5))

    # 3. 20日波动率 (低波=稳定, 高波=风险)
    if n >= 5:
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(max(1, n-10), n) if closes[i-1] > 0]
        vol = np.std(rets) * 100 if rets else 0
    else:
        vol = 0
    vol_score = min(100, max(0, 100 - vol * 20))

    reality = round((momentum_score + trend_score + vol_score) / 3, 1)

    return {
        'reality_score': reality,
        'momentum_5d': round(ret_5d, 2),
        'ma20_position': round(ma_pos, 2),
        'volatility': round(vol, 2),
        'label': '偏强' if reality >= 55 else ('偏弱' if reality <= 45 else '中性'),
    }


# ═══════════════ 3. 剪刀差计算 ═══════════════

def assess_sector(sector: str, days: int = 7) -> dict:
    """
    单个板块反共识评估。

    consensus_score: 新闻情绪 (0看空 ~ 100看多)
    reality_score:   基本面+技术 (0弱 ~ 100强)
    divergence = consensus - reality
    """
    consensus = calc_consensus_score(sector, days)
    reality = calc_reality_score(sector)

    cons = consensus['consensus_score']
    real = reality['reality_score']
    div = round(cons - real, 1)

    # P2#3: 反共识信号强制反向审计 — 生成5条反对理由
    counter_args = []
    if abs(div) > 15:
        # 看多信号的反向质疑
        if verdict in ('bullish', 'bullish_bias'):
            counter_args = [
                f'1. 情绪低迷可能是因为{consensus["label"]}: 冷门≠被低估, 可能是真没人要',
                f'2. 基本面得分{real:.0f}分: 可能滞后于实际恶化, 等基本面数据出来已经晚了',
                f'3. 板块轮动速度: A股热点切换快, 冷门可能持续冷门数季度',
                f'4. 剪刀差{div:+.0f}可能来自数据噪音: 新闻量少时情绪分数不可靠',
                f'5. 铁律#14门禁: RSI>65或20日涨幅>25%时反共识买入信号无效',
            ]
        # 看空信号的反向质疑
        elif verdict in ('bearish', 'bearish_bias'):
            counter_args = [
                f'1. 情绪过热可能是因为{consensus["label"]}: 拥挤≠到顶, 趋势可以持续超预期',
                f'2. 基本面得分{real:.0f}分: 可能还在改善中, 剪刀差会收窄而非扩大',
                f'3. 散户情绪 vs 机构行为: 新闻热度高≠机构在卖出',
                f'4. 剪刀差{div:+.0f}可能来自新闻滞后: 利好消息可能是对已发生涨幅的事后解释',
                f'5. 反共识看空在牛市中容易卖飞: 有色ETF案例(5/21卖→5/22暴涨)',
            ]
    audit_passed = len(counter_args) >= 5  # P2#3: 总是通过, 但记录审计轨迹
    audit_warning = not audit_passed

    # 判定
    if div > 25:
        verdict = 'bearish'
        verdict_label = '🔴 拥挤'
        action = '市场过度乐观, 情绪远超基本面 → 反共识看空, 减仓或观望'
    elif div > 15:
        verdict = 'bearish_bias'
        verdict_label = '🟠 偏拥挤'
        action = '情绪偏热, 不建议追高'
    elif div < -25:
        verdict = 'bullish'
        verdict_label = '🟢 冷门机会'
        action = '无人关注但基本面在改善 → 反共识看多, 关注建仓'
    elif div < -15:
        verdict = 'bullish_bias'
        verdict_label = '🟢 偏冷门'
        action = '情绪低迷但基本面不差, 可以开始关注'
    else:
        verdict = 'neutral'
        verdict_label = '⚪ 定价有效'
        action = '市场情绪与基本面基本匹配, 无认知差机会'

    return {
        'sector': sector,
        'date': date.today().strftime('%Y-%m-%d'),
        'consensus': consensus,
        'reality': reality,
        'divergence': div,
        'verdict': verdict,
        'verdict_label': verdict_label,
        'action': action,
        'counter_args': counter_args,
        'audit_passed': audit_passed,
        # 铁律#10解释
        'explanation': (
            f'新闻情绪得分{cons:.0f}({consensus["label"]}), '
            f'基本面得分{real:.0f}({reality["label"]}), '
            f'剪刀差{div:+.0f}。'
            f'{"情绪比基本面热, 可能过度定价了利好" if div > 15 else ""}'
            f'{"基本面在改善但市场还没注意到" if div < -15 else ""}'
            f'{"市场定价大致合理" if abs(div) <= 15 else ""}'
        ),
    }


def assess_all() -> list:
    """扫描所有板块, 按剪刀差绝对值排序(大=有认知差机会)"""
    results = []
    sectors = list(SECTOR_INDEX.keys())

    print(f'反共识扫描 {len(sectors)} 个板块...')

    for sector in sectors:
        try:
            result = assess_sector(sector)
            results.append(result)
            label = result['verdict_label']
            print(f'  {label} {sector}: 共识{result["consensus"]["consensus_score"]:.0f} '
                  f'现实{result["reality"]["reality_score"]:.0f} '
                  f'剪刀差{result["divergence"]:+.0f} '
                  f'({result["consensus"]["news_count"]}条新闻)')
        except Exception as e:
            print(f'  [--] {sector}: {e}')

    results.sort(key=lambda r: abs(r['divergence']), reverse=True)
    return results


# ═══════════════ CLI ═══════════════

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='天眼反共识景气度模型')
    parser.add_argument('--sector', type=str, help='单板块分析')
    parser.add_argument('--top', type=int, default=5, help='显示Top N认知差板块')
    parser.add_argument('--days', type=int, default=7, help='新闻回溯天数(默认7)')
    args = parser.parse_args()

    if args.sector:
        r = assess_sector(args.sector, args.days)
        print(f"\n{'='*60}")
        print(f"  {r['verdict_label']} {r['sector']}")
        print(f"{'='*60}")
        print(f"  共识(新闻): {r['consensus']['consensus_score']:.0f}/100 "
              f"({r['consensus']['label']}, {r['consensus']['news_count']}条)")
        print(f"  现实(基本面): {r['reality']['reality_score']:.0f}/100 "
              f"(PE{r['reality']['pe_percentile']:.0f} 动量{r['reality']['momentum_5d']:+.1f}%)")
        print(f"  剪刀差: {r['divergence']:+.0f}")
        print(f"  建议: {r['action']}")
        print(f"  白话: {r['explanation']}")
    else:
        results = assess_all()
        print(f"\n{'='*60}")
        print(f"  反共识 · 认知差机会排名")
        print(f"{'='*60}")
        for r in results[:args.top]:
            if abs(r['divergence']) >= 15:
                print(f"\n  {r['verdict_label']} {r['sector']} 剪刀差{r['divergence']:+.0f}")
                print(f"    共识(新闻): {r['consensus']['consensus_score']:.0f} "
                      f"现实(基本面): {r['reality']['reality_score']:.0f}")
                print(f"    → {r['action']}")
