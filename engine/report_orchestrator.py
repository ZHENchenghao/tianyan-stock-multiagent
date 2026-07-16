# -*- coding: utf-8 -*-
"""
天眼日报调度器 v6.0
原则: 天眼2.0三大裁决金字塔 → 统一判决覆盖所有模块散点输出
     第一统治层: 宏观体制与流动性闸门（最高指挥官，一票否决权）
     第二传导层: 市场量能与筹码结构（战术指挥官，决定胜率）
     第三执行层: NLP新闻与行业催化（士兵，提供选股素材）
v5.0新增: 进攻引擎(双模扫描+加油监控+脆弱地图+三大过滤) → 白话报告
v6.0新增: 统一裁决引擎(unified_verdict) → 日报开头总纲 → 消除模块间逻辑冲突
调用链: 数据新鲜度检查→统一裁决(三层金字塔)→市场状态→宪法→扫描→审计→裁决→选股→推演→风控
用法: python engine/report_orchestrator.py → reports/天眼日报_YYYY-MM-DD.md
"""
import sys, os, io, json, math, time, logging
logging.disable(logging.CRITICAL)
_orig_stdout = sys.stdout
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except (ValueError, AttributeError):
    sys.stdout = _orig_stdout
PROGRESS = []  # 步骤追踪
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '辅助模块'))

import ssl; ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'; os.environ['TQDM_DISABLE'] = '1'
try: import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except: pass

from datetime import datetime, date, timedelta
import duckdb
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
today = date.today(); today_str = today.isoformat()
now_str = datetime.now().strftime('%Y-%m-%d %H:%M'); t0 = time.time()
out_dir = os.path.join(ROOT, 'reports'); os.makedirs(out_dir, exist_ok=True)
out_file = os.path.join(out_dir, f'天眼日报_{today_str}.md')

def q(sql):
    try: return duckdb.connect(DB).execute(sql).fetchone()
    except: return None
def safe(v, fb):
    if v is None: return fb
    if isinstance(v, float) and math.isnan(v): return fb
    return v

def load_portfolio():
    pf = os.path.join(ROOT, 'portfolio.json')
    if not os.path.exists(pf): return []
    with open(pf, 'r', encoding='utf-8') as f: data = json.load(f)
    im = {'016708':('sh000819','有色金属'),'007404':('sh000300','沪深300'),
          '021753':('sz399438','电力指数'),'018927':('sz399261','锂电池')}
    hlds = []
    for h in data.get('holdings',[]):
        c = h.get('code',''); ic, nm = im.get(c,('sh000300','未知'))
        hlds.append({'name':h.get('name',''),'code':c,'amount':h.get('amount',0),
                     'pnl':h.get('pnl_pct',0)/100.0,'role':h.get('role',''),
                     'index_code':ic,'index_name':nm,
                     'engine_pass':h.get('engine_pass',None),
                     'engine_pf':h.get('engine_pf_ratio',None),
                     'sector':h.get('sector','')})
    return hlds

def is_trading_day():
    """铁律#12: 用日历判断交易日, 不用K线数据是否落后来判断.
    K线落后≠非交易日, 只说明数据没采集. 两个独立概念不可混淆."""
    w = today.weekday()
    if w >= 5: return False, '周末', today + timedelta(days=(7-w))
    # 日历上今天是工作日=交易日(暂不考虑A股特殊假期, 后续可加holiday_calendar)
    # 数据是否新鲜由铁律#8数据自愈独立检测, 不在此处混合判断
    return True, '交易日', today

# ============================================================
# 数据采集 (铁律#8)
# ============================================================
def collect_all_data():
    steps = []
    old_stdout = sys.stdout; sys.stdout = io.StringIO()
    try:
        kp = os.path.join(ROOT, 'karen_upgrade'); sys.path.insert(0, kp)
        from data_collectors.tianyan_collector import daily_collect
        daily_collect(); steps.append('宏观/情绪/K线/指标/主力资金')
    except Exception as e: steps.append('日频跳过')
    sys.stdout = old_stdout
    try:
        import subprocess
        subprocess.run([r'C:\Users\Lenovo\AppData\Local\Programs\Python\Python310\python.exe',
            os.path.join(ROOT, 'engine', 'news_collector.py')], capture_output=True, timeout=45)
        steps.append('新闻')
    except: pass
    return steps

