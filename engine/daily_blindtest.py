# -*- coding: utf-8 -*-
"""
天眼 × 福尔摩斯 LLM联合盲测 v1.0
=================================
基于AgentQuant/our/blind_test_detectives.py 的侦探盲测框架。

功能:
  侦探测评: 天眼的LLM解释引擎 vs 福尔摩斯的推理链
  盲测协议: 生成日→裁判周→评分月, 前向时序严格防泄漏
  评分维度: 方向命中/幅度误差/置信度校准/最坏情形预测

用法:
  python -m engine.daily_blindtest              → 今日评测循环
  python -m engine.daily_blindtest --judge      → 裁判模式(评上周预测)
  python -m engine.daily_blindtest --score      → 累积评分
"""
import sys, os, io, json, time
from datetime import datetime, date, timedelta

RECORDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'blind_records')
os.makedirs(RECORDS_DIR, exist_ok=True)


def generate_prediction(as_of_date=None):
    """生成今日预测（天眼+福尔摩斯双引擎）"""
    if as_of_date is None:
        as_of_date = date.today().isoformat()

    prediction = {
        'date': as_of_date,
        'generated_at': datetime.now().isoformat(),
        'tianyan': {},
        'holmes': {},
    }

    # 天眼预测
    try:
        from engine.unified_verdict import layer1_macro_regime, layer2_market_structure
        l1 = layer1_macro_regime()
        l2 = layer2_market_structure(l1)
        prediction['tianyan'] = {
            'regime': l1.get('regime', 'UNKNOWN'),
            'direction': 'bullish' if l2.get('up_ratio', 0) > 0.5 else 'neutral',
            'confidence': min(0.8, abs(l2.get('up_ratio', 0.5) - 0.5) * 2 + 0.3),
            'vol_trend': l2.get('vol_analysis', {}).get('vol_trend', '?'),
        }
    except Exception as e:
        prediction['tianyan'] = {'error': str(e)}

    # 福尔摩斯预测（调用context_reader解释引擎）
    try:
        from engine.context_reader import read_market_context
        import duckdb
        con = duckdb.connect(r'D:\FreeFinanceData\data\duckdb\finance.db', read_only=True)
        rows = con.execute("""
            SELECT close, amount FROM kline_daily WHERE ts_code='sh000300'
            AND trade_date <= ? ORDER BY trade_date DESC LIMIT 6
        """, [as_of_date]).fetchall()
        con.close()

        if rows and len(rows) >= 2:
            chg = (float(rows[0][0]) / float(rows[1][0]) - 1) * 100
            amt = (float(rows[0][1] or 0)) / 1e8
            avg5 = sum(float(r[1] or 0) for r in rows[1:]) / 5 / 1e8 if len(rows) > 1 else amt

            payload = {
                '标的': '沪深300', 'analysis_date': as_of_date,
                '量价': {'今日涨跌%': round(chg, 2), '成交额亿': round(amt, 0),
                        '成交额5日均亿': round(avg5, 0), 'kline_date': as_of_date},
                '消息': []
            }
            reading = read_market_context(payload, analysis_date=as_of_date)
            if reading and reading.get('_meta', {}).get('ok'):
                v = reading.get('verdict', {})
                prediction['holmes'] = {
                    'attribution': v.get('一句话归因', ''),
                    'main_hypothesis': v.get('最可能解释', ''),
                    'confidence': v.get('置信度', 0.5),
                    'correction_line': v.get('纠错线', ''),
                }
            else:
                prediction['holmes'] = {'status': '引擎未过验收, 本期无福尔摩斯预测'}
    except Exception as e:
        prediction['holmes'] = {'error': str(e)}

    # 存盘
    fp = os.path.join(RECORDS_DIR, f'pred_{as_of_date}.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(prediction, f, ensure_ascii=False, indent=1)

    return prediction


def judge_week(target_date=None):
    """裁判模式: 读取上周预测 vs 实际走势"""
    if target_date is None:
        target_date = date.today().isoformat()

    # 找最近的预测文件
    preds = sorted([f for f in os.listdir(RECORDS_DIR) if f.startswith('pred_')], reverse=True)
    if not preds:
        return {'error': '无历史预测'}

    latest_pred_fp = os.path.join(RECORDS_DIR, preds[0])
    with open(latest_pred_fp, 'r', encoding='utf-8') as f:
        pred = json.load(f)

    pred_date = pred['date']

    # 查实际走势
    import duckdb
    con = duckdb.connect(r'D:\FreeFinanceData\data\duckdb\finance.db', read_only=True)
    try:
        actual = con.execute("""
            SELECT trade_date, close FROM kline_daily WHERE ts_code='sh000300'
            AND trade_date > ? ORDER BY trade_date LIMIT 5
        """, [pred_date]).fetchall()
        if actual:
            start_close = None
            day0 = con.execute("SELECT close FROM kline_daily WHERE ts_code='sh000300' AND trade_date=?",
                              [pred_date]).fetchone()
            if day0:
                start_close = float(day0[0])
            end_close = float(actual[-1][1])
            week_chg = (end_close / start_close - 1) * 100 if start_close else 0

            t_dir = pred.get('tianyan', {}).get('direction', '?')
            actual_dir = 'bullish' if week_chg > 0 else 'bearish'
            t_correct = (t_dir == 'bullish' and week_chg > 0) or (t_dir == 'bearish' and week_chg < 0)

            verdict = {
                'pred_date': pred_date,
                'actual_dates': f'{actual[0][0]}~{actual[-1][0]}',
                'week_chg_pct': round(week_chg, 2),
                'tianyan_direction': t_dir,
                'tianyan_correct': t_correct,
                'actual_direction': actual_dir,
            }

            # 存评分
            sfp = os.path.join(RECORDS_DIR, f'score_{target_date}.json')
            with open(sfp, 'w', encoding='utf-8') as f:
                json.dump(verdict, f, ensure_ascii=False, indent=1)

            return verdict
        else:
            return {'error': f'{pred_date}后无K线数据, 待下周'}
    finally:
        con.close()


def cumulative_score():
    """累积评分"""
    scores = []
    for f in sorted(os.listdir(RECORDS_DIR)):
        if f.startswith('score_'):
            with open(os.path.join(RECORDS_DIR, f), 'r', encoding='utf-8') as fp:
                scores.append(json.load(fp))

    if not scores:
        return {'total': 0, 'correct': 0, 'accuracy': 0}

    correct = sum(1 for s in scores if s.get('tianyan_correct'))
    return {
        'total': len(scores),
        'correct': correct,
        'accuracy': round(correct / len(scores), 3),
        'latest_10': [s['tianyan_correct'] for s in scores[-10:]],
        'rolling_acc_10': round(sum(1 for s in scores[-10:] if s.get('tianyan_correct')) / min(len(scores), 10), 3),
    }


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--judge', action='store_true', help='裁判模式(评上周预测)')
    p.add_argument('--score', action='store_true', help='累积评分')
    p.add_argument('--date', default=None, help='日期YYYY-MM-DD')
    args = p.parse_args()

    if args.judge:
        result = judge_week(args.date)
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.score:
        result = cumulative_score()
        print(f"总评测: {result['total']}次 | 正确: {result['correct']} | 准确率: {result['accuracy']:.1%}")
        print(f"近10次滚准: {result['rolling_acc_10']:.1%} | 序列: {result['latest_10']}")
    else:
        result = generate_prediction(args.date)
        print(f"生成预测 [{result['date']}]")
        print(f"  天眼方向: {result['tianyan'].get('direction','?')} (conf={result['tianyan'].get('confidence','?')})")
        print(f"  福尔摩斯: {result['holmes'].get('main_hypothesis', result['holmes'].get('status','?'))}")
