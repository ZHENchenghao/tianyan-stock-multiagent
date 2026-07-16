"""天眼冲突裁决器 v2 — 五层验证塔集成
L0(溯源+门禁)→ L1(回测校验)→ L2(规则审计)→ L3(信号追踪)→ L4(生命周期)
规则: 卖出无条件优先 > 同标的高分胜出 > 总仓位硬上限 > 全局硬约束
"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from constitution import (MODEL_PARAMS, priority_score, check_risk_reward,
                          check_total_position, check_daily_loss, apply_constitution,
                          constitution_report)
from rule_sources import (enrich_rules_with_source, get_dynamic_confidence,
                          check_data_quality, update_confidence)
from rule_audit import (calc_voting_weight, weighted_vote, detect_contradictions)
from live_tracker import SignalTracker
from strategy_lifecycle import StrategyLifecycle
from sub_models import ALL_MODELS

MODEL_SCAN_ORDER = sorted(ALL_MODELS.keys(), key=lambda m: priority_score(m), reverse=True)

# 全局实例
_signal_tracker = SignalTracker()
_strategy_lifecycle = StrategyLifecycle(tracker=_signal_tracker)


def collect_all_rules(market_data, candidates, positions):
    """并行扫描全部15个子模型，L0门禁过滤"""
    scanners = ALL_MODELS
    all_rules = []

    for name in MODEL_SCAN_ORDER:
        # L4检查: 该大师的所有规则是否已全部退役
        from rule_sources import get_rules_by_master, RULE_SOURCES
        master_rules = get_rules_by_master(name)
        active_rules = [r for r in master_rules
                       if RULE_SOURCES.get(r, {}).get('state') != 'retired']
        if not active_rules:
            continue  # 该大师已全部退役，跳过

        try:
            model_rules = scanners[name].scan(market_data, candidates, positions)
            if model_rules:
                # L0: 数据质量门禁
                passed_rules = []
                for rule in model_rules:
                    data_ok, missing, degraded = check_data_quality(rule, market_data)
                    if data_ok:
                        passed_rules.append(rule)
                    elif degraded:
                        rule['data_degraded'] = True
                        rule['missing_fields'] = missing
                        passed_rules.append(rule)
                    # 完全不合格的丢弃
                all_rules.extend(passed_rules)
        except Exception as e:
            print(f"  [!] {name}子模型扫描异常: {e}")

    return all_rules


def resolve(all_rules, positions=None, today_pnl=0, market_data=None):
    """冲突裁决主循环 (L2审计增强)"""
    if positions is None:
        positions = []
    if market_data is None:
        market_data = {}

    if not all_rules:
        return [], {'passed': 0, 'rejected': 0, 'violations': []}

    # L2: 规则间矛盾检测
    contradictions = detect_contradictions()
    contradiction_pairs = set()
    for c in contradictions:
        if c.get('severity') == 'critical':
            contradiction_pairs.add((c['rule1'], c['rule2']))
            contradiction_pairs.add((c['rule2'], c['rule1']))

    # 按动态置信度降序（L0反馈）
    all_rules.sort(key=lambda r: r.get('dynamic_confidence',
                   get_dynamic_confidence(r.get('rule_id', ''))), reverse=True)

    final = []
    sell_signals = []
    buy_signals = []
    hold_signals = []

    for rule in all_rules:
        # L4: 过滤退役/冻结规则
        state = rule.get('state', 'active')
        if state == 'retired':
            continue
        if state == 'frozen' and rule.get('action', '') in ('买入', '加仓', '打板', '排板'):
            continue

        # L2: 矛盾对降级——双方都降为"关注"而不是买卖
        rid = rule.get('rule_id', '')
        action = rule.get('action', '')
        for (r1, r2) in contradiction_pairs:
            if rid == r1:
                matching = [rr for rr in all_rules if rr.get('rule_id') == r2]
                if matching:
                    if action in ('买入', '加仓', '打板', '排板') and \
                       matching[0].get('action') in ('卖出', '止损', '清仓'):
                        action = '关注'  # 降级
                        rule = dict(rule)
                        rule['action'] = '关注'
                        rule['note'] = f'L2矛盾降级: {rid}×{r2}冲突→关注'

        if action in ('卖出', '止损', '清仓', '核按钮', '减半仓', '减仓'):
            sell_signals.append(rule)
        elif action in ('买入', '加仓', '打板', '排板'):
            buy_signals.append(rule)
        else:
            hold_signals.append(rule)

    # 第一轮：卖出指令过v4.1熔断校验
    for rule in sell_signals:
        try:
            from engine.fuse_breaker import fuse_check
            fuse_result = fuse_check(
                rule.get('symbol', ''),
                rule.get('sector', ''),
                rule.get('ts_code', rule.get('symbol', '')),
                rule.get('action', '减仓'),
            )
            if fuse_result['fused']:
                rule = dict(rule)
                rule['action'] = fuse_result['override_action']
                rule['fuse_info'] = {
                    'level': fuse_result['fuse_level'],
                    'correction': fuse_result['correction_line'],
                }
                rule['note'] = (rule.get('note', '') + f' | 熔断{fuse_result["fuse_level"]}级: {fuse_result["correction_line"]}').strip('| ')
        except Exception:
            pass
        final.append(rule)

    sold_symbols = {r.get('symbol') for r in sell_signals}

    # 第二轮：买入指令冲突裁决 + L2加权投票
    total_buy_pos = sum(p.get('position', 0) for p in positions)
    symbol_buys = {}

    for rule in buy_signals:
        symbol = rule.get('symbol', '')
        if symbol in sold_symbols:
            continue
        if symbol in symbol_buys:
            existing = symbol_buys[symbol]
            existing_w = existing.get('dynamic_confidence',
                         get_dynamic_confidence(existing.get('rule_id', '')))
            rule_w = rule.get('dynamic_confidence',
                     get_dynamic_confidence(rule.get('rule_id', '')))
            if rule_w > existing_w:
                symbol_buys[symbol] = rule
        else:
            symbol_buys[symbol] = rule

    # 仓位叠加检查
    for symbol, rule in sorted(symbol_buys.items(),
                                key=lambda x: x[1].get('dynamic_confidence',
                                get_dynamic_confidence(x[1].get('rule_id', ''))), reverse=True):
        new_pos = rule.get('position', 0)
        if total_buy_pos + new_pos > 1.0:
            continue
        final.append(rule)
        total_buy_pos += new_pos

    # 第三轮：持有/关注/空仓
    for rule in hold_signals:
        if rule.get('action') == '空仓':
            final = [r for r in final if r.get('action') not in ('买入', '加仓', '打板', '排板')]
        final.append(rule)

    # 宪法过滤
    mania = _check_mania(market_data)
    final, violations = apply_constitution(final, positions, today_pnl, mania)

    # 记录信号到L3追踪器
    for rule in final:
        action = rule.get('action', '')
        if action in ('买入', '卖出', '加仓', '减仓', '打板', '排板', '止损', '清仓'):
            _signal_tracker.record(
                rule_id=rule.get('rule_id', '?'),
                symbol=rule.get('symbol', '?'),
                action=action,
                entry_price=rule.get('price', 0),
                position=rule.get('position', 0),
                master=rule.get('master', '?'),
                market_state=market_data.get('oneil_state', '?') if market_data else '?',
                emotion_stage=market_data.get('emotion_stage', '?') if market_data else '?',
                confidence=rule.get('confidence', '中'),
                note=rule.get('desc', ''),
            )

    # 排序
    sell_actions = {'卖出', '止损', '清仓', '核按钮', '减半仓', '减仓'}
    final.sort(key=lambda r: (
        0 if r.get('action') in sell_actions else 1,
        -(r.get('dynamic_confidence', get_dynamic_confidence(r.get('rule_id', ''))))
    ))

    con_report = constitution_report(final, violations)
    return final, con_report


def _check_mania(market_data):
    if market_data is None:
        return False
    limit_ratio = market_data.get('limit_up', 0) / max(market_data.get('limit_down', 1), 1)
    retail_heat = market_data.get('retail_heat', 0)
    return limit_ratio > 8 and retail_heat >= 5


def run_full_scan(market_data=None, candidates=None, positions=None, today_pnl=0):
    """完整五层扫描+裁决流程"""
    if market_data is None:
        market_data = {}
    if candidates is None:
        candidates = []
    if positions is None:
        positions = []

    print(f"\n{'='*60}")
    print(f"  天眼多模型扫描 · 五层验证塔")
    print(f"{'='*60}")

    # 显示各模型状态 (L0+L4)
    for name in MODEL_SCAN_ORDER:
        p = MODEL_PARAMS.get(name, {})
        freeze = '[冻结买入]' if p.get('freeze') else '[活跃]'
        sc = priority_score(name)
        from rule_sources import get_rules_by_master, RULE_SOURCES
        master_rules = get_rules_by_master(name)
        active_count = sum(1 for r in master_rules
                          if RULE_SOURCES.get(r, {}).get('state') in ('active', 'probation'))
        frozen_count = sum(1 for r in master_rules
                          if RULE_SOURCES.get(r, {}).get('state') in ('frozen', 'retired'))
        lifecycle_tag = f'(+{active_count})' if frozen_count == 0 else f'(+{active_count}/-{frozen_count})'
        print(f"  {name:<6s} 得分{sc:>6.0f}  胜率{p.get('win_rate',0):.0%}  "
              f"盈亏比{p.get('risk_reward',0)}:1  {freeze} {lifecycle_tag}")

    # L1+L2+L3+L4: 收集
    print(f"\n  --- 扫描触发规则 (L0门禁+L4过滤) ---")
    all_rules = collect_all_rules(market_data, candidates, positions)
    all_rules = enrich_rules_with_source(all_rules)

    if not all_rules:
        print(f"  当前市场无触发规则")
        return [], {}

    # 显示规则及验证层信息
    for r in all_rules:
        conf = r.get('confidence', '?')
        conf_icon = '[高]' if conf == '高' else ('[中]' if conf == '中' else '[低]')
        state = r.get('state', 'active')
        state_icon = '' if state == 'active' else f'[{state}]'
        data_warn = ' [数据降级]' if r.get('data_degraded') else ''
        dyn_conf = r.get('dynamic_confidence', 0)
        print(f"  {conf_icon}{state_icon} [{r['rule_id']}] {r['master']} {r['action']} "
              f"{r.get('symbol','?')} {r.get('position',0):.0%} 动态置信度{dyn_conf:.0%}"
              f"{data_warn}")

    # 裁决
    final, report = resolve(all_rules, positions, today_pnl, market_data)

    # 输出
    print(f"\n  --- 最终指令集 ({len(final)}条) ---")
    if final:
        for i, r in enumerate(final, 1):
            action = r['action']
            icon = '[SELL]' if action in ('卖出','止损','清仓','核按钮') else (
                '[BUY]' if action in ('买入','加仓','打板','排板') else '[HOLD]')
            conf = r.get('confidence', '?')
            dyn = r.get('dynamic_confidence', get_dynamic_confidence(r.get('rule_id', '')))
            print(f"  {i}. {icon} [{r['rule_id']}] {r['master']} {action} {r.get('symbol','?')} "
                  f"{r.get('position',0):.0%} conf:{dyn:.0%} — {r.get('desc','')}")
    else:
        print(f"  无最终指令")

    return final, report


def verify_signals(exits_data):
    """L3: 批量验证信号——传入 {signal_id: exit_price}"""
    verified = []
    for sig_id, exit_price in exits_data.items():
        result = _signal_tracker.verify(sig_id, exit_price)
        if result:
            verified.append(result)
            # 反馈到L0
            acc = 1 if result.get('direction_correct') else 0
            update_confidence(result['rule_id'], 'live_tracker',
                            'confirmed' if acc else 'rejected',
                            accuracy=acc,
                            note=f'信号验证: {result["symbol"]} {result["action"]} → PnL{result["pnl_pct"]}%')
    return verified


def run_lifecycle_check():
    """L4: 运行策略生命周期检查"""
    return _strategy_lifecycle.check_and_transition()


def get_full_verification_report():
    """五层验证塔综合报告"""
    from rule_sources import source_stats
    from rule_audit import run_full_audit
    from live_tracker import run_live_tracker_report

    print(f"\n{'='*60}")
    print(f"  天眼五层验证塔 · 综合报告")
    print(f"{'='*60}")

    # L0
    stats = source_stats()
    print(f"\n  L0 溯源+存活: {stats['total_rules']}条规则 | "
          f"活跃{stats['states']['active']} | 观察期{stats['states']['probation']} | "
          f"冻结{stats['states']['frozen']} | 退役{stats['states']['retired']}")

    # L2
    audit = run_full_audit()

    # L3
    live = run_live_tracker_report(_signal_tracker)

    # L4
    lc = _strategy_lifecycle.get_lifecycle_report()

    return {'l0': stats, 'l2': audit, 'l3': live, 'l4': lc}


# ═══════════════════════════════════════════
# v3.1: 上下文感知冲突裁决器
# ═══════════════════════════════════════════

def context_aware_resolve(conflicting_rules, market_data):
    """
    根据当前市场状态自动选择最优规则

    裁决原则:
      1. 上升确认(牛市) → 集中 > 分散, 宽松止损, 延长持有
      2. 上升承压 → 集中 ≈ 分散, 收紧止损, 选择性持有
      3. 下跌修正 → 分散 > 集中, 最严止损, 现金为王
      4. 反弹尝试 → 保守为上, 等待确认

    Args:
        conflicting_rules: 冲突规则对列表 [{rule1, rule2, conflict_type}, ...]
        market_data: 市场数据 (必须含oneil_state)

    Returns:
        {rule_id: 'apply'|'discard'|'downgrade', ...}
    """
    oneil = market_data.get('oneil_state', 'confirmed_uptrend')
    emotion = market_data.get('emotion_stage', '主升')

    # 市场状态 → 策略偏好
    if oneil == 'confirmed_uptrend' and emotion in ('主升', '高潮'):
        # 牛市: 集中持仓, 宽松止损, 让利润奔跑
        strategy = {
            '集中vs分散': '集中',
            '止损幅度': '宽松',      # 取较宽止损(如10%而非3%)
            '持有周期': '长周期',     # 取较长持有(如30天而非3天)
            '买入vs卖出': '买入优先',
        }
    elif oneil == 'uptrend_pressure':
        # 上升承压: 均衡, 收紧止损
        strategy = {
            '集中vs分散': '均衡',
            '止损幅度': '收紧',      # 取较严止损(如5%而非10%)
            '持有周期': '中周期',
            '买入vs卖出': '卖出优先',
        }
    elif oneil == 'correction':
        # 下跌修正: 分散防御, 最严止损
        strategy = {
            '集中vs分散': '分散',
            '止损幅度': '最严',      # 取最严止损(如3%而非10%)
            '持有周期': '短周期',
            '买入vs卖出': '只卖不买',
        }
    else:  # rally_attempt
        # 反弹尝试: 现金为王
        strategy = {
            '集中vs分散': '现金为王',
            '止损幅度': '立即清仓',
            '持有周期': '不持有',
            '买入vs卖出': '只卖不买',
        }

    decisions = {}
    for conflict in conflicting_rules:
        r1 = conflict.get('rule1', '')
        r2 = conflict.get('rule2', '')
        ctype = conflict.get('conflict_type', '')

        if ctype == '集中vs分散':
            if strategy['集中vs分散'] == '集中':
                # 保留集中规则(如Druck R50), 丢弃分散规则
                decisions[r1] = 'apply' if '集中' in conflict.get('r1_desc', '') else 'discard'
                decisions[r2] = 'discard' if decisions.get(r1) == 'apply' else 'apply'
            elif strategy['集中vs分散'] == '分散':
                decisions[r1] = 'apply' if '分散' in conflict.get('r1_desc', '') else 'discard'
                decisions[r2] = 'discard' if decisions.get(r1) == 'apply' else 'apply'
            else:
                decisions[r1] = 'downgrade'
                decisions[r2] = 'downgrade'

        elif ctype == '止损幅度冲突':
            if strategy['止损幅度'] == '宽松':
                # 选较大止损幅度
                pass  # 由具体规则层面的stop_loss值决定
            elif strategy['止损幅度'] == '收紧':
                # 选较小止损幅度 → 更保守
                pass
            decisions[r1] = 'apply'
            decisions[r2] = 'downgrade'

        elif ctype == '持有周期冲突':
            if strategy['持有周期'] == '长周期':
                decisions[r1] = 'apply'  # 保留长周期规则
                decisions[r2] = 'discard'
            else:
                decisions[r1] = 'discard'
                decisions[r2] = 'apply'

        elif ctype == '买卖方向矛盾':
            if strategy['买入vs卖出'] == '只卖不买':
                # 卖出规则胜出
                decisions[r1] = 'apply' if '卖' in conflict.get('r1_action', '') else 'discard'
                decisions[r2] = 'discard' if decisions.get(r1) == 'apply' else 'apply'
            elif strategy['买入vs卖出'] == '卖出优先':
                decisions[r1] = 'downgrade'
                decisions[r2] = 'downgrade'
            else:
                # 牛市: 买入优先
                decisions[r1] = 'apply' if '买' in conflict.get('r1_action', '') else 'discard'
                decisions[r2] = 'discard' if decisions.get(r1) == 'apply' else 'apply'

    return {
        'decisions': decisions,
        'strategy': strategy,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'oneil_state': oneil,
        'emotion_stage': emotion,
    }

# 注册O'Neil状态规则到ALL_MODELS (使collect_all_rules能调用)
def _register_oneil_rules():
    """在ALL_MODELS中注册O'Neil状态模块"""
    try:
        from sub_models import ALL_MODELS
        from sub_models.oneil_states import scan_by_state
        ALL_MODELS["O'Neil"] = type('ONeilModule', (), {
            'scan': staticmethod(scan_by_state)
        })
        return True
    except Exception as e:
        print(f'  [!] O\'Neil状态规则注册失败: {e}')
        return False

# 自动注册
_register_oneil_rules()

if __name__ == '__main__':
    demo_market = {
        'limit_up': 57, 'limit_down': 0,
        'emotion_stage': '启动',
        'new_theme_first_board_count': 6,
        'oneil_state': 'confirmed_uptrend',
    }
    demo_candidates = [
        {'code': '600519', 'is_first_board': True, 'board_time': '09:45',
         'turnover_amount': 8e8, 'is_second_board': True, 'seal_volume': 150000,
         'turnover_rate': 15, 'blow_open_minutes': 8, 're_seal_time': '13:30',
         'blow_low_pct': 4, 'new_theme_first_limit_up': True},
    ]
    run_full_scan(demo_market, demo_candidates, [])
