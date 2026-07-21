# -*- coding: utf-8 -*-
"""
事件前瞻引擎 v1.0 — 重大事件日历 + 结构化拆解 + 联动分析
覆盖: IPO/经济数据/央行会议/地缘事件/财报季
"""
import sys, os, json, hashlib, ssl, warnings, time, io
from datetime import datetime, timedelta
from pathlib import Path

# UTF-8 stdout 封装
_orig_stdout = sys.stdout
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except (ValueError, AttributeError):
    sys.stdout = _orig_stdout

ssl._create_default_https_context = ssl._create_unverified_context
warnings.filterwarnings('ignore')

import duckdb
import requests

DB_PATH = str(Path(__file__).parent.parent / "tianyan.duckdb")
PROJECT_ROOT = str(Path(__file__).parent.parent)

# ============================================================
# 数据库初始化
# ============================================================
def init_db():
    """创建event_calendar表（如果不存在）"""
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS event_calendar (
            event_date    DATE NOT NULL,
            event_type    VARCHAR NOT NULL,
            title         VARCHAR NOT NULL,
            expect_value  VARCHAR,
            prior_value   VARCHAR,
            impact_score  DOUBLE,
            days_away     INTEGER,
            structure_note TEXT,
            linked_assets TEXT,
            interactions  TEXT,
            siphon_estimate TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (event_date, title)
        )
    """)
    con.close()

def upsert_events(events, interactions):
    """写入/更新事件到DuckDB"""
    con = duckdb.connect(DB_PATH)
    init_db()

    # 构建事件→交互映射
    interaction_map = {}
    for ia in interactions:
        key_a = (ia['event_a'],)
        key_b = (ia['event_b'],)
        # 简单地用标题关联
        interaction_map[ia['event_a']] = ia.get('interaction', '')
        interaction_map[ia['event_b']] = ia.get('interaction', '')

    inserted = 0
    for e in events:
        linked = json.dumps(e.get('linked_assets', []), ensure_ascii=False)
        title_interaction = interaction_map.get(e['title'], '')
        con.execute("""
            INSERT OR REPLACE INTO event_calendar
                (event_date, event_type, title, expect_value, prior_value,
                 impact_score, days_away, structure_note,
                 linked_assets, interactions, siphon_estimate,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, [
            e['date'],
            e['type'],
            e['title'],
            e.get('expect', ''),
            e.get('prior', ''),
            e.get('impact_score', 0),
            e.get('days_away', 0),
            e.get('structure_note', '')[:2000],
            linked,
            title_interaction[:1000],
            e.get('siphon_estimate', '')[:1000],
        ])
        inserted += 1

    con.close()
    return inserted

# ============================================================
# 事件类型定义
# ============================================================
EVENT_TYPES = {
    'ipo':       {'label': 'IPO/上市',    'impact_weight': 1.0, 'lead_days': 7},
    'econ_data': {'label': '经济数据',    'impact_weight': 0.8, 'lead_days': 3},
    'cb_meeting':{'label': '央行会议',    'impact_weight': 1.2, 'lead_days': 7},
    'geo_event': {'label': '地缘事件',    'impact_weight': 0.9, 'lead_days': 3},
    'earnings':  {'label': '财报季',      'impact_weight': 0.6, 'lead_days': 7},
}

