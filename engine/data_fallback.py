# -*- coding: utf-8 -*-
"""
多源数据降级引擎 v3.1
====================
问题: 系统高度依赖AKShare, 存在限流和不稳定风险
方案: 四源降级链 + 数据质量评分 + 增量同步

降级链: AKShare → Baostock → Tushare → 新浪财经 → DuckDB缓存

学术依据:
  - 数据质量维度: 完整性/及时性/准确性/一致性 [Wang & Strong 1996, J. Management Information Systems]
  - 降级模式: Circuit Breaker [Nygard 2007, "Release It!"]
"""
import sys, os, time, json, ssl, warnings
from datetime import datetime, date, timedelta
import pandas as pd
import numpy as np
import duckdb

ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'
warnings.filterwarnings('ignore')

DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
BASE = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════
# 数据质量印章 — 铁律#9: 每条数据标来源/新鲜度/置信度/降级路径
# ═══════════════════════════════════════════

class DataStamp:
    """每条数据必须带的品质印章"""
    def __init__(self, source, freshness_days, confidence, fallback_path=None):
        self.source = source                # 数据源名称
        self.freshness_days = freshness_days  # 数据落后天数
        self.confidence = confidence        # 置信度 0-100
        self.fallback_path = fallback_path or []  # 降级路径

    @property
    def is_primary(self):
        return self.source == 'AKShare'

    @property
    def is_fresh(self):
        return self.freshness_days <= 1

    @property
    def is_degraded(self):
        return self.source not in ('AKShare',)

    @property
    def label(self):
        """单行质量标签 — ASCII安全"""
        tags = []
        if self.is_degraded:
            tags.append('[降级]')
        if not self.is_fresh:
            tags.append(f'[滞后{self.freshness_days}天]')
        if self.source == 'DuckDB缓存' or self.source == 'DuckDB缓存(兜底)':
            tags.append('[缓存兜底]')
        if not tags:
            tags.append('[OK]')
        return f"源={self.source} 置信={self.confidence}% {' '.join(tags)}"

    @property
    def warning(self):
        """醒目警告(用于报告首行) — ASCII安全, 无emoji"""
        warnings = []
        if self.is_degraded:
            warnings.append(f'[!] 数据来自降级源[{self.source}]而非主源AKShare')
        if not self.is_fresh:
            warnings.append(f'[!] 数据滞后{self.freshness_days}天')
        if self.source == 'DuckDB缓存(兜底)':
            warnings.append('[!] 所有在线源均不可用, 使用本地缓存')
        return '\n'.join(warnings) if warnings else '[OK] 数据正常(主源AKShare, 新鲜)'

    def to_dict(self):
        return {
            'source': self.source,
            'freshness_days': self.freshness_days,
            'confidence': int(self.confidence),
            'is_primary': self.is_primary,
            'is_fresh': self.is_fresh,
            'is_degraded': self.is_degraded,
            'fallback_path': self.fallback_path,
        }


def make_stamp(df, source_name, quality_score=None):
    """从DataFrame和质量评分生成DataStamp"""
    days = 999
    if df is not None and not df.empty and 'trade_date' in df.columns:
        try:
            latest = pd.to_datetime(df['trade_date'].max())
            days = (date.today() - latest.date()).days
        except:
            pass
    confidence = quality_score['total'] if quality_score else 50
    return DataStamp(source_name, days, confidence)


# ═══════════════════════════════════════════
# 数据质量评分系统 [Wang & Strong 1996]
# ═══════════════════════════════════════════

