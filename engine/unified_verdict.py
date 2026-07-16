# -*- coding: utf-8 -*-
"""
天眼2.0 · 一体化宏观量化决策引擎 v1.0
==============================================
三大裁决金字塔（严禁下级逆袭上级）：
  第一统治层: 宏观体制与流动性闸门（最高指挥官，一票否决权）
  第二传导层: 市场量能与筹码结构（战术指挥官，决定胜率）
  第三执行层: NLP新闻与行业催化（士兵，提供选股素材）

多源数据冲突消灭协议:
  时间轴冲突对齐 → 以最新现金市场确认的Regime为准
  指数与个股冲突对齐 → 上涨率<35%时禁止定性为全面Risk-on

铁律强制:
  #3.1 数据不过夜 — K线日期≠今天 → 先刷新再输出
  #3.2 基金持仓用ETF实时价 — 禁止用T+1净值
  #8   数据多源降级 — 单源挂→补搜索→标注

用法: python engine/unified_verdict.py
      from engine.unified_verdict import UnifiedVerdict
"""
import sys, os, io, json, math, time, ssl
from datetime import datetime, date, timedelta

# stdout UTF-8封装: 保存原始引用, 尝试封装, 失败则保留原始
_orig_stdout = sys.stdout
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except (ValueError, AttributeError):
    sys.stdout = _orig_stdout  # 封装失败, 保留原始
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'
os.environ['TQDM_DISABLE'] = '1'

import duckdb
import logging
logging.disable(logging.CRITICAL)

# v8 连续化概率流数学内核
from engine.verdict_math import (
    _calc_S_total, _calc_posterior_probabilities,
    _process_hysteresis, _calc_position_delta,
)

# v8: 终端宽字符对齐 — 中文字符占2个显示宽度, 英文占1个
try:
    import wcwidth
    def _wc_ljust(text: str, width: int) -> str:
        """按视觉显示宽度左对齐, 补齐中英文混排空格。"""
        display_w = wcwidth.wcswidth(text) if hasattr(wcwidth, 'wcswidth') else len(text)
        pad = max(0, width - display_w)
        return text + ' ' * pad
except ImportError:
    def _wc_ljust(text: str, width: int) -> str:
        """wcwidth不可用时回退: 每个中文字符算2宽度。"""
        display_w = sum(2 if ord(c) > 127 else 1 for c in text)
        pad = max(0, width - display_w)
        return text + ' ' * pad

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
ROOT = BASE
today = date.today()
today_str = today.isoformat()
now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

# ═══════════════════════════════════════════
# ε审计标签表 (Water-Filling框架, 可扩展)
# 操盘方伪装预算按c_i(腐败成本)分配——c_i低的通道先被污染(ε高), c_i极高/无限的通道ε≈0
# 新增通道: 只需加一行, 裁决逻辑不改
# ═══════════════════════════════════════════
SIGNAL_EPSILON = {
    # ε≈0: 操盘方结构上无法污染 → 正常权重
    'holder_concentration':  {'c_i': '极高(真金白银+账户级)',  'ε': '≈0', 'weight': 1.0, 'label': '户均集中'},
    '1min_active_direction': {'c_i': '无限(做市商撮合引擎)',  'ε': '≈0', 'weight': 1.0, 'label': '1min方向'},
    'option_iv_skew':        {'c_i': '无限(做市商BS定价跨资产)', 'ε': '≈0', 'weight': 1.0, 'label': '期权skew'},
    'etf_discount':          {'c_i': '极高(AP套利物理强制)',   'ε': '≈0', 'weight': 1.0, 'label': 'ETF折溢价'},
    # ε高: 操盘方可以较低成本污染 → 降权
    'volume_daily':          {'c_i': '极低(只需两个账户对倒)',   'ε': '高', 'weight': 0.3, 'label': '日线量价'},
    'money_flow':            {'c_i': '极低(拆单即可污染)',     'ε': '高', 'weight': 0.0, 'label': '资金流'},
    # ε=junk: 构造数据, 不进裁决
    'retail_institution_split': {'c_i': '构造',               'ε': 'junk', 'weight': 0.0, 'label': '主力散户分档'},
    # ε未标: 默认降权
    '_default':              {'c_i': '未标定',                'ε': '?',   'weight': 0.5, 'label': '未标定'},
}

def epsilon_audit_line(sources_used):
    """生成ε审计行: 'ε审计: 户均集中(ε≈0)✓ | 1min方向(ε≈0)✓ | 资金流(ε=junk)✗'
    sources_used: list of SIGNAL_EPSILON keys
    """
    parts = []
    for key in ['holder_concentration','1min_active_direction','option_iv_skew','etf_discount','volume_daily','money_flow','retail_institution_split']:
        se = SIGNAL_EPSILON.get(key, {})
        label = se.get('label', key)
        eps = se.get('ε', '?')
        used = '✓' if key in sources_used else '✗'
        mark = '⚠' if (eps == '高' and used == '✓') else ''
        parts.append(f'{label}(ε{eps}){used}{mark}')
    return 'ε审计: ' + ' | '.join(parts)

def epsilon_weight(key):
    """返回信号源的ε权重"""
    return SIGNAL_EPSILON.get(key, SIGNAL_EPSILON['_default'])['weight']


# ============================================================
# 数据新鲜度强制检查 (残B升级: 表级→字段级, 引入DataAvailability时间戳对齐)
# ============================================================
def check_data_freshness():
    """
    DataGuard 预检: 返回 (是否全部新鲜, DataCell列表)

    v3.0: 委托给 DataGuard.preflight_check(), 复用 data_availability_schedule.json。
    过期数据不再静默通过 — 调用方从返回值判断是否中断。
    """
    from engine.data_guard import DataGuard
    guard = DataGuard()
    ok, cells = guard.preflight_check()

    # 兼容旧格式: 转为 {key: {fresh, date, lag}} dict
    report = {}
    for cell in cells:
        key = f"{cell.table or '?'}.{cell.field_name or '?'}"
        report[key] = {
            'fresh': cell.is_fresh,
            'date': str(cell.data_date) if cell.data_date else '?',
            'lag': cell.freshness_days,
            'detail': f'conf={cell.confidence:.0f}%',
        }

    return ok, report


def _calc_shibor_slope(macro_rows, lookback=5):
    """
    计算 SHIBOR 隔夜 5日线性回归斜率 (bp/日)。
    数据不足 → 返回0。
    """
    vals = []
    for row in macro_rows[:lookback]:
        v = row[5]  # shibor_on
        if v is not None:
            try:
                if not (isinstance(v, float) and math.isnan(v)):
                    vals.append(float(v))
            except:
                pass
    if len(vals) < 3:
        return 0.0
    vals = vals[::-1]  # 时间升序
    x = list(range(len(vals)))
    try:
        slope = (vals[-1] - vals[0]) / (len(vals) - 1) if len(vals) > 1 else 0
    except:
        slope = 0
    return slope * 100  # 转为bp/日


# ============================================================
# 第一统治层: 宏观体制与流动性闸门
# ============================================================
def layer1_macro_regime():
    """
    最高指挥官，拥有一票否决权。
    核心监控: 10Y美债、WTI/布油、CNH汇率、SHIBOR
    输出: 当前交易体制(Regime) + 一票否决清单
    """
    conn = duckdb.connect(DB)
    try:
        # 读取最新宏观数据 + 前20日用于涨速计算
        macro = conn.execute("""
            SELECT trade_date, us10y, wti, usdcny, china_10y, shibor_on, gold, north_net, south_net
            FROM macro_indicators ORDER BY trade_date DESC LIMIT 25
        """).fetchall()

        if not macro:
            return {'regime': 'UNKNOWN', 'regime_desc': '数据缺失', 'veto_list': [],
                    'us10y': None, 'wti': None, 'cnh': None, 'gold': None}

        cur = macro[0]
        prev = macro[1] if len(macro) > 1 else None

        us10y = cur[1] if cur[1] is not None else (prev[1] if prev else 4.60)
        wti = cur[2] if cur[2] is not None else (prev[2] if prev else 100)
        cnh = cur[3]
        cnh_date = str(cur[0])[:10] if cur and cur[0] else None
        if cnh is None:
            all_cnh = conn.execute("""
                SELECT usdcny, trade_date FROM macro_indicators
                WHERE usdcny IS NOT NULL ORDER BY trade_date DESC LIMIT 10
            """).fetchall()
            if all_cnh:
                cnh = all_cnh[0][0]
                cnh_date = str(all_cnh[0][1])[:10] if all_cnh[0][1] else None

        china_10y = cur[4]

        # 美股大前提 — SPX/纳斯达克 (数据源: global_index_daily)
        spx_close = None; spx_chg_5d = 0; spx_chg_20d = 0
        nasdaq_close = None; nasdaq_chg_5d = 0
        spx_data_lag = 999; spx_status_override = None; spx_latest_date = None
        try:
            spx_rows = conn.execute("""
                SELECT trade_date, close FROM global_index_daily
                WHERE index_code='.INX' ORDER BY trade_date DESC LIMIT 25
            """).fetchall()
            if spx_rows and len(spx_rows) >= 2:
                spx_close = spx_rows[0][1]
                spx_latest_date = spx_rows[0][0]
                if isinstance(spx_latest_date, date):
                    spx_data_lag = (today - spx_latest_date).days
                # v9: 美股数据滞后>3天→退化到A股情绪代理
                if spx_data_lag > 3:
                    spx_status_override = 'stale'
                else:
                    spx_status_override = None
                    if spx_rows[0][1] and spx_rows[4][1] and len(spx_rows) >= 5:
                        spx_chg_5d = (spx_rows[0][1] - spx_rows[4][1]) / spx_rows[4][1] * 100
                    if spx_rows[0][1] and spx_rows[19][1] and len(spx_rows) >= 20:
                        spx_chg_20d = (spx_rows[0][1] - spx_rows[19][1]) / spx_rows[19][1] * 100
            nas_rows = conn.execute("""
                SELECT trade_date, close FROM global_index_daily
                WHERE index_code='.IXIC' ORDER BY trade_date DESC LIMIT 6
            """).fetchall()
            if nas_rows and len(nas_rows) >= 2:
                nasdaq_close = nas_rows[0][1]
                if nas_rows[0][1] and nas_rows[4][1] and len(nas_rows) >= 5:
                    nasdaq_chg_5d = (nas_rows[0][1] - nas_rows[4][1]) / nas_rows[4][1] * 100
        except Exception:
            pass  # 美股数据缺失不影响主流程
            pass  # 美股数据缺失不影响主流程
    finally:
        conn.close()
    shibor = cur[5] if cur[5] is not None else 1.30
    gold = cur[6] if cur[6] is not None else (prev[6] if prev else 4500)
    north = cur[7] if cur[7] is not None else 0
    south = cur[8] if cur[8] is not None else 0

    # WTI 20日涨速 (用于区分"缓涨通胀焦虑" vs "急涨流动性冲击")
    wti_20d_chg = 0
    if len(macro) >= 20:
        wti_20d_ago = macro[19][2]
        if wti_20d_ago and wti:
            wti_20d_chg = (wti - wti_20d_ago) / wti_20d_ago * 100

    macro_date = str(cur[0])[:10] if cur[0] else '?'
    macro_lag = (today - cur[0]).days if isinstance(cur[0], date) else 0

    # --- 判定宏观Regime ---
    # 参考: 华创证券(2026) WTI阈值框架 — 绝对价格 + 涨速双维判定
    #       GitHub FRED-Macro / Euler 综合压力指数思路
    veto_list = []
    risk_factors = []

    # ═══════════════════════════════════════════
    # 美债判定 (维持——4.50%当前环境不触发否决, 仅降仓位)
    # ═══════════════════════════════════════════
    if us10y > 4.70:
        us10y_status = '🔴 清仓线'
        risk_factors.append(f'美10Y={us10y:.2f}%触发4.70%清仓线')
    elif us10y > 4.50:
        us10y_status = '🟡 警戒'
        risk_factors.append(f'美10Y={us10y:.2f}%处于警戒区(>4.50%)')
    else:
        us10y_status = '🟢 安全'

    # ═══════════════════════════════════════════
    # WTI判定 v2.0 — 双维: 绝对价格 + 涨速
    # 数据源: 华创证券(张瑜,2026-03-10) "全球交易模式换档的油价阈值"
    #   WTI分位: 70th=$63, 85th=$82, 95th=$100
    #   $80 = 情绪分水岭, $100 = 危机/流动性冲击
    #   20日涨速≥20% = 急涨(历史96分位) → 流动性冲击
    #   <20% = 缓涨 → 通胀焦虑, 新兴市场先承压
    # ═══════════════════════════════════════════
    wti_speed_flag = 'gradual' if wti_20d_chg < 20 else 'rapid'

    if wti > 100:
        # 无论涨速, >$100 = 危机模式 (华创: 无差别抛售风险资产, VIX+USD飙升)
        wti_status = '🔴 危机($100+)'
        risk_factors.append(f'WTI=${wti:.1f}>{100}(危机线), 20日涨{wti_20d_chg:+.1f}% → 全面Risk-off')
        veto_list.append('ALL_EQUITY')
    elif wti > 80 and wti_speed_flag == 'rapid':
        # $80-100 + 急涨(≥20%/20d) = 流动性冲击 (华创: 股债双杀, USD急升)
        wti_status = '🔴 急涨冲击'
        risk_factors.append(f'WTI=${wti:.1f}(>$80)+急涨{wti_20d_chg:+.1f}%/20d → 流动性冲击, 股债双杀')
        veto_list.extend(['航空', '化工', '物流', '消费零售', '新能源车'])
    elif wti > 80:
        # $80-100 + 缓涨 = 通胀焦虑 (华创: 新兴市场先跌, DM仍正但减速)
        wti_status = '🟡 通胀焦虑'
        risk_factors.append(f'WTI=${wti:.1f}(>$80)+缓涨{wti_20d_chg:+.1f}%/20d → 通胀焦虑, 成本敏感板块承压')
        veto_list.extend(['航空', '化工'])
    elif wti > 70:
        wti_status = '🟠 偏高'
        risk_factors.append(f'WTI=${wti:.1f}偏高(>$70)→关注成本传导')
    else:
        wti_status = '🟢 正常'

    # CNH判定 (阈值维持——7.30是2015汇改后关键心理位)
    # 数据过期检查: 汇率>3天未更新则失真, 不当有效值标"稳定"(周末容差3天)
    _cnh_stale = None
    if cnh_date:
        try:
            from datetime import date as _d, datetime as _dt
            _cnh_stale = (_d.today() - _dt.strptime(cnh_date, '%Y-%m-%d').date()).days
        except Exception:
            _cnh_stale = None
    if cnh and _cnh_stale is not None and _cnh_stale > 3:
        cnh_status = f'⚠ 汇率过期{_cnh_stale}天({cnh:.4f}@{cnh_date},不可用)'
        risk_factors.append(f'CNH数据过期{_cnh_stale}天(最新{cnh_date})→汇率维度失真,需补数,不计入稳定')
    elif cnh:
        if cnh > 7.35:
            cnh_status = f'🔴 急贬({cnh:.4f})'
            risk_factors.append(f'CNH={cnh:.4f}急贬破7.35→资本外流加速')
        elif cnh > 7.20:
            cnh_status = f'🟡 偏弱({cnh:.4f})'
        else:
            cnh_status = f'🟢 稳定({cnh:.4f})'
    else:
        cnh_status = '⚠ 数据缺失'
        risk_factors.append('CNH数据缺失→需实时补充')

    # SHIBOR 3M 斜率 (残A预留——流动性突变检测)
    shibor_slope = _calc_shibor_slope(macro)
    if shibor_slope > 5:
        risk_factors.append(f'SHIBOR 3M 5日斜率={shibor_slope:.1f}bp/日→流动性急剧收紧')

    # Gold判定（避险情绪反向指标）
    gold_chg = 0
    if prev and prev[6] and gold:
        gold_chg = (gold - prev[6]) / prev[6] * 100
    gold_note = f'${gold:.0f}'
    if abs(gold_chg) > 2:
        gold_note += f'({gold_chg:+.1f}%)'

    # 北向判定
    north_note = f'{north:+.0f}亿'
    if north > 50:
        north_note += ' → 外资积极做多'
    elif north > 0:
        north_note += ' → 外资温和流入'
    elif north == 0:
        north_note += ' → 外资观望'
    elif north > -50:
        north_note += ' → 外资小幅流出'
    else:
        north_note += ' → 外资撤退'
        risk_factors.append(f'北向大幅流出{abs(north):.0f}亿')

    # ═══════════════════════════════════════════
    # 美股大前提 v1.0 — SPX/纳斯达克 趋势+波动
    # 数据已在try块内从global_index_daily查询, 此处仅做状态判定
    # ═══════════════════════════════════════════

    # 美股状态判定 (v9: 滞后>3天→退化到A股情绪代理)
    spx_status = '⚪ 无数据'
    if spx_status_override == 'stale':
        # 退化代理: 用A股市场情绪 + CNH汇率变动推断外盘风险偏好
        # 逻辑: CNH急贬+情绪冰点→外盘大概率Risk-off; CNH稳定+情绪正常→外盘平稳
        spx_status = f'⚠️ 美股断更{spx_data_lag}天(代理推断)'
        spx_note = f'SPX断更(末次{spx_close:.0f}@{spx_latest_date}), 代理=CNH+情绪'
        if cnh and cnh > 7.30:
            spx_status = f'⚠️ 代理偏空(CNH{cnh:.4f}贬值+断更{spx_data_lag}天)'
            stress_triggers += 1
        # 情绪代理: 不触发risk_factors（不确定不瞎报）
    elif spx_close:
        spx_note = f'SPX={spx_close:.0f}'
        if spx_chg_20d < -5:
            spx_status = f'🔴 趋势恶化(20日{spx_chg_20d:+.1f}%)'
            risk_factors.append(f'美股SPX 20日跌{abs(spx_chg_20d):.1f}%→全球Risk-off传导A股')
            stress_triggers += 1
        elif spx_chg_5d < -2:
            spx_status = f'🟡 短期承压(5日{spx_chg_5d:+.1f}%)'
            risk_factors.append(f'美股SPX 5日跌{abs(spx_chg_5d):.1f}%→短期外资流出压力')
        elif spx_chg_5d > 2:
            spx_status = f'🟢 强势(5日{spx_chg_5d:+.1f}%)'
        else:
            spx_status = f'🟢 平稳(5日{spx_chg_5d:+.1f}%)'
    nasdaq_note = f'纳斯达克={nasdaq_close:.0f}(5日{nasdaq_chg_5d:+.1f}%)' if nasdaq_close else ''

    # ═══════════════════════════════════════════
    # 综合Regime判定 v2.0
    # 规则: 双因子确认——单一维度不触发全局锁死, ≥2个维度同时触发才锁
    # ═══════════════════════════════════════════
    # 统计各维度触发情况
    stress_triggers = 0
    if wti > 100:
        stress_triggers += 2  # 危机级, 权重大
    elif wti > 80:
        stress_triggers += 1
    if us10y > 4.70:
        stress_triggers += 2  # 清仓级
    elif us10y > 4.50:
        stress_triggers += 1
    if shibor_slope > 5:
        stress_triggers += 1
    if cnh and cnh > 7.35:
        stress_triggers += 1
    if north and north < -100:
        stress_triggers += 1

    # Regime判定
    if stress_triggers >= 4:
        regime = 'DEFENSE_PANIC'
        regime_desc = '多因子共振——≥4个压力信号同时触发，全面收缩至现金+黄金'
    elif stress_triggers >= 3 and (wti > 100 or us10y > 4.70):
        # 回测: ≥3但无单个极端因子 → 准确率0%。必须有至少一个极端因子才触发危机
        regime = 'DEFENSE_CRISIS'
        regime_desc = '高压防御——≥3个压力信号且至少一个极端(WTI>$100或美10Y>4.70%)'
    elif stress_triggers >= 3:
        regime = 'DEFENSE_TIGHT'
        regime_desc = '多因子偏紧——但无极端值，降级为TIGHT而非CRISIS'
    elif wti > 80 and wti_speed_flag == 'rapid':
        regime = 'DEFENSE_SHOCK'
        regime_desc = '油价急涨冲击——20日涨超20%，流动性冲击模式，股债双杀风险'
    elif wti > 80 and us10y > 4.50:
        regime = 'DEFENSE_TIGHT'
        regime_desc = '油+利率双压——WTI偏高+美债偏贵，存量博弈，仅抱团龙头'
    elif wti > 80:
        regime = 'CAUTION_OIL'
        regime_desc = '油价通胀焦虑——WTI>80但缓涨，成本敏感板块承压，新兴市场先跌'
    elif us10y > 4.70:
        regime = 'DEFENSE_RATE'
        regime_desc = '利率清仓体制——美债触发4.70%清仓线，全仓转现金+国债逆回购'
    elif us10y > 4.50:
        regime = 'CAUTION'
        regime_desc = '利率警戒——美债偏高，估值空间被压缩，控制仓位上限'
    else:
        regime = 'NORMAL'
        regime_desc = '正常体制——宏观无显著警报，可按技术/景气正常操作'

    return {
        'regime': regime,
        'regime_desc': regime_desc,
        'us10y': us10y, 'us10y_status': us10y_status,
        'wti': wti, 'wti_status': wti_status,
        'wti_20d_chg': round(wti_20d_chg, 1), 'wti_speed_flag': wti_speed_flag,
        'cnh': cnh, 'cnh_status': cnh_status,
        'china_10y': china_10y, 'shibor': shibor,
        'shibor_slope': round(shibor_slope, 1),
        'gold': gold, 'gold_chg': gold_chg, 'gold_note': gold_note,
        'north': north, 'north_note': north_note,
        'south': south,
        # 美股大前提
        'spx': spx_close, 'spx_status': spx_status, 'spx_chg_5d': round(spx_chg_5d, 1),
        'spx_chg_20d': round(spx_chg_20d, 1),
        'nasdaq': nasdaq_close, 'nasdaq_chg_5d': round(nasdaq_chg_5d, 1),
        'nasdaq_note': nasdaq_note,
        'veto_list': veto_list,
        'risk_factors': risk_factors,
        'stress_triggers': stress_triggers,
        'macro_date': macro_date, 'macro_lag': macro_lag,
    }


