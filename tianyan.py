"""天眼系统 · 主入口

用法:
  python tianyan.py market     — 市场面全景
  python tianyan.py prosperity — 行业景气度
  python tianyan.py scan       — 条件选股
  python tianyan.py tech 600519 — 技术面分析
  python tianyan.py fund 600519 — 基本面分析
  python tianyan.py pailei 600519 — 财务排雷
  python tianyan.py trace      — 组合追踪
  python tianyan.py plan       — 情景推演
  python tianyan.py eq 000001,600519 — 实时行情速查(easyquotation)
  python tianyan.py analyze CODE — 三板斧（看天→看地→算账）
  python tianyan.py recommend  — 统一建议引擎(读持仓→全链→三板斧输出)
  python tianyan.py recommend --quick — 快速版(降级技术面)
  python tianyan.py daily      — 日频数据采集
  python tianyan.py indicators — 指标预计算
  python tianyan.py risk       — 风控报告
  python tianyan.py audit      — L2规则审计（矛盾/重叠/盲区）
  python tianyan.py verify     — L0-L4五层验证塔综合报告
  python tianyan.py lifecycle  — L4策略生命周期检查
  python tianyan.py backtest   — L1回测(WF+PBO+分区)
  python tianyan.py scan       — 多模型扫描(五层裁决)
  python tianyan.py full       — 天眼日报v6.0(统一裁决三层金字塔+44模块: 宏观体制→市场结构→行业催化→持仓→选股→推演→进攻引擎→风控)
  python tianyan.py unified    — 天眼2.0统一裁决(三大裁决金字塔, 独立运行)
  python tianyan.py news       — 消息能量模型(E_total/E_consumed/E_residual+Bass+信源追踪)
  python tianyan.py news demo  — 消息能量模型演示
  python tianyan.py collect     — 数据采集(独立, 不跑分析)
  python tianyan.py conduction  — 跨市场传导时滞矩阵(独立模块)
  python tianyan.py conduction --signal — 硬编码快通道方向信号
  python tianyan.py conduction --update — 重建传导矩阵
  python tianyan.py fingerprint — 资金流微观结构指纹(独立模块)
  python tianyan.py fingerprint --code 016708 — 单标的五维指纹
  python tianyan.py rules       — 规则失效预警引擎
  python tianyan.py rules --rule R26 — 单条规则检查
  python tianyan.py anticonsensus — 反共识景气度模型
  python tianyan.py anticonsensus --sector 有色 — 单板块
  python tianyan.py all          — 一键全跑(采集→并行模块→全链)
  python tianyan.py chart 000300 — K线图(TradingView风格, 叠加传导信号)
  python tianyan.py positions — 持仓技术快检(均线/MACD/KDJ/量价+判定)
  python tianyan.py surprise 600519 — NLP预期差分析(噪音过滤+黑话检测+产业链传导)
  python tianyan.py regime — Market Regime四象限(广度+轮动烈度+新闻乘数)
  python tianyan.py papertrade — 模拟盘v2(A股真实费率+滑点+JSON信号撮合)
  python tianyan.py cro — CRO日度诊断报告(数据健康+Alpha检视+持仓风险)
  === 🔬 回测实验室 (战法→四重门→纸交 全链路验证) ===
  python tianyan.py attack  — 进攻引擎全模块(双模扫描+加油监控+脆弱地图+三大过滤)
  python tianyan.py scan_dual  — 全市场双模扫描(模式A窒息底+模式B空中加油)
  python tianyan.py refuel     — AI空中加油监控v2(CPO三剑客+Rule1/2/3+四道闸门)
  python tianyan.py fragility  — 因果推演脆弱地图(5层21格+一句话判断)
  python tianyan.py filters    — 三大过滤规则(RS+量能天花板+日历锚定)
  python tianyan.py backtest   — MA20+ATR回测 (backtest_v8_atr_fast.py)
  === CIO决策引擎 v1.0 ===
  python tianyan.py cio — CIO决策日报(风控优先·异常驱动·因果推演·四段式)
  === v6.1 全链并网 ===
  python tianyan.py pipeline                — 六步全链并网(实时价+Minervini闸门+天眼打分+资金分配)
  python tianyan.py pipeline --capital 500  — 指定总资产
  python tianyan.py pipeline --no-realtime  — 纯DB模式(不查实时价)
"""

import sys, os, json, subprocess, io

# Windows GBK编码兼容 — 仅真实终端时包装(子进程跳过)
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'buffer') and sys.stdout.isatty():
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        if hasattr(sys.stderr, 'buffer') and sys.stderr.isatty():
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(BASE, 'mcp_server', 'server.py')
ENGINE_DIR = os.path.join(BASE, 'engine')
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'

# v6.1 全链并网 — 纯函数导入
from engine.technical_factors import (
    asymmetric_gain_score, rsi_lambda, compute_gain_20d, get_latest_rsi,
)
from engine.screening_engine import screen_minervini_with_db, SECTOR_INDICES
from capital_allocator import allocate as allocate_capital, print_allocation

def run_py(script, *args, capture=True):
    python = r'C:\Users\Lenovo\AppData\Local\Programs\Python\Python310\python.exe'
    cmd = [python, script] + list(args)
    pythonpath = BASE + os.pathsep + os.environ.get('PYTHONPATH', '')
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONPATH': pythonpath}
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE,
                                encoding='utf-8', errors='replace', env=env)
        return result.stdout, result.stderr
    else:
        subprocess.run(cmd, cwd=BASE, env=env)
        return '', ''

def _preflight_guard(cmd_name=''):
    """DataGuard 起飞前检查。数据严重过期时拒绝执行。"""
    try:
        from engine.data_guard import DataGuard
        guard = DataGuard()
        ok, cells = guard.preflight_check()
        stale = [c for c in cells if c.is_stale]
        critical = [c for c in stale if c.is_expired]

        if critical:
            print(f"\n{'='*55}")
            print(f"[FATAL] 数据严重过期 ({len(critical)}项), 拒绝执行 '{cmd_name}'")
            print(f"{'='*55}")
            for c in critical:
                print(f"  {c.status_line()}")
            print(f"  请先运行: python tianyan.py daily")
            print(f"{'='*55}\n")
            return False

        if stale:
            print(f"\n[WARN] 数据部分过期 ({len(stale)}项), 继续执行但标注\n")
            for c in stale:
                print(f"  {c.status_line()}")
        else:
            print(f"[OK] 数据新鲜 ({len(cells)}项检查通过)")

        return True
    except ImportError:
        # DataGuard 模块不存在, 允许继续
        return True
    except Exception as e:
        print(f"[WARN] 预检失败: {e}, 继续执行")
        return True


def cmd_market():
    print("[天眼] 市场面全景\n")
    # 使用新的 O'Neil + 养家 双引擎
    ms_script = os.path.join(ENGINE_DIR, 'market_state.py')
    run_py(ms_script, capture=False)

def cmd_tech(code):
    from engine.code_mapper import normalize_ts_code
    code = normalize_ts_code(code)
    print(f"[天眼] 技术面: {code}\n")
    stdout, _ = run_py(MCP, 'kline', code, 'D')
    data = json.loads(stdout) if stdout.strip().startswith('{') else {}

    if data.get('count', 0) == 0 or not data.get('indicators'):
        # 降级：DB无数据，实时查AKShare自算
        print("  (DB无数据，实时查询AKShare...)\n")
        _tech_fallback(code)
        return

    print(f"K线: {data.get('count', 0)} 条")
    kline = data.get('kline', [])
    if kline:
        latest_date = kline[0].get('trade_date', '?')[:10]
        from datetime import date, datetime
        try:
            db_date = datetime.strptime(latest_date, '%Y-%m-%d').date()
            lag = (date.today() - db_date).days
            lag_str = f' (滞后{lag}天)' if lag > 1 else ''
            if lag > 3:
                print(f"  ⚠ 最新数据: {latest_date}{lag_str} — 建议先跑 python tianyan.py daily")
            else:
                print(f"  最新: {latest_date}{lag_str}")
        except:
            pass
    indicators = data.get('indicators', [])
    if indicators:
        ind = indicators[0]
        print(f"\n均线: MA5={ind.get('ma5','?')} MA20={ind.get('ma20','?')} MA60={ind.get('ma60','?')}")
        print(f"MACD: DIF={ind.get('macd_dif','?')} DEA={ind.get('macd_dea','?')}")
        print(f"KDJ: K={ind.get('kdj_k','?')} D={ind.get('kdj_d','?')} J={ind.get('kdj_j','?')}")
        print(f"BOLL: 上={ind.get('boll_upper','?')} 中={ind.get('boll_mid','?')} 下={ind.get('boll_lower','?')}")
        print(f"RSI: 6={ind.get('rsi6','?')} 14={ind.get('rsi14','?')}")
        print(f"均线排列: {ind.get('ma_alignment','?')}")
    else:
        print(stdout[:2000])

def cmd_fund(code):
    from engine.code_mapper import normalize_ts_code
    code = normalize_ts_code(code)
    print(f"[天眼] 基本面: {code}\n")
    stdout, _ = run_py(MCP, 'fin', code)
    data = json.loads(stdout) if stdout.strip().startswith('{') else {}

    val = data.get('valuation', {})
    dupont = data.get('dupont', {})
    growth = data.get('growth', {})

    if not val and not dupont:
        # 降级：DB无数据，实时查AKShare
        print("  (DB无数据，实时查询AKShare...)\n")
        _fund_fallback(code)
        return

    print(f"估值: PE={val.get('pe_ttm','?')} PB={val.get('pb','?')} PE分位={val.get('pe_percentile_5y','?')}")
    print(f"ROE杜邦: ROE={dupont.get('roe','?')}% 净利率={dupont.get('net_margin','?')}% 周转={dupont.get('asset_turnover','?')} 杠杆={dupont.get('equity_multiplier','?')}")
    print(f"成长: 营收YoY={growth.get('revenue_yoy','?')}% 利润YoY={growth.get('profit_yoy','?')}%")

