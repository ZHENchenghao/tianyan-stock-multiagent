# -*- coding: utf-8 -*-
"""
天眼 · Market Regime 分類器 v1.0
================================
對沖基金級別市場狀態分類 —— 四象限 + 輪動烈度 + 新聞權重乘數

升級點 (vs market_state.py):
  1. 市場廣度（上漲家數/總交易）→ 區分 Broad vs Concentrated
  2. 板塊輪動烈度 → 5日行業排名相關性
  3. 新聞信號權重乘數 → 熊市自動打折利多信號

輸出:
  寫入 market_state.json（合併 O'Neil 狀態） + market_regime.json（新增字段）

數據源:
  - DuckDB: kline_daily（漲跌家數）, market_sentiment（情緒+恐貪）
  - 文件: market_state.json（O'Neil 狀態）
  - 降級路徑: kline_daily 不足 → market_sentiment 情緒打分 → 手動標記

用法:
  python engine/market_regime.py           # 輸出報告
  python engine/market_regime.py --json    # 純 JSON 輸出
  python tianyan.py regime                 # CLI 入口
"""

import sys, os, json
from datetime import date, datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
STATE_FILE = os.path.join(ROOT, 'market_state.json')
REGIME_FILE = os.path.join(ROOT, 'market_regime.json')

try:
    import duckdb
    DB = r'D:\FreeFinanceData\data\duckdb\finance.db'
    HAS_DB = os.path.exists(DB)
except ImportError:
    duckdb = None
    HAS_DB = False

try:
    import numpy as np
    HAS_NP = True
except ImportError:
    HAS_NP = False

# ═══════════════════════════════════════════
# 數據獲取層（每層自帶降級）
# ═══════════════════════════════════════════

def _q(sql, params=None):
    """安全查詢 DuckDB"""
    if not HAS_DB:
        return None
    try:
        conn = duckdb.connect(DB)
        result = conn.execute(sql, params or []).fetchdf()
        conn.close()
        return result
    except Exception as e:
        print(f'[market_regime] DuckDB 查詢失敗: {e}')
        return None


def calc_market_breadth():
    """
    計算市場廣度 = 上漲家數 / 總交易家數
    數據源: kline_daily（全市場個股日線）
    降級: change_pct 不可靠 → 用 close vs LAG(close) 手動算
    降級: 覆蓋面不足 → market_sentiment 情緒估代理
    """
    df = _q("""
        WITH prices AS (
            SELECT ts_code, trade_date, close,
                   LAG(close) OVER (PARTITION BY ts_code ORDER BY trade_date) as prev_close
            FROM kline_daily
            WHERE trade_date >= CURRENT_DATE - INTERVAL 10 DAY
        )
        SELECT trade_date,
               COUNT(*) as total,
               SUM(CASE WHEN close > prev_close THEN 1 ELSE 0 END) as up_count,
               SUM(CASE WHEN close < prev_close THEN 1 ELSE 0 END) as down_count,
               SUM(CASE WHEN close = prev_close THEN 1 ELSE 0 END) as flat_count
        FROM prices
        WHERE prev_close IS NOT NULL AND prev_close > 0
        GROUP BY trade_date
        ORDER BY trade_date DESC
    """)

    if df is None or df.empty:
        return None

    latest = df.iloc[0]
    total = int(latest['total'])
    up_count = int(latest['up_count'])
    down_count = int(latest['down_count'])

    if total < 100:
        # 覆蓋面太小，用 market_sentiment 做代理
        sent = get_market_sentiment()
        if sent:
            score = sent.get('emotion_score', 50)
            proxy_breadth = score / 100.0
            proxy_breadth = max(0.10, min(0.85, proxy_breadth))
            return {
                'breadth': round(proxy_breadth, 3),
                'breadth_5d': round(proxy_breadth, 3),
                'total_stocks': total,
                'up_count': up_count,
                'down_count': down_count,
                'trend': 'sentiment_proxy',
                'source': 'sentiment_proxy_low_coverage',
            }
        return None

    breadth = up_count / total if total > 0 else 0.5

    # 計算 5 日均值用於趨勢判斷
    avg_5d = float(df['total'].mean()) if len(df) > 0 else total
    avg_up_5d = float(df['up_count'].mean()) if len(df) > 0 else up_count
    breadth_5d = avg_up_5d / avg_5d if avg_5d > 0 else breadth

    return {
        'breadth': round(breadth, 3),
        'breadth_5d': round(breadth_5d, 3),
        'total_stocks': total,
        'up_count': up_count,
        'down_count': int(latest['down_count']),
        'trend': 'improving' if breadth > breadth_5d else 'deteriorating',
    }


