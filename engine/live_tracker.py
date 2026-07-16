"""天眼实战追踪引擎 (L3) — 信号记录+N日验证+盈亏归因+衰减检测
每条规则信号发出后，N日后自动验证方向准确性，多维度归因盈亏
"""
import sys, os, json, math
from datetime import datetime, timedelta, date
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rule_sources import RULE_SOURCES, update_confidence

# ═══════════════════════════════════════════
# 信号记录器
# ═══════════════════════════════════════════

class SignalTracker:
    """信号记录器——记录每条规则发出的每个信号，N日后验证"""

    def __init__(self, storage_path=None):
        if storage_path is None:
            storage_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       'signal_history.json')
        self.storage_path = storage_path
        self._load()

    def _load(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    self.records = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                self.records = []
        else:
            self.records = []

    def _save(self):
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False, default=str)

    def record(self, rule_id, symbol, action, entry_price, position, master='',
               market_state='', emotion_stage='', confidence='中', note=''):
        """记录一个信号"""
        record = {
            'id': len(self.records) + 1,
            'rule_id': rule_id,
            'master': master or RULE_SOURCES.get(rule_id, {}).get('master', '?'),
            'symbol': symbol,
            'action': action,
            'entry_price': entry_price,
            'position': position,
            'entry_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'entry_date': date.today().strftime('%Y-%m-%d'),
            'market_state': market_state,
            'emotion_stage': emotion_stage,
            'confidence': confidence,
            'note': note,
            # N日验证字段
            'verified': False,
            'verify_date': None,
            'exit_price': None,
            'direction_correct': None,  # 方向正确？
            'pnl_pct': None,
            'pnl_r_multiple': None,     # R倍数
        }
        self.records.append(record)
        self._save()
        return record

    def verify(self, record_id, exit_price, exit_date=None):
        """N日后验证信号方向准确性"""
        for rec in self.records:
            if rec['id'] == record_id:
                entry = rec['entry_price']
                rec['exit_price'] = exit_price
                rec['verify_date'] = exit_date or date.today().strftime('%Y-%m-%d')
                rec['verified'] = True

                pnl = (exit_price - entry) / entry
                rec['pnl_pct'] = round(pnl * 100, 2)

                # 方向正确性：买入后涨=正确，卖出后跌=正确
                if rec['action'] in ('买入', '加仓', '打板', '排板'):
                    rec['direction_correct'] = pnl > 0
                elif rec['action'] in ('卖出', '止损', '清仓', '减仓'):
                    rec['direction_correct'] = pnl < 0
                else:
                    rec['direction_correct'] = None

                # R倍数（盈亏比倍数）
                risk = 0.05  # 默认5%止损
                rec['pnl_r_multiple'] = round(pnl / risk, 1)

                self._save()
                return rec
        return None

    def get_unverified(self, days_old=5):
        """获取N天前发出但未验证的信号"""
        cutoff = (date.today() - timedelta(days=days_old)).strftime('%Y-%m-%d')
        return [r for r in self.records
                if not r['verified'] and r['entry_date'] <= cutoff]

    def get_accuracy_by_rule(self, rule_id, min_samples=5):
        """计算某规则的信号方向准确率"""
        signals = [r for r in self.records if r['rule_id'] == rule_id and r['verified']]
        if len(signals) < min_samples:
            return None
        correct = sum(1 for s in signals if s['direction_correct'])
        return round(correct / len(signals), 3)

    def get_accuracy_by_master(self, master, min_samples=5):
        """计算某大师的信号方向准确率"""
        signals = [r for r in self.records if r['master'] == master and r['verified']]
        if len(signals) < min_samples:
            return None
        correct = sum(1 for s in signals if s['direction_correct'])
        return round(correct / len(signals), 3)

# ═══════════════════════════════════════════
# 盈亏归因
# ═══════════════════════════════════════════

def attribution_report(tracker):
    """三维度盈亏归因：按大师×按规则类型×按市场状态"""
    records = [r for r in tracker.records if r['verified']]

    if len(records) < 5:
        return {'status': 'insufficient_data', 'message': f'仅{len(records)}条已验证记录，需≥5条'}

    # 维度1: 按大师
    by_master = defaultdict(lambda: {'count': 0, 'correct': 0, 'total_pnl': 0, 'total_r': 0})
    for r in records:
        m = r['master']
        by_master[m]['count'] += 1
        if r['direction_correct']:
            by_master[m]['correct'] += 1
        by_master[m]['total_pnl'] += r.get('pnl_pct', 0) or 0
        by_master[m]['total_r'] += r.get('pnl_r_multiple', 0) or 0

    master_report = {}
    for master, d in sorted(by_master.items(), key=lambda x: -x[1]['total_pnl']):
        acc = d['correct'] / d['count'] if d['count'] > 0 else 0
        master_report[master] = {
            'signals': d['count'],
            'accuracy': round(acc * 100, 1),
            'total_pnl_pct': round(d['total_pnl'], 2),
            'avg_r': round(d['total_r'] / d['count'], 2) if d['count'] > 0 else 0,
        }

    # 维度2: 按操作类型
    by_action = defaultdict(lambda: {'count': 0, 'correct': 0, 'total_pnl': 0})
    for r in records:
        action_type = 'buy' if r['action'] in ('买入', '加仓', '打板', '排板') else (
            'sell' if r['action'] in ('卖出', '止损', '清仓', '减仓') else 'hold')
        by_action[action_type]['count'] += 1
        if r['direction_correct']:
            by_action[action_type]['correct'] += 1
        by_action[action_type]['total_pnl'] += r.get('pnl_pct', 0) or 0

    action_report = {}
    for atype, d in by_action.items():
        action_report[atype] = {
            'signals': d['count'],
            'accuracy': round(d['correct'] / d['count'] * 100, 1) if d['count'] > 0 else 0,
            'total_pnl_pct': round(d['total_pnl'], 2),
        }

    # 维度3: 按市场状态
    by_regime = defaultdict(lambda: {'count': 0, 'correct': 0, 'total_pnl': 0})
    for r in records:
        regime = r.get('market_state', '?')
        by_regime[regime]['count'] += 1
        if r['direction_correct']:
            by_regime[regime]['correct'] += 1
        by_regime[regime]['total_pnl'] += r.get('pnl_pct', 0) or 0

    regime_report = {}
    for regime, d in sorted(by_regime.items(), key=lambda x: -x[1]['total_pnl']):
        regime_report[regime] = {
            'signals': d['count'],
            'accuracy': round(d['correct'] / d['count'] * 100, 1) if d['count'] > 0 else 0,
            'total_pnl_pct': round(d['total_pnl'], 2),
        }

    return {
        'by_master': master_report,
        'by_action': action_report,
        'by_regime': regime_report,
    }