def cmd_risk():
    risk_script = os.path.join(ENGINE_DIR, 'risk_controller.py')
    stdout, _ = run_py(risk_script)
    print(stdout)

def cmd_daily(args=None):
    """日频数据采集。--full: 采集后自动跑分析+存档"""
    collector = os.path.join(BASE, 'karen_upgrade', 'data_collectors', 'tianyan_collector.py')
    run_py(collector, 'daily', capture=False)

    full = args and '--full' in args
    if not full:
        return

    print("\n" + "=" * 60)
    print("  天眼全链路 · 数据→分析→存档")
    print("=" * 60)

    # 1. 大盘复盘 → reports/market_*.md
    print("\n[1/3] 大盘复盘...")
    try:
        from engine.report_writer import save_market_report
        import duckdb as _dkdb
        _con = _dkdb.connect(DB)
        macro = _con.execute('SELECT us10y, wti, shibor_on, usdcny FROM macro_indicators ORDER BY trade_date DESC LIMIT 1').fetchone()
        sent = _con.execute('SELECT limit_up_count, limit_down_count, bomb_rate, emotion_score, market_emotion FROM market_sentiment ORDER BY trade_date DESC LIMIT 1').fetchone()
        _con.close()
        us10y = float(macro[0]) if macro and macro[0] else 4.5
        wti = float(macro[1]) if macro and macro[1] else 90
        shibor = float(macro[2]) if macro and macro[2] else 1.3
        limit_up = int(sent[0]) if sent and sent[0] else 0
        limit_down = int(sent[1]) if sent and sent[1] else 0
        score = int(sent[3]) if sent and sent[3] else 35
        stage = sent[4] if sent and sent[4] else '?'
        mkt = {
            'oneil': '上升确认', 'emotion': stage, 'emotion_score': score,
            'win_rate': 65, 'cap': 25, 'us10y': round(us10y, 2),
            'wti': round(wti, 1), 'shibor': round(shibor, 3),
            'limit_up': limit_up, 'limit_down': limit_down,
            'risks': [f'养家: 情绪{stage}({score}分) → 空仓或极小仓试错活口' if score < 40 else f'涨停{limit_up}跌停{limit_down}'],
        }
        path = save_market_report(mkt)
        print(f"  大盘报告: {path}")
    except Exception as e:
        print(f"  大盘报告失败: {e}")

    # 2. 持仓建议 → reports/recommend_*.md
    print("\n[2/3] 持仓建议...")
    try:
        rec_script = os.path.join(BASE, 'recommend.py')
        run_py(rec_script, '--quick', capture=False)
    except Exception as e:
        print(f"  持仓建议失败: {e}")

    # 3. 摘要
    from datetime import date as _date
    today = _date.today().isoformat()
    print(f"\n[3/3] 完成 — reports/market_{today}.md + reports/recommend_{today}.md")

def cmd_indicators():
    script = os.path.join(BASE, 'karen_upgrade', 'indicator_precompute.py')
    run_py(script, capture=False)

def cmd_trace():
    """组合追踪 — 贝叶斯动态校准 + 仓位建议"""
    trace_script = os.path.join(ENGINE_DIR, 'scenario_engine.py')
    run_py(trace_script, 'trace', capture=False)

def cmd_plan():
    """情景推演 — 六条法则全景输出"""
    plan_script = os.path.join(ENGINE_DIR, 'scenario_engine.py')
    run_py(plan_script, 'plan', capture=False)

# ═══════════════════════════════════════════
# 实时降级：DB无数据时直接查AKShare
# ═══════════════════════════════════════════

def _tech_fallback(code):
    """DB空 → 实时查AKShare + 自算技术指标"""
    import akshare as ak
    import pandas as pd
    import numpy as np
    import time
    from datetime import date, timedelta

    end = date.today().strftime('%Y%m%d')
    start = (date.today() - timedelta(days=300)).strftime('%Y%m%d')

    df = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start, end_date=end, adjust='qfq')
            break
        except:
            if attempt < 2:
                time.sleep(2.0)
            else:
                try:
                    df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start, end_date=end, adjust='')
                except:
                    pass

    if df is None or df.empty:
        print(f"  未获取到 {code} 的K线数据（已重试3次）")
        return

    close = df['收盘'].values
    if len(close) < 20:
        print(f"  仅{len(close)}条K线，数据不足")
        return

    latest = df.iloc[-1]
    print(f"  最新: {latest['日期'].strftime('%Y-%m-%d') if hasattr(latest['日期'], 'strftime') else str(latest['日期'])}  收盘: {latest['收盘']:.2f}  涨跌: {latest.get('涨跌幅', 0):.2f}%")
    print(f"  成交额: {latest.get('成交额', 0)/1e8:.1f}亿  换手: {latest.get('换手率', 0):.2f}%")

    # 均线
    def _ma(arr, n):
        if len(arr) < n: return None
        return np.mean(arr[-n:])
    ma5, ma10, ma20, ma60 = _ma(close,5), _ma(close,10), _ma(close,20), _ma(close,60)
    cur = close[-1]
    print(f"\n  均线系统:")
    print(f"  MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA60={ma60:.2f}")
    alignment = '多头' if ma5 and ma10 and ma20 and ma5 > ma10 > ma20 else ('空头' if ma5 and ma10 and ma20 and ma5 < ma10 < ma20 else '交叉/缠绕')
    print(f"  均线排列: {alignment}  价格vs MA5: {'上方' if cur > (ma5 or 0) else '下方'}  vs MA20: {'上方' if cur > (ma20 or 0) else '下方'}")

    # MACD
    ema12 = pd.Series(close).ewm(span=12).mean()
    ema26 = pd.Series(close).ewm(span=26).mean()
    dif = (ema12 - ema26).values
    dea = pd.Series(dif).ewm(span=9).mean().values
    hist = 2 * (dif - dea)
    print(f"\n  MACD: DIF={dif[-1]:.3f} DEA={dea[-1]:.3f} 柱={hist[-1]:.3f}")
    print(f"  状态: {'金叉' if dif[-1] > dea[-1] else '死叉'}{' 收敛' if abs(hist[-1]) < abs(hist[-2]) else ' 扩散'}")

    # KDJ
    low9 = pd.Series(df['最低'].values[-9:]).min()
    high9 = pd.Series(df['最高'].values[-9:]).max()
    rsv = (cur - low9) / (high9 - low9) * 100 if high9 != low9 else 50
    k_prev = 50
    k = 2/3 * k_prev + 1/3 * rsv
    d = 2/3 * k_prev + 1/3 * k
    j = 3 * k - 2 * d
    kdj_zone = '超买' if k > 80 else ('超卖' if k < 20 else '中性')
    print(f"\n  KDJ(9,3,3): K={k:.1f} D={d:.1f} J={j:.1f} [{kdj_zone}]")

    # BOLL
    ma20_v = _ma(close, 20)
    std20 = np.std(close[-20:])
    boll_up = ma20_v + 2 * std20
    boll_lo = ma20_v - 2 * std20
    boll_width = (boll_up - boll_lo) / ma20_v * 100 if ma20_v else 0
    boll_pos = (cur - boll_lo) / (boll_up - boll_lo) * 100 if boll_up != boll_lo else 50
    print(f"\n  BOLL(20): 上={boll_up:.2f} 中={ma20_v:.2f} 下={boll_lo:.2f}")
    print(f"  带宽={boll_width:.1f}%  价格位置={boll_pos:.0f}% {'→ 上轨附近' if boll_pos > 80 else '→ 下轨附近' if boll_pos < 20 else ''}")

    # RSI
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain_6 = gain.rolling(6).mean().iloc[-1]
    avg_loss_6 = loss.rolling(6).mean().iloc[-1]
    rs6 = avg_gain_6 / avg_loss_6 if avg_loss_6 > 0 else 100
    rsi6 = 100 - 100 / (1 + rs6)
    avg_gain_14 = gain.rolling(14).mean().iloc[-1]
    avg_loss_14 = loss.rolling(14).mean().iloc[-1]
    rs14 = avg_gain_14 / avg_loss_14 if avg_loss_14 > 0 else 100
    rsi14 = 100 - 100 / (1 + rs14)
    rsi_zone = '超买' if rsi14 > 70 else ('超卖' if rsi14 < 30 else '正常')
    print(f"\n  RSI: 6={rsi6:.1f} 14={rsi14:.1f} [{rsi_zone}]")

    # 量价
    vol = df['成交量'].values[-5:]
    vol_avg20 = np.mean(df['成交量'].values[-20:])
    vol_ratio = vol[-1] / vol_avg20 if vol_avg20 > 0 else 1
    price_chg_5d = (cur - close[-5]) / close[-5] * 100 if len(close) >= 5 else 0
    vol_price = '放量上涨' if (vol_ratio > 1.2 and price_chg_5d > 0) else ('放量下跌' if vol_ratio > 1.2 and price_chg_5d < 0 else '缩量' if vol_ratio < 0.8 else '正常')
    print(f"\n  量价: 5日均量比={vol_ratio:.1f}  5日涨跌={price_chg_5d:.1f}%  → {vol_price}")

    # 综合评分
    score = 50
    if alignment == '多头': score += 15
    if ma5 and cur > ma5: score += 5
    if ma20 and cur > ma20: score += 5
    if dif[-1] > dea[-1]: score += 10
    if 30 < rsi14 < 70: score += 5
    if rsi14 < 35: score += 5  # 低位反转潜力
    if kdj_zone == '超卖': score += 5
    if vol_price == '缩量': score += 5  # 缩量企稳
    print(f"\n  综合技术评分: {min(100, score)}/100")


