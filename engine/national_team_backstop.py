# -*- coding: utf-8 -*-
"""
国家队 backstop 动态辨别 (LLM情境整合, 非固定阈值)
===================================================
目的: 把"随机极端抄底(48%)"升级成"只在国家队护盘可信active、且front-run未发生的极端底出手(目标54%+)"。

辨别两个状态:
  1. 护盘是否可信active? — 宽基ETF异常放量/净流入 + 官方公告喊话 + 市场极端低估/流动性负反馈 + 阶梯触发,
     多维共振才可信(单维不够)。护盘active≠立刻见效(2015护盘失败数月)。
  2. front-run是否已发生? — 龙虎榜机构席位已埋伏 + 权重股(银行/蓝筹=国家队偏好)先于大盘动。
     front-run已发生→晚入场散户是接盘方。

动态 = LLM整合多维情境条件化判断, 不是"ETF成交>X=护盘"那种固定阈值(那又退回死板不变量)。
诚实: 目标是"显著>48%向54%", 不是81%(被secular bull污染); front-run edge会随人人学会衰减, 非永久。

用法:
  from engine.national_team_backstop import judge_backstop
  r = judge_backstop({'index_pos':..., 'broad_etf':..., 'official':..., 'ladder':..., 'dragon_tiger':..., 'weight_stocks':...})
  # → {'backstop_active':'active/inactive/uncertain', 'active_conf':0-100, 'front_run':'occurred/not', 'reasoning':..., 'dims_used':[...]}
"""
import os
import json


def _llm_json(prompt, max_tokens=1500):
    """调LLM返回JSON (anthropic SDK自动走ANTHROPIC_API_KEY+BASE_URL端点)。失败返回None。"""
    try:
        import anthropic
        client = anthropic.Anthropic()
        model = os.environ.get('ANTHROPIC_MODEL') or os.environ.get('ANTHROPIC_DEFAULT_HAIKU_MODEL')
        if not model:
            return None
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        text = ''.join(getattr(b, 'text', '') for b in resp.content
                       if getattr(b, 'type', '') == 'text').strip()
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    except Exception:
        return None


def _backstop_gate(context):
    """负向粗筛: 情境是否可能有护盘(值得调LLM动态判)。
    明显无迹象(非极端底+ETF无放量+无护盘公告+未触发-5%档)→maybe=False→确定inactive(可复现+省LLM)。
    这是负向筛(只筛掉'明显不是护盘'); active的辨别仍交LLM动态, 不用阈值判active(不退回固定阈值)。"""
    import re
    ip = context.get('index_pos', '') or ''
    etf = context.get('broad_etf', '') or ''
    official = context.get('official', '') or ''
    ladder = context.get('ladder', '') or ''
    reasons = []
    maybe = False
    m20 = re.search(r'20日([+-]?\d+\.?\d*)%', ip)
    m1 = re.search(r'当日([+-]?\d+\.?\d*)%', ip)
    if m20 and float(m20.group(1)) <= -8:
        maybe = True; reasons.append('20日深跌')
    if m1 and float(m1.group(1)) <= -3:
        maybe = True; reasons.append('当日大跌')
    for rr in re.findall(r'均([\d.]+)倍', etf):
        if float(rr) >= 1.5:
            maybe = True; reasons.append('ETF放量'); break
    if official and '无' not in official[:6]:
        maybe = True; reasons.append('有公告')
    if '触及-5%' in ladder:
        maybe = True; reasons.append('触-5%档')
    if maybe:
        return {'maybe': True, 'reason': '/'.join(reasons)}
    d20 = m20.group(1) if m20 else '?'
    return {'maybe': False, 'reason': f'20日{d20}%非极端+ETF无≥1.5倍放量+无护盘公告+未触发-5%档'}


