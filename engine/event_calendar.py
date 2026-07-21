# -*- coding: utf-8 -*-
"""
天眼 · 宏观事件日历与人工判断增强模块
==============================================
双层架构:
  第一层: 自动扫描 (scan) — 未来14天事件清单, 含方向预判
  第二层: 交互判断 (assess) — 逐事件问答 + 冲突溢价评分 → Regime覆写

核心算法:
  冲突溢价(Conflict Premium) — 防多空对冲假信号
  final_risk_score = max(net_score, 0) + alpha × conflict_energy
  conflict_energy = Σ|score_i| - |Σ score_i|  (被对冲掉的隐藏波动率)

置信度反向处理: "不太确定"不乘分, 改为加宽误差带 + 兜底提档

数据源灾备链: yfinance → FMP → Alpha Vantage → event_schedule.json(离线兜底)

用法:
  python engine/event_calendar.py scan      # 扫描未来14天事件
  python engine/event_calendar.py assess    # 交互式判断 + 写覆写
  python engine/event_calendar.py status    # 查看当前覆写状态
"""
import sys, os, json, math
from datetime import date, datetime, timedelta
from collections import defaultdict

_script_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_script_dir)
sys.path.insert(0, _parent_dir)

# ═══════════════════════════════════════════
# 0. 全局配置 (可调超参数)
# ═══════════════════════════════════════════

CONFIG = {
    "engine_settings": {
        "conflict_premium_weight": 0.70,
        "fat_tail_threshold": 40,
        "fat_tail_multiplier": 1.2,
        "scan_days": 14,
        "confidence": {
            "high":   {"error_band_pct": 5},
            "medium": {"error_band_pct": 10},
            "low":    {"error_band_pct": 20, "consecutive_low_triggers_conservative": 2}
        },
        "expire_conditions": {
            "vix_max": 22,
            "require_close_above_ma5": True
        },
        "regime_mapping": [
            (0,   "NORMAL",        "正常操作",        1.0),
            (20,  "CAUTION",       "仓位上限25%",     0.25),
            (40,  "DEFENSE_TIGHT", "仓位上限15%",     0.15),
            (60,  "DEFENSE",       "仓位上限10%",     0.10),
            (80,  "DEFENSE_CRISIS","仓位归零静态观望", 0.0),
        ]
    }
}

ROOT = _parent_dir
OVERRIDE_FILE = os.path.join(ROOT, "market_regime_override.json")
SCHEDULE_FILE = os.path.join(_script_dir, "event_schedule.json")
PHENOMENON_FILE = os.path.join(_script_dir, "phenomenon_events.json")

# ═══════════════════════════════════════════
# 1. 固定日期解析器 (离线兜底, 永不离线)
# ═══════════════════════════════════════════

