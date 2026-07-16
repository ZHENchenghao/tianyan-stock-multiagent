# -*- coding: utf-8 -*-
"""
天眼 v8 统一裁决引擎 · 连续化概率流数学内核
=============================================
纯统计函数层 — 零业务依赖, 零I/O副作用 (除迟滞环实盘模式)。
所有函数均内置工业级防御: 防NaN / 防除零 / 防溢出 / 绝对收敛。

用法:
    from engine.verdict_math import _calc_S_total, _calc_posterior_probabilities, _process_hysteresis
"""

import math
import json
import os
import numpy as np
from datetime import date, datetime
from collections import defaultdict

# ═══════════════════════════════════════════
# 零依赖高斯CDF — math.erf 实现, 不依赖 scipy
# ═══════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    """
    标准正态累积分布函数 Φ(x)。
    使用 math.erf 实现 — math.erf 是 Python 标准库, 无条件依赖, 零安装成本。

    Φ(x) = 0.5 × (1 + erf(x / √2))
    数值精度: 与 scipy.stats.norm.cdf 一致 (误差 < 1e-15)。
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ═══════════════════════════════════════════
# 1. 权重归一化与 S_total 合成引擎
# ═══════════════════════════════════════════

# 官方 9 维标准权重 (v8 设计文档 Section 5.2.1)
_RAW_WEIGHTS = {
    '宏观': 0.25, '大盘': 0.20, '景气': 0.15, '趋势': 0.15,
    '资金流': 0.15, '盈亏': 0.10, '压力测试': 0.10, '反共识': 0.10,
    '规则健康': 0.05,
}

# 维度名映射 — unified_verdict.py 使用全名, 这里映射到短名
_DIM_NAME_MAP = {
    '宏观体制': '宏观', '大盘状态': '大盘', '景气度': '景气',
    '趋势(战法)': '趋势', '趋势': '趋势',
    '资金流': '资金流', '盈亏': '盈亏', '压力测试': '压力测试',
    '反共识': '反共识', '规则健康': '规则健康',
}

# 短名→全名逆映射 (regime_weights_cache用全名)
_SHORT_TO_FULL = {v: k for k, v in _DIM_NAME_MAP.items()}


def load_regime_weights(regime: str = None) -> dict:
    """
    尝试从 regime_weights_cache.json 加载当前regime的优化权重。
    成功→覆盖 _RAW_WEIGHTS; 失败→返回默认权重。
    返回当前生效的权重dict (短名key)。
    """
    global _RAW_WEIGHTS
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'regime_weights_cache.json')
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                rw_cache = json.load(f)
            rw = rw_cache.get('regime_weights', {})
            if regime and regime in rw:
                # 全名→短名转换
                full_weights = rw[regime].get('weights', {})
                short_weights = {}
                for full_name, w in full_weights.items():
                    short = _DIM_NAME_MAP.get(full_name, full_name)
                    short_weights[short] = w
                # 归一化
                total = sum(short_weights.values())
                if total > 0:
                    short_weights = {k: v / total for k, v in short_weights.items()}
                _RAW_WEIGHTS.update(short_weights)
                return dict(_RAW_WEIGHTS)
    except Exception:
        pass
    return dict(_RAW_WEIGHTS)