# ============================================================
# 第二传导层: 市场量能与筹码结构
# ============================================================
def layer2_market_structure(layer1):
    """
    战术指挥官，决定胜率。
    核心监控: 成交量、涨跌停比、上涨率(普涨/抱团率)
    铁律: 量能不足→任何宏观利多视为反弹非反转→强制扣减胜率
    """
    conn = duckdb.connect(DB)

    # 获取指数数据
    indices = {
        'sh000016': '上证50', 'sh000300': '沪深300', 'sh000688': '科创50',
        'sh000905': '中证500', 'sz399006': '创业板',
        'sh000819': '有色金属', 'sz399261': '锂电池', 'sz399997': '中证白酒',
        'sz399438': '电力指数', 'sh000849': '中证电池'
    }

    idx_data = {}
    for code, name in indices.items():
        row = conn.execute(f"""
            SELECT trade_date, close, amount, vol
            FROM kline_daily WHERE ts_code='{code}'
            ORDER BY trade_date DESC LIMIT 2
        """).fetchall()
        if row and len(row) >= 2:
            data_date = row[0][0]  # 最新K线日期
            cur, prev = row[0][1], row[1][1]
            cur_amt = row[0][2]
            chg = (cur / prev - 1) * 100 if prev and prev > 0 else 0
            amt_yi = cur_amt / 1e8 if cur_amt else 0
            # 铁律#3.1: 数据过期→涨跌清零, 防止过期涨幅污染P&L计算
            if isinstance(data_date, date):
                days_behind = (today - data_date).days
            elif isinstance(data_date, str):
                try:
                    days_behind = (today - datetime.strptime(data_date, '%Y-%m-%d').date()).days
                except:
                    days_behind = 999
            else:
                days_behind = 999
            if days_behind > 1:
                chg = 0.0  # 数据过期, 清零当日涨跌
                idx_data[name] = {'close': cur, 'chg': 0.0, 'amount_yi': round(amt_yi, 0),
                                  'stale': True, 'data_date': str(data_date)[:10], 'lag': days_behind}
            else:
                idx_data[name] = {'close': cur, 'chg': round(chg, 2), 'amount_yi': round(amt_yi, 0),
                                  'stale': False}

        # 5日数据
        row5 = conn.execute(f"""
            SELECT close FROM kline_daily WHERE ts_code='{code}'
            ORDER BY trade_date DESC LIMIT 5
        """).fetchall()
        if row5 and len(row5) >= 5:
            chg5 = (row5[0][0] / row5[-1][0] - 1) * 100
            if name in idx_data:
                idx_data[name]['chg_5d'] = round(chg5, 2)

    # 成交量分析 (指数K线无amount字段→使用vol*close估算或外部补充)
    vol_analysis = {}
    try:
        # 用全市场（取K线表有amount数据的个股汇总）作为代理
        total_vols = conn.execute("""
            SELECT trade_date, SUM(amount) as total_amt, COUNT(*) as n_stocks
            FROM kline_daily
            WHERE amount IS NOT NULL AND amount > 0
            AND trade_date >= (SELECT MAX(trade_date) FROM kline_daily WHERE amount IS NOT NULL) - 7
            GROUP BY trade_date ORDER BY trade_date DESC
        """).fetchall()
        if total_vols and total_vols[0][1]:
            today_amt = total_vols[0][1] / 1e8 if total_vols[0][1] else 0
            today_n = total_vols[0][2]
            hist = total_vols[1:] if len(total_vols) > 1 else total_vols
            avg_5d = sum(r[1] or 0 for r in hist) / len(hist) / 1e8 if hist else today_amt
            avg_n = sum(r[2] for r in hist) / len(hist) if hist else today_n
            # 数据完整性: 今日成交股票数远少于往期→成交额口径不可比(如只补了部分股票),不硬判放量/缩量
            data_incomplete = avg_n > 0 and today_n < avg_n * 0.5
            if data_incomplete:
                vol_trend = f'数据不足({today_n}只/均{avg_n:.0f}只)'
                vol_suf = False
            else:
                # 对照往期均值判(不再只比昨日): >1.1倍=放量, <0.9倍=缩量, 之间=平量
                if today_amt > avg_5d * 1.1: vol_trend = '放量'
                elif today_amt < avg_5d * 0.9: vol_trend = '缩量'
                else: vol_trend = '平量'
                vol_suf = today_amt > 20000
            vol_analysis = {
                'today_amt_yi': round(today_amt, 0),
                'avg_5d_amt_yi': round(avg_5d, 0),
                'vol_trend': vol_trend,
                'vol_sufficient': vol_suf,  # >2万亿≈充足
                'today_n_stocks': today_n,
                'data_incomplete': data_incomplete,
            }
        else:
            vol_analysis = {'today_amt_yi': 0, 'avg_5d_amt_yi': 0, 'vol_trend': '?', 'vol_sufficient': False}
    except:
        vol_analysis = {'today_amt_yi': 0, 'avg_5d_amt_yi': 0, 'vol_trend': '?', 'vol_sufficient': False}

    # 两融 (取total_balance=全市场两融余额)
    margin = conn.execute("""
        SELECT total_balance, margin_balance FROM margin_trading ORDER BY trade_date DESC LIMIT 2
    """).fetchall()
    margin_cur = margin[0][0] if margin and margin[0][0] else (margin[0][1] if margin and len(margin[0]) > 1 and margin[0][1] else 0)
    margin_prev = margin[1][0] if len(margin) > 1 and margin[1][0] else margin_cur
    margin_chg = (margin_cur - margin_prev) / margin_prev * 100 if margin_prev > 0 else 0
    # 如果total_balance为None, 使用margin_balance
    if margin_cur is None or margin_cur == 0:
        margin_cur = margin[0][1] if margin and len(margin[0]) > 1 and margin[0][1] else 0
        margin_prev = margin[1][1] if len(margin) > 1 and len(margin[1]) > 1 and margin[1][1] else margin_cur

    # 市场情绪
    sent = conn.execute("""
        SELECT limit_up_count, limit_down_count, market_emotion, emotion_score
        FROM market_sentiment ORDER BY trade_date DESC LIMIT 1
    """).fetchone()

    conn.close()

    lup = sent[0] if sent else 50
    ldown = sent[1] if sent else 5
    emotion_label = sent[2] if sent and sent[2] else '平静'
    emotion_score = sent[3] if sent and len(sent) > 3 and sent[3] else 50

    # ── O'Neil市场状态判定 (v9新增: 替代?占位符) ──
    # 基于沪深300的MA60/MA200相对位置 + 5日涨跌
    # 注: technical_indicators.close全为NULL, 需JOIN kline_daily取close
    oneil_state = '?'  # fallback
    hs300 = idx_data.get('沪深300', {})
    if hs300 and not hs300.get('stale', False):
        try:
            conn2 = duckdb.connect(DB)
            ti_row = conn2.execute("""
                SELECT k.close, t.ma60, t.ma200
                FROM technical_indicators t
                JOIN kline_daily k ON k.ts_code = t.ts_code AND k.trade_date = t.trade_date
                WHERE t.ts_code='sh000300' AND k.close > 0
                ORDER BY t.trade_date DESC LIMIT 1
            """).fetchone()
            conn2.close()
            if ti_row:
                close, ma60, ma200 = ti_row
                chg5 = hs300.get('chg_5d', 0) or 0
                if close and ma60 and close > ma60 and ma200 and close > ma200 and chg5 > 0.5:
                    oneil_state = 'confirmed_uptrend'
                elif close and ma60 and close > ma60:
                    oneil_state = 'rally_attempt'
                elif close and ma60 and close < ma60:
                    oneil_state = 'market_in_correction'
                else:
                    oneil_state = 'correction'
        except Exception:
            pass  # MA数据缺失不影响主流程

    # 上涨率（需要外部数据补充，先用指数层面估算）
    # 实际使用时通过WebSearch获取精确值
    up_ratio = None  # 由外部传入

    # --- 结构判定 ---
    is_concentrated = False  # 28% up ratio = 抱团
    if up_ratio is not None and up_ratio < 0.35:
        is_concentrated = True
    elif idx_data:
        # 如果创业板涨>2%但中证500微涨→抱团信号
        cyb = idx_data.get('创业板', {}).get('chg', 0)
        zz500 = idx_data.get('中证500', {}).get('chg', 0)
        if cyb > 1.5 and zz500 < 1.0:
            is_concentrated = True

    # 胜率扣减
    base_win_rate = 0.45  # 基准胜率45%
    deductions = []

    if not vol_analysis.get('vol_sufficient', False):
        deductions.append(('量能不足', 0.10))
    if is_concentrated:
        deductions.append(('极端抱团', 0.12))
    if margin_chg < -2:
        deductions.append(('两融骤降', 0.08))
    if layer1.get('wti', 100) > 90:
        deductions.append(('地缘溢价', 0.05))

    adj_win_rate = base_win_rate - sum(d[1] for d in deductions)
    adj_win_rate = max(0.10, min(0.60, adj_win_rate))

    return {
        'idx_data': idx_data,
        'vol_analysis': vol_analysis,
        'margin_balance': round(margin_cur / 1e8, 0) if margin_cur > 1e8 else margin_cur,
        'margin_chg': round(margin_chg, 1),
        'limit_up': lup, 'limit_down': ldown,
        'emotion_label': emotion_label, 'emotion_score': emotion_score,
        'oneil_state': oneil_state,  # v9: O'Neil状态, 替代硬编码?
        'is_concentrated': is_concentrated,
        'up_ratio': up_ratio,
        'base_win_rate': base_win_rate,
        'adj_win_rate': adj_win_rate,
        'deductions': deductions,
        'structure_verdict': '反弹非反转' if (not vol_analysis.get('vol_sufficient', False) or is_concentrated) else '正常行情',
    }


