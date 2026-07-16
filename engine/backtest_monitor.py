# -*- coding: utf-8 -*-
"""
天眼 v8 10年回测 · 三陷阱监控器
=================================
嵌入回测主循环, 每个交易日收盘后记录异常事件。

陷阱1: 战法层 vs HRP 的"基因冲突"
  - 监控: 战法选出的高动量标的被HRP压到极低权重(<2%)的频次
  - 危险信号: 牛市中此频次 > 50% → λ因子需要动态调校

陷阱2: 500元门槛的"慢性失血"
  - 监控: 因低于500元被拒绝执行的订单占比
  - 危险信号: 占比 > 30% → 门槛太高, 调仓精度被蚕食

陷阱3: 停牌复牌后的"记忆断层"
  - 监控: EMA平滑后的权重与当前NAV权重的偏离度
  - 危险信号: 偏离 > 50% → 记忆断层, 可能产生畸形执行目标

用法:
  from engine.backtest_monitor import BacktestMonitor
  mon = BacktestMonitor()
  mon.record_trap1(date, high_momentum_stocks, hrp_weights)
  mon.record_trap2(date, rejected_orders, total_orders)
  mon.record_trap3(date, ema_weights, nav_weights)
  mon.summary()  # 回测结束后输出
"""

import json, os
from datetime import date, datetime
from typing import Dict, List, Optional
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
MONITOR_LOG = os.path.join(PROJECT_DIR, 'reports', 'backtest_monitor.json')