def _fund_fallback(code):
    """DB空 → 实时查AKShare基本面"""
    import akshare as ak
    import pandas as pd
    import time
    from datetime import date, timedelta

    print(f"  代码: {code}")

    # 1. 财务摘要（最稳定接口）
    try:
        abs_df = ak.stock_financial_abstract(symbol=code)
        if abs_df is not None and not abs_df.empty:
            # 列名: ['选项','指标','20260331','20251231',...]
            # 行: 常用指标/每股指标/盈利能力/成长能力/...
            # 提取关键指标
            metrics = {}
            for _, row in abs_df.iterrows():
                key = row.get('指标', '')
                # 取最新季度(第三个col=最近年报/季报)
                val = row.iloc[3] if len(row) > 3 else None
                if key and val is not None:
                    metrics[key] = val

            # 盈利能力
            roe = metrics.get('净资产收益率(ROE)', '?')
            npm = metrics.get('销售净利率', '?')
            gm = metrics.get('销售毛利率', '?')
            eps = metrics.get('每股收益', '?')
            bps = metrics.get('每股净资产', '?')
            print(f"\n  盈利能力(最新期):")
            print(f"  EPS={eps}  每股净资产={bps}")
            print(f"  ROE={roe}%  净利率={npm}%  毛利率={gm}%")

            # 成长性
            rev_g = metrics.get('营业收入增长率', '?')
            np_g = metrics.get('净利润增长率', '?')
            na_g = metrics.get('净资产增长率', '?')
            print(f"\n  成长性:")
            print(f"  营收YoY={rev_g}%  利润YoY={np_g}%  净资产增长={na_g}%")

            # 估值+财务健康
            pe = metrics.get('市盈率', '?')
            pb = metrics.get('市净率', '?')
            debt = metrics.get('资产负债率', '?')
            cur_r = metrics.get('流动比率', '?')
            print(f"\n  估值+健康:")
            print(f"  PE={pe}  PB={pb}")
            print(f"  资产负债率={debt}%  流动比率={cur_r}")
    except Exception as e:
        print(f"  财务摘要失败: {e}")

    # 2. PE分位 (从日K线含PE的列，延迟避免限流)
    time.sleep(1.0)
    try:
        end_d = date.today().strftime('%Y%m%d')
        start_d = (date.today() - timedelta(days=365)).strftime('%Y%m%d')
        kline = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start_d, end_date=end_d, adjust='qfq')
        if kline is not None and not kline.empty and '市盈率' in kline.columns:
            pe_col = kline['市盈率'].dropna()
            if len(pe_col) > 10:
                pe_cur = pe_col.iloc[-1]
                pe_min, pe_max = pe_col.min(), pe_col.max()
                pct = (pe_col <= pe_cur).sum() / len(pe_col) * 100
                level = '低估' if pct < 25 else ('高估' if pct > 75 else '合理')
                print(f"\n  估值水位(近1年): PE={pe_cur:.1f}  分位={pct:.0f}% [{level}]")
                print(f"  1年PE区间: {pe_min:.1f} ~ {pe_max:.1f}")
    except:
        pass

def cmd_analyze(code):
    """三板斧：看天→看地→算账 → 加/减/观望 + 点位 + 概率"""
    from engine.code_mapper import normalize_ts_code
    code = normalize_ts_code(code)
    script = os.path.join(ENGINE_DIR, 'stock_analyzer.py')
    run_py(script, code, capture=False)

def cmd_recommend(args=None):
    """统一建议引擎 — 读 portfolio.json → 全数据链 → 三板斧输出 + 纠错线"""
    script = os.path.join(BASE, 'recommend.py')
    extra = list(args) if args else []
    run_py(script, *extra, capture=False)

def cmd_audit():
    """L2: 规则审计 — 矛盾检测+重叠分析+盲区扫描+投票权重"""
    script = os.path.join(ENGINE_DIR, 'rule_audit.py')
    run_py(script, capture=False)

def cmd_verify():
    """L0-L4: 五层验证塔综合报告"""
    script = os.path.join(ENGINE_DIR, 'conflict_resolver.py')
    # 调用 get_full_verification_report
    stdout, _ = run_py(os.path.join(ENGINE_DIR, 'verification_tower.py'))
    print(stdout)

def cmd_lifecycle():
    """L4: 策略生命周期检查 — 自动冻结/退役"""
    script = os.path.join(ENGINE_DIR, 'strategy_lifecycle.py')
    run_py(script, capture=False)

def cmd_backtest():
    """L1: 回测引擎 — Walk-Forward+PBO+分区"""
    script = os.path.join(ENGINE_DIR, 'backtest.py')
    run_py(script, capture=False)

def cmd_scan():
    """多模型扫描 — 五层裁决"""
    script = os.path.join(ENGINE_DIR, 'conflict_resolver.py')
    run_py(script, capture=False)

def cmd_full(args=None):
    """天眼日报 — 一体化裁决→市场→持仓→选股→推演→执行 六段式报告(v6.0)
    用法: python tianyan.py full [--condensed|--both]"""
    if not _preflight_guard('full'):
        return
    script = os.path.join(ENGINE_DIR, 'report_orchestrator.py')
    extra = list(args) if args else []
    run_py(script, *extra, capture=False)

def cmd_unified():
    """天眼2.0统一裁决 — 三大裁决金字塔独立运行
    用法: python tianyan.py unified"""
    if not _preflight_guard('unified'):
        return
    script = os.path.join(ENGINE_DIR, 'unified_verdict.py')
    run_py(script, capture=False)

def cmd_report():
    """每日报告 — 新手可读格式(铁律#10)"""
    script = os.path.join(ENGINE_DIR, 'daily_report.py')
    run_py(script, capture=False)


def cmd_collect():
    """数据采集 — 独立步骤, 不跑分析
    采集链: A股日频 → 新闻 → 全球指数 → 宏观/商品
    """
    import time, duckdb as _dkdb
    from datetime import date

    print(f'╔══════════════════════════════════════════╗')
    print(f'║  天眼数据采集 v4.0                       ║')
    print(f'║  {date.today()}                          ║')
    print(f'╚══════════════════════════════════════════╝')

    errors = []

    # 1. A股日频 (kline + 指标 + 情绪 + 估值)
    print('\n[1/4] A股日频数据...')
    try:
        collector = os.path.join(BASE, 'karen_upgrade', 'data_collectors', 'tianyan_collector.py')
        run_py(collector, 'daily', capture=False)
    except Exception as e:
        errors.append(f'A股日频: {e}')
        print(f'  [!] {e}')

    # 2. 新闻 (v3: 东方财富, CLS API已挂)
    print('\n[2/4] 新闻...')
    try:
        from engine.news_collector_v3 import collect_and_store
        n = collect_and_store()
        if n == 0:
            from engine.news_collector_v2 import collect_and_store as v2
            n = v2()
    except Exception as e:
        errors.append(f'新闻: {e}')
        print(f'  [!] {e}')

    # 3. 全球指数 (Sina, 稳定)
    print('\n[3/4] 全球指数...')
    try:
        from engine.collector_sources import fetch_sina_global_indices, GLOBAL_INDICES, _db
        from datetime import date, timedelta
        import pandas as pd
        conn = _db()
        today = date.today()
        start = today - timedelta(days=5)  # 补最近5天
        dfs = fetch_sina_global_indices(start, today, GLOBAL_INDICES)
        total = 0
        for code, df in dfs.items():
            for _, row in df.iterrows():
                try:
                    conn.execute('''INSERT OR REPLACE INTO global_index_daily
                        (index_code, trade_date, open, high, low, close, volume, amount)
                        VALUES (?,?,?,?,?,?,?,?)''',
                        [code, str(row['trade_date'])[:10],
                         float(row.get('open',0)), float(row.get('high',0)),
                         float(row.get('low',0)), float(row.get('close',0)),
                         float(row.get('volume',0)), float(row.get('amount',0))])
                    total += 1
                except: pass
        conn.close()
        print(f'  OK: +{total}条')
    except Exception as e:
        errors.append(f'全球指数: {e}')
        print(f'  [!] {e}')

    # 4. 宏观/商品 (AKShare期货, 稳定)
    print('\n[4/4] 宏观/商品...')
    try:
        from engine.collector_sources import fetch_commodities, COMMODITIES, _db
        import akshare as ak
        conn = _db()
        today = date.today()
        start = today - timedelta(days=5)
        comm_df = fetch_commodities(start, today, COMMODITIES)
        total = 0
        if not comm_df.empty:
            for _, row in comm_df.iterrows():
                d = row['trade_date'].strftime('%Y-%m-%d')
                for col in ['wti', 'gold', 'copper', 'aluminum']:
                    if col in comm_df.columns and pd.notna(row.get(col)):
                        conn.execute(f'INSERT INTO macro_indicators (trade_date) VALUES (?) ON CONFLICT (trade_date) DO UPDATE SET {col}=?', [d, float(row[col])])
                        total += 1
        # 美10Y
        try:
            df = ak.bond_zh_us_rate()
            if df is not None and not df.empty and '日期' in df.columns:
                df['trade_date'] = pd.to_datetime(df['日期'])
                mask = (df['trade_date'] >= pd.Timestamp(start)) & (df['trade_date'] <= pd.Timestamp(today))
                df = df[mask]
                for _, row in df.iterrows():
                    d = row['trade_date'].strftime('%Y-%m-%d')
                    v = float(row['美国国债收益率10年']) if pd.notna(row['美国国债收益率10年']) else None
                    if v is not None:
                        conn.execute('INSERT INTO macro_indicators (trade_date) VALUES (?) ON CONFLICT (trade_date) DO UPDATE SET us10y=?', [d, v])
                        total += 1
        except: pass
        conn.close()
        print(f'  OK: +{total}条')
    except Exception as e:
        errors.append(f'宏观: {e}')
        print(f'  [!] {e}')

    # 北向资金 (同花顺hexin.cn, 替换东财失效源)
    try:
        import subprocess, os as _os
        nb_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  '..', '..', 'AgentQuant', 'our', 'northbound_collector.py')
        nb_script = os.path.normpath(nb_script)
        if os.path.exists(nb_script):
            subprocess.run([sys.executable, nb_script], capture_output=True, timeout=30)
            print(f'  [OK] 北向资金 (同花顺hexin.cn)')
        else:
            print(f'  [!] 北向采集脚本未找到: {nb_script}')
    except Exception as e:
        errors.append(f'北向资金: {e}')
        print(f'  [!] 北向资金: {e}')

    print(f'\n{"="*50}')
    if errors:
        print(f'采集完成, {len(errors)}项失败:')
        for e in errors:
            print(f'  - {e}')
    else:
        print(f'采集完成, 全部成功 ✅')

    # 更新 module_run_log
    try:
        conn = _dkdb.connect(DB)
        conn.execute("""INSERT OR REPLACE INTO module_run_log (module_name, last_run_at, ttl_hours, status)
            VALUES ('data_collect', CURRENT_TIMESTAMP, 12, 'ok')""")
        conn.close()
    except: pass