# ============================================================
# 第三执行层: NLP新闻与行业催化
# ============================================================
def _llm_judge_news_directions(news_titles):
    """LLM按语义批量判每条新闻方向(bullish/bearish/neutral)+名实简评。
    替代纯关键词匹配(关键词把'上涨25%后重挫'误判bullish)。
    LLM不可用/失败时返回None, 调用方fallback到关键词。一次调用判全部, 省时省钱。"""
    if not news_titles:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()  # 自动读ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL
        model = os.environ.get('ANTHROPIC_DEFAULT_HAIKU_MODEL') or os.environ.get('ANTHROPIC_MODEL')
        if not model:
            return None
        lines = [f"{i}. {(t or '')[:100]}" for i, t in enumerate(news_titles)]
        prompt = (
            "你是A股分析师。判断每条新闻按其内在逻辑对相关标的是 bullish(利好)/bearish(利空)/neutral(中性)。\n"
            "按逻辑判,不按市场反应。名实要点: 标题含'上涨'但主旨是'重挫/回落'→bearish; 政策'推动/支持/加大投放'→bullish; '预亏/抛售/减持/裁员'→bearish; 纯聚合快讯无明确方向→neutral。\n"
            f"新闻:\n{chr(10).join(lines)}\n\n"
            '只返回JSON,不要其他文字: {"results":[{"i":序号,"dir":"bullish/bearish/neutral","why":"8字内理由"}]}'
        )
        resp = client.messages.create(
            model=model, max_tokens=2000, temperature=0,  # temp=0: 同输入必须同结论(读懂层确定性要求)
            messages=[{"role": "user", "content": prompt}]
        )
        text = ''.join(getattr(b, 'text', '') for b in resp.content
                       if getattr(b, 'type', '') == 'text').strip()
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        data = json.loads(text)
        out = {}
        for r in data.get('results', []):
            try:
                d = r.get('dir', 'neutral')
                if d not in ('bullish', 'bearish', 'neutral'):
                    d = 'neutral'
                out[int(r['i'])] = (d, str(r.get('why', ''))[:20])
            except Exception:
                continue
        return out if out else None
    except Exception:
        return None


def layer3_sector_catalysts(layer1, layer2):
    """
    士兵，提供选股素材。
    铁律: 该模块仅为素材库。行业利好结论必须通过第一层和第二层安检后才能输出。
    """
    conn = duckdb.connect(DB)

    # 申万行业日频
    sectors = conn.execute("""
        SELECT industry_name, close FROM sw_index_daily
        ORDER BY trade_date DESC LIMIT 30
    """).fetchall()

    # 新闻
    news = conn.execute("""
        SELECT publish_date, source, title, sector_tags
        FROM news_articles
        WHERE publish_date >= CURRENT_DATE - 3
        ORDER BY publish_date DESC LIMIT 20
    """).fetchall()

    # 注意: conn不在此处关闭——下方"改2"的ε≈0复核还要查etf_daily。
    # 修复(2026-07-17): 此前在这里提前close导致ETF折溢价复核自上线起一直抛异常被吞, etf_disc恒=0。

    # --- 安检过滤 ---
    veto = layer1.get('veto_list', [])

    # LLM语义批量判方向(替代关键词; 关键词把"上涨25%后重挫"误判bullish)。失败fallback关键词。
    llm_dirs = _llm_judge_news_directions([(r[2] if len(r) > 2 else '') for r in news])

    sector_signals = []
    for idx, row in enumerate(news):
        tags = row[3] if len(row) > 3 and row[3] else ''
        title = row[2] if len(row) > 2 else ''
        # 检查是否触碰第一层否决
        blocked = False
        block_reason = ''
        for v in veto:
            if v in tags or v in title:
                blocked = True
                block_reason = f'第一层否决: {v}板块被封印'
                break
        if blocked:
            continue
        # 方向判断: 优先LLM语义, 失败fallback关键词
        why = ''
        if llm_dirs and idx in llm_dirs:
            direction, why = llm_dirs[idx]
            src = 'llm'
        else:
            direction = 'neutral'
            if any(w in title for w in ['涨', '买入', '增持', '利好', '净买入', '涨停']):
                direction = 'bullish'
            elif any(w in title for w in ['跌', '卖出', '减持', '利空', '净卖出', '跌停']):
                direction = 'bearish'
            src = 'keyword'
        sector_signals.append({
            'title': title[:60], 'tags': tags, 'direction': direction,
            'why': why, 'src': src,
            'blocked': blocked, 'block_reason': block_reason
        })

    # 改2: 利好消息自动ε≈0多通道复核
    bullish_signals = [s for s in sector_signals if s['direction'] == 'bullish']
    name_reality_conflicts = []
    if bullish_signals:
        # 拉三条ε≈0通道快速交叉验证(只拉当日第一只需要的数据, 不扫全市场)
        try:
            # ETF折溢价: 当日510300/588000 max |disc|
            etf_disc = conn.execute("""
                SELECT MAX(ABS(CAST(discount_rate AS DOUBLE))) FROM etf_daily
                WHERE trade_date=(SELECT MAX(trade_date) FROM etf_daily)
            """).fetchone()[0] or 0
        except: etf_disc = 0
        # 期权skew: 快速定性(数据拉取太重, 此处用占位——前向记录器已实时拉取)
        try:
            import requests as _rq
            _opt_hdr = {'Referer': 'https://stock.finance.sina.com.cn/', 'User-Agent': 'Mozilla/5.0'}
            _mo = _rq.get('https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName?exchange=null&cate=50ETF',
                          headers=_opt_hdr, timeout=5).json()
            _near = _mo['result']['data']['contractMonth'][1].replace('-', '')[2:]
            _pr = _rq.get(f'https://hq.sinajs.cn/list=OP_DOWN_510050{_near}', headers=_opt_hdr, timeout=5)
            _pr.encoding = 'gbk'
            _pcodes = [l.split('CON_OP_')[1].split('"')[0].split(',')[0] for l in _pr.text.split('\n') if 'CON_OP_' in l]
            _put_iv = None
            if _pcodes:
                _gr = _rq.get(f'https://hq.sinajs.cn/list=CON_SO_{_pcodes[0]}', headers=_opt_hdr, timeout=5)
                _gr.encoding = 'gbk'; _v = _gr.text.split('"')[1].split(','); _v = [_v[0]] + _v[4:]
                _put_iv = round(float(_v[6]) * 100, 1)
        except: _put_iv = None
        # 交叉验证
        conflict_reasons = []
        if etf_disc and float(etf_disc) > 2:
            conflict_reasons.append(f'ETF折溢价{float(etf_disc):.1f}%>2%: 成分股被掰, 利好可能是假象')
        if _put_iv and _put_iv > 20:
            conflict_reasons.append(f'Put IV={_put_iv}%: 期权恐慌>>现货利好, 名实背离')
        if conflict_reasons:
            name_reality_conflicts = conflict_reasons
            # 把所有bullish信号降级为neutral+标注
            for s in bullish_signals:
                s['direction'] = 'neutral'
                s['why'] = (s.get('why', '') or '') + ' [⚠ε复核:' + '; '.join(conflict_reasons) + ']'
    conn.close()

    return {
        'sector_signals': sector_signals,
        'veto_applied': len([s for s in sector_signals if s.get('blocked')]),
        'pass_count': len([s for s in sector_signals if not s.get('blocked')]),
        'name_reality_conflicts': name_reality_conflicts,
        # 读懂层降级可见化(2026-07-17): LLM不可用时静默退回关键词是审计查出的病, 降级必须喊出来
        'reader_mode': 'llm' if llm_dirs else 'keyword-fallback',
        'epsilon_audit': {
            'etf_disc': round(float(etf_disc), 2) if 'etf_disc' in dir() else None,
            'put_iv': _put_iv if '_put_iv' in dir() else None,
            'bullish_checked': len(bullish_signals),
            'conflicts': len(name_reality_conflicts) > 0
        }
    }


# ============================================================
# 进攻引擎集成 (v1.1) — Layer2增强 + Layer3增强
# ============================================================
def layer2b_fragility():
    """第二层增强: 脆弱地图 → 市场攻守能量判定"""
    try:
        sys.path.insert(0, os.path.join(BASE, 'engine'))
        from fragility_map import build_fragility_matrix, matrix_to_map, generate_daily_judgment
        matrix = build_fragility_matrix(today_str)
        fmap = matrix_to_map(matrix)
        judgment = generate_daily_judgment(today_str)

        # 攻守判定
        energy = fmap.get('overall_energy', 0)
        fragility = fmap.get('overall_fragility', 0)
        dominant = fmap.get('dominant_signal', 'neutral')

        if energy > fragility + 1.0:
            attack_stance = '🟡 高动能(多头结构成型)'  # 剥夺发令权: 不输出"可进攻"
            stance_detail = f'能量({energy:.1f}) > 脆弱({fragility:.1f})，技术面多头结构成立'
        elif fragility > energy + 1.0:
            attack_stance = '🔴 极度脆弱(空头主导)'  # 剥夺发令权: 不输出"防守"
            stance_detail = f'脆弱({fragility:.1f}) > 能量({energy:.1f})，空头结构主导'
        else:
            attack_stance = '🟡 中性(方向不明)'
            stance_detail = f'能量({energy:.1f})≈脆弱({fragility:.1f})，方向等待选择'

        return {
            'available': True,
            'energy': round(energy, 1),
            'fragility': round(fragility, 1),
            'dominant_signal': dominant,
            'attack_stance': attack_stance,
            'stance_detail': stance_detail,
            'judgment': judgment,
        }
    except Exception as e:
        return {'available': False, 'error': str(e)[:80]}


def layer3b_attack_signals(layer1):
    """第三层增强: 双模扫描 + CPO体检 + 量能天花板过滤"""
    result = {
        'available': False,
        'mode_a': None,   # 超跌反弹机会
        'mode_b': None,   # 空中加油信号
        'cpo': None,      # CPO三剑客体检
        'volume_cap': {}, # 板块量能天花板
    }
    veto = layer1.get('veto_list', [])

    # 1. 双模扫描
    try:
        sys.path.insert(0, os.path.join(BASE, 'engine'))
        from dual_mode_scanner import scan_full_market
        scan = scan_full_market(today_str)
        ma_count = scan['alerts']['mode_a_count']
        mb_count = scan['alerts']['mode_b_count']
        result['mode_a'] = {
            'count': ma_count,
            'top': scan['alerts']['top_a'][:3] if ma_count > 0 else [],
            'verdict': f'找到{ma_count}只超跌标的' if ma_count > 0 else '今日无超跌机会'
        }
        result['mode_b'] = {
            'count': mb_count,
            'top': scan['alerts']['top_b'][:3] if mb_count > 0 else [],
            'verdict': f'找到{mb_count}只空中加油信号' if mb_count > 0 else '今日无空中加油信号'
        }
        result['available'] = True
    except Exception as e:
        result['mode_a'] = {'count': 0, 'top': [], 'verdict': f'扫描引擎未就绪: {e}'}
        result['mode_b'] = {'count': 0, 'top': [], 'verdict': ''}

    # 2. CPO三剑客体检
    try:
        from ai_refuel_monitor_v2 import run_monitor
        mon = run_monitor(today_str)
        cpo_stocks = mon.get('all_results', mon.get('positions', []))
        cpo_signals = []
        for s in cpo_stocks[:3]:
            base = s.get('base_score', s.get('gate', 0))
            name = s.get('name', '?')
            if base >= 3:
                cpo_signals.append(f'{name}: 🟢 {base}/4灯→接近加油')
            elif base >= 2:
                cpo_signals.append(f'{name}: 🟡 {base}/4灯→还差一点')
            else:
                cpo_signals.append(f'{name}: 🔴 {base}/4灯→继续等')
        result['cpo'] = {
            'status': mon.get('summary', {}).get('market_status', 'waiting'),
            'avg_score': mon.get('summary', {}).get('avg_score', 0),
            'signals': cpo_signals,
        }
    except Exception as e:
        result['cpo'] = {'status': 'error', 'signals': [f'CPO监控未就绪: {e}']}

    # 3. 三大过滤 — 对关注板块执行量能天花板检查
    try:
        from three_filters import volume_cap_check
        watch_sectors = ['科创50', '有色金属', '新能源', 'AI']
        for sector in watch_sectors:
            # 第一层安检
            if any(v in sector for v in veto):
                continue
            try:
                vc = volume_cap_check(sector, today_str)
                result['volume_cap'][sector] = {
                    'verdict': vc.get('verdict', 'NORMAL'),
                    'detail': vc.get('reason', vc.get('detail', '')),
                }
            except:
                pass
    except Exception as e:
        pass

    return result


