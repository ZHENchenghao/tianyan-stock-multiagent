# -*- coding: utf-8 -*-
"""
DataGuard - data freshness enforcement framework v1.0

Core principle: stale data CANNOT be silently consumed.
Default behavior is BLOCK, not WARN.

Usage:
    from engine.data_guard import DataCell, DataGuard, DataStaleError

    cell = DataCell.from_value({'price': 1.295}, 'akshare.fund_etf_spot_em',
                                data_date=date.today())
    cell.require_fresh(max_days=0)
    cell.acknowledge_stale("weekend, using Friday close")
"""

import os
import json
import warnings
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field as dc_field
from typing import Any, Optional, List, Tuple, Dict


# ---- freshness classification (single source of truth) ----

def freshness_level(days: int) -> str:
    """Unified freshness level for the entire system."""
    if days == 999:
        return 'unknown'
    if days <= 1:
        return 'fresh'
    elif days <= 3:
        return 'stale'
    else:
        return 'expired'


def _compute_freshness(data_date):
    """Compute freshness days and level from a date."""
    if data_date is None:
        return 999, 'unknown'
    days = (date.today() - data_date).days
    return days, freshness_level(days)


# ---- exceptions ----

class DataStaleError(Exception):
    """Raised when require_fresh() encounters stale data.

    This is a HARD block. Callers MUST either:
      A) try/except and fallback to alternative data source
      B) cell.acknowledge_stale(reason) to explicitly accept
      C) let it crash (safe default)
    """

    def __init__(self, cell, max_days):
        self.cell = cell
        self.max_days = max_days
        chain = ' -> '.join(cell.fallback_chain) if cell.fallback_chain else 'none'
        super().__init__(
            f"STALE DATA: {cell.source} | "
            f"data_date={cell.data_date or 'unknown'} "
            f"({cell.freshness_days}d old, max={max_days}d) | "
            f"fallback: {chain} | "
            f"call cell.acknowledge_stale('reason') to accept"
        )


class DataGuardWarning(UserWarning):
    """Warning emitted when DataGuard enforcement is bypassed."""
    pass


# ---- DataCell ----

