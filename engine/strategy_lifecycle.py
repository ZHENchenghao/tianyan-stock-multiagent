"""天眼策略生命周期引擎 (L4) — 规则状态机+自动冻结/退役+规则博物馆
管理每条规则从 活跃→观察期→冻结→退役 的完整生命周期
"""
import sys, os, json
from datetime import datetime, date, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rule_sources import (RULE_SOURCES, RULE_STATES, STATE_TRANSITIONS,
                          update_confidence, get_dynamic_confidence)

# ═══════════════════════════════════════════
# 状态转移阈值
# ═══════════════════════════════════════════

LIFECYCLE_THRESHOLDS = {
    'probation_trigger': {
        'accuracy_drop': 0.10,    # 准确率下降10% → 观察期
        'consecutive_miss': 5,     # 连续5个信号错误 → 观察期
        'min_signals': 10,         # 最少信号数才能触发
    },
    'frozen_trigger': {
        'accuracy_below': 0.40,    # 准确率跌破40% → 冻结
        'sharpe_below': -1.0,     # 夏普<-1 → 冻结
        'max_drawdown_pct': -30,  # 最大回撤超-30% → 冻结
        'consecutive_miss': 8,     # 连续8个信号错误 → 冻结
    },
    'retire_trigger': {
        'frozen_months': 3,        # 冻结3个月确认失效 → 退役
        'no_improvement': True,    # 冻结期无改善
    },
    'revival_trigger': {
        'market_regime_match': True,  # 市场状态回归到规则历史擅长区间
        'min_hist_accuracy': 0.55,    # 历史最高准确率>55%才能复活
    },
}

# ═══════════════════════════════════════════
# 规则生命周期管理器
# ═══════════════════════════════════════════