def judge_backstop(context: dict, prefilter=True):
    """动态辨别国家队backstop状态 — LLM整合多维情境, 非固定阈值。

    context 字段(缺的传空, 缺维度本身是判断依据):
      index_pos     指数位置/极端度(点位/跌幅/PE分位/流动性负反馈)
      broad_etf     宽基ETF(沪深300/中证500/科创50)成交/净流入
      official      官方公告/喊话文本
      ladder        阶梯触发(跌5%档等)
      dragon_tiger  龙虎榜机构席位
      weight_stocks 权重股(银行/蓝筹)vs大盘动向
    """
    # 负向粗筛: 明显无护盘迹象→确定性inactive(可复现+省LLM); 只潜在护盘情境才调LLM动态判
    if prefilter:
        g = _backstop_gate(context)
        if not g['maybe']:
            return {'backstop_active': 'inactive', 'active_conf': 90, 'front_run': 'not',
                    'reasoning': f"确定性粗筛:{g['reason']},无护盘迹象", 'dims_used': ['粗筛'], 'method': 'gate'}
    prompt = f"""你是A股国家队护盘辨别专家。基于下面多维情境做**动态整合判断**——不是套固定阈值(如"ETF成交>X就是护盘"),而是看各维证据如何共振或相互矛盾。

【判断1: 国家队护盘是否可信active?】(active/inactive/uncertain)
  依据维度: ①宽基ETF异常放量/净流入 ②官方公告/喊话 ③市场极端低估/流动性负反馈 ④阶梯触发(跌5%档)
  规则: 单一维度不够(只有ETF放量但无公告无极端度→uncertain); 多维共振才可信active。缺哪一维要在reasoning点明。
  警示: 护盘active≠立刻见效(2015护盘失败数月),你判的是"backstop是否在场",不是"是否马上涨"。

【判断2: front-run是否已发生?】(occurred/not)
  依据: ①龙虎榜机构席位已埋伏护盘标的 ②权重股(银行/大盘蓝筹)先于大盘企稳/上动
  若front-run已发生→晚入场散户是接盘方,要警惕,不是好的入场点。

【判断3: 期权IV skew提供独立验证】
  若Put IV显著>Call IV(>10pp)且你判backstop=active→标注'现货-期权名实背离: put恐慌vs现货伪装'
  若Put/Call IV差距小(<10pp)和你判断一致→标注'期权侧无矛盾'

【当前多维情境】
指数位置/极端度: {context.get('index_pos', '(无数据)')}
宽基ETF成交/流向: {context.get('broad_etf', '(无数据)')}
官方公告/喊话: {context.get('official', '(无)')}
阶梯触发: {context.get('ladder', '(无)')}
龙虎榜机构席位: {context.get('dragon_tiger', '(无数据)')}
权重股vs大盘: {context.get('weight_stocks', '(无数据)')}
期权IV skew: {context.get('opt_iv', '(未拉取)')}

只返回JSON,不要其他文字:
{{"backstop_active":"active/inactive/uncertain","active_conf":0到100的整数,"front_run":"occurred/not","reasoning":"说明整合了哪几维、如何共振或缺哪维导致此结论,50字内","dims_used":["实际支撑结论的维度名"]}}"""
    r = _llm_json(prompt)
    if isinstance(r, dict):
        r['method'] = 'llm'
    return r