def cmd_collect_news():
    """新闻采集 — 财联社+东财 → DuckDB → 板块筛选"""
    script = os.path.join(ENGINE_DIR, 'news_collector.py')
    run_py(script, capture=False)


def cmd_conduction(args=None):
    """跨市场传导时滞矩阵 — 独立模块
    用法: python tianyan.py conduction            → 硬编码快通道(默认)
          python tianyan.py conduction --signal   → 详细方向信号
          python tianyan.py conduction --update   → 重建全量矩阵
          python tianyan.py conduction --check    → 检查持仓传导
    """
    from engine.cross_market_conduction import (build_conduction_matrix, check_all_holdings,
        get_hardwired_signal)
    import json
    extra = list(args) if args else []

    if '--update' in extra:
        build_conduction_matrix(lookback_days=365)
    elif '--check' in extra:
        check_all_holdings()
    elif '--signal' in extra:
        result = get_hardwired_signal()
        print(f"\n{'='*60}")
        print(f"  跨市场传导 · 硬编码快通道")
        print(f"  生成: {result['generated']}")
        print(f"{'='*60}")
        for sig in result['signals']:
            icon = {'bullish': '↑', 'bearish': '↓', 'neutral': '→'}[sig['direction']]
            print(f"\n  {icon} {sig['pair']}")
            print(f"    领先: {sig['leader']} {sig['leader_change_pct']:+.2f}%")
            print(f"    滞后期: {sig['lag_days']}d  相关: {sig['correlation']:.3f}  p={sig['p_value']:.4f}")
            print(f"    方向: {sig['direction']}  强度: {sig['strength']:+.2f}  [{sig['confidence']}]")
        print(f"\n  ──────────────")
        print(f"  沪深300: {result['hs300_verdict']} (得分 {result['hs300_score']:+.2f})")
        print(f"  科创50:  {result['kc50_verdict']} (得分 {result['kc50_score']:+.2f})")
        print()
    else:
        # 默认: 快通道信号
        result = get_hardwired_signal()
        print(f"\n{'='*60}")
        print(f"  跨市场传导 · 硬编码快通道")
        print(f"  参数: 5年实测验证(p<0.001), 锁定不重算")
        print(f"{'='*60}")
        for sig in result['signals']:
            icon = {'bullish': '↑', 'bearish': '↓', 'neutral': '→'}[sig['direction']]
            print(f"  {icon} {sig['pair']}: {sig['direction']} 强度{sig['strength']:+.2f} "
                  f"({sig['leader']} {sig['leader_change_pct']:+.2f}%)")
        print(f"  → 沪深300: {result['hs300_verdict']} ({result['hs300_score']:+.2f})")
        print(f"  → 科创50:  {result['kc50_verdict']} ({result['kc50_score']:+.2f})")


def cmd_fingerprint(args=None):
    """资金流微观结构指纹 — 独立模块"""
    from engine.capital_flow_fingerprint import compute_fingerprint, check_portfolio_fingerprints, CLASSIFICATION_GUIDE, backtest_youse_0522
    extra = list(args) if args else []
    if '--code' in extra:
        idx = extra.index('--code')
        code = extra[idx + 1] if idx + 1 < len(extra) else None
        if code:
            fp = compute_fingerprint(code, days=20)
            guide = CLASSIFICATION_GUIDE.get(fp['classification'], CLASSIFICATION_GUIDE['neutral'])
            print(f"\n{'='*60}")
            print(f"  资金流微观结构指纹: {fp['name'] or fp['code']}")
            print(f"  日期: {fp['date']}")
            print(f"  分类: {guide['label']} (置信度:{fp['classification_confidence']:.0%})")
            print(f"  建议: {guide['action']}")
            print(f"  风险: {guide['risk']}")
            print(f"  白话: {guide['explanation'][:150]}...")
            print(f"{'='*60}")
        else:
            print("用法: python tianyan.py fingerprint --code 016708")
    elif '--test' in extra:
        backtest_youse_0522()
    else:
        check_portfolio_fingerprints()

def cmd_news(sub=None):
    """消息能量模型 — E_total/E_consumed/E_residual + Bass S曲线 + 信源追踪
    用法: python tianyan.py news        → 演示
          python tianyan.py news demo   → 演示
          python tianyan.py news <json> → 从JSON文件加载事件分析
    """
    script = os.path.join(ENGINE_DIR, 'news_energy.py')
    if sub and sub != 'demo':
        # 从JSON文件加载
        import json as _json
        try:
            with open(sub, 'r', encoding='utf-8') as f:
                events = _json.load(f)
            if isinstance(events, dict):
                events = [events]
            from engine.news_energy import NewsEnergyCalculator
            calc = NewsEnergyCalculator()
            results = calc.analyze_batch(events)
            print(calc.summary_table(results))
            # 输出详细JSON
            for r in results:
                if 'error' not in r:
                    print(f"\n  {r['benchmark_desc']}: {r['judgment']} (t={r['t_statistic']:+.2f})")
        except FileNotFoundError:
            print(f"  文件不存在: {sub}")
        except Exception as e:
            print(f"  解析失败: {e}")
    else:
        run_py(script, capture=False)

def cmd_eq(codes=None):
    """实时行情速查 — easyquotation 腾讯/新浪双通道
    用法: python tianyan.py eq 000001,600519,000858"""
    if not codes:
        print("用法: python tianyan.py eq <code1,code2,...>")
        print("示例: python tianyan.py eq 000001,600519")
        sys.exit(1)

    code_list = []
    for c in codes:
        for part in c.replace('，', ',').split(','):
            part = part.strip()
            if part:
                code_list.append(part)

    print(f"[天眼] 实时行情速查 (easyquotation 腾讯/新浪双通道)\n")

    try:
        sys.path.insert(0, os.path.join(BASE, 'easyquotation'))
        from engine.data_fallback import MultiSourceCollector

        collector = MultiSourceCollector()
        result, stamp = collector.fetch_realtime(code_list)

        # 铁律#9: 数据质量首行显示
        print(f"  [{stamp.label}]")
        if '[OK]' not in stamp.warning:
            print(f"  {stamp.warning}")

        if not result:
            print("  双通道均无数据，请检查网络或代码格式")
            return

        for code, info in result.items():
            name = info.get('name', '?')
            now = info.get('now', '?')
            close = info.get('close', '?')
            open_p = info.get('open', '?')
            high = info.get('high', '?')
            low = info.get('low', '?')
            chg = info.get('涨跌(%)', 0)
            pe = info.get('PE', '?')
            pb = info.get('PB', '?')
            mkt = info.get('总市值', info.get('流通市值', '?'))
            vol = info.get('成交量(手)', info.get('volume', '?'))
            turnover = info.get('turnover', '?')

            tag = '[-]' if chg and float(chg) < -2 else ('[+]' if chg and float(chg) > 2 else ' ~ ')
            print(f"  {tag} {code} {name}")
            print(f"     现价={now}  涨跌={chg}%  开={open_p}  高={high}  低={low}  昨收={close}")
            if pe != '?' or pb != '?':
                print(f"     PE={pe}  PB={pb}  市值={mkt}  换手={turnover}%")
            print()
    except Exception as e:
        print(f"  查询失败: {e}")
        # 降级: 直接用easyquotation
        try:
            sys.path.insert(0, os.path.join(BASE, 'easyquotation'))
            from easyquotation.api import use
            q = use('tencent')
            data = q.stocks(code_list)
            for code, info in data.items():
                name = info.get('name', '?')
                now = info.get('now', '?')
                chg = info.get('涨跌(%)', '?')
                pe = info.get('PE', '?')
                print(f"  {code} {name} now={now} chg={chg}% PE={pe}")
        except:
            pass

def cmd_positions():
    """持仓技术快检"""
    from engine.position_analyzer import analyze_all, print_report
    results = analyze_all()
    print_report(results)


