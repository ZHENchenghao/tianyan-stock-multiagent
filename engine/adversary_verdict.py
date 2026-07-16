# -*- coding: utf-8 -*-
"""天眼V8 敌情分析维度 — 集成adversarial_engine到日报
用法:
  from engine.adversary_verdict import adversary_verdict
  section = adversary_verdict(today_str)  # 返回日报markdown章节
"""
import sys, os, io, json, traceback
from datetime import date

# 添加AgentQuant路径
OUR = r'D:\AgentQuant\our'
if OUR not in sys.path:
    sys.path.insert(0, OUR)

def adversary_verdict(today_str=None):
    """
    运行敌情分析引擎, 返回日报markdown章节。

    Args:
        today_str: 'YYYY-MM-DD' 格式, None=今天

    Returns:
        str: markdown格式的敌情分析章节, 或空字符串(引擎不可用时)
    """
    if today_str is None:
        today_str = date.today().isoformat()

    try:
        from adversarial_engine import AdversaryDetective
    except ImportError:
        return '> ⚠ 敌情分析引擎不可用 (adversarial_engine not found)\n\n'

    try:
        det = AdversaryDetective(today_str)
        det.analyze_L1_manipulation()
        det.analyze_L2_capital_adversary()
        det.analyze_L3_institutional()
        det.analyze_L4_narrative()
        det.analyze_L5_recursive()
        det.analyze_L6_history()
        det.synthesize()
    except Exception as e:
        det.close() if hasattr(det, 'close') else None
        return f'> ⚠ 敌情分析引擎运行失败: {e}\n\n'

    score = det.manipulation_score
    traps = det.trap_signals
    i = det.today_i

    lines = []
    lines.append('## 🔍 敌情分析 (假设市场背后的人非常坏)')
    lines.append('')

    # ── 操纵分 ──
    if score >= 8:
        verdict = '🔴🔴 **强烈出货信号** — 主力在利用散户追高心理派发筹码'
        advice = '不追高, 已有仓位考虑减仓'
    elif score >= 4:
        verdict = '🔴 **出货嫌疑较明显** — 谨慎追高'
        advice = '观望, 等放量方向确认'
    elif score >= 1:
        verdict = '🟡 **轻微出货倾向**'
        advice = '中性偏谨慎'
    elif score <= -7:
        verdict = '🟢🟢 **强烈洗盘/吸筹信号** — 主力在利用恐慌收集筹码'
        advice = '不割肉, 等放量反弹确认, 中长期配置机会'
    elif score <= -3:
        verdict = '🟢 **洗盘/吸筹特征较明显**'
        advice = '偏多, 底部区域布局'
    elif score <= -1:
        verdict = '🟢 **轻微吸筹倾向**'
        advice = '中性偏多'
    else:
        verdict = '⚪ **无明确单边操纵** — 方向不明确'
        advice = '等待信号, 不操作'

    lines.append(f'**操纵嫌疑分**: {score:+d} → {verdict}')
    lines.append(f'**建议**: {advice}')
    lines.append(f'**状态**: HS300={det.all_closes[i]:.0f} | '
                 f'250日分位{det.pct_250[i]:.0f}% | '
                 f'MA60偏离{det.dev_ma60[i]:+.1f}%')
    lines.append('')

    # ── 陷阱列表 ──
    if traps:
        lines.append('**检测到的陷阱**:')
        for t in traps:
            icon = '🔴' if t['type'] in ('出货','诱多陷阱','诱多') else \
                   '🟢' if t['type'] in ('洗盘','吸筹','洗盘/吸筹','诱空') else '🟡'
            lines.append(f'- {icon} [{t["type"]}] {t["signal"]}')
        lines.append('')

    # ── 信号冲突 ──
    bull_traps = [t for t in traps if t['type'] in ('出货','诱多陷阱','诱多')]
    bear_traps = [t for t in traps if t['type'] in ('洗盘','吸筹','洗盘/吸筹','诱空')]
    if bull_traps and bear_traps:
        lines.append('**⚡ 信号冲突**:')
        bull_sigs = "; ".join(t["signal"][:30] for t in bull_traps[:2])
        bear_sigs = "; ".join(t["signal"][:30] for t in bear_traps[:2])
        lines.append(f'- 出货方: {len(bull_traps)}个信号 ({bull_sigs})')
        lines.append(f'- 吸筹方: {len(bear_traps)}个信号 ({bear_sigs})')
        lines.append(f'- 裁决: {"极端位置→K线量价优先" if det.pct_250[i] > 80 or det.pct_250[i] < 20 else "非极端→资金流优先"}')
        lines.append('')

    # ── 北向/融资关键数字 ──
    nb_5d = sum(det.nb_dict.get(det.all_dates[max(0,i-j)], 0) for j in range(5))
    nb_20d = sum(det.nb_dict.get(det.all_dates[max(0,i-j)], 0) for j in range(20))
    if abs(nb_5d) > 10 or abs(nb_20d) > 50:
        lines.append(f'**北向**: 5日{nb_5d:+.0f}亿 | 20日{nb_20d:+.0f}亿')
    import numpy as np
    if i >= 1250:
        cape_val = det.all_closes[i] / np.mean(det.all_closes[max(0,i-1249):i+1])
        lines.append(f'**CAPE**: {cape_val:.2f}')

    # ── 纠错线 ──
    lines.append('')
    lines.append('**纠错线**:')
    if score >= 3:
        lines.append('- 若5日内放量突破20日高点+北向转净买 → 出货判断错误, 纠错追入')
    elif score <= -3:
        lines.append('- 若跌破今日低点+北向持续净卖 → 洗盘判断错误, 纠错止损')
    else:
        lines.append('- 若北向3日累计方向反转>30亿 → 当前中性判断需重新评估')

    lines.append('')
    det.close()
    return '\n'.join(lines)


# ═══ 自测 ═══
if __name__ == '__main__':
    import numpy as np
    # 需要先import adversarial_engine确保stdout被正确设置
    report = adversary_verdict('2026-06-12')
    print(report)