class DataQualityScorer:
    """
    评估每个数据源的实时质量

    四维度(每维0-25分, 满分100):
      - 完整性: 必需字段是否齐全, 缺失率
      - 及时性: 数据延迟(最新日期 vs 今天)
      - 准确性: 值是否在合理范围(无NaN/Inf/离群)
      - 一致性: 与前一交易日数据是否连续(无异常跳变)
    """

    def __init__(self):
        self.scores = {}  # {source_name: {completeness, timeliness, accuracy, consistency, total}}

    def score_source(self, name, df, required_cols, date_col='trade_date', value_cols=None):
        """
        评分单个数据源

        Args:
            name: 数据源名称
            df: DataFrame
            required_cols: 必需列名列表
            date_col: 日期列名
            value_cols: 需要检查的值列

        Returns:
            dict: {completeness, timeliness, accuracy, consistency, total}
        """
        if df is None or df.empty:
            self.scores[name] = {'completeness': 0, 'timeliness': 0,
                                 'accuracy': 0, 'consistency': 0, 'total': 0,
                                 'status': 'FAIL'}
            return self.scores[name]

        # 1. 完整性 (25分): 必需列是否齐全
        missing_cols = [c for c in required_cols if c not in df.columns]
        completeness = 25 * (1 - len(missing_cols) / max(len(required_cols), 1))

        # 2. 及时性 (25分): 最新数据日期距今天数
        timeliness = 0
        if date_col in df.columns:
            try:
                latest = pd.to_datetime(df[date_col].max())
                days_behind = (date.today() - latest.date()).days
                if days_behind <= 0: timeliness = 25
                elif days_behind <= 1: timeliness = 22
                elif days_behind <= 3: timeliness = 18
                elif days_behind <= 7: timeliness = 12
                else: timeliness = 5
            except:
                timeliness = 10

        # 3. 准确性 (25分): NaN和异常值检查
        accuracy = 25
        if value_cols:
            nan_count = 0
            total_count = 0
            for col in value_cols:
                if col in df.columns:
                    nan_count += df[col].isna().sum()
                    total_count += len(df)
            if total_count > 0:
                nan_rate = nan_count / total_count
                accuracy -= int(nan_rate * 50)  # 每1% NaN扣0.5分
            accuracy = max(0, accuracy)

        # 4. 一致性 (25分): 与DuckDB缓存比对
        consistency = 15  # 基准分(无缓存比对时)
        try:
            conn = duckdb.connect(DB)
            cached_latest = conn.execute(
                f"SELECT MAX({date_col}) FROM kline_daily"
            ).fetchone()[0]
            conn.close()
            if cached_latest and date_col in df.columns:
                df_latest = str(df[date_col].max())
                if str(cached_latest) == df_latest:
                    consistency = 25  # 与缓存一致
                else:
                    consistency = 15  # 新数据, 无法比对
        except:
            pass

        total = completeness + timeliness + accuracy + consistency
        status = 'PASS' if total >= 70 else ('WARN' if total >= 50 else 'FAIL')

        self.scores[name] = {
            'completeness': round(completeness, 1),
            'timeliness': round(timeliness, 1),
            'accuracy': round(accuracy, 1),
            'consistency': round(consistency, 1),
            'total': round(total, 1),
            'status': status,
            'missing_cols': missing_cols,
        }
        return self.scores[name]

    def best_source(self):
        """返回当前最高分数据源"""
        if not self.scores:
            return None
        return max(self.scores, key=lambda k: self.scores[k]['total'])

    def report(self):
        """质量评分报告"""
        lines = ['数据源质量评分:']
        for name, s in sorted(self.scores.items(), key=lambda x: x[1]['total'], reverse=True):
            lines.append(
                f'  {name:12s}: {s["total"]:5.0f}分 '
                f'(完整{s["completeness"]:.0f} 及时{s["timeliness"]:.0f} '
                f'准确{s["accuracy"]:.0f} 一致{s["consistency"]:.0f}) [{s["status"]}]'
            )
        return '\n'.join(lines)


# ═══════════════════════════════════════════
# Tushare 数据源 (免费版)
# ═══════════════════════════════════════════

def fetch_tushare(ts_code, start_date='20250101', token=None):
    """
    Tushare免费版 — 个股日线

    免费版限制: 每分钟200次, 部分接口需积分
    本函数使用基础接口 stock_daily, 免费可用

    Args:
        ts_code: Tushare格式代码 (如 '000001.SZ')
        start_date: 起始日期
        token: Tushare API token (默认从环境变量读取)

    Returns:
        DataFrame 或 None
    """
    if token is None:
        token = os.environ.get('TUSHARE_TOKEN', '')
    if not token:
        return None  # 无token, 静默跳过

    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
        df = pro.daily(ts_code=ts_code, start_date=start_date)
        if df is not None and not df.empty:
            df = df.rename(columns={
                'trade_date': 'trade_date',
                'open': 'open', 'close': 'close',
                'high': 'high', 'low': 'low',
                'vol': 'vol', 'amount': 'amount'
            })
            df['ts_code'] = ts_code
            return df
    except ImportError:
        pass  # tushare未安装
    except Exception as e:
        pass
    return None