@dataclass
class DataCell:
    """Universal wrapper for all external data fetches.

    Carries mandatory freshness metadata and enforcement latch.
    """

    data: Any
    source: str
    fetched_at: datetime
    data_date: Optional[date] = None
    freshness_days: int = 999
    freshness_level: str = 'unknown'
    fallback_chain: List[str] = dc_field(default_factory=list)
    confidence: float = 100.0
    table: Optional[str] = None
    field_name: Optional[str] = None
    extra: Dict[str, Any] = dc_field(default_factory=dict)

    # internal audit state
    _acknowledged_stale: bool = dc_field(default=False, repr=False)
    _stale_ack_reason: Optional[str] = dc_field(default=None, repr=False)

    @property
    def is_fresh(self):
        return self.freshness_level == 'fresh'

    @property
    def is_stale(self):
        return self.freshness_level in ('stale', 'expired', 'unknown')

    @property
    def is_expired(self):
        return self.freshness_level in ('expired', 'unknown')

    @property
    def ok(self):
        """Data is usable (fresh or explicitly acknowledged stale)."""
        return self.is_fresh or self._acknowledged_stale

    def require_fresh(self, max_days=1):
        """Enforce freshness. Raises DataStaleError if stale."""
        if self.freshness_days > max_days:
            raise DataStaleError(self, max_days)
        return self

    def acknowledge_stale(self, reason):
        """Explicitly accept stale data. Logs to permanent audit trail."""
        self._acknowledged_stale = True
        self._stale_ack_reason = reason
        _audit_log(
            level='STALE_ACCEPTED',
            source=self.source,
            data_date=str(self.data_date) if self.data_date else 'unknown',
            freshness_days=self.freshness_days,
            freshness_level=self.freshness_level,
            fallback_chain=self.fallback_chain,
            reason=reason,
        )
        return self

    def unwrap(self):
        """Extract raw data. Warns if stale and unacknowledged."""
        if self.is_stale and not self._acknowledged_stale:
            warnings.warn(
                f"DataCell.unwrap() called on unacknowledged stale data "
                f"({self.source}, {self.freshness_days}d old). "
                f"Call cell.acknowledge_stale('reason') first.",
                DataGuardWarning, stacklevel=2,
            )
        return self.data

    def badge(self):
        """Freshness badge for reports."""
        if self.freshness_level == 'fresh':
            return '[FRESH]'
        elif self.freshness_level == 'stale':
            return f'[STALE:{self.freshness_days}d]'
        elif self.freshness_level == 'expired':
            return f'[EXPIRED:{self.freshness_days}d]'
        else:
            return '[UNKNOWN]'

    def status_line(self):
        """One-line status summary."""
        s = self.badge()
        if self._acknowledged_stale:
            s += ' ACK'
        src = self.source.split('.')[-1] if '.' in self.source else self.source
        return f"{s} {src} conf={self.confidence:.0f}%"

    # ---- factories ----

    @classmethod
    def from_value(cls, value, source, data_date=None, confidence=100.0, **kwargs):
        """Wrap a scalar/dict value."""
        days, level = _compute_freshness(data_date)
        return cls(
            data=value, source=source, fetched_at=datetime.now(),
            data_date=data_date, freshness_days=days, freshness_level=level,
            confidence=confidence, **kwargs,
        )

    @classmethod
    def from_dataframe(cls, df, source, date_col='trade_date', **kwargs):
        """Wrap a DataFrame with auto freshness detection."""
        data_date = None
        days = 999
        if df is not None and not df.empty and date_col in df.columns:
            try:
                import pandas as pd
                latest = pd.to_datetime(df[date_col].max())
                data_date = latest.date()
                days = (date.today() - data_date).days
            except Exception:
                pass
        level = freshness_level(days)
        return cls(
            data=df, source=source, fetched_at=datetime.now(),
            data_date=data_date, freshness_days=days, freshness_level=level,
            **kwargs,
        )

    @classmethod
    def empty(cls, reason):
        """Create an empty/failed sentinel cell."""
        return cls(
            data=None, source='NONE', fetched_at=datetime.now(),
            data_date=None, freshness_days=999, freshness_level='unknown',
            confidence=0.0, extra={'empty_reason': reason},
        )


# ---- audit log ----

_AUDIT_LOG_PATH = None


def _get_audit_log_path():
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is None:
        log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        _AUDIT_LOG_PATH = os.path.join(log_dir, 'data_guard_audit.jsonl')
    return _AUDIT_LOG_PATH