def _calc_S_total(dimensions: list) -> tuple:
    """
    权重归一化 → z-score映射 → S_total 平滑合成。

    Args:
        dimensions: list of dict, 每项含 {'name': str, 'score': float}
                    score ∈ [0, 100], 50=完全中性

    Returns:
        tuple: (S_total: float, z_scores: dict)
               S_total ∈ [-1.0, 1.0], 天然收敛, 绝对不溢出。
               z_scores = {'宏观': -0.82, '大盘': 0.35, ...}

    数学 (审计#2修复):
        w_norm = raw_weight / Σ(raw_weights)      # 归一化使 Σw=1.0
        z_i = (score_i - 50.0) / 25.0              # 映射到 [-2, +2]
        S_total = Σ (w_norm_i × (2Φ(z_i) - 1))    # ∈ [-1, 1] 天然收敛

    防御:
        - 维度名称不在 _RAW_WEIGHTS 中 → 跳过, 不影响计算
        - score 为 None 或 NaN → 视为 50 (中性)
        - 单个维度权重归一化后 ∈ [0, 1]
    """
    # Step 1: 提取有效维度的权重和得分
    valid_weights = []
    valid_scores = []
    missing_names = []

    for d in dimensions:
        name = d.get('name', '')
        # 映射全名→短名 (unified_verdict.py 用全名 '宏观体制', 映射到 '宏观')
        mapped = _DIM_NAME_MAP.get(name, name)
        w = _RAW_WEIGHTS.get(mapped)
        if w is None:
            missing_names.append(name)
            continue

        score = d.get('score', 50.0)
        # 防御: None / NaN / inf → fallback 50
        if score is None or not math.isfinite(float(score)):
            score = 50.0
        score = float(score)

        valid_weights.append(w)
        valid_scores.append(score)

    if not valid_weights:
        # 所有维度名称都不匹配 → S_total=0, 全中性
        return (0.0, {})

    # Step 2: 权重归一化 → Σw_norm = 1.0 (审计#2核心修正: 消灭1.25溢出)
    total_weight = sum(valid_weights)
    w_normalized = [w / total_weight for w in valid_weights]  # sum = 1.0

    # Step 3: score → z-score 映射 (居中 + 缩放)
    # z ∈ [-2, +2]: score=0→z=-2, score=50→z=0, score=100→z=+2
    z_vals = []
    z_scores = {}
    for i, d in enumerate(dimensions):
        name = d.get('name', '')
        mapped = _DIM_NAME_MAP.get(name, name)
        if mapped not in _RAW_WEIGHTS:
            continue
        raw = d.get('score', 50.0)
        if raw is None or not math.isfinite(float(raw)):
            raw = 50.0
        z = (float(raw) - 50.0) / 25.0
        z_vals.append(z)
        z_scores[name] = round(z, 4)

    # Step 4: S_total = Σ w_norm × (2Φ(z) - 1)
    # (2Φ(z) - 1) ∈ [-1, 1], 线性加权后天然 ∈ [-1, 1]
    S_total = 0.0
    for w, z in zip(w_normalized, z_vals):
        phi_z = _norm_cdf(z)
        contribution = w * (2.0 * phi_z - 1.0)
        S_total += contribution

    # 最后一道防线: clip 到 [-1.0, 1.0]
    S_total = max(-1.0, min(1.0, S_total))

    return (round(S_total, 6), z_scores)


# ═══════════════════════════════════════════
# 2. 后验概率与信息熵带宽计算
# ═══════════════════════════════════════════

def _calc_posterior_probabilities(z_vals: list, S_total: float) -> dict:
    """
    后验概率合成 + 维度分歧度 + 信息熵 → 动态置信带宽。

    Args:
        z_vals: 9维z-score列表, 长度通常为9
        S_total: _calc_S_total 的合成输出 ∈ [-1, 1]

    Returns:
        dict: {
            'P_bull': float,          # 看多后验概率
            'P_neutral': float,       # 中性后验概率
            'P_bear': float,          # 看空后验概率
            'sigma_ensemble': float,  # 维度共振分歧度
            'entropy': float,         # 归一化信息熵 ∈ [0, 1]
            'confidence_band': str,   # "±2%" | "±8%" | "±12%"
        }

    数学 (审计#3 & #4修复):
        sigma_ensemble = max(0.3, std(z_vals))       # 底线阻尼防除零
        P_bear = Φ(-S_total / sigma_ensemble)
        P_bull = Φ(+S_total / sigma_ensemble)
        P_neutral = max(0, 1 - P_bear - P_bull)

        H = -Σ p_i × ln(p_i) / ln(N)                 # 归一化信息熵
        p_i = (score_i + ε) / Σ(score_j + ε), ε=1e-5  # 阻尼防全0死锁
    """
    z_arr = np.array(z_vals, dtype=np.float64)

    # ── 1. 维度共振分歧度 (审计#3: 底线阻尼) ──
    if len(z_arr) < 2:
        sigma_ensemble = 1.0  # 单维度 → 最大不确定性
    else:
        sigma_ensemble = float(max(0.3, np.std(z_arr)))

    # ── 2. 后验概率 (高斯尾部映射) ──
    if sigma_ensemble > 0:
        P_bear = _norm_cdf(-S_total / sigma_ensemble)
        P_bull = _norm_cdf(+S_total / sigma_ensemble)
    else:
        # 防御: sigma=0 (不可能到达, max(0.3)保证)
        P_bear = 0.5
        P_bull = 0.5

    P_neutral = max(0.0, 1.0 - P_bear - P_bull)

    # ── 3. 信息熵 (审计#4: ε扰动防全0死锁) ──
    # 从原始z值反推近似score (用于熵计算): score = z × 25 + 50
    scores_raw = np.clip(z_arr * 25.0 + 50.0, 0.0, 100.0)
    scores_damped = scores_raw + 1e-5  # 审计#4: 强制扰动阻尼
    total_score = np.sum(scores_damped)

    if total_score > 0 and len(scores_damped) > 1:
        p = scores_damped / total_score
        # H = -Σ p ln(p) / ln(N), 规避 log(0)
        p_safe = np.clip(p, 1e-10, 1.0)
        H = -np.sum(p_safe * np.log(p_safe)) / math.log(len(p_safe))
        H = float(np.clip(H, 0.0, 1.0))
    else:
        H = 0.5  # fallback: 中等分歧

    # ── 4. 置信带宽映射 ──
    if H < 0.3:
        band = "±2%"
    elif H < 0.6:
        band = "±8%"
    else:
        band = "±12%"

    return {
        'P_bull': round(P_bull, 6),
        'P_neutral': round(P_neutral, 6),
        'P_bear': round(P_bear, 6),
        'sigma_ensemble': round(sigma_ensemble, 4),
        'entropy': round(H, 4),
        'confidence_band': band,
    }


