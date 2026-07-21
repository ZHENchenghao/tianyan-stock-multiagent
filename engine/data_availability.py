# -*- coding: utf-8 -*-
"""
数据可用性时间戳对齐层
======================
残B落地: 回测和实盘必须根据每张表/每个字段的真实发布时间,
扣减滞后天数, 消除后视镜偏见(Look-ahead Bias)。

核心函数:
  get_available_data_cutoff(as_of_date, as_of_time, table, column=None)
    → 返回该时间点实际能查询到的数据截止日期

  check_live_data_freshness()
    → 实盘14:50运行前检查所有数据是否足够新鲜

用法:
  from engine.data_availability import DataAvailability
  da = DataAvailability()
  cutoff = da.get_cutoff('macro_indicators', 'us10y', date.today())
  # → date(2026, 6, 2)  # T日只能看到T-1的美10Y

集成:
  pipeline_backtest.py:  回测每步调用 get_cutoff()
  UnifiedVerdict.__init__: 实盘启动时调用 check_live_data_freshness()
"""

import json, os, sys
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(BASE, 'data_availability_schedule.json')


def _last_trading_day(d: date) -> date:
    """把日期滚到不晚于它的最近交易日(周末→周五)。
    weekday近似(不含法定节假日),用于新鲜度检查:周末/周日不再把'数据只到周五'误判为过期。
    不会掩盖真过期——真过期的数据日期仍早于最近交易日,照样flag。"""
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d = d - timedelta(days=1)
    return d