def _audit_log(level, **fields):
    """Write to permanent audit log."""
    entry = {'timestamp': datetime.now().isoformat(), 'level': level, **fields}
    try:
        with open(_get_audit_log_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


# ---- DataGuard facade ----

class DataGuard:
    """Unified entry point for data freshness enforcement.

    Usage:
        guard = DataGuard()
        ok, issues = guard.preflight_check()
        if not ok:
            raise SystemExit("data stale, run collect first")
    """

    def __init__(self):
        self._da = None

    @property
    def da(self):
        if self._da is None:
            try:
                from engine.data_availability import get_da
                self._da = get_da()
            except Exception:
                self._da = None
        return self._da

    def preflight_check(self, as_of_date=None):
        """Check all critical tables before running reports.

        Returns (all_ok, issues_list).
        If all_ok is False, the run should ABORT.
        """
        today = as_of_date or date.today()

        # Path 1: DataAvailability (field-level precision)
        if self.da is not None:
            try:
                result = self.da.check_live_freshness(as_of_date=today)
                if isinstance(result, dict):
                    all_fresh = result.get('all_fresh', True)
                    items = result.get('items', [])
                elif isinstance(result, (list, tuple)) and len(result) == 2:
                    all_fresh, items = result
                else:
                    all_fresh, items = True, []
            except Exception:
                all_fresh, items = True, []
        else:
            all_fresh, items = True, []

        # Path 2: fallback simple check
        if not items:
            all_fresh, items = self._simple_preflight(today)

        issues = self._items_to_cells(items)

        stale_count = sum(1 for c in issues if c.is_stale)
        if stale_count > 0:
            _audit_log(
                level='PREFLIGHT_WARN',
                all_ok=all_fresh,
                stale_count=stale_count,
                total_checked=len(issues),
                sources=[c.source for c in issues if c.is_stale],
            )

        return all_fresh, issues

    def _simple_preflight(self, today):
        """Fallback: check MAX(date) on critical DuckDB tables."""
        items = []
        critical = [
            ('kline_daily', 'trade_date', 0),
            ('macro_indicators', 'trade_date', 0),
            ('market_sentiment', 'trade_date', 1),
            ('technical_indicators', 'trade_date', 0),
        ]
        try:
            import duckdb
            db = r'D:\FreeFinanceData\data\duckdb\finance.db'
            conn = duckdb.connect(db, read_only=True)
            for table, date_col, max_lag in critical:
                try:
                    row = conn.execute(
                        f"SELECT MAX({date_col}) FROM {table}"
                    ).fetchone()
                    if row and row[0]:
                        latest_date = row[0]
                        if isinstance(latest_date, str):
                            latest_date = date.fromisoformat(latest_date)
                        lag = (today - latest_date).days
                        items.append({
                            'table': table, 'column': date_col,
                            'latest': str(latest_date), 'actual_lag': lag,
                            'max_lag': max_lag, 'fresh': lag <= max_lag,
                        })
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass

        all_fresh = all(item['fresh'] for item in items)
        return all_fresh, items

    def _items_to_cells(self, items):
        """Convert preflight result items to DataCell list."""
        cells = []
        for item in items:
            if isinstance(item, DataCell):
                cells.append(item)
                continue

            data_date = None
            if item.get('latest') and item['latest'] != '?':
                try:
                    data_date = date.fromisoformat(item['latest'])
                except (ValueError, TypeError):
                    pass

            days = item.get('actual_lag', 999)
            cells.append(DataCell(
                data=None,
                source=f"duckdb.{item.get('table', '?')}.{item.get('column', '?')}",
                fetched_at=datetime.now(),
                data_date=data_date,
                freshness_days=days,
                freshness_level=freshness_level(days),
                confidence=100 if item.get('fresh') else 50,
                table=item.get('table'),
                field_name=item.get('column'),
            ))

        return cells

    # ---- convenience factories ----

    def wrap_value(self, value, source, data_date=None, **kwargs):
        return DataCell.from_value(value, source, data_date, **kwargs)

    def wrap_dataframe(self, df, source, date_col='trade_date', **kwargs):
        return DataCell.from_dataframe(df, source, date_col, **kwargs)


# ---- module-level singleton ----

_guard_instance = None


def get_guard():
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = DataGuard()
    return _guard_instance


# ---- self-test ----

if __name__ == '__main__':
    print("=== DataGuard Self-Test ===\n")

    # Test 1: fresh data passes
    cell = DataCell.from_value({'price': 1.5}, 'test.source', data_date=date.today())
    try:
        cell.require_fresh(max_days=0)
        print(f"OK fresh: {cell.status_line()}")
    except DataStaleError as e:
        print(f"FAIL: {e}")

    # Test 2: stale data blocked
    cell2 = DataCell.from_value({'price': 1.5}, 'test.source',
                                 data_date=date.today() - timedelta(days=5))
    try:
        cell2.require_fresh(max_days=0)
        print("FAIL: should have raised")
    except DataStaleError:
        print(f"OK blocked: freshness_days={cell2.freshness_days}")

    # Test 3: acknowledge
    cell2.acknowledge_stale("test: explicit acceptance")
    assert cell2.ok
    print(f"OK acknowledged: ok={cell2.ok}")

    # Test 4: preflight
    guard = DataGuard()
    ok, issues = guard.preflight_check()
    print(f"\nPreflight: ok={ok}, checked={len(issues)} items")
    for c in issues:
        print(f"  {c.status_line()}")

    # Test 5: empty
    empty = DataCell.empty("test")
    assert empty.is_expired and empty.data is None
    print(f"\nOK empty: {empty.status_line()}")

    print("\n=== All Tests Passed ===")