class BacktestMonitor:
    """三陷阱监控器 — 嵌在回测主循环中, 每日收盘记录"""

    def __init__(self):
        self.trap1_events = []    # 基因冲突事件
        self.trap2_rejected = []  # 500元门槛拒绝记录
        self.trap3_diverged = []  # 权重偏离记录
        self.daily_nav = []       # 每日NAV快照
        self.drawdown_duration = 0  # 当前回撤持续天数
        self.max_drawdown_duration = 0
        self.peak_nav = 0.0
        self._start_date = None

    # ── 陷阱1: 基因冲突 ──────────────────────────

    def record_trap1(self, trade_date, survivor_pool: List[str],
                     hrp_weights: Dict[str, float],
                     strategy_scores: Dict[str, float]):
        """
        检测: 战法层高分标的被HRP压到<2%权重。

        survivor_pool: 战法层今日存活信号 (List[code])
        hrp_weights:   HRP输出的目标权重
        strategy_scores: 战法层对各标的的评分 {code: score}
        """
        if not survivor_pool or not hrp_weights:
            return

        # 战法层TOP3 (得分最高的)
        sorted_by_score = sorted(strategy_scores.items(), key=lambda x: -x[1])
        top3 = [c for c, _ in sorted_by_score[:3] if c in hrp_weights]

        suppressed = []
        for code in top3:
            w = hrp_weights.get(code, 0.0)
            if w < 0.02:
                suppressed.append({
                    'code': code,
                    'strategy_score': strategy_scores.get(code, 0),
                    'hrp_weight': w,
                })

        if suppressed:
            event = {
                'date': str(trade_date)[:10],
                'type': 'trap1_gene_conflict',
                'top3_suppressed': suppressed,
                'count': len(suppressed),
                'severity': 'CRITICAL' if len(suppressed) >= 3 else ('WARN' if len(suppressed) >= 2 else 'INFO'),
            }
            self.trap1_events.append(event)

    # ── 陷阱2: 500元门槛 ──────────────────────────

    def record_trap2(self, trade_date, rejected_orders: List[dict],
                     executed_orders: List[dict]):
        """
        检测: 因低于500元被拒绝的订单占比。

        rejected_orders: [{'code','action','shares','amount','reason':'BELOW_MIN'}, ...]
        """
        total = len(rejected_orders) + len(executed_orders)
        if total == 0:
            return

        rejected_count = len(rejected_orders)
        ratio = rejected_count / total

        event = {
            'date': str(trade_date)[:10],
            'type': 'trap2_min_amount',
            'rejected': rejected_count,
            'executed': len(executed_orders),
            'total': total,
            'ratio': round(ratio, 4),
            'severity': 'CRITICAL' if ratio > 0.4 else ('WARN' if ratio > 0.25 else 'INFO'),
            'details': rejected_orders[:5],  # 只保留前5条避免日志爆炸
        }
        self.trap2_rejected.append(event)

    # ── 陷阱3: 记忆断层 ──────────────────────────

    def record_trap3(self, trade_date,
                     ema_weights: Dict[str, float],
                     nav_weights: Dict[str, float],
                     positions: dict):
        """
        检测: EMA记忆权重 vs NAV真实权重的偏离度。

        ema_weights: 从hrp_state.json继承+EMA平滑后的权重
        nav_weights:  当前真实持仓 ÷ NAV 的实际权重

        偏离度 = Σ|ema_w - nav_w| / 2  (单边换手率等价)
        危险阈值: > 0.50 = 权重偏移超过50%
        """
        all_codes = set(ema_weights.keys()) | set(nav_weights.keys())
        if not all_codes:
            return

        divergence = 0.0
        anomalies = []
        for code in all_codes:
            ew = ema_weights.get(code, 0.0)
            nw = nav_weights.get(code, 0.0)
            diff = abs(ew - nw)
            divergence += diff
            if diff > 0.15:  # 单只偏离 > 15%
                anomalies.append({
                    'code': code,
                    'ema_weight': round(ew, 4),
                    'nav_weight': round(nw, 4),
                    'diff': round(diff, 4),
                })

        divergence /= 2.0  # 双边换手率等价

        if divergence > 0.30 or anomalies:
            event = {
                'date': str(trade_date)[:10],
                'type': 'trap3_memory_gap',
                'divergence': round(divergence, 4),
                'anomalies': anomalies[:10],
                'positions_count': len(positions),
                'severity': 'CRITICAL' if divergence > 0.50 else ('WARN' if divergence > 0.30 else 'INFO'),
            }
            self.trap3_diverged.append(event)

    # ── NAV追踪 & 回撤持续期 ──────────────────────

    def record_nav(self, trade_date, nav: float):
        """每日NAV快照, 跟踪最大回撤持续期"""
        d = str(trade_date)[:10]
        self.daily_nav.append({'date': d, 'nav': round(nav, 2)})

        if self._start_date is None:
            self._start_date = d

        # 更新峰值
        if nav > self.peak_nav:
            self.peak_nav = nav
            self.drawdown_duration = 0
        else:
            self.drawdown_duration += 1
            self.max_drawdown_duration = max(
                self.max_drawdown_duration, self.drawdown_duration
            )

    # ── 回测结束汇总 ──────────────────────────────

    def summary(self) -> dict:
        """回测结束后输出完整监控报告"""
        n_days = len(self.daily_nav)

        # 陷阱1统计
        trap1_total = len(self.trap1_events)
        trap1_critical = sum(1 for e in self.trap1_events if e['severity'] == 'CRITICAL')
        trap1_rate = trap1_total / n_days if n_days > 0 else 0

        # 陷阱2统计
        total_rejected = sum(e['rejected'] for e in self.trap2_rejected)
        total_orders = sum(e['total'] for e in self.trap2_rejected)
        trap2_reject_rate = total_rejected / total_orders if total_orders > 0 else 0

        # 陷阱3统计
        trap3_total = len(self.trap3_diverged)
        trap3_critical = sum(1 for e in self.trap3_diverged if e['severity'] == 'CRITICAL')

        # NAV统计
        navs = [r['nav'] for r in self.daily_nav]
        if navs:
            peak = max(navs)
            final = navs[-1]
            total_return = (final - navs[0]) / navs[0] if navs[0] > 0 else 0
            # 最大回撤
            peak_so_far = navs[0]
            max_dd = 0.0
            for v in navs:
                if v > peak_so_far:
                    peak_so_far = v
                dd = (peak_so_far - v) / peak_so_far
                max_dd = max(max_dd, dd)
        else:
            total_return = 0.0
            max_dd = 0.0

        report = {
            'period': f'{self._start_date} ~ {self.daily_nav[-1]["date"] if self.daily_nav else "N/A"}',
            'total_days': n_days,
            # 陷阱1
            'trap1_gene_conflict': {
                'total_events': trap1_total,
                'critical_count': trap1_critical,
                'daily_rate': round(trap1_rate, 4),
                'verdict': 'PASS' if trap1_rate < 0.3 else ('WARN' if trap1_rate < 0.5 else 'FAIL'),
                'note': '牛市战法高动量标的被HRP压制>50%天数 → 需调低λ_illiq, 放松成长股权重' if trap1_rate > 0.5 else '',
            },
            # 陷阱2
            'trap2_min_amount': {
                'total_rejected': total_rejected,
                'reject_rate': round(trap2_reject_rate, 4),
                'verdict': 'PASS' if trap2_reject_rate < 0.25 else ('WARN' if trap2_reject_rate < 0.40 else 'FAIL'),
                'note': '500元门槛拒绝率>40% → 降门槛到200元或改用按股数下限' if trap2_reject_rate > 0.40 else '',
            },
            # 陷阱3
            'trap3_memory_gap': {
                'total_events': trap3_total,
                'critical_count': trap3_critical,
                'verdict': 'PASS' if trap3_critical == 0 else ('WARN' if trap3_critical <= 3 else 'FAIL'),
                'note': '停牌复牌后权重偏离>50% → 需在_apply_momentum中加偏离上限钳制' if trap3_critical > 3 else '',
            },
            # NAV
            'performance': {
                'total_return': round(total_return, 4),
                'max_drawdown': round(max_dd, 4),
                'max_drawdown_duration_days': self.max_drawdown_duration,
                'verdict': 'FAIL' if self.max_drawdown_duration > 252 else ('WARN' if self.max_drawdown_duration > 126 else 'PASS'),
                'note': f'最长回撤期{self.max_drawdown_duration}天 = {self.max_drawdown_duration/21:.1f}个月 '
                        f'{"— 实盘心理防线崩溃" if self.max_drawdown_duration > 126 else ""}',
            },
        }

        return report

    def print_summary(self):
        """终端输出回测监控总结"""
        s = self.summary()
        print('\n' + '=' * 60)
        print('  天眼 v8 10年回测 · 三陷阱监控报告')
        print('=' * 60)
        print(f'  回测区间: {s["period"]}')
        print(f'  交易日数: {s["total_days"]}')

        print(f'\n  ── 陷阱1: 基因冲突 ──')
        t1 = s['trap1_gene_conflict']
        print(f'    战法高分被HRP压制: {t1["total_events"]}次 ({t1["daily_rate"]:.1%})')
        print(f'    评级: {t1["verdict"]}')
        if t1['note']:
            print(f'    [!] {t1["note"]}')

        print(f'\n  ── 陷阱2: 500元门槛 ──')
        t2 = s['trap2_min_amount']
        print(f'    因门槛拒绝: {t2["total_rejected"]}笔 ({t2["reject_rate"]:.1%})')
        print(f'    评级: {t2["verdict"]}')
        if t2['note']:
            print(f'    [!] {t2["note"]}')

        print(f'\n  ── 陷阱3: 记忆断层 ──')
        t3 = s['trap3_memory_gap']
        print(f'    权重偏离事件: {t3["total_events"]}次 (危险级{t3["critical_count"]}次)')
        print(f'    评级: {t3["verdict"]}')
        if t3['note']:
            print(f'    [!] {t3["note"]}')

        print(f'\n  ── 业绩 ──')
        p = s['performance']
        print(f'    总收益: {p["total_return"]:+.2%}')
        print(f'    最大回撤: {p["max_drawdown"]:.2%}')
        print(f'    最长回撤期: {p["max_drawdown_duration_days"]}天 ({p["max_drawdown_duration_days"]/21:.1f}个月)')
        print(f'    评级: {p["verdict"]}')
        if p['note']:
            print(f'    [!] {p["note"]}')

    def save(self, path: str = MONITOR_LOG):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.summary(), f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════