# ============================================================
# 手工维护的重大事件日历（兜底数据源）
# 每次Web搜索无法覆盖时由此提供硬数据
# ============================================================
HARD_EVENTS_2026_06 = [
    {
        'date': '2026-06-10',
        'type': 'econ_data',
        'title': '美国5月CPI',
        'expect': '4.2% YoY (核心2.9%)',
        'prior': '3.8%',
        'market_implied': '70%概率加息',
        'structure_note': '关注核心CPI是否≥2.9%→直接影响FOMC措辞',
        'linked_assets': ['纳指100', '标普500', '美元指数', '美10Y'],
        'interactions': ['2026-06-12 SpaceX IPO', '2026-06-16 FOMC'],
    },
    {
        'date': '2026-06-12',
        'type': 'ipo',
        'title': 'SpaceX (SPCX) 纳斯达克上市',
        'expect': '估值$1.75万亿 / 融资$750亿 / IPO价$135',
        'prior': '史上最大IPO',
        'structure_note': (
            '散户配售30%($5400亿)→史上最高散户比例。'
            '星链是唯一盈利引擎(订阅1030万/ARPU$66↓/EBITDA 63%)。'
            '其余部门烧钱: GAAP净亏-$49.4亿, FCF -$91亿。'
            '双类股权: 马斯克42%股权→85.1%投票权。'
            '晨星公允估值$7800亿, vs IPO $1.75万亿→溢价124%。'
        ),
        'siphon_estimate': (
            '被动纳入: 纳指100预计15日内纳入→强制买入→机构提前调仓。'
            '虹吸已现: Mag7月内仅+2% vs 标普+4%, BTC -17%。'
            '预估科技股抽血$500-1000亿(含主动调仓+被动纳入)。'
        ),
        'linked_assets': ['纳指100', 'Mag7', 'BTC', 'RKLB', 'GOOGL(持7%)', 'QCOM(星链芯片)'],
        'interactions': ['2026-06-10 CPI', '2026-06-16 FOMC'],
    },
    {
        'date': '2026-06-16',
        'type': 'cb_meeting',
        'title': '美联储FOMC会议 (6/16-17) — 沃什首秀',
        'expect': '维持5.25-5.50%不变',
        'prior': '上次会议后非农+17.2万远超预期',
        'market_implied': '市场定价72%概率年底前加息',
        'structure_note': (
            '沃什首秀措辞关键。非农表面强但结构差(全职-7.9万/兼职+22.6万)→'
            '若沃什删除"降息倾向"表述→鹰派确认→科技股再杀。'
            '若措辞中性→市场可能重新定价加息概率→修复行情。'
        ),
        'linked_assets': ['纳指100', '标普500', '美元指数', '美10Y', '黄金'],
        'interactions': ['2026-06-10 CPI', '2026-06-12 SpaceX IPO', '2026-07-01 6月非农'],
    },
    {
        'date': '2026-07-01',
        'type': 'econ_data',
        'title': '美国6月非农（预估7/1-7/3公布）',
        'expect': '回落至+8-12万（休闲酒店回吐）',
        'prior': '5月+17.2万（结构: 全职-7.9万/兼职+22.6万）',
        'structure_note': (
            '关键验证: 5月休闲酒店+7万(纪念日前置)→预测6月回吐至<3万。'
            '若回吐兑现→证实5月非农水分→加息叙事崩塌→降息预期回摆。'
            '若6月继续>15万且全职转正→加息逻辑有据→防御继续。'
        ),
        'linked_assets': ['纳指100', '标普500', '美元指数', '美10Y', 'A股外资'],
        'interactions': ['2026-06-16 FOMC'],
    },
]

# ============================================================
# 事件联动分析矩阵
# ============================================================
def analyze_interactions(events):
    """标注事件间的交叉影响"""
    analyses = []
    for i, e1 in enumerate(events):
        for e2 in events[i+1:]:
            if e1['date'] <= e2['date']:
                gap = (datetime.strptime(e2['date'], '%Y-%m-%d') -
                       datetime.strptime(e1['date'], '%Y-%m-%d')).days
                if gap <= 14:  # 14天内的事件才做联动
                    analyses.append({
                        'event_a': e1['title'],
                        'event_b': e2['title'],
                        'gap_days': gap,
                        'interaction': _judge_interaction(e1, e2, gap),
                    })
    return analyses

def _judge_interaction(e1, e2, gap):
    """判断两个事件的交互方向"""
    a_type = e1['type']
    b_type = e2['type']

    # CPI + FOMC: 直接传导
    if a_type == 'econ_data' and b_type == 'cb_meeting':
        return (
            f'CPI({e1["expect"]})→FOMC措辞调整。'
            f'若CPI≤4.1%→FOMC偏鸽→利好。'
            f'若CPI≥4.3%→FOMC偏鹰→利空。'
            f'间隔{gap}天, CPI先行定价FOMC预期。'
        )

    # IPO + FOMC: 虹吸+宏观双压
    if a_type == 'ipo' and b_type == 'cb_meeting':
        return (
            f'SpaceX虹吸抽血→FOMC可能鹰派→双杀风险。'
            f'即使FOMC中性, 被动纳入调仓(gap={gap}天)仍持续抽水。'
            f'纳指需两个力都解除才能有效反弹。'
        )

    # CPI + IPO: 对冲
    if a_type == 'econ_data' and b_type == 'ipo':
        return (
            f'CPI利好→加息恐慌退→对纳指正面。'
            f'但SpaceX虹吸同步抽血→两个力对冲。'
            f'净效应=CPI偏离幅度 vs IPO首日认购倍数。'
        )

    return f'间隔{gap}天, {e1["type"]}→{e2["type"]}, 需逐日跟踪交互效应。'