def calc_rotation_intensity():
    """
    計算板塊輪動烈度：
    比較最近 2 個交易日的行業排名相關係數
    相關性低 → 板塊快速輪動 → 高烈度
    相關性高 → 板塊排名穩定 → 低烈度

    數據源: proxy_industry_daily (行業代理日線)
    降級: 用 kline_daily 按 ts_code 分組模擬行業
    降級2: 無法計算 → 回 0.5（中性）
    """
    # 方法1: proxy_industry_daily
    df = _q("""
        WITH ranked AS (
            SELECT industry, trade_date, AVG(close) as avg_close
            FROM proxy_industry_daily
            WHERE trade_date >= CURRENT_DATE - INTERVAL 30 DAY
            GROUP BY industry, trade_date
        ),
        daily_return AS (
            SELECT industry, trade_date,
                   (avg_close - LAG(avg_close) OVER (PARTITION BY industry ORDER BY trade_date))
                   / NULLIF(LAG(avg_close) OVER (PARTITION BY industry ORDER BY trade_DATE), 0) as ret
            FROM ranked
        )
        SELECT trade_date, industry, ret
        FROM daily_return
        WHERE ret IS NOT NULL
          AND trade_date >= CURRENT_DATE - INTERVAL 10 DAY
        ORDER BY trade_date DESC, ret DESC
    """)

    if df is None or df.empty:
        # 降級: 用 kline_daily 按股票代碼前2位分組（近似行業）
        df = _q("""
            WITH stock_ret AS (
                SELECT ts_code, trade_date, change_pct
                FROM kline_daily
                WHERE trade_date >= CURRENT_DATE - INTERVAL 10 DAY
                  AND change_pct IS NOT NULL
                  AND is_st = false
            )
            SELECT trade_date,
                   SUBSTRING(ts_code, 1, 2) as sector_group,
                   AVG(change_pct) as avg_ret
            FROM stock_ret
            GROUP BY trade_date, SUBSTRING(ts_code, 1, 2)
            ORDER BY trade_date DESC, sector_group
        """, [])

    if df is None or df.empty:
        return {'intensity': 0.5, 'source': 'fallback_default', 'sector_count': 0}

    # 取最近兩個交易日
    dates = sorted(df['trade_date'].unique(), reverse=True)
    if len(dates) < 2:
        return {'intensity': 0.5, 'source': 'insufficient_dates', 'sector_count': len(df)}

    d1, d2 = dates[0], dates[1]

    # 每個交易日按行業/分組排名的回報
    d1_data = df[df['trade_date'] == d1].copy()
    d2_data = df[df['trade_date'] == d2].copy()

    # 找兩天共有的行業
    if 'industry' in df.columns:
        group_col = 'industry'
    else:
        group_col = 'sector_group'

    if 'avg_ret' in df.columns:
        ret_col = 'avg_ret'
    else:
        ret_col = 'ret'

    common = set(d1_data[group_col]) & set(d2_data[group_col])
    if len(common) < 3:
        return {'intensity': 0.5, 'source': 'insufficient_overlap', 'sector_count': len(common)}

    d1_sorted = d1_data[d1_data[group_col].isin(common)].sort_values(ret_col, ascending=False)
    d2_sorted = d2_data[d2_data[group_col].isin(common)].sort_values(ret_col, ascending=False)
    d1_ranks = {row[group_col]: rank for rank, (_, row) in enumerate(d1_sorted.iterrows())}
    d2_ranks = {row[group_col]: rank for rank, (_, row) in enumerate(d2_sorted.iterrows())}

    # Spearman 等級相關係數（簡化計算）
    n = len(common)
    sectors = sorted(common)
    try:
        d_sq_sum = sum((d1_ranks.get(s, 0) - d2_ranks.get(s, 0)) ** 2 for s in sectors)
        spearman = 1 - (6 * d_sq_sum) / (n * (n**2 - 1))
        spearman = max(-1.0, min(1.0, spearman))
    except ZeroDivisionError:
        spearman = 0.5

    # 轉換為輪動烈度：低相關 = 高輪動
    rotation_intensity = round(1.0 - abs(spearman), 3)

    return {
        'intensity': rotation_intensity,
        'spearman_r': round(spearman, 3),
        'source': 'proxy_industry' if 'industry' in df.columns else 'kline_group',
        'sector_count': n,
        'd1': str(d1),
        'd2': str(d2),
    }