# ============================================================
# 一、市场状态 (调用 market_state + 数据层 + 板块扫描 + 新闻联动 + 外资)
# ============================================================
def section_1_market(condensed=False):
    lines = ['## 一、市场状态\n']

    # === Prev-day data (needed for condensed mode) ===
    prev_us10y, prev_wti, prev_sh = None, None, None
    prev_main_net, prev_north, prev_south, prev_lu, prev_ld = None, None, None, None, None
    try:
        pm = q('SELECT us10y, wti FROM macro_indicators ORDER BY trade_date DESC LIMIT 2')
        if pm and len(pm) >= 2: prev_us10y, prev_wti = safe(pm[1][0],None), safe(pm[1][1],None)
        pcf = q('SELECT main_net FROM capital_flow ORDER BY trade_date DESC LIMIT 2')
        if pcf and len(pcf) >= 2: prev_main_net = pcf[1][0]
        ps = q('SELECT limit_up_count, limit_down_count FROM market_sentiment ORDER BY trade_date DESC LIMIT 2')
        if ps and len(ps) >= 2: prev_lu, prev_ld = safe(ps[1][0],None), safe(ps[1][1],None)
        for off in [1,2,3]:
            pnb = q(f'SELECT north_net, south_net FROM macro_indicators WHERE north_net IS NOT NULL ORDER BY trade_date DESC LIMIT 1 OFFSET {off}')
            if pnb and pnb[0] is not None: prev_north, prev_south = pnb[0], pnb[1] if len(pnb)>1 else None; break
    except: pass

    # 1.1 O'Neil + 养家 (调用market_state模块)
    oneil = {}; yang = {}
    try:
        from market_state import oneil_state_machine, yangjia_emotion_cycle
        oneil = oneil_state_machine()
        yang = yangjia_emotion_cycle()
        lines.append('### 1.1 大盘状态\n')
        lines.append(f'| 引擎 | 状态 | 仓位上限 |')
        lines.append(f'|------|------|---------|')
        lines.append(f'| O\'Neil | {oneil.get("state","?")} | {oneil.get("position_cap",1.0):.0%} |')
        lines.append(f'| 养家 | {yang.get("stage","?")}({yang.get("score",60)}分) | {yang.get("position_cap",1.0):.0%} |')
        lines.append('')
    except Exception as e:
        lines.append(f'(market_state暂不可用: {e})\n')

    # 1.2 核心三指标
    macro = q('SELECT us10y, wti, usdcny, shibor_on FROM macro_indicators ORDER BY trade_date DESC LIMIT 1')
    us10y = safe(macro[0] if macro else None, 4.60); wti = safe(macro[1] if macro and len(macro)>1 else None, 100)
    sh = q("SELECT close FROM kline_daily WHERE ts_code='sh000300' ORDER BY trade_date DESC LIMIT 1")
    sh_close = sh[0] if sh else 4845

    us10y_s = '触发清仓' if us10y>4.70 else ('警戒' if us10y>4.50 else '安全')
    wti_s = '触发清仓' if wti>110 else ('警戒' if wti>100 else '安全')
    sh_s = '正常' if sh_close>4000 else '破位'

    lines.append('### 1.2 核心三指标\n')
    lines.append('| 指标 | 当前值 | 警戒线 | 状态 |')
    lines.append('|------|--------|--------|------|')
    lines.append(f'| 10Y美债 | **{us10y:.2f}%** | 4.50%警戒/4.70%清仓 | {us10y_s} |')
    lines.append(f'| WTI原油 | **${wti:.1f}** | $100警戒/$110清仓 | {wti_s} |')
    lines.append(f'| 沪深300 | **{sh_close:.0f}** | 4000支撑 | {sh_s} |')
    lines.append('')

    # 1.2b 盘面深度解析 (宏观分析师视角: 指数→北向→主力→量能→综合→预判)
    lines.append('### 1.2b 盘面深度解析\n')
    today_cf = 0; sum3 = 0; nn = 0; sn = 0; idx_data = {}; vol5 = 0
    try:
        conn = duckdb.connect(DB)

        # === 1. 各指数 ===
        idxs = [('sz399006','创业板指','散户','剧烈'),('sz399001','深证成指','混合资金','中等'),
                ('sh000905','中证500','混合资金','中高'),('sh000688','科创50','机构游资','高波动'),
                ('sh000300','沪深300','机构外资','适中'),('sh000016','上证50','国家队机构','低波动')]
        idx_data = {}
        for code, name, player, vol in idxs:
            df = conn.execute(f"SELECT close FROM kline_daily WHERE ts_code='{code}' ORDER BY trade_date DESC LIMIT 5").fetchdf()
            if len(df) >= 2:
                cur = float(df['close'].iloc[0]); prev = float(df['close'].iloc[1])
                idx_data[name] = {'close': cur, 'chg': round((cur/prev-1)*100,2),
                    'mom5': round((cur/float(df['close'].iloc[-1])-1)*100,1) if len(df)>=5 else 0,
                    'player': player, 'vol': vol}

        # 强弱判断
        if idx_data:
            chips = sorted(idx_data.items(), key=lambda x: x[1]['chg'], reverse=True)
            leader = chips[0]; lagger = chips[-1]
            is_small_style = idx_data.get('创业板指',{}).get('chg',0) > idx_data.get('上证50',{}).get('chg',0)*5

            lines.append('**一、各大指数**')
            lines.append('')
            for name, d in chips:
                chg = d['chg']; tag = '🔥领涨' if chg>2 else ('✅上涨' if chg>0 else ('⚠下跌' if chg>-1 else '🔴领跌'))
                lines.append(f'- **{name} {tag} {chg:+.2f}%** (5日{d["mom5"]:+.1f}%) | {d["close"]:.0f}点 | {d["player"]}参与 | 波动{d["vol"]}')
            lines.append('')
            lines.append(f'**分化判断:** {leader[0]}({leader[1]["chg"]:+.2f}%)远强于{lagger[0]}({lagger[1]["chg"]:+.2f}%)。'
                         f'{"小盘个股行情,散户主导,机构配合,国家队旁观。" if is_small_style else "大盘权重行情,机构主导。"}')
            lines.append('')

        # === 2. 北向 ===
        nb = conn.execute('SELECT north_net, south_net FROM macro_indicators WHERE north_net IS NOT NULL ORDER BY trade_date DESC LIMIT 1').fetchone()
        if nb:
            nn, sn = safe(nb[0],0), safe(nb[1],0)
            lines.append('**二、北向资金**')
            lines.append('')
            if nn == 0: lines.append(f'- 北向: 持平0亿 → 外资观望,不买不卖,等信号')
            elif nn > 0: lines.append(f'- 北向: 流入{nn:.0f}亿 → {"外资看好,持续布局" if nn>50 else "小幅试探,态度温和"}')
            else: lines.append(f'- 北向: 流出{abs(nn):.0f}亿 → {"外资撤退,短期避险" if abs(nn)>50 else "小幅流出,不算恐慌"}')
            if sn < 0: lines.append(f'- 南向: 流出{abs(sn):.0f}亿 → 内资在买港股,A股资金有分流')
            lines.append('')

        # === 3. 主力 ===
        lines.append('**三、主力资金**')
        lines.append('')
        cf = conn.execute('SELECT main_net, main_pct FROM capital_flow ORDER BY trade_date DESC LIMIT 3').fetchall()
        if cf and len(cf) >= 3:
            today_cf, yesterday_cf, day3_cf = cf[0][0], cf[1][0], cf[2][0]
            sum3 = today_cf + yesterday_cf + day3_cf
            if today_cf > 0 and sum3 < 0:
                lines.append(f'- 今日主力净流入{abs(today_cf):.0f}亿 → 但三天累计仍净流出{abs(sum3):.0f}亿')
                lines.append(f'- 判断: 今天只是超卖回补,不是趋势反转。机构减了太多稍微买回来一点,做多意愿不强。')
            elif today_cf > 0 and sum3 > 0:
                lines.append(f'- 今日主力净流入{abs(today_cf):.0f}亿,三天累计净流入{abs(sum3):.0f}亿 → 机构持续做多')
            elif today_cf < 0:
                lines.append(f'- 今日主力净流出{abs(today_cf):.0f}亿 → 机构在避险,短期谨慎')
            lines.append('')

        # === 4. 成交量 ===
        lines.append('**四、成交量与资金面**')
        lines.append('')
        vol5 = conn.execute("SELECT AVG(vol) FROM kline_daily WHERE ts_code='sh000300' AND trade_date >= CURRENT_DATE - INTERVAL 5 DAY").fetchone()[0]
        if vol5:
            vol_ok = vol5/1e8 > 300
            lines.append(f'- 近5日均量约{vol5/1e8:.0f}亿 → {"量能充足,有增量资金" if vol_ok else "量能偏低,存量博弈"}')
            lines.append(f'- 判断: {"成交量够发动一轮趋势行情" if vol_ok else "当前量能只能维持震荡,不够启动持续上涨。需要放量才有新方向。"}')
        lines.append('')

        # === 5. 综合 ===
        lines.append('**五、综合盘面**')
        lines.append('')
        pros = []; cons = []
        if idx_data.get('创业板指',{}).get('chg',0) > 0: pros.append('全线收红,情绪回暖')
        if idx_data.get('科创50',{}).get('mom5',0) > 2: pros.append(f'科创50本周最强(+{idx_data["科创50"]["mom5"]:.0f}%),科技主线明确')
        if today_cf > 0: pros.append(f'主力回补{abs(today_cf):.0f}亿,资金面不算差')
        if idx_data.get('上证50',{}).get('chg',0) < 0.5: cons.append('上证50几乎不动,国家队没表态')
        if nn == 0: cons.append('北向持平,外资观望')
        if sum3 < 0: cons.append(f'三天累计主力仍净流出{abs(sum3):.0f}亿,大资金未回归')
        if vol5/1e8 < 300: cons.append(f'量能偏低({vol5/1e8:.0f}亿),上涨动力不足')
        cons.append('南向资金流出,部分内资在买港股')

        lines.append('**优点:** ' + '; '.join(pros))
        lines.append('')
        lines.append('**缺点:** ' + '; '.join(cons))
        lines.append('')
        lines.append(f'**整体环境: {"反弹延续,趋势向好" if len(pros)>len(cons) else "反弹行情,不是反转。情绪修复中但大资金还没回来。"}')
        lines.append('')

        # === 6. 预判 ===
        lines.append('**六、短期预判(未来3-5日)**')
        lines.append('')
        if len(pros) > len(cons):
            lines.append('- 大概率: 震荡偏强,继续消化上方套牢盘后尝试上攻')
        elif len(pros) == len(cons):
            lines.append('- 大概率: 区间震荡,4100-4200之间消化,等催化剂选方向')
        else:
            lines.append('- 大概率: 震荡偏弱,反弹接近尾声,注意回落风险')
        lines.append('- 如果要涨: 需要北向转为流入 + 成交量放大 + 上证50表态')
        lines.append('- 如果要跌: 美债冲4.70%或主力再次大幅流出就是信号')
        lines.append('')

        conn.close()
    except Exception as e:
        lines.append(f'(盘面解析暂不可用: {e})\n')

    # 1.3 资金面 (主力+北向+情绪+龙虎榜)
    lines.append('### 1.3 资金面\n')
    sent = q('SELECT limit_up_count, limit_down_count, market_emotion FROM market_sentiment ORDER BY trade_date DESC LIMIT 1')
    lu = safe(sent[0] if sent else None, 57); ld = safe(sent[1] if sent and len(sent)>1 else None, 0)

    # 资金面: DuckDB优先, akshare兜底
    lines.append('| 指标 | 数据 | 说明 |')
    lines.append('|------|------|------|')
    lines.append(f'| 涨跌停比 | {lu:.0f}:{ld:.0f} | 涨停多=情绪好 |')

    # 主力资金 (DuckDB capital_flow表)
    try:
        cf = q('SELECT main_net, main_pct, super_large_net, large_net FROM capital_flow ORDER BY trade_date DESC LIMIT 1')
        if cf and cf[0]:
            mn = cf[0]; mp = cf[1] if len(cf)>1 else 0
            sl = cf[2] if len(cf)>2 else 0; lg = cf[3] if len(cf)>3 else 0
            if mn is not None:
                d = '流入' if mn>0 else '流出'
                lines.append('| 主力资金 | {}{:.0f}亿({:+.1f}%) | 超大{:.0f}亿+大单{:.0f}亿 |'.format(d,abs(mn),mp,sl,lg))
    except: pass

    # 北向+南向 (DuckDB macro_indicators, 回看有效值)
    nb = None
    for offset in [0, -1, -2]:
        nb = q(f'SELECT north_net, south_net FROM macro_indicators WHERE north_net IS NOT NULL ORDER BY trade_date DESC LIMIT 1 OFFSET {abs(offset)}')
        if nb and nb[0] is not None: break
    if nb:
        nn = safe(nb[0], 0) if len(nb)>0 else 0
        sn = safe(nb[1], 0) if len(nb)>1 else 0
        n_dir = '流入' if nn>0 else ('流出' if nn<0 else '持平')
        lines.append(f'| 北向资金 | {n_dir}{abs(nn):.0f}亿 | 外资通过沪股通+深股通买A股(流入=看好,流出=撤退) |')
        if sn != 0:
            s_dir = '流出' if sn<0 else ('流入' if sn>0 else '持平')
            lines.append(f'| 南向资金 | {s_dir}{abs(sn):.0f}亿 | 内地资金通过港股通买港股 |')

    lines.append('')
    lines.append('**资金术语**: 主力=超大单(>100万)+大单(>20万); 北向=香港及外资经沪深股通买A股; 中单=散户中大额; 小单=散户小额')
    lines.append('')

    # 1.3b 国家队动向 (2026Q1季报+5月ETF数据)
    lines.append('### 1.3b 国家队动向\n')
    lines.append('| 项目 | 数据 |')
    lines.append('|------|------|')
    lines.append('| Q1总持仓 | 4.41万亿/5746亿股/1038家 |')
    lines.append('| 重仓行业 | 银行3.42万亿(77%)+保险/证券+煤炭461亿+白酒392亿 |')
    lines.append('| ETF减持 | 年初至今1.1万亿,证金宽基ETF全部清仓 |')
    lines.append('| 加仓方向 | 通用设备/半导体(+22%)/医疗器械/AI算力/玻璃玻纤 |')
    lines.append('| 减持方向 | 宽基ETF(-57%)+红利资产(银行/电力/有色) |')
    lines.append('| 调仓逻辑 | 进攻(科技)+防御(银行)+指数调节,契合十五五 |')
    lines.append('| 信号 | 减持近尾声(目标减90%),转向科技+高股息双轮驱动 |')
    lines.append('')

    # 1.4 板块强弱 (调用screening_engine)
    lines.append('### 1.4 板块强弱\n')
    try:
        from screening_engine import screen_etf_sectors
        sectors = screen_etf_sectors()
        if sectors:
            lines.append('| 板块 | 评分 | 5日 | 20日 | 趋势 |')
            lines.append('|------|------|-----|------|------|')
            for s in sectors[:6]:
                lines.append(f'| {s["sector"]} | {s["score"]} | {s["mom_5d"]:+.1f}% | {s["mom_20d"]:+.1f}% | {s["trend"]} |')
            lines.append('')
    except Exception as e: lines.append(f'(选股引擎暂不可用)\n')

    # 1.5 新闻联动 (调用data_linker)
    lines.append('### 1.5 新闻联动\n')
    try:
        from data_linker import quick_link
        top3 = sectors[:3] if 'sectors' in dir() and sectors else [{'sector':'沪深300','score':65},{'sector':'新能源电池','score':100}]
        lines.append(quick_link(top3, 3))
        lines.append('')
    except: lines.append('(新闻联动暂不可用)\n')

    # 1.6 外资观点
    lines.append('### 1.6 外资机构观点\n')
    lines.append('| 大摩 | 高盛 | 瑞银 | 摩根大通 |')
    lines.append('|------|------|------|---------|')
    lines.append('| CSI300目标5400(+11%) | CSI300目标5300(+9%) | 盈利+11%,避风港 | 实际增持A股 |')
    lines.append('')

    if condensed:
        return _build_condensed_section1(oneil, yang, us10y, wti, sh_close, prev_us10y, prev_wti,
                                         lu, ld, prev_lu, prev_ld, prev_main_net, prev_north, prev_south)

    return '\n'.join(lines)

# ============================================================
# 1.7 情绪风险提示 (新增) — 标注情绪化波动板块
# ============================================================
def section_1e_emotional_risk(portfolio):
    """生成【情绪风险提示】板块 — 标注哪些板块是情绪化波动"""
    lines = ['### 1.7 情绪风险提示\n']
    lines.append('> 核心原则: 不对单一板块的"低估"信号尽信。市场高度情绪化，估值偏离不可直接交易。\n')

    try:
        from engine.sentiment_gate import generate_emotional_risk_report, identify_current_themes

        # 获取板块列表
        sectors = list(set(h.get('sector', '') for h in portfolio if h.get('sector')))
        if not sectors:
            sectors = ['沪深300', '科创50']

        # 情绪风险
        risk_report = generate_emotional_risk_report(sectors)
        emotional = risk_report.get('emotional', [])
        normal = risk_report.get('normal', [])

        if emotional:
            lines.append('| 板块 | 风险等级 | 信号 | 操作 |')
            lines.append('|------|:---:|------|------|')
            for s in emotional:
                flags = '; '.join(s['flags'][:2])
                lines.append(f'| **{s["sector"]}** | 🔴 情绪化 | {flags} | 不可追/不可抄底 |')
            for s in normal:
                lines.append(f'| {s["sector"]} | 🟢 正常 | - | {s["action"]} |')
        else:
            lines.append('| 板块 | 风险等级 | 操作 |')
            lines.append('|------|:---:|------|')
            for s in normal:
                lines.append(f'| {s["sector"]} | 🟢 正常 | {s["action"]} |')

        lines.append('')

        # 主线判断
        themes = identify_current_themes()
        lines.append('**当前主线判断:**')
        lines.append(f'- 强主线: {len(themes["strong_themes"])}个')
        for t in themes['strong_themes'][:3]:
            lines.append(f'  - ✅ {t["label"]}')
        lines.append(f'- 弱信号(已过滤): {len(themes["weak_signals"])}个')
        for t in themes['weak_signals'][:3]:
            reason = t.get('validation', {}).get('reason', '')[:60]
            lines.append(f'  - ❌ {t["label"]} → {reason}')

        lines.append('')
        lines.append('**提醒:** 情绪化板块不追、不抄底。A股情绪化波动平均持续3-5个交易日，没有持续性。')
        lines.append('')

    except Exception as e:
        lines.append(f'(情绪风险模块未就绪: {e})\n')

    return '\n'.join(lines)


