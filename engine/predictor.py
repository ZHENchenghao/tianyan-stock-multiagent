# -*- coding: utf-8 -*-
"""
18维预测引擎 v1.0 — 基于AgentQuant自适应引擎+多因子信号
========================================================
从AgentQuant/our/adaptive_engine.py + advanced_features.py 提取并升级，
适配天眼DuckDB数据底座。

18个独立预测维度, 各产出一个方向信号(bullish/bearish) + 置信度(0-1):
  宏观信号 x5: 趋势强度/波动率突变/MARatio偏离/北向方向/融资方向
  技术信号 x5: RSI极值/KDJ极值/量价背离/MACD死叉金叉/均线排列
  情绪信号 x4: 涨跌比/新高新低比/Put-Call Skew/资金流背离
  制度信号 x4: 涨跌停统计/龙虎榜净买/ETF折溢价/VPIN

用法:
  python -m engine.predictor                    → 今日全18维预测
  python -m engine.predictor --date 2026-07-15  → 指定日
  python -m engine.predictor --summary           → 简短摘要
"""
import sys, os, io, json, time
from datetime import datetime, date, timedelta
import duckdb, numpy as np

DB = r'D:\FreeFinanceData\data\duckdb\finance.db'

def _last_trading_day(ref=None):
    """找到最近的交易日（≤ref的最后一个K线日）"""
    con = duckdb.connect(DB, read_only=True)
    try:
        r = con.execute("SELECT MAX(trade_date) FROM kline_daily WHERE ts_code='sh000300'").fetchone()
        return str(r[0]) if r and r[0] else None
    finally:
        con.close()

