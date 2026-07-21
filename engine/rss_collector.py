# -*- coding: utf-8 -*-
"""
天眼 · RSS新闻采集器 v1.0
来源: 华尔街见闻/东方财富/36氪/港经/WSJ/BBC → 全文提取 → 板块分类 → DuckDB

用法:
  python rss_collector.py           → 采集+存库+输出摘要
  python rss_collector.py --dry-run → 只拉不存, 看采集质量
  python rss_collector.py --source 华尔街见闻 → 只拉指定源
"""
import sys, os, io, re, hashlib, time, ssl, socket
from datetime import datetime, date
from collections import defaultdict

# 全局socket超时兜底: 防境外RSS源(BBC/WSJ/HKET)无响应时永久挂起
socket.setdefaulttimeout(20)

# UTF-8 stdout 封装 (防GBK吞emoji)
_orig_stdout = sys.stdout
try:
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
except (ValueError, AttributeError):
    sys.stdout = _orig_stdout

ssl._create_default_https_context = ssl._create_unverified_context

import feedparser
import requests
from bs4 import BeautifulSoup
import duckdb

DB = 'D:/FreeFinanceData/data/duckdb/finance.db'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

# ===== RSS源配置 =====
RSS_FEEDS = {
    "华尔街见闻": {"url": "https://dedicated.wallstreetcn.com/rss.xml", "extract": "newspaper"},
    "东方财富":    {"url": "http://rss.eastmoney.com/rss_partener.xml", "extract": "eastmoney"},
    "36氪":        {"url": "https://36kr.com/feed", "extract": "36kr"},
    "香港经济日报": {"url": "https://www.hket.com/rss/china", "extract": "hket"},
    "WSJ市场":     {"url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "extract": "newspaper"},
    "BBC商业":     {"url": "http://feeds.bbci.co.uk/news/business/rss.xml", "extract": "newspaper"},
}

# ===== 板块关键词 =====
SECTOR_KEYWORDS = {
    "AI/科技": ["AI", "人工智能", "芯片", "半导体", "光模块", "CPO", "算力", "GPU", "英伟达",
                "寒武纪", "中际旭创", "新易盛", "英特尔", "谷歌", "微软"],
    "新能源/电力": ["新能源", "锂电池", "光伏", "风电", "储能", "宁德时代", "比亚迪",
                    "电力", "公用事业", "充电桩", "固态电池"],
    "有色/商品":   ["有色", "铜", "铝", "黄金", "稀土", "紫金矿业", "山东黄金", "原油",
                    "石油", "WTI", "油价", "金价"],
    "金融/地产":   ["银行", "保险", "券商", "降准", "降息", "MLF", "LPR",
                    "房地产", "万科", "保利", "楼市", "房贷"],
    "宏观/政策":   ["GDP", "PMI", "CPI", "美联储", "央行", "社融", "M2", "出口",
                    "贸易", "汇率", "人民币", "国家队", "汇金", "证监会", "加息",
                    "特朗普", "沃什", "FOMC"],
    "消费":        ["白酒", "茅台", "五粮液", "消费", "食品", "家电", "汽车", "比亚迪"],
    "医药":        ["医药", "药明", "恒瑞", "医保", "集采", "生物"],
}

# ===== 利空关键词 =====
BEARISH_KEYWORDS = [
    "跌", "暴跌", "崩盘", "重挫", "下行", "下滑", "回落",
    "亏损", "预亏", "业绩下滑", "不及预期", "减持", "套现",
    "制裁", "黑名单", "限制", "封杀", "违约", "暴雷", "退市",
    "地缘", "冲突", "战争", "通胀", "加息", "收紧",
]


def classify_sectors(text):
    """分类新闻板块"""
    hits = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text.lower():
                hits.append(sector)
                break
    return ",".join(hits) if hits else "其他"


def detect_bearish(text):
    """检测是否利空"""
    for kw in BEARISH_KEYWORDS:
        if kw in text:
            return True
    return False


# ===== 正文提取 =====

def extract_eastmoney(url):
    """东方财富: requests + BS4 #ContentBody"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        div = soup.select_one("div#ContentBody")
        if div:
            return div.get_text(strip=True)[:3000]
    except Exception:
        pass
    return ""


def extract_36kr(url):
    """36氪: requests + 内嵌JSON提取widgetContent"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup.find_all('script'):
            if not s.string or 'articleDetail' not in s.string:
                continue
            for key in ['widgetContent', 'summary']:
                pos = s.string.find('"' + key + '"')
                if pos < 0:
                    continue
                colon = s.string.find(':', pos)
                val_start = s.string.find('"', colon + 1)
                if val_start < 0:
                    continue
                chars = []
                i = val_start + 1
                while i < len(s.string):
                    ch = s.string[i]
                    if ch == '\\' and i + 1 < len(s.string):
                        chars.append(s.string[i+1])
                        i += 2
                    elif ch == '"':
                        break
                    else:
                        chars.append(ch)
                        i += 1
                raw = ''.join(chars)
                text = BeautifulSoup(raw, 'html.parser').get_text()
                text = text.replace('\\n', '\n').replace('\\t', ' ')
                if len(text) > 100:
                    return text[:3000]
    except Exception:
        pass
    return ""


def extract_hket(url):
    """香港经济日报: requests + BS4 div.article-detail"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        div = soup.select_one("div.article-detail") or soup.select_one("div#article-detail")
        if div:
            return div.get_text(strip=True)[:3000]
    except Exception:
        pass
    return ""


def extract_newspaper(url):
    """通用: newspaper3k (用requests预取html, 避免article.download()无超时挂起)"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(5, 15))
        if resp.status_code != 200 or not resp.text:
            return ""
        from newspaper import Article
        article = Article(url)
        article.set_html(resp.text)   # 不走article.download(), 用已取的html
        article.parse()
        return article.text[:3000] if article.text else ""
    except Exception:
        pass
    return ""


EXTRACTORS = {
    "eastmoney": extract_eastmoney,
    "36kr": extract_36kr,
    "hket": extract_hket,
    "newspaper": extract_newspaper,
}


def connect_db_with_retry(db_path, max_retries=10, base_delay=2):
    """连接DuckDB (带重试, 避免与daily进程冲突)"""
    for i in range(max_retries):
        try:
            conn = duckdb.connect(db_path)
            conn.execute("SELECT 1")
            return conn
        except duckdb.IOException as e:
            if "另一个程序正在使用此文件" in str(e) or "Cannot open file" in str(e):
                wait = base_delay * (2 ** i)
                print(f"   ⏳ DB被锁定, {wait}s后重试 ({i+1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise
    # 最后一次尝试
    return duckdb.connect(db_path)


def fetch_feed(url, retries=2, delay=2):
    """拉取RSS (用requests带超时取内容再parse, 防feedparser.parse(url)底层urllib无超时挂起)"""
    for i in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=(5, 15))
            if resp.status_code == 200 and resp.content:
                feed = feedparser.parse(resp.content)
                if feed and feed.entries:
                    return feed
        except Exception:
            if i < retries - 1:
                time.sleep(delay)
    return None


def collect_and_store(sources=None, dry_run=False):
    """主函数: 采集全部RSS源, 存库, 返回统计"""
    conn = None if dry_run else connect_db_with_retry(DB)
    if not dry_run:
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM news_articles").fetchone()[0]
        next_id = max_id + 1

    today = date.today().isoformat()
    stats = {"total_entries": 0, "fulltext_ok": 0, "inserted": 0, "skipped": 0,
             "bearish": 0, "sectors": defaultdict(int), "by_source": {}}

    targets = {k: v for k, v in RSS_FEEDS.items() if sources is None or k in sources}

    for name, cfg in targets.items():
        url = cfg["url"]
        method = cfg["extract"]
        extract_fn = EXTRACTORS.get(method, extract_newspaper)

        print(f"\n📡 {name} ({method})")
        feed = fetch_feed(url)
        if not feed:
            print(f"   ❌ RSS拉取失败")
            stats["by_source"][name] = {"entries": 0, "text_ok": 0, "inserted": 0}
            continue

        entries = feed.entries[:8]  # 每个源最多8条
        src_ok = 0
        src_ins = 0
        print(f"   ✅ {len(feed.entries)}条可用, 采前{min(8, len(feed.entries))}条")

        for entry in entries:
            stats["total_entries"] += 1
            title = entry.get('title', '').strip()
            link = entry.get('link', '') or entry.get('guid', '')

            if not title or len(title) < 5:
                continue

            # 正文提取
            text = extract_fn(link) if link else ""
            if text and len(text) > 100:
                stats["fulltext_ok"] += 1
                src_ok += 1

            if dry_run:
                status = "📝" if text else "⚠️空"
                print(f"   {status} {title[:70]}")
                continue

            # 入库
            sector_tags = classify_sectors(f"{title} {text}")
            is_bearish = detect_bearish(f"{title} {text}")
            if is_bearish:
                stats["bearish"] += 1
            for sec in sector_tags.split(","):
                stats["sectors"][sec] += 1

            content_hash = hashlib.md5((title + (link or "")).encode()).hexdigest()[:12]
            exists = conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE content_hash = ?",
                [content_hash]
            ).fetchone()[0]

            if exists > 0:
                stats["skipped"] += 1
                continue

            conn.execute(
                """INSERT INTO news_articles (id, title, content, source, publish_date, sector_tags, content_hash, collect_time)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [next_id, title[:200], text[:3000], name, today,
                 sector_tags, content_hash, datetime.now().strftime('%Y-%m-%d %H:%M:%S')]
            )
            next_id += 1
            stats["inserted"] += 1
            src_ins += 1

        stats["by_source"][name] = {"entries": len(entries), "text_ok": src_ok, "inserted": src_ins}
        time.sleep(0.5)  # 礼貌延迟

    if not dry_run:
        conn.close()

    return stats


def print_summary(stats):
    """打印采集摘要"""
    print(f"\n{'='*60}")
    print(f"  RSS新闻采集报告 — {date.today()}")
    print(f"{'='*60}")
    print(f"  总条目: {stats['total_entries']} | 正文提取: {stats['fulltext_ok']} | "
          f"入库: {stats['inserted']} | 跳过: {stats['skipped']}")
    print(f"  利空检测: {stats['bearish']}条")
    print(f"\n  各源统计:")
    for name, s in stats["by_source"].items():
        print(f"    {name:10s}: {s['entries']}条 正文{s['text_ok']} 入库{s['inserted']}")
    if stats["sectors"]:
        print(f"\n  板块分布:")
        for sec, cnt in sorted(stats["sectors"].items(), key=lambda x: x[1], reverse=True):
            print(f"    {sec}: {cnt}条")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="天眼RSS新闻采集器")
    p.add_argument("--dry-run", action="store_true", help="只拉不存")
    p.add_argument("--source", help="只拉指定源")
    args = p.parse_args()

    sources = [args.source] if args.source else None
    stats = collect_and_store(sources=sources, dry_run=args.dry_run)
    print_summary(stats)