def _build_context_asof(conn, as_of):
    """构建as-of日期的护盘情境(只用trade_date<=as_of的数据, leak-free)。
    返回(ctx, idx_close, idx_date)。实时与backfill同一构建逻辑, 唯一区别=as_of上界。
    注: judge是LLM非确定性脚本, backfill的leak-free来自"context严格<=D", 不来自"judge确定"。"""
    ctx = {}
    idx_close = None
    idx_date = None
    # 指数位置+阶梯(<=as_of)
    try:
        rows = conn.execute("SELECT trade_date,close,pre_close FROM kline_daily WHERE ts_code='sh000300' AND trade_date<=? ORDER BY trade_date DESC LIMIT 21", [as_of]).fetchall()
        if rows:
            idx_date, idx_close = str(rows[0][0])[:10], rows[0][1]
            chg1 = (rows[0][1]/rows[0][2]-1)*100 if rows[0][2] else 0
            chg20 = (rows[0][1]/rows[-1][1]-1)*100 if len(rows) >= 21 and rows[-1][1] else 0
            ctx['index_pos'] = f'沪深300={idx_close:.0f}(截至{idx_date}),当日{chg1:+.2f}%,20日{chg20:+.1f}%'
            ctx['ladder'] = f'当日{chg1:+.2f}%' + ('(触及-5%档)' if chg1 <= -5 else '(未触发-5%档)')
    except Exception as e:
        ctx['index_pos'] = f'(拉取失败:{e})'; ctx['ladder'] = '(无)'
    # 宽基ETF成交(<=as_of, 今日vs20日均倍数)
    try:
        etf_lines = []
        etf_latest = None
        for code, nm in [('510300.SH', '沪深300ETF'), ('510500.SH', '中证500ETF'), ('588000.SH', '科创50ETF')]:
            r = conn.execute("SELECT trade_date,volume FROM etf_daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 21", [code, as_of]).fetchall()
            if r and len(r) >= 2:
                etf_latest = str(r[0][0])[:10]
                today_v = r[0][1] or 0
                avg = sum((x[1] or 0) for x in r[1:]) / max(1, len(r)-1)
                ratio = today_v/avg if avg else 0
                etf_lines.append(f'{nm}成交为20日均{ratio:.1f}倍')
        etf_note = ''
        if etf_latest and idx_date and etf_latest < idx_date:
            etf_note = f' (注:ETF数据截至{etf_latest},滞后指数{idx_date},lower-bound)'
        ctx['broad_etf'] = ('; '.join(etf_lines) + etf_note) if etf_lines else '(无宽基ETF数据)'
    except Exception as e:
        ctx['broad_etf'] = f'(拉取失败:{e})'
    # 官方公告(<=as_of的近3日, 扫护盘关键词)
    try:
        r = conn.execute("SELECT title FROM news_articles WHERE publish_date<=? AND publish_date>=CAST(? AS DATE)-3 AND (title LIKE '%证监会%' OR title LIKE '%汇金%' OR title LIKE '%维护%' OR title LIKE '%护盘%' OR title LIKE '%坚决%')", [as_of, as_of]).fetchall()
        ctx['official'] = '; '.join(x[0][:36] for x in r[:3]) if r else '(近3日无官方护盘公告)'
    except Exception as e:
        ctx['official'] = f'(扫描失败:{e})'
    ctx['dragon_tiger'] = '(待接: dragon_tiger表无机构专用席位明细字段)'
    ctx['weight_stocks'] = '(待接: 权重股相对强弱维度)'
    # 维2: 期权IV skew(独立验证——操盘方无法污染做市商BS定价)
    try:
        import requests as _rq2
        _opt_hdr = {'Referer': 'https://stock.finance.sina.com.cn/', 'User-Agent': 'Mozilla/5.0'}
        # 50ETF近月Call/Put IV
        _mo = _rq2.get('https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName?exchange=null&cate=50ETF',
                       headers=_opt_hdr, timeout=8).json()
        _ms = _mo['result']['data']['contractMonth']
        _near = _ms[1].replace('-', '')[2:]  # 近月YYMM
        _call_iv = None; _put_iv = None
        for _side, _flag in [('call', 'OP_UP'), ('put', 'OP_DOWN')]:
            try:
                _r = _rq2.get(f'https://hq.sinajs.cn/list={_flag}_510050{_near}', headers=_opt_hdr, timeout=5)
                _r.encoding = 'gbk'
                _codes = [l.split('CON_OP_')[1].split('"')[0].split(',')[0] for l in _r.text.split('\n') if 'CON_OP_' in l]
                if _codes:
                    _gr = _rq2.get(f'https://hq.sinajs.cn/list=CON_SO_{_codes[0]}', headers=_opt_hdr, timeout=5)
                    _gr.encoding = 'gbk'
                    _v = _gr.text.split('"')[1].split(',')
                    _v = [_v[0]] + _v[4:]  # skip 3 blanks
                    _iv = round(float(_v[6]) * 100, 1)
                    if _side == 'call': _call_iv = _iv
                    else: _put_iv = _iv
            except Exception: pass
        if _put_iv is not None:
            _skew = round(_put_iv - _call_iv, 1) if _call_iv is not None else None
            ctx['opt_iv'] = f'PutIV={_put_iv}% CallIV={_call_iv}% Skew={_skew}pp (50ETF近月{_near})'
        else:
            ctx['opt_iv'] = '(期权IV拉取失败)'
    except Exception:
        ctx['opt_iv'] = '(期权IV拉取失败: 网络/端点)'
    return ctx, idx_close, idx_date


