# -*- coding: utf-8 -*-
"""
政策监控看门狗 v1.0
akshare不抓监管政策新闻 → 本模块用搜索引擎兜底
用法: python policy_watchdog.py          → 搜索+存库+输出
      python policy_watchdog.py --today  → 查看今日政策新闻
TTL: 4小时 (重大政策不会高频变化)
"""
import sys, os, io, hashlib, re, json, time, ssl
_orig_stdout = sys.stdout
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except (ValueError, AttributeError):
    sys.stdout = _orig_stdout

# SSL补丁 (解决Windows Python3.10 GIL冲突)
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'
os.environ['REQUESTS_CA_BUNDLE'] = ''
os.environ['CURL_CA_BUNDLE'] = ''

from datetime import datetime, date
import duckdb

# requests monkey-patch
try:
    import requests as _req
    _orig_req = _req.Session.request
    def _patched_req(self, method, url, **kwargs):
        kwargs.setdefault('verify', False)
        kwargs.setdefault('timeout', 15)
        return _orig_req(self, method, url, **kwargs)
    _req.Session.request = _patched_req
except:
    pass

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
SEEN_FILE = os.path.join(BASE, 'engine', '.policy_seen.json')

# === 搜索词条 (国家队/监管/政策三大类) ===
QUERIES = [
    # 国家队动向
    '国家队 汇金 证金 买入 A股 2026',
    '社保基金 养老金 入市 持仓 A股',
    '中央汇金 增持 ETF 救市',
    # 监管整治
    '证监会 监管 整治 处罚 2026年5月',
    '八部门 联合 整治 金融 证券 跨境',
    '老虎证券 富途 跨境 券商 整治',
    # 政策法规
    'A股 印花税 交易规则 T+0 做空',
    '减持 新规 大股东 融券 量化',
    '退市 制度 改革 注册制 IPO',
    # 宏观政策
    '国常会 资本市场 金融 稳定',
    '政治局 经济 会议 货币 财政 政策',
    '央行 降准 降息 公开市场 操作',
]

def news_db():
    """确保news_articles表存在 + policy_watchdog source可写入"""
    conn = duckdb.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY,
            title VARCHAR,
            content VARCHAR,
            source VARCHAR,
            publish_date DATE,
            publish_time VARCHAR,
            sector_tags VARCHAR,
            content_hash VARCHAR UNIQUE,
            collect_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE SEQUENCE IF NOT EXISTS news_id_seq START 1")
    return conn

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_seen(seen):
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(seen, f, ensure_ascii=False)