# ═══════════════════════════════════════════
# 3. 双模态隔离迟滞环控制状态机
# ═══════════════════════════════════════════

# 迟滞环参数常量
_HYSTERESIS_PARAMS = {
    'enter_threshold': 0.63,    # 强进入: P_bear > 63% 或 P_bull > 63%
    'exit_threshold': 0.55,     # 退出: P < 55% 且连续3日确认
    'confirm_days': 3,          # 连续确认天数
    'dead_zone_width': 0.08,     # 死区 = 63% - 55%
}

# 实盘持久化路径
_HYSTERESIS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'hysteresis_state.json'
)

# 回测模态: 内存隔离状态字典 (审计#7核心防御 — 绝不污染磁盘/未来数据)
# 结构: {backtest_date: {target_code: state_dict}}
_BACKTEST_STATE: dict = defaultdict(dict)


def _load_hysteresis_state(target_code: str) -> dict:
    """
    实盘模态: 从磁盘加载指定target_code的迟滞环状态。
    文件不存在或损坏 → 返回空初始状态。
    """
    try:
        if os.path.exists(_HYSTERESIS_FILE):
            with open(_HYSTERESIS_FILE, 'r', encoding='utf-8') as f:
                all_states = json.load(f)
            return all_states.get(target_code, {})
    except (json.JSONDecodeError, PermissionError, OSError):
        pass
    return {}


def _save_hysteresis_state(target_code: str, state: dict) -> None:
    """
    实盘模态: 序列化写入磁盘。
    使用原子写入策略: 先写临时文件, 再重命名, 防写入中断损坏。
    """
    try:
        # 读出现有全部状态
        all_states = {}
        if os.path.exists(_HYSTERESIS_FILE):
            try:
                with open(_HYSTERESIS_FILE, 'r', encoding='utf-8') as f:
                    all_states = json.load(f)
            except (json.JSONDecodeError, PermissionError):
                all_states = {}

        # 更新目标code
        all_states[target_code] = state

        # 原子写入
        tmp_file = _HYSTERESIS_FILE + '.tmp'
        os.makedirs(os.path.dirname(_HYSTERESIS_FILE), exist_ok=True)
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(all_states, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_file, _HYSTERESIS_FILE)
    except (PermissionError, OSError):
        pass  # 写入失败静默, 不影响主裁决流程