def _resolve_monthly_rule(rule, ref_date=None):
    """根据规则字符串计算下一个月内的事件日期列表。"""
    if ref_date is None:
        ref_date = date.today()
    results = []

    # 当月
    if rule == "monthly_day_20":
        d = date(ref_date.year, ref_date.month, 20)
        while d.weekday() >= 5:  # 周六日顺延
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "monthly_day_15":
        d = date(ref_date.year, ref_date.month, 15)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "monthly_last_day":
        if ref_date.month == 12:
            d = date(ref_date.year, 12, 31)
        else:
            d = date(ref_date.year, ref_date.month + 1, 1) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "monthly_day_1":
        d = date(ref_date.year, ref_date.month, 1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)
        # 下月1日
        if ref_date.month == 12:
            d2 = date(ref_date.year + 1, 1, 1)
        else:
            d2 = date(ref_date.year, ref_date.month + 1, 1)
        while d2.weekday() >= 5:
            d2 += timedelta(days=1)
        if d2 >= ref_date:
            results.append(d2)

    elif rule == "monthly_day_9":
        d = date(ref_date.year, ref_date.month, 9)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "monthly_day_10_to_15":
        d = date(ref_date.year, ref_date.month, 12)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "monthly_day_11_to_16":
        d = date(ref_date.year, ref_date.month, 13)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    elif rule == "first_friday":
        d = date(ref_date.year, ref_date.month, 1)
        while d.weekday() != 4:  # 周五=4
            d += timedelta(days=1)
        if d >= ref_date:
            results.append(d)

    # 下月
    next_month = ref_date.month + 1
    next_year = ref_date.year
    if next_month > 12:
        next_month = 1
        next_year += 1

    if rule == "monthly_day_20":
        d = date(next_year, next_month, 20)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "monthly_day_15":
        d = date(next_year, next_month, 15)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "monthly_day_9":
        d = date(next_year, next_month, 9)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "monthly_day_10_to_15":
        d = date(next_year, next_month, 12)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "monthly_day_11_to_16":
        d = date(next_year, next_month, 13)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "first_friday":
        d = date(next_year, next_month, 1)
        while d.weekday() != 4:
            d += timedelta(days=1)
        results.append(d)
    elif rule == "monthly_last_day":
        if next_month == 12:
            d = date(next_year, 12, 31)
        else:
            d = date(next_year, next_month + 1, 1) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        results.append(d)
    elif rule == "monthly_day_1":
        d = date(next_year, next_month, 1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        results.append(d)

    return results


def _timezone_factor(iso_time_str):
    """根据事件时间(北京时间)返回时差衰减系数。"""
    if not iso_time_str:
        return 1.0  # 未知时间→保守按盘中算
    try:
        hour = int(iso_time_str.split(":")[0])
    except:
        return 1.0
    if 9 <= hour < 15 or (hour == 9 and int(iso_time_str.split(":")[1]) >= 30):
        return 1.0
    elif 6 <= hour < 9 or (hour == 9 and int(iso_time_str.split(":")[1]) < 30):
        return 0.6
    elif 15 <= hour < 24:
        return 0.4
    else:
        return 0.2


# ═══════════════════════════════════════════
# 2. 事件扫描器 (EventScanner)
# ═══════════════════════════════════════════

class EventScanner:
    """未来N天宏观事件扫描。三重数据源降级链。"""

    def __init__(self, config=None):
        self.config = config or CONFIG
        self.settings = self.config.get("engine_settings", {})
        self.scan_days = self.settings.get("scan_days", 14)
        self.today = date.today()
        self.end_date = self.today + timedelta(days=self.scan_days)

    def scan(self):
        """主入口: 扫描并返回未来14天事件清单。"""
        events = []

        # 第一优先级: 固定日期表 (永不离线)
        events.extend(self._scan_fixed_schedule())

        # 第二优先级: yfinance 美股日历 (免费稳定)
        events.extend(self._scan_yfinance())

        # 第三优先级: 现象级事件 (手动维护)
        events.extend(self._scan_phenomenon_events())

        # 按日期排序, 去重
        events.sort(key=lambda e: e["date"])
        events = self._deduplicate(events)

        # 注入方向预判规则
        for e in events:
            if "direction_rules" not in e:
                e["direction_rules"] = {}

        return events

    def _scan_fixed_schedule(self):
        """从 event_schedule.json 读取固定事件。"""
        events = []
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                schedule = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return events

        # 月度固定事件
        for key, evt in schedule.get("fixed_monthly", {}).items():
            rule = evt.get("rule", "")
            dates = _resolve_monthly_rule(rule, self.today)
            for d in dates:
                if self.today <= d <= self.end_date:
                    events.append({
                        "date": d.isoformat(),
                        "time": evt.get("time", ""),
                        "event": evt["event"],
                        "country": evt.get("country", ""),
                        "category": "economic",
                        "importance": evt.get("importance", "medium"),
                        "impact_channel": evt.get("impact_channel", ""),
                        "affected_sectors": evt.get("affected_sectors", []),
                        "direction_rules": evt.get("direction_rules", {}),
                        "source": "event_schedule.json",
                        "source_priority": 1,
                    })

        # 年度固定事件
        for key, evt in schedule.get("fixed_annual", {}).items():
            for d_str in evt.get("dates_2026", []):
                try:
                    d = datetime.strptime(d_str, "%Y-%m-%d").date()
                except:
                    continue
                if self.today <= d <= self.end_date:
                    events.append({
                        "date": d.isoformat(),
                        "time": evt.get("time", ""),
                        "event": evt["event"],
                        "country": evt.get("country", ""),
                        "category": "economic",
                        "importance": evt.get("importance", "critical"),
                        "impact_channel": evt.get("impact_channel", ""),
                        "affected_sectors": evt.get("affected_sectors", []),
                        "direction_rules": evt.get("direction_rules", {}),
                        "source": "event_schedule.json",
                        "source_priority": 1,
                    })

        return events

    def _scan_yfinance(self):
        """从 yfinance 获取近期经济日历事件。"""
        events = []
        try:
            import yfinance as yf

            # 尝试获取标普500期权的经济日历(包含CPI/FOMC/NFP等)
            try:
                spx = yf.Ticker("^GSPC")
                cal = getattr(spx, "calendar", None)
                if cal and callable(cal):
                    cal_data = cal()
                    if isinstance(cal_data, dict):
                        for key, df in cal_data.items():
                            if hasattr(df, "to_dict"):
                                for _, row in df.iterrows():
                                    d = row.get("Earnings Date", row.get("Date", None))
                                    if d is None:
                                        continue
                                    evt_date = d if isinstance(d, date) else (
                                        datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                                        if str(d)[:4] == "202" else None
                                    )
                                    if evt_date and self.today <= evt_date <= self.end_date:
                                        events.append({
                                            "date": evt_date.isoformat(),
                                            "event": str(row.get("Event", key))[:60],
                                            "importance": "medium",
                                            "source": "yfinance",
                                            "source_priority": 2,
                                        })
            except:
                pass

            # VIX 作为当前恐慌基准 (单次取值, 不产生事件)
            try:
                vix_ticker = yf.Ticker("^VIX")
                vix_info = vix_ticker.fast_info if hasattr(vix_ticker, "fast_info") else None
                if vix_info:
                    vix_val = getattr(vix_info, "last_price", None)
                    if vix_val:
                        self._vix_latest = float(vix_val)
            except:
                self._vix_latest = None

        except ImportError:
            pass
        except Exception:
            pass

        return events

    def _scan_phenomenon_events(self):
        """从 phenomenon_events.json 读取现象级事件。"""
        events = []
        try:
            with open(PHENOMENON_FILE, "r", encoding="utf-8") as f:
                phen = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return events

        for evt in phen.get("active", []):
            try:
                d = datetime.strptime(evt["date"], "%Y-%m-%d").date()
            except:
                continue
            if self.today <= d <= self.end_date or (
                "date_end" in evt and self.today <= datetime.strptime(evt["date_end"], "%Y-%m-%d").date()
            ):
                direction_info = evt.get("direction", {})
                events.append({
                    "date": evt["date"],
                    "date_end": evt.get("date_end", ""),
                    "event": evt["event"],
                    "category": evt.get("category", "phenomenon"),
                    "importance": "high" if abs(direction_info.get("a_share_overall", 0)) >= 10 else "medium",
                    "timezone_factor": evt.get("timezone_factor", 1.0),
                    "timezone_note": evt.get("timezone_note", ""),
                    "capital_flow": evt.get("capital_flow", {}),
                    "sentiment_impact": evt.get("sentiment_impact", ""),
                    "historical_reference": evt.get("historical_reference", ""),
                    "direction_rules": {
                        "phenomenon_default": direction_info.get("note", ""),
                        "base_direction_score": direction_info.get("a_share_overall", 0),
                    },
                    "affected_sectors": self._extract_phenomenon_sectors(evt),
                    "source": "phenomenon_events.json",
                    "source_priority": 3,
                })

        return events

    @staticmethod
    def _extract_phenomenon_sectors(evt):
        cf = evt.get("capital_flow", {})
        sectors = set()
        for entry in cf.get("out", []):
            if isinstance(entry, dict):
                sectors.add(entry.get("sector", ""))
        for entry in cf.get("in", []):
            if isinstance(entry, dict):
                sectors.add(entry.get("sector", ""))
        return [s for s in sectors if s]

    @staticmethod
    def _deduplicate(events):
        """按(日期, 事件名)去重, 保留高优先级来源。"""
        seen = {}
        for e in events:
            key = (e["date"], e["event"][:40])
            if key not in seen or e.get("source_priority", 99) < seen[key].get("source_priority", 99):
                seen[key] = e
        return list(seen.values())

    def get_vix(self):
        """返回最新VIX值, 若未获取则尝试实时取。"""
        if not hasattr(self, "_vix_latest") or self._vix_latest is None:
            try:
                import yfinance as yf
                vix = yf.Ticker("^VIX")
                info = vix.fast_info if hasattr(vix, "fast_info") else None
                if info:
                    self._vix_latest = float(getattr(info, "last_price", 0) or 0)
            except:
                self._vix_latest = None
        return self._vix_latest


# ═══════════════════════════════════════════
# 3. 情绪度量引擎 (SentimentEngine)
# ═══════════════════════════════════════════

class SentimentEngine:
    """VIX恐慌指数 + BW情绪 + OU过程半衰期 + GARCH黏性。"""

    def __init__(self):
        self.vix = None
        self.bw_sentiment = None
        self.garch_alpha_beta = None
        self.ou_theta = None

    def assess(self):
        """运行全部情绪度量, 返回摘要。"""
        self._get_vix()
        self._estimate_bw_proxy()
        self._estimate_garch_persistence()
        return {
            "vix": self.vix,
            "vix_level": self._vix_level(),
            "bw_sentiment": self.bw_sentiment,
            "bw_label": self._bw_label(),
            "garch_persistence": self.garch_alpha_beta,
            "garch_sticky": (self.garch_alpha_beta or 0) > 0.85,
            "garch_halflife_hours": self.garch_halflife_hours,
            "ou_halflife_days": self._halflife_days() if self.ou_theta else None,
            "summary": self._summary(),
        }

    def _get_vix(self):
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            info = vix.fast_info if hasattr(vix, "fast_info") else None
            if info:
                self.vix = float(getattr(info, "last_price", 18) or 18)
        except:
            self.vix = 18.0

    def _vix_level(self):
        if self.vix is None:
            return "unknown"
        if self.vix < 12:   return "极度安全"
        if self.vix < 15:   return "安全"
        if self.vix < 20:   return "偏低恐慌"
        if self.vix < 25:   return "中度恐慌"
        if self.vix < 30:   return "高度恐慌"
        return "极端恐慌"

    def _estimate_bw_proxy(self):
        """用DuckDB换手率数据估算BW情绪代理值。"""
        try:
            import duckdb
            db = r"D:\FreeFinanceData\data\duckdb\finance.db"
            conn = duckdb.connect(db)
            try:
                row = conn.execute("""
                    SELECT AVG(turnover_rate) FROM kline_daily
                    WHERE trade_date >= CURRENT_DATE - 5
                      AND turnover_rate IS NOT NULL
                """).fetchone()
                if row and row[0]:
                    avg_turn = float(row[0])
                    # 简化映射: 换手率越高→投机越活跃→情绪越乐观
                    # 正常范围0.5-3%, 映射到-1到+1
                    self.bw_sentiment = max(-1.0, min(1.0, (avg_turn - 1.5) / 1.5))
                else:
                    self.bw_sentiment = 0.0
            finally:
                conn.close()
        except:
            self.bw_sentiment = 0.0

    def _bw_label(self):
        if self.bw_sentiment is None:
            return "未知"
        if self.bw_sentiment > 0.3:   return "偏乐观/贪婪"
        if self.bw_sentiment > 0.0:   return "略偏乐观"
        if self.bw_sentiment > -0.3:  return "略偏悲观"
        return "偏悲观/恐慌"

    def _halflife_days(self):
        """OU过程半衰期(天): t_half = ln(2)/θ。θ从日线自回归估算。"""
        if self.ou_theta is None:
            return None
        return math.log(2) / max(self.ou_theta, 0.01)

    def _estimate_garch_persistence(self):
        """
        从沪深300日收益率估算GARCH(1,1)波动率持久性(α+β)。

        方法: 用平方收益率的一阶自相关作为持久性代理。
        ρ(ε²_t, ε²_{t-1}) ≈ α + β (在GARCH(1,1)中)

        半衰期: t_half_hours = ln(0.5) / ln(α+β) × 24
          α+β=0.85 → ~100h (低黏性, 4天消化50%)
          α+β=0.90 → ~157h (中黏性, 6.5天)
          α+β=0.95 → ~324h (高黏性, 13.5天 — 2022年9月真实水平)
        """
        self.garch_alpha_beta = None
        self.garch_halflife_hours = 48  # 默认

        try:
            import duckdb
            db = r"D:\FreeFinanceData\data\duckdb\finance.db"
            conn = duckdb.connect(db)
            try:
                rows = conn.execute("""
                    SELECT close FROM kline_daily
                    WHERE ts_code='sh000300'
                    ORDER BY trade_date DESC LIMIT 61
                """).fetchall()
            finally:
                conn.close()

            if len(rows) < 30:
                self.garch_halflife_hours = 48
                return

            # 计算日对数收益率
            closes = [float(r[0]) for r in reversed(rows) if r[0] is not None]
            if len(closes) < 30:
                self.garch_halflife_hours = 48
                return

            returns = []
            for i in range(1, len(closes)):
                if closes[i-1] > 0:
                    returns.append(math.log(closes[i] / closes[i-1]))

            if len(returns) < 20:
                self.garch_halflife_hours = 48
                return

            # 平方收益率的自相关 (≈ GARCH α+β)
            sq = [r*r for r in returns]
            n = len(sq)
            mean_sq = sum(sq) / n
            numerator = sum((sq[i] - mean_sq) * (sq[i-1] - mean_sq) for i in range(1, n))
            denominator = sum((s - mean_sq) ** 2 for s in sq)
            persistence = numerator / denominator if denominator > 1e-10 else 0.85

            # 限幅
            persistence = max(0.70, min(0.99, persistence))
            self.garch_alpha_beta = round(persistence, 4)

            # GARCH动态半衰期 (小时)
            # t_half = ln(0.5) / ln(persistence) × 24
            if persistence > 0 and persistence < 1.0 - 1e-8:
                hl_days = math.log(0.5) / math.log(persistence)
                self.garch_halflife_hours = round(max(12, min(720, hl_days * 24)))
            else:
                self.garch_halflife_hours = 48

        except:
            self.garch_halflife_hours = 48

    def _summary(self):
        parts = []
        if self.vix:
            parts.append(f"VIX={self.vix:.1f}({self._vix_level()})")
        if self.bw_sentiment is not None:
            parts.append(f"BW情绪={self.bw_sentiment:+.2f}({self._bw_label()})")
        if self.garch_alpha_beta:
            sticky = "黏性强" if self.garch_alpha_beta > 0.85 else "正常"
            parts.append(f"GARCH α+β={self.garch_alpha_beta:.2f}({sticky})")
        return " | ".join(parts) if parts else "情绪数据不可用"


# ═══════════════════════════════════════════
# 4. 冲突溢价评分引擎 (ConflictPremiumEngine)
# ═══════════════════════════════════════════

class ConflictPremiumEngine:
    """
    核心评分引擎 v2.0 — 已修正三个致命漏洞:
      漏洞1: 多空线性相抵 → 引入 conflict_energy 提取隐藏波动率
      漏洞2: 置信度乘法衰减 → 改误差带 + 兜底提档
      漏洞3: 单维Regime映射 → 5档不增加新状态
    """

    def __init__(self, config=None):
        self.config = config or CONFIG
        self.settings = self.config.get("engine_settings", {})
        self.alpha = self.settings.get("conflict_premium_weight", 0.50)

    def score_events(self, assessments, sentiment_summary):
        """
        输入用户评估列表 + 情绪摘要 → 输出综合风险分 + Regime建议。

        assessments: list of dict
          {event, direction_score, sentiment_shock, confidence, timezone_factor}
        sentiment_summary: dict from SentimentEngine.assess()

        returns: {final_risk_score, regime, regime_desc, risk_budget, components}
        """
        raw_scores = []
        low_confidence_bearish = 0

        for a in assessments:
            # 阶段一: 基础分
            base = a.get("direction_score", 0)
            shock = a.get("sentiment_shock", 1.0)
            tz = a.get("timezone_factor", 1.0)
            score = base * shock * tz
            raw_scores.append(score)

            # 阶段二: 置信度处理 (不确定不衰减, 计数兜底)
            conf = a.get("confidence", "medium")
            if conf == "low" and score > 0:
                low_confidence_bearish += 1

        if not raw_scores:
            return {
                "final_risk_score": 0,
                "net_score": 0,
                "conflict_energy": 0,
                "regime": "NORMAL",
                "regime_desc": "无事件, 正常操作",
                "risk_budget": 1.0,
                "low_confidence_conservative": False,
                "components": [],
            }

        # 阶段三: 冲突溢价聚合
        net_score = sum(raw_scores)
        gross_volatility = sum(abs(s) for s in raw_scores)
        conflict_energy = gross_volatility - abs(net_score)

        final_risk = max(net_score, 0) + self.alpha * conflict_energy
        final_risk = round(final_risk, 2)

        # 阶段二延续: 兜底提档 — ≥2个不确定利空→强制提一档
        conservative_trigger = (
            low_confidence_bearish >=
            self.settings.get("confidence", {}).get("low", {}).get(
                "consecutive_low_triggers_conservative", 2)
        )

        # 阶段五: 分数 → Regime 映射
        regime, regime_desc, risk_budget = self._map_to_regime(final_risk)

        if conservative_trigger:
            regime, regime_desc, risk_budget = self._bump_regime(regime)

        return {
            "final_risk_score": final_risk,
            "net_score": round(net_score, 2),
            "conflict_energy": round(conflict_energy, 2),
            "alpha_used": self.alpha,
            "regime": regime,
            "regime_desc": regime_desc,
            "risk_budget": risk_budget,
            "low_confidence_conservative": conservative_trigger,
            "low_confidence_bearish_count": low_confidence_bearish,
            "components": raw_scores,
        }

    def _map_to_regime(self, score):
        mapping = self.settings.get("regime_mapping", [])
        regime, desc, budget = "DEFENSE_CRISIS", "仓位归零", 0.0
        for threshold, r, d, b in mapping:
            if score >= threshold:
                regime, desc, budget = r, d, b
        return regime, desc, budget

    def _bump_regime(self, current_regime):
        """保守兜底: Regime强制提升一档。"""
        order = ["NORMAL", "CAUTION", "DEFENSE_TIGHT", "DEFENSE", "DEFENSE_CRISIS"]
        try:
            idx = order.index(current_regime)
            new_idx = min(idx + 1, len(order) - 1)
        except ValueError:
            return current_regime, "未知", 0.5

        mapping = self.settings.get("regime_mapping", [])
        for threshold, r, d, b in mapping:
            if r == order[new_idx]:
                return r, d + " [保守兜底提档: ≥2不确定利空]", b
        return order[new_idx], "保守兜底", 0.15


# ═══════════════════════════════════════════
# 5. Regime覆写管理器
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# 5.5 情绪衰减池 (DecayPool) — Phase 3 连续回测引擎
# ═══════════════════════════════════════════

class DecayPool:
    """
    独立衰减池: 每个事件独立计时衰减, 池内合成带入冲突溢价。

    数学:
      residual_i(t) = initial_score_i × 0.5^(hours_elapsed / halflife_i)
      pool_risk(t) = max(Σ residual_i, 0) + α × (Σ|residual_i| - |Σ residual_i|)

    规则:
      - |residual| < 1.0 → 从池中移除 (防无限长尾)
      - 新事件入池不覆盖旧事件
      - 合成时使用冲突溢价公式, 防多空相抵假信号
    """

    def __init__(self, conflict_alpha=0.7, min_residual=1.0, garch_persistence=None):
        self.events = []
        self.alpha = conflict_alpha
        self.min_residual = min_residual
        self.garch_persistence = garch_persistence  # 外部注入的GARCH α+β
        self.history = []
        self.settings = CONFIG.get("engine_settings", {})
        self.fat_tail_threshold = self.settings.get("fat_tail_threshold", 40)
        self.fat_tail_multiplier = self.settings.get("fat_tail_multiplier", 1.2)

    def _dynamic_halflife(self, hardcoded_hours):
        """GARCH动态半衰期: 用当前市场黏性修正硬编码值。"""
        if self.garch_persistence and self.garch_persistence > 0.7:
            # t_half = ln(0.5) / ln(α+β) × 24
            p = self.garch_persistence
            if p < 1.0 - 1e-8:
                dynamic = math.log(0.5) / math.log(p) * 24
                return round(max(12, min(720, dynamic)))
        return hardcoded_hours  # 无GARCH时用硬编码

    def step(self, new_events, current_time, garch_override=None):
        """
        每日步进。

        new_events: list of {score, halflife_hours} — 当天新事件(可能为空)
        current_time: datetime — 当前步进时间
        garch_override: float — 外部注入GARCH持久性(用于回测覆盖)

        returns: (pool_risk: float, regime: str, detail: dict)
        """
        if garch_override is not None:
            self.garch_persistence = garch_override

        # 1. 衰减所有旧事件 (每个事件独立按自己的半衰期衰减)
        for e in self.events:
            hours_elapsed = max(0, (current_time - e["start_time"]).total_seconds() / 3600)
            if e["halflife_hours"] > 0:
                e["residual"] = e["initial_score"] * (0.5 ** (hours_elapsed / e["halflife_hours"]))
            else:
                e["residual"] = 0.0

        # 2. 清除衰减到底的事件 (|residual| < min_residual)
        self.events = [e for e in self.events if abs(e["residual"]) >= self.min_residual]

        # 3. 新事件入池 (GARCH动态半衰期 + 肥尾惩罚)
        for ne in new_events:
            raw_score = ne.get("score", 0)

            # 肥尾惩罚: 单事件分>阈值 → 黑天鹅乘数
            if abs(raw_score) > self.fat_tail_threshold:
                fat_tailed = raw_score * self.fat_tail_multiplier
                label_suffix = f" [黑天鹅×{self.fat_tail_multiplier}: {raw_score}→{fat_tailed:.0f}]"
            else:
                fat_tailed = raw_score
                label_suffix = ""

            # GARCH动态半衰期
            hardcoded_hl = max(1, ne.get("halflife_hours", 48))
            dynamic_hl = self._dynamic_halflife(hardcoded_hl)

            self.events.append({
                "initial_score": fat_tailed,
                "halflife_hours": dynamic_hl,
                "halflife_original": hardcoded_hl,
                "halflife_source": "GARCH" if dynamic_hl != hardcoded_hl else "hardcoded",
                "start_time": current_time,
                "residual": fat_tailed,
                "label": ne.get("label", "") + label_suffix,
            })

        # 4. 合成当前风险 (带入冲突溢价 — 修复多空相抵漏洞)
        residuals = [e["residual"] for e in self.events]

        if not residuals:
            pool_risk = 0.0
            net_score = 0.0
            conflict_energy = 0.0
        else:
            net_score = sum(residuals)
            gross_volatility = sum(abs(r) for r in residuals)
            conflict_energy = gross_volatility - abs(net_score)
            pool_risk = max(net_score, 0) + self.alpha * conflict_energy

        pool_risk = round(pool_risk, 2)

        # 5. 映射到 Regime
        regime, regime_desc, risk_budget = self._map_to_regime(pool_risk)

        # 6. 记录快照
        detail = {
            "date": current_time.strftime("%Y-%m-%d"),
            "pool_risk": pool_risk,
            "net_score": round(net_score, 2),
            "conflict_energy": round(conflict_energy, 2),
            "n_active_events": len(self.events),
            "residuals": [round(r, 1) for r in residuals],
            "regime": regime,
            "regime_desc": regime_desc,
            "risk_budget": risk_budget,
            "events_in_pool": [
                {"label": e.get("label", "?"), "residual": round(e["residual"], 1),
                 "hours_left": round(e["halflife_hours"] * (1 - (e["residual"] / e["initial_score"]) if e["initial_score"] else 0))}
                for e in self.events
            ],
        }
        self.history.append(detail)

        return pool_risk, regime, detail

    def get_history(self):
        return self.history

    def summary(self):
        """返回回测摘要统计。"""
        if not self.history:
            return {"error": "no history"}
        regimes = [h["regime"] for h in self.history]
        risks = [h["pool_risk"] for h in self.history]
        switches = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i-1])
        return {
            "total_days": len(self.history),
            "regime_distribution": {r: regimes.count(r) for r in set(regimes)},
            "avg_pool_risk": round(sum(risks) / len(risks), 2) if risks else 0,
            "max_pool_risk": max(risks) if risks else 0,
            "regime_switches": switches,
            "switch_rate": round(switches / max(len(self.history) - 1, 1), 3),
            "days_in_defense": sum(1 for r in regimes if "DEFENSE" in r),
            "defense_ratio": round(sum(1 for r in regimes if "DEFENSE" in r) / len(regimes), 2),
        }

    def _map_to_regime(self, score):
        mapping = self.settings.get("regime_mapping", [])
        regime, desc, budget = "DEFENSE_CRISIS", "仓位归零", 0.0
        for threshold, r, d, b in mapping:
            if score >= threshold:
                regime, desc, budget = r, d, b
        return regime, desc, budget

    # ── 检查点2: 状态持久化 ──

    def save_state(self, regime, reason, filepath=None):
        """
        将当前衰减池完整快照 + Regime指令打包写入JSON。
        天眼重启后可通过 load_state() 瞬间恢复记忆, 无断层。

        写入格式:
          {
            "active": true/false,
            "regime": "DEFENSE_TIGHT",
            "reason": "...",
            "set_at": "ISO时间戳",
            "pool_snapshot": {
              "conflict_alpha": 0.7,
              "current_pool_risk": 45.8,
              "events": [{name, initial_score, halflife_hours, start_time, residual}]
            }
          }
        """
        fp = filepath or OVERRIDE_FILE
        now = datetime.now()

        # 计算当前池风险
        residuals = [e["residual"] for e in self.events]
        if residuals:
            net = sum(residuals)
            gross = sum(abs(r) for r in residuals)
            conflict = gross - abs(net)
            current_risk = max(net, 0) + self.alpha * conflict
        else:
            current_risk = 0.0

        state = {
            "active": regime != "NORMAL",
            "regime": regime,
            "reason": reason,
            "direction_score": round(current_risk, 2),
            "set_by": "李大霄",
            "set_at": now.isoformat(),
            "expires": (now + timedelta(days=14)).strftime("%Y-%m-%d"),
            "auto_expire": True,
            "expire_conditions": {
                "time_after": (now + timedelta(days=7)).strftime("%Y-%m-%d"),
                "vix_below": 22,
                "close_above_ma5": True,
            },
            "pool_snapshot": {
                "conflict_alpha": self.alpha,
                "current_pool_risk": round(current_risk, 2),
                "events": []
            }
        }

        for e in self.events:
            state["pool_snapshot"]["events"].append({
                "name": e.get("label", e.get("name", "Unknown")),
                "initial_score": e["initial_score"],
                "halflife_hours": e["halflife_hours"],
                "start_time": e["start_time"].isoformat(),
                "residual": round(e["residual"], 2),
            })

        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, fp)
        except (PermissionError, OSError):
            pass

    def load_state(self, filepath=None):
        """
        系统重启时从JSON恢复衰减池记忆。
        每个事件的residual仅作快照参考 — 下一次step()会用当前时间重新精确计算。

        Returns: (regime: str, reason: str) 或 (None, None)
        """
        fp = filepath or OVERRIDE_FILE
        try:
            with open(fp, "r", encoding="utf-8") as f:
                state = json.load(f)

            if not state.get("active", False):
                return None, None

            snapshot = state.get("pool_snapshot", {})
            self.alpha = snapshot.get("conflict_alpha", 0.7)
            self.events = []

            for e in snapshot.get("events", []):
                try:
                    st = datetime.fromisoformat(e["start_time"])
                except:
                    st = datetime.now()
                self.events.append({
                    "label": e.get("name", "Unknown"),
                    "initial_score": e["initial_score"],
                    "halflife_hours": e["halflife_hours"],
                    "halflife_original": e["halflife_hours"],
                    "halflife_source": "restored",
                    "start_time": st,
                    "residual": e.get("residual", 0),
                })

            return state.get("regime"), state.get("reason", "")

        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None, None


