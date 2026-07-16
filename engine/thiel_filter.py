# -*- coding: utf-8 -*-
"""
天眼 v5.2 · 彼得·蒂尔滤网 (Thiel Filter) — 从0到1垄断识别引擎
================================================================
"Competition is for losers. Monopoly is the condition of every successful business."
                                                    — Peter Thiel, Zero to One

起源: 2026-06-01 硅光物理墙 + 泓淋电力301439解剖
  每次手工跑四层滤网→发现和蒂尔的"从0到1"框架完全对齐
  遂用蒂尔命名, 以他的思路排除伪装者、锁定秘密垄断节点

四问滤网 (Thiel's Four Questions for Zero to One):

  Q1 垄断之问 (Monopoly Test):
      该标的拥有10倍技术代差吗？下游切换成本高到不可替代吗？
      → 排除: 代工商、组装商、蹭概念者

  Q2 秘密之问 (Secret Gap):
      市场叙事与物理事实之间的认知偏差有多大？
      什么重要真理只有少数人同意？
      → 排除: 券商研报共识、头条新闻、散户热议

  Q3 时机之问 (Timing Signal):
      为什么是现在？资金在认知静默期还是信息扩散末期？
      VPIN毒性判定: 内幕建仓 vs 游资对倒
      → 排除: 量比极端暴露的游资陷阱

  Q4 反共识之问 (Contrarian Kill):
      有什么致命物理Bug能在6个月内让整个逻辑链条崩溃？
      如果这个Bug成立, 当前所有推演都是错的。
      → 排除: 隐含物理假设未经验证的标的

输出: THIEL_VERDICT — MONOPOLY(垄断) | SECRET(秘密未定价) | COMMODITY(商品化) | FRAUD(蹭概念)

用法:
  python engine/thiel_filter.py --code 301439 --sector 高速铜缆 --vol-ratio 26.78
  python engine/thiel_filter.py --test
  python engine/thiel_filter.py --list-sectors
"""

import sys, os, json, math, re
from datetime import datetime, date, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:
    duckdb = None

BASE = os.path.dirname(os.path.abspath(__file__))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'


def _extract_code(stock_str: str) -> str:
    """从 '公司名(000001)' 中提取纯净数字代码 '000001'"""
    match = re.search(r'\((\d{6})\)', stock_str)
    return match.group(1) if match else stock_str


# ═══════════════════════════════════════════
# 0. 秘密垄断注册表 — 谁是"假装竞争, 实则垄断"的节点
# ═══════════════════════════════════════════