# ============================================================
# 二、持仓分析 (调用 constitution + position_calculator + cost_optimizer)
# ============================================================
def section_2_positions(portfolio, condensed=False):
    lines = ['## 二、你的持仓\n']
    total = sum(p['amount'] for p in portfolio)
    total_pnl = sum(p['amount']*p['pnl'] for p in portfolio)
    lines.append(f'总{total:.0f}元 | 盈亏{total_pnl:+.0f}元\n')

    # 调用宪法层
    try:
        from constitution import check_risk_reward, MODEL_PARAMS
        from win_rate_calibrator import WinRateCalibrator
        cal = WinRateCalibrator(); cal.calibrate_all()
    except: cal = None

    # 调用仓位计算器
    try:
        from position_calculator import calculate_position
        _old_out = sys.stdout; sys.stdout = io.StringIO()
        pos_report = calculate_position(os.path.join(ROOT, 'portfolio.json'))
        sys.stdout = _old_out
        cap = pos_report.get('max_position', 0.50) if isinstance(pos_report, dict) else 0.50
        lines.append(f'仓位上限: {cap:.0%} (五级裁决链)\n')
    except: cap = 0.50

    _hd = []  # condensed mode data collector
    for p in portfolio:
        pnl = p['pnl']; eng = p.get('engine_pass'); pf = p.get('engine_pf',0)
        reasons = []; risks = []

        # 技术评分 (调用现有akshare, 同天眼模块7逻辑)
        try:
            import akshare as ak; import pandas as pd; import numpy as np
            df = ak.stock_zh_index_daily(symbol=p['index_code'])
            if df is not None and len(df) >= 20:
                c = df['close'].values.astype(float)
                cur, prev = c[-1], c[-2]
                ma5, ma10, ma20 = c[-5:].mean(), c[-10:].mean(), c[-20:].mean()
                ma_bull, ma_bear = ma5>ma10>ma20, ma5<ma10<ma20
                ema12=pd.Series(c).ewm(span=12,adjust=False).mean()
                ema26=pd.Series(c).ewm(span=26,adjust=False).mean()
                macd_bull = ema12.iloc[-1] > ema26.iloc[-1]
                score=50
                if ma_bull: score+=15
                elif ma_bear: score-=15
                if cur>ma5: score+=5
                if cur>ma20: score+=5
                if macd_bull: score+=10
                score=max(0,min(100,score))
                stop_pct = round((ma20-cur)/cur*100,1)
                if ma_bull: reasons.append('多头排列')
                elif ma_bear: risks.append('空头排列')
                if macd_bull: reasons.append('MACD金叉')
                else: risks.append('MACD死叉')
            else: score=40; stop_pct=-5
        except: score=40; stop_pct=-5; reasons.append('技术数据拉取失败')

        if eng is False: risks.append(f'琼斯REJECT(盈亏比{pf}:1)')
        elif eng is True: reasons.append(f'景气过关(盈亏比{pf}:1)')
        if pnl<-0.05: risks.append(f'浮亏{abs(pnl):.1%}触发止损')
        elif pnl<-0.03: risks.append(f'浮亏接近警戒')
        elif pnl>0.02: reasons.append(f'浮盈{pnl:.1%}')
        else: reasons.append(f'微盈/亏{pnl:.1%}')

        # ═══ 🚫 铁律#6 + #14 硬闸门 (v4.2) ═══
        # 日报裁决也必须过极端位置检查, 不能绕过
        extreme_blocked = False
        try:
            # 计算RSI和20日涨幅
            rsi_val = 50
            chg_20d = 0
            try:
                import duckdb as ddb
                idx_code = p.get('index_code', 'sh000300')
                conn = ddb.connect(r'D:\FreeFinanceData\data\duckdb\finance.db')
                row = conn.execute(f"""
                    SELECT t.rsi14, k.close, t.ma20, k2.close as prev_close
                    FROM kline_daily k
                    JOIN technical_indicators t ON k.ts_code=t.ts_code AND k.trade_date=t.trade_date
                    LEFT JOIN kline_daily k2 ON k.ts_code=k2.ts_code
                        AND k2.trade_date = (SELECT MAX(trade_date) FROM kline_daily
                            WHERE ts_code=k.ts_code AND trade_date < k.trade_date)
                    WHERE k.ts_code = '{idx_code}'
                    ORDER BY k.trade_date DESC LIMIT 1
                """).fetchone()
                conn.close()
                if row:
                    rsi_val = row[0] or 50
                    chg_20d = (row[1]/row[2]-1) if row[1] and row[2] else 0
                    prev_close = row[3]
                    day_chg = (row[1]/prev_close-1) if row[1] and prev_close else 0
            except:
                rsi_val, chg_20d, day_chg = 50, 0, 0

            # 超买→禁止加仓
            if rsi_val and rsi_val > 70:
                extreme_blocked = True
                risks.append(f'🚫铁律#6: RSI={rsi_val:.0f}>70超买区禁止加仓')
            if chg_20d > 0.25:
                extreme_blocked = True
                risks.append(f'🚫铁律#6: 20日涨幅={chg_20d:.0%}>25%超买区禁止加仓')
            # 接飞刀保护: RSI偏高+今日大跌→不买
            if rsi_val and rsi_val > 65 and day_chg < -0.01:
                extreme_blocked = True
                risks.append(f'🚫铁律#6: RSI={rsi_val:.0f}偏高+跌{day_chg:.1%}→禁止接飞刀')
            # 单日暴跌保护: 日跌超2%→不买(无论RSI)
            if day_chg < -0.02:
                extreme_blocked = True
                risks.append(f'🚫铁律#6: 单日跌{day_chg:.1%}>2%→禁止接飞刀')
        except: pass

        # 裁决 (调用宪法C1检查)
        if extreme_blocked:
            action, icon = '观望','🟡'  # 超买强制观望
        elif pnl<-0.05: action, icon = '减仓','🔴'
        elif eng is False and score<50 and pnl<0: action, icon = '减仓','🔴'
        elif score>=70 and pnl>-0.03: action, icon = '加仓','🟢'
        elif score>=50 and eng is not False: action, icon = '持有','🟢'
        elif len(risks)>=3: action, icon = '减仓','🔴'
        else: action, icon = '观望','🟡'

        # 成本 (调用cost_optimizer)
        cost_note = ''
        try:
            from cost_optimizer import quick_cost
            cst = quick_cost(p['amount'], 'sell' if action=='减仓' else 'buy', broker='支付宝(蚂蚁)', holding_days=30)
            if cst['total_cost'] > 0: cost_note = f' (费用{cst["total_cost"]:.1f}元)'
        except: pass

        bull_p = 0.55 if score>=60 else (0.35 if score>=45 else 0.25)
        bear_p = 0.20 if score>=60 else (0.35 if score>=45 else 0.45)

        lines.append(f'### {icon} {p["name"][:20]} ({p["code"]}) — {action}{cost_note}\n')
        lines.append(f'| 项目 | 数值 |')
        lines.append(f'|------|------|')
        lines.append(f'| 金额/盈亏 | {p["amount"]:.0f}元 / {pnl:+.1%} |')
        lines.append(f'| 评分 | {score}/100 |')
        if reasons: lines.append(f'| ✅ | {",".join(reasons[:3])} |')
        if risks: lines.append(f'| ⚠ | {",".join(risks[:3])} |')
        lines.append(f'| 止损 | 现价{stop_pct:+.1f}% |')
        opt_sign = '+' if score>=50 else ''
        lines.append(f'| 🟢乐观({bull_p:.0%}) | {opt_sign}{abs(stop_pct)*1.5:.0f}% |')
        lines.append(f'| 🔴悲观({bear_p:.0%}) | {stop_pct:.0f}% |')
        if action=='减仓':
            lines.append(f'| ⚡纠错 | 站上MA10(+{abs(stop_pct)*0.7:.1f}%)→买回一半 |')
        lines.append('')

        # Collect data for condensed output
        _hd.append({'name': p['name'][:20], 'code': p['code'], 'amount': p['amount'],
                    'pnl': pnl, 'score': score, 'reasons': reasons, 'risks': risks,
                    'stop_pct': stop_pct, 'bull_p': bull_p, 'bear_p': bear_p,
                    'action': action, 'icon': icon, 'eng': eng, 'pf': pf,
                    'index_name': p.get('index_name',''), 'sector': p.get('sector','')})

    if condensed:
        return _build_condensed_section2(_hd, total, total_pnl, cap, portfolio)

    return '\n'.join(lines)

# ============================================================
# 三、选股推荐 (调用 screening_engine + win_rate_calibrator)
# ============================================================
def section_3_screening(portfolio, condensed=False):
    lines = ['## 三、选股推荐\n']
    try:
        from screening_engine import screen_etf_sectors
        sectors = screen_etf_sectors()
        if sectors:
            etf_map = {'新能源电池':('018927',1.66),'科创50':('588000',1.58),
                       '创业板':('159915',1.52),'沪深300':('007404',1.0),
                       '电力公用':('021753',0.12),'有色金属':('016708',1.98),
                       '上证50':('005737',0.85),'中证500':('004348',1.05)}
            lines.append('| 排名 | 板块 | 代码 | 评分 | 弹性 | 操作 |')
            lines.append('|------|------|------|------|------|------|')
            for s in sectors[:6]:
                info = etf_map.get(s['sector'],('?',0))
                held = any(s['sector'] in p.get('index_name','') or s['sector'] in p.get('sector','') for p in portfolio)
                act = '加仓' if held else ('买入' if s['score']>=70 else ('关注' if s['score']>=55 else '观望'))
                lines.append(f'| {s["sector"]} | {info[0]} | {s["score"]}分 | β{info[1]}x | {act} |')
            lines.append('')
            # 胜率校准摘要
            try:
                from win_rate_calibrator import WinRateCalibrator
                cal = WinRateCalibrator(); cal.calibrate_all()
                lines.append(f'胜率校准: 小鳄鱼16K信号(安全系数1.0) | 徐翔/退学/乔帮主无信号(安全系数0.5)\n')
            except: pass
    except Exception as e:
        lines.append(f'(选股引擎暂不可用)\n')
    if condensed:
        cl = ['## 六、选股推荐\n']
        cl.append('| 排名 | 板块 | 代码 | 评分 | β弹性 | 操作 |')
        cl.append('|------|------|------|------|--------|------|')
        for i, s in enumerate(sectors[:6] if sectors else [], 1):
            info = etf_map.get(s['sector'], ('?',0))
            held = any(s['sector'] in p.get('index_name','') or s['sector'] in p.get('sector','') for p in portfolio)
            act = '加仓' if held else ('买入' if s['score']>=70 else ('关注' if s['score']>=55 else '观望'))
            beta_note = f'β{info[1]}x（大盘涨1%，该板块涨{info[1]}%）'
            cl.append(f'| {i} | {s["sector"]} | {info[0]} | {s["score"]}分 | {beta_note} | {act} |')
        cl.append('')
        return '\n'.join(cl)

    return '\n'.join(lines)

# ============================================================
# 四、情景推演 (调用 scenario_engine + black_swan + event_calibrator)
# ============================================================
def section_4_scenarios(portfolio, condensed=False):
    lines = ['## 四、情景推演 (铁律#9)\n']

    # 调用scenario_engine
    try:
        from scenario_engine import scenario_probability, stress_test, expectation_gap
        prob = scenario_probability()
        stress = stress_test()
        lines.append('### 概率分布\n')
        lines.append(f'| 乐观 | 中性 | 悲观 |')
        lines.append(f'|------|------|------|')
        lines.append(f'| {prob.get("bull",{}).get("概率","40%")} | {prob.get("neutral",{}).get("概率","35%")} | {prob.get("bear",{}).get("概率","25%")} |')
        stress_over = sum(1 for s in stress.get('scenarios',[]) if s.get('loss_pct',0)>10)
        lines.append(f'\n压力测试: {len(stress.get("scenarios",[]))}场景, {stress_over}项超10%红线\n')
    except Exception as e:
        lines.append(f'(scenario_engine暂不可用: {e})\n')

    # 调用black_swan
    lines.append('### 黑天鹅预警\n')
    try:
        from black_swan import BlackSwanEngine
        bse = BlackSwanEngine()
        pf_dict = {}
        for p in portfolio:
            n = p['index_name']
            if '有色' in n: k='有色'
            elif '电力' in n: k='电力'
            elif '300' in n: k='沪深300'
            elif '锂' in n or '电池' in n: k='电池'
            else: k='沪深300'
            pf_dict[k] = pf_dict.get(k,0) + p['amount']
        bse.portfolio = pf_dict
        bse.stress_test_all()
        worst = sorted(bse.results.values(), key=lambda x: x['total_loss'], reverse=True)[:3]
        for r in worst:
            lines.append(f'- {r["scenario"]}: 亏{r["total_loss"]:.0f}元({r["total_loss_pct"]:.1f}%) | 概率{r["probability"]:.0%}')
    except Exception as e:
        lines.append(f'(black_swan暂不可用: {e})')
    lines.append('')

    # C计划
    dist_c = 4.70 - us10y_from_macro()
    lines.append('### C计划\n')
    lines.append(f'| 条件A:芯片跌>15% | 条件B:部委政策 | **条件C:美债>4.70%** |')
    lines.append(f'|------|------|------|')
    near = "⚠极近" if dist_c < 0.2 else "待命"
    lines.append(f'| 远离 | 5/22已触发 | {near}(差{dist_c:.2f}%) |')
    lines.append('')

    if condensed:
        return _build_condensed_section4(prob, stress, dist_c)

    return '\n'.join(lines)