# ============================================================
# 冲突消解协议
# ============================================================
def resolve_conflicts(layer1, layer2, layer3):
    """
    多源数据冲突对齐:
      时间轴冲突 → 以最新现金市场确认的Regime为准
      指数与个股冲突 → 上涨率<35%时禁止定性为全面Risk-on
    """
    conflicts = []
    resolutions = []

    # 冲突检测1: 指数涨 vs 上涨率低
    cyb = layer2.get('idx_data', {}).get('创业板', {}).get('chg', 0)
    up_ratio = layer2.get('up_ratio')
    is_conc = layer2.get('is_concentrated', False)
    if cyb > 1.5 and is_conc:
        conflicts.append(
            f'创业板+{cyb:.2f}%但上涨率仅{up_ratio:.0%}' if up_ratio else
            f'创业板+{cyb:.2f}%但中证500未跟→指数虚假繁荣'
        )
        resolutions.append(
            '冲突已对齐→定为「存量抱团虚假高潮」，禁止定性为全面Risk-on。'
            '赚钱概率扣减至个股层面，非指数层面。'
        )

    # 冲突检测2: 宏观利多 vs 量能不足
    wti = layer1.get('wti', 100)
    north = layer1.get('north', 0)
    vol_suf = layer2.get('vol_analysis', {}).get('vol_sufficient', False)
    if north > 10 and not vol_suf:
        conflicts.append(f'北向流入{north:.0f}亿(利多) vs 量能不足(利空)')
        resolutions.append(
            '冲突已对齐→北向流入视为试探性建仓，非趋势反转信号。'
            '量能不足时所有利多信号强制扣减50%权重。'
        )

    # 冲突检测3: WTI暴涨 vs 部分板块上涨
    if wti > 90:
        oil_beneficiaries = ['有色', '能源', '煤炭', '黄金']
        for sector_name in oil_beneficiaries:
            pass  # 这些板块上涨与油价一致，不冲突
        # 但航空/化工如有上涨信号→冲突
        for s in layer3.get('sector_signals', []):
            if s['direction'] == 'bullish' and any(v in s.get('tags', '') for v in ['航空', '化工']):
                conflicts.append(f'{s["tags"]}有利多信号 vs WTI=${wti:.1f}成本压制')
                resolutions.append(f'冲突已对齐→{s["tags"]}利多信号被第一层一票否决。WTI地缘溢价下成本逻辑不成立。')

    return {
        'conflicts': conflicts,
        'resolutions': resolutions,
        'has_conflicts': len(conflicts) > 0,
    }


# ============================================================
# 交叉验证矩阵 (第一刀: 动态维度 — 基于实际战法存活情况)
# ============================================================
def cross_validation_matrix(layer1, layer2, layer3, idx_name, today_pnl, idx_chg_today,
                            as_of_date=None, news_verdict=None):
    """
    8+1维交叉验证矩阵 v2.3 (2026-06-14 战法维度移至回测实验室)
    =========================================================
    v2.2: 接入新闻中枢间接修正 (Nudge机制)
          新闻不独立成第10维, 而是渗透修正宏观/资金流/景气度已有维度得分。
    v2.3: 战法维度挂起 — 战法聚合器+四重门+纸交引擎拆至回测实验室单独验证。

    as_of_date: 回测用——指定历史日期, 所有DuckDB查询只读<=该日期的数据。
                实盘时为None, 自动用最新数据。
    news_verdict: news_hub.daily_verdict() 的输出, None=不启用新闻修正
      宏观体制(25%)       — 独立指标
      大盘(20%)          — 独立指标(O'Neil+养家)
      景气(15%)          — 独立模块(四层引擎)
      趋势/战法(🔬挂起)   — 已移至回测实验室 (战法→四重门→纸交全链验证)
      资金流(15%)        — 独立模块(资金流指纹)
      盈亏(10%)          — 独立指标
      压力测试(10%)      — 独立模块(scenario_engine)
      反共识(10%)        — 独立模块(剪刀差, 仅参考)
      规则健康(5%)       — 元监控(规则失效预警)

    Args:
        layer1: 第一层宏观体制输出
        layer2: 第二层市场结构输出
        idx_name: 指数名称(如'有色金属')
        today_pnl: 当日估计P&L
        idx_chg_today: 当日涨跌幅

    Returns:
        {
            'dimensions': [{name, weight, score, direction, signal}],
            'bullish_count': int,
            'bearish_count': int,
            'neutral_count': int,
            'verdict': '加仓'|'持有'|'观望'|'减仓',
            'verdict_detail': str,
            'reasoning_chain': str,  # 第二刀: 三段式推理链
        }
    """
    dimensions = []

    # ── 维度1: 宏观体制 (25%, 最高权重但非一票否决) ──
    # v2.1: 废除一票否决。宏观和其他8个维度平级投票。
    # 即使宏观看空, 只要其余维度多数看多, 系统仍可放行。
    regime = layer1.get('regime', 'UNKNOWN')
    wti = layer1.get('wti', 100)
    us10y = layer1.get('us10y', 5)
    stress = layer1.get('stress_triggers', 0)

    # v2.3: 宏观可看空, 但不可单独锁死。≥4维看空才减仓。
    # 回测: PANIC/CRISIS bearish→准确率0%(错杀反弹), 降为neutral+极低分
    #       DEFENSE_SHOCK bearish→准确率51%(一半对), 保留但降分
    #       CAUTION bearish→准确率34%(太偏空), 降为neutral
    if 'PANIC' in regime:
        macro_direction = 'neutral'; macro_score = 20
    elif 'CRISIS' in regime:
        macro_direction = 'neutral'; macro_score = 30
    elif 'DEFENSE_SHOCK' in regime:
        macro_direction = 'bearish'; macro_score = 30
    elif 'DEFENSE' in regime:
        macro_direction = 'bearish'; macro_score = 35
    elif 'CAUTION' in regime:
        macro_direction = 'neutral'; macro_score = 50
    else:
        macro_direction = 'neutral'; macro_score = 70
    dimensions.append({
        'name': '宏观体制', 'weight': '25%', 'direction': macro_direction,
        'score': macro_score,
        'signal': f'Regime={regime}, 压力信号={stress}/5, 美10Y={us10y:.2f}%, WTI=${wti:.1f}',
    })

    # ── 维度2: 大盘状态 (O'Neil + 养家, 独立指标) ──
    oneil_state = layer2.get('oneil_state', '?')
    emotion = layer2.get('emotion_label', '平静')
    emotion_score = layer2.get('emotion_score', 50)
    vol_analysis = layer2.get('vol_analysis', {})
    vol_suf = vol_analysis.get('vol_sufficient', False)
    vol_trend = vol_analysis.get('vol_trend', '?')

    market_bullish = (oneil_state in ('confirmed_uptrend', 'rally_attempt'))
    market_bearish = (oneil_state in ('market_in_correction', 'correction'))
    market_direction = 'bullish' if market_bullish else ('bearish' if market_bearish else 'neutral')
    market_score = 75 if market_bullish else (35 if market_bearish else 55)
    dimensions.append({
        'name': '大盘状态', 'weight': '20%', 'direction': market_direction,
        'score': market_score,
        'signal': f"O'Neil={oneil_state}, 情绪={emotion}({emotion_score}), "
                  f"成交{vol_analysis.get('today_amt_yi',0):.0f}亿({vol_trend})",
    })

    # ── 维度3: 景气度 (四层引擎, 独立模块) ──
    prosperity_score = 50; prosperity_direction = 'neutral'
    prosperity_signal = '景气数据未接入'
    if as_of_date:
        prosperity_signal = f'回测模式: 景气仅支持实时, 强制NEUTRAL'
    else:
        try:
            from engine.anti_consensus_prosperity import assess_sector
            sector_map = {'有色金属': '有色', '沪深300': '沪深300', '电力指数': '电力',
                          '锂电池': '新能源车', '科创50': '科创50'}
            sector = sector_map.get(idx_name, idx_name)
            ac = assess_sector(sector, days=7)
            if ac:
                reality = ac.get('reality', {}).get('reality_score', 50)
                prosperity_score = reality
                prosperity_direction = 'bullish' if reality > 55 else ('bearish' if reality < 45 else 'neutral')
                prosperity_signal = f"景气={reality:.0f}({ac.get('reality',{}).get('label','?')})"
        except:
            pass
    dimensions.append({
        'name': '景气度', 'weight': '15%', 'direction': prosperity_direction,
        'score': prosperity_score,
        'signal': prosperity_signal,
    })

    # ── 维度4: 趋势(战法) 🔬已移至回测实验室 ──
    # 2026-06-14: 战法聚合器+四重门+纸交引擎整条链路拆至回测实验室单独验证。
    #   验证通过后再接回生产。当前维度挂起，不参与投票。
    #   回测实验室: engine/strategy_aggregator.py + paper_trade.py + backtest_v8_*.py
    trend_score = 50; trend_direction = 'neutral'
    trend_signal = '🔬已移至回测实验室 | 战法→四重门→纸交 全链路回测验证中'
    dimensions.append({
        'name': '趋势(战法)', 'weight': '🔬15%(挂起)', 'direction': trend_direction,
        'score': trend_score,
        'signal': trend_signal,
    })

    # ── 维度5: 资金流 (资金流指纹, 独立模块) ──
    flow_score = 50; flow_direction = 'neutral'; flow_signal = '资金流指纹未接入'
    if as_of_date:
        flow_signal = f'回测模式: 资金流仅支持实时, 强制NEUTRAL (防数据泄漏)'
    else:
        try:
            from engine.capital_flow_fingerprint import fingerprint_dim_score
            flow_code = idx_map_inv.get(idx_name, 'sh000300')
            fs = fingerprint_dim_score(flow_code, name=idx_name)
            flow_score = fs
            flow_direction = 'bullish' if fs > 55 else ('bearish' if fs < 45 else 'neutral')
            flow_signal = f'资金流指纹={fs:.0f}分'
        except:
            pass
    dimensions.append({
        'name': '资金流', 'weight': '15%', 'direction': flow_direction,
        'score': flow_score,
        'signal': flow_signal,
    })

    # ── 维度6: 盈亏 (独立指标) ──
    pnl_direction = 'bearish' if today_pnl < -0.05 else ('bullish' if today_pnl > 0.02 else 'neutral')
    pnl_score = 30 if today_pnl < -0.05 else (70 if today_pnl > 0.02 else 50)
    dimensions.append({
        'name': '盈亏', 'weight': '10%', 'direction': pnl_direction,
        'score': pnl_score,
        'signal': f'浮盈{today_pnl:+.2%} | 当日涨跌{idx_chg_today:+.2%}',
    })

    # ── 维度7: 压力测试 (scenario_engine.stress_test, 独立模块) ──
    stress_direction = 'neutral'; stress_score = 50; stress_signal = '压力测试未接入'
    if as_of_date:
        stress_signal = f'回测模式: 压力测试仅支持实时, 强制NEUTRAL'
    else:
        try:
            from engine.scenario_engine import stress_test
            st = stress_test()
            scenarios = st.get('scenarios', [])
            over_limit = [s for s in scenarios if '超标' in str(s.get('是否超标', ''))]
            if len(over_limit) >= 4:
                stress_direction = 'bearish'; stress_score = 30
                stress_signal = f'{len(over_limit)}/5场景超标 → 需关注'
            elif len(over_limit) >= 3:
                stress_direction = 'neutral'; stress_score = 45
                stress_signal = f'{len(over_limit)}/5场景超标 → 边界, 中性'
            elif scenarios:
                max_loss = max(
                    float(str(s.get('组合损失', '0%')).rstrip('%'))
                    for s in scenarios if s.get('组合损失'))
                stress_score = max(40, 100 - max_loss * 100)
                stress_direction = 'bullish' if stress_score > 60 else 'neutral'
                stress_signal = f'最大压力损失{max_loss:.0%} → 风控范围内'
            else:
                stress_signal = '压力测试无数据'
        except Exception as e:
            stress_signal = f'压力测试不可用: {str(e)[:40]}'
    dimensions.append({
        'name': '压力测试', 'weight': '10%', 'direction': stress_direction,
        'score': stress_score,
        'signal': stress_signal,
    })

    # ── 维度8: 反共识剪刀差 (独立模块, 权重10%) ──
    # v9修复: 剪刀差映射到真实分数。负剪刀差(冷门)→高分, 正剪刀差(拥挤)→低分
    ac_score = 50; ac_direction = 'neutral'; ac_signal = '反共识未接入'
    try:
        if ac:
            div = ac.get('divergence', 0)
            if div is not None:
                # max/min 裁剪已处理极端值: div=-100→score=80, div=+200→score=20
                ac_score = max(20, min(80, 50 - div))
                ac_direction = 'bullish' if div < -5 else ('bearish' if div > 5 else 'neutral')
                ac_signal = f'剪刀差{div:+.0f} (信号激活: {ac_score}分)'
            else:
                ac_signal = '剪刀差为空→回退中性'
    except Exception as e:
        ac_signal = f'反共识计算失败: {str(e)[:40]}'
    dimensions.append({
        'name': '反共识', 'weight': '10%', 'direction': ac_direction,
        'score': ac_score,
        'signal': ac_signal,
    })

    # ── 维度9: 规则健康度 (元监控, 权重5%扣减) ──
    rule_health_score = 50; rule_direction = 'neutral'; rule_signal = '规则审计未接入'
    try:
        from engine.rule_failure_early_warning import assess_all_rules
        all_rules = assess_all_rules()
        frozen = sum(1 for r in all_rules if r.get('risk_level') == 'red')
        warned = sum(1 for r in all_rules if r.get('risk_level') == 'orange')
        active = sum(1 for r in all_rules if r.get('risk_level') == 'green')
        if frozen >= 5:
            rule_health_score = 25; rule_direction = 'bearish'
            rule_signal = f'冻结{frozen}条, 警告{warned}条, 健康{active}条 → 规则大面积失效'
        elif frozen >= 3:
            rule_health_score = 40; rule_direction = 'bearish'
            rule_signal = f'冻结{frozen}条, 上场{active}条 → 偏弱'
        else:
            rule_health_score = 65; rule_direction = 'neutral'
            rule_signal = f'上场{active}条, 冻结{frozen}条 → 正常'
    except:
        pass
    dimensions.append({
        'name': '规则健康', 'weight': '5%(扣减)', 'direction': rule_direction,
        'score': rule_health_score,
        'signal': rule_signal,
    })

    # ═══════════════════════════════════════════
    # v9 新闻中枢间接注入 (Nudge机制)
    # 不改权重、不加新维度, 只渗透修正已有维度得分
    # ═══════════════════════════════════════════
    if news_verdict and news_verdict.get('status') not in ('empty', None):
        news_delta = float(news_verdict.get('dimension_score', 50.0) - 50.0)

        if abs(news_delta) > 1.0:  # 偏差<1分不触发, 避免噪声
            # 创建维度索引字典 (方便按名修改)
            dim_map = {d['name']: d for d in dimensions}

            # 1. 渗透宏观体制 — 新闻宏观冲击直接映射
            ms = news_verdict.get('macro_shock', {})
            macro_nudge = news_delta * 0.6  # 杠杆0.6: -10分新闻→宏观扣6分
            if '宏观体制' in dim_map:
                old = dim_map['宏观体制']['score']
                dim_map['宏观体制']['score'] = max(5.0, min(95.0, old + macro_nudge))
                dim_map['宏观体制']['signal'] += f' [新闻修正{macro_nudge:+.1f}]'

            # 2. 渗透资金流 — flow_alert触发刚性扣分
            fa = news_verdict.get('flow_alert', {})
            if fa.get('active', False) and '资金流' in dim_map:
                flow_penalty = 10.0
                old_f = dim_map['资金流']['score']
                dim_map['资金流']['score'] = max(5.0, min(95.0, old_f - flow_penalty))
                dim_map['资金流']['signal'] += f' [新闻预警扣{flow_penalty:.0f}分]'

            # 3. 渗透景气度 — 板块能量偏空/偏多微调
            se = news_verdict.get('sector_energy', {})
            bearish = se.get('bearish_sectors', [])
            if bearish and '景气度' in dim_map:
                # 检查当前指数是否在利空板块清单中
                sector_hit = any(b in idx_name or idx_name in b for b in bearish)
                if sector_hit:
                    old_p = dim_map['景气度']['score']
                    dim_map['景气度']['score'] = max(5.0, min(95.0, old_p - 5.0))
                    dim_map['景气度']['signal'] += f' [板块利空-5]'

    # ═══════════════════════════════════════════
    # 裁决引擎 v8: 连续化概率流 (替代旧离散硬切换)
    # ═══════════════════════════════════════════

    # v9: 尝试从周末审计缓存加载当前regime优化权重 (fallback=硬编码默认)
    from engine.verdict_math import load_regime_weights
    load_regime_weights(regime)

    # 1. 底座1: 9维score → z-score → S_total 平滑合成
    S_total, z_scores_dict = _calc_S_total(dimensions)

    # 2. 底座2: 后验概率 + 信息熵 + 置信带宽
    z_vals = list(z_scores_dict.values())
    prob_assets = _calc_posterior_probabilities(z_vals, S_total)

    # 3. 底座3: 双模态迟滞环状态机
    # target_code: 用指数代码, 回退到idx_name (防御: idx_map_inv可能在趋势维度异常时未定义)
    try:
        idx_code_target = idx_map_inv.get(idx_name, str(idx_name))
    except NameError:
        idx_code_target = str(idx_name)
    h_state = _process_hysteresis(
        target_code=str(idx_code_target),
        P_bear=prob_assets['P_bear'],
        P_bull=prob_assets['P_bull'],
        backtest_date=str(as_of_date) if as_of_date else None,
    )

    # 4. 底座4: 体制感知仓位映射
    pos = _calc_position_delta(S_total, regime)

    # ═══════════════════════════════════════════
    # 5. 刚性宏观风控截断 (Regime Override)
    # ═══════════════════════════════════════════
    # 硬性宏观风控修正案: 模型只能感知历史数据, 无法预知未来大事件。
    # 当 regime 被判定为 DEFENSE / PANIC / CRISIS 时,
    # 系统必须实施刚性约束: 无论贝叶斯胜率多高,
    # delta_pos_pct 强制压缩至 0%, 终极裁决转为"静态观望"。
    _RIGID_OVERRIDE_REGIMES = {'DEFENSE', 'PANIC', 'CRISIS'}
    regime_override = any(r in regime for r in _RIGID_OVERRIDE_REGIMES)

    if regime_override:
        # 刚性截断: 仓位变动归零, 风险预算归零
        pos['delta_pos_pct'] = 0.0
        pos['max_risk_budget'] = 0.0
        pos['action'] = '静态观望'
        # 裁决覆写: 无视底层 S_total 和概率流
        verdict = '静态观望'
        verdict_detail = (
            f"[宏观干预] Regime={regime} → 刚性截断激活。"
            f"底层S_total={S_total:+.3f} (P_bull={prob_assets['P_bull']:.2f}) "
            f"已被体制锁死。最大风险预算强制归零。"
            f"等待人类指挥官确认大事件落地方可解除。"
        )
        commander_override = True
    else:
        commander_override = False

    # 6. 向后兼容: z-score → 方向计数 (供旧报表使用)
    bullish_count = sum(1 for z in z_vals if z > 0.4)
    bearish_count = sum(1 for z in z_vals if z < -0.4)
    neutral_count = len(z_vals) - bullish_count - bearish_count

    # 7. S_total → 旧版标签 (向后兼容) — 仅在无刚性截断时生效
    if not regime_override:
        if S_total > 0.25:
            verdict = '加仓'
        elif 0.10 < S_total <= 0.25:
            verdict = '持有偏多'
        elif -0.10 <= S_total <= 0.10:
            verdict = '观望偏空' if prob_assets['P_bear'] > 0.5 else '持有'
        elif -0.25 <= S_total < -0.10:
            verdict = '观望偏空'
        else:
            verdict = '减仓'

    if not regime_override:
        verdict_detail = (
            f"连续综合胜率(S_total): {S_total:+.3f} | "
            f"P_bull={prob_assets['P_bull']:.2f} P_bear={prob_assets['P_bear']:.2f} | "
            f"迟滞环: {h_state['state_node']} → {h_state['last_action']}"
        )

    # 8. 三段式推理链
    if regime_override:
        reasoning = (
            f"大前提: [宏观干预] Regime={regime}触发刚性风控截断(美10Y={us10y:.2f}%, WTI=${wti:.1f}) | "
            f"小前提: 底层S_total={S_total:+.3f}, σ_ensemble={prob_assets['sigma_ensemble']:.3f}, "
            f"熵={prob_assets['entropy']:.3f} | "
            f"结论: {verdict} — 仓位强制归零, 禁止任何加仓动作 "
            f"(等待人类指挥官解除体制熔断)"
        )
    else:
        reasoning = (
            f"大前提: 宏观Regime={regime}(权重25%), "
            f"美10Y={us10y:.2f}%, WTI=${wti:.1f} | "
            f"小前提: S_total={S_total:+.3f}, σ_ensemble={prob_assets['sigma_ensemble']:.3f}, "
            f"熵={prob_assets['entropy']:.3f}, 带宽={prob_assets['confidence_band']} | "
            f"结论: {verdict} — {h_state['last_action']} "
            f"(P_bear={prob_assets['P_bear']:.2f}, 死区:{h_state['dead_zone']})"
        )

    return {
        # === 旧字段 (向后兼容, 零破坏) ===
        'dimensions': dimensions,
        'bullish_count': bullish_count,
        'bearish_count': bearish_count,
        'neutral_count': neutral_count,
        'verdict': verdict,
        'verdict_detail': verdict_detail,
        'reasoning_chain': reasoning,

        # === 新增连续概率流资产 ===
        'continuous': {
            'z_scores': z_scores_dict,
            'S_total': round(S_total, 4),
            'sigma_ensemble': round(prob_assets['sigma_ensemble'], 4),
            'P_bull': round(prob_assets['P_bull'], 4),
            'P_neutral': round(prob_assets['P_neutral'], 4),
            'P_bear': round(prob_assets['P_bear'], 4),
            'entropy': round(prob_assets['entropy'], 4),
            'confidence_band': prob_assets['confidence_band'],
        },
        'position': {
            'delta_pos_pct': pos['delta_pos_pct'],
            'max_risk_budget': pos['max_risk_budget'],
            'action': pos.get('action', h_state['last_action']),
        },
        'hysteresis': {
            'active': h_state['state_node'] not in ('idle',),
            'state': h_state['state_node'],
            'enter_threshold': h_state['enter_threshold'],
            'exit_threshold': h_state['exit_threshold'],
            'dead_zone': h_state['dead_zone'],
            'consecutive_days': h_state['consecutive_days'],
        },
        'commander_note': (
            f"体制: {regime} | 置信带宽: {prob_assets['confidence_band']} | "
            f"仓位变动: {pos['delta_pos_pct']:+.1f}% | "
            f"迟滞环: {h_state['state_node']}"
        ),
        'commander_override': commander_override,
        'regime': regime,
    }


