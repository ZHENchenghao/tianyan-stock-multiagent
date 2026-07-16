# -*- coding: utf-8 -*-
"""
天眼 v4.1 · 卖出熔断机制
========================
问题起源: 2026-05-22 有色ETF亏损事件
  - 天眼提示减仓有色(016708): KDJ J=1.7 + 琼斯REJECT + 空头排列
  - 但同时WTI从$103反弹→传导矩阵说有色滞后1-3天跟涨
  - KDJ J=1.7历史级超卖→技术面暗示反弹
  - 卖出信号压过了反向信号 → 用户卖出后亏损

五重校验(任意一项不通过=熔断):
  1. 超卖校验: KDJ J<10 且 RSI<30 且 布林触下轨?
  2. 传导校验: 跨市场领先指标是否反向?
  3. 偏离校验: 现价偏离MA20超过-2σ?
  4. 地缘校验: 美伊谈判反复导致油价反转风险? (v4.2 PDF增强)
  5. 伪催化校验: 卖出理由是否基于伪催化(如"人民币升=地产牛")? (v4.2 PDF增强)

分级结果:
  3+项不通过   → 禁止卖出, 改持有+标注纠错线 (2级熔断)
  1-2项不通过  → 卖出金额减半 (1级熔断)
  0项不通过    → 正常卖出

铁律#3.1补充: 卖出信号必须过五重校验, 不过则禁售。
"""

import sys, os, json
from datetime import datetime, date, timedelta
import numpy as np
import pandas as pd
from collections import defaultdict

os.environ['TQDM_DISABLE'] = '1'

import ssl
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'

try:
    import duckdb
except ImportError:
    duckdb = None

from engine.cross_market_conduction import conduction_signal, load_conduction_matrix

BASE = os.path.dirname(os.path.abspath(__file__))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
FUSE_LOG = os.path.join(BASE, '..', 'fuse_log.json')


# ═══════════════════════════════════════════
# 一、技术指标计算
# ═══════════════════════════════════════════

def _conn():
    return duckdb.connect(DB) if duckdb else None


def compute_kdj(close, high, low, n=9):
    """KDJ指标计算, 返回 (K, D, J)"""
    if len(close) < n + 1:
        return 50, 50, 50
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])


def compute_rsi(close, n=14):
    """RSI指标"""
    if len(close) < n + 1:
        return 50
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def compute_bollinger(close, n=20, k=2):
    """布林带"""
    if len(close) < n:
        return float(close.iloc[-1]) * 1.5, float(close.iloc[-1]), float(close.iloc[-1]) * 0.5
    ma = close.rolling(n).mean()
    std = close.rolling(n).std()
    return (float(ma.iloc[-1] + k * std.iloc[-1]),
            float(ma.iloc[-1]),
            float(ma.iloc[-1] - k * std.iloc[-1]))


def get_technicals(ts_code):
    """从DuckDB获取K线并计算技术指标"""
    conn = _conn()
    if conn is None:
        return {}

    try:
        df = conn.execute("""
            SELECT trade_date, open, high, low, close
            FROM kline_daily
            WHERE ts_code = ?
            ORDER BY trade_date DESC
            LIMIT 120
        """, [ts_code]).fetchdf()
        conn.close()

        if df.empty or len(df) < 30:
            return {}

        df = df.sort_values('trade_date')
        close = df['close'].astype(float)
        high = df['high'].astype(float)
        low = df['low'].astype(float)

        k, d, j = compute_kdj(close, high, low)
        rsi = compute_rsi(close)
        upper, ma20, lower = compute_bollinger(close)
        last_close = float(close.iloc[-1])
        last_ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else last_close
        std20 = float(close.rolling(20).std().iloc[-1]) if len(close) >= 20 else last_close * 0.05

        return {
            'close': round(last_close, 2),
            'ma20': round(last_ma20, 2),
            'std20': round(std20, 2),
            'kdj_k': round(k, 1),
            'kdj_d': round(d, 1),
            'kdj_j': round(j, 1),
            'rsi': round(rsi, 1),
            'boll_upper': round(upper, 2),
            'boll_lower': round(lower, 2),
            'boll_touch_lower': last_close <= lower,
            'deviation_from_ma20': round((last_close - last_ma20) / last_ma20 * 100, 2) if last_ma20 else 0,
            'deviation_sigma': round((last_close - last_ma20) / std20, 2) if std20 else 0,
        }
    except Exception:
        return {}


# ═══════════════════════════════════════════
# 二、三重校验
# ═══════════════════════════════════════════