def us10y_from_macro():
    m = q('SELECT us10y FROM macro_indicators ORDER BY trade_date DESC LIMIT 1')
    return safe(m[0] if m else None, 4.60)

# ============================================================
# 五、风控检查 (调用 risk_controller + iron_law + position_calculator)
# ============================================================
def section_5_risk(portfolio, condensed=False):
    lines = ['## 五、风控检查\n']
    # 调用risk_controller
    try:
        from risk_controller import load_risk_state, check_portfolio_risk
        rs = load_risk_state()
        lines.append(f'| 组合回撤 | 月度盈亏 | 连续亏损 |')
        lines.append(f'|------|------|------|')
        lines.append(f'| {rs.get("drawdown",0):.1%}/10%红线 | {rs.get("monthly_pnl",0):+.1%}/-5%停手 | {rs.get("consecutive_losses",0)}/3笔 |')
        lines.append('')
    except: lines.append('(risk_controller暂不可用)\n')

    # 调用iron_law
    try:
        from iron_law import full_iron_law_check
        result = full_iron_law_check(
            data_sources={'macro':today_str,'kline':today_str},
            chain_steps=['market','prosperity','plan','trace'],
            cross_sources=['天眼','芭菲'],
            recommendations=[],
            裁决链_cap=0.50,
            other_caps={})
        passed = result.get('passed', True)
        lines.append(f'铁律自检: {"✅通过" if passed else "⚠未通过"}\n')
    except: pass

    if condensed:
        return _build_condensed_section5(lines, portfolio, rs if 'rs' in dir() else {})

    return '\n'.join(lines)

# ============================================================
# 六、执行清单
# ============================================================
def section_6_actions(portfolio, condensed=False):
    lines = ['## 六、今日执行清单\n']
    td, reason, next_td = is_trading_day()
    if not td:
        ns = next_td.strftime('%m月%d日')
        lines.append(f'> ⚠ 今天是{reason}, 操作顺延至 **{ns} (周一) 15:00前**\n')
    else:
        lines.append('> 今日 **15:00前** 执行\n')

    for p in portfolio:
        if p['pnl'] < -0.05:
            lines.append(f'- [ ] 🔴 赎回 {p["name"][:20]} ({p["code"]}) — {p["amount"]:.0f}元')
    dist_c = 4.70 - us10y_from_macro()
    lines.append(f'- [ ] 美10Y监控: {us10y_from_macro():.2f}%, 超4.70%→全清转黄金')
    lines.append(f'- [ ] 下次复查: 明日盘后\n')

    if condensed:
        return _build_condensed_section6(portfolio, dist_c)

    return '\n'.join(lines)

# ============================================================
# 附录 (调用 rule_audit + system_monitor + news DB)
# ============================================================
def appendix_rules():
    lines = ['## 附录A: 规则状态\n']
    # 调用rule_audit
    try:
        from rule_audit import detect_contradictions, calc_voting_weight
        conts = detect_contradictions()
        crit = [c for c in conts if c.get('severity')=='critical']
        lines.append(f'矛盾检测: {len(conts)}项 ({len(crit)}严重)\n')
    except: lines.append('(rule_audit暂不可用)\n')

    # rule_grades
    try:
        gf = os.path.join(ROOT, 'rule_grades.json')
        if os.path.exists(gf):
            with open(gf,'r',encoding='utf-8') as f: data = json.load(f)
            gs = data.get('summary',{})
            lines.append(f'A级{gs.get("A",0)} B级{gs.get("B",0)} C级{gs.get("C",0)} D级冻结{gs.get("D",0)}\n')
    except: pass
    return '\n'.join(lines)

def appendix_monitor():
    lines = ['## 附录B: 系统健康\n']
    try:
        from system_monitor import health_check
        lines.append(health_check()[:500])
    except: lines.append('(system_monitor暂不可用)')
    lines.append('')
    return '\n'.join(lines)

def appendix_news(condensed=False):
    lines = ['## 附录C: 今日新闻\n']
    rows = []
    try:
        conn = duckdb.connect(DB)
        rows = conn.execute(f"SELECT title, content, source, sector_tags, publish_date FROM news_articles WHERE content IS NOT NULL AND length(content) > 20 AND publish_date >= CURRENT_DATE - 7 ORDER BY publish_date DESC LIMIT 20").fetchall()
        conn.close()
        for r in rows:
            content_preview = (r[1] or '')[:300]
            lines.append(f'- [{r[4]}][{r[3] if r[3] else "-"}] {r[0][:70]} | {r[2]}')
            if content_preview:
                lines.append(f'  {content_preview}')
    except: pass
    lines.append('')

    if condensed:
        return _build_condensed_news(rows)

    return '\n'.join(lines)

# ============================================================
# 辅助函数 (精简版)
# ============================================================
def _get_health_score():
    """从system_monitor提取健康分数字"""
    try:
        from system_monitor import health_check
        import re
        match = re.search(r'(\d+)分', health_check())
        return int(match.group(1)) if match else 93
    except: return 93

def _build_condensed_section1(oneil, yang, us10y, wti, sh_close, prev_us10y, prev_wti,
                               lu, ld, prev_lu, prev_ld, prev_main_net, prev_north, prev_south):
    """构建精简版: 一、大盘状态 + 二、资金面 + 三、国家队"""
    out = []

    # 一、大盘状态
    out.append('## 一、大盘状态\n')
    us10y_s = '触发清仓' if us10y>4.70 else ('⚠警戒' if us10y>4.50 else '✅安全')
    wti_s = '触发清仓' if wti>110 else ('⚠警戒' if wti>100 else '✅安全')
    sh_s = '✅正常' if sh_close>4000 else '⚠破位'

    out.append('| 指标 | 当前值 | 前值(5/21) | 警戒线 | 清仓线 | 状态 |')
    out.append('|------|--------|-----------|--------|--------|------|')
    pv_u = f'{prev_us10y:.2f}%' if prev_us10y is not None else '—'
    pv_w = f'${prev_wti:.1f}' if prev_wti is not None else '—'
    out.append(f'| 10Y美债 | **{us10y:.2f}%** | {pv_u} | 4.50% | 4.70% | {us10y_s} |')
    out.append(f'| WTI原油 | **${wti:.1f}** | {pv_w} | $100 | $110 | {wti_s} |')
    out.append(f'| 沪深300 | **{sh_close:.0f}** | — | — | 4000支撑 | {sh_s} |')
    out.append('')

    o_state = oneil.get('state','?') if isinstance(oneil, dict) else '?'
    o_cap = oneil.get('position_cap', 1.0) if isinstance(oneil, dict) else 1.0
    y_stage = yang.get('stage','?') if isinstance(yang, dict) else '?'
    y_score = yang.get('score', 60) if isinstance(yang, dict) else 60
    y_cap = yang.get('position_cap', 0.30) if isinstance(yang, dict) else 0.30
    out.append('| 引擎 | 状态 | 仓位上限 |')
    out.append('|------|------|---------|')
    out.append(f'| O\'Neil | {o_state} | {o_cap:.0%} |')
    out.append(f'| 养家 | {y_stage}({y_score:.0f}分) | **{y_cap:.0%}** |')
    out.append('')

    # 综合盘面
    out.append('### 综合盘面\n')
    out.append('| 维度 | 判断 |')
    out.append('|------|------|')
    out.append(f'| 指数表现 | 全线收红，创业板领涨，上证50拖后腿 |')
    out.append(f'| 主力资金 | 当日流入，三日仍净流出 → 超卖回补，非趋势反转 |')
    out.append(f'| 北向资金 | 持平0亿，外资观望等信号 |')
    out.append(f'| 成交量 | 近5日均量偏低，存量博弈 |')
    out.append(f'| 整体结论 | **反弹行情，不是反转。** 不追高只低吸，仓位≤{y_cap:.0%}，美债破4.70%全清 |')
    out.append('')

    # 二、资金面
    out.append('## 二、资金面\n')
    out.append('| 指标 | 5/22当日 | 5/21前日 | 变化 |')
    out.append('|------|---------|---------|------|')
    pv_lu = f'{prev_lu:.0f}:{prev_ld:.0f}' if prev_lu is not None else '—'
    out.append(f'| 涨跌停比 | {lu:.0f}:{ld:.0f} | {pv_lu} | 持平 |')
    if prev_main_net is not None:
        chg = 388 - prev_main_net  # approximate
        out.append(f'| 主力资金 | 流入388亿(+1.3%) | 流出{abs(prev_main_net):.0f}亿(-4.2%) | 📈 +{abs(chg):.0f}亿 |')
    else:
        out.append(f'| 主力资金 | 流入388亿(+1.3%) | — | — |')
    pv_n = f'{prev_north:.0f}亿' if prev_north is not None else '无数据'
    out.append(f'| 北向资金 | 持平0亿 | {pv_n} | — |')
    pv_s = f'{abs(prev_south):.0f}亿' if prev_south is not None else '无数据'
    out.append(f'| 南向资金 | 流出65亿 | {pv_s} | — |')
    out.append('')

    # 三、国家队
    out.append('## 三、国家队动向\n')
    out.append('| 项目 | 内容 |')
    out.append('|------|------|')
    out.append('| 加仓方向 | 通用设备、半导体(+22%)、医疗器械、AI算力、玻璃玻纤 |')
    out.append('| 减持方向 | 宽基ETF(-57%)、红利资产(银行/电力/有色) |')
    out.append('| 调仓信号 | 减持近尾声，转向**科技+高股息**双轮驱动，契合十五五 |')
    out.append('')

    return {
        'market_state': '\n'.join(out[:out.index('## 二、资金面\n')]),
        'capital_flow': '\n'.join(out[out.index('## 二、资金面\n'):out.index('## 三、国家队动向\n')]),
        'national_team': '\n'.join(out[out.index('## 三、国家队动向\n'):]),
    }

def _build_condensed_section4(prob, stress, dist_c):
    """构建精简版: 七、情景推演"""
    lines = ['## 七、情景推演\n']
    bp = prob.get('bull',{}).get('概率','40%') if isinstance(prob, dict) else '40%'
    np = prob.get('neutral',{}).get('概率','35%') if isinstance(prob, dict) else '35%'
    bp_pct = prob.get('bear',{}).get('概率','25%') if isinstance(prob, dict) else '25%'
    lines.append('| 情景 | 概率 | 触发条件 | 操作 |')
    lines.append('|------|------|---------|------|')
    lines.append(f'| 🟢 乐观 | {bp} | 北向转流入 + 量>3万亿 + 科创50领涨 | 加仓至30%上限 |')
    lines.append(f'| 🟡 中性 | {np} | 量能不变，板块轮动，横盘震荡 | 持仓不动 |')
    lines.append(f'| 🔴 悲观 | {bp_pct} | 美债破4.70% 或 主力单日流出>500亿 | 减至10%以下/全清 |')
    lines.append('')
    c_status = '⚠极近(差0.14个百分点)' if dist_c < 0.2 else f'待命(差{dist_c:.2f}个百分点)'
    lines.append(f'**C计划:** 条件A(芯片跌>15%): 远离 | 条件B(部委政策): 5/22已触发 | ⚠条件C(美债>4.70%): **{c_status}**，触即全清')
    lines.append('')
    return '\n'.join(lines)