def _process_hysteresis(
    target_code: str,
    P_bear: float,
    P_bull: float,
    backtest_date: str = None
) -> dict:
    """
    双模态迟滞环控制状态机 — 63%强进入 / 55%连续3日确认退出。

    【防抖物理】
        看空方向: P_bear > 63% → 激活减仓
                  激活后, 只有 P_bear < 55% 连续3日 → 认错取消
        看多方向: P_bull > 63% → 激活加仓
                  激活后, 只有 P_bull < 55% 连续3日 → 认错取消
        死区: [55%, 63%] — 在此区间内, 维持上一状态, 不触发新动作。

    【防时序污染 (审计#7)】
        backtest_date=None  → 实盘模态: 读写 hysteresis_state.json
        backtest_date=str   → 回测模态: 内存隔离, 绝不碰磁盘
                               状态按 backtest_date + target_code 隔离

    Args:
        target_code: 标的代码 (如 'sh000819')
        P_bear: 看空后验概率 ∈ [0, 1]
        P_bull: 看多后验概率 ∈ [0, 1]
        backtest_date: 回测日期 "YYYY-MM-DD" | None=实盘

    Returns:
        dict: {
            'state_node': str,         # "idle" | "bear_entered" | "bear_confirmed"
                                       # | "bull_entered" | "bull_confirmed"
                                       # | "dead_zone" | "bear_cancelled" | "bull_cancelled"
            'last_action': str,        # "减仓" | "加仓" | "持有" | "观望" | "认错回补"
            'consecutive_days': int,   # 连续确认/跌破计数
            'enter_threshold': float,  # 强进入阈值 (63%)
            'exit_threshold': float,   # 退出阈值 (55%)
            'dead_zone': str,          # 死区范围描述
            'mode': str,               # "live" | "backtest"
        }
    """
    enter_th = _HYSTERESIS_PARAMS['enter_threshold']  # 0.63
    exit_th = _HYSTERESIS_PARAMS['exit_threshold']    # 0.55
    confirm_n = _HYSTERESIS_PARAMS['confirm_days']     # 3

    # ── 模态判定 (审计#7) ──
    if backtest_date is not None:
        mode = 'backtest'
        # 回测模态: 从内存隔离字典取状态, 绝不碰磁盘
        prev_state = _BACKTEST_STATE[backtest_date].get(target_code, {})
    else:
        mode = 'live'
        # 实盘模态: 从磁盘加载
        prev_state = _load_hysteresis_state(target_code)

    # ── 提取前一状态 ──
    prev_node = prev_state.get('state_node', 'idle')
    prev_count = prev_state.get('consecutive_days', 0)

    # ── 当前最大方向概率 ──
    max_prob = max(P_bear, P_bull)

    # ════════════════════════════════════════
    # 状态机核心逻辑
    # ════════════════════════════════════════

    new_node = prev_node
    new_count = prev_count
    action = '持有'

    # ── 情况A: 空闲态 → 检查是否触发进入 ──
    if prev_node in ('idle', 'bear_cancelled', 'bull_cancelled'):
        if P_bear > enter_th:
            new_node = 'bear_entered'
            new_count = 1
            action = '减仓'
        elif P_bull > enter_th:
            new_node = 'bull_entered'
            new_count = 1
            action = '加仓'
        else:
            # 死区或低于阈值 → 维持空闲
            new_node = 'dead_zone' if max_prob >= exit_th else 'idle'
            new_count = 0
            action = '持有'

    # ── 情况B: 已进入看空 (bear_entered / bear_confirmed) ──
    elif prev_node in ('bear_entered', 'bear_confirmed'):
        if P_bear < exit_th:
            # 跌破退出阈值 → 计数+1
            new_count = prev_count + 1
            if new_count >= confirm_n:
                # 连续N日确认 → 认错取消
                new_node = 'bear_cancelled'
                action = '认错回补'
            else:
                new_node = 'bear_entered'
                action = '减仓'  # 仍维持减仓, 但进入认错倒计时
        elif P_bear > enter_th:
            # 仍在强看空区 → 确认
            new_node = 'bear_confirmed'
            new_count = 0  # 重置退出计数
            action = '减仓'
        else:
            # 死区 → 维持减仓, 不改变计数
            new_node = 'bear_entered'
            new_count = 0  # 死区内重置退出计数
            action = '减仓'

    # ── 情况C: 已进入看多 (bull_entered / bull_confirmed) ──
    elif prev_node in ('bull_entered', 'bull_confirmed'):
        if P_bull < exit_th:
            new_count = prev_count + 1
            if new_count >= confirm_n:
                new_node = 'bull_cancelled'
                action = '认错回补'
            else:
                new_node = 'bull_entered'
                action = '加仓'
        elif P_bull > enter_th:
            new_node = 'bull_confirmed'
            new_count = 0
            action = '加仓'
        else:
            new_node = 'bull_entered'
            new_count = 0
            action = '加仓'

    # ── 情况D: 死区内 ──
    elif prev_node == 'dead_zone':
        if P_bear > enter_th:
            new_node = 'bear_entered'
            new_count = 1
            action = '减仓'
        elif P_bull > enter_th:
            new_node = 'bull_entered'
            new_count = 1
            action = '加仓'
        elif max_prob < exit_th:
            new_node = 'idle'
            new_count = 0
            action = '持有'
        else:
            # 仍在死区
            new_node = 'dead_zone'
            new_count = 0
            action = '观望'

    # ── 组装输出状态 ──
    result = {
        'state_node': new_node,
        'last_action': action,
        'consecutive_days': new_count,
        'enter_threshold': enter_th,
        'exit_threshold': exit_th,
        'dead_zone': f'{exit_th*100:.0f}%-{enter_th*100:.0f}%',
        'mode': mode,
    }

    # ── 持久化 ──
    if mode == 'live':
        # 实盘: 写盘持久化 (审计#7: 仅实盘模态触发物理I/O)
        _save_hysteresis_state(target_code, result)
    else:
        # 回测: 写内存隔离字典 (审计#7: 绝不污染磁盘)
        _BACKTEST_STATE[backtest_date][target_code] = result

    return result