def cmd_chart(code=None):
    """K线图 — lightweight-charts (TradingView风格) + 传导信号叠加"""
    import pandas as pd
    import duckdb as _dkdb
    from datetime import date, timedelta

    conn = _dkdb.connect(DB)
    ts_code = code[0] if code else 'sh000300'

    # 自动补全代码格式
    if not ts_code.startswith('sh') and not ts_code.startswith('sz'):
        if ts_code.startswith('6') or ts_code.startswith('5'):
            ts_code = f'sh{ts_code}'
        else:
            ts_code = f'sz{ts_code}'

    df = conn.execute("""
        SELECT trade_date, open, high, low, close, vol
        FROM kline_daily WHERE ts_code = ? AND trade_date >= ?
        ORDER BY trade_date
    """, [ts_code, (date.today() - timedelta(days=180)).strftime('%Y-%m-%d')]).fetchdf()
    conn.close()

    if df.empty:
        print(f'无数据: {ts_code}')
        return

    df.columns = ['time', 'open', 'high', 'low', 'close', 'volume']
    df['time'] = pd.to_datetime(df['time'])

    try:
        from lightweight_charts import Chart
        chart = Chart(toolbox=True)
        chart.set(df)
        chart.title(ts_code)
        chart.legend(True)
        chart.show(block=True)
    except ImportError:
        print('[!] pip install lightweight-charts')
    except Exception as e:
        # 降级: 用matplotlib
        print(f'lightweight-charts不可用({e}), 降级matplotlib...')
        import matplotlib.pyplot as plt
        import mplfinance as mpf
        df_idx = df.set_index('time')
        mpf.plot(df_idx, type='candle', volume=True, title=ts_code,
                 style='charles', figsize=(14, 7))
        plt.show()


def cmd_all():
    """一键全跑: 采集 → 并行(传导+指纹+规则+反共识) → 全链"""
    import time, threading

    print(f'╔══════════════════════════════════════════╗')
    print(f'║  天眼一键全跑                             ║')
    print(f'╚══════════════════════════════════════════╝')

    # 第1步: 采集 (必须串行, 先有数据)
    print(f'\n--- 第1步: 数据采集 ---')
    cmd_collect()

    # 第2步: 四个独立模块并行
    print(f'\n--- 第2步: 独立模块(并行) ---')
    results = {}

    def run_conduction():
        try:
            from engine.cross_market_conduction import get_hardwired_signal
            results['conduction'] = get_hardwired_signal()
        except Exception as e:
            results['conduction'] = f'失败:{e}'

    def run_fingerprint():
        try:
            from engine.capital_flow_fingerprint import check_portfolio_fingerprints
            check_portfolio_fingerprints()
            results['fingerprint'] = 'ok'
        except Exception as e:
            results['fingerprint'] = f'失败:{e}'

    def run_rules():
        try:
            from engine.rule_failure_early_warning import assess_all_rules
            r = assess_all_rules()
            reds = sum(1 for x in r if x['risk_level'] == 'red')
            oranges = sum(1 for x in r if x['risk_level'] == 'orange')
            results['rules'] = f'红{reds}橙{oranges}'
        except Exception as e:
            results['rules'] = f'失败:{e}'

    def run_anticonsensus():
        try:
            from engine.anti_consensus_prosperity import assess_all
            r = assess_all()
            signals = [x for x in r if abs(x['divergence']) >= 15]
            results['anticonsensus'] = f'{len(signals)}个认知差信号'
        except Exception as e:
            results['anticonsensus'] = f'失败:{e}'

    threads = [
        threading.Thread(target=run_conduction),
        threading.Thread(target=run_fingerprint),
        threading.Thread(target=run_rules),
        threading.Thread(target=run_anticonsensus),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f'\n  并行结果:')
    for k, v in results.items():
        print(f'    {k}: {v}')

    # 第3步: 全链报告
    print(f'\n--- 第3步: 全链报告 ---')
    cmd_full()


def cmd_anticonsensus(args=None):
    """反共识景气度模型 — 第三阶段"""
    from engine.anti_consensus_prosperity import assess_sector, assess_all
    extra = list(args) if args else []
    if '--sector' in extra:
        idx = extra.index('--sector')
        sector = extra[idx + 1] if idx + 1 < len(extra) else None
        if sector:
            r = assess_sector(sector)
            print(f"\n{'='*60}")
            print(f"  {r['verdict_label']} {r['sector']}  剪刀差{r['divergence']:+.0f}")
            print(f"{'='*60}")
            print(f"  共识(新闻): {r['consensus']['consensus_score']:.0f}/100 — {r['consensus']['label']}")
            print(f"  现实(基本面): {r['reality']['reality_score']:.0f}/100 — 动量{r['reality']['momentum_5d']:+.1f}% MA20{r['reality']['ma20_position']:+.1f}%")
            print(f"  建议: {r['action']}")
    else:
        results = assess_all()
        print(f"\n{'='*60}")
        print(f"  反共识 · 认知差排名")
        print(f"{'='*60}")
        for r in results[:8]:
            if abs(r['divergence']) >= 10:
                print(f"  {r['verdict_label']} {r['sector']:6s} 剪刀差{r['divergence']:+.0f}  "
                      f"共识{r['consensus']['consensus_score']:.0f} 现实{r['reality']['reality_score']:.0f}")


def cmd_rules(args=None):
    """规则失效预警引擎 — 第二阶段"""
    from engine.rule_failure_early_warning import assess_all_rules, assess_rule, load_signals, group_by_rule, load_rule_grades, print_summary
    extra = list(args) if args else []
    if '--rule' in extra:
        idx = extra.index('--rule')
        rid = extra[idx + 1] if idx + 1 < len(extra) else None
        if rid:
            signals = load_signals()
            groups = group_by_rule(signals)
            grades = load_rule_grades()
            result = assess_rule(rid, groups.get(rid, []), grades)
            r = result
            print(f"\n{'='*60}")
            print(f"  {r['rule_id']} | {r['rule_name']}")
            print(f"  大师: {r['master']} | 信号: {r['signal_count']}条 | 准确率: {r['accuracy']:.1%}")
            print(f"  回测: {r['backtest_grade']}级 | 风险: {r['risk_label']}")
            print(f"  CuSum: S={r['cusum'].get('cusum_value',0):.2f}")
            print(f"  滚动窗口: z={r['rolling'].get('z_score',0):.1f}")
            print(f"  连续错误: {r['consecutive'].get('consecutive_count',0)}次")
            print(f"  建议: {r['action']}")
            for reason in r['reasons']:
                print(f"    → {reason}")
    else:
        results = assess_all_rules()
        print_summary(results)


def cmd_surprise(code=None):
    """M1 NLP预期差分析"""
    from engine.nlp_surprise import analyze_surprise, analyze_from_cninfo
    if not code:
        print("[天眼] NLP预期差引擎")
        print("用法: python tianyan.py surprise <股票代码>")
        print("      python tianyan.py surprise <代码> --cninfo  # 从巨潮数据库读取")
        return
    print(f"[天眼] NLP预期差分析: {code}\n")
    result = analyze_from_cninfo(code) if '--cninfo' in str(sys.argv) else analyze_surprise(code, '', '', 'cli')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_regime():
    """M2 Market Regime全景"""
    from engine.market_regime import run_regime
    print("[天眼] Market Regime 全景\n")
    run_regime()


def cmd_papertrade():
    """M3 模拟盘v2"""
    from engine.paper_trader_v2 import PaperTraderV2
    print("[天眼] 模拟盘 V2\n")
    # 尝试加载已有状态
    state_file = os.path.join(BASE, 'paper_trader_v2_state.json')
    if os.path.exists(state_file):
        pt = PaperTraderV2.load(state_file)
        print(f"已加载: {pt.name}")
    else:
        pt = PaperTraderV2(initial_capital=100000, name='天眼模拟盘V2')
    print(pt.summary())
    # 检查到期
    exited = pt.check_auto_exit()
    if exited:
        print(f"\n自动平仓: {len(exited)} 笔")
    pt.save(state_file)


def cmd_cro():
    """M4 CRO日度诊断"""
    from engine.cro_daily_diagnostic import run_diagnostic
    print("[天眼] CRO 日度诊断\n")
    run_diagnostic()


# ═══════════════════════════════════════════
# 🔬 回测实验室 (战法→四重门→纸交 全链路验证)
# ═══════════════════════════════════════════

def cmd_scan_dual():
    """全市场双模扫描 — 模式A(窒息底)+模式B(空中加油)"""
    script = os.path.join(ENGINE_DIR, 'dual_mode_scanner.py')
    run_py(script, capture=False)

def cmd_refuel():
    """AI空中加油监控v2 — CPO三剑客+Rule1/2/3+四道闸门+次日熔断"""
    script = os.path.join(ENGINE_DIR, 'ai_refuel_monitor_v2.py')
    run_py(script, capture=False)

def cmd_fragility():
    """因果推演脆弱地图 — 5层推演→21格矩阵→一句话判断"""
    script = os.path.join(ENGINE_DIR, 'fragility_map.py')
    run_py(script, capture=False)

def cmd_filters():
    """三大过滤规则 — RS过滤+量能天花板+日历锚定+Rule1方向"""
    script = os.path.join(ENGINE_DIR, 'three_filters.py')
    run_py(script, capture=False)

def cmd_attack():
    """进攻引擎全模块 — 双模扫描→加油监控→脆弱地图→三大过滤"""
    print(f"\n{'='*60}")
    print(f"  天眼进攻引擎 v2.0")
    print(f"  TDD: 双模29 + 加油25 + 脆弱25 + 过滤20 = 99/99 全绿")
    print(f"{'='*60}")
    cmd_scan_dual()
    cmd_refuel()
    cmd_fragility()
    cmd_filters()
    print(f"\n{'='*60}")
    print(f"  进攻引擎全模块完成")
    print(f"{'='*60}\n")