# ═══════════════════════════════════════════
# easyquotation 数据源 (Sina/Tencent 双通道)
# ═══════════════════════════════════════════

def fetch_easyquotation_realtime(codes, source='tencent'):
    """
    easyquotation 实时行情 — 新浪/腾讯双通道

    优势: 免费、无频率限制、延迟<3秒
    数据: 现价/涨跌幅/PE/PB/市值/成交量/买卖盘五档

    Args:
        codes: 代码列表, 如 ['000001', '600519']
        source: 'sina' | 'tencent' (腾讯字段更全)

    Returns:
        dict {code: {now, open, high, low, close, volume, PE, PB, ...}}
    """
    try:
        import sys
        EQ_PATH = os.path.join(BASE, '..', 'easyquotation')
        if EQ_PATH not in sys.path:
            sys.path.insert(0, EQ_PATH)
        from easyquotation.api import use

        if source == 'tencent':
            q = use('tencent')
        else:
            q = use('sina')

        # 格式化为腾讯/新浪格式: sh000001, sz000001
        formatted = []
        for c in codes:
            c = str(c).replace('.SH', '').replace('.SZ', '')
            if c.startswith('sh') or c.startswith('sz'):
                formatted.append(c)
            elif c.startswith(('0', '3')):
                formatted.append(f'sz{c}')
            elif c.startswith(('6', '9')):
                formatted.append(f'sh{c}')
            else:
                formatted.append(f'sh{c}')

        result = q.stocks(formatted)
        return result
    except Exception as e:
        return None


def fetch_easyquotation_kline(code, days=365):
    """
    easyquotation 日K线 — 腾讯源

    覆盖: 港股(腾讯源支持), A股用新浪日K线接口兜底

    Args:
        code: 股票代码
        days: 获取天数

    Returns:
        DataFrame 或 None
    """
    try:
        import sys
        import pandas as pd
        EQ_PATH = os.path.join(BASE, '..', 'easyquotation')
        if EQ_PATH not in sys.path:
            sys.path.insert(0, EQ_PATH)

        # A股日K线: 复用新浪接口(已在fetch_sina中实现)
        # 港股日K线: easyquotation DayKline
        if 'hk' in code.lower():
            from easyquotation.api import use
            q = use('daykline')
            code_clean = code.replace('hk', '').replace('.HK', '')
            data = q.stocks([code_clean], day=days)
            if data and code_clean in data:
                rows = data[code_clean]
                df = pd.DataFrame(rows)
                if not df.empty:
                    for c in ['open', 'close', 'high', 'low', 'volume']:
                        if c in df.columns:
                            df[c] = df[c].astype(float)
                    df = df.rename(columns={'date': 'trade_date', 'volume': 'vol'})
                    df['ts_code'] = code
                    return df
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════
# 新浪财经 数据源 (HTTP直连)
# ═══════════════════════════════════════════

def fetch_sina(symbol, market='sh', days=365):
    """
    新浪财经 — 个股/指数日线

    新浪接口免费, 无频率限制, 但数据质量低于AKShare

    Args:
        symbol: 股票代码(纯数字, 如 '000300')
        market: 'sh' 或 'sz'
        days: 获取天数

    Returns:
        DataFrame 或 None
    """
    try:
        import requests
        # 新浪日K线接口 (公开, 无需API key)
        # 格式: http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
        url = 'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData'
        params = {
            'symbol': f'{market}{symbol}',
            'scale': '240',  # 日线
            'ma': 'no',
            'datalen': days,
        }
        resp = requests.get(url, params=params, timeout=15, verify=False)
        data = resp.json()
        if not data or not isinstance(data, list):
            return None

        records = []
        for item in data:
            records.append({
                'trade_date': item.get('day', ''),
                'open': float(item.get('open', 0)),
                'high': float(item.get('high', 0)),
                'low': float(item.get('low', 0)),
                'close': float(item.get('close', 0)),
                'vol': float(item.get('volume', 0)),
            })
        df = pd.DataFrame(records)
        if not df.empty:
            df['ts_code'] = f'{market}{symbol}'
        return df
    except ImportError:
        pass
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════
# 四源降级采集器
# ═══════════════════════════════════════════

