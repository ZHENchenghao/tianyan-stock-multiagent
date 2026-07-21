# -*- coding: utf-8 -*-
"""
T+1 熔断引擎 v2.0 — 三级防线
防线1: 冷却期 — 熔断后5日静默, 禁止任何开仓
防线2: 传染隔离 — CPO/板块联动, A熔断→B自动降仓
防线3: 跳空穿透 — 多级熔断(1.5%/3%/5%), 极端跳空集合竞价强平
"""
import sys, os, ssl, warnings, json, time
ssl._create_default_https_context = ssl._create_unverified_context
warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb, pandas as pd, numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

DB = 'D:/FreeFinanceData/data/duckdb/finance.db'
STATE_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / 'circuit_breaker_state.json'

# ============================================================
# 板块关联映射 (防线2: 传染隔离)
# ============================================================
SECTOR_GROUPS = {
    'CPO/光模块': ['300308.SZ', '300502.SZ', '300394.SZ'],
    'AI/芯片': ['688256.SH', '688041.SH', '688981.SH'],
    'AI/服务器': ['000977.SZ', '603019.SH'],
    '新能源/光伏': ['601012.SH', '688599.SH', '300274.SZ', '002129.SZ'],
    '新能源/锂电': ['300750.SZ', '002466.SZ', '002460.SZ', '300014.SZ'],
    '有色/黄金': ['600489.SH', '600547.SH', '002155.SZ'],
    '白酒': ['600519.SH', '000858.SZ', '000568.SZ', '002304.SZ'],
}

def get_sector(code):
    for sector, codes in SECTOR_GROUPS.items():
        if code in codes:
            return sector
    return None

def get_sector_peers(code):
    sector = get_sector(code)
    if sector:
        return [c for c in SECTOR_GROUPS[sector] if c != code]
    return []