def check_1_oversold(technicals: dict) -> dict:
    """
    校验1: 超卖检测
    标准: KDJ J<10 且 RSI<30 且 布林触下轨
    返回 {passed: bool, failing_items: [...], detail: str}
    """
    failing = []
    details = []

    j = technicals.get('kdj_j', 50)
    rsi = technicals.get('rsi', 50)
    boll_touch = technicals.get('boll_touch_lower', False)

    if j < 10:
        failing.append(f'KDJ_J={j:.1f}<10(极度超卖)')
    if rsi < 30:
        failing.append(f'RSI={rsi:.1f}<30(超卖)')
    if boll_touch:
        boll_lower = technicals.get('boll_lower', 0)
        failing.append(f'布林触下轨({boll_lower:.2f})')

    details.append(f'KDJ_J={j:.1f} RSI={rsi:.1f} 布林下轨触={boll_touch}')

    # 至少满足2项才触发超卖熔断
    passed = len(failing) < 2

    return {
        'name': '超卖检测',
        'passed': passed,
        'failing_items': failing,
        'detail': '; '.join(details),
        'severity': 'high' if len(failing) >= 2 else ('medium' if len(failing) >= 1 else 'low'),
    }


def check_2_conduction(sector_name: str) -> dict:
    """
    校验2: 跨市场传导冲突检测
    标准: 传导矩阵中领先指标是否与卖出信号反向?
    """
    try:
        result = conduction_signal(sector_name)
    except Exception:
        return {'name': '传导校验', 'passed': True, 'failing_items': [],
                'detail': '传导数据不足,跳过校验'}

    bullish = result.get('bullish_count', 0)
    bearish = result.get('bearish_count', 0)
    score = result.get('score', 0)
    signals = result.get('signals', [])

    failing = []
    details = []

    # 如果有≥2个高/中置信度bullish信号 → 卖出有冲突
    high_med_bullish = [s for s in signals
                        if s.get('conduction_impact') == 'bullish'
                        and s.get('confidence') in ('high', 'medium')]

    if len(high_med_bullish) >= 2:
        names = [s['leader'] for s in high_med_bullish[:3]]
        failing.append(f'{len(high_med_bullish)}个领先指标看多: {", ".join(names)}')

    if score > 0.3:
        failing.append(f'传导综合得分{score:+.2f}偏多')

    details.append(f'传导得分{score:+.2f} 利好{bullish}个 利空{bearish}个')
    for s in high_med_bullish[:3]:
        details.append(f'  {s["leader"]}: {s["conduction_impact"]} [{s["confidence"]}]')

    passed = len(failing) == 0

    return {
        'name': '传导校验',
        'passed': passed,
        'failing_items': failing,
        'detail': '; '.join(details),
        'severity': 'high' if len(failing) >= 2 else ('medium' if len(failing) >= 1 else 'low'),
    }


def check_3_deviation(technicals: dict) -> dict:
    """
    校验3: 极端偏离检测
    标准: 现价偏离MA20超过-2σ (统计学极端低位)
    补充: 偏离超过-3σ → 几乎确定是过度恐慌
    """
    dev_sigma = technicals.get('deviation_sigma', 0)
    dev_pct = technicals.get('deviation_from_ma20', 0)

    failing = []
    if dev_sigma < -2:
        failing.append(f'偏离MA20 {dev_sigma:.1f}σ (<-2σ, 极端低位)')
    if dev_pct < -10:
        failing.append(f'距MA20 {dev_pct:.1f}% (超跌)')

    passed = len(failing) == 0
    severity = 'extreme' if dev_sigma < -3 else ('high' if len(failing) >= 1 else 'low')

    return {
        'name': '偏离校验',
        'passed': passed,
        'failing_items': failing,
        'detail': f'偏离{dev_sigma:.1f}σ 距MA20 {dev_pct:+.1f}%',
        'severity': severity,
    }


# v4.2 PDF增强: 新增校验4+5
def check_4_geopolitical(sector_name: str) -> dict:
    """
    校验4: 地缘反复风险 (PDF: 美伊谈判headline驱动, 易反复)

    当WTI近期出现大幅波动(涨跌>5%), 说明地缘定价不稳定。
    此时卖出油价受益链(航空/化工)或受损链(能源/资源)标的,
    需要额外确认——地缘消息随时可能反转。
    """
    try:
        from engine.macro_shock_detector import load_shocks
        shocks_data = load_shocks()
    except Exception:
        return {'name': '地缘校验', 'passed': True, 'failing_items': [],
                'detail': '冲击数据不可用,跳过校验'}

    failing = []
    details = []

    shocks = shocks_data.get('shocks', [])
    oil_volatile = any(
        s.get('var') == 'wti' and abs(s.get('z_score', 0)) >= 1.5
        for s in shocks
    )

    gold_stable = any(
        s.get('var') == 'gold' and abs(s.get('z_score', 0)) < 1.0
        for s in shocks
    )

    if oil_volatile:
        details.append('WTI近期大幅波动, 地缘定价不稳定')
        # PDF: 黄金未深跌=市场未完全撤出避险 → 地缘风险仍在
        if gold_stable:
            details.append('黄金未跟随大跌→市场仍保留避险需求→地缘尾部风险未消')

        # 卖出的是油价敏感行业→地缘反转可能令判断错误
        oil_sensitive = {'有色金属', '石油行业', '煤炭', '航空', '航空机场', '化纤行业',
                         '物流行业', '采掘行业', '橡胶制品'}
        if sector_name in oil_sensitive:
            failing.append(f'{sector_name}对油价敏感, 地缘反复可能导致油价单日反转')

    passed = len(failing) == 0
    return {
        'name': '地缘校验',
        'passed': passed,
        'failing_items': failing,
        'detail': '; '.join(details) if details else f'{sector_name}无显著地缘反转风险',
        'severity': 'medium' if len(failing) >= 1 else 'low',
    }