# ═══════════════════════════════════════════
# 衰减检测
# ═══════════════════════════════════════════

def detect_decay(tracker, rule_id, window=20, threshold=0.15):
    """检测规则准确率是否出现衰减

    方法：比较最近window个信号 vs 历史均值的差异
    如果最近准确率比历史低15%以上 → 衰减警告
    """
    signals = [r for r in tracker.records
               if r['rule_id'] == rule_id and r['verified'] and r['direction_correct'] is not None]

    if len(signals) < window + 10:
        return {'decayed': False, 'reason': f'信号不足({len(signals)}条，需≥{window+10})'}

    # 历史准确率（除最近window个）
    hist_acc = sum(1 for s in signals[:-window] if s['direction_correct']) / len(signals[:-window])
    # 最近准确率
    recent_acc = sum(1 for s in signals[-window:] if s['direction_correct']) / window

    delta = hist_acc - recent_acc
    decayed = delta > threshold

    if decayed:
        # 自动更新规则状态
        update_confidence(rule_id, 'live_tracker', 'decay_detected',
                         accuracy=recent_acc,
                         note=f'衰减{delta:.0%}: 历史{hist_acc:.0%}→最近{recent_acc:.0%}')

    return {
        'rule_id': rule_id,
        'decayed': decayed,
        'hist_accuracy': round(hist_acc, 3),
        'recent_accuracy': round(recent_acc, 3),
        'delta': round(delta, 3),
        'threshold': threshold,
        'total_signals': len(signals),
    }

def detect_all_decay(tracker, window=20, threshold=0.15):
    """对所有规则运行衰减检测"""
    all_rids = set(r['rule_id'] for r in tracker.records if r['verified'])
    results = {}
    for rid in sorted(all_rids):
        result = detect_decay(tracker, rid, window, threshold)
        results[rid] = result
    return results

# ═══════════════════════════════════════════
# 综合追踪报告
# ═══════════════════════════════════════════

def run_live_tracker_report(tracker=None):
    """实战追踪综合报告"""
    if tracker is None:
        tracker = SignalTracker()

    print(f"\n{'='*60}")
    print(f"  天眼实战追踪 (L3)")
    print(f"{'='*60}")

    total = len(tracker.records)
    verified = sum(1 for r in tracker.records if r['verified'])
    unverified = total - verified

    print(f"\n  --- 信号概览 ---")
    print(f"  总信号: {total} | 已验证: {verified} | 待验证: {unverified}")

    if verified > 0:
        correct = sum(1 for r in tracker.records if r.get('direction_correct'))
        overall_acc = correct / verified * 100 if verified > 0 else 0
        print(f"  综合准确率: {overall_acc:.1f}% ({correct}/{verified})")

        pos_signals = [r for r in tracker.records if r['verified'] and r.get('pnl_pct', 0) is not None]
        if pos_signals:
            total_pnl = sum(r['pnl_pct'] for r in pos_signals)
            avg_pnl = total_pnl / len(pos_signals)
            print(f"  累计盈亏: {total_pnl:+.2f}% | 平均盈亏: {avg_pnl:+.2f}%")

    # 归因
    attr = attribution_report(tracker)
    if attr.get('status') != 'insufficient_data':
        print(f"\n  --- 大师归因 ---")
        for master, d in sorted(attr['by_master'].items(), key=lambda x: -x[1]['total_pnl_pct']):
            print(f"  {master:<8s}  {d['signals']:>3d}单  准确率{d['accuracy']:>5.1f}%  "
                  f"盈亏{d['total_pnl_pct']:>+7.2f}%  R{d['avg_r']:>+5.1f}")

        print(f"\n  --- 操作类型归因 ---")
        for atype, d in attr['by_action'].items():
            print(f"  {atype:<6s}  {d['signals']:>3d}单  准确率{d['accuracy']:>5.1f}%  "
                  f"盈亏{d['total_pnl_pct']:>+7.2f}%")

    # 衰减检测
    decay_results = detect_all_decay(tracker)
    decayed = {rid: r for rid, r in decay_results.items() if r.get('decayed')}
    if decayed:
        print(f"\n  --- 衰减警告 ({len(decayed)}条) ---")
        for rid, d in decayed.items():
            print(f"  {rid}: 历史{d['hist_accuracy']:.0%}→最近{d['recent_accuracy']:.0%} "
                  f"({d['delta']:+.0%} | {d['total_signals']}个信号)")

    return {
        'total': total, 'verified': verified, 'unverified': unverified,
        'attribution': attr, 'decay': decay_results,
    }

if __name__ == '__main__':
    tracker = SignalTracker()
    run_live_tracker_report(tracker)