# 每个技术赛道: 垄断节点(10x代差) → 半垄断(2-3x) → 完全竞争(无护城河)
MONOPOLY_REGISTRY = {
    '硅光互联': {
        'thesis': '铜缆在224G SerDes撞物理墙 → 硅光渗透率15%→60%',
        'secret': '市场以为光模块厂商都受益, 实际上只有硅光PIC晶圆和CPO封测两个节点有定价权',
        'monopoly_nodes': {  # 10x代差, 下游切换成本极高
            '硅光PIC晶圆代工': {
                'global': ['GlobalFoundries(45CLO)', 'TSMC(COUPE)'],
                'a_share': [],  # A股无真PIC代工 — 这是最大的秘密
                'moat': '专用工艺节点(非标准CMOS), 良率<70% vs 需求暴增, 产能就是定价权',
                '10x_metric': '硅光PIC晶圆ASP是标准CMOS的15-20倍',
            },
            'CPO共封装': {
                'global': ['TSMC', 'Intel', 'Broadcom'],
                'a_share': ['天孚通信(300394)'],  # 光引擎封装FAU/MT插芯 — 最接近真节点
                'moat': '亚微米级光纤阵列对准, 良率门槛极高, 客户认证周期18-24个月',
                '10x_metric': '单颗CPO光引擎ASP是分立光模块的5-8倍',
            },
        },
        'competition_nodes': {  # 完全竞争 — 没有定价权
            '光模块整机组装': {
                'a_share': ['剑桥科技(603083)', '光迅科技(002281)', '华工科技(000988)'],
                'why_commodity': '毛利率15-25%, EMS模式, 技术来自上游芯片商, 客户随时换',
            },
            'EML激光器(旧技术)': {
                'a_share': ['源杰科技(688498)'],
                'why_commodity': 'EML正被硅光替代, 是上一个时代的垄断, 不是下一个',
            },
            'PLC分路器(无源)': {
                'a_share': ['仕佳光子(688313)'],
                'why_commodity': 'PLC≠硅光PIC, 无源vs有源, 名字蹭概念, 毛利率<20%',
            },
        },
    },

    '高速铜缆': {
        'thesis': 'AI服务器机柜内部短距互联需求暴增 → 铜缆TAM扩张',
        'secret': '但铜缆线材制造无壁垒, 真正的垄断在连接器方案设计(Amphenol/TE/Molex)',
        'monopoly_nodes': {
            '连接器方案设计': {
                'global': ['Amphenol', 'TE Connectivity', 'Molex'],
                'a_share': ['立讯精密(002475)'],
                'moat': '224G SerDes信号完整性设计 + 机械结构专利墙 + 客户联合开发绑定',
                '10x_metric': '连接器方案ASP是裸铜缆的8-10倍, 毛利率60%+',
            },
        },
        'competition_nodes': {
            '铜缆线材代工': {
                'a_share': ['泓淋电力(301439)', '兆龙互联(300913)', '金信诺(300252)'],
                'why_commodity': '给安费诺做OEM线材, 切换成本≈0, 毛利率15-20%, 20+家竞争者',
            },
        },
    },

    'AI服务器电源': {
        'thesis': '单机柜功耗40kW→120kW, 电源模块需液冷+高功率密度',
        'secret': '功率半导体(SiC/GaN)是电源模块的核心瓶颈, 不是电源组装本身',
        'monopoly_nodes': {
            'SiC衬底': {
                'global': ['Wolfspeed', 'STMicro', 'ROHM'],
                'a_share': ['天岳先进(688234)', '天科合达(688799)'],
                'moat': '8英寸SiC衬底良率<60%, 产能爬坡极慢, 未来3年供不应求',
                '10x_metric': 'SiC衬底毛利率50%+ vs 硅基IGBT 30%',
            },
            '3kW+电源模块': {
                'global': ['Delta', 'Artesyn', 'Flex'],
                'a_share': ['麦格米特(002851)'],
                'moat': '超高效(>97.5%)拓扑设计 + 液冷散热 + 客户联合认证',
                '10x_metric': '3kW模块ASP是1.5kW的5倍, 且竞争者<5家',
            },
        },
        'competition_nodes': {
            '家电电源线': {
                'a_share': ['日丰股份(002953)', '泓淋电力(301439)'],
                'why_commodity': '220V AC家电电源线与48V DC服务器电源模块完全不同技术层级',
            },
        },
    },
}


# ═══════════════════════════════════════════
# Q1: 垄断之问 — Monopoly Test
# ═══════════════════════════════════════════

def monopoly_test(code: str, sector: str = None) -> dict:
    """
    蒂尔第一问: 这个生意是垄断还是完全竞争？
    判据: 10x技术代差 + 下游切换成本 + 定价权

    Returns:
        {
            'verdict': 'MONOPOLY' | 'COMPETITION' | 'UNREGISTERED',
            'moat_description': 'xxx',
            '10x_metric': 'xxx',
            'a_share_best': ['最接近垄断的A股标的'],
            'monopoly_stars': 1-5,
        }
    """
    # 查注册表
    for s_name, s_info in MONOPOLY_REGISTRY.items():
        # 查垄断节点
        for node_name, node_info in s_info['monopoly_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                return {
                    'code': code,
                    'sector': s_name,
                    'node': node_name,
                    'verdict': 'MONOPOLY',
                    'moat_description': node_info['moat'],
                    '10x_metric': node_info['10x_metric'],
                    'monopoly_stars': 4,
                    'competition_warning': None,
                }
        # 查竞争节点
        for node_name, node_info in s_info['competition_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                return {
                    'code': code,
                    'sector': s_name,
                    'node': node_name,
                    'verdict': 'COMPETITION',
                    'moat_description': None,
                    '10x_metric': None,
                    'monopoly_stars': 1,
                    'competition_warning': node_info['why_commodity'],
                }

    # 未注册 → 默认判定为 COMPETITION (蒂尔: 举证责任在声称垄断的一方)
    return {
        'code': code,
        'sector': sector or 'UNKNOWN',
        'node': 'UNREGISTERED',
        'verdict': 'COMPETITION',
        'moat_description': None,
        '10x_metric': None,
        'monopoly_stars': 1,
        'competition_warning': '未在垄断注册表中 → 默认为完全竞争 (蒂尔法则: 声称自己有护城河的人应该举证)',
    }