class StrategyLifecycle:
    def __init__(self, tracker=None, storage_path=None):
        if storage_path is None:
            storage_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       'strategy_lifecycle.json')
        self.storage_path = storage_path
        self.tracker = tracker
        self._load()

    def _load(self):
        if os.path.exists(self.storage_path):
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                self.history = json.load(f)
        else:
            self.history = {
                'state_changes': [],     # 状态变更日志
                'retired_museum': {},    # 退役规则博物馆
                'revival_candidates': [],# 待复活规则
                'last_check': None,
            }

    def _save(self):
        self.history['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False)

    def check_and_transition(self):
        """主检查循环：检查所有规则的存活状态，执行状态转移"""
        changes = []
        today = date.today()

        for rule_id, src in RULE_SOURCES.items():
            current_state = src.get('state', 'active')
            feedbacks = src.get('feedback', [])

            # 过滤出live_tracker反馈
            live_fbs = [f for f in feedbacks if f.get('source') == 'live_tracker']

            if current_state == 'active':
                result = self._check_active_to_probation(rule_id, src, live_fbs)
                if result:
                    changes.append(result)
                    self._apply_transition(rule_id, 'probation', result['reason'])

            elif current_state == 'probation':
                # 检查是否恢复
                recover = self._check_probation_to_active(rule_id, src, live_fbs)
                if recover:
                    changes.append(recover)
                    self._apply_transition(rule_id, 'active', recover['reason'])
                else:
                    # 检查是否恶化
                    worsen = self._check_probation_to_frozen(rule_id, src, live_fbs)
                    if worsen:
                        changes.append(worsen)
                        self._apply_transition(rule_id, 'frozen', worsen['reason'])

            elif current_state == 'frozen':
                src_frozen = src
                freeze_date = None
                for fb in feedbacks:
                    if fb.get('state_change', '').endswith('→frozen'):
                        freeze_date = fb.get('timestamp', '')
                        break

                if freeze_date:
                    try:
                        frozen_since = datetime.strptime(freeze_date[:10], '%Y-%m-%d').date()
                        months_frozen = (today - frozen_since).days / 30
                    except:
                        months_frozen = 0

                    # 检查是否恢复
                    if months_frozen <= LIFECYCLE_THRESHOLDS['retire_trigger']['frozen_months']:
                        recover = self._check_probation_to_active(rule_id, src_frozen, live_fbs)
                        if recover:
                            changes.append(recover)
                            self._apply_transition(rule_id, 'probation', recover['reason'])

                    # 检查是否退役
                    if months_frozen >= LIFECYCLE_THRESHOLDS['retire_trigger']['frozen_months']:
                        retire = self._check_retire(rule_id, src_frozen, months_frozen)
                        if retire:
                            changes.append(retire)
                            self._apply_transition(rule_id, 'retired', retire['reason'])

            elif current_state == 'retired':
                # 检查复活条件
                revival = self._check_revival(rule_id, src)
                if revival:
                    changes.append(revival)
                    self._apply_transition(rule_id, 'probation', revival['reason'])

        self._save()
        return changes

    def _apply_transition(self, rule_id, new_state, reason):
        """执行状态转移"""
        old_state = RULE_SOURCES[rule_id].get('state', 'active')
        RULE_SOURCES[rule_id]['state'] = new_state
        if new_state == 'frozen':
            RULE_SOURCES[rule_id]['freeze_reason'] = reason

        log_entry = {
            'rule_id': rule_id,
            'master': RULE_SOURCES[rule_id]['master'],
            'from': old_state,
            'to': new_state,
            'reason': reason,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }
        self.history['state_changes'].append(log_entry)

        if new_state == 'retired':
            self.history['retired_museum'][rule_id] = {
                'rule': RULE_SOURCES[rule_id]['rule'],
                'master': RULE_SOURCES[rule_id]['master'],
                'retired_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'reason': reason,
            }

    def _check_active_to_probation(self, rule_id, src, live_fbs):
        recent_accs = [f.get('accuracy', 0.5) for f in live_fbs[-10:] if f.get('accuracy')]
        hist_accs = [f.get('accuracy', 0.5) for f in live_fbs[:-10] if f.get('accuracy')]

        if len(recent_accs) >= 5 and len(hist_accs) >= 5:
            recent_avg = sum(recent_accs) / len(recent_accs)
            hist_avg = sum(hist_accs) / len(hist_accs)
            drop = hist_avg - recent_avg

            if drop > LIFECYCLE_THRESHOLDS['probation_trigger']['accuracy_drop']:
                return {
                    'rule_id': rule_id,
                    'from': 'active',
                    'to': 'probation',
                    'reason': f'准确率下降{drop:.0%}: {hist_avg:.0%}→{recent_avg:.0%}',
                    'drop': drop,
                }
        return None

    def _check_probation_to_active(self, rule_id, src, live_fbs):
        recent_accs = [f.get('accuracy', 0.5) for f in live_fbs[-10:] if f.get('accuracy')]
        hist_accs = [f.get('accuracy', 0.5) for f in live_fbs[:-10] if f.get('accuracy')]

        if len(recent_accs) >= 5:
            recent_avg = sum(recent_accs) / len(recent_accs)
            # 恢复 = 最近准确率回到历史水平
            if hist_accs:
                hist_avg = sum(hist_accs) / len(hist_accs)
                if recent_avg >= hist_avg * 0.95:
                    return {
                        'rule_id': rule_id,
                        'from': 'probation',
                        'to': 'active',
                        'reason': f'准确率恢复: {recent_avg:.0%}≥{hist_avg:.0%}×0.95',
                    }
            elif recent_avg > 0.55:
                return {
                    'rule_id': rule_id,
                    'from': 'probation',
                    'to': 'active',
                    'reason': f'准确率达标: {recent_avg:.0%}>55%',
                }
        return None

    def _check_probation_to_frozen(self, rule_id, src, live_fbs):
        if len(live_fbs) >= LIFECYCLE_THRESHOLDS['frozen_trigger']['consecutive_miss']:
            recent_results = [f.get('result') for f in live_fbs[-8:]]
            if recent_results.count('rejected') >= 8:
                return {
                    'rule_id': rule_id,
                    'from': 'probation',
                    'to': 'frozen',
                    'reason': f'连续8个信号错误',
                }

        recent_accs = [f.get('accuracy', 0) for f in live_fbs[-10:] if f.get('accuracy') is not None]
        if recent_accs:
            avg_acc = sum(recent_accs) / len(recent_accs)
            if avg_acc < LIFECYCLE_THRESHOLDS['frozen_trigger']['accuracy_below']:
                return {
                    'rule_id': rule_id,
                    'from': 'probation',
                    'to': 'frozen',
                    'reason': f'准确率{avg_acc:.0%}<40%',
                }
        return None

    def _check_retire(self, rule_id, src, months_frozen):
        return {
            'rule_id': rule_id,
            'from': 'frozen',
            'to': 'retired',
            'reason': f'冻结{months_frozen:.0f}月无改善，确认失效',
        }

    def _check_revival(self, rule_id, src):
        hist_accs = [f.get('accuracy', 0) for f in src.get('feedback', [])
                     if f.get('accuracy')]
        if hist_accs:
            max_acc = max(hist_accs)
            if max_acc >= LIFECYCLE_THRESHOLDS['revival_trigger']['min_hist_accuracy']:
                return {
                    'rule_id': rule_id,
                    'from': 'retired',
                    'to': 'probation',
                    'reason': f'市场状态回归，历史最佳准确率{max_acc:.0%}>55%',
                }
        return None

    def get_state_summary(self):
        """获取所有规则的状态摘要"""
        summary = defaultdict(lambda: {'count': 0, 'rules': []})
        for rid, src in RULE_SOURCES.items():
            state = src.get('state', 'active')
            summary[state]['count'] += 1
            summary[state]['rules'].append(rid)
        return dict(summary)

    def get_lifecycle_report(self):
        """生命周期综合报告"""
        print(f"\n{'='*60}")
        print(f"  天眼策略生命周期 (L4)")
        print(f"{'='*60}")

        summary = self.get_state_summary()
        for state, info in sorted(summary.items()):
            desc = RULE_STATES.get(state, '')
            print(f"\n  [{state}] {desc}: {info['count']}条")
            if info['count'] <= 10:
                for rid in info['rules']:
                    s = RULE_SOURCES[rid]
                    reason = s.get('freeze_reason', '')
                    print(f"    {rid} [{s['master']}] {s['rule'][:50]}{' — '+reason if reason else ''}")

        print(f"\n  --- 退役博物馆 ({len(self.history.get('retired_museum', {}))}条) ---")
        for rid, info in self.history.get('retired_museum', {}).items():
            print(f"  {rid} [{info['master']}]: {info['reason']} ({info['retired_at']})")

        return {
            'summary': summary,
            'state_changes': len(self.history.get('state_changes', [])),
            'retired': len(self.history.get('retired_museum', {})),
        }

if __name__ == '__main__':
    lifecycle = StrategyLifecycle()
    lifecycle.get_lifecycle_report()