# CLI 自检
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    import random

    mon = BacktestMonitor()
    print('模拟回测30天...')

    nav = 100000.0
    for day in range(30):
        d = f'2026-06-{day+1:02d}'

        # 模拟陷阱1: 偶尔发生基因冲突
        if day % 7 == 3:
            mon.record_trap1(d,
                survivor_pool=['sh600519', 'sz300750', 'sz000858'],
                hrp_weights={'sh600519': 0.15, 'sz300750': 0.01, 'sz000858': 0.10},
                strategy_scores={'sz300750': 92, 'sz000858': 78, 'sh600519': 65}
            )

        # 模拟陷阱2: 500元门槛拒绝
        if day % 5 == 0:
            mon.record_trap2(d,
                rejected_orders=[{'code': 'sh600001', 'action': 'BUY', 'shares': 100, 'amount': 350, 'reason': 'BELOW_MIN'}],
                executed_orders=[{'code': 'sz000858', 'action': 'BUY', 'shares': 200}]
            )

        # 模拟陷阱3: 权重偏离
        if day == 15:
            mon.record_trap3(d,
                ema_weights={'sh600519': 0.60, 'sh600036': 0.15, 'sz300750': 0.10},
                nav_weights={'sh600519': 0.25, 'sh600036': 0.08, 'sz300750': 0.05},
                positions={'sh600519': {'shares': 100}}
            )

        # NAV随机波动
        nav *= (1 + random.gauss(0.0005, 0.015))
        mon.record_nav(d, nav)

    mon.print_summary()
    print('\n自检完成.')