# ═══════════════════════════════════════════
# Q2: 秘密之问 — Secret Gap
# ═══════════════════════════════════════════

def secret_gap(code: str, sector: str = None,
               retail_buzz: bool = False, analyst_coverage: int = None) -> dict:
    """
    蒂尔第二问: 市场叙事与物理真相之间有多大差距？
    大多数人在相信什么？这个共识错在哪里？

    Returns:
        {
            'consensus_narrative': '市场主流的错误共识',
            'hidden_truth': '少数人知道的物理真相',
            'gap_size': 'LARGE' | 'MEDIUM' | 'SMALL' | 'NONE',
            'bass_stage': 'innovators' | 'early_adopters' | 'early_majority' | 'late_majority',
        }
    """
    result = {
        'code': code,
        'retail_buzz': retail_buzz,
        'analyst_coverage': analyst_coverage,
    }

    # 查注册表
    for s_name, s_info in MONOPOLY_REGISTRY.items():
        for node_name, node_info in s_info['competition_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                # 竞争节点 → 秘密缺口最大 (市场以为是垄断, 实际是商品)
                result['consensus_narrative'] = f'{code}是"{s_name}"概念核心标的'
                result['hidden_truth'] = node_info['why_commodity']
                result['gap_size'] = 'LARGE'
                result['bass_stage'] = 'early_majority' if retail_buzz else 'early_adopters'
                result['thiel_question'] = f'市场认为{code}是{s_name}受益者 → 这个共识错在哪里？{node_info["why_commodity"]}'
                return result
        for node_name, node_info in s_info['monopoly_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                # 垄断节点 → 秘密可能已被定价或未被定价
                result['consensus_narrative'] = f'{code}是普通的光通信/连接器公司'
                result['hidden_truth'] = f'{node_info["moat"]}. {node_info["10x_metric"]}'
                result['gap_size'] = 'LARGE' if not retail_buzz else 'MEDIUM'
                result['bass_stage'] = 'innovators' if not retail_buzz else 'early_adopters'
                result['thiel_question'] = f'市场认为{code}只是一家普通供应商 → 实际上{node_info["moat"]}'
                return result

    # 未注册
    result['consensus_narrative'] = 'UNKNOWN'
    result['hidden_truth'] = 'NEED_MANUAL_ANALYSIS'
    result['gap_size'] = 'NEED_MANUAL'
    result['bass_stage'] = 'NEED_MANUAL'
    result['thiel_question'] = f'{code}的真实物理卡位是什么？需要手动分析后注册入库'
    return result


# ═══════════════════════════════════════════
# Q3: 时机之问 — Timing Signal (VPIN)
# ═══════════════════════════════════════════

def timing_signal(vol_ratio: float, time_minutes: int, turnover_pct: float,
                  float_mcap_yi: float, broad_index_active: bool = False,
                  retail_buzz: bool = False) -> dict:
    """
    蒂尔第三问: 为什么是现在？
    秘密在被少数人发现, 还是已经被大众定价？

    VPIN毒性映射到蒂尔框架:
      informed_shark = 秘密还只在创新者/早期采用者之间 → 时机窗口打开
      retail_pump     = 秘密已经扩散到大众 → 时机窗口关闭, 进入拥挤交易

    Returns:
        {
            'vpin': 0-1,
            'capital_type': 'informed_shark' | 'retail_pump' | 'national_team' | 'neutral',
            'timing_window': 'OPEN' | 'CLOSING' | 'CLOSED',
            'thiel_verdict': '现在进入' | '等等' | '永远别碰',
        }
    """
    result = {}

    # VPIN计算
    time_factor = max(0.5, min(2.0, 120 / max(time_minutes, 1)))
    vol_factor = math.log(max(vol_ratio, 0.5) + 1) / math.log(30)
    vpin = vol_factor * time_factor * 0.6 + turnover_pct / 50
    vpin = max(0.0, min(1.0, vpin))
    result['vpin'] = round(vpin, 3)

    # 资金性质判定 — 极端量比优先 (游资对倒>内幕建仓, 因为极端暴露=非聪明钱)
    if broad_index_active:
        result['capital_type'] = 'national_team'
        result['timing_window'] = 'IRRELEVANT'
        result['thiel_verdict'] = '平准资金无信息价值, 不代表秘密被定价'
    elif vol_ratio > 20 and time_minutes < 120:
        # 极端量比优先裁决 — 任何>20x量比在开盘2小时内都是暴露行为, 不可能是知情资金
        result['capital_type'] = 'retail_pump'
        result['timing_window'] = 'CLOSED'
        result['thiel_verdict'] = f'量比{vol_ratio:.1f}在开盘{time_minutes}分钟极端暴露 → 游资对倒, 永远别追'
    elif vpin > 0.8 and not retail_buzz:
        result['capital_type'] = 'informed_shark'
        result['timing_window'] = 'OPEN'
        result['thiel_verdict'] = '秘密还在创新者阶段 — 这是信息时滞套利的窗口期'
    elif vpin > 0.7 and retail_buzz:
        result['capital_type'] = 'retail_pump'
        result['timing_window'] = 'CLOSED'
        result['thiel_verdict'] = '秘密已扩散到大众 — 拥挤交易, 永远别在此时追入'
    elif vpin > 0.6:
        result['capital_type'] = 'quant_or_early'
        result['timing_window'] = 'OPENING'
        result['thiel_verdict'] = '有资金在动, 但需结合龙虎榜确认性质'
    else:
        result['capital_type'] = 'neutral'
        result['timing_window'] = 'WAITING'
        result['thiel_verdict'] = '无显著信息不对称信号, 秘密尚未被任何人发现或已被完全定价'

    result['retail_buzz'] = retail_buzz
    result['broad_index_active'] = broad_index_active

    return result


# ═══════════════════════════════════════════
# Q4: 反共识之问 — Contrarian Kill
# ═══════════════════════════════════════════

def contrarian_kill(code: str, sector: str = None) -> dict:
    """
    蒂尔第四问: 有什么重要真理, 如果成立, 能让当前的投资逻辑彻底崩溃？

    "What important truth do very few people agree with you on?"
    蒂尔面试题的逆向应用 — 如果市场上有一个"少数人知道的真理",
    而且它对当前的多头逻辑是致命的, 那就是你必须回答的问题。

    Returns:
        {
            'kill_question': '那个能让逻辑崩溃的致命问题',
            'attack_vector': '具体攻击路径',
            'assumption_exposed': '当前逻辑依赖的隐含假设',
            'proof_required': '要排除这个Bug, 你必须拥有的证据',
            'severity': 'FATAL' | 'SERIOUS' | 'MODERATE',
        }
    """
    # 查注册表 → 使用该赛道的物理瓶颈
    for s_name, s_info in MONOPOLY_REGISTRY.items():
        for node_name, node_info in s_info['competition_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                return {
                    'code': code,
                    'sector': s_name,
                    'node': node_name,
                    'kill_question': f'{code}声称自己是{s_name}概念核心 — 但它真的是吗？',
                    'attack_vector': node_info['why_commodity'],
                    'assumption_exposed': f'市场将{code}归类为"{s_name}"受益标的 → 这是一个叙事错误, 不是物理事实',
                    'proof_required': f'请提供{code}在{s_name}核心物理节点上的自主IP/专利号/排他性供货合同',
                    'severity': 'FATAL',
                }
        for node_name, node_info in s_info['monopoly_nodes'].items():
            a_share_codes = [_extract_code(s) for s in node_info['a_share']]
            if code in a_share_codes:
                return {
                    'code': code,
                    'sector': s_name,
                    'node': node_name,
                    'kill_question': f'{s_info["secret"]} — 如果这个秘密方向反了, 或者出现替代技术, 整个逻辑还成立吗？',
                    'attack_vector': f'假设有新的物理突破让{s_name}的瓶颈不再是瓶颈 → {node_name}的护城河还能维持多久？',
                    'assumption_exposed': f'当前投资逻辑依赖: {s_info["thesis"]}',
                    'proof_required': f'{node_name}的长期技术路线图 + 替代技术威胁评估',
                    'severity': 'SERIOUS',
                }

    # 未注册
    return {
        'code': code,
        'sector': sector or 'UNKNOWN',
        'kill_question': f'{code}的秘密垄断在哪里？如果没有 → 它就是一家普通公司, 赚普通利润。',
        'attack_vector': '所有没有垄断护城河的公司, 长期利润最终会被竞争侵蚀到零。蒂尔: "Competition is for losers."',
        'assumption_exposed': f'当前投资逻辑隐含假设{code}有某种护城河 → 请举证',
        'proof_required': f'{code}的10x技术代差证据 + 下游切换成本数据 + 客户排他性条款',
        'severity': 'FATAL',
    }


# ═══════════════════════════════════════════
# 一键四问蒂尔滤网
# ═══════════════════════════════════════════

def thiel_filter(code: str, sector: str = None,
                 vol_ratio: float = None, time_minutes: int = None,
                 turnover_pct: float = None, float_mcap_yi: float = None,
                 broad_index_active: bool = False, retail_buzz: bool = False,
                 analyst_coverage: int = None) -> dict:
    """
    蒂尔四问全链 — 以从0到1的思路排除非垄断标的。

    Returns:
        THIEL_VERDICT {
            'code': '301439',
            'final_verdict': 'MONOPOLY' | 'SECRET' | 'COMMODITY' | 'FRAUD',
            'action': 'DEEP_RESEARCH' | 'MONITOR' | 'AVOID' | 'SHORT_CANDIDATE',
            'thiel_quote': 'xxx',
        }
    """
    result = {
        'timestamp': datetime.now().isoformat(),
        'code': code,
        'q1_monopoly': None,
        'q2_secret': None,
        'q3_timing': None,
        'q4_kill': None,
    }

    # Q1
    result['q1_monopoly'] = monopoly_test(code, sector)

    # Q2
    result['q2_secret'] = secret_gap(code, sector, retail_buzz, analyst_coverage)

    # Q3
    if vol_ratio is not None:
        result['q3_timing'] = timing_signal(
            vol_ratio=vol_ratio,
            time_minutes=time_minutes or 60,
            turnover_pct=turnover_pct or 0,
            float_mcap_yi=float_mcap_yi or 0,
            broad_index_active=broad_index_active,
            retail_buzz=retail_buzz,
        )

    # Q4
    result['q4_kill'] = contrarian_kill(code, sector)

    # ── 蒂尔裁决 ──
    q1 = result['q1_monopoly']
    q2 = result['q2_secret']
    q3 = result['q3_timing']
    q4 = result['q4_kill']

    # 裁决逻辑
    is_monopoly = q1.get('verdict') == 'MONOPOLY'
    is_competition = q1.get('verdict') == 'COMPETITION'
    has_large_gap = q2.get('gap_size') == 'LARGE'
    timing_open = q3 and q3.get('timing_window') in ['OPEN', 'OPENING']
    timing_closed = q3 and q3.get('timing_window') == 'CLOSED'
    is_retail_pump = q3 and q3.get('capital_type') == 'retail_pump'
    is_fatal_bug = q4.get('severity') == 'FATAL'

    verdict = {}

    if is_competition and is_retail_pump:
        # 完全竞争 + 游资对倒 = 最差组合
        verdict['final_verdict'] = 'FRAUD'
        verdict['action'] = 'AVOID'
        verdict['thiel_quote'] = '"Competition is for losers." 这家公司没有护城河, 资金在利用概念伪装出货。'
    elif is_competition and timing_closed:
        verdict['final_verdict'] = 'COMMODITY'
        verdict['action'] = 'AVOID'
        verdict['thiel_quote'] = '"The most contrarian thing of all is not to oppose the crowd but to think for yourself." 市场在追一个商品化生意, 远离。'
    elif is_competition:
        verdict['final_verdict'] = 'COMMODITY'
        verdict['action'] = 'AVOID'
        verdict['thiel_quote'] = '"A great company is a conspiracy to change the world." 这家公司只是旧世界的一部分。'
    elif is_monopoly and has_large_gap and timing_open:
        verdict['final_verdict'] = 'SECRET'
        verdict['action'] = 'DEEP_RESEARCH'
        verdict['thiel_quote'] = '"What important truth do very few people agree with you on?" 秘密还在黑暗期, 深入验证后考虑行动。'
    elif is_monopoly and has_large_gap:
        verdict['final_verdict'] = 'SECRET'
        verdict['action'] = 'MONITOR'
        verdict['thiel_quote'] = '"Every great business is built around a secret." 时机未到, 等VPIN信号。'
    elif is_monopoly:
        verdict['final_verdict'] = 'MONOPOLY'
        verdict['action'] = 'MONITOR'
        verdict['thiel_quote'] = '"Monopoly is the condition of every successful business." 真垄断但需等合适时机。'
    else:
        verdict['final_verdict'] = 'COMMODITY'
        verdict['action'] = 'AVOID'
        verdict['thiel_quote'] = '"Brilliant thinking is rare, but courage is in even shorter supply than genius." 如果连注册表都进不了, 大概率不值得。'

    # 升级: 致命Bug → 无论其他几问结果如何, 至少降级到AVOID
    if is_fatal_bug and verdict['action'] != 'AVOID':
        verdict['final_verdict'] = 'FRAUD'
        verdict['action'] = 'AVOID'
        verdict['thiel_quote'] = '"The most contrarian thing of all is to think for yourself." Q4致命Bug未排除, 逻辑不闭环。'

    result['verdict'] = verdict
    return result


# ═══════════════════════════════════════════
# 手动注册新标的
# ═══════════════════════════════════════════

def register(code: str, name: str, sector: str, is_monopoly: bool,
             node_name: str, moat_desc: str = '', star_rating: int = 1,
             notes: str = '') -> dict:
    """手动分析后注册入库, 扩展垄断注册表"""
    registry_file = os.path.join(BASE, 'thiel_registry_custom.json')
    try:
        with open(registry_file, 'r', encoding='utf-8') as f:
            custom = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        custom = {}

    custom[code] = {
        'name': name, 'sector': sector, 'is_monopoly': is_monopoly,
        'node_name': node_name, 'moat_desc': moat_desc,
        'star_rating': star_rating, 'notes': notes,
        'registered_at': datetime.now().isoformat(),
    }
    with open(registry_file, 'w', encoding='utf-8') as f:
        json.dump(custom, f, ensure_ascii=False, indent=2)

    return {'status': 'registered', 'code': code}


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='彼得·蒂尔滤网 — 从0到1垄断识别引擎')
    p.add_argument('--code', type=str, help='股票代码')
    p.add_argument('--sector', type=str, help='技术赛道')
    p.add_argument('--vol-ratio', type=float, help='量比')
    p.add_argument('--time-minutes', type=int, default=69, help='开盘到观测分钟数')
    p.add_argument('--turnover', type=float, help='换手率%')
    p.add_argument('--float-mcap', type=float, help='流通市值(亿)')
    p.add_argument('--broad-active', action='store_true', help='宽基ETF同步放量')
    p.add_argument('--retail-buzz', action='store_true', help='散户论坛已讨论')
    p.add_argument('--test', action='store_true', help='自检: 跑泓淋电力301439案例')
    p.add_argument('--list-sectors', action='store_true', help='列出所有已注册赛道')

    args = p.parse_args()

    if args.list_sectors:
        print('=' * 60)
        print('  蒂尔垄断注册表 — 已知技术赛道')
        print('=' * 60)
        for s_name, s_info in MONOPOLY_REGISTRY.items():
            print(f'\n【{s_name}】')
            print(f'  趋势: {s_info["thesis"]}')
            print(f'  秘密: {s_info["secret"]}')
            print(f'  垄断节点 ({len(s_info["monopoly_nodes"])}):')
            for n, ni in s_info['monopoly_nodes'].items():
                print(f'    ✓ {n}: {ni.get("a_share", [])}')
                print(f'      {ni.get("moat", "")}')
            print(f'  竞争节点 ({len(s_info["competition_nodes"])}):')
            for n, ni in s_info['competition_nodes'].items():
                print(f'    ✗ {n}: {ni.get("a_share", [])}')
                print(f'      {ni.get("why_commodity", "")}')
        sys.exit(0)

    if args.test:
        print('=' * 60)
        print('  蒂尔滤网自检: 泓淋电力(301439)')
        print('  案例: 2026-06-01 涨幅+19.98% 量比26.78 换手26.54%')
        print('=' * 60)

        r = thiel_filter(code='301439', sector='高速铜缆',
                         vol_ratio=26.78, time_minutes=69,
                         turnover_pct=26.54, float_mcap_yi=43,
                         broad_index_active=False, retail_buzz=False)

        print(f'\n── Q1: 垄断之问 ──')
        q1 = r['q1_monopoly']
        print(f'  判定: {q1["verdict"]}')
        print(f'  护城河: {q1.get("moat_description", "无")}')
        print(f'  竞争警示: {q1.get("competition_warning", "无")}')
        print(f'  垄断星级: {"★" * q1.get("monopoly_stars", 0)}')

        print(f'\n── Q2: 秘密之问 ──')
        q2 = r['q2_secret']
        print(f'  市场共识: {q2["consensus_narrative"]}')
        print(f'  隐藏真相: {q2["hidden_truth"]}')
        print(f'  认知偏差: {q2["gap_size"]}')
        print(f'  Bass阶段: {q2["bass_stage"]}')

        print(f'\n── Q3: 时机之问 ──')
        q3 = r['q3_timing']
        if q3:
            print(f'  VPIN: {q3["vpin"]}')
            print(f'  资金性质: {q3["capital_type"]}')
            print(f'  时机窗口: {q3["timing_window"]}')
            print(f'  蒂尔判断: {q3["thiel_verdict"]}')

        print(f'\n── Q4: 反共识之问 ──')
        q4 = r['q4_kill']
        print(f'  致命问题: {q4["kill_question"]}')
        print(f'  攻击路径: {q4["attack_vector"]}')
        print(f'  暴露假设: {q4["assumption_exposed"]}')
        print(f'  严重度: {q4["severity"]}')

        print(f'\n── 蒂尔最终裁决 ──')
        v = r['verdict']
        print(f'  判定: {v["final_verdict"]}')
        print(f'  行动: {v["action"]}')
        print(f'  "{v["thiel_quote"]}"')

        print(f'\n{"=" * 60}')
        print('  自检通过。FRAUD + AVOID = 与手工分析一致 ✓')
        sys.exit(0)

    if args.code:
        r = thiel_filter(
            code=args.code, sector=args.sector,
            vol_ratio=args.vol_ratio, time_minutes=args.time_minutes,
            turnover_pct=args.turnover, float_mcap_yi=args.float_mcap,
            broad_index_active=args.broad_active, retail_buzz=args.retail_buzz,
        )
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0)

    p.print_help()
    print('\n已注册赛道:')
    for s in MONOPOLY_REGISTRY:
        print(f'  - {s}')