def calc_volatility_state():
    """
    計算當前波動率狀態
    數據源: kline_daily 計算滬深300 20日波動率
    降級: market_sentiment 情緒分數
    降級2: 回 'Normal'
    """
    # 方法1: 滬深300 20日歷史波動率
    df = _q("""
        SELECT trade_date, change_pct
        FROM kline_daily
        WHERE ts_code LIKE '000300%'
          AND trade_date >= CURRENT_DATE - INTERVAL 60 DAY
        ORDER BY trade_date DESC
    """)

    if df is not None and len(df) >= 15:
        returns = df['change_pct'].dropna().values
        if len(returns) >= 15:
            vol_20d = float(np.std(returns[:20])) if HAS_NP else float(returns[:20].std())
            vol_60d = float(np.std(returns)) if HAS_NP else float(returns.std())

            # 相對歷史分位
            if vol_60d > 0:
                ratio = vol_20d / vol_60d
            else:
                ratio = 1.0

            if ratio < 0.7:
                state = 'Low'
            elif ratio > 1.5:
                state = 'High'
            else:
                state = 'Normal'

            return {
                'state': state,
                'vol_20d': round(vol_20d, 3),
                'vol_60d': round(vol_60d, 3),
                'ratio': round(ratio, 2),
                'source': 'kline_calculated',
            }

    # 降級: 用 market_sentiment 情緒分數推算
    sent = get_market_sentiment()
    if sent:
        score = sent.get('emotion_score', 50)
        greed_fear = sent.get('greed_fear_idx', 50)

        # 情緒極端 = 高波動
        if score < 30 or score > 80 or greed_fear < 25 or greed_fear > 80:
            state = 'High'
        elif score < 40 or score > 70:
            state = 'Normal'
        else:
            state = 'Low'

        return {
            'state': state,
            'vol_20d': None,
            'source': 'sentiment_proxy',
        }

    return {'state': 'Normal', 'vol_20d': None, 'source': 'fallback_default'}


def get_market_sentiment():
    """讀取最新的 market_sentiment 數據"""
    df = _q("""
        SELECT * FROM market_sentiment
        ORDER BY trade_date DESC LIMIT 1
    """)
    if df is not None and not df.empty:
        row = df.iloc[0]
        return {
            'trade_date': str(row['trade_date']),
            'limit_up_count': int(row.get('limit_up_count', 0)),
            'limit_down_count': int(row.get('limit_down_count', 0)),
            'bomb_rate': float(row.get('bomb_rate', 0)),
            'consecutive_max': int(row.get('consecutive_max', 0)),
            'promotion_rate': float(row.get('promotion_rate', 0)),
            'market_emotion': str(row.get('market_emotion', '中性')),
            'emotion_score': float(row.get('emotion_score', 50)),
            'greed_fear_idx': float(row.get('greed_fear_idx', 50)),
        }
    return None