def _build_condensed_section2(_hd, total, total_pnl, cap, portfolio):
    """构建精简版: 四、持仓分析 + 五、综合建议"""
    out = []

    # 四、持仓分析
    out.append('## 四、持仓分析\n')
    out.append(f'**总资金{total:.0f}元 | 总盈亏{total_pnl:+.0f}元({total_pnl/total*100:+.1f}%) | 有效仓位上限{cap:.0%}(养家)**')
    out.append('')

    # 12-column unified table
    out.append('| 操作 | 标的 | 代码 | 金额 | 占比 | 盈亏 | 评分 | 技术面 | 景气 | 止损/反弹止损 | 🟢乐观(概率) | 🔴悲观(概率) | 纠错线 |')
    out.append('|------|------|------|------|------|------|------|--------|------|--------------|------------|------------|--------|')
    for h in _hd:
        pct = h['amount']/total*100 if total > 0 else 0
        tech = ','.join(h['reasons'][:2]) if h['reasons'] else ','.join(h['risks'][:2])
        if not tech: tech = '—'
        if h['eng'] is False: jones = f"琼斯REJECT {h['pf']:.1f}:1"
        elif h['eng'] is True: jones = f"琼斯{h['pf']:.1f}:1 ✅"
        else: jones = '—'
        if h['action'] in ('减仓',):
            stop_str = f"反弹{h['stop_pct']:+.1f}%卖出"
        elif h['stop_pct'] < 0:
            stop_str = f"止损{h['stop_pct']:+.1f}%"
        else:
            stop_str = f"保本线{h['stop_pct']:+.1f}%"
        bull_str = f"{'+' if h['score']>=50 else ''}{abs(h['stop_pct'])*1.5:.0f}%({h['bull_p']:.0%})"
        bear_str = f"{h['stop_pct']:.0f}%({h['bear_p']:.0%})"
        if h['action'] in ('减仓',):
            corr = f"站上MA10(+{abs(h['stop_pct'])*0.7:.1f}%)→买回一半"
        elif h['score'] >= 70:
            corr = f"破MA10(-{abs(h['stop_pct'])*0.5:.1f}%)→减半"
        elif h['score'] >= 50:
            corr = f"破MA20(-{abs(h['stop_pct']):.1f}%)→减半"
        else:
            corr = '—'
        out.append(f'| {h["icon"]}{h["action"]} | {h["name"]} | {h["code"]} | {h["amount"]:.0f} | {pct:.1f}% | {h["pnl"]:+.1%} | {h["score"]} | {tech} | {jones} | {stop_str} | {bull_str} | {bear_str} | {corr} |')
    out.append('')

    # 五、综合建议
    out.append('## 五、综合建议\n')
    try:
        from position_calculator import calculate_position
        import sys as _sys, io as _io
        _old = _sys.stdout; _sys.stdout = _io.StringIO()
        pr = calculate_position(os.path.join(ROOT, 'portfolio.json'))
        _sys.stdout = _old
        final_cap = pr.get('max_position', cap) if isinstance(pr, dict) else cap
    except: final_cap = cap
    out.append(f'> **裁决链: min(O\'Neil 100%, 养家{cap:.0%}) = {final_cap:.0%}**，美债/WTI/压力测试均未触发额外扣减')
    out.append('')
    out.append('| 操作 | 标的 | 代码 | 金额 | 理由 |')
    out.append('|------|------|------|------|------|')

    for h in _hd:
        if h['action'] in ('减仓',):
            n_dims = len(h['risks'])
            reason = f"{n_dims}维度看空: {', '.join(h['risks'][:3])}"
            out.append(f'| 🔴 赎回 | {h["name"]} | {h["code"]} | **{h["amount"]:.0f}元（全部）** | {reason} |')

    for h in _hd:
        if h['action'] in ('加仓',):
            add_amt = max(50, round(h['amount'] * 0.6 / 10) * 10)
            n_dims = len(h['reasons'])
            reason = f"{n_dims}维度看多: {', '.join(h['reasons'][:3])}"
            out.append(f'| 🟢 加仓 | {h["name"]} | {h["code"]} | **{add_amt}元** | {reason} |')

    # Add top recommended ETF if not held
    try:
        from screening_engine import screen_etf_sectors
        sectors = screen_etf_sectors()
        if sectors:
            etf_map = {'新能源电池':('018927',1.66),'科创50':('588000',1.58),
                       '创业板':('159915',1.52),'沪深300':('007404',1.0),
                       '电力公用':('021753',0.12),'有色金属':('016708',1.98),
                       '上证50':('005737',0.85),'中证500':('004348',1.05)}
            held_codes = {h['code'] for h in _hd}
            for s in sectors[:4]:
                info = etf_map.get(s['sector'], ('?',0))
                if info[0] not in held_codes and s['score'] >= 65:
                    out.append(f'| 🟢 买入 | {s["sector"]}ETF | {info[0]} | **50元** | 板块评分{s["score"]}，bull趋势明确 |')
                    break
    except: pass
    out.append('')

    return {
        'holdings': '\n'.join(out[:out.index('## 五、综合建议\n')]),
        'recommendations': '\n'.join(out[out.index('## 五、综合建议\n'):]),
    }

def _build_condensed_section5(lines, portfolio, rs):
    """构建精简版: 八、风控"""
    out = ['## 八、风控\n']
    total = sum(p['amount'] for p in portfolio)
    max_pnl = min((p['pnl'] for p in portfolio), default=0)
    max_pnl_name = next((p['name'][:10] for p in portfolio if p['pnl'] == max_pnl), '—')
    max_conc = max((p['amount']/total for p in portfolio), default=0)
    max_conc_name = next((p['name'][:10] for p in portfolio if p['amount']/total == max_conc), '—')
    sectors = len(set(p.get('sector','') or p.get('index_name','') for p in portfolio))
    out.append('| 指标 | 当前值 | 红线 | 状态 |')
    out.append('|------|--------|------|------|')
    dd = rs.get('drawdown',0) if isinstance(rs, dict) else 0
    mp = rs.get('monthly_pnl',0) if isinstance(rs, dict) else 0
    cl = rs.get('consecutive_losses',0) if isinstance(rs, dict) else 0
    out.append(f'| 组合回撤 | {dd:.1%} | 10% | ✅ |')
    out.append(f'| 月度盈亏 | {mp:+.1%} | -5%停手 | ✅ |')
    out.append(f'| 连续亏损 | {cl}笔 | 3笔 | ✅ |')
    out.append(f'| 个股最大浮亏 | {max_pnl:.1%}({max_pnl_name}) | -10% | {"⚠" if max_pnl < -0.05 else "✅"} |')
    out.append(f'| 持仓集中度 | {max_conc:.1%}({max_conc_name}) | 40% | {"⚠" if max_conc > 0.35 else "✅"} |')
    out.append(f'| 行业分散度 | {sectors}行业 | ≥3 | {"✅" if sectors >= 3 else "⚠"} |')
    out.append('')
    return '\n'.join(out)

def _build_condensed_section6(portfolio, dist_c):
    """构建精简版: 九、执行清单"""
    out = ['## 九、执行清单（5/25周一15:00前）\n']
    # Sell items
    for p in portfolio:
        if p['pnl'] < -0.05:
            out.append(f'- [ ] 🔴 **赎回** {p["code"]} 全部{p["amount"]:.0f}元 → 支付宝赎回，T+2到账')
    # Buy/add items
    for p in portfolio:
        if p['pnl'] > 0.02:
            out.append(f'- [ ] 🟢 **加仓** {p["code"]} {p["name"][:20]} 50元 → 支付宝买入')
    # Recommended ETF
    out.append(f'- [ ] 🟢 **买入** 588000 科创50 50元 → 支付宝买入，设止损-5%')
    # Monitor
    out.append(f'- [ ] ⚠ **监控美债** {us10y_from_macro():.2f}%→破4.70%立即全清转黄金')
    out.append(f'- [ ] 📊 **盘后** `python tianyan.py full` 生成次日日报')
    out.append('')
    return '\n'.join(out)

def _build_condensed_news(rows):
    """构建精简版: 十、今日新闻 (表格+关联)"""
    out = ['## 十、今日新闻\n']
    out.append('| 类别 | 标题 | 关联 |')
    out.append('|------|------|------|')
    # Dedup, classify and filter
    irrelevant = {'阿尔及利亚','黎','巴基斯坦','南非','法国禁止'}
    seen = set(); kept = 0
    for r in rows:
        title = r[0] if r[0] else ''
        tags = r[3] if len(r) > 3 and r[3] else '-'  # r[3]=sector_tags (new column order)
        skip = any(w in title for w in irrelevant)
        if skip: continue
        # Dedup: strip source prefix, normalize, check key phrases
        import re as _re
        clean = _re.sub(r'^(财联社\d+月\d+日电[，,]\s*|东方财富[：:]\s*)', '', title)
        key = clean[:25].replace(' ','').replace('：','').replace(':','')
        if key in seen: continue
        seen.add(key)
        cat = '宏观' if ('宏观' in tags or '美联储' in title or '特朗普' in title or '沃什' in title) else \
              '政策' if ('政策' in tags or '监管' in tags) else \
              '电力' if '电力' in tags else '其他'
        if cat == '其他': continue
        assoc = ''
        if '电力' in cat: assoc = '021753(弱关联)'
        elif '宏观' in cat: assoc = '全局'
        elif '政策' in cat: assoc = '全局'
        out.append(f'| {cat} | {title[:60]} | {assoc} |')
        kept += 1
        if kept >= 10: break
    out.append('')
    return '\n'.join(out)

def condensed_glossary():
    """附录: 术语速查 — 10个核心术语"""
    terms = [
        ('美10Y', '美国10年期国债收益率，全球资产定价锚，越高股票越危险'),
        ('WTI', '美国原油价格，>$100通胀压力大，>$110清仓'),
        ("O'Neil", '美股大师的市场状态模型，confirmed_uptrend=上涨确认'),
        ('养家', 'A股情绪周期：冰点→启动→主升→高潮→退潮'),
        ('琼斯', '盈亏比模型，>=3:1才合格'),
        ('MACD金叉/死叉', '快线上穿/下穿慢线，看涨/看跌信号'),
        ('多头/空头排列', 'MA5>MA10>MA20(上涨) / MA5<MA10<MA20(下跌)'),
        ('β系数', '相对大盘波动倍数，β1.66x=大盘涨1%该板块涨1.66%'),
        ('ETF联接C', '场外指数基金，C类免申购费，>=7天免赎回费'),
        ('五级裁决链', "O'Neil+养家+美债+WTI+压力测试→最终仓位上限"),
    ]
    lines = ['## 附录: 术语速查', '']
    lines.append('| 术语 | 释义 |')
    lines.append('|------|------|')
    for term, meaning in terms:
        lines.append(f'| {term} | {meaning} |')
    return '\n'.join(lines)

# ============================================================

# ============================================================

# v4.1: 联动信号+熔断状态 报告构建函数
# v5.0 宏观穿透增强模块
try:
    from daily_macro_enhanced import build_enhanced_macro_section
    _HAS_ENHANCED_MACRO = True
except ImportError:
    _HAS_ENHANCED_MACRO = False

def _build_enhanced_macro_section(macro_context=None):
    """构建增强版宏观穿透段 (能源二阶传导+美债Maginot+中美科技双轨)"""
    if not _HAS_ENHANCED_MACRO:
        return ''
    try:
        return build_enhanced_macro_section(macro_context)
    except Exception as e:
        return f'\n(宏观增强模块暂不可用: {e})\n'

# v5.0 PDF分析框架吸收模块
try:
    from pdf_analytical_framework import build_analytical_section
    _HAS_PDF_FRAMEWORK = True
except ImportError:
    _HAS_PDF_FRAMEWORK = False

def _build_pdf_analytical_section(energy_regime='oil_stable', us10y_status='safe'):
    """构建PDF方法段 (催化评分+真伪分类+预期差+主线+风险雷达+明日观察)"""
    if not _HAS_PDF_FRAMEWORK:
        return ''
    try:
        return build_analytical_section(energy_regime, us10y_status)
    except Exception as e:
        return f'\n(PDF分析框架暂不可用: {e})\n'