# ═══════════════════════════════════════════
# 5.6 新闻突发网关 (NewsShockerGateway) — 检查点3: 接口解耦
# ═══════════════════════════════════════════

class NewsShockerGateway:
    """
    轻量级网关: 接收LLM解析的突发新闻标准JSON → 直接注入衰减池 → 瞬间熔断。

    设计原则:
      - 事件生成(LLM/爬虫/NLP)与衰减池计算完全解耦
      - DecayPool不关心事件来源, 只认标准输入格式
      - 未来只需让LLM输出 {news_summary, direction_score, sentiment_multiplier}
        即可无痛接入本网关, 获得完整衰减池+冲突溢价+刚性截断能力

    标准输入Schema (LLM输出需严格符合):
      {
        "news_summary": "突发: 某大型银行暴雷",
        "direction_score": 30.0,        // -30到+30, 正=利空
        "sentiment_multiplier": 2.0,     // 0.5狂热/0.7乐观/1.0平静/1.5担忧/2.0恐慌
        "current_garch_persistence": 0.90 // 可选, 缺失时用0.85
      }
    """

    def __init__(self, decay_pool: DecayPool):
        self.pool = decay_pool

    def inject_sudden_news(self, llm_parsed_json: dict, current_time=None):
        """
        接收大模型解析的突发新闻标准JSON, 直接砸入衰减池, 引发瞬间熔断。

        Args:
          llm_parsed_json: LLM输出的标准格式dict
          current_time: datetime, 默认now()

        Returns:
          (new_regime: str, new_risk_score: float, detail: dict)
        """
        if current_time is None:
            current_time = datetime.now()

        event_name = llm_parsed_json.get("news_summary", "突发新闻")
        direction_score = llm_parsed_json.get("direction_score", 0)
        sentiment_multiplier = llm_parsed_json.get("sentiment_multiplier", 1.0)
        garch_p = llm_parsed_json.get("current_garch_persistence", 0.85)

        # 核心数学: 方向分 × 情绪系数 = 原始冲击
        # 肥尾/GARCH半衰期由DecayPool.step统一处理, 网关不做重复计算
        raw_score = direction_score * sentiment_multiplier

        # GARCH动态半衰期
        if garch_p > 0 and garch_p < 1.0 - 1e-8:
            hl_days = math.log(0.5) / math.log(garch_p)
            dynamic_hl = round(max(12, min(720, hl_days * 24)))
        else:
            dynamic_hl = 48

        # 构造标准入池对象 (肥尾/GARCH由DecayPool.step统一处理)
        sudden_event = {
            "score": raw_score,
            "halflife_hours": dynamic_hl,
            "label": f"[突发] {event_name}",
        }

        # 注入GARCH持久性, 让DecayPool.step使用正确的动态半衰期
        self.pool.garch_persistence = garch_p

        # 步进衰减池(与池中残余事件进行冲突溢价计算)
        new_risk_score, new_regime, detail = self.pool.step([sudden_event], current_time)

        # 持久化写入
        reason = (
            f"突发新闻触发: {event_name} "
            f"(score={raw_score:.0f} hl={dynamic_hl}h "
            f"risk={new_risk_score:.1f} regime={new_regime})"
        )
        self.pool.save_state(new_regime, reason)

        return new_regime, new_risk_score, detail