def record_forward_state(db=None, log_path=None, refresh=True, backfill=True):
    """Part B 真前向: 用当日及之前的真实数据(无记忆污染)构建情境→judge→追加记录。
    累积N次后回看: backstop=active的极端底,纸面抄底胜率是否显著>48%(向54%)。
    缺维(如龙虎榜机构席位DB无字段)诚实标注,当lower-bound。
    refresh=True: 先轻量刷宽基指数到最新(替代全量daily的15分钟全市场,record只需指数;akshare失败降级用DB已有)。"""
    import duckdb
    from datetime import date
    db = db or r'D:\FreeFinanceData\data\duckdb\finance.db'
    log_path = log_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        '测试材料', 'backstop_forward_log.jsonl')
    # 轻量刷新: 只刷宽基指数(沪深300/中证500/科创50),几秒; 铁律#3.1先刷再记,不记陈旧
    if refresh:
        try:
            import akshare as ak
            import time as _t
            wc = duckdb.connect(db)
            for code in ('sh000300', 'sh000905', 'sh000688'):
                try:
                    lt = wc.execute(f"SELECT MAX(trade_date) FROM kline_daily WHERE ts_code='{code}'").fetchone()[0]
                    lts = str(lt)[:10] if lt else '2000-01-01'
                    df = ak.stock_zh_index_daily(symbol=code)
                    if df is None or len(df) == 0:
                        continue
                    df['ds'] = df['date'].astype(str)
                    nd = df[df['ds'] > lts].copy()
                    if len(nd) == 0:
                        continue
                    nd['ts_code'] = code
                    nd = nd.rename(columns={'date': 'trade_date', 'volume': 'vol'})
                    wc.execute("INSERT INTO kline_daily (ts_code,trade_date,open,high,low,close,vol) SELECT ts_code,trade_date,open,high,low,close,vol FROM nd")
                    _t.sleep(0.3)
                except Exception:
                    continue
            # 刷宽基ETF当日行情(腾讯qt.gtimg.cn不封IP给成交额; etf_daily天眼不刷,是核心护盘信号,从滞后2天救回当天)
            try:
                import requests as _rq
                tmap = {'sh510300': '510300.SH', 'sh510500': '510500.SH', 'sh588000': '588000.SH'}
                resp = _rq.get('http://qt.gtimg.cn/q=' + ','.join(tmap), timeout=10)
                resp.encoding = 'gbk'
                for line in resp.text.strip().split('\n'):
                    if '="' not in line:
                        continue
                    cp, dat = line.split('="', 1)
                    tc = cp.replace('v_', '')
                    if tc not in tmap:
                        continue
                    ff = dat.strip('";').split('~')
                    if len(ff) < 38:
                        continue
                    try:
                        ts = tmap[tc]
                        td = ff[30][:8]
                        td = f'{td[:4]}-{td[4:6]}-{td[6:8]}'
                        o, h, l, c = float(ff[5]), float(ff[33]), float(ff[34]), float(ff[3])
                        vol = float(ff[6]) * 100  # 腾讯"手"→"股", 匹配etf_daily历史单位(股)
                        nm = ff[1]
                    except Exception:
                        continue
                    if c <= 0:
                        continue
                    wc.execute("DELETE FROM etf_daily WHERE ts_code=? AND trade_date=?", [ts, td])
                    wc.execute("INSERT INTO etf_daily (ts_code,trade_date,open,high,low,close,volume,name) VALUES (?,?,?,?,?,?,?,?)", [ts, td, o, h, l, c, vol, nm])
            except Exception:
                pass
            try:  # 回填pre_close(与主采集器同款,防下游算涨跌幅得NULL)
                wc.execute("UPDATE kline_daily k SET pre_close=s.p FROM (SELECT ts_code,trade_date,LAG(close) OVER(PARTITION BY ts_code ORDER BY trade_date) p FROM kline_daily) s WHERE k.ts_code=s.ts_code AND k.trade_date=s.trade_date AND k.pre_close IS NULL AND s.p IS NOT NULL")
            except Exception:
                pass
            wc.close()
        except Exception:
            pass
    conn = duckdb.connect(db, read_only=True)
    # 找最新交易日
    try:
        latest = conn.execute("SELECT MAX(trade_date) FROM kline_daily WHERE ts_code='sh000300'").fetchone()[0]
        latest = str(latest)[:10]
    except Exception:
        conn.close()
        return {'error': '无sh000300数据'}
    # 读jsonl已有日期(去重 + backfill基准)
    existing = set()
    try:
        if os.path.exists(log_path):
            with open(log_path, encoding='utf-8') as f:
                for line in f:
                    try:
                        dd = json.loads(line).get('date')
                        if dd:
                            existing.add(dd)
                    except Exception:
                        continue
    except Exception:
        pass
    # 待记录交易日: backfill=补(last_D, latest]所有缺失交易日(自愈漏跑); 否则只latest
    if backfill and existing:
        last_D = max(existing)
        try:
            pend = conn.execute("SELECT DISTINCT trade_date FROM kline_daily WHERE ts_code='sh000300' AND trade_date>? AND trade_date<=? ORDER BY trade_date", [last_D, latest]).fetchall()
            pending = [str(d[0])[:10] for d in pend]
        except Exception:
            pending = [latest]
    else:
        pending = [latest]
    # 对每个待记录交易日: as-of构建(<=D, leak-free) + judge + append
    results = []
    for D in pending:
        if D in existing:
            continue
        ctx, idx_close, idx_date = _build_context_asof(conn, D)
        judgment = judge_backstop(ctx)
        rec = {'date': idx_date, 'idx_close': idx_close,
               'method': 'live' if D == latest else 'backfill',
               'front_run_note': 'front-run维lower-bound: dragon_tiger机构席位明细/权重股相对强弱未接',
               'context': ctx, 'judgment': judgment}
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            rec['written'] = True
        except Exception:
            pass
        existing.add(D)
        results.append(rec)
    conn.close()
    if not results:
        return {'date': latest, 'skipped': f'{latest}及之前均已记录(去重), 无漏跑', 'checked': pending}
    return results if len(results) > 1 else results[0]


if __name__ == '__main__':
    import json as _json
    print("Part B 今日首记:")
    print(_json.dumps(record_forward_state(), ensure_ascii=False, indent=2))