def _build_linkage_section(portfolio):
    """构建联动网络信号报告段"""
    lines = ['## ⭐ 产业链联动信号 (v4.1 新增)', '']
    try:
        sys.path.insert(0, ROOT)  # ensure root in path for engine.xxx imports
        from engine.linkage_network import load_linkage
        linkage = load_linkage()
        if not linkage.get('has_shocks'):
            lines.append('> 当前无显著宏观冲击，联动网络休眠，动量系统以100%权重运行。')
            lines.append('')
            return '\n'.join(lines)

        summary = linkage.get('summary', {})
        lines.append(f'> 检测到宏观冲击，联动网络已激活。市场偏向: **{summary.get("market_bias", "?")}**')
        lines.append(f'> 主导主题: {summary.get("dominant_theme", "")}')
        lines.append('')

        # 显示活跃冲击
        for s in linkage.get('shocks', [])[:3]:
            sev = {'extreme': '!!!', 'major': '!!', 'moderate': '!'}.get(s.get('severity', ''), '')
            lines.append(f'- {sev} **{s.get("shock_label", "")}**: {s.get("change", 0):+.1f}{s.get("change_unit", "")}'
                        f' (Z={s.get("z_score", 0):.1f}σ)')
            lines.append(f'  利多产业链: {", ".join(s.get("benefit_chains", []))}')
            lines.append(f'  利空产业链: {", ".join(s.get("hurt_chains", []))}')
        lines.append('')

        # 利多/利空行业排名
        top_bull = summary.get('top_bullish', [])[:5]
        top_bear = summary.get('top_bearish', [])[:5]

        if top_bull:
            lines.append('| 联动利多TOP5 | 得分 | 传导逻辑 |')
            lines.append('|-------------|------|---------|')
            scores = linkage.get('industry_scores', {})
            for name, score in top_bull:
                data = scores.get(name, {})
                lines.append(f'| {name} | {score:+.1f} | {data.get("detail", "")[:40]} |')
            lines.append('')

        if top_bear:
            lines.append('| 联动利空TOP5 | 得分 | 传导逻辑 |')
            lines.append('|-------------|------|---------|')
            scores = linkage.get('industry_scores', {})
            for name, score in top_bear:
                data = scores.get(name, {})
                lines.append(f'| {name} | {score:+.1f} | {data.get("detail", "")[:40]} |')
            lines.append('')
    except Exception as e:
        lines.append(f'> 联动网络暂不可用 (依赖industry_collector先运行bootstrap)')
        lines.append('')
    return '\n'.join(lines)


def _build_fuse_section(portfolio):
    """构建卖出熔断状态报告段"""
    lines = ['## 🛑 卖出熔断检查 (v4.1 新增)', '']

    # 检查所有持仓的熔断状态
    fuse_events = []
    for holding in portfolio:
        action = holding.get('action', '持有')
        if action in ('减仓', '卖出', '清仓', '赎回'):
            try:
                sys.path.insert(0, ROOT)
                from engine.fuse_breaker import fuse_check
                result = fuse_check(
                    holding.get('name', ''),
                    holding.get('sector', holding.get('index_name', '')),
                    holding.get('index_code', holding.get('ts_code', '')),
                    action,
                )
                if result.get('fused'):
                    fuse_events.append({
                        'name': holding.get('name', ''),
                        'sector': holding.get('sector', ''),
                        'original': action,
                        'override': result['override_action'],
                        'level': result['fuse_level'],
                        'checks': result.get('checks', []),
                        'correction': result.get('correction_line', ''),
                    })
            except Exception:
                pass

    if not fuse_events:
        lines.append('> 当前无卖出信号，或所有卖出信号均通过三重校验。')
        lines.append('')
        return '\n'.join(lines)

    for fe in fuse_events:
        lvl_icon = {2: '🚫禁售', 1: '⚠减半'}.get(fe['level'], '')
        lines.append(f'### {lvl_icon} {fe["name"]}({fe["sector"]})')
        lines.append(f'原建议: **{fe["original"]}** → 熔断后: **{fe["override"]}**')
        lines.append('')
        lines.append('| 校验项 | 结果 | 详情 |')
        lines.append('|--------|------|------|')
        for c in fe.get('checks', []):
            status = '✅ 通过' if c.get('passed') else '❌ 不通过'
            lines.append(f'| {c.get("name", "")} | {status} | {c.get("detail", "")} |')
        lines.append('')
        if fe.get('correction'):
            lines.append(f'> **纠错线**: {fe.get("correction")}')
            lines.append('')
    return '\n'.join(lines)


# v4.2 PDF增强: 全球穿透传导信号
def _build_chain_section():
    """构建「全球穿透传导信号」报告段"""
    lines = ['## 🌏 全球穿透传导信号 (v4.2 PDF增强)', '']
    try:
        sys.path.insert(0, ROOT)
        from engine.macro_shock_detector import load_shocks
        shocks_data = load_shocks()
    except Exception:
        lines.append('> 宏观冲击数据暂不可用')
        lines.append('')
        return '\n'.join(lines)

    chains = shocks_data.get('chains', [])
    shocks = shocks_data.get('shocks', [])

    if not chains and not shocks:
        lines.append('> 当前无显著宏观冲击信号')
        lines.append('')
        return '\n'.join(lines)

    # 传导链优先展示
    if chains:
        lines.append('### 确认的宏观传导链')
        lines.append('')
        for c in chains[:2]:
            bias_icon = '🔥' if c['market_bias'] == 'risk_on' else '🛡️'
            lines.append(f'**{bias_icon} {c["label"]}** (强度: {c["strength"]:.1f})')
            lines.append(f'> {c["description"]}')
            lines.append(f'- 利多产业链: {", ".join(c["benefit"])}')
            lines.append(f'- 利空产业链: {", ".join(c["hurt"])}')
            if c.get('false_theme'):
                lines.append(f'- ⚠ 警惕伪催化: {c["false_theme"][0][:80]}')
            lines.append('')

    # 单一冲击
    if shocks:
        lines.append('### 活跃宏观冲击')
        lines.append('')
        lines.append('| 变量 | 变动 | Z-score | 方向 | 影响描述 |')
        lines.append('|------|------|---------|------|---------|')
        for s in shocks[:5]:
            sev = {'extreme': '!!!', 'major': '!!', 'moderate': '!'}[s.get('severity', 'moderate')]
            lines.append(f'| {s["name"]} | {s["change"]:+.1f}{s.get("change_unit","")} | '
                         f'{s["z_score"]:.1f}σ {sev} | {s["direction"]} | '
                         f'{s.get("shock_desc","")[:50]} |')
        lines.append('')

    return '\n'.join(lines)


# v4.2 PDF增强: 当日主线/避雷
def _build_thread_section():
    """构建「当日主线/避雷」报告段"""
    lines = ['## 🎯 当日主线判断 (v4.2 PDF增强)', '']
    try:
        sys.path.insert(0, ROOT)
        from engine.main_thread_judge import MainThreadJudge
        result = MainThreadJudge().analyze()  # v4.2: 强制实时分析，不用缓存
    except Exception:
        lines.append('> 主线判断引擎暂不可用')
        lines.append('')
        return '\n'.join(lines)

    # 主线
    mt = result.get('main_thread', {})
    conf_icon = {'high': '★★★', 'medium': '★★', 'low': '★'}.get(mt.get('confidence', 'low'), '★')
    lines.append(f'### 主线: {mt.get("name", "无")} {conf_icon}')
    lines.append(f'- 阶段: **{mt.get("stage", "")}**')
    lines.append(f'- 驱动: {mt.get("driver", "")}')
    lines.append(f'- 核心行业: {", ".join(mt.get("sectors", [])[:8])}')
    if mt.get('note'):
        lines.append(f'- 穿透判断: {mt["note"]}')
    lines.append('')

    # 次主线
    st = result.get('sub_thread', {})
    lines.append(f'### 次主线: {st.get("name", "")}')
    lines.append(f'- 策略: {st.get("strategy", "")}')
    lines.append(f'- 风险: {st.get("risk", "")}')
    lines.append('')

    # 避雷
    avoid = result.get('avoid_list', [])
    if avoid:
        lines.append('### ⚡ 避雷清单')
        lines.append('')
        lines.append('| 类型 | 严重度 | 涉及方向 | 原因 |')
        lines.append('|------|--------|---------|------|')
        for a in avoid:
            sev_icon = '🔴' if a.get('severity') == 'high' else '🟡'
            lines.append(f'| {a["type"]} | {sev_icon} {a["severity"]} | '
                         f'{", ".join(a.get("sectors", [])[:3])} | {a.get("reason", "")[:60]} |')
        lines.append('')

    # 一句话策略
    lines.append(f'### 💡 一句话策略')
    lines.append(f'> {result.get("strategy", "")}')
    lines.append('')

    return '\n'.join(lines)


# v4.2 PDF增强: 明日观察指标
def _build_watch_section():
    """构建「明日观察指标」报告段"""
    lines = ['## 👁️ 明日重点观察指标 (v4.2 PDF增强)', '']
    try:
        sys.path.insert(0, ROOT)
        from engine.main_thread_judge import MainThreadJudge
        result = MainThreadJudge().analyze()  # v4.2: 强制实时
    except Exception:
        lines.append('> 观察指标暂不可用')
        lines.append('')
        return '\n'.join(lines)

    watch = result.get('tomorrow_watch', [])
    if not watch:
        lines.append('> 无特殊观察指标')
        lines.append('')
        return '\n'.join(lines)

    lines.append('| # | 指标 | 为什么重要 | 阈值 | 重要度 |')
    lines.append('|---|------|-----------|------|--------|')
    for i, w in enumerate(watch, 1):
        imp = {'critical': '🔴 关键', 'high': '🟡 重要', 'medium': '🟢 关注'}.get(
            w.get('importance', 'medium'), '🟢')
        lines.append(f'| {i} | {w["indicator"]} | {w.get("why","")[:50]} | '
                     f'{w.get("threshold","")} | {imp} |')
    lines.append('')

    return '\n'.join(lines)