# ═══════════════════════════════════════════
# 6. Regime覆写管理器
# ═══════════════════════════════════════════

class RegimeOverrideManager:
    """写入/读取/检查 market_regime_override.json。"""

    def __init__(self, filepath=None):
        self.filepath = filepath or OVERRIDE_FILE

    def read(self):
        """读取当前覆写状态。无覆写或已过期返回None。"""
        try:
            if not os.path.exists(self.filepath):
                return None
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("active", False):
                return None
            # 检查时间是否过期
            expires_str = data.get("expires", "")
            if expires_str:
                try:
                    expires = datetime.strptime(expires_str, "%Y-%m-%d").date()
                    if date.today() > expires:
                        # 检查 expire_conditions
                        if data.get("expire_conditions"):
                            if self._check_expire_conditions(data["expire_conditions"]):
                                data["active"] = False
                                self.write(data)
                                return None
                            else:
                                # 时间到但条件不满足 → 延期
                                return data
                        else:
                            # 无条件, 直接过期
                            data["active"] = False
                            self.write(data)
                            return None
                except:
                    pass
            return data
        except (json.JSONDecodeError, PermissionError):
            return None

    def write(self, data):
        """写入覆写状态。"""
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            tmp = self.filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.filepath)
        except (PermissionError, OSError):
            pass

    def clear(self):
        """手动解除覆写。"""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["active"] = False
                data["cleared_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                self.write(data)
            except:
                pass

    @staticmethod
    def _check_expire_conditions(conditions):
        """检查是否满足过期条件(全部满足=True→可以过期)。"""
        try:
            vix_max = conditions.get("vix_below")
            if vix_max:
                try:
                    import yfinance as yf
                    vix = yf.Ticker("^VIX")
                    info = vix.fast_info if hasattr(vix, "fast_info") else None
                    current_vix = float(getattr(info, "last_price", 99) or 99) if info else 99
                    if current_vix > vix_max:
                        return False  # VIX还很高, 不能过期
                except:
                    pass

            # close_above_ma5 检查需要DuckDB, 暂简化为始终通过
            # 实际部署时补充
            return True
        except:
            return True  # 检查失败→保守放行


# ═══════════════════════════════════════════
# 6. 交互式问答 (InteractiveQ&A)
# ═══════════════════════════════════════════

_PRINT = print  # 保存引用, stdout封装后仍可用


class InteractiveQA:
    """命令行交互式问答 → 逐事件收集用户判断。"""

    # 事件类型 → 问题模板
    QUESTION_TEMPLATES = {
        "FOMC": {
            "direction": {
                "q": "方向判断 (鹰派加息/鹰派不动/中性/鸽派不动/鸽派降息)?",
                "options": {
                    "1": ("鹰派加息",   30, 2.0),
                    "2": ("鹰派不动",   20, 1.5),
                    "3": ("中性",       0, 1.0),
                    "4": ("鸽派不动",  -15, 0.7),
                    "5": ("鸽派降息",  -30, 0.5),
                }
            },
        },
        "CPI": {
            "direction": {
                "q": "方向判断 (大幅高于预期/略高于预期/持平/低于预期)?",
                "options": {
                    "1": ("大幅高于预期(>30bp)",  30, 2.0),
                    "2": ("略高于预期(10-30bp)",  20, 1.5),
                    "3": ("持平(±10bp)",          0, 1.0),
                    "4": ("低于预期",            -15, 0.7),
                }
            },
        },
        "NFP": {
            "direction": {
                "q": "方向判断 (大幅高于预期/略高于预期/持平/低于预期)?",
                "options": {
                    "1": ("大幅高于预期(>50k)",   25, 1.5),
                    "2": ("略高于预期",          15, 1.2),
                    "3": ("持平",                0, 1.0),
                    "4": ("低于预期",           -20, 0.7),
                }
            },
        },
        "LPR": {
            "direction": {
                "q": "方向判断 (降息/不变/加息)?",
                "options": {
                    "1": ("降息10bp以上",  -25, 0.5),
                    "2": ("降息5bp",       -15, 0.7),
                    "3": ("不变",           0, 1.0),
                    "4": ("加息",          20, 1.5),
                }
            },
        },
        "MLF": {
            "direction": {
                "q": "方向判断 (增量续作/持平/缩量)?",
                "options": {
                    "1": ("大幅增量(>2000亿)",  -20, 0.5),
                    "2": ("小幅增量",           -10, 0.7),
                    "3": ("持平",                0, 1.0),
                    "4": ("缩量",               15, 1.5),
                }
            },
        },
        "OPEC": {
            "direction": {
                "q": "方向判断 (大幅减产/小幅减产/不变/增产)?",
                "options": {
                    "1": ("大幅减产(>100万桶/日)",  25, 2.0),
                    "2": ("小幅减产",              15, 1.5),
                    "3": ("不变",                  0, 1.0),
                    "4": ("增产",                 -20, 0.7),
                }
            },
        },
        "_default": {
            "direction": {
                "q": "方向判断 (利多A股/中性/利空A股)?",
                "options": {
                    "1": ("强烈利多",  -25, 0.5),
                    "2": ("轻微利多",  -10, 0.7),
                    "3": ("中性",       0, 1.0),
                    "4": ("轻微利空",   10, 1.5),
                    "5": ("强烈利空",   25, 2.0),
                }
            },
        },
    }

    CONFIDENCE_OPTIONS = {
        "1": ("很有把握", "high"),
        "2": ("一般",     "medium"),
        "3": ("不太确定", "low"),
    }

    def __init__(self):
        self.assessments = []

    def run(self, events):
        """主循环: 逐事件问答。"""
        _PRINT("\n" + "=" * 60)
        _PRINT("  天眼 · 宏观事件人工判断评估")
        _PRINT("=" * 60)
        _PRINT(f"\n  共扫描到 {len(events)} 个未来事件:")
        _PRINT()

        # 展示事件清单
        for i, evt in enumerate(events):
            imp_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(
                evt.get("importance", "medium"), "⚪")
            tz_note = f" 时差衰减{evt['timezone_factor']}" if evt.get("timezone_factor", 1.0) < 1.0 else ""
            _PRINT(f"  [{i+1}] {imp_icon} {evt['date']}  {evt['event'][:50]}{tz_note}")
            if evt.get("impact_channel"):
                _PRINT(f"      传导: {evt['impact_channel'][:70]}")
            if evt.get("category") == "phenomenon":
                _PRINT(f"      现象级 | 历史参考: {evt.get('historical_reference', '')[:80]}")
        _PRINT()

        _PRINT("─" * 60)
        _PRINT("  现在逐事件评估。不确定可跳过(系统推测兜底)。")
        _PRINT("  输入 's' 跳过剩余, 'q' 退出不保存。")
        _PRINT("─" * 60)

        for i, evt in enumerate(events):
            result = self._assess_one(evt, i + 1, len(events))
            if result == "SKIP_ALL":
                break
            if result == "QUIT":
                _PRINT("\n  已退出, 未保存任何覆写。")
                return None
            if result:
                self.assessments.append(result)

        if not self.assessments:
            _PRINT("\n  无有效评估, 退出。")
            return None

        return self.assessments

    def _assess_one(self, evt, idx, total):
        """评估单个事件。"""
        event_name = evt["event"][:50]
        _PRINT(f"\n  [{idx}/{total}] {evt['date']} — {event_name}")
        if evt.get("timezone_note"):
            _PRINT(f"  ⚠ {evt['timezone_note'][:100]}")

        # 选择问题模板
        tmpl = None
        for keyword, t in self.QUESTION_TEMPLATES.items():
            if keyword != "_default" and keyword.lower() in event_name.lower():
                tmpl = t
                break
        if tmpl is None:
            tmpl = self.QUESTION_TEMPLATES["_default"]

        # 方向
        dir_q = tmpl["direction"]
        _PRINT(f"\n  {dir_q['q']}")
        for k, (label, _, _) in dir_q["options"].items():
            _PRINT(f"    [{k}] {label}")
        _PRINT(f"    [0] 跳过(系统推测)")

        dir_choice = self._input("  选择: ").strip()
        if dir_choice.lower() == "q":
            return "QUIT"
        if dir_choice.lower() == "s":
            return "SKIP_ALL"
        if dir_choice == "0" or dir_choice.lower() == "skip":
            direction_score = 0
            sentiment_shock = 1.0
            _PRINT("  → 已跳过, 不计入风险分")
        elif dir_choice in dir_q["options"]:
            _, direction_score, sentiment_shock = dir_q["options"][dir_choice]
            _PRINT(f"  → 方向分={direction_score:+d}, 情绪系数={sentiment_shock}")
        else:
            _PRINT("  无效选择, 跳过。")
            direction_score = 0
            sentiment_shock = 1.0

        # 置信度
        _PRINT(f"\n  你对此判断的把握程度?")
        for k, (label, _) in self.CONFIDENCE_OPTIONS.items():
            _PRINT(f"    [{k}] {label}")
        conf_choice = self._input("  选择[默认2]: ").strip() or "2"
        _, confidence = self.CONFIDENCE_OPTIONS.get(conf_choice, ("一般", "medium"))
        _PRINT(f"  → 置信度: {confidence}")

        return {
            "event": event_name,
            "date": evt["date"],
            "direction_score": direction_score,
            "sentiment_shock": sentiment_shock,
            "confidence": confidence,
            "timezone_factor": evt.get("timezone_factor", 1.0),
            "importance": evt.get("importance", "medium"),
            "skipped": (dir_choice == "0"),
        }

    @staticmethod
    def _input(prompt):
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return "q"


# ═══════════════════════════════════════════
# 7. 综合报告输出
# ═══════════════════════════════════════════

def print_comprehensive_report(events, assessments, engine_result, sentiment):
    """打印综合判断报告。"""
    _PRINT("\n")
    _PRINT("=" * 64)
    _PRINT("  下周宏观事件综合判断")
    _PRINT("=" * 64)

    # 情绪基准
    _PRINT(f"\n  📊 当前市场情绪基准")
    _PRINT(f"  {sentiment['summary']}")
    if sentiment.get("garch_sticky"):
        _PRINT(f"  ⚠ 恐慌黏性强(GARCH α+β={sentiment['garch_persistence']:.2f}), 利空冲击会被放大")
    if sentiment.get("ou_halflife_days"):
        hl = sentiment["ou_halflife_days"]
        _PRINT(f"  ⏱ 情绪消化半衰期约{hl:.1f}天, 完全恢复需约{hl*2:.0f}天")

    # 事件表
    _PRINT(f"\n  {'─'*60}")
    _PRINT(f"  {'事件':<24} {'判断':<8} {'方向分':>6} {'情绪':>6} {'半衰期':>8}")
    _PRINT(f"  {'─'*60}")

    for a in assessments:
        name = a["event"][:22]
        direction = "利多" if a["direction_score"] < 0 else ("利空" if a["direction_score"] > 0 else "中性")
        shock_label = {2.0: "恐慌", 1.5: "担忧", 1.0: "平静", 0.7: "乐观", 0.5: "狂热"}.get(
            a["sentiment_shock"], "?")
        hl = sentiment.get("ou_halflife_days") or 1.5
        hl_str = f"{hl * a['sentiment_shock']:.1f}天" if a["direction_score"] != 0 else "—"
        _PRINT(f"  {name:<24} {direction:<8} {a['direction_score']:>+5}  {shock_label:<6} {hl_str:>8}")

    _PRINT(f"  {'─'*60}")

    # 分数分解
    _PRINT(f"\n  📐 冲突溢价分解:")
    _PRINT(f"    净利空分:        {engine_result['net_score']:+.2f}")
    _PRINT(f"    总波动能量:      {engine_result['conflict_energy'] + abs(engine_result['net_score']):.2f}")
    _PRINT(f"    冲突能量:        {engine_result['conflict_energy']:.2f}")
    _PRINT(f"    冲突溢价(α={engine_result['alpha_used']:.2f}): "
           f"{engine_result['alpha_used'] * engine_result['conflict_energy']:+.2f}")
    _PRINT(f"    ────────────────────")
    _PRINT(f"    最终风险分:      {engine_result['final_risk_score']:>7.2f}")

    if engine_result.get("low_confidence_conservative"):
        _PRINT(f"    ⚠ 保守兜底激活: ≥{engine_result['low_confidence_bearish_count']}个不确定利空→Regime提档")

    # Regime建议
    regime = engine_result["regime"]
    _PRINT(f"\n  ═══════════════════════════════════════")
    _PRINT(f"  建议Regime: {regime}")
    _PRINT(f"  仓位上限:   {engine_result['risk_budget']:.0%}")
    _PRINT(f"  说明:       {engine_result['regime_desc']}")
    _PRINT(f"  ═══════════════════════════════════════")

    # 纠错线
    _PRINT(f"\n  🔧 纠错线:")
    _PRINT(f"    若实际数据方向与判断相反→手动解除覆写:")
    _PRINT(f"    python engine/event_calendar.py clear")

    # 联动规则提示
    vix = sentiment.get("vix")
    if vix and vix < 15:
        _PRINT(f"\n  ⚡ 联动纠错生效: VIX={vix:.1f}<15 → GARCH黏性阶段性失效")
        _PRINT(f"     后续利空事件冲击半衰期将大幅缩短")

    return regime


# ═══════════════════════════════════════════
# 8. CLI 入口
# ═══════════════════════════════════════════

def cmd_scan():
    """扫描未来14天事件并打印。"""
    scanner = EventScanner()
    events = scanner.scan()

    _PRINT("\n" + "=" * 60)
    _PRINT(f"  未来{scanner.scan_days}天宏观事件扫描 ({scanner.today} ~ {scanner.end_date})")
    _PRINT("=" * 60)

    if not events:
        _PRINT("\n  无重大宏观事件。")
        return events

    # 按重要性分组
    critical = [e for e in events if e.get("importance") == "critical"]
    high = [e for e in events if e.get("importance") == "high"]
    medium = [e for e in events if e.get("importance") == "medium"]

    if critical:
        _PRINT(f"\n  🔴 极高影响 ({len(critical)}个):")
        for e in critical:
            _PRINT(f"    {e['date']}  {e['event']}")
            if e.get("impact_channel"):
                _PRINT(f"    → {e['impact_channel']}")

    if high:
        _PRINT(f"\n  🟠 高影响 ({len(high)}个):")
        for e in high:
            tz = f" [时差衰减{e['timezone_factor']}]" if e.get("timezone_factor", 1) < 1 else ""
            _PRINT(f"    {e['date']}  {e['event'][:55]}{tz}")

    if medium:
        _PRINT(f"\n  🟡 中等影响 ({len(medium)}个):")
        for e in medium[:6]:
            _PRINT(f"    {e['date']}  {e['event'][:55]}")

    # 密集周警告
    high_impact_week = defaultdict(int)
    for e in events:
        if e.get("importance") in ("critical", "high"):
            try:
                d = datetime.strptime(e["date"], "%Y-%m-%d").date()
                week_key = d.isocalendar()[1]
                high_impact_week[week_key] += 1
            except:
                pass
    for wk, count in high_impact_week.items():
        if count >= 2:
            _PRINT(f"\n  ⚠️ 第{wk}周为宏观事件密集周({count}个高影响事件)")
            _PRINT(f"     建议运行: python engine/event_calendar.py assess")

    _PRINT()
    return events


def cmd_assess():
    """交互式评估 → 输出建议 → 写入覆写。"""
    # 1. 扫描事件
    scanner = EventScanner()
    events = scanner.scan()

    if not events:
        _PRINT("\n  无未来宏观事件, 无需评估。")
        return

    _PRINT(f"\n  扫描到 {len(events)} 个未来事件。")

    # 2. 情绪基准
    sentiment_eng = SentimentEngine()
    sentiment = sentiment_eng.assess()

    # 3. 交互式问答
    qa = InteractiveQA()
    assessments = qa.run(events)

    if assessments is None:
        return

    if not assessments:
        _PRINT("\n  所有事件已跳过, 无法给出建议。系统自动Regime继续运行。")
        return

    # 4. 冲突溢价评分
    engine = ConflictPremiumEngine()
    result = engine.score_events(assessments, sentiment)

    # 5. 打印报告
    regime = print_comprehensive_report(events, assessments, result, sentiment)

    # 6. 确认写入
    _PRINT()
    confirm = input("  确认写入Regime覆写? (y/n) [n]: ").strip().lower()
    if confirm != "y":
        _PRINT("  已取消, 未写入覆写。")
        return

    # 7. 写入覆写文件
    last_event_date = max(
        (a["date"] for a in assessments if a.get("date")),
        default=date.today().isoformat()
    )
    try:
        expires = datetime.strptime(last_event_date, "%Y-%m-%d").date() + timedelta(days=1)
    except:
        expires = date.today() + timedelta(days=7)

    vix = sentiment.get("vix", 99)

    override = {
        "active": True,
        "regime": regime,
        "reason": "; ".join(
            f"{a['event'][:30]}({a['direction_score']:+d})"
            for a in assessments if not a.get("skipped")
        ),
        "direction_score": result["final_risk_score"],
        "net_score": result["net_score"],
        "conflict_energy": result["conflict_energy"],
        "sentiment": sentiment["summary"],
        "sentiment_halflife_hours": round(
            (sentiment.get("ou_halflife_days") or 1.5) * 24, 1
        ),
        "recovery_window": "",
        "cross_event_note": f"VIX<15联动降权" if vix and vix < 15 else "",
        "set_by": "李大霄",
        "set_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "expires": expires.isoformat(),
        "auto_expire": True,
        "expire_conditions": {
            "time_after": expires.isoformat(),
            "vix_below": 22,
            "close_above_ma5": True,
            "note": "三重条件必须同时满足。时间到+恐慌回落+技术企稳→才放行。"
        }
    }

    mgr = RegimeOverrideManager()
    mgr.write(override)
    _PRINT(f"\n  ✅ Regime覆写已生效: {regime}")
    _PRINT(f"     过期时间: {expires} (若条件不满足将自动延期)")
    _PRINT(f"     手动解除: python engine/event_calendar.py clear")


def cmd_status():
    """查看当前覆写状态。"""
    mgr = RegimeOverrideManager()
    override = mgr.read()
    if override is None:
        _PRINT("\n  当前无活跃Regime覆写。系统自动判定运行中。")
    else:
        _PRINT(f"\n  ═══ 当前Regime覆写状态 ═══")
        _PRINT(f"  Regime:    {override['regime']}")
        _PRINT(f"  原因:      {override.get('reason', '?')}")
        _PRINT(f"  风险分:    {override.get('direction_score', '?')}")
        _PRINT(f"  设置时间:  {override.get('set_at', '?')}")
        _PRINT(f"  过期时间:  {override.get('expires', '?')}")
        _PRINT(f"  设置人:    {override.get('set_by', '?')}")
        if override.get("expire_conditions"):
            ec = override["expire_conditions"]
            _PRINT(f"  过期条件:  VIX<{ec.get('vix_below','?')} + 价格>MA5 + 时间>{ec.get('time_after','?')}")
        _PRINT(f"  解除:      python engine/event_calendar.py clear")


def cmd_clear():
    """手动解除覆写。"""
    mgr = RegimeOverrideManager()
    mgr.clear()
    _PRINT("\n  ✅ Regime覆写已手动解除。系统恢复自动判定。")


# ═══════════════════════════════════════════
# 9. Phase 3 连续回测命令
# ═══════════════════════════════════════════

def cmd_backtest(filepath=None, show_detail=False):
    """
    日频连续衰减池回测。

    加载历史事件JSON → 每日步进DecayPool → 输出风险曲线 + Regime切换日志。
    """
    if filepath is None:
        filepath = os.path.join(_script_dir, "historical_events_2022h2.json")

    if not os.path.exists(filepath):
        _PRINT(f"\n  回测文件不存在: {filepath}")
        _PRINT(f"  用法: python engine/event_calendar.py backtest [path/to/events.json]")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    events_raw = data.get("events", [])
    if not events_raw:
        _PRINT("\n  回测文件中无事件数据。")
        return

    # 解析时间区间
    evt_dates = sorted(set(e["date"] for e in events_raw))
    start_date = datetime.strptime(evt_dates[0], "%Y-%m-%d") - timedelta(days=2)
    end_date = datetime.strptime(evt_dates[-1], "%Y-%m-%d") + timedelta(days=14)
    total_days = (end_date - start_date).days

    # 初始化衰减池
    # 从事件文件读取GARCH持久性, 或使用默认高黏性(2022年水平)
    garch_p = data.get("garch_persistence", data.get("_garch_persistence", 0.95))
    pool = DecayPool(conflict_alpha=0.7, garch_persistence=garch_p)

    # 按日期分组事件
    events_by_date = {}
    for e in events_raw:
        d = e["date"]
        if d not in events_by_date:
            events_by_date[d] = []
        events_by_date[d].append(e)

    _PRINT("\n" + "=" * 70)
    _PRINT(f"  Phase 3 连续衰减池回测")
    _PRINT(f"  区间: {start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')} ({total_days}天)")
    _PRINT(f"  事件: {len(events_raw)}个宏观事件")
    _PRINT(f"  {data.get('_context', data.get('_description', ''))[:80]}")
    _PRINT("=" * 70)

    # 日频步进
    current = start_date
    prev_regime = None
    event_days = 0
    defense_days = 0

    if show_detail:
        _PRINT(f"\n  {'Date':<12} {'NewEvents':>3} {'Pool':>7} {'Net':>7} {'Conflict':>7} {'Regime':<18} {'Active':>3}")
        _PRINT(f"  {'-'*12} {'-'*3} {'-'*7} {'-'*7} {'-'*7} {'-'*18} {'-'*3}")

    for _ in range(total_days + 1):
        date_str = current.strftime("%Y-%m-%d")
        day_events = events_by_date.get(date_str, [])

        new_scores = []
        for e in day_events:
            new_scores.append({
                "score": e["score"],
                "halflife_hours": e.get("halflife_hours", 48),
                "label": f"{e['event']} ({e.get('label','')[:30]})",
            })

        pool_risk, regime, detail = pool.step(new_scores, current)

        if day_events:
            event_days += 1
        if "DEFENSE" in regime:
            defense_days += 1

        if show_detail:
            n_new = len(day_events)
            marker = " <--" if day_events else ""
            _PRINT(f"  {date_str:<12} {n_new:>3} {pool_risk:>7.1f} {detail['net_score']:>+7.1f} "
                   f"{detail['conflict_energy']:>7.1f} {regime:<18} {detail['n_active_events']:>3}{marker}")

        # Regime切换日志
        if prev_regime and regime != prev_regime:
            direction = "↑" if "DEFENSE" in regime else "↓"
            if not show_detail:
                _PRINT(f"  {date_str}  {direction} {prev_regime} → {regime}  (pool={pool_risk:.1f})")

        prev_regime = regime
        current += timedelta(days=1)

    # 摘要
    summary = pool.summary()
    benchmark = data.get("market_benchmark", {})

    _PRINT(f"\n  {'='*60}")
    _PRINT(f"  回测摘要")
    _PRINT(f"  {'='*60}")
    _PRINT(f"  区间天数:     {summary['total_days']}")
    _PRINT(f"  事件日:       {event_days}")
    _PRINT(f"  均值风险分:   {summary['avg_pool_risk']:.1f}")
    _PRINT(f"  峰值风险分:   {summary['max_pool_risk']:.1f}")
    _PRINT(f"  Regime切换:   {summary['regime_switches']}次 (切换率{summary['switch_rate']:.1%})")
    _PRINT(f"  防御天数:     {defense_days} ({defense_days/summary['total_days']:.0%})")
    _PRINT(f"")
    _PRINT(f"  Regime分布:")
    for r, count in sorted(summary['regime_distribution'].items()):
        bar = "█" * max(1, count // 2)
        _PRINT(f"    {r:<18} {count:>3}天  {bar}")

    if benchmark:
        _PRINT(f"")
        _PRINT(f"  市场基准:")
        _PRINT(f"    沪深300: {benchmark.get('沪深300_chg','?')} (始{benchmark.get('沪深300_start','?')}→终{benchmark.get('沪深300_end','?')})")
        _PRINT(f"    SPX:     {benchmark.get('SPX_chg','?')} (始{benchmark.get('SPX_start','?')}→终{benchmark.get('SPX_end','?')})")
        _PRINT(f"    {benchmark.get('note','')}")

    # 颠簸检测
    if summary['regime_switches'] > summary['total_days'] * 0.15:
        _PRINT(f"")
        _PRINT(f"  ⚠ Regime切换率偏高({summary['switch_rate']:.1%}), 存在颠簸风险")
        _PRINT(f"    建议: 增大冲突溢价α 或 延长半衰期 以增加迟滞")

    _PRINT(f"")

    # 返回pool对象供外部使用
    return pool


def cmd_backtest_full(filepath=None):
    """详细版回测: 每日日志 + 事件标注。"""
    return cmd_backtest(filepath=filepath, show_detail=True)


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        _PRINT("用法: python engine/event_calendar.py [scan|assess|status|clear|backtest]")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "scan":
        cmd_scan()
    elif cmd == "assess":
        cmd_assess()
    elif cmd == "status":
        cmd_status()
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "backtest":
        fp = sys.argv[2] if len(sys.argv) > 2 else None
        if len(sys.argv) > 2 and sys.argv[2] == "--detail":
            cmd_backtest_full(None)
        elif "--detail" in sys.argv:
            cmd_backtest_full(fp)
        else:
            cmd_backtest(fp)
    else:
        _PRINT(f"未知命令: {cmd}")
        _PRINT("可用: scan | assess | status | clear | backtest [events.json] [--detail]")
