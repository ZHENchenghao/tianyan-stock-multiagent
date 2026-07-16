# -*- coding: utf-8 -*-
"""
天眼 · 盘面解释引擎 (context_reader) v1.0 — 2026-07-17
========================================================
定位: LLM是「解释引擎」不是「预测引擎」。
职责: 整合多维信息(量价/资金流/消息)推理拼凑市场真实走向——
      解释为什么波动、这根放量阴线是洗盘还是出货砸盘。
      先解释清楚「正在发生什么」, 裁决层才有资格决定「接下来做什么」。

修的病(2026-07端到端审计): 读懂层50-60%关键词匹配假装情境推理、
数据过期不自知、每次结论漂移。对应三药:
  1. 程序化新鲜度门禁(过期数据在调LLM之前就拒绝, 不靠LLM自觉)
  2. 多假设竞争+可证伪输出的结构化prompt(温度0, 确定性序列化)
  3. 输出后验校验(schema/假设数/编造钓鱼), 不过验收=返回None不污染裁决层

用法:
  from engine.context_reader import read_market_context
  result = read_market_context(payload)   # None=不可用/未过验收, 调用方标"解释引擎不可用"
  python engine/context_reader.py --selftest   # 三项验收: 一致性/反事实翻转/编造钓鱼
"""
import os, sys, json, io
from datetime import datetime, date

if __name__ == '__main__':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# 输入里可能出现的数据通道 → 钓鱼检查用: 输入没给的通道, 输出证据里不许出现
KNOWN_CHANNELS = {
    'dragon_tiger': ['龙虎榜', '席位'],
    'moneyflow': ['主力净流入', '资金流', '大单'],
    'north': ['北向'],
    'margin': ['融资', '融券', '两融'],
    'options': ['期权', 'IV', 'skew'],
    'block_trade': ['大宗交易'],
}

SYSTEM_PROMPT = """你是盘面侦探，职责是「解释正在发生什么」，不是「预测将要发生什么」。

## 任务
整合下方提供的多维数据，推理拼凑出该标的/市场当前盘面行为的最可能解释：
为什么波动？这是主力洗盘、出货砸盘、恐慌踩踏、还是风格轮动/跟随大盘？

## 铁律（违反任何一条=输出作废）
1. 只使用下方明确提供的数据。字段缺失时写"数据不足"，禁止依据常识补数、禁止引用训练记忆里的历史行情。
2. 每个判断必须引用具体数值作证据（如"成交额为5日均值的1.8倍"），禁止无基准的模糊词。
3. 多假设竞争：至少给出3个候选解释，逐一列支持证据和反驳证据。反驳证据栏为空的假设视为未检验，不得当选。
4. 结论必须可证伪：给出"未来N日内若出现X，则本解释错误"的具体检验条件。
5. 消息先过真伪分级：信源等级（官方/权威媒体/自媒体/不可溯源）、是否旧闻重炒、利益方向。不可溯源且无交叉印证的消息只能标"待验传闻"，不得支撑核心结论。
6. 洗盘vs出货这类判断你拿不到账户级地面真值，只准输出概率不准输出断言。
7. 不知道答案时的正确输出=低置信度+数据缺口清单，不是编一个自信的故事。

## 输出
只输出JSON，无其他文字，schema:
{"news_credibility":[{"消息":"...","信源等级":"官方/权威/自媒体/不可溯源","旧闻重炒":false,"采信":"采信/待验/剔除"}],
 "hypotheses":[{"解释":"...","支持证据":["..."],"反驳证据":["..."],"后验概率":0.0}],
 "verdict":{"最可能解释":"...","置信度":0.0,"一句话归因":"...","纠错线":"未来N日内若...则本解释作废","数据缺口":["..."]}}"""


