# -*- coding: utf-8 -*-
"""
天眼全量数据采集器 v5.0
======================
数据源: Sina财经 (全历史日线, ~0.5s/只)
存储: DuckDB @ D:\FreeFinanceData\data\finance_full.db
速度: 单线程+间隔, ~30-40只/分, 5252只≈3小时

用法:
  python engine/collector_full.py              # 全量
  python engine/collector_full.py --max 100    # 测试
  python engine/collector_full.py --resume     # 续传
  python engine/collector_full.py --verify     # 验证
"""

import sys, os, io, json, time, re
from datetime import datetime, date, timedelta

if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

import urllib.request
import socket as sock
import duckdb
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DB_PATH = r'D:\FreeFinanceData\data\finance_full.db'
STOCK_LIST_FILE = os.path.join(BASE, 'stock_list_sina.json')
PROGRESS_FILE = os.path.join(BASE, 'collector_full_progress.json')

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Referer': 'https://finance.sina.com.cn'}

# ═══════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════

def get_conn():
    conn = duckdb.connect(DB_PATH)
    conn.execute('SET threads=1')
    return conn

def init_database():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kline_daily (
            ts_code VARCHAR, trade_date DATE,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE,
            change_pct DOUBLE,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collector_log (
            id INTEGER PRIMARY KEY,
            task VARCHAR, status VARCHAR,
            total_stocks INTEGER, completed_stocks INTEGER, total_rows INTEGER,
            start_time TIMESTAMP, end_time TIMESTAMP
        )
    """)
    conn.close()
    print(f'[DB] {DB_PATH}')


# ═══════════════════════════════════════════
# K线获取 (Sina, 单线程极速)
# ═══════════════════════════════════════════

def fetch_kline_sina(sina_code):
    """Sina日线K线, 返回 [{trade_date,open,high,low,close,volume,change_pct}]"""
    endpoints = [
        f'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen=10000',
        f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen=10000',
    ]
    for url in endpoints:
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(2)
                req = urllib.request.Request(url, headers=HEADERS)
                resp = urllib.request.urlopen(req, timeout=10)
                raw = resp.read().decode('gbk', errors='replace')
                data = json.loads(raw)
                if not isinstance(data, list) or len(data) == 0:
                    return None

                result = []
                prev_close = None
                for row in data:
                    try:
                        close_val = float(row['close'])
                        if prev_close and prev_close > 0:
                            change_pct = round((close_val / prev_close - 1) * 100, 2)
                        else:
                            change_pct = None
                        prev_close = close_val
                        result.append({
                            'trade_date': row['day'],
                            'open': float(row['open']),
                            'high': float(row['high']),
                            'low': float(row['low']),
                            'close': close_val,
                            'volume': float(row['volume']),
                            'change_pct': change_pct,
                        })
                    except (KeyError, ValueError):
                        continue
                return result if result else None
            except Exception:
                if attempt == 2:
                    break  # 换endpoint
    return None


# ═══════════════════════════════════════════
# 批量写入 (逐行避免GIL)
# ═══════════════════════════════════════════

def bulk_insert(conn, ts_code, klines):
    if not klines:
        return 0
    # 用DataFrame批量写入, 绕过executemany的GIL问题
    data = []
    for k in klines:
        data.append({
            'ts_code': ts_code,
            'trade_date': k['trade_date'],
            'open': k['open'],
            'high': k['high'],
            'low': k['low'],
            'close': k['close'],
            'volume': k['volume'],
            'change_pct': k.get('change_pct'),
        })
    df = pd.DataFrame(data)
    conn.register('_tmp_batch', df)
    conn.execute("""
        INSERT OR REPLACE INTO kline_daily
        SELECT * FROM _tmp_batch
    """)
    conn.unregister('_tmp_batch')
    return len(klines)


# ═══════════════════════════════════════════
# 进度管理
# ═══════════════════════════════════════════

def save_progress(completed_codes):
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'task': 'collect_a_v5',
            'completed': completed_codes,
            'updated_at': datetime.now().isoformat(),
        }, f, ensure_ascii=False, indent=2)

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            p = json.load(f)
            return p.get('completed', [])
    return []


# ═══════════════════════════════════════════
# 主采集
# ═══════════════════════════════════════════

def collect_all(max_stocks=0, resume=True):
    init_database()

    with open(STOCK_LIST_FILE, 'r', encoding='utf-8') as f:
        all_stocks = json.load(f)
    print(f'[列表] {len(all_stocks)}只A股')

    completed = set(load_progress()) if resume else set()
    if completed:
        print(f'[进度] 已采{len(completed)}只')

    target_stocks = all_stocks[:max_stocks] if max_stocks > 0 else all_stocks
    pending = [s for s in target_stocks if s['ts_code'] not in completed]
    print(f'[任务] {len(pending)}/{len(target_stocks)}只')

    conn = get_conn()
    start_time = time.time()
    ok, fail, skip = 0, 0, 0
    total_rows = 0
    batch_codes, batch_data = [], {}

    for stock in pending:
        code = stock['ts_code']
        name = stock.get('name', '')

        if re.search(r'[^a-z0-9]', code, re.I):
            skip += 1
            continue

        klines = fetch_kline_sina(code)
        if klines is None:
            fail += 1
            if fail <= 10:
                print(f'  [FAIL] {code} {name[:30]}')
            time.sleep(0.5)
            continue

        batch_codes.append(code)
        batch_data[code] = klines
        total_rows += len(klines)

        if len(batch_codes) >= 50:
            for c in batch_codes:
                bulk_insert(conn, c, batch_data[c])
            conn.commit()
            ok += len(batch_codes)
            completed.update(batch_codes)
            save_progress(list(completed))

            elapsed = time.time() - start_time
            rate = ok / elapsed * 60
            eta = (len(pending) - ok - fail) / max(rate, 0.01)
            print(f'  [{ok}/{len(pending)}] {code} {name[:20]} '
                  f'({len(klines)}条) | {rate:.0f}只/分 | ETA {eta:.0f}分')

            batch_codes, batch_data = [], {}
            time.sleep(0.5)  # 防止Sina限流

        # 每只间隔, 稳在2只/秒以下
        time.sleep(0.3)

    # 最后一批
    if batch_codes:
        for c in batch_codes:
            bulk_insert(conn, c, batch_data[c])
        conn.commit()
        ok += len(batch_codes)
        completed.update(batch_codes)
        save_progress(list(completed))

    conn.execute("""
        INSERT INTO collector_log VALUES (?, 'collect_a_v5', 'completed', ?, ?, ?, ?, ?)
    """, (int(time.time()), len(target_stocks), ok, total_rows,
          datetime.fromtimestamp(start_time).isoformat(), datetime.now().isoformat()))
    conn.close()

    elapsed = (time.time() - start_time) / 60
    rate = ok / elapsed if elapsed > 0 else 0
    print(f'\n[完成] {ok}成功 {fail}失败 {skip}跳过 | {total_rows:,}条 | {elapsed:.0f}分 | {rate:.0f}只/分')


# ═══════════════════════════════════════════
# 验证
# ═══════════════════════════════════════════

def verify_data():
    conn = get_conn()
    print('=' * 64)
    print('  数据完整性验证')
    print('=' * 64)

    n = conn.execute('SELECT COUNT(DISTINCT ts_code) FROM kline_daily').fetchone()[0]
    total = conn.execute('SELECT COUNT(*) FROM kline_daily').fetchone()[0]
    dr = conn.execute('SELECT MIN(trade_date), MAX(trade_date) FROM kline_daily').fetchone()
    print(f'\n总标的: {n}只 | 总K线: {total:,}条')
    print(f'日期范围: {dr[0]} ~ {dr[1]}')

    yrs = conn.execute("""
        SELECT LEFT(CAST(trade_date AS VARCHAR),4) yr, COUNT(DISTINCT ts_code) stocks, COUNT(*) cnt
        FROM kline_daily GROUP BY 1 ORDER BY 1
    """).fetchall()
    print(f'\n年度覆盖:')
    for y in yrs:
        bar = '#' * max(1, int(y[2] / 5000))
        print(f'  {y[0]}: {y[1]:>5}只 {y[2]:>9,}条 {bar}')

    recent = (date.today() - timedelta(days=5)).isoformat()
    fresh = conn.execute(f"""
        SELECT COUNT(DISTINCT ts_code) FROM kline_daily WHERE trade_date >= '{recent}'
    """).fetchone()[0]
    print(f'\n5天内更新: {fresh}/{n}只')
    conn.close()


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='天眼全量数据采集器 v5.0')
    p.add_argument('--max', type=int, default=0)
    p.add_argument('--resume', action='store_true', default=True)
    p.add_argument('--fresh', action='store_true')
    p.add_argument('--verify', action='store_true')
    args = p.parse_args()

    if args.verify:
        verify_data()
    else:
        print('=' * 64)
        print('  天眼全量数据采集器 v5.0')
        print(f'  数据源: Sina财经 (全历史日线, 单线程速)')
        print(f'  存储: {DB_PATH}')
        print(f'  速度: ~30-40只/分, 预估3小时')
        print('=' * 64)

        resume = not args.fresh
        collect_all(max_stocks=args.max, resume=resume)
        verify_data()