# ============================================================
# 事件影响力评分
# ============================================================
def score_events(events):
    """对事件打分, 用于排序和预警"""
    scored = []
    for e in events:
        score = EVENT_TYPES.get(e['type'], {}).get('impact_weight', 0.5)
        # 越临近 + 越大
        days_away = (datetime.strptime(e['date'], '%Y-%m-%d') - datetime.now()).days
        urgency = max(0, 1.0 - days_away / 14.0)  # 14天内从0→1
        score *= (1.0 + urgency)
        scored.append({**e, 'impact_score': round(score, 2), 'days_away': days_away})
    return sorted(scored, key=lambda x: x['impact_score'], reverse=True)

# ============================================================
# 生成事件日报片段
# ============================================================
def generate_event_brief(events, interactions):
    """生成可注入日报的事件简报"""
    scored = score_events(events)
    today = datetime.now().strftime('%Y-%m-%d')

    lines = ['## 📅 事件前瞻（7天窗口）', '']

    # 今日事件
    today_events = [e for e in scored if e['date'] == today]
    if today_events:
        lines.append('### ⚡ 今日事件')
        for e in today_events:
            lines.append(f"- **{e['title']}**: {e.get('expect', '')} | 前值: {e.get('prior', '')}")
            if e.get('structure_note'):
                lines.append(f"  > 结构: {e['structure_note'][:200]}")
        lines.append('')

    # 未来7天
    future = [e for e in scored if e['date'] > today and e['days_away'] <= 7]
    if future:
        lines.append('### 📆 即将到来')
        for e in future:
            tag = '🔴' if e['impact_score'] > 1.5 else '🟡' if e['impact_score'] > 0.8 else '🟢'
            lines.append(
                f"- {tag} **{e['date']}** ({e['days_away']}天后) "
                f"| {e['title']} | {e.get('expect', '')} "
                f"| 影响力: {e['impact_score']}"
            )
        lines.append('')

    # 联动分析
    if interactions:
        lines.append('### 🔗 事件联动')
        for ia in interactions[:5]:
            lines.append(f"- [{ia['gap_days']}天间隔] **{ia['event_a']}** ←→ **{ia['event_b']}**")
            lines.append(f"  > {ia['interaction'][:200]}")
        lines.append('')

    # 风险汇总
    high_impact = [e for e in scored if e['impact_score'] > 1.2]
    if high_impact:
        lines.append('### ⚠️ 事件风险汇总')
        lines.append(f'高影响力事件 {len(high_impact)}个:')
        for e in high_impact:
            lines.append(f"- {e['date']} {e['title']} (得分: {e['impact_score']})")
        lines.append('')

    return '\n'.join(lines)

# ============================================================
# 主入口
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description='事件前瞻引擎')
    parser.add_argument('--days', type=int, default=7, help='前瞻天数')
    parser.add_argument('--event', type=str, help='指定事件名称')
    parser.add_argument('--output', type=str, default='brief',
                       choices=['brief', 'json', 'both'], help='输出格式')
    args = parser.parse_args()

    # 加载硬编码事件（后续可扩展Web抓取补充）
    events = HARD_EVENTS_2026_06

    # 过滤指定事件
    if args.event:
        events = [e for e in events if args.event.lower() in e['title'].lower()]
        if not events:
            print(f"未找到事件: {args.event}")
            sys.exit(1)

    # 过滤天数范围
    cutoff = (datetime.now() + timedelta(days=args.days)).strftime('%Y-%m-%d')
    today = datetime.now().strftime('%Y-%m-%d')
    events_in_range = [e for e in events if today <= e['date'] <= cutoff]

    events_in_range.sort(key=lambda e: e['date'])

    # 打分
    scored = score_events(events_in_range)

    # 联动分析
    interactions = analyze_interactions(scored)

    # 入库
    n = upsert_events(scored, interactions)
    print(f">> 入库: {n} 条事件 → {DB_PATH} (event_calendar)", file=sys.stderr)

    # 输出
    if args.output in ('brief', 'both'):
        brief = generate_event_brief(scored, interactions)
        print(brief)

    if args.output in ('json', 'both'):
        output = {
            'generated': datetime.now().isoformat(),
            'window_days': args.days,
            'event_count': len(events_in_range),
            'events': events_in_range,
            'interactions': interactions,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