# ============================================================
# 持仓裁决 (v2.0: 集成交叉验证矩阵)
# ============================================================
def judge_holdings(layer1, layer2, layer3):
    """对 portfolio.json 中每个持仓进行三层安检"""
    pf_file = os.path.join(ROOT, 'portfolio.json')
    if not os.path.exists(pf_file):
        return []

    with open(pf_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 指数→标的映射
    idx_map = {
        '016708': ('sh000819', '有色金属'),
        '007404': ('sh000300', '沪深300'),
        '021753': ('sz399438', '电力指数'),
        '018927': ('sh000849', '中证电池'),
        '011613': ('sh000688', '科创50'),
    }

    veto = layer1.get('veto_list', [])
    idx_data = layer2.get('idx_data', {})
    results = []

    # ── v9 新闻中枢: 每个日报周期调一次, 结果复用给所有持仓 ──
    news_verdict = None
    try:
        from engine.news_hub import daily_verdict as news_daily_verdict
        news_verdict = news_daily_verdict()
    except Exception:
        pass  # 新闻模块不可用→不修正, 不影响主流程

    for h in data.get('holdings', []):
        code = h.get('code', '')
        name = h.get('name', '')
        amount = h.get('amount', 0)
        cost = h.get('cost_basis', amount)
        # 防脏数据(2026-07-17): portfolio.json手填占位符(如amount="待确认")曾直接TypeError炸掉全局裁决。
        # 一条脏持仓只跳过自己并警告, 不准拖死整个系统。
        try:
            amount = float(amount)
            cost = float(cost)
            pnl_old = float(h.get('pnl_pct', 0)) / 100.0  # 这是T+1净值
        except (TypeError, ValueError):
            print(f'[WARN] 持仓"{h.get("name", "?")}"字段非数值(amount={h.get("amount")!r}), 本期跳过该持仓, 请修portfolio.json')
            continue
        sector = h.get('sector', '')
        role = h.get('role', '')

        idx_code, idx_name = idx_map.get(code, ('sh000300', '未知'))

        # 用底层指数实时价修正P&L (铁律#3.2)
        idx_perf = idx_data.get(idx_name, {})
        idx_chg_today = idx_perf.get('chg', 0) / 100.0

        # P&L: portfolio.json已更新为今日真实净值时直接使用, 否则用指数推算
        pf_updated = data.get('updated', '2000-01-01')
        if pf_updated == today_str:
            # portfolio已是今日真实净值, 不做二次推算
            today_pnl = pnl_old  # pnl_pct 已是今日累计P&L
            # 但 idx_chg_today 仍用于9维矩阵, 这里取指数涨跌作为参考
        else:
            # portfolio数据滞后, 用指数涨跌推算
            real_cost = amount / (1 + pnl_old) if pnl_old > -1 else cost
            today_value = amount * (1 + idx_chg_today)
            today_pnl = (today_value / real_cost - 1) if real_cost > 0 else 0

        # --- 第一刀: 交叉验证矩阵 (替代旧三层安检) ---
        matrix = cross_validation_matrix(
            layer1, layer2, layer3, idx_name, today_pnl, idx_chg_today,
            news_verdict=news_verdict
        )

        # 最终裁决: 完全由9维矩阵投票决定 (v2.1: 废除一票否决)
        final_verdict = matrix['verdict']
        final_reason = matrix['verdict_detail']
        l1_blocked = False  # 不再有单独的L1否决

        # 纠错线 (第六刀: 每个持仓必须有, 禁止留空)
        # v8: 优先级次序 —
        #   1) 宏观体制干预 (最高优先级, 刚性截断)
        #   2) 迟滞环防抖倒计时
        #   3) 标的特定规则
        hysteresis = matrix.get('hysteresis', {})
        cons_days = hysteresis.get('consecutive_days', 0)
        exit_th = hysteresis.get('exit_threshold', 0.55)
        h_active = hysteresis.get('active', False)
        cmd_override = matrix.get('commander_override', False)

        # ── 刚性宏观体制干预: 最高优先级 ──
        ov_regime = layer1.get('regime', 'NORMAL')
        if cmd_override:
            s_total = matrix["continuous"]["S_total"]
            p_bull = matrix["continuous"]["P_bull"]
            correction = (
                f'[宏观体制干预激活: 下周大事件防御锁死] '
                f'Regime={ov_regime}, 仓位变动已刚性截断至0%。'
                f'底层S_total={s_total:+.3f} '
                f'(P_bull={p_bull:.2%}) 暂被锁死。'
                f'待人类指挥官确认大事件落地方可解除。'
            )
        elif cons_days > 0:
            # 迟滞环倒计时中 → 白盒化输出时序预期
            correction = (
                f'[迟滞防抖] 已连续{cons_days}日跌破退出阈值({exit_th*100:.0f}%), '
                f'若明日收盘继续低于{exit_th*100:.0f}%, '
                f'触发认错回补/状态退出。'
            )
        elif h_active:
            # 迟滞环激活中, 但退出计数为0 → 概率在死区或已确认
            correction = (
                f'[迟滞激活] 概率已冲破进入线(63%), 当前状态: {hysteresis.get("state","?")}。'
                f'需后验概率跌破{exit_th*100:.0f}%且连续3日收盘确认, 方可退出。'
            )
        elif '沪深300' in name:
            correction = f'沪深300跌破MA20({idx_perf.get("close",0)*0.97:.0f})→减半仓; 反之3日涨回MA20→恢复仓位'
        elif '电力' in name:
            correction = f'电力ETF日跌>2%或跌破MA20→减半仓; 放量站上MA10→买回'
        elif '电池' in name:
            correction = f'电池ETF连跌3日→清仓; 放量站上MA10→可买回试探'
        elif '有色' in name:
            correction = f'有色放量站上MA20→买回; 跌破今日低点×0.97→清仓'
        elif '科创' in name:
            correction = f'科创50跌破前低→清仓; RSI<30且缩量企稳→可试探'
        elif idx_chg_today < -0.03:
            correction = f'{idx_name}连跌3日→清仓'
        elif today_pnl < -0.05:
            correction = f'浮亏超-5%→无条件减仓'
        else:
            correction = f'若放量跌破MA20→减半仓; 5日跌幅>5%→清仓止损'

        results.append({
            'name': name, 'code': code, 'amount': amount,
            'pnl_old': pnl_old, 'pnl_today': today_pnl,
            'idx_chg_today': idx_chg_today,
            'sector': sector, 'role': role, 'idx_name': idx_name,
            'l1_blocked': l1_blocked,
            'final_blocked': l1_blocked,
            'veto_by': 'L1' if l1_blocked else '',
            'matrix': matrix,       # 完整的交叉验证矩阵
            'correction': correction,
            'correction_line': correction,  # v8: 战术动态纠错指引线 (别名字段)
            'verdict': final_verdict,
            'verdict_detail': final_reason,
            'reasoning_chain': matrix['reasoning_chain'],  # 第二刀
        })

    return results


# ============================================================
# 统一生成报告 (主入口)
# ============================================================
def generate_unified_report(external_up_ratio=None, external_news=None):
    """
    生成天眼2.0统一裁决报告
    external_up_ratio: 外部传入的上涨率 (float 0-1)
    external_news: 外部传入的新闻列表
    """
    # Step 0: 数据新鲜度检查
    fresh, freshness = check_data_freshness()
    if not fresh:
        stale_items = [k for k, v in freshness.items() if not v['fresh']]
        stale_desc = ', '.join(f'{k}(滞后{v["lag"]}天)' for k, v in freshness.items() if not v['fresh'])
    else:
        stale_desc = ''
        stale_items = []

    # Step 1: 第一统治层
    layer1 = layer1_macro_regime()

    # 接线1: 每次运行自动记录宏观信号到审计日志
    try:
        from engine.veto_auditor import log_macro_signal
        log_macro_signal(
            layer1['regime'], layer1.get('stress_triggers', 0),
            layer1['us10y'], layer1['wti'], layer1.get('wti_20d_chg', 0),
            layer1.get('shibor_slope', 0)
        )
    except:
        pass

    # 盲区1传导: 贝叶斯认知熔断 → 仓位锁
    bayes_meltdown = False
    try:
        bsf = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bayesian_state.json')
        if os.path.exists(bsf):
            with open(bsf, 'r', encoding='utf-8') as f:
                bs = json.load(f)
            if bs.get('posterior_neutral', 0) >= 0.50 and bs.get('error_streak', 0) >= 3:
                bayes_meltdown = True
    except:
        pass

    # Step 2: 第二传导层
    layer2 = layer2_market_structure(layer1)
    if external_up_ratio is not None:
        layer2['up_ratio'] = external_up_ratio
        layer2['is_concentrated'] = external_up_ratio < 0.35

    # Step 3: 第三执行层
    layer3 = layer3_sector_catalysts(layer1, layer2)

    # Step 3b: 进攻引擎 (v1.1)
    fragility = layer2b_fragility()
    attack_signals = layer3b_attack_signals(layer1)

    # Step 3c: 盘面归因 — LLM解释引擎 (context_reader v1.0, 2026-07-17)
    # LLM=解释引擎非预测引擎: 解释为什么波动/洗盘还是砸盘。不可用/未过验收→不污染裁决层。
    context_reading = None
    try:
        from engine.context_reader import read_market_context
        _idx = layer2.get('idx_data', {})
        _hs300 = _idx.get('沪深300') or _idx.get('上证指数') or {}
        _va = layer2.get('vol_analysis', {})
        _payload = {
            '标的': '大盘(沪深300)', 'analysis_date': today_str,
            '量价': {'今日涨跌%': _hs300.get('chg', 0),
                    '成交额亿': _va.get('today_amt_yi', 0),
                    '成交额5日均亿': _va.get('avg_5d_amt_yi', 0),
                    '量能判定': _va.get('vol_trend', '?'),
                    '上涨家数占比%': round((layer2.get('up_ratio') or 0) * 100, 1),
                    'kline_date': str(freshness.get('kline', {}).get('date', today_str))},
            '资金流': {'北向净亿': layer1.get('north', 0),
                      'moneyflow_date': str(freshness.get('macro', {}).get('date', today_str))},
            '消息': [{'标题': s.get('title', ''), '日期': today_str}
                    for s in layer3.get('sector_signals', [])[:8]],
        }
        context_reading = read_market_context(_payload, analysis_date=today_str)
    except Exception:
        context_reading = None

    # Step 4: 冲突消解
    conflicts = resolve_conflicts(layer1, layer2, layer3)

    # 中枢裁决: 底层状态 → 行动信号 (发令权唯一归V8枢纽)
    # 底层 fragility 只描述状态(高动能/极度脆弱)，不输出行动指令
    if fragility.get('available'):
        regime = layer1.get('regime', 'NORMAL')
        energy = fragility.get('energy', 0)
        frag = fragility.get('fragility', 0)
        stance = fragility.get('attack_stance', '')

        if 'DEFENSE' in regime or 'CAUTION' in regime:
            if energy > frag:
                # 宏观压制: 高动能 → 结构性反弹，不是进攻信号
                fragility['attack_stance'] = f'🟡 结构性反弹(被{regime}压制)'
                fragility['stance_detail'] = (
                    f'底层状态: 高动能(能量{energy:.1f}>脆弱{frag:.1f})。'
                    f'但宏观体制{regime}下，此信号为短期反弹动能，非趋势反转。'
                    f'严禁盲目进攻，仅可谨慎防守反击。'
                )
            elif frag > energy:
                fragility['attack_stance'] = f'🔴 全面防御({regime}确认)'
                fragility['stance_detail'] = (
                    f'底层状态: 极度脆弱 + 宏观{regime}双重压制。仓位收缩至最低。'
                )
            else:
                fragility['attack_stance'] = f'🟡 中性(被{regime}压制)'
        elif energy > frag and 'NORMAL' in regime:
            # 只有宏观NORMAL时，高动能才真正具备进攻意义
            fragility['attack_stance'] = f'🟢 具备进攻条件(NORMAL确认)'
            fragility['stance_detail'] = (
                f'宏观体制NORMAL + 底层高动能(能量{energy:.1f}>脆弱{frag:.1f})。'
                f'这是真正的可进攻信号。'
            )

    # Step 5: 持仓裁决
    holdings_verdicts = judge_holdings(layer1, layer2, layer3)

    # Step 6: 计算总账
    total_value = sum(h['amount'] for h in holdings_verdicts)
    total_pnl = sum(
        h['amount'] * h['pnl_today']
        for h in holdings_verdicts
    ) if holdings_verdicts else 0

    return {
        'generated_at': now_str,
        'data_freshness': freshness,
        'stale_items': stale_items,
        'stale_desc': stale_desc,
        'layer1': layer1,
        'layer2': layer2,
        'layer3': layer3,
        'context_reading': context_reading,
        'fragility': fragility,
        'attack_signals': attack_signals,
        'conflicts': conflicts,
        'holdings': holdings_verdicts,
        'total_value': total_value,
        'total_pnl': total_pnl,
        'bayes_meltdown': bayes_meltdown,
    }


# ============================================================
# 输出格式化
# ============================================================
def format_report(report_data):
    """将裁决数据格式化为Markdown报告"""
    l1 = report_data['layer1']
    l2 = report_data['layer2']
    l3 = report_data['layer3']
    cf = report_data['conflicts']
    hlds = report_data['holdings']
    stale = report_data.get('stale_desc', '')

    lines = []
    lines.append('')
    lines.append('---')
    lines.append('')
    lines.append('# 🏛️ 天眼2.0 · 一体化宏观量化决策引擎')
    lines.append('')
    lines.append(f'> 判决日期: {today_str} | 生成时间: {report_data["generated_at"]}')
    lines.append(f'> 数据底座: K线{report_data["data_freshness"].get("kline",{}).get("date","?")} | 宏观{report_data["data_freshness"].get("macro",{}).get("date","?")}')
    if stale:
        lines.append(f'> ⚠ 数据过期警告: {stale} → 已降级查询补充')
    if l3.get('reader_mode') == 'keyword-fallback':
        lines.append('> ⚠ 读懂层降级: LLM语义判向不可用, 本期新闻方向为关键词粗判(会把"上涨25%后重挫"误判利好), 第三层催化置信度打五折')
    lines.append('')

    # ====== 一、绝对主导体制 ======
    lines.append('## 🏛️ 绝对主导体制（Master Regime）')
    lines.append('')
    regime = l1['regime_desc']
    # 构造一句话总结
    oil_desc = f'WTI${l1["wti"]:.1f}({l1["wti_status"]})' if l1['wti'] else 'WTI数据缺失'
    rate_desc = f'美10Y={l1["us10y"]:.2f}%({l1["us10y_status"]})' if l1['us10y'] else '美10Y数据缺失'
    north_desc = f'北向{l1["north"]:+.0f}亿'
    conc_desc = f'上涨率仅{l2["up_ratio"]:.0%}' if l2.get('up_ratio') else '极端抱团'
    vol_amt = l2["vol_analysis"].get("today_amt_yi", 0)
    vol_desc = f'成交{vol_amt:.0f}亿' if vol_amt > 0 else '成交需外部补充'

    lines.append(f'> **今天是由【{oil_desc}+{rate_desc}】和【{north_desc}+{conc_desc}/{vol_desc}{l2["vol_analysis"].get("vol_trend","")}】共同主导的「{regime}」。**')
    lines.append('')

    lines.append(f'**{l1["wti_status"]}** | **{l1["us10y_status"]}** | CNH: {l1["cnh_status"]} | 黄金: {l1["gold_note"]}')
    lines.append('')

    # 盲区1: 贝叶斯认知熔断 → 仓位锁 + 第六刀纠错线
    if report_data.get('bayes_meltdown'):
        # 读取阈值配置 (去硬编码)
        wti_normal = 80; us10y_normal = 4.5  # 默认
        try:
            meta_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'thresholds_meta.json')
            if os.path.exists(meta_file):
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                wti_normal = meta.get('WTI_NORMAL_CEILING', {}).get('current_value', 80)
                us10y_normal = meta.get('US10Y_NORMAL_CEILING', {}).get('current_value', 4.5)
        except:
            pass

        lines.append('> 🔴 **认知熔断生效**: 贝叶斯模块连续预测错误, 后验崩塌至50%中性.')
        lines.append('> W_max 强制 ≤ 20% | 禁止开新仓 | 仅保留已有防御仓位.')
        lines.append('')
        lines.append('**熔断解除条件 (纠错线)**:')
        lines.append('')
        lines.append('| 条件 | 阈值 | 状态检测 |')
        lines.append('|------|------|---------|')
        lines.append('| 贝叶斯连续正确 | ≥2日 error=0 | 每日盘后 bayesian_loop_update() 更新 |')
        lines.append('| 后验中性回落 | posterior_neutral < 40% | 贝叶斯状态文件 |')
        lines.append(f'| 宏观Regime降级 | NORMAL (WTI<${wti_normal} 且 美10Y<{us10y_normal}%) | layer1_macro_regime() 每日检测 |')
        lines.append('| 9维矩阵净看多 | (看多-看空) ≥ 3 | cross_validation_matrix() |')
        lines.append('')
        lines.append('> 四个条件**全部满足** → 熔断自动解除, W_max恢复.')
        lines.append('> 注: 条件④用净看多而非零容忍——允许1-2个滞后维度看空, 只要多头力量形成绝对压制.')
        lines.append('')

    # ====== 二、逻辑串联链条 ======
    lines.append('## 🔄 逻辑串联链条（Unified Narrative Loop）')
    lines.append('')

    # 宏观因子定位
    lines.append('### 宏观因子最高警戒状态')
    lines.append('')
    lines.append('| 因子 | 数值 | 5日变化 | 警戒状态 |')
    lines.append('|------|------|---------|---------|')
    wti_chg_str = f'{l1["wti_20d_chg"]:+.1f}%' if l1.get('wti_20d_chg') else '-'
    lines.append(f'| WTI原油 | **${l1["wti"]:.2f}** | {wti_chg_str} | {l1["wti_status"]} |')
    lines.append(f'| 美10Y | {l1["us10y"]:.2f}% | - | {l1["us10y_status"]} |')
    if l1['cnh']:
        lines.append(f'| CNH | {l1["cnh"]:.4f} | - | {l1["cnh_status"]} |')
    else:
        lines.append(f'| CNH | 数据缺失 | - | ⚠ |')
    lines.append(f'| 黄金 | ${l1["gold"]:.0f} | {l1["gold_chg"]:+.1f}% | 🟡 |')
    lines.append(f'| 北向 | {l1["north_note"]} | - | - |')
    lines.append(f'| SHIBOR O/N | {l1["shibor"]:.3f}% | - | 🟢 流动性充裕 |')
    lines.append('')

    if l1.get('risk_factors'):
        lines.append(f'**宏观一句话**: {"; ".join(l1["risk_factors"])}')
        lines.append('')

    # 产业成本推演
    lines.append('### 产业成本推演')
    lines.append('')
    # 基于Regime推导受益/承压板块
    veto = l1.get('veto_list', [])
    lines.append('| 方向 | 板块 | 第一层安检 | 第二层安检 | 第三层催化 |')
    lines.append('|------|------|-----------|-----------|-----------|')

    # 从指数数据推断板块方向
    idx_data = l2.get('idx_data', {})
    for name, perf in idx_data.items():
        chg = perf.get('chg', 0)
        if chg > 1.5:
            direction = '🟢 进攻'
        elif chg > 0:
            direction = '🟡 中性'
        else:
            direction = '🔴 承压'

        # 安检
        l1_pass = '✅' if not any(v in name for v in veto) else '🚫 一票否决'
        l2_pass = '⚠ 抱团股' if l2.get('is_concentrated') else '✅'
        l3_pass = '✅'  # 待实际催化填充

        lines.append(f'| {direction} | {name} | {l1_pass} | {l2_pass} | {l3_pass} |')

    lines.append('')
    if veto:
        lines.append(f'**第一层否决清单**: {", ".join(veto)} → 今日禁止买入')
        lines.append('')

    # 盘面归因 (LLM解释引擎 v1.0, 2026-07-17): 解释为什么波动, 洗盘还是砸盘, 只输出概率+纠错线
    cr = report_data.get('context_reading')
    lines.append('### 盘面归因（LLM解释引擎）')
    lines.append('')
    if cr and cr.get('_meta', {}).get('ok'):
        v = cr.get('verdict', {})
        try: _conf = f'{float(v.get("置信度", 0)):.0%}'
        except Exception: _conf = '?'
        lines.append(f'**最可能解释**: {v.get("最可能解释", "?")}（置信度{_conf}）')
        lines.append(f'**一句话归因**: {v.get("一句话归因", "")}')
        hyps = cr.get('hypotheses', [])
        if hyps:
            lines.append('')
            lines.append('| 候选解释 | 后验概率 | 关键支持证据 | 关键反驳证据 |')
            lines.append('|---|---|---|---|')
            for h in hyps[:4]:
                sup = (h.get('支持证据') or ['—'])[0]
                ref = (h.get('反驳证据') or ['—'])[0]
                try: _p = f'{float(h.get("后验概率", 0)):.0%}'
                except Exception: _p = '?'
                lines.append(f'| {h.get("解释", "?")} | {_p} | {str(sup)[:45]} | {str(ref)[:45]} |')
            lines.append('')
        lines.append(f'**纠错线**: {v.get("纠错线", "")}')
        gaps = v.get('数据缺口') or []
        if gaps:
            lines.append(f'**数据缺口**: {"; ".join(str(g) for g in gaps[:3])}')
    elif cr and cr.get('_meta', {}).get('stage') == 'freshness_gate':
        lines.append(f'⚠ 解释引擎拒绝分析: 数据过期 — {"; ".join(cr["_meta"].get("stale", [])[:3])}（先补数再归因, 过期数据不解读）')
    elif cr:
        lines.append(f'⚠ 解释引擎输出未过验收（{"; ".join(cr.get("_meta", {}).get("reasons", [])[:2])}）, 本期不提供归因——宁缺毋滥')
    else:
        lines.append('⚠ 解释引擎不可用（LLM未配置）, 本期无盘面归因')
    lines.append('')

    # 热门板块全景 (v7新增: 所有指数涨跌排名)
    lines.append('### 今日热门板块全景')
    lines.append('')
    try:
        conn = duckdb.connect(DB)
        idx_names = {
            'sh000001':'上证指数','sh000300':'沪深300','sh000016':'上证50',
            'sh000688':'科创50','sh000905':'中证500','sz399006':'创业板',
            'sh000819':'有色金属','sz399997':'新能源','sz399967':'军工',
            'sz399986':'银行','sz399001':'深证成指','sz399438':'电力指数',
            'sz399261':'锂电池',
        }
        sector_results = []
        for code, name in idx_names.items():
            rows = conn.execute('SELECT trade_date, close FROM kline_daily WHERE ts_code=\'' + code + '\' ORDER BY trade_date DESC LIMIT 2').fetchall()
            if len(rows) == 2 and str(rows[0][0])[:10] == today_str:
                chg = (rows[0][1]/rows[1][1]-1)*100 if rows[1][1] else 0
                sector_results.append((name, chg))
        conn.close()

        if sector_results:
            sector_results.sort(key=lambda x: x[1])
            lines.append('| 板块 | 涨跌 | 热度 |')
            lines.append('|------|:--:|:--:|')
            for name, chg in sector_results:
                if chg > 1: icon = '🟢'
                elif chg > 0: icon = '🟡'
                elif chg > -1: icon = '🟠'
                elif chg > -2: icon = '🔴'
                else: icon = '💥'
                lines.append(f'| {name} | {icon} {chg:+.2f}% | {icon} |')
            lines.append('')

        # 主力资金
        try:
            conn2 = duckdb.connect(DB)
            cf_row = conn2.execute('SELECT main_net, main_pct FROM capital_flow ORDER BY trade_date DESC LIMIT 1').fetchone()
            if cf_row and cf_row[0]:
                lines.append(f'> 今日主力资金: **{cf_row[0]:+.0f}亿** (净占比 {cf_row[1]:+.1f}%)')
            conn2.close()
        except:
            pass
        lines.append('')
    except:
        pass

    # 冲突消解
    if cf.get('has_conflicts'):
        lines.append('### ⚡ 冲突消解记录')
        lines.append('')
        for i, (c, r) in enumerate(zip(cf['conflicts'], cf['resolutions'])):
            lines.append(f'**冲突{i+1}**: {c}')
            lines.append(f'> 对齐: {r}')
            lines.append('')

    # ====== 二点五、进攻引擎信号 (v1.1) ======
    fragility = report_data.get('fragility', {})
    attack = report_data.get('attack_signals', {})

    lines.append('## ⚔️ 进攻引擎信号')
    lines.append('')

    # 脆弱地图
    if fragility.get('available'):
        lines.append('### 市场攻守体检（脆弱地图）')
        lines.append('')
        lines.append(f'**{fragility["attack_stance"]}**: {fragility["stance_detail"]}')
        lines.append(f'> 能量: {fragility["energy"]} | 脆弱: {fragility["fragility"]} | 主导信号: {fragility["dominant_signal"]}')
        lines.append('')
    else:
        lines.append('### 市场攻守体检（脆弱地图）')
        lines.append(f'> 暂不可用: {fragility.get("error", "模块未加载")}')
        lines.append('')

    # 双模扫描
    if attack.get('available'):
        lines.append('### 双模扫描')
        lines.append('')
        ma = attack.get('mode_a', {})
        mb = attack.get('mode_b', {})
        lines.append(f'**模式A·超跌反弹**: {ma.get("verdict", "无数据")}')
        if ma.get('top'):
            for r in ma['top'][:3]:
                lines.append(f'- {r.get("name","?")} ({r.get("sector","?")}): 跌幅{r.get("dd_pct","?")}%, RSI={r.get("rsi_14","?")}, 量比={r.get("vol_ratio","?")}')
        lines.append(f'**模式B·空中加油**: {mb.get("verdict", "无数据")}')
        if mb.get('top'):
            for r in mb['top'][:3]:
                lines.append(f'- {r.get("name","?")}: 趋势完好，加油信号确认')
        lines.append('')

    # CPO三剑客
    cpo = attack.get('cpo', {})
    if cpo and cpo.get('signals'):
        lines.append('### CPO三剑客体检')
        lines.append('')
        lines.append(f'状态: **{cpo.get("status","?")}** (均分{cpo.get("avg_score",0)})')
        for s in cpo.get('signals', []):
            lines.append(f'- {s}')
        lines.append('')

    # 量能天花板
    vc = attack.get('volume_cap', {})
    if vc:
        lines.append('### 板块量能天花板')
        lines.append('')
        lines.append('| 板块 | 裁决 | 详情 |')
        lines.append('|------|------|------|')
        for sector, data in vc.items():
            v = data.get('verdict', '?')
            icon = '🚨' if v == 'DEFENSE' else ('⚠' if v == 'WARN' else '✅')
            lines.append(f'| {sector} | {icon} {v} | {data.get("detail","")[:40]} |')
        lines.append('')

    # ====== 三、最终裁决 ======
    lines.append('## ⚖️ 最终裁决与执行建议（The Verdict）')
    lines.append('')

    # 盘面定性
    lines.append('### 盘面定性')
    lines.append('')
    # 行情定性
    win_rate = l2.get('adj_win_rate', 0.35)
    structure = l2.get('structure_verdict', '未判定')
    if structure == '反弹非反转':
        verdict_line = f'**{structure}**。赚钱概率（个股层面）约{win_rate:.0%}。'
    else:
        verdict_line = f'**{structure}**。赚钱概率约{win_rate:.0%}。'

    lines.append(f'{verdict_line}')
    lines.append('')

    # 关键数据一览
    lines.append('| 指标 | 数值 | 裁决 |')
    lines.append('|------|------|------|')
    for name in ['上证50', '沪深300', '科创50', '创业板', '中证500']:
        d = idx_data.get(name, {})
        chg = d.get('chg', 0)
        icon = '🟢' if chg > 1 else ('🟡' if chg > 0 else '🔴')
        lines.append(f'| {name} | {d.get("close","-"):.0f} ({chg:+.2f}%) | {icon} |')
    vol_amt = l2["vol_analysis"].get("today_amt_yi", 0)
    if vol_amt > 0:
        vol_str = f'{vol_amt:.0f}亿({l2["vol_analysis"].get("vol_trend","?")})'
    else:
        vol_str = 'DB未存(需外部补充)'
    lines.append(f'| 成交量 | {vol_str} | {"🟡 偏低" if not l2["vol_analysis"].get("vol_sufficient",False) else "🟢 充足"} |')
    lines.append(f'| 两融 | {l2.get("margin_balance",0):.0f}亿({l2.get("margin_chg",0):+.1f}%) | {"🔴 骤降" if l2.get("margin_chg",0) < -2 else "🟡 正常"} |')
    lines.append('')

    # 持仓交叉验证矩阵 (第一刀: 9维裁决)
    if hlds:
        lines.append('### 持仓交叉验证裁决')
        lines.append('')
        lines.append('| 持仓 | 代码 | 市值 | 估计P&L | 胜率分(S_total) | 置信带宽 | **最终裁决** | 推理链 |')
        lines.append('|------|------|------|-----------|:--:|:--:|-------------|--------|')
        for h in hlds:
            pnl_str = f'{h["pnl_today"]:+.2%}' if abs(h['pnl_today']) < 1 else f'{h["pnl_today"]:+.1%}'
            cont = h.get('matrix', {}).get('continuous', {})
            s_total_str = f'{cont.get("S_total", 0):+.3f}'
            band_str = cont.get('confidence_band', '?')
            raw_v = h['verdict']
            if h.get('final_blocked') or '封印' in raw_v:
                verdict = '🔒 封印'
            elif raw_v.startswith('加仓'):
                verdict = '🟢 加仓'
            elif raw_v.startswith('减仓'):
                verdict = '🔴 减仓'
            elif '观望' in raw_v:
                verdict = '🟡 观望'
            elif '持有偏多' in raw_v:
                verdict = '🟢 持有偏多'
            else:
                verdict = '⚪ 持有'
            chain = h.get('reasoning_chain', '')[:60] + ('...' if len(h.get('reasoning_chain', '')) > 60 else '')
            # wcwidth 精确对齐: 标的名按显示宽度补齐
            name_col = _wc_ljust(h['name'][:10], 10)
            lines.append(f'| {name_col} | {h["code"]} | {h["amount"]:.0f} | {pnl_str} | {s_total_str} | {band_str} | **{verdict}** | {chain} |')
            # 每行下方追加战术动态纠错指引线
            corr_line = h.get('correction_line', h.get('correction', ''))
            if corr_line:
                lines.append(f'> ⚡ {corr_line}')

        lines.append('')
        lines.append(f'**总账**: {report_data["total_value"]:.0f}元')

        # 第一个持仓的维度明细 (其余持仓格式相同, 省略)
        if hlds and hlds[0].get('matrix'):
            m = hlds[0]['matrix']
            lines.append('')
            lines.append(f'**{hlds[0]["name"]} 维度明细** ({m["bullish_count"]}看多/{m["bearish_count"]}看空/{m["neutral_count"]}中性 → {m["verdict"]}):')
            lines.append('')
            lines.append('| 维度 | 权重 | 方向 | z-score | 信号 |')
            lines.append('|------|------|:--:|:--:|------|')
            z_map = m.get('continuous', {}).get('z_scores', {})
            for d in m['dimensions']:
                icon = '🟢' if d['direction'] == 'bullish' else ('🔴' if d['direction'] == 'bearish' else '⚪')
                z_val = z_map.get(d['name'], 0)
                lines.append(f'| {d["name"]} | {d["weight"]} | {icon} | {z_val:+.4f} | {d["signal"][:60]} |')
        lines.append('')

    # 胜率扣减明细
    if l2.get('deductions'):
        lines.append('### 胜率扣减明细')
        lines.append('')
        lines.append(f'基准胜率: {l2["base_win_rate"]:.0%}')
        for reason, penalty in l2['deductions']:
            lines.append(f'- {reason}: -{penalty:.0%}')
        lines.append(f'**调整后胜率: {l2["adj_win_rate"]:.0%}**')
        lines.append('')

    # ── v8: 迟滞环综合防抖监控面板 ──
    if hlds and hlds[0].get('matrix', {}).get('continuous'):
        cont = hlds[0]['matrix']['continuous']
        hyst = hlds[0]['matrix'].get('hysteresis', {})
        pos = hlds[0]['matrix'].get('position', {})
        cmdr = hlds[0]['matrix'].get('commander_note', '')
        lines.append('### 🎛️ 迟滞环综合防抖监控面板')
        lines.append('')
        lines.append('| 指标 | 数值 |')
        lines.append('|------|------|')
        lines.append(f'| 组合综合胜率 (S_total) | {cont["S_total"]:+.4f} |')
        lines.append(f'| 三元态后验分布 (看多/中性/看空) | {cont["P_bull"]:.2%} / {cont["P_neutral"]:.2%} / {cont["P_bear"]:.2%} |')
        lines.append(f'| 维度共振分歧度 (σ_ensemble) | {cont["sigma_ensemble"]:.3f} |')
        lines.append(f'| 信息熵 (H) | {cont["entropy"]:.3f} |')
        lines.append(f'| 置信带宽 | {cont["confidence_band"]} |')
        lines.append(f'| 连续仓位变动 (ΔPos) | {pos.get("delta_pos_pct", 0):+.1f}% (风险预算 {pos.get("max_risk_budget", 0):.0%}) |')
        lines.append('')
        lines.append('| 迟滞环防抖 | 状态 |')
        lines.append('|------|------|')
        lines.append(f'| 当前状态节点 | {hyst.get("state", "?")} |')
        lines.append(f'| 连续确认天数 | {hyst.get("consecutive_days", 0)} |')
        lines.append(f'| 强进入阈值 → 弱退出阈值 | {hyst.get("enter_threshold", 0.63):.0%} → {hyst.get("exit_threshold", 0.55):.0%} |')
        lines.append(f'| 系统死区宽度 | {hyst.get("dead_zone", "55%-63%")} |')
        lines.append(f'| 当前防抖裁决 | {"🟡 激活中 — 死区/确认态, 维持当前裁决不触发新交易" if hyst.get("active") else "🟢 空闲 — 等待后验概率冲破63%进入线"} |')
        lines.append('')
        if cmdr:
            lines.append(f'> 🧠 **指挥官日志**: {cmdr}')
        # ── 宏观体制熔断告警 ──
        if hlds[0].get('matrix', {}).get('commander_override'):
            lines.append(f'> 🚨 **警告: 检测到下周重大非对称事件预期, 人类指挥官已激活体制熔断, 最大风险预算已被刚性剥夺, 当前强制静态观望!**')
        if cmdr or hlds[0].get('matrix', {}).get('commander_override'):
            lines.append('')

    # 风控红线 + 纠错线 (第六刀: 每个系统输出附带可证伪命题)
    lines.append('### 纠错线与风控红线')
    lines.append('')

    # 宏观维度纠错线
    regime = l1.get('regime', 'NORMAL')
    if 'CAUTION' in regime or 'DEFENSE' in regime:
        lines.append(f'**宏观预警纠错** (Regime={regime}):')
        lines.append(f'- 若未来5个交易日沪深300涨>2% → 宏观看空信号标记为"错杀", 下调该阈值可信度')
        lines.append(f'- 若SHIBOR斜率回落<3bp/日 → 流动性警报解除')
    if l1.get('wti', 0) > 80:
        lines.append(f'- 若WTI跌破$80 → CAUTION_OIL降级为NORMAL, 航空/化工解除')
    if l1.get('us10y', 5) > 4.50:
        lines.append(f'- 若美10Y升至4.70% → 全面清仓转现金+国债逆回购')
    lines.append('')

    # 仓位盖帽纠错线
    w_max_val = l2.get('W_max', l2.get('adj_win_rate', 0.35))
    if isinstance(w_max_val, (int, float)) and w_max_val < 0.80:
        lines.append(f'**仓位纠错** (W_max≈{w_max_val:.0%}):')
        lines.append(f'- 若本周组合跑赢基准>3% → 仓位盖帽可能过严, 下周恢复至W_max=80%')
    lines.append('')

    # 各持仓纠错线
    if hlds:
        lines.append('**持仓纠错**:')
        for h in hlds:
            v = h.get('verdict', '')
            corr = h.get('correction', '')
            if corr:
                lines.append(f'- {h["name"]}: {corr}')
            elif '减仓' in v:
                lines.append(f'- {h["name"]}: 若放量站上MA20 → 买回一半, 纠错成本约2-3%')
            elif '加仓' in v:
                lines.append(f'- {h["name"]}: 买入后5日跌幅>5% → 止损清仓')
            elif '封印' in v:
                lines.append(f'- {h["name"]}: 封印解除条件 → 宏观Regime降为NORMAL')
        lines.append('')

    # 矩阵裁决本身的纠错线
    lines.append(f'- 若连续3日全部持仓"减仓"但市场未跌 → 矩阵看空偏向过重, 建议重检维度权重')
    lines.append(f'- 若连续3日"持有/加仓"但市场跌>3% → 矩阵漏判, 建议下调看多阈值')
    lines.append('')

    # ====== 三、情景推演 (第三刀: scenario_engine 六法则 → 前照灯) ======
    lines.append('## 🔮 穿透式情景推演（明日预演）')
    lines.append('')
    lines.append('> 依托 `scenario_engine.py` 六法则: 预期差→四象限→百分位→概率推演→压力测试→贝叶斯校准')
    lines.append('')

    try:
        from engine.scenario_engine import (
            expectation_gap, four_quadrant, percentile_rank,
            scenario_probability, stress_test, bayesian_loop_update
        )

        # 百分位
        perc = percentile_rank()
        if perc.get('rankings'):
            lines.append('### 关键指标百分位')
            lines.append('')
            lines.append('| 指标 | 当前值 | 历史分位 | 水位 |')
            lines.append('|------|--------|:--:|------|')
            for r in perc['rankings'][:5]:
                lines.append(f'| {r["指标"]} | {r["当前"]} | {r["分位"]} | {r["水位"]} |')
            lines.append('')

        # 四象限
        quad = four_quadrant()
        lines.append(f'**市场象限**: {quad.get("quadrant", "?")} → {quad.get("style", "?")} — {quad.get("desc", "")}')
        lines.append('')

        # 概率推演
        probs = scenario_probability()
        lines.append('### 三情景推演')
        lines.append('')
        lines.append('| 情景 | 概率 | 触发条件 | 仓位 | 标的 |')
        lines.append('|------|:--:|------|------|------|')
        for key, label in [('bull', '🟢 乐观'), ('neutral', '🟡 中性'), ('bear', '🔴 悲观')]:
            p = probs.get(key, {})
            lines.append(f'| {label} | **{p.get("概率","?")}** | {p.get("触发条件","?")[:40]} | '
                        f'{p.get("仓位","?")} | {p.get("标的","?")[:30]} |')
        lines.append('')

        # 贝叶斯回路
        bayes = bayesian_loop_update()
        bs_alert = bayes.get('alert')
        lines.append(f'**贝叶斯动态校准**: 先验牛{bayes["prior"]["bull"]}/熊{bayes["prior"]["bear"]} '
                    f'→ 后验{bayes["posterior"]:.1%} | 预测={bayes["predicted"]} 实际={bayes["actual"]} '
                    f'| 误差={bayes["error"]:.0f} | 连续错误={bayes["error_streak"]}天')
        if bs_alert:
            lines.append(f'> {bs_alert["level"]} {bs_alert["msg"]}')
        lines.append('')

        # 压力测试
        stress = stress_test()
        scenarios = stress.get('scenarios', [])
        if scenarios:
            lines.append('### 压力测试')
            lines.append('')
            lines.append('| 场景 | 市场跌幅 | 组合损失 | 是否超标 |')
            lines.append('|------|:--:|:--:|:--:|')
            for s in scenarios[:5]:
                lines.append(f'| {s["场景"][:20]} | {s["市场跌幅"]} | {s["组合损失"]} | {s["是否超标"]} |')
            lines.append('')

    except Exception as e:
        lines.append(f'> ⚠ 情景推演模块暂不可用: {e}')
        lines.append('')

    # 数据底座溯源
    lines.append('### 📊 数据底座溯源')
    lines.append('')
    lines.append('| 数据项 | 最新值 | 日期 | 新鲜度 |')
    lines.append('|--------|--------|------|--------|')
    for k, v in report_data['data_freshness'].items():
        icon = '✅' if v['fresh'] else f'⚠ 滞后{v["lag"]}天'
        lines.append(f'| {k} | - | {v["date"]} | {icon} |')
    lines.append('')

    # ====== 接线2: 管道回测面板 (v7两阶段: 工作日只读缓存, 周末全量刷新) ======
    lines.append('## 📈 管道回测健康度')
    lines.append('')
    try:
        from engine.pipeline_backtest import render_report_md
        import json as _json

        cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'pipeline_backtest_cache.json')
        cached = None
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = _json.load(f)

        if cached:
            bt_report = cached['report']
            cache_dt = cached.get('date', '?')
            wd = date.today().weekday()
            next_refresh = '周末' if wd < 5 else '下周末'
            lines.append(f'> 上次全量回测: **{cache_dt}** | 下次刷新: **{next_refresh}** (周末自动)')
            lines.append(f'> 工作日策略: 只出裁决不磨刀——跳过回测, 直接读缓存')
            lines.append('')
            lines.append(render_report_md(bt_report))
        else:
            lines.append('> ⚠ 管道回测缓存不存在。请在周末运行: python run_weekend_audit.py')
    except Exception as e:
        lines.append(f'> ⚠ 管道回测暂不可用: {e}')
    lines.append('')

    # ====== 接线3: 新闻舆情 (v9 news_hub 间接修正) ======
    lines.append('## 📰 新闻舆情数据')
    lines.append('')

    # 调新闻中枢 (与 judge_holdings 内调用共享同一数据源, 无副作用)
    nv = None
    try:
        from engine.news_hub import daily_verdict as _nv_daily
        nv = _nv_daily()
    except Exception:
        pass

    if nv and nv.get('status') not in ('empty', None):
        direction = nv.get('dimension_direction', 'neutral')
        score = nv.get('dimension_score', 50.0)
        news_delta = score - 50.0
        icon = '🔴' if direction == 'bearish' else ('🟢' if direction == 'bullish' else '⚪')

        lines.append(f'**当前状态**: {icon} {direction.upper()} | 舆情综合分: {score:.1f} 分 (中性=50)')
        lines.append(f'**今日焦点**: 捕获新闻 **{nv.get("news_count", 0)}** 条 | 降级模块: {len(nv.get("degradations", []))} 个')
        lines.append(f'**矩阵渗透**: 新闻偏离度 {news_delta:+.1f} 分 → 宏观体制 / 资金流已执行全局偏置修正')
        lines.append('')

        # 宏观冲击
        ms = nv.get('macro_shock', {})
        if ms.get('triggers'):
            lines.append(f'**宏观冲击**: {ms.get("level", "normal")} (得分 {ms.get("score", 0)}) | VIX={ms.get("vix", "?")}')
            for t in ms.get('triggers', [])[:3]:
                lines.append(f'- 📅 {t.get("date", "?")}: {t.get("name", "?")[:60]}')

        # 板块能量
        se = nv.get('sector_energy', {})
        bullish = se.get('bullish_sectors', [])
        bearish = se.get('bearish_sectors', [])
        if bullish or bearish:
            lines.append(f'**板块能量**: 偏多 {len(bullish)} 个 | 偏空 {len(bearish)} 个')
            if bullish:
                lines.append(f'- 🟢 偏多: {", ".join(bullish[:5])}')
            if bearish:
                lines.append(f'- 🔴 偏空: {", ".join(bearish[:5])}')

        # 资金流预警
        fa = nv.get('flow_alert', {})
        if fa.get('active'):
            lines.append(f'**⚠ 资金流预警**: 触发! 风险板块: {", ".join(fa.get("sectors_at_risk", []))}')
            for a in fa.get('alerts', [])[:3]:
                lines.append(f'- {a.get("sector", "?")}: {a.get("alert", "")[:60]}')

        # NLP预期差
        nl = nv.get('nlp_surprise', {})
        flagged = nl.get('flagged_events', [])
        if flagged:
            lines.append(f'**NLP预期差**: {nl.get("surprise_count", 0)} 条超预期事件')
            for fe in flagged[:3]:
                lines.append(f'- [{fe.get("direction", "?")}] {fe.get("title", "")[:50]} (mag={fe.get("magnitude", 0)})')

    else:
        status_text = 'CALM — 今日无重大新闻事件' if (nv and nv.get('status') == 'empty') else '新闻中枢不可用'
        lines.append(f'> ⚪ **当前状态**: {status_text}，舆情维持 50.0 中性。')
        if nv and nv.get('degradations'):
            for d in nv['degradations']:
                lines.append(f'> ⚠ 降级: {d[:80]}')
    lines.append('')

    lines.append('---')
    lines.append(f'> 天眼2.0裁决引擎 v2.1 | 9维矩阵投票制 | 无一票否决 | 纠错线全覆盖 | {now_str} 执行完毕')
    # 改3: ε审计标签行
    l3_data = report_data.get('layer3', {})
    eps_audit_info = l3_data.get('epsilon_audit', {})
    if eps_audit_info:
        lines.append('')
        lines.append('---')
        lines.append('')
        lines.append('## 🔬 ε审计标签 (Water-Filling框架)')
        lines.append('')
        lines.append(f'> 操盘方伪装预算分配: 低成本通道先被污染, 极高/无限成本通道ε≈0')
        lines.append(f'> 当日利好复核: {eps_audit_info.get("bullish_checked",0)}条, 名实冲突: {eps_audit_info.get("conflicts",False)}')
        lines.append('')
        lines.append('| 通道 | ε等级 | 腐败成本(c_i) | 权重 | 状态 |')
        lines.append('|------|------|-------------|------|------|')
        for key, se in SIGNAL_EPSILON.items():
            if key == '_default': continue
            weight = se['weight']
            w_label = '正常' if weight >= 1.0 else ('降权' if weight > 0 else '排除')
            label = se['label']; eps_val = se['ε']; ci = se['c_i']
            lines.append('| {} | ε{} | {} | {} | - |'.format(label, eps_val, ci, '{:.0%}({})'.format(weight, w_label)))
        lines.append('')
        # 名实背离告警
        conflicts = l3_data.get('name_reality_conflicts', [])
        if conflicts:
            lines.append('### ⚠ 名实背离告警 (ε≈0通道与利好矛盾)')
            for c in conflicts:
                lines.append(f'- {c}')
            lines.append('')
    lines.append('')
    return '\n'.join(lines)