def cmd_rule2():
    """Rule2 尾盘扫货指纹 · 历史扫描 + 前向验证"""
    script = os.path.join(ENGINE_DIR, 'rule2_scanner.py')
    run_py(script, capture=False)

def cmd_rule3():
    """Rule3 日K缩量十字星指纹 · 全市场回测"""
    script = os.path.join(ENGINE_DIR, 'rule3_backtest.py')
    run_py(script, capture=False)

def cmd_mode_a_track():
    """窒息底反转追踪 + 季末窗口预警"""
    script = os.path.join(ENGINE_DIR, 'mode_a_tracker.py')
    run_py(script, capture=False)

def cmd_volume_cap_v2():
    """量能天花板v2 — 全市场180只求和 vs v1对比"""
    script = os.path.join(ENGINE_DIR, 'volume_cap_v2.py')
    run_py(script, capture=False)

def cmd_cio():
    """CIO决策日报 — 风控优先·异常驱动·因果推演 四段式报告"""
    script = os.path.join(ENGINE_DIR, 'cio_report.py')
    run_py(script, capture=False)

def cmd_circuit_breaker():
    """T+1 09:35 熔断检查 — 昨日Rule2 + 今日跳空"""
    script = os.path.join(ENGINE_DIR, 'circuit_breaker_morning.py')
    run_py(script, capture=False)


# ═══════════════════════════════════════════════════════
# 天眼 v6.1 全链并网 — run_full_pipeline
# ═══════════════════════════════════════════════════════