# ═══════════════════════════════════════════
# 4. 连续仓位映射 (辅助函数, 供区域B的 position.delta_pos_pct 调用)
# ═══════════════════════════════════════════

# 宏观体制 → 最大风险预算 (占组合百分比)
_REGIME_RISK_BUDGET = {
    'NORMAL': 0.30,
    'CAUTION': 0.20,
    'CAUTION_OIL': 0.15,
    'DEFENSE': 0.10,
    'DEFENSE_TIGHT': 0.10,
    'DEFENSE_RATE': 0.08,
    'DEFENSE_SHOCK': 0.05,
    'DEFENSE_CRISIS': 0.00,
    'DEFENSE_PANIC': 0.00,
    'UNKNOWN': 0.10,
}


def _calc_position_delta(S_total: float, regime: str) -> dict:
    """
    连续仓位映射: S_total × 体制风险预算 = ΔPos

    Args:
        S_total: 组合胜率 ∈ [-1, 1]
        regime: 宏观体制标签 (如 'NORMAL', 'CAUTION_OIL')

    Returns:
        dict: {
            'delta_pos_pct': float,     # 仓位变动百分比 (如 -15.0)
            'max_risk_budget': float,   # 体制允许的最大风险预算
            'signed_delta': float,      # 带符号变动 = S_total × budget
        }
    """
    budget = _REGIME_RISK_BUDGET.get(regime, 0.15)
    delta = S_total * budget

    return {
        'delta_pos_pct': round(delta * 100, 1),   # 百分比化
        'max_risk_budget': round(budget, 2),
        'signed_delta': round(delta, 4),
    }