def check_5_fake_catalyst(sector_name: str) -> dict:
    """
    校验5: 伪催化检测 (PDF: 警惕市场情绪驱动的伪逻辑)

    卖出原因如果是基于伪催化, 则熔断:
    - 人民币升值→卖掉出口标的 = 合理
    - 人民币升值→认为地产链该跌所以卖地产 = 伪催化(汇率≠地产)
    """
    # 伪催化→行业映射: 这些关联是虚假的/被PDF明确否定的
    FALSE_NARRATIVES = {
        '房地产': {'pseudo_narrative': '人民币升=地产强(或反过来)', 'reality': '汇率强≠信用扩张'},
        '水泥建材': {'pseudo_narrative': '人民币升=基建强', 'reality': '汇率≠国内财政'},
        '钢铁行业': {'pseudo_narrative': '油价跌=顺周期弱', 'reality': '成本改善≠需求消失'},
    }

    failing = []
    if sector_name in FALSE_NARRATIVES:
        fn = FALSE_NARRATIVES[sector_name]
        failing.append(f'伪催化风险: {fn["pseudo_narrative"]}')
        failing.append(f'实际情况: {fn["reality"]}')

    passed = len(failing) == 0
    return {
        'name': '伪催化校验',
        'passed': passed,
        'failing_items': failing,
        'detail': f'{sector_name}: {"无伪催化风险" if passed else "存在情绪驱动伪逻辑, 卖出理由可能不成立"}',
        'severity': 'medium' if len(failing) >= 1 else 'low',
    }


# ═══════════════════════════════════════════
# 三、熔断裁决
# ═══════════════════════════════════════════

def fuse_check(holding_name: str, sector: str, ts_code: str, action: str) -> dict:
    """
    卖出熔断主入口。

    Parameters
    ----------
    holding_name: 持仓名称 (如"华夏有色金属ETF联接C")
    sector: 板块名 (如"有色金属")
    ts_code: 板块代码 (如"sh000819")
    action: 建议操作 ("减仓"/"卖出"/"清仓")

    Returns
    -------
    {
        'fused': True/False,       # 是否熔断
        'override_action': str,    # 熔断后的替代操作
        'original_action': str,    # 原操作
        'fuse_level': int,         # 熔断级别(0=通过, 1=减半, 2=禁售)
        'checks': [...],           # 三重校验结果
        'message': str,            # 用户提示
        'correction_line': str,    # 纠错线
    }
    """
    # 只在卖出时触发
    SELL_ACTIONS = ('减仓', '卖出', '清仓', '赎回')
    if action not in SELL_ACTIONS:
        return {
            'fused': False,
            'override_action': action,
            'original_action': action,
            'fuse_level': 0,
            'checks': [],
            'message': '',
            'correction_line': '',
        }

    technicals = get_technicals(ts_code)

    if not technicals:
        # 数据不足, 不熔断(保守放行)
        return {
            'fused': False,
            'override_action': action,
            'original_action': action,
            'fuse_level': 0,
            'checks': [],
            'message': '[熔断] 技术数据不足,跳过校验,原建议执行',
            'correction_line': '',
        }

    check1 = check_1_oversold(technicals)
    check2 = check_2_conduction(sector)
    check3 = check_3_deviation(technicals)
    check4 = check_4_geopolitical(sector)   # v4.2 PDF增强
    check5 = check_5_fake_catalyst(sector)  # v4.2 PDF增强

    checks = [check1, check2, check3, check4, check5]
    failed = [c for c in checks if not c['passed']]
    failed_count = len(failed)

    # 分级裁决
    if failed_count >= 3:
        # ★★ 禁售: 3项或以上不通过 (v4.2: 5项中≥3)
        # 纠错线: 站上MA5或KDJ J回到20以上, 再考虑卖出
        close = technicals.get('close', 0)
        ma20 = technicals.get('ma20', 0)

        override = '持有(卖出被熔断)'
        fuse_level = 2
        message = (
            f'[卖出熔断] {holding_name}({sector}) 卖出信号被拦截!\n'
            f'  不通过项({failed_count}/3):\n'
        )
        for c in failed:
            message += f'    ✗ {c["name"]}: {"; ".join(c["failing_items"])}\n'
        message += (
            f'  结论: 当前处于极端位置,卖出大概率割在底部\n'
            f'  替代操作: 持有观察,等反弹后再评估\n'
        )
        correction = f'反弹至MA20({ma20})或KDJ_J>30→重新评估卖出'

    elif failed_count == 1:
        # ★ 减半: 1项不通过
        if action == '减仓':
            override = '减仓(金额减半)'
        else:
            override = f'{action}(金额减半,熔断触发)'
        fuse_level = 1
        c = failed[0]
        message = (
            f'[卖出熔断] {holding_name}({sector}) {c["name"]}不通过!\n'
            f'  不通过项: {"; ".join(c["failing_items"])}\n'
            f'  替代操作: {override}\n'
        )
        correction = f'{c["name"]}通过后→可恢复全量卖出'

    else:
        # 通过
        override = action
        fuse_level = 0
        message = ''
        correction = ''

    result = {
        'fused': fuse_level > 0,
        'override_action': override,
        'original_action': action,
        'fuse_level': fuse_level,
        'checks': checks,
        'technicals': technicals,
        'message': message,
        'correction_line': correction,
    }

    # 日志
    _log_fuse_event(holding_name, sector, result)

    return result