# ============================================================
# 状态管理 (防线1: 冷却期)
# ============================================================
def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except: pass
    return {'cooldowns': {}, 'trigger_history': []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def is_in_cooldown(code):
    """检查某标的/板块是否在冷却期"""
    state = load_state()
    # 个股冷却
    cd = state['cooldowns'].get(code)
    if cd:
        cooldown_until = datetime.fromisoformat(cd['until'])
        if datetime.now() < cooldown_until:
            return True, cd['reason'], (cooldown_until - datetime.now()).days
    # 板块冷却 (传染)
    sector = get_sector(code)
    if sector:
        scd = state['cooldowns'].get(f'SECTOR:{sector}')
        if scd:
            cooldown_until = datetime.fromisoformat(scd['until'])
            if datetime.now() < cooldown_until:
                return True, f'板块{sector}熔断传染: {scd["reason"]}', (cooldown_until - datetime.now()).days
    return False, None, 0

def set_cooldown(code, reason, days=5):
    """设置冷却期 (防线1)"""
    state = load_state()
    until = datetime.now() + timedelta(days=days)
    state['cooldowns'][code] = {'until': until.isoformat(), 'reason': reason, 'set_at': datetime.now().isoformat()}
    # 防线2: 板块传染 — 同板块所有标的也进入冷却
    sector = get_sector(code)
    if sector:
        until_sector = datetime.now() + timedelta(days=days)
        state['cooldowns'][f'SECTOR:{sector}'] = {
            'until': until_sector.isoformat(),
            'reason': f'{code}触发熔断 → {sector}板块传染隔离',
            'set_at': datetime.now().isoformat()
        }
    # 记录触发历史
    state['trigger_history'].append({
        'code': code, 'reason': reason, 'sector': sector,
        'time': datetime.now().isoformat(), 'cooldown_days': days
    })
    # 只保留最近20条
    state['trigger_history'] = state['trigger_history'][-20:]
    save_state(state)

# ============================================================
# 防线3: 多级跳空熔断
# ============================================================
def check_gap_level(prev_close, today_open):
    """
    三级跳空熔断:
    Level 1: 跳空低开 >= 1.5% → 立即退出, 冷却3天
    Level 2: 跳空低开 >= 3.0% → 集合竞价强平, 冷却7天 + 板块传染
    Level 3: 跳空低开 >= 5.0% → 跌停排队平仓, 冷却10天 + 全板块隔离
    """
    if prev_close is None or today_open is None or prev_close <= 0:
        return None

    gap = (today_open / prev_close - 1) * 100

    if gap <= -5.0:
        return {
            'level': 3, 'gap_pct': round(gap, 2),
            'action': '跌停排队强平',
            'cooldown_days': 10,
            'contagion': True,
            'note': f'极端跳空低开{gap:.1f}% → Level 3熔断 → 跌停封单排队平仓 + 全板块隔离10天'
        }
    elif gap <= -3.0:
        return {
            'level': 2, 'gap_pct': round(gap, 2),
            'action': '集合竞价强平',
            'cooldown_days': 7,
            'contagion': True,
            'note': f'大幅跳空低开{gap:.1f}% → Level 2熔断 → 集合竞价强平 + 板块传染7天'
        }
    elif gap <= -1.5:
        return {
            'level': 1, 'gap_pct': round(gap, 2),
            'action': '立即退出',
            'cooldown_days': 3,
            'contagion': False,
            'note': f'跳空低开{gap:.1f}% → Level 1熔断 → 立即退出, 冷却3天'
        }
    elif gap > 1.0:
        return {
            'level': 0, 'gap_pct': round(gap, 2),
            'action': 'HOLD',
            'cooldown_days': 0,
            'contagion': False,
            'note': 'gap_up_reassess'
        }
    return None

# ============================================================
# 主检查
# ============================================================
def check_circuit_breakers():
    """T+1开盘熔断全量检查"""
    now = datetime.now()
    print(f"\n{'='*55}")
    print(f"  熔断引擎 v2.0 — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"  防线1: 5日冷却期 | 防线2: 板块传染 | 防线3: 三级跳空")
    print(f"{'='*55}")

    state = load_state()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    alerts = []

    # 扫描全部监控标的
    conn = duckdb.connect(DB, read_only=True)
    monitored = conn.execute("""
        SELECT DISTINCT ts_code FROM kline_minute
        UNION SELECT DISTINCT ts_code FROM kline_daily
        WHERE ts_code IN ('sh000688','sh000016','sh000300')
    """).fetchall()
    conn.close()

    for (code,) in monitored:
        # 防线1: 冷却期检查
        in_cd, cd_reason, cd_days = is_in_cooldown(code)
        if in_cd:
            alerts.append({
                'code': code, 'level': 'COOLDOWN',
                'action': f'静默中(剩余{cd_days}天)',
                'reason': cd_reason,
                'note': f'冷却期未结束 → 禁止任何开仓'
            })
            continue

        # 防线3: 跳空检查
        # 转换代码格式查kline_daily
        if code.endswith('.SZ'): daily_code = 'sz' + code.replace('.SZ', '')
        elif code.endswith('.SH'): daily_code = 'sh' + code.replace('.SH', '')
        else: daily_code = code

        conn2 = duckdb.connect(DB, read_only=True)
        rows = conn2.execute(f"""
            SELECT open, close FROM kline_daily WHERE ts_code='{daily_code}'
            ORDER BY trade_date DESC LIMIT 2
        """).fetchall()
        conn2.close()

        if len(rows) >= 2:
            prev_close = float(rows[1][1]) if rows[1][1] else 0  # 昨收
            today_open = float(rows[0][0]) if rows[0][0] else 0   # 今开(真开盘价!)
            gap_result = check_gap_level(prev_close, today_open)
            if gap_result and gap_result['level'] >= 1:
                alerts.append({'code': code, **gap_result})
                # 设置冷却期
                set_cooldown(code, gap_result['note'], gap_result['cooldown_days'])
                # 防线2: 传染 — 通知同板块
                if gap_result['contagion']:
                    peers = get_sector_peers(code)
                    if peers:
                        alerts.append({
                            'code': f'SECTOR:{get_sector(code)}',
                            'level': 'CONTAGION',
                            'action': f'板块传染: {len(peers)}只关联标的自动降仓',
                            'note': f'关联标的: {", ".join(peers[:5])}'
                        })

    # ====== 输出 ======
    if alerts:
        # 活跃熔断(非冷却期)
        active = [a for a in alerts if a.get('level') != 'COOLDOWN']
        cooldowns = [a for a in alerts if a.get('level') == 'COOLDOWN']

        if active:
            print(f"\n[ACTIVE] {len(active)} 项活跃熔断:")
            for a in active:
                print(f"  [{a.get('level','?')}] {a['code']}")
                print(f"        {a.get('note', a.get('reason',''))}")

        if cooldowns:
            print(f"\n[COOLDOWN] {len(cooldowns)} 项冷却中")
            for a in cooldowns[:3]:
                print(f"  {a['code']}: {a['reason'][:60]}")

        print(f"\n  活跃熔断: {len(active)} | 冷却中: {len(cooldowns)} | 板块隔离: {len([a for a in alerts if a.get('level')=='CONTAGION'])}")
        return True
    else:
        print(f"\n  无熔断信号。系统正常。")
        return False

def main():
    check_circuit_breakers()

if __name__ == '__main__':
    main()