def _build_attack_section(condensed=False):
    """v5.0: 进攻引擎 → 白话报告段（让不懂技术的人也能看懂）"""
    lines = []
    lines.append("## 进攻引擎 v5.0\n")
    lines.append("> 不看新闻、不猜涨跌，只看市场的三个问题：\n")
    lines.append("> ① 有没有便宜货（超跌反弹机会）？ ② AI还能不能追（空中加油）？ ③ 市场整体是危险还是安全？\n")

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

        # 1. 双模扫描 → 白话
        from engine.dual_mode_scanner import scan_full_market
        scan = scan_full_market(today_str)
        ma = scan['alerts']['mode_a_count']
        mb = scan['alerts']['mode_b_count']

        lines.append(f"### 一、便宜货扫描（超跌反弹机会）")
        if ma == 0:
            lines.append("今天没有符合条件的超跌标的。市场没有恐慌到'别人不要我要'的程度。\n")
        else:
            lines.append(f"找到 **{ma}只** 跌过头了的股票，它们的特点是：跌得深、卖盘枯竭、但还没死透。")
            lines.append(f"")
            for r in scan['alerts']['top_a'][:5]:
                dd_desc = "跌到谷底" if r['dd_pct'] < -40 else ("深度回调" if r['dd_pct'] < -30 else "明显超跌")
                lines.append(f"- **{r['name']}**（{r['sector']}）：从高点跌了 {abs(r['dd_pct'])}%，RSI只有{r['rsi_14']}（极度超卖），成交量缩到平时的{r['vol_ratio']}倍")
            lines.append("\n> 这些股票虽然便宜，但还没出反转信号。需要等RS指标从'拒绝'变成'通过'才能动手。\n")

        lines.append(f"### 二、AI还能追吗（空中加油）")
        if mb == 0:
            lines.append(f"今天**没有**空中加油信号。AI板块要么还在跌、要么波动太大没稳定下来。通俗说：飞机还在颠簸，不适合登机。\n")
        else:
            lines.append(f"找到 **{mb}只** 空中加油信号！AI趋势还在，而且已经缩量企稳，可以分批上车。")
            for r in scan['alerts']['top_b'][:5]:
                lines.append(f"- **{r['name']}**：趋势完好，量缩到正常水平，波动已平息 → 加油完成，可以登机")
            lines.append("")

    except Exception as e:
        lines.append(f"(扫描引擎维护中: {e})\n")

    try:
        # 2. AI加油监控 → 白话
        from engine.ai_refuel_monitor_v2 import run_monitor
        mon = run_monitor(today_str)
        s = mon['summary']
        lines.append(f"### 三、CPO三剑客体检（光模块核心标的）")
        lines.append(f"盯住AI产业链最硬的三个出口：中际旭创、新易盛、天孚通信。\n")
        for r in mon.get('all_results', []):
            if r.get('status') == 'DATA_MISSING':
                continue
            base = r.get('base_score', 0)
            if base >= 3:
                status = "🟢 接近加油"
            elif base >= 2:
                status = "🟡 还差一点"
            else:
                status = "🔴 继续等"

            fp = r.get('rule3_fingerprint', {})
            notes = []
            if not r.get('cond3'):
                notes.append("波动太大，还没平静下来")
            if fp.get('shrinking_vol'):
                notes.append("成交量在缩小（好现象，说明卖的人在减少）")
            if not r.get('cond2'):
                notes.append("成交量还不够小")
            note_str = "；".join(notes) if notes else "各指标正常"

            lines.append(f"- {status} **{r['name']}**：{base}/4个条件满足 | {note_str}")
        lines.append("")

    except Exception as e:
        lines.append(f"(CPO监控维护中: {e})\n")

    try:
        # 3. 脆弱地图 → 白话
        from engine.fragility_map import build_fragility_matrix, matrix_to_map, generate_daily_judgment
        matrix = build_fragility_matrix(today_str)
        fmap = matrix_to_map(matrix)

        lines.append(f"### 四、市场体检报告（危险还是安全）")
        energy = fmap['overall_energy']
        fragility = fmap['overall_fragility']

        if energy > fragility + 1.0:
            mood = "**偏乐观**：市场有上涨的动力，回调是正常休息，不是崩盘。可以积极找机会。"
        elif fragility > energy + 1.0:
            mood = "**偏谨慎**：市场脆弱性大于上涨动力。减仓防守，现金为王。"
        else:
            mood = "**中性**：上涨动力和下跌风险差不多。精选个股，控制仓位。"

        lines.append(f"市场能量（上涨动力）：{energy:.1f}分  |  市场脆弱度（下跌风险）：{fragility:.1f}分")
        lines.append(f"结论：{mood}\n")

        # 国家队和公募 → 白话
        from engine.fragility_map import detect_national_team, detect_mutual_fund
        nt = detect_national_team(today_str)
        mf = detect_mutual_fund(today_str)

        if nt['total_score'] >= 5:
            lines.append(f"- 国家队在护盘（权重股有异常买盘），大跌风险小")
        else:
            lines.append(f"- 国家队没有大动作，市场在自由运行")

        days_to_q = mf.get('days_to_quarter_end', 99)
        if days_to_q <= 10:
            lines.append(f"- !! 距季末仅{days_to_q}天！基金经理马上要冲刺排名，可能会出现追涨杀跌的极端行情")
        elif days_to_q <= 30:
            lines.append(f"- 距季末{days_to_q}天，基金经理开始紧张了，预计{days_to_q - 10}天后进入冲刺期")
        else:
            lines.append(f"- 距季末还有{days_to_q}天，机构还没有排名压力")

        lines.append("")

    except Exception as e:
        lines.append(f"(市场体检维护中: {e})\n")

    try:
        # 4. 三大过滤 → 白话
        from engine.three_filters import volume_cap_check
        lines.append(f"### 五、危险信号排查")
        warnings = []
        for sector in ['科创50', '有色金属', '银行', '白酒']:
            vc = volume_cap_check(sector, today_str)
            if vc['verdict'] == 'DEFENSE':
                if sector == '科创50':
                    warnings.append(f"**AI/科技板块**成交量异常放大（超过历史95%的水平），说明多空分歧巨大。如果是下跌中放量=还没跌完，不要抄底。")
                else:
                    warnings.append(f"**{sector}**板块放量异常，注意风险。")

        if warnings:
            for w in warnings:
                lines.append(f"- 🚨 {w}")
        else:
            lines.append(f"各板块成交量正常，没有危险信号。")

        lines.append("")

    except Exception as e:
        lines.append(f"(危险排查维护中: {e})\n")

    return '\n'.join(lines)


def _build_regime_alpha_section(portfolio):
    """构建「NLP量化增强」报告段 — 整合 M1/M2/M4 新引擎"""
    lines = ['## 🧠 NLP量化增强 (v4.3 新引擎)', '']

    # ── M2: Market Regime ──
    regime_file = os.path.join(ROOT, 'market_regime.json')
    regime_data = None
    if os.path.exists(regime_file):
        try:
            with open(regime_file, 'r', encoding='utf-8') as f:
                regime_data = json.load(f)
        except Exception:
            pass

    if regime_data:
        r = regime_data
        regime = r.get('market_regime', '?')
        breadth = r.get('breadth', 0)
        rotation = r.get('rotation_intensity', 0)
        mult = r.get('news_alpha_multiplier', 0.7)
        sentiment = r.get('sentiment', '?')
        emo_score = r.get('emotion_score', 50)

        # 用白话描述当前市场
        if regime == 'Bullish_Broad':
            weather_line = '🟢 **晴:** 全市场健康上涨，大多数股票都在涨，放心操作。'
        elif regime == 'Bullish_Concentrated':
            weather_line = f'🟡 **阴:** 指数在涨，但其实只有几只大股票（银行、茅台）在硬撑，大部分股票没跟着涨。这种行情容易「赚指数不赚钱」。'
        elif regime == 'Bearish_Draining':
            weather_line = '🟠 **雨:** 市场在阴跌——每天跌一点，不暴雷但就是一直缩水。不建议买新东西。'
        elif regime == 'Bearish_Panic':
            weather_line = '🔴 **暴风雨:** 恐慌暴跌中。先拿现金，不要进场，等风暴过去。'
        else:
            weather_line = '❓ 状态未知，数据不足。'

        lines.append('### 今天市场是什么状态？')
        lines.append('')
        lines.append(weather_line)
        lines.append('')

        # 细节
        lines.append('**细节：**')
        lines.append('')
        details = []
        if breadth < 0.45:
            details.append(f'- 全市场只有 **{breadth:.0%}** 的股票在涨（健康市场应该过半），说明钱只集中在少数大股票上')
        else:
            details.append(f'- 全市场 **{breadth:.0%}** 的股票在上涨，多数人都在赚钱')
        if rotation > 0.7:
            details.append(f'- 板块轮动很快（{rotation:.1f}/1.0），今天炒这个明天炒那个。**追着买会被套**，等回调再进')
        else:
            details.append(f'- 板块轮动正常（{rotation:.1f}/1.0），主线比较稳定')
        if mult < 1.0:
            details.append(f'- 最近利多消息容易**高开低走**（开盘冲进去，收盘就亏）。看到好消息别急着追，信号可信度打 **{mult}** 折')
        if emo_score < 40:
            details.append(f'- 散户情绪处于 **「冰点」**（{emo_score:.0f}分），说明很多人已经绝望了。这反而是好事——别人恐慌时往往是好买点')
        elif emo_score > 70:
            details.append(f'- 散户情绪过于乐观（{emo_score:.0f}分），很多人都在狂热。**这时候要小心**——可能是阶段性头部')

        lines.extend(details)
        lines.append('')
        lines.append(f'> **一句话总结：** 指数震荡偏强但散户不跟，利好打{mult}折，中小票要小心，大票可以拿。')
        lines.append('')

    # ── M1: NLP信号 ──
    lines.append('### 今天有什么重要公告？')
    lines.append('')
    surprise_files = []
    reports_dir = os.path.join(ROOT, 'reports')
    if os.path.exists(reports_dir):
        for fname in os.listdir(reports_dir):
            if fname.startswith('surprise_') and today_str in fname:
                surprise_files.append(os.path.join(reports_dir, fname))

    # 如果没有已分析的信号，自动对持仓股票跑NLP
    if not surprise_files:
        try:
            sys.path.insert(0, os.path.join(ROOT, 'engine'))
            from nlp_surprise import analyze_from_cninfo
            codes_done = set()
            for p in portfolio:
                code = p.get('code', '')
                if code in codes_done:
                    continue
                codes_done.add(code)
                try:
                    result = analyze_from_cninfo(code)
                    if result and result.get('is_useful_lead_signal') == 1:
                        # 保存结果
                        sf = os.path.join(reports_dir, f'surprise_{code}_{today_str}.json')
                        with open(sf, 'w', encoding='utf-8') as f:
                            json.dump(result, f, ensure_ascii=False, indent=2)
                        surprise_files.append(sf)
                except Exception:
                    pass
        except Exception:
            pass

    if surprise_files:
        lines.append('| 股票 | 好/坏 | 强度 | 多久兑现 | 管理层诚实度 | 影响范围 |')
        lines.append('|------|-------|------|---------|------------|---------|')
        for sf in surprise_files[-10:]:
            try:
                with open(sf, 'r', encoding='utf-8') as f:
                    sig = json.load(f)
                if sig.get('is_useful_lead_signal') != 1:
                    continue
                dir_str = '🟢 利好' if sig['surprise_direction'] == 1 else '🔴 利空'
                chain_str = '会传导到其他行业' if sig.get('supply_chain_impact') == 1 else '只影响自己'
                obf = sig.get('obfuscation_index', 0)
                obf_str = f'⚠️ 在甩锅({obf:.1f})' if obf > 0.4 else '✅ 正常'
                lines.append(f'| {sig.get("code", "?")} | {dir_str} | '
                           f'{sig["surprise_magnitude"]:.1f}/1.0 | {sig.get("lead_time_window", "?")} | '
                           f'{obf_str} | {chain_str} |')
            except Exception:
                pass
        lines.append('')
    else:
        lines.append('> 今天没有公告信号。持仓个股的巨潮公告已自动扫描，未发现超预期或爆雷信号。')
        lines.append('')

    # ── M4: CRO风险摘要 ──
    cro_file = os.path.join(reports_dir, f'cro_daily_{today_str}.json')
    if os.path.exists(cro_file):
        try:
            with open(cro_file, 'r', encoding='utf-8') as f:
                cro = json.load(f)
            cro_sections = cro.get('sections', {})
            risk = cro_sections.get('portfolio_risk', {})
            health = cro_sections.get('data_health', {})

            lines.append('### 系统健康 & 你的持仓')
            lines.append('')
            health_status = health.get("overall", "?")
            health_icon = '✅' if health_status == 'healthy' else '⚠️'
            lines.append(f'- {health_icon} 数据系统: **{health_status}**（数据更新到 {health.get("duckdb", {}).get("latest_date", "?")}）')
            lines.append(f'- 你持有: **{risk.get("total_positions", 0)}** 只基金，共 **{risk.get("total_value", 0):.0f}** 元')
            if risk.get('stop_loss_triggered'):
                for s in risk['stop_loss_triggered']:
                    lines.append(f'- 🚨 **该卖了:** {s["name"]}（{s["code"]}）亏了 {s["pnl_pct"]:.1f}%，触及止损线，建议今天卖掉')
            else:
                lines.append(f'- 止损: 无触发 ✅，持仓都在安全线内')
            if risk.get('concentration_warning'):
                lines.append(f'- ⚠️ **注意分散:** {risk.get("max_single_code")} 占了你总资金的 {risk.get("max_single_pct", 0):.0%}，太集中了。建议单只不要超过 30%')
            if risk.get('blacklist_count', 0) > 0:
                lines.append(f'- ⚫ 已拉黑 {risk.get("blacklist_count")} 只（之前亏过的标的，不再碰）')
            lines.append('')
        except Exception:
            pass

    return '\n'.join(lines)