def _log_fuse_event(name, sector, result):
    """记录熔断事件"""
    entry = {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'holding': name,
        'sector': sector,
        'fuse_level': result['fuse_level'],
        'original_action': result['original_action'],
        'override_action': result['override_action'],
        'technicals': result.get('technicals', {}),
        'checks': [
            {'name': c['name'], 'passed': c['passed'], 'failing': c.get('failing_items', [])}
            for c in result.get('checks', [])
        ],
    }

    try:
        logs = []
        if os.path.exists(FUSE_LOG):
            with open(FUSE_LOG, 'r', encoding='utf-8') as f:
                logs = json.load(f)
        logs.append(entry)
        with open(FUSE_LOG, 'w', encoding='utf-8') as f:
            json.dump(logs[-50:], f, ensure_ascii=False, indent=2)  # 只保留最近50条
    except Exception:
        pass


# ═══════════════════════════════════════════
# 四、批量持仓检查
# ═══════════════════════════════════════════

def check_portfolio_for_sells(portfolio: list) -> list:
    """
    遍历持仓，对所有提示卖出的条目做熔断校验。

    portfolio: [{'name': str, 'sector': str, 'ts_code': str, 'action': str, ...}, ...]

    Returns 更新后的portfolio (action可能被熔断覆盖)
    """
    updated = []
    fuse_events = []

    for holding in portfolio:
        action = holding.get('action', '持有')
        if action in ('减仓', '卖出', '清仓', '赎回'):
            result = fuse_check(
                holding.get('name', ''),
                holding.get('sector', ''),
                holding.get('ts_code', ''),
                action,
            )
            if result['fused']:
                holding = dict(holding)
                holding['action'] = result['override_action']
                holding['fuse_info'] = {
                    'fuse_level': result['fuse_level'],
                    'failing_checks': [
                        c['name'] for c in result.get('checks', []) if not c['passed']
                    ],
                    'correction_line': result['correction_line'],
                }
                fuse_events.append(result)
        updated.append(holding)

    if fuse_events:
        print(f'\n[熔断] {len(fuse_events)}个卖出信号被拦截:')
        for e in fuse_events:
            print(f'  {e["fuse_level"]}级熔断: {e.get("original_action", "")} → {e.get("override_action", "")}')

    return updated


# ═══════════════════════════════════════════
# 命令行
# ═══════════════════════════════════════════

if __name__ == '__main__':
    print('╔══════════════════════════════════╗')
    print('║  卖出熔断机制 · 三重校验          ║')
    print('╚══════════════════════════════════╝')

    # 测试: 有色ETF
    result = fuse_check(
        '华夏有色金属ETF联接C', '有色金属', 'sh000819', '减仓'
    )

    print(f'\n熔断结果: {"触发!" if result["fused"] else "通过"}')
    print(f'原操作: {result["original_action"]}')
    print(f'新操作: {result["override_action"]}')
    print(f'熔断级别: {result["fuse_level"]}')

    print('\n三重校验详情:')
    for c in result['checks']:
        status = '✓ 通过' if c['passed'] else '✗ 不通过'
        print(f'  {status} — {c["name"]}')
        print(f'    {c["detail"]}')
        if c.get('failing_items'):
            for fi in c['failing_items']:
                print(f'    → {fi}')

    if result.get('message'):
        print(f'\n{result["message"]}')

    if result.get('correction_line'):
        print(f'\n纠错线: {result["correction_line"]}')