class DataAvailability:
    """数据时间戳对齐管理器"""

    def __init__(self, schedule_path: str = None):
        path = schedule_path or SCHEDULE_FILE
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"data_availability_schedule.json 不存在: {path}\n"
                f"请先创建该文件(残B第一步)"
            )
        with open(path, 'r', encoding='utf-8') as f:
            self.schedule = json.load(f)
        self.tables = self.schedule.get('tables', {})
        self.known_issues = self.schedule.get('dirty_data_known_issues', [])

    # ═══════════════════════════════════════════
    # 核心: 获取某时间点的数据截止日期
    # ═══════════════════════════════════════════

    def get_cutoff(self, table: str, column: str = 'all',
                   as_of_date: date = None, as_of_time: str = '15:00') -> date:
        """
        返回在 as_of_date 的 as_of_time 时刻, 实际能查到的 table.column 的最新数据日期。

        Args:
            table: 表名 (如 'macro_indicators')
            column: 列名 (如 'us10y'), 默认 'all' 代表整表统一滞后
            as_of_date: 决策日期, 默认今天
            as_of_time: 决策时间, '15:00' = 盘中决策, '15:30' = 盘后日报

        Returns:
            实际可用的数据截止日期 (查询时应使用 WHERE trade_date <= 此日期)

        Example:
            da = DataAvailability()
            # T日15:00决策, 美10Y滞后1天 → 只能看到T-1
            da.get_cutoff('macro_indicators', 'us10y', date(2026,6,3), '15:00')
            # → date(2026, 6, 2)

            # T日15:30日报, K线当天可用
            da.get_cutoff('kline_daily', 'all', date(2026,6,3), '15:30')
            # → date(2026, 6, 3)
        """
        if as_of_date is None:
            as_of_date = date.today()

        table_info = self.tables.get(table)
        if table_info is None:
            # 未知表: 保守滞后1天
            return as_of_date - timedelta(days=1)

        # 获取字段级或表级滞后
        cols = table_info.get('columns', {})
        col_info = cols.get(column, cols.get('all', {'lag_days': 1}))

        lag_days = col_info.get('lag_days', 1)

        # 额外检查: 15:00 vs 15:30 的差异
        available_at = col_info.get('available_at', 'T+1 08:00')
        if as_of_time >= '15:30' and '15:30' in available_at:
            # 盘后可用 → 15:30后不需扣lag
            pass  # lag_days 已经反映了这个逻辑
        elif as_of_time <= '15:00' and '15:30' in available_at:
            # 盘中决策, 但数据要15:30才有 → 多扣1天
            if lag_days == 0:
                lag_days = 1

        return as_of_date - timedelta(days=lag_days)

    def get_all_cutoffs(self, as_of_date: date = None,
                        as_of_time: str = '15:30') -> Dict[str, Dict[str, date]]:
        """
        获取所有表/字段在指定时间点的数据截止日期。
        供回测引擎批量使用。

        Returns:
            {
              'macro_indicators': {
                'us10y': date(2026,6,2),
                'wti': date(2026,6,2),
                'shibor_on': date(2026,6,3),
                ...
              },
              'kline_daily': {'all': date(2026,6,3)},
              ...
            }
        """
        if as_of_date is None:
            as_of_date = date.today()

        result = {}
        for table_name, table_info in self.tables.items():
            cols = table_info.get('columns', {})
            result[table_name] = {}
            for col_name in cols:
                result[table_name][col_name] = self.get_cutoff(
                    table_name, col_name, as_of_date, as_of_time
                )
        return result

    # ═══════════════════════════════════════════
    # 回测SQL生成: 自动拼接时间戳约束
    # ═══════════════════════════════════════════

    def sql_where_clause(self, table: str, column: str = 'all',
                         as_of_date: date = None, as_of_time: str = '15:30',
                         date_column: str = 'trade_date') -> str:
        """
        生成回测安全的WHERE子句。

        Example:
            da.sql_where_clause('macro_indicators', 'us10y', date(2026,6,3))
            # → "trade_date <= '2026-06-02'"
        """
        cutoff = self.get_cutoff(table, column, as_of_date, as_of_time)
        return f"{date_column} <= '{cutoff.isoformat()}'"

    # ═══════════════════════════════════════════
    # 实盘新鲜度检查
    # ═══════════════════════════════════════════

    def check_live_freshness(self, conn=None, as_of_date=None) -> Tuple[bool, list]:
        """
        实盘运行前检查所有关键数据是否足够新鲜。

        Returns:
            (all_fresh: bool, issues: list of dict)

        检查逻辑:
          不是检查"最新日期==今天", 而是检查"最新日期 >= 今天-该字段的预期滞后天数"。
          例: us10y滞后1天 → 最新日期 >= 昨天 → 通过。
        """
        if conn is None:
            try:
                import duckdb
                conn = duckdb.connect(r'D:\FreeFinanceData\data\duckdb\finance.db')
            except Exception as e:
                return False, [{'table': 'N/A', 'column': 'N/A', 'fresh': False,
                                'detail': f'无法连接DuckDB: {e}'}]

        issues = []
        today = as_of_date or date.today()

        # 只检查裁决链实际依赖的表
        critical_checks = [
            # (table, column, date_col, sql)
            ('kline_daily', 'all', 'trade_date',
             "SELECT MAX(trade_date) FROM kline_daily"),
            ('macro_indicators', 'us10y', 'trade_date',
             "SELECT MAX(trade_date) FROM macro_indicators WHERE us10y IS NOT NULL"),
            ('macro_indicators', 'shibor_on', 'trade_date',
             "SELECT MAX(trade_date) FROM macro_indicators WHERE shibor_on IS NOT NULL"),
            ('macro_indicators', 'usdcny', 'trade_date',
             "SELECT MAX(trade_date) FROM macro_indicators WHERE usdcny IS NOT NULL"),
            ('macro_indicators', 'wti', 'trade_date',
             "SELECT MAX(trade_date) FROM macro_indicators WHERE wti IS NOT NULL"),
            ('market_sentiment', 'all', 'trade_date',
             "SELECT MAX(trade_date) FROM market_sentiment"),
            ('margin_trading', 'all', 'trade_date',
             "SELECT MAX(trade_date) FROM margin_trading WHERE total_balance IS NOT NULL"),
            ('news_articles', 'all', 'publish_date',
             "SELECT MAX(publish_date) FROM news_articles"),
        ]

        for table, column, date_col, sql in critical_checks:
            try:
                row = conn.execute(sql).fetchone()
                latest = row[0] if row and row[0] else None

                if latest is None:
                    issues.append({'table': table, 'column': column, 'fresh': False,
                                   'latest': None, 'actual_lag': 999,
                                   'detail': '无数据'})
                    continue

                if isinstance(latest, str):
                    latest = datetime.strptime(latest[:10], '%Y-%m-%d').date()
                elif isinstance(latest, datetime):
                    latest = latest.date()

                expected_cutoff = self.get_cutoff(table, column, today, '15:30')
                # 交易日历修正: cutoff滚到最近交易日, 周末/节后不把"数据只到上个交易日"误判过期。
                # 真过期数据日期仍早于最近交易日, 不被掩盖。
                expected_cutoff = _last_trading_day(expected_cutoff)
                actual_lag = (today - latest).days
                expected_lag = (today - expected_cutoff).days
                is_fresh = latest >= expected_cutoff

                issues.append({
                    'table': table, 'column': column, 'fresh': is_fresh,
                    'latest': str(latest), 'actual_lag': actual_lag,
                    'expected_lag': expected_lag,
                    'detail': '正常' if is_fresh else f'数据过期(滞后{actual_lag}天, 预期<={expected_lag}天)'
                })

            except Exception as e:
                issues.append({'table': table, 'column': column, 'fresh': False,
                               'latest': None, 'actual_lag': 999,
                               'detail': f'查询失败: {e}'})

        all_fresh = all(item['fresh'] for item in issues)
        return all_fresh, issues

    # ═══════════════════════════════════════════
    # 脏数据检查: 新闻时间戳错位
    # ═══════════════════════════════════════════

    def detect_news_timestamp_issues(self, conn=None) -> list:
        """
        检查 news_articles 中可能存在的时间戳错位:
          - 标题含"昨日/昨晚/隔夜"但 publish_date = 今天
          - 美股收盘相关的新闻时间戳在非美股交易时段
        """
        if conn is None:
            try:
                import duckdb
                conn = duckdb.connect(r'D:\FreeFinanceData\data\duckdb\finance.db')
            except Exception:
                return []

        warnings = []

        # 检查1: "昨日"关键词但日期是今天
        try:
            rows = conn.execute("""
                SELECT publish_date, title FROM news_articles
                WHERE (title LIKE '%昨日%' OR title LIKE '%昨晚%' OR title LIKE '%隔夜%')
                  AND publish_date >= CURRENT_DATE - 7
                ORDER BY publish_date DESC LIMIT 10
            """).fetchall()
            if rows:
                warnings.append(
                    f'⚠ 发现{len(rows)}条含"昨日/昨晚/隔夜"的新闻, '
                    f'publish_date可能与实际事件日期错位1天'
                )
        except:
            pass

        # 检查2: 美股相关新闻的publish_date在A股交易时段
        try:
            rows = conn.execute("""
                SELECT publish_date, title FROM news_articles
                WHERE (title LIKE '%美股%' OR title LIKE '%美联储%')
                  AND publish_date >= CURRENT_DATE - 3
                ORDER BY publish_date DESC LIMIT 5
            """).fetchall()
            if rows:
                for r in rows:
                    warnings.append(f'  [{r[0]}] {r[1][:80]}')
        except:
            pass

        return warnings

    # ═══════════════════════════════════════════
    # 日报用: 数据含金量面板
    # ═══════════════════════════════════════════

    def get_freshness_panel(self, as_of_date: date = None) -> dict:
        """
        生成日报Section 1的数据新鲜度面板。
        """
        if as_of_date is None:
            as_of_date = date.today()

        panel = {}
        for table_name, table_info in self.tables.items():
            cols = table_info.get('columns', {})
            for col_name, col_info in cols.items():
                lag = col_info.get('lag_days', 0)
                cutoff = as_of_date - timedelta(days=lag)
                panel[f'{table_name}.{col_name}'] = {
                    'expected_lag_days': lag,
                    'expected_cutoff': cutoff.isoformat(),
                    'available_at': col_info.get('available_at', '?'),
                    'reason': col_info.get('reason', ''),
                }

        return panel