class MultiSourceCollector:
    """
    五源降级采集器 + 自愈机制

    降级链: AKShare → Baostock → easyquotation → Tushare → 新浪 → DuckDB缓存

    铁律#8: 每一层失败自动重试2次(间隔2s)，还失败降级。DuckDB最终兜底，永不空白返回。
    连续失败≥3次自动降权，健康检查每10分钟探测一次。

    新增 easyquotation (5228★): Sina/Tencent双通道实时行情, 免费无频率限制

    用法:
        collector = MultiSourceCollector()
        df, source, quality = collector.fetch_kline('000300.SH')
        quote = collector.fetch_realtime(['000001', '600519'])
        status = collector.health_check()  # 所有源可用性探测
    """

    MAX_RETRIES = 2       # 每源最多重试次数
    RETRY_DELAY = 2.0     # 重试间隔(秒)
    FAIL_THRESHOLD = 3    # 连续失败次数→降权

    def __init__(self):
        self.scorer = DataQualityScorer()
        self.required_cols = ['trade_date', 'open', 'close', 'high', 'low', 'vol']
        # 自愈追踪: {source_name: {failures: int, last_success: datetime, weight: float}}
        self.source_health = {
            'AKShare': {'failures': 0, 'last_success': None, 'weight': 1.0},
            'Baostock': {'failures': 0, 'last_success': None, 'weight': 1.0},
            'easyquotation': {'failures': 0, 'last_success': None, 'weight': 1.0},
            'Tushare': {'failures': 0, 'last_success': None, 'weight': 1.0},
            '新浪财经': {'failures': 0, 'last_success': None, 'weight': 1.0},
            'DuckDB缓存': {'failures': 0, 'last_success': None, 'weight': 1.0},
        }

    def _retry_wrapper(self, fetch_fn, source_name, *args, **kwargs):
        """带重试的数据源包装器。失败自动重试MAX_RETRIES次，记录健康状态"""
        import time as _time
        last_err = None
        for attempt in range(1 + self.MAX_RETRIES):
            try:
                result = fetch_fn(*args, **kwargs)
                if result is not None and (not hasattr(result, 'empty') or not result.empty):
                    # 成功 → 恢复权重
                    self.source_health[source_name]['failures'] = 0
                    self.source_health[source_name]['last_success'] = datetime.now()
                    self.source_health[source_name]['weight'] = min(1.0,
                        self.source_health[source_name]['weight'] + 0.1)
                    return result
                last_err = Exception(f"{source_name}: 返回空数据")
            except Exception as e:
                last_err = e
            if attempt < self.MAX_RETRIES:
                _time.sleep(self.RETRY_DELAY)

        # 全部重试失败 → 记录
        self.source_health[source_name]['failures'] += 1
        self.source_health[source_name]['weight'] = max(0.1,
            self.source_health[source_name]['weight'] - 0.2)
        return None

    def _source_skip(self, source_name):
        """检查是否跳过该源(连续失败≥FAIL_THRESHOLD)"""
        return self.source_health[source_name]['failures'] >= self.FAIL_THRESHOLD

    def fetch_kline(self, code, start_date='20250101'):
        """
        五源降级获取K线数据 — 铁律#8: 永不空白返回

        Returns:
            (DataFrame, source_name, quality_score, DataStamp)
            DataFrame永远不会是None —— DuckDB兜底
        """
        df = None
        source = ''
        fallback_path = []  # v5.1: 记录降级路径
        import time as _time

        # L1: AKShare (主源)
        if not self._source_skip('AKShare'):
            fallback_path.append('AKShare')
            df = self._retry_wrapper(
                lambda: _fetch_akshare_kline(code, start_date), 'AKShare')
            if df is not None:
                source = 'AKShare'

        # L2: Baostock
        if df is None and not self._source_skip('Baostock'):
            fallback_path.append('Baostock')
            df = self._retry_wrapper(
                lambda: _fetch_baostock_kline(code, start_date), 'Baostock')
            if df is not None:
                source = 'Baostock'

        # L3: easyquotation (实时行情 + K线兜底)
        if df is None and not self._source_skip('easyquotation'):
            fallback_path.append('easyquotation')
            df = self._retry_wrapper(
                lambda: fetch_easyquotation_kline(code), 'easyquotation')
            if df is not None:
                source = 'easyquotation'

        # L4: Tushare
        if df is None and not self._source_skip('Tushare'):
            fallback_path.append('Tushare')
            ts_code = code.replace('sh', '').replace('sz', '') + \
                      ('.SH' if 'sh' in code else '.SZ')
            df = self._retry_wrapper(
                lambda: fetch_tushare(ts_code, start_date), 'Tushare')
            if df is not None:
                source = 'Tushare'

        # L5: 新浪财经
        if df is None and not self._source_skip('新浪财经'):
            fallback_path.append('新浪财经')
            parts = code.replace('sh', '').replace('sz', '').replace('.SH', '').replace('.SZ', '')
            market = 'sh' if ('sh' in code or '.SH' in code) else 'sz'
            df = self._retry_wrapper(
                lambda: fetch_sina(parts, market), '新浪财经')
            if df is not None:
                source = '新浪财经'

        # L6: DuckDB缓存 (最终兜底 —— 铁律#8: 永不空白)
        if df is None:
            fallback_path.append('DuckDB缓存')
            df = self._retry_wrapper(
                lambda: _fetch_duckdb_cache(code), 'DuckDB缓存')
            if df is not None:
                source = 'DuckDB缓存(兜底)'

        # v5.1: 全源失败时返回空DataFrame而非None, 维持铁律#8
        # 下游模块可安全调用 .fillna()/.iterrows() 不会AttributeError雪崩
        if df is None or (hasattr(df, 'empty') and df.empty):
            import pandas as _pd
            df = _pd.DataFrame(columns=self.required_cols + ['amount', 'change_pct'])
            df['ts_code'] = code  # type: ignore
            df.attrs['data_stamp'] = '[全源溃散_强行兜底]'
            df.attrs['fallback_path'] = fallback_path
            source = 'DuckDB缓存(兜底)'
            if 'DuckDB缓存' not in fallback_path:
                fallback_path.append('DuckDB缓存')

        # 评分 + 印章 (v5.1: 记录降级路径)
        quality = self.scorer.score_source(source, df, self.required_cols)
        stamp = make_stamp(df, source, quality)
        stamp.fallback_path = fallback_path  # 记录完整降级链

        return df, source, quality, stamp

    def fetch_realtime(self, codes):
        """
        实时行情 — easyquotation双通道 + 重试

        腾讯(字段全) → 新浪(兜底) → 空dict绝不报错

        Returns:
            (dict, DataStamp)
        """
        result = self._retry_wrapper(
            lambda: fetch_easyquotation_realtime(codes, source='tencent'),
            'easyquotation')
        source = 'easyquotation(腾讯)'
        if result is None:
            result = fetch_easyquotation_realtime(codes, source='sina')
            source = 'easyquotation(新浪)'
        stamp = DataStamp(source, 0, 90 if result else 0)
        return result or {}, stamp

    def health_check(self):
        """
        数据源健康探测 — 所有源可用性 + 数据新鲜度

        Returns:
            {status: 'OK'|'DEGRADED'|'CRITICAL',
             sources: {name: {available, failures, weight, last_success}},
             freshness: {latest_date, days_behind, ok}}
        """
        report = {'status': 'OK', 'sources': {}, 'freshness': {}}

        # 测各源
        test_code = '000001.SZ'
        test_start = '20260101'
        sources_tests = [
            ('AKShare', lambda: _fetch_akshare_kline(test_code, test_start)),
            ('Baostock', lambda: _fetch_baostock_kline(test_code, test_start)),
            ('easyquotation', lambda: fetch_easyquotation_realtime(['000001'])),
            ('Tushare', lambda: fetch_tushare('000001.SZ', test_start)),
            ('新浪财经', lambda: fetch_sina('000001', 'sz')),
            ('DuckDB缓存', lambda: _fetch_duckdb_cache(test_code)),
        ]

        available_count = 0
        for name, fn in sources_tests:
            try:
                result = fn()
                available = result is not None and (not hasattr(result, 'empty') or not result.empty)
            except:
                available = False

            report['sources'][name] = {
                'available': available,
                'failures': self.source_health[name]['failures'],
                'weight': round(self.source_health[name]['weight'], 1),
                'last_success': str(self.source_health[name]['last_success'])[:19] if self.source_health[name]['last_success'] else 'never',
            }
            if available:
                available_count += 1

        # 数据新鲜度
        try:
            conn = duckdb.connect(DB)
            latest = conn.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
            conn.close()
            latest_str = str(latest)[:10] if latest else 'unknown'
            days_behind = (date.today() - date.fromisoformat(latest_str)).days if latest else 999
            report['freshness'] = {
                'latest_date': latest_str,
                'days_behind': days_behind,
                'ok': days_behind <= 1,
            }
        except:
            report['freshness'] = {'latest_date': 'error', 'days_behind': 999, 'ok': False}

        if available_count == 0:
            report['status'] = 'CRITICAL'
        elif available_count < 3 or not report['freshness']['ok']:
            report['status'] = 'DEGRADED'
        else:
            report['status'] = 'OK'

        return report