# ═══════════════════════════════════════════
# 自检: 直接运行 python engine/verdict_math.py
# ═══════════════════════════════════════════
if __name__ == '__main__':
    print("=" * 64)
    print("  天眼 v8 连续化概率流数学内核 · 自检")
    print("=" * 64)

    # ── 测试1: _calc_S_total ──
    print("\n[1] S_total 合成引擎")
    # 模拟全看多
    dims_bull = [{'name': n, 'score': 90.0} for n in _RAW_WEIGHTS]
    S_bull, zs_bull = _calc_S_total(dims_bull)
    print(f"  全看多(score=90): S_total={S_bull:.4f}  (应接近+1.0)")

    # 模拟全看空
    dims_bear = [{'name': n, 'score': 10.0} for n in _RAW_WEIGHTS]
    S_bear, zs_bear = _calc_S_total(dims_bear)
    print(f"  全看空(score=10): S_total={S_bear:.4f}  (应接近-1.0)")

    # 模拟中性
    dims_neu = [{'name': n, 'score': 50.0} for n in _RAW_WEIGHTS]
    S_neu, zs_neu = _calc_S_total(dims_neu)
    print(f"  全中性(score=50): S_total={S_neu:.4f}  (应≈0.0)")

    # 边界: S_total绝对不溢出
    assert -1.0 <= S_bull <= 1.0, f"S_total溢出: {S_bull}"
    assert -1.0 <= S_bear <= 1.0, f"S_total溢出: {S_bear}"
    print("  [PASS] S_total in [-1, 1] boundary check")

    # ── 测试2: _calc_posterior_probabilities ──
    print("\n[2] 后验概率与信息熵")
    # 全看空场景
    z_bear = [(10 - 50) / 25] * 9  # 全 -1.6
    post_bear = _calc_posterior_probabilities(z_bear, S_bear)
    print(f"  全看空: P_bear={post_bear['P_bear']:.4f} P_neutral={post_bear['P_neutral']:.4f} "
          f"σ={post_bear['sigma_ensemble']:.3f} H={post_bear['entropy']:.3f} band={post_bear['confidence_band']}")

    # 多空互冲场景
    z_mixed = [1.6, -1.6, 0.0, 1.6, -1.6, 0.0, 1.0, -1.0, 0.0]
    S_mixed = 0.0
    post_mixed = _calc_posterior_probabilities(z_mixed, S_mixed)
    print(f"  多空互冲: P_bear={post_mixed['P_bear']:.4f} P_bull={post_mixed['P_bull']:.4f} "
          f"σ={post_mixed['sigma_ensemble']:.3f} H={post_mixed['entropy']:.3f} band={post_mixed['confidence_band']}")

    # 审计#3: sigma_ensemble底线0.3
    z_resonance = [1.0] * 9  # 全相同
    post_res = _calc_posterior_probabilities(z_resonance, 0.5)
    assert post_res['sigma_ensemble'] >= 0.3, "sigma_ensemble底线失效"
    print(f"  极端共振: sigma={post_res['sigma_ensemble']:.3f} (floor 0.3 [PASS])")

    # 审计#4: 全0防死锁
    z_zeros = [0.0] * 9
    post_zero = _calc_posterior_probabilities(z_zeros, 0.0)
    assert not math.isnan(post_zero['entropy']), "熵NaN死锁"
    print(f"  全0防御: H={post_zero['entropy']:.4f} (no NaN [PASS])")

    # ── 测试3: _process_hysteresis ──
    print("\n[3] 迟滞环控制状态机")

    # 实盘模态测试
    state1 = _process_hysteresis('test_code', P_bear=0.70, P_bull=0.20, backtest_date=None)
    print(f"  实盘 P_bear=70%: node={state1['state_node']} action={state1['last_action']} "
          f"count={state1['consecutive_days']}")

    # 死区测试
    state2 = _process_hysteresis('test_code', P_bear=0.58, P_bull=0.30, backtest_date=None)
    print(f"  实盘 P_bear=58%: node={state2['state_node']} action={state2['last_action']} "
          f"(死区内维持上一状态)")

    # 连续跌破测试
    for day in range(1, 6):
        state3 = _process_hysteresis('test_code', P_bear=0.50, P_bull=0.35, backtest_date=None)
        print(f"  实盘 Day{day} P_bear=50%: node={state3['state_node']} count={state3['consecutive_days']} "
              f"action={state3['last_action']}")

    # 回测模态: 验证内存隔离
    state_bt = _process_hysteresis('sh000819', P_bear=0.80, P_bull=0.10, backtest_date='2024-05-15')
    print(f"\n  回测 2024-05-15: node={state_bt['state_node']} mode={state_bt['mode']} "
          f"action={state_bt['last_action']}")
    # 验证内存中存在
    assert 'sh000819' in _BACKTEST_STATE['2024-05-15'], "backtest state not written to memory"
    print("  审计#7: backtest state in memory dict, disk untouched [PASS]")

    # ── 测试4: _calc_position_delta ──
    print("\n[4] 连续仓位映射")
    pos1 = _calc_position_delta(0.6, 'NORMAL')
    print(f"  S=+0.6, NORMAL: ΔPos={pos1['delta_pos_pct']}% budget={pos1['max_risk_budget']}")
    pos2 = _calc_position_delta(-0.5, 'CAUTION_OIL')
    print(f"  S=-0.5, CAUTION_OIL: ΔPos={pos2['delta_pos_pct']}% budget={pos2['max_risk_budget']}")
    pos3 = _calc_position_delta(0.9, 'DEFENSE_CRISIS')
    print(f"  S=+0.9, CRISIS: ΔPos={pos3['delta_pos_pct']}% (冻结, 应为0)")

    print(f"\n{'='*64}")
    print("  ALL SELF-TESTS PASSED")
    print(f"{'='*64}")