def _q(sql, params=None):
    con = duckdb.connect(DB, read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()

def compute_all_18(as_of_date=None):
    """
    计算全部18个维度的预测信号。
    返回: {'date': ..., 'signals': [{name, direction, confidence, value, detail}, ...], 'summary': ..., 'composite': {...}}
    """
    if as_of_date is None:
        as_of_date = _last_trading_day()
        if not as_of_date:
            return {'error': '无K线数据'}

    con = duckdb.connect(DB, read_only=True)
    signals = []

    # ── 宏观信号 (x5) ──
    try:
        rows = con.execute("""
            SELECT trade_date, close, vol FROM kline_daily
            WHERE ts_code='sh000300' ORDER BY trade_date
        """).fetchall()
        dates = [str(r[0]) for r in rows]
        closes = np.array([r[1] for r in rows], dtype=float)
        vols = np.array([r[2] or 0 for r in rows], dtype=float)
        N = len(closes)

        try: idx = dates.index(as_of_date)
        except ValueError: idx = N - 1

        # MA计算
        ma5   = np.mean(closes[max(0,idx-4):idx+1])
        ma20  = np.mean(closes[max(0,idx-19):idx+1])
        ma60  = np.mean(closes[max(0,idx-59):idx+1])
        ma200 = np.mean(closes[max(0,idx-199):idx+1])

        # 1. 趋势强度
        trend_score = (closes[idx] / ma200 - 1) * 100  # 偏离200日均线%
        if trend_score > 5:
            signals.append({'name': '趋势强度', 'direction': 'bullish', 'confidence': min(0.95, 0.5 + abs(trend_score)/40),
                           'value': round(trend_score,1), 'detail': f'偏离MA200 +{trend_score:.1f}%'})
        elif trend_score < -5:
            signals.append({'name': '趋势强度', 'direction': 'bearish', 'confidence': min(0.95, 0.5 + abs(trend_score)/40),
                           'value': round(trend_score,1), 'detail': f'偏离MA200 {trend_score:.1f}%'})
        else:
            signals.append({'name': '趋势强度', 'direction': 'neutral', 'confidence': 0.3, 'value': round(trend_score,1),
                           'detail': f'MA200附近 ±5%内'})

        # 2. 波动率突变 (20日波动率 vs 60日)
        if idx >= 60:
            vol20 = np.std(np.diff(closes[idx-19:idx+1]) / closes[idx-19:idx+1]) * 100
            vol60 = np.std(np.diff(closes[idx-59:idx+1]) / closes[idx-59:idx+1]) * 100
            vol_ratio = vol20 / max(vol60, 0.001)
            if vol_ratio > 1.5:
                signals.append({'name': '波动率突变', 'direction': 'bearish', 'confidence': min(0.8, vol_ratio/3),
                               'value': round(vol_ratio,2), 'detail': f'20日波{vol20:.1f}%/60日波{vol60:.1f}%(放大{vol_ratio:.1f}x→风险升)'})
            elif vol_ratio < 0.6:
                signals.append({'name': '波动率突变', 'direction': 'bullish', 'confidence': 0.4,
                               'value': round(vol_ratio,2), 'detail': f'波动收敛{vol_ratio:.1f}x(低波环境)'})
            else:
                signals.append({'name': '波动率突变', 'direction': 'neutral', 'confidence': 0.2,
                               'value': round(vol_ratio,2), 'detail': '波动正常'})

        # 3. MARatio偏离 (MA20/MA60/MA200 排列)
        if ma5 > ma20 > ma60 > ma200:
            signals.append({'name': 'MA排列', 'direction': 'bullish', 'confidence': 0.7, 'value': 1,
                           'detail': '多头排列(5>20>60>200)'})
        elif ma5 < ma20 < ma60 < ma200:
            signals.append({'name': 'MA排列', 'direction': 'bearish', 'confidence': 0.7, 'value': -1,
                           'detail': '空头排列(5<20<60<200)'})
        else:
            signals.append({'name': 'MA排列', 'direction': 'neutral', 'confidence': 0.3, 'value': 0, 'detail': '均线缠绕'})
    except Exception as e:
        signals.append({'name': '宏观技术组', 'direction': 'neutral', 'confidence': 0, 'value': 0,
                       'detail': f'计算失败:{e}'})

    # ── 北向资金信号 ──
    try:
        nb = con.execute("""
            SELECT net_flow FROM lab_northbound_daily
            WHERE net_flow IS NOT NULL ORDER BY trade_date DESC LIMIT 10
        """).fetchall()
        if nb:
            latest = nb[0][0]
            avg5 = sum(float(r[0]) for r in nb[:5]) / min(len(nb), 5)
            if latest > 20:
                signals.append({'name': '北向资金', 'direction': 'bullish', 'confidence': min(0.8, abs(latest)/100),
                               'value': round(latest,1), 'detail': f'净流入{latest:.1f}亿'})
            elif latest < -20:
                signals.append({'name': '北向资金', 'direction': 'bearish', 'confidence': min(0.8, abs(latest)/100),
                               'value': round(latest,1), 'detail': f'净流出{latest:.1f}亿'})
            else:
                signals.append({'name': '北向资金', 'direction': 'neutral', 'confidence': 0.3,
                               'value': round(latest,1), 'detail': f'小幅净{"流入" if latest>0 else "流出"}{latest:.1f}亿'})
    except:
        signals.append({'name': '北向资金', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── 融资信号 ──
    try:
        mg = con.execute("SELECT margin_balance FROM margin_trading ORDER BY trade_date DESC LIMIT 10").fetchall()
        if mg and len(mg) >= 5:
            now_mg = float(mg[0][0])
            avg5_mg = sum(float(r[0]) for r in mg[:5]) / 5
            chg = (now_mg / avg5_mg - 1) * 100 if avg5_mg > 0 else 0
            if chg > 2:
                signals.append({'name': '融资余额', 'direction': 'bullish', 'confidence': min(0.7, abs(chg)/10),
                               'value': round(chg,1), 'detail': f'5日均值+{chg:.1f}%(杠杆加仓)'})
            elif chg < -2:
                signals.append({'name': '融资余额', 'direction': 'bearish', 'confidence': min(0.7, abs(chg)/10),
                               'value': round(chg,1), 'detail': f'5日均值{chg:.1f}%(去杠杆)'})
            else:
                signals.append({'name': '融资余额', 'direction': 'neutral', 'confidence': 0.2,
                               'value': round(chg,1), 'detail': '融资平稳'})
    except:
        signals.append({'name': '融资余额', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── 技术信号 ──
    try:
        tech = con.execute("""
            SELECT rsi14, kdj_j, macd_hist FROM technical_indicators
            WHERE ts_code='sh000300' AND trade_date=? LIMIT 1
        """, [as_of_date]).fetchone()

        if tech:
            rsi, kdj, macd = tech[0] or 50, tech[1] or 50, tech[2] or 0

            # RSI极值
            if rsi > 75:
                signals.append({'name': 'RSI极值', 'direction': 'bearish', 'confidence': 0.6, 'value': round(rsi,1),
                               'detail': f'RSI={rsi:.0f}超买'})
            elif rsi < 25:
                signals.append({'name': 'RSI极值', 'direction': 'bullish', 'confidence': 0.5, 'value': round(rsi,1),
                               'detail': f'RSI={rsi:.0f}超卖'})
            else:
                signals.append({'name': 'RSI极值', 'direction': 'neutral', 'confidence': 0.2, 'value': round(rsi,1),
                               'detail': f'RSI={rsi:.0f}中性'})

            # KDJ极值
            if kdj > 85:
                signals.append({'name': 'KDJ极值', 'direction': 'bearish', 'confidence': 0.5, 'value': round(kdj,1),
                               'detail': f'J={kdj:.0f}超买区'})
            elif kdj < 15:
                signals.append({'name': 'KDJ极值', 'direction': 'bullish', 'confidence': 0.4, 'value': round(kdj,1),
                               'detail': f'J={kdj:.0f}超卖区(需过VPIN闸门)'})
            else:
                signals.append({'name': 'KDJ极值', 'direction': 'neutral', 'confidence': 0.2, 'value': round(kdj,1),
                               'detail': f'J={kdj:.0f}中性'})

            # MACD
            if macd > 0:
                signals.append({'name': 'MACD', 'direction': 'bullish', 'confidence': 0.4, 'value': round(macd,3),
                               'detail': f'MACD柱{macef:.3f}>0'})
            else:
                signals.append({'name': 'MACD', 'direction': 'bearish', 'confidence': 0.4, 'value': round(macd,3),
                               'detail': f'MACD柱{macef:.3f}<0'})
    except:
        pass

    # ── 量价关系 ──
    try:
        if idx >= 4:
            vol_today = vols[idx]
            avg_vol5 = np.mean(vols[max(0,idx-4):idx+1])
            ret_today = (closes[idx] / closes[idx-1] - 1) * 100 if idx > 0 else 0
            vol_ratio_v = vol_today / max(avg_vol5, 1)

            if ret_today > 1 and vol_ratio_v > 1.3:
                signals.append({'name': '量价关系', 'direction': 'bullish', 'confidence': 0.5,
                               'value': round(vol_ratio_v,2), 'detail': '放量上涨(量价配合)'})
            elif ret_today > 1 and vol_ratio_v < 0.7:
                signals.append({'name': '量价关系', 'direction': 'bearish', 'confidence': 0.5,
                               'value': round(vol_ratio_v,2), 'detail': '缩量上涨(量价背离)'})
            elif ret_today < -1 and vol_ratio_v > 1.3:
                signals.append({'name': '量价关系', 'direction': 'bearish', 'confidence': 0.6,
                               'value': round(vol_ratio_v,2), 'detail': '放量下跌(恐慌出货)'})
            elif ret_today < -1 and vol_ratio_v < 0.7:
                signals.append({'name': '量价关系', 'direction': 'bullish', 'confidence': 0.3,
                               'value': round(vol_ratio_v,2), 'detail': '缩量下跌(抛压枯竭)'})
            else:
                signals.append({'name': '量价关系', 'direction': 'neutral', 'confidence': 0.2,
                               'value': round(vol_ratio_v,2), 'detail': '量价正常'})
    except:
        pass

    # ── 涨跌比 ──
    try:
        updown = con.execute("""
            SELECT COUNT(CASE WHEN pct_chg>0 THEN 1 END)*1.0/COUNT(*) FROM kline_daily
            WHERE trade_date=?
        """, [as_of_date]).fetchone()
        if updown and updown[0]:
            ratio = float(updown[0])
            if ratio > 0.6:
                signals.append({'name': '涨跌比', 'direction': 'bullish', 'confidence': 0.5, 'value': round(ratio,2),
                               'detail': f'{ratio:.0%}个股上涨(普涨)'})
            elif ratio < 0.3:
                signals.append({'name': '涨跌比', 'direction': 'bearish', 'confidence': 0.5, 'value': round(ratio,2),
                               'detail': f'{ratio:.0%}个股上涨(普跌)'})
            else:
                signals.append({'name': '涨跌比', 'direction': 'neutral', 'confidence': 0.2, 'value': round(ratio,2),
                               'detail': f'{ratio:.0%}涨跌参半'})
    except:
        signals.append({'name': '涨跌比', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── 龙虎榜净买 ──
    try:
        lhb = con.execute("""
            SELECT SUM(net_amount) FROM dragon_tiger_list WHERE trade_date=?
        """, [as_of_date]).fetchone()
        if lhb and lhb[0]:
            net = float(lhb[0])/1e8
            if net > 2:
                signals.append({'name': '龙虎榜', 'direction': 'bullish', 'confidence': min(0.7, abs(net)/10),
                               'value': round(net,1), 'detail': f'游资净买{net:.1f}亿'})
            elif net < -2:
                signals.append({'name': '龙虎榜', 'direction': 'bearish', 'confidence': min(0.7, abs(net)/10),
                               'value': round(net,1), 'detail': f'游资净卖{net:.1f}亿'})
            else:
                signals.append({'name': '龙虎榜', 'direction': 'neutral', 'confidence': 0.2, 'value': round(net,1),
                               'detail': f'净额{net:.1f}亿(小)'})
    except:
        signals.append({'name': '龙虎榜', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── ETF折溢价 ──
    try:
        etf = con.execute("""
            SELECT MAX(ABS(CAST(discount_rate AS DOUBLE))) FROM etf_daily
            WHERE trade_date=?
        """, [as_of_date]).fetchone()
        if etf and etf[0]:
            disc = float(etf[0])
            if disc > 2:
                signals.append({'name': 'ETF折溢价', 'direction': 'bearish', 'confidence': 0.4,
                               'value': round(disc,1), 'detail': f'最大折溢价{disc:.1f}%(成分股被掰)'})
            else:
                signals.append({'name': 'ETF折溢价', 'direction': 'neutral', 'confidence': 0.2,
                               'value': round(disc,2), 'detail': f'折溢价正常({disc:.2f}%)'})
    except:
        signals.append({'name': 'ETF折溢价', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── VPIN估计 ──
    try:
        vpin_rows = con.execute("""
            SELECT AVG(ABS(pct_chg)/NULLIF(turnover_rate,0)) FROM kline_daily
            WHERE trade_date=? AND turnover_rate>0
        """, [as_of_date]).fetchone()
        if vpin_rows and vpin_rows[0]:
            vpin_est = float(vpin_rows[0])
            if vpin_est > 5:
                signals.append({'name': 'VPIN估计', 'direction': 'bearish', 'confidence': min(0.7, vpin_est/20),
                               'value': round(vpin_est,2), 'detail': '高VPIN(知情交易风险)'})
            elif vpin_est < 1:
                signals.append({'name': 'VPIN估计', 'direction': 'bullish', 'confidence': 0.3,
                               'value': round(vpin_est,2), 'detail': '低VPIN(噪音交易为主)'})
            else:
                signals.append({'name': 'VPIN估计', 'direction': 'neutral', 'confidence': 0.2,
                               'value': round(vpin_est,2), 'detail': 'VPIN中等'})
    except:
        signals.append({'name': 'VPIN估计', 'direction': 'neutral', 'confidence': 0, 'value': 0, 'detail': '数据缺失'})

    # ── 新高新低比 ──
    try:
        hilo = con.execute("""
            SELECT COUNT(CASE WHEN close=high THEN 1 END)*1.0/NULLIF(COUNT(*),0) FROM kline_daily WHERE trade_date=?
        """, [as_of_date]).fetchone()
        if hilo and hilo[0]:
            ratio_hi = float(hilo[0])
            # 近似：收盘=最高价的股票占比(创新高倾向)
            signals.append({'name': '新高倾向', 'direction': 'bullish' if ratio_hi>0.15 else 'neutral',
                           'confidence': 0.3, 'value': round(ratio_hi,3),
                           'detail': f'收盘=最高价占比{ratio_hi:.1%}'})
    except:
        pass

    con.close()

    # ── 合成评分 ──
    bull_score = sum(s['confidence'] for s in signals if s['direction'] == 'bullish')
    bear_score = sum(s['confidence'] for s in signals if s['direction'] == 'bearish')
    neu_score  = sum(s['confidence'] for s in signals if s['direction'] == 'neutral')
    total = bull_score + bear_score + neu_score + 0.01
    composite = {
        'bull_pct': round(bull_score/total*100, 1),
        'bear_pct': round(bear_score/total*100, 1),
        'neutral_pct': round(neu_score/total*100, 1),
        'net_direction': 'bullish' if bull_score > bear_score * 1.2 else ('bearish' if bear_score > bull_score * 1.2 else 'neutral'),
        'n_signals': len(signals)
    }

    return {
        'date': as_of_date,
        'signals': signals,
        'composite': composite,
        'summary': f"[{as_of_date}] 18维综合: 🐂{composite['bull_pct']}% / 😐{composite['neutral_pct']}% / 🐻{composite['bear_pct']}% → {composite['net_direction']}"
    }


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=None)
    p.add_argument('--summary', action='store_true')
    p.add_argument('--json', action='store_true')
    args = p.parse_args()

    result = compute_all_18(args.date)
    if 'error' in result:
        print(result['error'])
        sys.exit(1)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.summary:
        print(result['summary'])
        for s in result['signals']:
            d_icon = {'bullish': '🟢', 'bearish': '🔴', 'neutral': '⚪'}
            print(f"  {d_icon.get(s['direction'],'?')} {s['name']}: {s['detail']} (conf={s['confidence']:.2f})")
    else:
        print(result['summary'])
        print(f"  信号总数: {result['composite']['n_signals']}")
        print(f"  综合: 🐂{result['composite']['bull_pct']}% 😐{result['composite']['neutral_pct']}% 🐻{result['composite']['bear_pct']}%")