# ═══════════════════════════════════════════
# 各数据源提取函数（供_retry_wrapper调用）
# ═══════════════════════════════════════════

def _fetch_akshare_kline(code, start_date):
    import akshare as ak
    if code.startswith('sh') or code.startswith('sz'):
        df = ak.stock_zh_index_daily(symbol=code)
        if df is not None and not df.empty:
            # 指数数据列名归一化
            rename_map = {k: v for k, v in {
                'date': 'trade_date', 'open': 'open', 'close': 'close',
                'high': 'high', 'low': 'low', 'volume': 'vol'
            }.items() if k in df.columns}
            if rename_map:
                df = df.rename(columns=rename_map)
            df['ts_code'] = code
        return df
    elif '.SH' in code or '.SZ' in code:
        symbol = code.replace('.SH', '').replace('.SZ', '')
        df = ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date=start_date, adjust='qfq')
        if df is not None and not df.empty:
            df['ts_code'] = code
            df = df.rename(columns={'日期': 'trade_date', '开盘': 'open', '收盘': 'close',
                                    '最高': 'high', '最低': 'low', '成交量': 'vol'})
        return df
    return None

def _fetch_baostock_kline(code, start_date):
    import baostock as bs
    bs.login()
    code_bs = code.replace('sh', 'sh.').replace('sz', 'sz.')
    rs = bs.query_history_k_data_plus(code_bs,
        'date,open,close,high,low,volume', start_date=start_date, frequency='d')
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    if rows:
        df = pd.DataFrame(rows, columns=['trade_date', 'open', 'close', 'high', 'low', 'vol'])
        for c in ['open', 'close', 'high', 'low', 'vol']:
            df[c] = df[c].astype(float)
        df['ts_code'] = code
        return df
    return None