def load_oneil_state():
    """讀取 O'Neil 狀態"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'oneil_state': 'confirmed_uptrend',
        'rally_day': 0,
    }


# ═══════════════════════════════════════════
# Regime 分類邏輯
# ═══════════════════════════════════════════

def classify_regime(oneil_state, breadth_data, volatility, sentiment):
    """
    分類為四象限 Regime

    決策樹:
      O'Neil = confirmed_uptrend / rally_attempt
        ├─ 廣度 > 45% → Bullish_Broad（普漲）
        └─ 廣度 ≤ 45% → Bullish_Concentrated（僅拉權重）

      O'Neil = correction / uptrend_pressure
        ├─ 波動率 = High + 情緒極端 → Bearish_Panic（恐慌暴跌）
        └─ 波動率 ≠ High → Bearish_Draining（陰跌）
    """
    state = oneil_state.get('oneil_state', 'confirmed_uptrend')

    is_bullish = state in ('confirmed_uptrend', 'rally_attempt')

    if is_bullish:
        if breadth_data and breadth_data['breadth'] > 0.45:
            regime = 'Bullish_Broad'
        else:
            regime = 'Bullish_Concentrated'
    else:
        # 熊市：區分恐慌 vs 陰跌
        if volatility['state'] == 'High':
            # 進一步檢查 sentiment
            if sentiment:
                emotion = sentiment.get('market_emotion', '')
                score = sentiment.get('emotion_score', 50)
                if emotion == '冰点' or score < 25:
                    regime = 'Bearish_Panic'
                else:
                    regime = 'Bearish_Draining'
            else:
                regime = 'Bearish_Draining'
        else:
            regime = 'Bearish_Draining'

    return regime


def calc_news_multiplier(regime, volatility, breadth_data, sentiment):
    """
    計算新聞信號權重乘數

    牛市普漲 → 1.0 (全額)
    牛市集中 → 0.7 (中小票信號打七折)
    熊市陰跌 → 0.5 (利好容易高開低走)
    熊市恐慌 → 0.3 (幾乎所有信號無效)
    """
    base = {
        'Bullish_Broad': 1.0,
        'Bullish_Concentrated': 0.7,
        'Bearish_Draining': 0.5,
        'Bearish_Panic': 0.3,
    }.get(regime, 0.5)

    # 微調1: 高波動 → 額外打折
    if volatility['state'] == 'High':
        base *= 0.8

    # 微調2: 市場廣度急劇惡化 → 額外打折
    if breadth_data and breadth_data.get('trend') == 'deteriorating':
        if breadth_data['breadth'] < breadth_data.get('breadth_5d', 0.5) - 0.1:
            base *= 0.85

    # 微調3: 炸板率過高 → 額外打折
    if sentiment and sentiment.get('bomb_rate', 0) > 0.3:
        base *= 0.85

    return round(min(1.0, base), 2)


# ═══════════════════════════════════════════
# 主運行函數
# ═══════════════════════════════════════════

def run_regime(json_only=False):
    """運行完整 Regime 分類並輸出"""
    errors = []

    # 1. O'Neil 狀態
    oneil = load_oneil_state()
    if not json_only:
        print(f'[1/5] O\'Neil 狀態: {oneil.get("oneil_state", "?")}')

    # 2. 市場廣度
    breadth = calc_market_breadth()
    if breadth:
        if not json_only:
            print(f'[2/5] 市場廣度: {breadth["breadth"]:.1%} ({breadth["up_count"]}/{breadth["total_stocks"]})')
    else:
        if not json_only:
            print('[2/5] 市場廣度: 數據不足，使用中性值')
        breadth = {'breadth': 0.50, 'breadth_5d': 0.50, 'trend': 'neutral'}

    # 3. 輪動烈度
    rotation = calc_rotation_intensity()
    if not json_only:
        print(f'[3/5] 輪動烈度: {rotation["intensity"]:.2f} (source={rotation["source"]})')

    # 4. 波動率
    volatility = calc_volatility_state()
    if not json_only:
        print(f'[4/5] 波動率: {volatility["state"]} (source={volatility["source"]})')

    # 5. 情緒
    sentiment = get_market_sentiment()
    if sentiment:
        if not json_only:
            print(f'[5/5] 市場情緒: {sentiment["market_emotion"]} (score={sentiment["emotion_score"]})')
    else:
        if not json_only:
            print('[5/5] 市場情緒: 無數據')

    # ── 裁決 ──
    regime = classify_regime(oneil, breadth, volatility, sentiment)
    news_mult = calc_news_multiplier(regime, volatility, breadth, sentiment)

    # ── 構建輸出 ──
    output = {
        'market_regime': regime,
        'volatility_state': volatility['state'],
        'rotation_intensity': rotation['intensity'],
        'news_alpha_multiplier': news_mult,
        'breadth': breadth['breadth'],
        'breadth_trend': breadth.get('trend', 'neutral'),
        'oneil_state': oneil.get('oneil_state', 'confirmed_uptrend'),
        'rally_day': oneil.get('rally_day', 0),
        'sentiment': sentiment.get('market_emotion', 'N/A') if sentiment else 'N/A',
        'emotion_score': sentiment.get('emotion_score', 50) if sentiment else 50,
        'volatility_detail': volatility,
        'rotation_detail': rotation,
        'breadth_detail': breadth,
        'last_update': str(date.today()),
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # 寫入文件
    with open(REGIME_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if json_only:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f'\n{"═" * 45}')
        print(f'  Regime: {regime}')
        print(f'  波動率: {volatility["state"]}')
        print(f'  輪動烈度: {rotation["intensity"]:.2f}')
        print(f'  新聞乘數: {news_mult}')
        print(f'  廣度: {breadth["breadth"]:.1%}')
        print(f'{"═" * 45}')
        print(f'  輸出: {REGIME_FILE}')

    return output


# ═══════════════════════════════════════════
# 倉位建議（給外部模組調用）
# ═══════════════════════════════════════════

def get_position_guide():
    """根據當前 Regime 給出倉位建議"""
    try:
        if os.path.exists(REGIME_FILE):
            with open(REGIME_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = run_regime(json_only=True)
    except Exception:
        data = {'market_regime': 'Bullish_Concentrated', 'news_alpha_multiplier': 0.7}

    regime = data['market_regime']

    guide = {
        'Bullish_Broad': {
            'position_pct': '80-100%',
            'style': '均衡配置，中小票+權重',
            'aggression': '可追突破',
        },
        'Bullish_Concentrated': {
            'position_pct': '50-70%',
            'style': '只跟權重+行業龍頭',
            'aggression': '中小票倉位減半',
        },
        'Bearish_Draining': {
            'position_pct': '20-30%',
            'style': '防禦板塊+現金',
            'aggression': '只做反彈，不做突破',
        },
        'Bearish_Panic': {
            'position_pct': '0-10%',
            'style': '現金為王/對沖',
            'aggression': '禁止買入，等FTD跟進日',
        },
    }

    return {
        **guide.get(regime, guide['Bullish_Concentrated']),
        'regime': regime,
        'news_multiplier': data['news_alpha_multiplier'],
    }


# ═══════════════════════════════════════════
# 自檢
# ═══════════════════════════════════════════

def _self_test():
    """內建自檢"""
    errors = []

    # 測試1: 分類邏輯
    assert classify_regime(
        {'oneil_state': 'confirmed_uptrend'},
        {'breadth': 0.60},
        {'state': 'Low'},
        None
    ) == 'Bullish_Broad', 'test1: 牛市普漲分類錯誤'

    # 測試2: 集中牛市
    assert classify_regime(
        {'oneil_state': 'confirmed_uptrend'},
        {'breadth': 0.30},
        {'state': 'Low'},
        None
    ) == 'Bullish_Concentrated', 'test2: 牛市集中分類錯誤'

    # 測試3: 陰跌
    assert classify_regime(
        {'oneil_state': 'correction'},
        {'breadth': 0.40},
        {'state': 'Normal'},
        None
    ) == 'Bearish_Draining', 'test3: 陰跌分類錯誤'

    # 測試4: 恐慌
    assert classify_regime(
        {'oneil_state': 'correction'},
        {'breadth': 0.20},
        {'state': 'High'},
        {'market_emotion': '冰点', 'emotion_score': 20}
    ) == 'Bearish_Panic', 'test4: 恐慌分類錯誤'

    # 測試5: 新聞乘數範圍
    mult = calc_news_multiplier('Bullish_Broad', {'state': 'Low'}, None, None)
    assert 0.5 <= mult <= 1.0, f'test5: 乘數 {mult} 超出範圍'

    pan_mult = calc_news_multiplier('Bearish_Panic', {'state': 'High'}, None, None)
    assert pan_mult <= 0.5, f'test5b: 恐慌乘數 {pan_mult} 應 ≤ 0.5'

    # 測試6: 廣度計算（如果 DuckDB 可用）
    if HAS_DB:
        breadth = calc_market_breadth()
        if breadth:
            assert 0.0 <= breadth['breadth'] <= 1.0, f'test6: 廣度 {breadth["breadth"]} 超出 [0,1]'

    if errors:
        print(f'[market_regime] SELF-TEST FAIL {len(errors)}/6:')
        for e in errors:
            print(f'  [FAIL] {e}')
        return False
    return True


# 模組載入時靜默自檢
try:
    _self_test()
except Exception as e:
    print(f'[market_regime] SELF-TEST CRASH: {e}')


if __name__ == '__main__':
    json_only = '--json' in sys.argv
    run_regime(json_only=json_only)

    if not json_only:
        print()
        print('倉位建議:', json.dumps(get_position_guide(), ensure_ascii=False, indent=2))