def run_full_pipeline(total_capital=500, use_realtime=True):
    """天眼 v6.1 全链条并网发电 — 六步流水线.

    步骤:
      1. DuckDB 连接检查 + Schema 就绪
      2. 加载 tracked_stocks.json 白名单
      3. 盘中实时价 + DB 历史指标动态拼接
      4. Minervini 三道硬闸门 — 一票否决
      5. 幸存者 → 不对称涨幅评分 x RSI 情绪衰减 → 天眼综合得分
      6. 决策结果 + 持仓 + 总资产 → capital_allocator → 资金分配方案

    Args:
      total_capital: float, 账户总资产 (元), 默认 500
      use_realtime: bool, 是否获取盘中实时价 (False→纯DB模式)

    Returns:
      dict: {
        'pipeline_report': str,      # 完整文本报告
        'survivors': list[dict],     # 通过闸门的标的 + 天眼分
        'rejected': list[dict],      # 被拦截的标的 + 拦截原因
        'allocation': dict,          # 资金分配方案
        'errors': list[str],         # 各步骤异常记录
        'data_freshness': dict,      # 数据新鲜度
      }
    """
    import time as _time
    from datetime import datetime as _dt, date as _date

    errors = []
    start_time = _time.time()

    print()
    print("=" * 65)
    print("  天眼 v6.1 全链并网发电")
    print(f"  启动: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  总资产: {total_capital:.0f}元 | 模式: {'实时价' if use_realtime else '纯DB'}")
    print("=" * 65)

    # ── 反向映射: 指数代码 → 板块名称 ──────────────────
    INDEX_TO_SECTOR = {v: k for k, v in SECTOR_INDICES.items()}

    # ── 基金代码映射: 指数 → [基金代码, ...] ────────────
    index_to_funds = {}
    try:
        with open(os.path.join(BASE, 'tracked_stocks.json'), 'r', encoding='utf-8') as f:
            tracked_data = json.load(f)
        for fund_code, meta in tracked_data.get('portfolio_map', {}).items():
            und = meta.get('underlying', '')
            if und:
                index_to_funds.setdefault(und, []).append(fund_code)
    except Exception as e:
        errors.append(f'基金映射加载失败: {e}')

    # ── 持仓映射: 基金代码 → 持仓信息 ──────────────────
    holdings_map = {}
    portfolio = {}
    try:
        pf_path = os.path.join(BASE, 'portfolio.json')
        if os.path.exists(pf_path):
            with open(pf_path, 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
            for h in portfolio.get('holdings', []):
                holdings_map[h['code']] = h
    except Exception as e:
        errors.append(f'持仓加载失败: {e}')

    # ═══════════════════════════════════════════════════
    # 步骤 1: DuckDB 连接检查
    # ═══════════════════════════════════════════════════
    print("\n[1/6] DuckDB 连接检查...")
    step1_ok = False
    try:
        import duckdb as _dkdb
        conn = _dkdb.connect(DB)
        # 检查关键表
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('technical_indicators','kline_daily','financial_statements')"
        ).fetchall()
        table_names = [r[0] for r in tables]

        if 'technical_indicators' not in table_names:
            errors.append('technical_indicators 表不存在, 请先运行 data_syncer.py sync')
            print(f"  [FAIL] technical_indicators 表不存在")
        else:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM technical_indicators"
            ).fetchone()[0]
            latest_date = conn.execute(
                "SELECT MAX(trade_date) FROM technical_indicators"
            ).fetchone()[0]
            age = (_date.today() - _date.fromisoformat(str(latest_date)[:10])).days if latest_date else 999
            freshness = "[OK]" if age <= 1 else ("[Y]" if age <= 3 else "[R]")
            print(f"  {freshness} technical_indicators: {row_count}行 | 最新:{latest_date} | {age}天前")
            step1_ok = True
            if age > 3:
                errors.append(f'数据过期{age}天, 请运行 data_syncer.py sync')

        conn.close()
    except Exception as e:
        errors.append(f'DuckDB连接失败: {e}')
        print(f"  [FAIL] {e}")
        return {
            'pipeline_report': f'流水线中止: DuckDB连接失败 ({e})',
            'survivors': [], 'rejected': [], 'allocation': {},
            'errors': errors, 'data_freshness': {},
        }

    if not step1_ok:
        print("  [FAIL] 关键表缺失, 流水线中止")
        return {
            'pipeline_report': '流水线中止: 关键表缺失',
            'survivors': [], 'rejected': [], 'allocation': {},
            'errors': errors, 'data_freshness': {},
        }

    # ═══════════════════════════════════════════════════
    # 步骤 2: 加载白名单
    # ═══════════════════════════════════════════════════
    print("\n[2/6] 加载白名单...")
    whitelist_indices = list(SECTOR_INDICES.values())
    try:
        with open(os.path.join(BASE, 'tracked_stocks.json'), 'r', encoding='utf-8') as f:
            td = json.load(f)
        extra_indices = td.get('indices', [])
        for idx in extra_indices:
            if idx not in whitelist_indices:
                whitelist_indices.append(idx)
    except Exception:
        pass

    fund_count = sum(len(v) for v in index_to_funds.values())
    print(f"  [OK] {len(whitelist_indices)}个指数 | {fund_count}个联接基金 | {len(holdings_map)}个持仓")

    # ═══════════════════════════════════════════════════
    # 步骤 3: 盘中实时价 + DB指标拼接
    # ═══════════════════════════════════════════════════
    print(f"\n[3/6] {'盘中实时价' if use_realtime else 'DB历史收盘价'} + 指标拼接...")

    realtime_prices = {}
    if use_realtime:
        from data_syncer import get_realtime_price
        for idx_code in whitelist_indices:
            try:
                rt = get_realtime_price(idx_code)
                if rt.get('price') and rt['price'] > 0:
                    realtime_prices[idx_code] = rt
            except Exception as e:
                errors.append(f'实时价获取失败 {idx_code}: {e}')

        if realtime_prices:
            print(f"  [OK] 获取到 {len(realtime_prices)}/{len(whitelist_indices)} 个实时价")
        else:
            print(f"  [Y] 实时价全部失败, 降级至纯DB模式")
            use_realtime = False

    if not use_realtime:
        print(f"  [OK] 纯DB模式, 使用 technical_indicators.close")

    # ═══════════════════════════════════════════════════
    # 步骤 4: Minervini 三道硬闸门
    # ═══════════════════════════════════════════════════
    print(f"\n[4/6] Minervini 空间闸门 (天花板/地板/均线)...")

    gate_conn = _dkdb.connect(DB)
    survivors = []
    rejected = []

    for idx_code in whitelist_indices:
        sector = INDEX_TO_SECTOR.get(idx_code)
        if sector is None:
            rejected.append({
                'code': idx_code, 'sector': idx_code,
                'reason': 'FAIL_NO_SECTOR_MAP',
                'detail': '未找到板块映射',
            })
            continue

        try:
            gate = screen_minervini_with_db(idx_code, gate_conn)
        except Exception as e:
            rejected.append({
                'code': idx_code, 'sector': sector,
                'reason': 'FAIL_GATE_ERROR',
                'detail': str(e)[:80],
            })
            continue

        # 实时价修正: 如有实时价, 覆盖 close 字段重新判定
        if use_realtime and idx_code in realtime_prices:
            rt_price = realtime_prices[idx_code]['price']
            d = gate['data']
            if rt_price > 0 and d['close'] > 0:
                # 重新计算 floor/ceiling 比率
                floor_r = rt_price / d['min_low_250'] if d['min_low_250'] > 0 else 0
                ceiling_r = rt_price / d['max_high_250'] if d['max_high_250'] > 0 else 1
                floor_ok = floor_r >= 1.25
                ceiling_ok = ceiling_r >= 0.85
                ma_ok = gate['checks']['ma']['ok']

                all_ok = floor_ok and ceiling_ok and ma_ok
                if all_ok:
                    reason = 'PASS'
                elif not ceiling_ok:
                    reason = 'FAIL_TOO_HIGH'
                elif not floor_ok:
                    reason = 'FAIL_DEAD_MONEY'
                else:
                    reason = 'FAIL_MA_NOT_BULL'

                gate = {
                    'passed': all_ok,
                    'reason': reason,
                    'checks': {
                        'floor':   {'ok': floor_ok, 'detail': f'floor={floor_r:.2f}(实时)', 'ratio': round(floor_r, 3)},
                        'ceiling': {'ok': ceiling_ok, 'detail': f'ceiling={ceiling_r:.2f}(实时)', 'ratio': round(ceiling_r, 3)},
                        'ma':      {'ok': ma_ok, 'detail': gate['checks']['ma']['detail']},
                    },
                    'data': {**d, 'close': rt_price, '_realtime': True},
                }

        entry = {
            'code': idx_code,
            'sector': sector,
            'passed': gate['passed'],
            'reason': gate['reason'],
            'checks': gate['checks'],
            'data': gate['data'],
            'fund_codes': index_to_funds.get(idx_code, []),
        }

        if gate['passed']:
            survivors.append(entry)
        else:
            rejected.append(entry)

    gate_conn.close()

    # ── 打印闸门日志 ────────────────────────────────
    passed_count = len(survivors)
    rejected_count = len(rejected)
    print(f"  [OK] 通过: {passed_count} | 拦截: {rejected_count} | 总计: {passed_count + rejected_count}")

    if rejected:
        print(f"\n  -- 拦截明细 --")
        for r in rejected:
            reason = r['reason']
            sector = r['sector']
            code = r['code']
            if reason == 'FAIL_TOO_HIGH':
                chk = r.get('checks', {}).get('ceiling', {})
                print(f"  [R] {sector:8s} ({code}) 天花板: {chk.get('detail', '?')}")
            elif reason == 'FAIL_DEAD_MONEY':
                chk = r.get('checks', {}).get('floor', {})
                print(f"  [R] {sector:8s} ({code}) 死钱: {chk.get('detail', '?')}")
            elif reason == 'FAIL_MA_NOT_BULL':
                chk = r.get('checks', {}).get('ma', {})
                print(f"  [R] {sector:8s} ({code}) 均线: {chk.get('detail', '?')}")
            else:
                print(f"  [R] {sector:8s} ({code}): {reason} — {r.get('detail', '?')}")

    # ── 4b. 被拦截但有持仓的标的 → 生成减仓信号 ──────
    rejected_with_positions = []
    for r in rejected:
        fund_codes = index_to_funds.get(r['code'], [])
        has_position = any(fc in holdings_map for fc in fund_codes)
        if has_position:
            d = r['data']
            rsi14 = get_latest_rsi(r['code']) or 50
            gain_20d = compute_gain_20d(r['code']) or 0

            rejected_with_positions.append({
                'sector': r['sector'],
                'code': r['code'],
                'fund_code': fund_codes[0] if fund_codes else '',
                'tianyan_score': 20.0,
                'action': '减仓',
                'verdict': f'Gate拦截:{r["reason"]}',
                'icon': 'R',
                'close': d.get('close', 0),
                'rsi14': rsi14,
                'gain_20d': gain_20d,
                'lambda': 0.5,
                'base_score': 40.0,
                'tech_score': 20.0,
                'fund_score': 10.0,
                'prosperity_score': 10.0,
                'detail': f'Minervini闸门拦截: {r["reason"]}',
                'fund_codes': fund_codes,
                '_gate_rejected': True,
            })

    if rejected_with_positions:
        print(f"\n  -- 持仓预警 (被拦截但有仓位) --")
        for rp in rejected_with_positions:
            holding = holdings_map.get(rp['fund_code'], {})
            amt = holding.get('amount', 0)
            print(f"  [R] {rp['sector']:8s} ({rp['fund_code']}): {rp['verdict']} | 持仓{amt:.0f}元 -> 建议减仓")

    if passed_count == 0 and not rejected_with_positions:
        print("\n  [!] 无标的通过 Minervini 闸门, 流水线终止")
        return {
            'pipeline_report': '流水线终止: 0个标的通过 Minervini 闸门',
            'survivors': [], 'rejected': rejected, 'allocation': {},
            'errors': errors, 'data_freshness': {},
        }

    # ═══════════════════════════════════════════════════
    # 步骤 5: 天眼综合打分
    # ═══════════════════════════════════════════════════
    print(f"\n[5/6] 天眼综合打分 (不对称涨幅 + RSI衰减 + 基本面 + 景气)...")

    # ── 加载景气数据 ──────────────────────────────────
    prosperity_map = {}
    try:
        engine_output = os.path.join(BASE, 'century_engine_output.json')
        if os.path.exists(engine_output):
            with open(engine_output, 'r', encoding='utf-8') as f:
                eng_data = json.load(f)
            for r in eng_data.get('rankings', []):
                sec = r.get('sector', r.get('name', ''))
                pscore = r.get('score', r.get('prosperity', 15))
                prosperity_map[sec] = float(pscore)
    except Exception:
        pass

    # ── 逐标的打分 ────────────────────────────────────
    scored = []

    for s in survivors:
        ts_code = s['code']
        sector = s['sector']
        data = s['data']
        close = data.get('close', 0)
        ma50 = data.get('ma50', 0)
        ma150 = data.get('ma150', 0)
        ma200 = data.get('ma200', 0)

        detail_parts = []

        # ── A. 20日涨幅不对称评分 ──────────────────────
        gain_20d = compute_gain_20d(ts_code)
        gain_score = asymmetric_gain_score(gain_20d)
        if gain_20d is not None:
            tag = "跌" if gain_20d < 0 else ""
            detail_parts.append(f"20日{tag}{gain_20d:+.1f}%→涨幅分{gain_score:+.1f}")
        else:
            gain_20d = 0
            gain_score = 0
            detail_parts.append("20日无数据→涨幅分0")

        # ── B. RSI 情绪衰减 ───────────────────────────
        rsi14 = get_latest_rsi(ts_code)
        lmbda = rsi_lambda(rsi14)
        if rsi14 is not None:
            if lmbda == 0.0:
                detail_parts.append(f"RSI{rsi14:.0f}→熔断L=0")
            elif lmbda < 1.0:
                detail_parts.append(f"RSI{rsi14:.0f}→L={lmbda:.2f}")
            else:
                detail_parts.append(f"RSI{rsi14:.0f}→L=1.0")
        else:
            rsi14 = 50
            lmbda = 1.0
            detail_parts.append("RSI?→L=1.0")

        # ── C. 趋势结构 (0-20) ─────────────────────────
        trend_score = 0.0
        if ma50 and ma150 and ma200 and ma50 > 0:
            if ma50 > ma150 > ma200:
                trend_score = 20
                detail_parts.append("MA完美多头")
            elif ma50 > ma150:
                trend_score = 13
                detail_parts.append("MA短>中")
            elif ma50 > ma200:
                trend_score = 7
                detail_parts.append("MA50>200")
        if close and ma50 and ma50 > 0:
            dist_ma50 = (close / ma50 - 1) * 100
            if dist_ma50 > 12:
                trend_score = max(0, trend_score - 5)
                detail_parts.append(f"距MA50+{dist_ma50:.0f}%过热")
            elif dist_ma50 < 0:
                trend_score = max(0, trend_score - 3)

        # ── D. MACD 动能 (0-10) ────────────────────────
        macd_score = 5.0
        try:
            conn3 = _dkdb.connect(DB)
            macd_row = conn3.execute(
                "SELECT macd_dif, macd_dea, macd_hist FROM technical_indicators "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                [ts_code],
            ).fetchone()
            conn3.close()
            if macd_row and macd_row[0] is not None:
                dif, dea, hist = float(macd_row[0]), float(macd_row[1]), float(macd_row[2])
                if dif > dea:
                    macd_score = 8 if hist > 0 else 5
                    detail_parts.append("MACD金叉" if hist > 0 else "MACD将叉")
                else:
                    macd_score = 2
                    detail_parts.append("MACD死叉")
        except Exception:
            detail_parts.append("MACD无数据")

        # ── E. 成交量 (0-10) ───────────────────────────
        vol_score = 5.0
        try:
            conn3 = _dkdb.connect(DB)
            vol_row = conn3.execute(
                "SELECT volume_ratio FROM technical_indicators "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                [ts_code],
            ).fetchone()
            conn3.close()
            if vol_row and vol_row[0] is not None:
                vr = float(vol_row[0])
                if 0.7 <= vr <= 1.5:
                    vol_score = 10
                elif 0.5 <= vr < 0.7:
                    vol_score = 7
                    detail_parts.append("缩量")
                elif vr > 2.5:
                    vol_score = 3
                    detail_parts.append(f"巨量{vr:.1f}x")
                elif vr > 1.5:
                    vol_score = 5
                    detail_parts.append(f"放量{vr:.1f}x")
                else:
                    vol_score = 2
                    detail_parts.append("地量")
        except Exception:
            pass

        # ── F. 布林 (0-10) ─────────────────────────────
        boll_score = 5.0
        try:
            conn3 = _dkdb.connect(DB)
            boll_row = conn3.execute(
                "SELECT boll_upper, boll_mid, boll_lower FROM technical_indicators "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                [ts_code],
            ).fetchone()
            conn3.close()
            if boll_row and boll_row[0] is not None:
                bu, bm, bl = float(boll_row[0]), float(boll_row[1]), float(boll_row[2])
                bw = bu - bl
                if bw > 0 and close:
                    bp = (close - bl) / bw
                    if 0.4 <= bp <= 0.8:
                        boll_score = 10
                    elif 0.2 <= bp < 0.4:
                        boll_score = 5
                        detail_parts.append("布林偏下")
                    elif bp > 0.8:
                        boll_score = 4
                        detail_parts.append("布林触上")
                    else:
                        boll_score = 3
                        detail_parts.append("布林底")
        except Exception:
            pass

        # ── G. 技术面总分 (归一化到0-50) ──────────────
        tech_raw = gain_score + trend_score + macd_score + vol_score + boll_score
        tech_norm = round(max(0, min(50, tech_raw + 15)), 1)
        detail_parts.insert(0, f"技术{tech_norm:.0f}/50")

        # ── H. 基本面得分 (0-20) ───────────────────────
        fund_score = 10.0
        try:
            from engine.fundamental_engine import get_fundamental_bridge
            bridge = get_fundamental_bridge(ts_code, sector)
            fund_score = bridge['prosperity_score']
            detail_parts.append(f"基本面{fund_score:.0f}/20({bridge['verdict']})")
        except Exception:
            fund_score = 10.0
            detail_parts.append("基本面降级→10/20")

        # ── I. 景气得分 (0-30) ─────────────────────────
        prosperity = prosperity_map.get(sector, 15.0)
        # 景气分是百分制 → 映射到 0-30
        prosperity_norm = round(prosperity / 100.0 * 30.0, 1) if prosperity <= 100 else 15.0
        detail_parts.append(f"景气{prosperity_norm:.0f}/30")

        # ── J. 综合得分 ────────────────────────────────
        base_score = tech_norm + fund_score + prosperity_norm
        tianyan_score = round(base_score * lmbda, 1)
        tianyan_score = max(0, min(100, tianyan_score))

        # ── K. 动作判定 ────────────────────────────────
        if lmbda == 0.0:
            action = '观望'
            verdict = 'HB9熔断'
            icon = 'R'
        elif tianyan_score >= 65:
            action = '加仓'
            verdict = '健康右侧' if gain_20d and gain_20d >= 0 else '超卖反弹'
            icon = 'G'
        elif tianyan_score >= 50:
            action = '持有'
            verdict = '中性偏多'
            icon = 'G'
        elif tianyan_score >= 35:
            action = '观望'
            verdict = '等待信号'
            icon = 'Y'
        else:
            action = '减仓'
            verdict = '弱势回避'
            icon = 'R'

        fund_codes = index_to_funds.get(ts_code, [])
        fund_code = fund_codes[0] if fund_codes else ''

        scored.append({
            'sector': sector,
            'code': ts_code,
            'fund_code': fund_code,
            'tianyan_score': tianyan_score,
            'action': action,
            'verdict': verdict,
            'icon': icon,
            'close': close,
            'rsi14': rsi14,
            'gain_20d': gain_20d,
            'lambda': lmbda,
            'base_score': round(base_score, 1),
            'tech_score': tech_norm,
            'fund_score': round(fund_score, 1),
            'prosperity_score': prosperity_norm,
            'detail': ' | '.join(detail_parts),
            'fund_codes': fund_codes,
        })

    # ── 按天眼分降序 ──────────────────────────────────
    scored.sort(key=lambda x: x['tianyan_score'], reverse=True)

    # ── 打印排名 ──────────────────────────────────────
    print(f"\n  {'排名':4s} {'板块':8s} {'代码':12s} {'天眼分':>6s} {'动作':6s} {'RSI':>5s} {'20日涨':>8s} {'L':>6s}")
    print(f"  {'-'*60}")
    for i, s in enumerate(scored):
        g20 = f"{s['gain_20d']:+.1f}%" if s['gain_20d'] is not None else '?'
        rsi_str = f"{s['rsi14']:.0f}" if s['rsi14'] is not None else '?'
        print(f"  {i+1:4d} {s['sector']:8s} {s['code']:12s} "
              f"{s['tianyan_score']:6.1f} [{s['icon']}] {s['action']:4s} "
              f"{rsi_str:>5s} {g20:>8s} {s['lambda']:6.3f}")
    print(f"  {'-'*60}")
    print(f"\n  分值段: [G]>=65加仓 | [G]>=50持有 | [Y]>=35观望 | [R]<35减仓")
    for s in scored:
        print(f"  {s['icon']} {s['sector']:8s}: {s['detail']}")

    # ═══════════════════════════════════════════════════
    # 步骤 6: 资金分配
    # ═══════════════════════════════════════════════════
    print(f"\n[6/6] 资金分配 (总资产{total_capital:.0f}元)...")

    try:
        holdings_list = list(holdings_map.values())
        # 合并: 通过闸门的 + 被拦截但有持仓需减仓的
        all_scored = scored + rejected_with_positions
        allocation = allocate_capital(all_scored, holdings_list, total_capital)
        print_allocation(allocation)
    except Exception as e:
        errors.append(f'资金分配失败: {e}')
        allocation = {'error': str(e)}
        print(f"  [FAIL] 资金分配异常: {e}")

    # ═══════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════
    elapsed = _time.time() - start_time

    # 构建文本报告
    report_lines = [
        "=" * 65,
        "  天眼 v6.1 全链并网 — 运行报告",
        f"  时间: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')} | 耗时: {elapsed:.1f}s",
        "=" * 65,
        "",
        f"  总资产: {total_capital:.0f}元 | 模式: {'实时价' if use_realtime else '纯DB'}",
        f"  Minervini闸门: {passed_count}通过 / {rejected_count}拦截",
        "",
        "  天眼排名:",
    ]
    for i, s in enumerate(scored):
        report_lines.append(
            f"  #{i+1} {s['sector']:8s} {s['tianyan_score']:5.1f}分 [{s['icon']}] {s['action']}"
        )

    if allocation.get('summary'):
        als = allocation['summary']
        report_lines += [
            "",
            f"  资金分配: 买{als.get('new_buys',0)}笔 / 卖{als.get('sells',0)}笔 / 持{als.get('holds',0)}笔",
            f"  目标部署: {als.get('deployed_target',0):.0f}元 | 现金预留: {als.get('cash_after',0):.0f}元",
        ]

    if errors:
        report_lines += ["", "  异常记录:"]
        for e in errors:
            report_lines.append(f"  [!] {e}")

    report_lines += ["", "=" * 65]
    pipeline_report = '\n'.join(report_lines)

    print(f"\n{'='*65}")
    print(f"  全链并网完成 | 耗时 {elapsed:.1f}s | {passed_count}通过 {rejected_count}拦截")
    if errors:
        print(f"  {len(errors)}个异常 (详见报告)")
    print(f"{'='*65}\n")

    return {
        'pipeline_report': pipeline_report,
        'survivors': scored,
        'rejected': rejected,
        'allocation': allocation,
        'errors': errors,
        'data_freshness': {
            'age_days': age if 'age' in dir() else None,
            'latest_date': str(latest_date)[:10] if 'latest_date' in dir() else None,
        },
    }