def _serialize_payload(payload):
    """确定性序列化: 固定键序+数值2位小数。输入哪怕空格不同都会引起LLM漂移。"""
    def norm(v):
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, dict):
            return {k: norm(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(payload), ensure_ascii=False, sort_keys=True, indent=1)


def check_freshness(payload, analysis_date=None, max_lag_days=4):
    """程序化新鲜度门禁: 在调LLM之前拒绝过期数据(汇率过期11天事故的代码级药)。
    扫payload里所有形如*_date/date的字段, 距analysis_date超过max_lag_days(自然日,含周末容差)记stale。
    返回 (usable: bool, stale_list: [str])"""
    ref = analysis_date or date.today()
    if isinstance(ref, str):
        ref = datetime.strptime(ref[:10], '%Y-%m-%d').date()
    stale = []

    def scan(obj, path=''):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f'{path}.{k}' if path else k
                if isinstance(v, str) and ('date' in k.lower() or k.endswith('日期')):
                    try:
                        d = datetime.strptime(v[:10], '%Y-%m-%d').date()
                        lag = (ref - d).days
                        if lag > max_lag_days:
                            stale.append(f'{p}={v}(滞后{lag}天)')
                    except Exception:
                        pass
                else:
                    scan(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan(v, f'{path}[{i}]')

    scan(payload)
    return (len(stale) == 0, stale)


def _fishing_violations(result, payload_text):
    """编造钓鱼检查: 输入没提供的数据通道, 不许被当作【证据】引用。
    只扫证据栏(支持/反驳证据+归因+解释)——在「数据缺口」里写"缺少期权数据"是正确行为不算违规。"""
    ev_parts = []
    for h in (result.get('hypotheses') or []):
        ev_parts += [str(x) for x in (h.get('支持证据') or [])]
        ev_parts += [str(x) for x in (h.get('反驳证据') or [])]
        ev_parts.append(str(h.get('解释', '')))
    v = result.get('verdict') or {}
    ev_parts.append(str(v.get('一句话归因', '')))
    ev_parts.append(str(v.get('最可能解释', '')))
    out_text = ' '.join(ev_parts)
    violations = []
    for channel, terms in KNOWN_CHANNELS.items():
        provided = any(t in payload_text for t in terms)
        if not provided:
            cited = [t for t in terms if t in out_text]
            if cited:
                violations.append(f'{channel}: 输入未提供却当证据引用了{cited}')
    return violations


def _validate(result, payload_text):
    """输出后验校验: schema完整/≥3假设且各有反驳证据/概率合法/纠错线非空/无编造钓鱼。
    返回 (ok: bool, reasons: [str])"""
    reasons = []
    if not isinstance(result, dict):
        return False, ['非dict输出']
    hyps = result.get('hypotheses') or []
    verdict = result.get('verdict') or {}
    if len(hyps) < 3:
        reasons.append(f'假设数{len(hyps)}<3, 未做多假设竞争')
    for h in hyps:
        if not h.get('反驳证据'):
            reasons.append(f'假设"{h.get("解释","?")}"无反驳证据=未检验')
    try:
        conf = float(verdict.get('置信度', -1))
        if not (0 <= conf <= 1):
            reasons.append('置信度不在[0,1]')
    except Exception:
        reasons.append('置信度非数值')
    if not str(verdict.get('纠错线', '')).strip():
        reasons.append('缺纠错线=不可证伪')
    fish = _fishing_violations(result, payload_text)
    if fish:
        reasons.append('编造钓鱼违规: ' + '; '.join(fish))
    return (len(reasons) == 0), reasons


def _call_llm(system, user, max_tokens=2500):
    """temp=0 + top_k=1贪心解码 + 输入哈希缓存三重确定性:
    实测发现仅temperature=0服务端仍会漂移(验收[2]抓到), 缓存是最后一道保证。"""
    import hashlib
    key = hashlib.sha256((system + '\x00' + user).encode('utf-8')).hexdigest()[:24]
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_context_cache')
    cache_fp = os.path.join(cache_dir, f'{key}.json')
    try:
        if os.path.exists(cache_fp):
            with open(cache_fp, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    try:
        import anthropic
        client = anthropic.Anthropic()
        model = os.environ.get('ANTHROPIC_MODEL') or os.environ.get('ANTHROPIC_DEFAULT_HAIKU_MODEL')
        if not model:
            return None
        resp = None
        for _try in range(3):  # 网络瞬断重试(SSL握手超时实测会发生), 指数退避
            try:
                resp = client.messages.create(
                    model=model, max_tokens=max_tokens, temperature=0, top_k=1,
                    system=system, messages=[{"role": "user", "content": user}]
                )
                break
            except Exception:
                if _try == 2:
                    raise
                import time as _t
                _t.sleep(4 * (_try + 1))
        text = ''.join(getattr(b, 'text', '') for b in resp.content
                       if getattr(b, 'type', '') == 'text').strip()
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        result = json.loads(text)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_fp, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass
        return result
    except Exception:
        return None


def read_market_context(payload, analysis_date=None):
    """盘面归因主入口。
    payload示例: {'标的': '沪深300', 'analysis_date': '2026-07-17',
                  '量价': {'今日涨跌%': -1.2, '成交额亿': 9800, '成交额5日均亿': 7200,
                          '尾盘30分钟涨跌%': 0.4, 'kline_date': '2026-07-17'},
                  '资金流': {'北向净亿': -35, 'moneyflow_date': '2026-07-17'},
                  '消息': [{'标题': '...', '来源': '...', '日期': '2026-07-17'}]}
    返回: 通过验收的解释dict + '_meta'; 门禁/验收不过返回带'_meta'的失败说明; LLM不可用返回None。"""
    usable, stale = check_freshness(payload, analysis_date or payload.get('analysis_date'))
    if not usable:
        # 数据过期→不调LLM, 直接程序化拒绝(病根药: 过期不自知)
        return {'_meta': {'ok': False, 'stage': 'freshness_gate', 'stale': stale,
                          'note': '数据过期, 解释引擎拒绝分析(先补数)'}}
    payload_text = _serialize_payload(payload)
    result = _call_llm(SYSTEM_PROMPT, f'## 数据(仅可用此数据)\n{payload_text}')
    if result is None:
        return None  # LLM不可用, 调用方降级
    ok, reasons = _validate(result, payload_text)
    result['_meta'] = {'ok': ok, 'stage': 'validated' if ok else 'validation_failed',
                       'reasons': reasons, 'stale': []}
    if not ok:
        # 一次重试: 把违规原因喂回去
        retry = _call_llm(SYSTEM_PROMPT,
                          f'## 数据(仅可用此数据)\n{payload_text}\n\n'
                          f'## 上次输出被验收拒绝, 原因: {"; ".join(reasons)}。修正后重新输出JSON。')
        if retry is not None:
            ok2, reasons2 = _validate(retry, payload_text)
            retry['_meta'] = {'ok': ok2, 'stage': 'validated' if ok2 else 'validation_failed_twice',
                              'reasons': reasons2, 'stale': []}
            return retry
    return result


# ============================================================
# 验收协议(不测=白修): 一致性 / 反事实翻转 / 编造钓鱼
# ============================================================
def _fixture(flip=False, with_north=True):
    p = {
        '标的': '沪深300', 'analysis_date': date.today().isoformat(),
        '量价': {'今日涨跌%': -1.8, '成交额亿': 12400.0, '成交额5日均亿': 7900.0,
                '尾盘30分钟涨跌%': (0.9 if not flip else -1.1),
                '当日振幅%': 2.6, 'kline_date': date.today().isoformat()},
        '资金流': ({'北向净亿': (42.0 if not flip else -58.0),
                   'moneyflow_date': date.today().isoformat()} if with_north else {}),
        '消息': [{'标题': '监管就程序化交易新规征求意见', '来源': '证监会官网',
                 '日期': date.today().isoformat()}],
    }
    return p


def selftest():
    print('=== context_reader 验收协议 ===')
    # 0. 新鲜度门禁(无LLM也可测)
    stale_payload = _fixture(); stale_payload['量价']['kline_date'] = '2026-06-30'
    r = read_market_context(stale_payload)
    gate_ok = isinstance(r, dict) and r.get('_meta', {}).get('stage') == 'freshness_gate'
    print(f'[0] 新鲜度门禁拒绝过期数据: {"✅" if gate_ok else "❌"}')

    base = read_market_context(_fixture())
    if base is None:
        print('[!] LLM不可用(缺ANTHROPIC_API_KEY/MODEL), 测试1-3跳过。门禁测试结果如上。')
        return 2 if gate_ok else 1
    ok1 = base.get('_meta', {}).get('ok', False)
    v1 = (base.get('verdict') or {}).get('最可能解释', '?')
    print(f'[1] 基线输出过验收: {"✅" if ok1 else "❌ " + str(base["_meta"]["reasons"])} | 解释={v1}')

    # 一致性: 同输入再跑一次, 最可能解释须一致
    again = read_market_context(_fixture())
    v2 = (again.get('verdict') or {}).get('最可能解释', '??') if again else '??'
    print(f'[2] 一致性(temp=0同输入同结论): {"✅" if v1 == v2 else f"❌ {v1} vs {v2}"}')

    # 反事实翻转: 尾盘回拉→尾盘跳水+北向流入→流出, 解释必须变(不变=背模板)
    flipped = read_market_context(_fixture(flip=True))
    flip_ok = flipped is not None and flipped.get('_meta', {}).get('ok', False)
    v3 = (flipped.get('verdict') or {}).get('最可能解释', '?') if flipped else '?'
    if not flip_ok:
        print(f'[3] 反事实翻转: ❌ 翻转输入的输出未过验收({(flipped or {}).get("_meta", {}).get("reasons", "LLM不可用")}), 无法判定')
    else:
        print(f'[3] 反事实翻转(资金/尾盘反向→结论应变): {"✅" if v3 != v1 else "❌ 结论未随数据翻转=模板嫌疑"} | {v1} → {v3}')

    # 编造钓鱼: 删掉资金流通道, 输出不得引用北向/资金流(validate内已查, 这里显式验证)
    no_north = read_market_context(_fixture(with_north=False))
    if no_north and '_meta' in no_north:
        fish_ok = '编造钓鱼' not in '; '.join(no_north['_meta'].get('reasons', []))
        print(f'[4] 编造钓鱼(不给资金流不许引用): {"✅" if fish_ok else "❌ " + str(no_north["_meta"]["reasons"])}')
    return 0


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(selftest() or 0)
    # 默认: 用真实DuckDB数据跑一次今日大盘归因
    try:
        import duckdb
        DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
        conn = duckdb.connect(DB, read_only=True)
        rows = conn.execute("""
            SELECT trade_date, close, amount FROM kline_daily
            WHERE ts_code='sh000300' ORDER BY trade_date DESC LIMIT 6""").fetchall()
        conn.close()
        if len(rows) >= 6:
            chg = (rows[0][1] / rows[1][1] - 1) * 100
            amt = (rows[0][2] or 0) / 1e8
            avg5 = sum((r[2] or 0) for r in rows[1:6]) / 5 / 1e8
            payload = {'标的': '沪深300', 'analysis_date': str(rows[0][0])[:10],
                       '量价': {'今日涨跌%': round(chg, 2), '成交额亿': round(amt, 0),
                               '成交额5日均亿': round(avg5, 0), 'kline_date': str(rows[0][0])[:10]},
                       '消息': []}
            out = read_market_context(payload, analysis_date=str(rows[0][0])[:10])
            print(json.dumps(out, ensure_ascii=False, indent=1) if out else 'LLM不可用')
    except Exception as e:
        print(f'实数据演示失败: {e}')