def _fetch_duckdb_cache(code):
    conn = duckdb.connect(DB)
    df = conn.execute(f"""
        SELECT trade_date, open, close, high, low, vol
        FROM kline_daily WHERE ts_code='{code}'
        ORDER BY trade_date DESC LIMIT 365
    """).fetchdf()
    conn.close()
    return df if not df.empty else None


# ═══════════════════════════════════════════
# 增量同步机制
# ═══════════════════════════════════════════

class IncrementalSyncer:
    """
    增量同步: 只拉取DuckDB最新日期之后的数据, 减少API调用

    原则:
      - 首次采集: 全量拉取5年
      - 增量更新: 只拉DB最新日期到今天的缺口
      - DB缺失超过7天: 降级为全量重拉
    """

    def __init__(self):
        self.db = DB

    def get_latest_date(self, ts_code):
        """获取DuckDB中某标的的最新日期"""
        try:
            conn = duckdb.connect(self.db)
            latest = conn.execute(
                f"SELECT MAX(trade_date) FROM kline_daily WHERE ts_code='{ts_code}'"
            ).fetchone()[0]
            conn.close()
            return str(latest)[:10] if latest else None
        except:
            return None

    def sync(self, code, collector=None):
        """
        增量同步单个标的

        Returns:
            {status, new_rows, source}
        """
        if collector is None:
            collector = MultiSourceCollector()

        latest = self.get_latest_date(code)
        today = date.today().isoformat()

        if latest is None:
            # 首次: 全量拉取
            df, source, quality, stamp = collector.fetch_kline(code, '20210101')
            status = '首次全量'
        else:
            days_behind = (date.today() - date.fromisoformat(latest)).days
            if days_behind <= 0:
                return {'status': '已是最新', 'new_rows': 0, 'source': 'DuckDB',
                        'stamp': DataStamp('DuckDB缓存', 0, 90).to_dict()}
            elif days_behind <= 7:
                # 增量: 只拉缺口
                df, source, quality, stamp = collector.fetch_kline(code, latest)
                status = f'增量+{days_behind}天'
            else:
                # 缺口太大: 全量重拉
                df, source, quality, stamp = collector.fetch_kline(code, '20210101')
                status = f'全量重拉(缺口{days_behind}天)'

        if df is not None and not df.empty:
            # 列名归一化(各源可能不同)
            DF_RENAME_MAP = {'date': 'trade_date', 'volume': 'vol',
                             '开盘': 'open', '收盘': 'close', '最高': 'high', '最低': 'low', '成交量': 'vol',
                             '日期': 'trade_date'}
            for old_c, new_c in DF_RENAME_MAP.items():
                if old_c in df.columns and new_c not in df.columns:
                    df = df.rename(columns={old_c: new_c})

            # 写入DuckDB (去重)
            if 'trade_date' in df.columns:
                conn = duckdb.connect(self.db)
                existing = conn.execute(
                    f"SELECT trade_date FROM kline_daily WHERE ts_code='{code}'"
                ).fetchdf()
                if not existing.empty:
                    existing_dates = set(existing['trade_date'].astype(str))
                    df = df[~df['trade_date'].astype(str).isin(existing_dates)]
                if not df.empty:
                    cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol']
                    df_in = df[[c for c in cols if c in df.columns]]
                    conn.execute(
                        f"INSERT INTO kline_daily (ts_code, trade_date, open, high, low, close, vol) SELECT * FROM df_in"
                    )
                new_rows = len(df)
                conn.close()
            else:
                new_rows = 0
            return {'status': status, 'new_rows': new_rows, 'source': source,
                    'quality': quality['total'] if quality else 0,
                    'stamp': stamp.to_dict()}
        return {'status': '全源失败', 'new_rows': 0, 'source': '无',
                'stamp': DataStamp('无', 999, 0).to_dict()}


if __name__ == '__main__':
    print('=== 数据质量印章 + 五源降级链 ===')
    c = MultiSourceCollector()
    df, source, quality, stamp = c.fetch_kline('000001.SZ')
    print(f'  [{stamp.label}]')
    if '[OK]' not in stamp.warning:
        print(f'  {stamp.warning}')
    print(f'  源={source} 行={len(df) if df is not None else 0} 质量={quality["total"] if quality else 0}分')

    print('\n=== 实时行情(带印章) ===')
    quotes, qstamp = c.fetch_realtime(['000001', '600519'])
    print(f'  [{qstamp.label}]')
    for code, info in quotes.items():
        print(f'  {code} {info.get("name","?")} now={info.get("now","?")} PE={info.get("PE","?")}')

    print('\n=== 增量同步 ===')
    syncer = IncrementalSyncer()
    result = syncer.sync('sh000300')
    s = result.get('stamp', {})
    print(f'  状态={result["status"]} 源={result["source"]} 印章={s}')