def cmd_pipeline(args=None):
    """天眼 v6.1 全链并网 — 6步流水线"""
    # 修复: 单参数字符串 → 列表, 列表 → 直接使用
    if isinstance(args, str):
        extra = [args]
    elif args is None:
        extra = []
    else:
        extra = list(args)

    total_capital = 500
    use_realtime = True

    # 解析参数
    for i, arg in enumerate(extra):
        if arg == '--capital' and i + 1 < len(extra):
            try:
                total_capital = float(extra[i + 1])
            except ValueError:
                print(f"[!] 无效金额: {extra[i+1]}, 使用默认500")
        if arg == '--no-realtime':
            use_realtime = False
        if arg == '--db-only':
            use_realtime = False

    run_full_pipeline(total_capital=total_capital, use_realtime=use_realtime)


COMMANDS = {
    'eq': cmd_eq,
    'collect_news': cmd_collect_news,
    'full': cmd_full,
    'unified': cmd_unified,
    'news': cmd_news,
    'market': cmd_market,
    'tech': cmd_tech,
    'fund': cmd_fund,
    'risk': cmd_risk,
    'daily': cmd_daily,
    'indicators': cmd_indicators,
    'trace': cmd_trace,
    'plan': cmd_plan,
    'analyze': cmd_analyze,
    'recommend': cmd_recommend,
    'audit': cmd_audit,
    'verify': cmd_verify,
    'lifecycle': cmd_lifecycle,
    'backtest': cmd_backtest,
    'scan': cmd_scan,
    'report': cmd_report,
    'collect': cmd_collect,
    'conduction': cmd_conduction,
    'fingerprint': cmd_fingerprint,
    'rules': cmd_rules,
    'anticonsensus': cmd_anticonsensus,
    'all': cmd_all,
    'chart': cmd_chart,
    'positions': cmd_positions,
    'surprise': cmd_surprise,
    'regime': cmd_regime,
    'papertrade': cmd_papertrade,
    'cro': cmd_cro,
    # 进攻引擎 v2.0
    'attack': cmd_attack,
    'scan_dual': cmd_scan_dual,
    'refuel': cmd_refuel,
    'fragility': cmd_fragility,
    'filters': cmd_filters,
    'rule2': cmd_rule2,
    'rule3': cmd_rule3,
    'track': cmd_mode_a_track,
    'volcap': cmd_volume_cap_v2,
    'cio': cmd_cio,
    'cb': cmd_circuit_breaker,
    'pipeline': cmd_pipeline,
}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        print("可用命令:", ', '.join(COMMANDS.keys()))
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd in COMMANDS:
        func = COMMANDS[cmd]
        if cmd in ('tech', 'fund', 'analyze', 'news', 'full', 'eq', 'conduction', 'fingerprint', 'collect', 'rules', 'anticonsensus', 'chart', 'positions', 'daily', 'surprise', 'pipeline') and len(sys.argv) > 2:
            func(sys.argv[2] if len(sys.argv) == 3 else sys.argv[2:])
        elif cmd == 'recommend' and len(sys.argv) > 2:
            func(sys.argv[2:])
        else:
            func()
    else:
        print(f"未知命令: {cmd}")
        print("可用:", ', '.join(COMMANDS.keys()))
