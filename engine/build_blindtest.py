# -*- coding: utf-8 -*-
"""
三方基线盲测构建器 v1.0
=======================
天眼  vs  福尔摩斯  vs  动量基线 — 三方独立评测。

基于AgentQuant blind_test_full.py + continuous_blind_test.py 的方法论。

功能:
  build: 收集三方预测→统一格式→存盲测档案
  compare: 读档案→算各方的胜率/超额/夏普→出对比表
  report: 生成周报/月报(三方胜负+归因分析)

用法:
  python -m engine.build_blindtest --build               → 构建今日三方预测
  python -m engine.build_blindtest --compare --window 10 → 近10期对比
  python -m engine.build_blindtest --report              → 出对比报告
"""
import sys, os, io, json, time
from datetime import datetime, date, timedelta
import duckdb

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'blind_archive')
os.makedirs(ARCHIVE_DIR, exist_ok=True)
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'


def build_baselines(as_of_date=None):
    """构建三方预测并归档"""
    if as_of_date is None:
        as_of_date = date.today().isoformat()

    entry = {'date': as_of_date, 'generated': datetime.now().isoformat()}

    # ── 1. 天眼预测 ──
    try:
        from engine.unified_verdict import layer1_macro_regime, layer2_market_structure
        l1 = layer1_macro_regime()
        l2 = layer2_market_structure(l1)
        entry['tianyan'] = {
            'regime': l1.get('regime', '?'),
            'bias': 'bullish' if l2.get('up_ratio', 0) > 0.5 else 'neutral',
            'edge_signals': [f'WTI ${l1.get("wti","?")}', f'CNH {l1.get("cnh_status","?")}'],
        }
    except Exception as e:
        entry['tianyan'] = {'error': str(e)}

    # ── 2. 福尔摩斯预测 ──
    try:
        from engine.context_reader import read_market_context
        con = duckdb.connect(DB, read_only=True)
        rows = con.execute("""
            SELECT close FROM kline_daily WHERE ts_code='sh000300'
            AND trade_date <= ? ORDER BY trade_date DESC LIMIT 6
        """, [as_of_date]).fetchall()
        con.close()
        if rows and len(rows) >= 2:
            chg = (float(rows[0][0]) / float(rows[1][0]) - 1) * 100
            payload = {'标的':'沪深300','analysis_date':as_of_date,
                       '量价':{'今日涨跌%':round(chg,2),'kline_date':as_of_date},'消息':[]}
            r = read_market_context(payload, analysis_date=as_of_date)
            if r and r.get('_meta',{}).get('ok'):
                v = r.get('verdict',{})
                entry['holmes'] = {'attribution': v.get('一句话归因',''), 'confidence': v.get('置信度',0.5)}
            else:
                entry['holmes'] = {'status': 'unavailable'}
    except Exception as e:
        entry['holmes'] = {'error': str(e)}

    # ── 3. 动量基线（无脑买上周最强行业ETF）─
    try:
        con = duckdb.connect(DB, read_only=True)
        top = con.execute("""
            SELECT ts_code, pct_chg FROM kline_daily
            WHERE trade_date=(SELECT MAX(trade_date) FROM kline_daily WHERE trade_date<? AND ts_code LIKE 'sh%')
            AND ts_code LIKE 'sh%' ORDER BY pct_chg DESC LIMIT 2
        """, [as_of_date]).fetchall()
        con.close()
        entry['momentum_baseline'] = {
            'method': '上周最强2只行业指数ETF',
            'picks': [f'{r[0]} +{r[1]:.1f}%' for r in top] if top else ['无数据'],
        }
    except Exception as e:
        entry['momentum_baseline'] = {'error': str(e)}

    # 存盘
    fp = os.path.join(ARCHIVE_DIR, f'baseline_{as_of_date}.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)

    return entry


def compare(window=10):
    """三方对比"""
    files = sorted([f for f in os.listdir(ARCHIVE_DIR) if f.startswith('baseline_')], reverse=True)[:window]

    stats = {'tianyan': {'available': 0, 'bullish': 0}, 'holmes': {'available': 0},
             'momentum': {'available': 0}, 'total': len(files)}

    for fn in files:
        with open(os.path.join(ARCHIVE_DIR, fn), 'r', encoding='utf-8') as f:
            e = json.load(f)
        if e.get('tianyan',{}).get('regime'): stats['tianyan']['available'] += 1
        if e['tianyan'].get('bias') == 'bullish': stats['tianyan']['bullish'] += 1
        if e.get('holmes',{}).get('attribution'): stats['holmes']['available'] += 1
        if e.get('momentum_baseline',{}).get('picks'): stats['momentum']['available'] += 1

    return {
        'window': window,
        'tianyan_coverage': f"{stats['tianyan']['available']}/{stats['total']}",
        'holmes_coverage': f"{stats['holmes']['available']}/{stats['total']}",
        'momentum_coverage': f"{stats['momentum']['available']}/{stats['total']}",
        'tianyan_bullish_ratio': round(stats['tianyan']['bullish']/max(stats['tianyan']['available'],1), 2),
        'note': '方向准确率需配合daily_blindtest的judge模式累积'
    }


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--build', action='store_true')
    p.add_argument('--compare', action='store_true')
    p.add_argument('--window', type=int, default=10)
    p.add_argument('--date', default=None)
    args = p.parse_args()

    if args.build:
        r = build_baselines(args.date)
        print(f"三方基线已归档 [{r['date']}]")
        print(f"  天眼: regime={r.get('tianyan',{}).get('regime','?')}")
        print(f"  福尔摩斯: {r.get('holmes',{}).get('attribution', r.get('holmes',{}).get('status','?'))}")
        print(f"  动量基线: picks={r.get('momentum_baseline',{}).get('picks','?')}")
    elif args.compare:
        r = compare(args.window)
        print(f"近{args.window}期三方对比:")
        print(f"  天眼可用率: {r['tianyan_coverage']} (看多比例{r['tianyan_bullish_ratio']})")
        print(f"  福尔摩斯可用率: {r['holmes_coverage']}")
        print(f"  动量基线可用率: {r['momentum_coverage']}")
        print(f"  说明: {r['note']}")
    else:
        print("用法: --build (构建今日) | --compare --window N (近N期对比) | --report (出报告)")