# ============================================================
# CLI入口
# ============================================================
if __name__ == '__main__':
    import argparse as _ap
    _parser = _ap.ArgumentParser(description='天眼2.0 统一裁决引擎')
    _parser.add_argument('--full-data', action='store_true', help='日报v5: 输出全量JSON数据(无叙述)')
    _args = _parser.parse_args()

    if _args.full_data:
        from engine.data_guard import DataGuard
        guard = DataGuard()
        ok, cells = guard.preflight_check()
        stale_cells = [c for c in cells if c.is_stale]
        critical = [c for c in stale_cells if c.is_expired]

        report = generate_unified_report()
        # 预过滤：空模块不传
        filtered = {
            "generated_at": report.get("generated_at", ""),
            "data_freshness": report.get("data_freshness", {}),
            "stale_desc": report.get("stale_desc", ""),
            "critical_stale": len(critical),
            "layer1": report.get("layer1", {}),
            "layer2": {k: v for k, v in report.get("layer2", {}).items()
                       if k in ('oneil_state', 'emotion', 'emotion_score', 'vol_trend',
                                'vol_sufficient', 'up_ratio', 'base_win_rate', 'adj_win_rate',
                                'key_indices', 'vol_analysis')},
            "layer3": report.get("layer3", {}),
            "fragility": report.get("fragility", {}),
            "attack_signals": report.get("attack_signals", {}),
            "conflicts": report.get("conflicts", {}),
            "holdings": report.get("holdings", []),
            "total_value": report.get("total_value", 0),
            "total_pnl": report.get("total_pnl", 0),
            "bayes_meltdown": report.get("bayes_meltdown", False),
            "scenario_engine": report.get("scenario_engine", {}),
            "negative_list_vetoes": report.get("negative_list_vetoes", []),
            "correction_lines": report.get("correction_lines", []),
        }
        print(json.dumps(filtered, ensure_ascii=False, indent=2, default=str))
        sys.exit(0)

    from engine.data_guard import DataGuard

    print("天眼2.0 统一裁决引擎")
    print("=" * 55)

    # DataGuard 预检
    guard = DataGuard()
    ok, cells = guard.preflight_check()
    stale_cells = [c for c in cells if c.is_stale]
    critical = [c for c in stale_cells if c.is_expired]

    if critical:
        print(f"\n{'='*55}")
        print(f"[FATAL] 数据严重过期 ({len(critical)}项), 拒绝生成报告")
        print(f"{'='*55}")
        for c in critical:
            print(f"  {c.status_line()} - 请先运行: python tianyan.py daily")
        print(f"{'='*55}")
        sys.exit(1)

    if stale_cells:
        print(f"\n[WARN] 数据部分过期 ({len(stale_cells)}项), 继续生成但标注过期")
        for c in stale_cells:
            print(f"  {c.status_line()}")
    else:
        print(f"\n[OK] 数据新鲜 ({len(cells)}项检查通过)")

    print()

    # 生成裁决
    report = generate_unified_report()
    formatted = format_report(report)
    print(formatted)

    # 保存
    out_dir = os.path.join(ROOT, 'reports')
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f'天眼2.0裁决_{today_str}.md')
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(formatted)
    print(f'\n已保存: {out_file}')