# ═══════════════════════════════════════════
# 快捷函数 (供 UnifiedVerdict 和 pipeline_backtest 调用)
# ═══════════════════════════════════════════

_global_da = None

def get_da() -> DataAvailability:
    """获取全局单例"""
    global _global_da
    if _global_da is None:
        _global_da = DataAvailability()
    return _global_da


# ═══════════════════════════════════════════
# 自检入口
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    da = DataAvailability()
    today = date.today()

    print('=' * 64)
    print('  数据可用性时间戳对齐层 · 自检')
    print('=' * 64)

    # 1. 字段级截止日期
    print(f'\n📅 今日({today}) 15:00 决策时的数据截止日期:')
    print(f'  {"表.字段":<40s} {"截止日期":>12s}  {"滞后":>5s}')
    print(f'  {"-"*60}')
    for table in ['kline_daily', 'macro_indicators', 'margin_trading',
                   'market_sentiment', 'news_articles', 'financial_statements']:
        table_info = da.tables.get(table, {})
        cols = table_info.get('columns', {})
        for col in cols:
            cutoff = da.get_cutoff(table, col, today, '15:00')
            lag = (today - cutoff).days
            print(f'  {table}.{col:<30s} {str(cutoff):>12s}  {lag:>3}天')

    # 2. 实盘新鲜度检查
    print(f'\n🔍 实盘新鲜度检查:')
    fresh, issues = da.check_live_freshness()
    for issue in issues:
        print(f'  {issue}')
    print(f'\n  总体: {"✅ 全部新鲜" if fresh else "🔴 存在过期数据, 需先采集"}')

    # 3. 脏数据警告
    print(f'\n⚠ 已知脏数据风险:')
    for issue in da.known_issues:
        print(f'  [{issue["severity"]}] {issue["issue"]}')
        print(f'         → {issue["mitigation"]}')

    # 4. 新闻时间戳检查
    print(f'\n📰 新闻时间戳错位检查:')
    news_warnings = da.detect_news_timestamp_issues()
    if news_warnings:
        for w in news_warnings:
            print(f'  {w}')
    else:
        print(f'  未检测到明显的时间戳错位')

    print(f'\n{"=" * 64}')
    print(f'  数据可用性时间表已加载: {len(da.tables)}张表')