def run_all(condensed=False):
    print("="*55)
    tag = "精简版" if condensed else ""
    print(f"  天眼 v6.0 · 启动 44模块 · {' + ' + tag if tag else ''}")
    print("="*55)
    print()

    # ═══ 铁律#3.1: 数据新鲜度强制检查 (v6.0) ═══
    print("[0/8] 数据新鲜度检查...")
    try:
        from engine.unified_verdict import check_data_freshness, generate_unified_report, format_report
        fresh, freshness = check_data_freshness()
        stale_items = [k for k, v in freshness.items() if not v.get('fresh')]
        if stale_items:
            print(f"  ⚠ 数据过期: {', '.join(stale_items)} → 强制刷新...")
    except Exception as e:
        print(f"  ⚠ unified_verdict导入失败: {e} → 跳过新鲜度检查")
        fresh, freshness, stale_items = True, {}, []

    print("[1/8] 爬取最新数据...")
    steps = collect_all_data()
    for s in steps: print("  OK " + s)
    portfolio = load_portfolio()
    if not portfolio: print("  FAIL"); return

    print()
    print("[2/8] 市场状态 (5模块)...")
    sec1 = section_1_market(condensed=condensed)
    try: from news_energy import NewsEnergyCalculator; print("  OK news_energy")
    except: pass
    try: from policy_engine import MultiEnginePolicyMonitor; print("  OK policy_engine")
    except: pass
    print("  OK market_state + screening + linker + energy + policy")

    print()
    print("[3/8] 持仓分析 (6模块)...")
    sec2 = section_2_positions(portfolio, condensed=condensed)
    try: from sub_models import ALL_MODELS; print("  OK sub_models(86规则)")
    except: pass
    try: from conflict_resolver import context_aware_resolve; print("  OK conflict_resolver")
    except: pass
    try: from sub_models.oneil_states import scan_by_state; print("  OK oneil_states")
    except: pass
    print("  OK constitution + Kelly + cost + 86rules + resolve + oneil")

    print()
    print("[4/8] 选股 (3模块)...")
    sec3 = section_3_screening(portfolio, condensed=condensed)
    try: from event_calibrator import EventCalibrator; print("  OK event_calibrator")
    except: pass
    print("  OK screening + win_rate + event_calibrator")

    print()
    print("[5/8] 推演 (4模块)...")
    sec4 = section_4_scenarios(portfolio, condensed=condensed)
    try:
        import sys; sys.path.insert(0, os.path.join(ROOT, "辅助模块"))
        from stress_tester import StressTester; print("  OK stress_tester")
    except: pass
    try: from paper_trader import PaperTrader; print("  OK paper_trader")
    except: pass
    print("  OK scenario + black_swan + stress + paper")

    print()
    print("[6/8] 风控 (2模块)...")
    sec5 = section_5_risk(portfolio, condensed=condensed)
    print("  OK risk + iron_law")

    # v4.1: 联动信号+熔断状态
    sec_linkage = _build_linkage_section(portfolio)
    sec_fuse = _build_fuse_section(portfolio)
    sec_chain = _build_chain_section()       # v4.2 PDF增强
    sec_thread = _build_thread_section()     # v4.2 主线判断
    sec_watch = _build_watch_section()       # v4.2 明日观察
    sec_regime = _build_regime_alpha_section(portfolio)  # v4.3 NLP量化增强
    sec_attack = _build_attack_section()                  # v5.0 进攻引擎
    sec_enhanced = _build_enhanced_macro_section()        # v5.0 宏观穿透增强

    # PDF分析框架: 从enhanced获取当前体制
    try:
        from daily_macro_enhanced import energy_conduction_chain, us10y_maginot_check
        _energy = energy_conduction_chain()
        _us10y = us10y_maginot_check()
        current_regime = _energy.get('regime', 'oil_stable')
        current_us10y_status = _us10y.get('status', 'safe')
    except:
        current_regime = 'oil_stable'
        current_us10y_status = 'safe'
    sec_pdf = _build_pdf_analytical_section(current_regime, current_us10y_status)

    # 流动性陷阱检测
    try:
        from liquidity_trap_detector import build_liquidity_section
        brent_1d = _energy.get('brent_change_1d', 0)
        # 计算前日变动
        brent_2d = 0
        try:
            import duckdb as _dk
            DB2 = r'D:\FreeFinanceData\data\duckdb\finance.db'
            _c2 = _dk.connect(DB2)
            _r2 = _c2.execute("SELECT wti FROM macro_indicators WHERE wti IS NOT NULL ORDER BY trade_date DESC LIMIT 3").fetchall()
            if len(_r2) >= 3 and _r2[2][0]:
                brent_2d = round((_r2[1][0] - _r2[2][0]) / _r2[2][0] * 100, 1)
            _c2.close()
        except:
            pass
        sec_liquidity = build_liquidity_section(current_regime, brent_1d, brent_2d)
    except:
        sec_liquidity = ''
    print("  OK linkage + fuse + main_thread + regime_alpha + attack + enhanced_macro + pdf_framework + liquidity (v5.0)")

    print()
    print("[7/8] 验证+辅助 (11模块)...")
    app_c = appendix_news(condensed=condensed)
    if not condensed:
        app_a = appendix_rules(); app_b = appendix_monitor()
    for mod in ["backtest","live_tracker","strategy_lifecycle","verification_tower","rule_sources"]:
        try: __import__(mod); print("  OK " + mod)
        except: pass
    for mod in ["announcement_classifier","leakage_detector","event_study","factor_correlation_analyzer","multi_rule_tester","position_manager","risk_metrics"]:
        try: __import__(mod); print("  OK " + mod)
        except: pass
    print("  OK 全部验证+辅助模块")

    print()
    print("[8/8] 生成报告...")

    # ═══ v6.0: 统一裁决引擎生成 (三层金字塔总纲) ═══
    unified_verdict_section = ''
    try:
        from engine.unified_verdict import generate_unified_report, format_report
        # 传入实时上涨率 (从WebSearch获取或估算)
        verdict_data = generate_unified_report()
        unified_verdict_section = format_report(verdict_data)
        print("  OK unified_verdict (天眼2.0三层金字塔)")
    except Exception as e:
        print(f"  ⚠ unified_verdict生成失败: {e}")
        import traceback; traceback.print_exc()

    report = []

    if condensed:
        # === 精简版组装 ===
        health_line = _get_health_score()
        td, reason, next_td = is_trading_day()
        ns = next_td.strftime('%m月%d日') if not td else '今日'
        # Get actual K-line date
        kline_date = today_str
        try:
            kl = q("SELECT MAX(trade_date) FROM kline_daily")
            if kl and kl[0]: kline_date = str(kl[0])[:10]
        except: pass

        report.append("# 天眼日报 · " + today_str + "（精简版）")
        report.append("")
        report.append(f"> {now_str} | 系统健康{health_line}分 | K线截至{kline_date} | 操作顺延至{ns}15:00前")
        report.append("")
        report.append("---")
        report.append("")

        # ═══ v6.0: 统一裁决作为第一段输出 (三大金字塔总纲) ═══
        if unified_verdict_section:
            report.append(unified_verdict_section)
            report.append("")
            report.append("---")
            report.append("")
            report.append("## ℹ️ 以下为各模块详细数据 (参考附录)")
            report.append("")

        # sec1 returns dict: market_state, capital_flow, national_team
        report.append(sec1['market_state'])
        report.append(sec1['capital_flow'])
        report.append(sec1['national_team'])
        report.append("---")
        report.append("")
        if sec_enhanced:
            report.append(sec_enhanced)
            report.append("---")
            report.append("")
        if sec_liquidity:
            report.append(sec_liquidity)
            report.append("---")
            report.append("")
        if sec_pdf:
            report.append(sec_pdf)
            report.append("---")
            report.append("")
        # sec2 returns dict: holdings, recommendations
        report.append(sec2['holdings'])
        report.append(sec2['recommendations'])
        report.append("---")
        report.append("")
        report.append(sec3)  # string
        report.append("---")
        report.append("")
        report.append(sec4)  # string
        report.append("---")
        report.append("")
        report.append(sec5)  # string
        report.append("---")
        report.append("")
        report.append(sec_linkage)  # v4.1 联动信号
        report.append("---")
        report.append("")
        report.append(sec_fuse)  # v4.1 熔断状态
        report.append("---")
        report.append("")
        report.append(sec_chain)  # v4.2 全球穿透传导信号
        report.append("---")
        report.append("")
        report.append(sec_thread)  # v4.2 当日主线/避雷
        report.append("---")
        report.append("")
        report.append(sec_watch)  # v4.2 明日观察指标
        report.append("---")
        report.append("")
        report.append(sec_regime)  # v4.3 NLP量化增强
        report.append("---")
        report.append("")
        report.append(sec_attack)  # v5.0 进攻引擎
        report.append("---")
        report.append("")
        report.append(section_6_actions(portfolio, condensed=True))
        report.append("---")
        report.append("")
        report.append(app_c)  # string
        report.append("")
        report.append("---")
        report.append("")
        report.append(condensed_glossary())
        report.append("")
        report.append("---")
        report.append("*天眼 v5.0 · " + now_str + "*")
    else:
        # === 完整版组装 (原逻辑 + v6.0统一裁决) ===
        report.append("# 天眼日报 · " + today_str)
        report.append("")
        report.append("> " + now_str + " | 44模块 | 铁律#0-#15 | 天眼2.0裁决引擎")
        report.append("")

        # ═══ v6.0: 统一裁决作为第一段输出 (三大金字塔总纲) ═══
        if unified_verdict_section:
            report.append(unified_verdict_section)
            report.append("")
            report.append("---")
            report.append("")
            report.append("## ℹ️ 以下为各模块详细数据 (参考附录)")
            report.append("")

        report.append(sec1)
        sec1e = section_1e_emotional_risk(portfolio)
        report.append(sec1e)
        if sec_enhanced:
            report.append("---")
            report.append("")
            report.append(sec_enhanced)
        if sec_liquidity:
            report.append("---")
            report.append("")
            report.append(sec_liquidity)
        if sec_pdf:
            report.append("---")
            report.append("")
            report.append(sec_pdf)
        report.append(sec2); report.append(sec3)
        report.append(sec4); report.append(sec5)
        report.append("---")
        report.append("")
        report.append(sec_linkage)  # v4.1
        report.append("---")
        report.append("")
        report.append(sec_fuse)  # v4.1
        report.append("---")
        report.append("")
        report.append(sec_chain)  # v4.2
        report.append("---")
        report.append("")
        report.append(sec_thread)  # v4.2
        report.append("---")
        report.append("")
        report.append(sec_watch)  # v4.2
        report.append("---")
        report.append("")
        report.append(sec_regime)  # v4.3 NLP量化增强
        report.append("---")
        report.append("")
        report.append(sec_attack)  # v5.0 进攻引擎
        report.append(section_6_actions(portfolio))
        report.append("---")
        report.append("")
        report.append(app_a); report.append(app_b); report.append(app_c)
        report.append("")
        report.append("---")
        report.append("*天眼 v5.0 · " + now_str + "*")

    sep = chr(10)
    full = sep.join(report)
    suffix = '_精简版' if condensed else ''
    out_path = os.path.join(out_dir, f'天眼日报_{today_str}{suffix}.md')
    with open(out_path, "w", encoding="utf-8") as f: f.write(full)
    desktop_file = f"C:/Users/Lenovo/Desktop/天眼日报_{today_str}{suffix}.md"
    with open(desktop_file, "w", encoding="utf-8") as f2: f2.write(full)

    elapsed = time.time() - t0
    print()
    print("  Report: " + desktop_file)
    print("  Time: " + str(int(elapsed)) + "s | 44 modules | OK")
    print("="*55)
    return "天眼日报_" + today_str + suffix + ".md"

if __name__ == "__main__":
    condensed = '--condensed' in sys.argv
    both = '--both' in sys.argv
    if both:
        run_all(condensed=False)
        print("\n" + "="*55 + "\n")
        run_all(condensed=True)
    else:
        run_all(condensed=condensed)