def search_web(query, max_results=8):
    """多源搜索: Bing→DuckDuckGo Lite→Google (curl绕过SSL GIL)"""
    import subprocess, urllib.parse
    results = []
    headers = ['-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
               '-H', 'Accept-Language: zh-CN,zh;q=0.9']

    urls = [
        # Bing (中文优先)
        f'https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=zh-cn',
        # DuckDuckGo Lite (无JS版本, 更稳定)
        f'https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}',
    ]

    for url in urls:
        if len(results) >= max_results:
            break
        try:
            r = subprocess.run(['curl', '-s', '-k', '-m', '12'] + headers + [url],
                              capture_output=True, text=True, timeout=18)
            html = r.stdout
            if not html or len(html) < 200:
                continue

            # 通用解析: <a href=...>标题</a> 模式
            # Bing结果
            links_found = re.findall(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
            for href, anchor in links_found:
                title = re.sub(r'<[^>]+>', '', anchor).strip()
                if title and len(title) > 8 and 'http' not in title[:10]:
                    # 过滤导航链接
                    skip_words = ['下一页', '上一页', '搜索', '登录', '注册', '帮助', '隐私', '©']
                    if not any(w in title for w in skip_words):
                        results.append({'title': title, 'content': '', 'url': href})
                        if len(results) >= max_results:
                            break

            # 如果Bing没结果, 尝试纯文本提取
            if not results:
                # DuckDuckGo Lite提取
                ddg_rows = re.findall(r'<a[^>]*href="(https?://[^"]+)[^"]*"[^>]*>\s*(.*?)\s*</a>', html, re.DOTALL)
                for href, text in ddg_rows:
                    title = re.sub(r'<[^>]+>', '', text).strip()
                    if title and len(title) > 10:
                        results.append({'title': title, 'content': '', 'url': href})
                        if len(results) >= max_results:
                            break
        except Exception:
            continue

    return results

def match_policy_tags(title, content=''):
    """匹配政策监管子标签"""
    text = f'{title} {content}'
    tags = []
    tag_map = {
        '国家队': r'国家队|汇金|证金|中金|国新|诚通|社保基金|养老金|救市',
        '监管整治': r'整治|处罚|取缔|关停|清理|整顿|禁止|叫停|约谈|调查|罚款|立案|问责|清退',
        '跨境券商': r'老虎|富途|长桥|跨境证券|跨境开户|非法跨境',
        '减持新规': r'减持|大股东减持|融券|限售股',
        '货币政策': r'降准|降息|LPR|MLF|逆回购|SLF|PSL|公开市场',
        '印花税': r'印花税|交易费|T\+0|做空机制',
        '退市制度': r'退市|ST|\*ST|暂停上市|恢复上市',
        '政治局/国常会': r'政治局|国常会|深改委|金融委|中央经济',
    }
    for tag, pattern in tag_map.items():
        if re.search(pattern, text):
            tags.append(tag)
    return ','.join(tags)

def run():
    """主流程"""
    conn = news_db()
    seen = load_seen()
    today = date.today().isoformat()
    all_found = []

    print(f'政策监控看门狗 v1.0 · {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    print(f'搜索词条: {len(QUERIES)}组\n')

    for i, q in enumerate(QUERIES):
        print(f'  [{i+1}/{len(QUERIES)}] {q[:50]}...', end=' ')
        results = search_web(q)
        new_count = 0
        for r in results:
            h = hashlib.md5((r['title'] + 'websearch').encode()).hexdigest()
            if h in seen:
                continue
            seen[h] = today
            tags = match_policy_tags(r['title'], r.get('content', ''))
            if not tags:
                # 无匹配 → 仍入库但标为"政策监管_未分类"
                tags = '政策监管'
            else:
                tags = f'政策监管,{tags}'
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO news_articles (id, title, content, source, publish_date, publish_time, sector_tags, content_hash)
                    VALUES (nextval('news_id_seq'), ?, ?, '政策监控', ?, 'web', ?, ?)
                """, [r['title'][:200], r.get('content', '')[:500], today, tags, h])
                if conn.changes > 0:
                    new_count += 1
                    all_found.append({'title': r['title'][:80], 'tags': tags})
            except:
                pass
        print(f'{len(results)}条结果, {new_count}条新')
        # 避免触发Python GIL冲突, 不加sleep

    save_seen(seen)
    conn.close()

    # 输出摘要
    print(f'\n===== 政策监控摘要 =====')
    print(f'本次新增: {len(all_found)}条')
    if all_found:
        for item in all_found[:20]:
            print(f'  [{item["tags"]}] {item["title"]}')
    else:
        print('  无新增政策新闻')

    # 今日汇总
    conn2 = duckdb.connect(DB)
    today_count = conn2.execute(f"SELECT COUNT(*) FROM news_articles WHERE source='政策监控' AND publish_date='{today}'").fetchone()[0]
    conn2.close()
    print(f'\n今日政策监控: {today_count}条')

    return len(all_found)

def show_today():
    conn = duckdb.connect(DB)
    today = date.today().isoformat()
    rows = conn.execute(f"""
        SELECT title, sector_tags, publish_date FROM news_articles
        WHERE source='政策监控' AND publish_date='{today}'
        ORDER BY collect_time DESC LIMIT 30
    """).fetchall()
    conn.close()
    print(f'今日政策监控: {len(rows)}条')
    for r in rows:
        print(f'  [{r[1]}] {r[0][:80]}')

if __name__ == '__main__':
    if '--today' in sys.argv:
        show_today()
    elif '--count' in sys.argv:
        conn = duckdb.connect(DB)
        c = conn.execute("SELECT COUNT(*) FROM news_articles WHERE source='政策监控'").fetchone()[0]
        conn.close()
        print(f'政策监控总条数: {c}')
    else:
        run()
