import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict
import arrow
from .base import Partition
from ..exception import PartitionSpecError


class CalendarPartition(Partition):
    """Calendar-based partitioning with timezone support.
    
    Partition key is a formatted string (e.g., '20240115', '202401').
    Boundaries are calculated based on the format and timezone.
    
    Example:
        partition = CalendarPartition(fmt='YYYYMMDD', tz='Asia/Shanghai', ts_col='date')
        # date "2024-01-15 08:30:00+08:00" (Shanghai time) 
        # → partition_key = "20240115"
        # files: /table/20240115.sqlite3
    """
    
    PARTITION_TYPE = 'calendar'
    
    FMT_SHIFT_MAP = {
        'YYYY': {'years': 1},
        'YYYYMM': {'months': 1},
        'YYYYMMDD': {'days': 1},
        'YYYYMMDDHH': {'hours': 1},
        'YYYYMMDDHHmm': {'minutes': 1},
    }

    UNIT_ALIASES = {
        'y': 'year',
        'year': 'year',
        'years': 'year',
        'mo': 'month',
        'mon': 'month',
        'month': 'month',
        'months': 'month',
        'w': 'week',
        'week': 'week',
        'weeks': 'week',
        'd': 'day',
        'day': 'day',
        'days': 'day',
        'h': 'hour',
        'hr': 'hour',
        'hour': 'hour',
        'hours': 'hour',
        'min': 'minute',
        'minute': 'minute',
        'minutes': 'minute',
    }

    FMT_BY_UNIT = {
        'year': 'YYYY',
        'month': 'YYYYMM',
        'week': 'YYYYMMDD',
        'day': 'YYYYMMDD',
        'hour': 'YYYYMMDDHH',
        'minute': 'YYYYMMDDHHmm',
    }

    DEFAULT_EVERY_BY_FMT = {
        'YYYY': '1year',
        'YYYYMM': '1month',
        'YYYYMMDD': '1day',
        'YYYYMMDDHH': '1hour',
        'YYYYMMDDHHmm': '1minute',
    }
    
    def __init__(self, fmt: str, tz: str, ts_col: str, every: Any = None,
                 *, time_unit=None):
        """
        Args:
            fmt: Date format string (e.g., 'YYYYMMDD', 'YYYYMM', 'YYYYMMDDHH')
            tz: Timezone string (e.g., 'Asia/Shanghai', 'UTC')
            ts_col: Column name containing timestamp/datetime
            every: Calendar interval, e.g. '1day', '1week', '4day'
        """
        # fmt 白名单收口：任意字符串会经 format() 生成分区 key 并拼进文件名，
        # 非白名单值（如 ../../../pwn、YYYY/MM/DD）可能造成路径穿越或多级目录。
        if fmt not in self.FMT_SHIFT_MAP:
            raise PartitionSpecError(
                f"不支持的 calendar fmt `{fmt}`；可选: "
                f"{', '.join(self.FMT_SHIFT_MAP)}。"
                "修复: 更新 manifest 中的 fmt，或删除该表 partition 元数据后重建"
            )
        self.fmt = fmt
        self.tz = tz
        self.TS_COL = ts_col
        if time_unit not in (None, 'date', 'ms', 's'):
            raise PartitionSpecError('calendar time_unit 必须是 ms/s/date')
        self.time_unit = time_unit
        self.every = self.normalize_every(every) if every else None
        count, unit = self.parse_every(self.every or self.default_every_for_fmt(fmt))
        self._every_count = count
        self._every_unit = unit
        self._key_for_value = lru_cache(maxsize=4096)(
            self._key_for_value_uncached
        )

    @classmethod
    def parse_every(cls, every: Any) -> tuple[int, str]:
        text = str(every or '1day').strip().lower()
        match = re.fullmatch(r'(?:(\d+)\s*)?([a-z]+)', text)
        if not match:
            raise PartitionSpecError(
                f'Unsupported calendar partition interval: {every}'
            )
        count = int(match.group(1) or '1')
        if count <= 0:
            raise PartitionSpecError('calendar partition interval must be positive')
        unit = cls.UNIT_ALIASES.get(match.group(2))
        if unit is None:
            raise PartitionSpecError(
                f'Unsupported calendar partition interval: {every}'
            )
        return count, unit

    @classmethod
    def normalize_every(cls, every: Any) -> str:
        count, unit = cls.parse_every(every)
        return f'{count}{unit}'

    @classmethod
    def format_for_every(cls, every: Any) -> str:
        _, unit = cls.parse_every(every)
        return cls.FMT_BY_UNIT[unit]

    @classmethod
    def default_every_for_fmt(cls, fmt: str) -> str:
        return cls.DEFAULT_EVERY_BY_FMT.get(fmt, '1day')

    def _bucket_start(self, dt):
        count = self._every_count
        unit = self._every_unit
        if unit == 'year':
            year = max((dt.year // count) * count, 1)
            return dt.replace(
                year=year, month=1, day=1,
                hour=0, minute=0, second=0, microsecond=0,
            )
        if unit == 'month':
            month_index = dt.year * 12 + dt.month - 1
            start_index = (month_index // count) * count
            return dt.replace(
                year=start_index // 12,
                month=start_index % 12 + 1,
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )
        if unit == 'week':
            origin = arrow.get(1970, 1, 5, tzinfo=self.tz)
            week_start = dt.floor('week')
            delta_days = (week_start.date() - origin.date()).days
            weeks = delta_days // 7
            return origin.shift(weeks=(weeks // count) * count)
        if unit == 'day':
            if count <= 366:
                origin = arrow.get(dt.year, 1, 1, tzinfo=self.tz)
            else:
                origin = arrow.get(1970, 1, 1, tzinfo=self.tz)
            delta_days = (dt.floor('day').date() - origin.date()).days
            return origin.shift(days=(delta_days // count) * count)

        origin = arrow.get(1970, 1, 1, tzinfo=self.tz)
        seconds = int((dt.datetime - origin.datetime).total_seconds())
        scale = 3600 if unit == 'hour' else 60
        units = seconds // scale
        shift = {'hours' if unit == 'hour' else 'minutes': (units // count) * count}
        return origin.shift(**shift)

    def _shift_kwargs(self) -> Dict[str, int]:
        plural = {
            'year': 'years',
            'month': 'months',
            'week': 'weeks',
            'day': 'days',
            'hour': 'hours',
            'minute': 'minutes',
        }[self._every_unit]
        return {plural: self._every_count}
    
    def _key_for_value_uncached(self, value: Any) -> str:
        dt = self._datetime(value)
        return self._bucket_start(dt).format(self.fmt)

    def _datetime(self, value):
        if self.time_unit in ('ms', 's') and isinstance(value, (int, float)):
            seconds = value / 1000 if self.time_unit == 'ms' else value
            # 显式单位避免 Arrow 把早期 epoch 毫秒猜成秒。
            value = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return arrow.get(value).to(self.tz)

    def get_key(self, row: Dict[str, Any]) -> str:
        """Get partition key as formatted string.
        
        Args:
            row: Data row with ts_col containing datetime/timestamp
            
        Returns:
            Formatted partition key string
        """
        value = row[self.TS_COL]
        try:
            return self._key_for_value(value)
        except TypeError:
            # 非 hashable 自定义日期对象仍走兼容路径。
            dt = self._datetime(value)
            return self._bucket_start(dt).format(self.fmt)
    
    def get_range(self, key: str) -> tuple[int, int]:
        """Get [start_ms, end_ms) range for partition key.
        
        Args:
            key: Formatted partition key string
            
        Returns:
            Tuple of (start_timestamp_ms, end_timestamp_ms)
        """
        start_dt = arrow.get(key, self.fmt, tzinfo=self.tz)
        end_dt = start_dt.shift(**self._shift_kwargs())
        return (
            int(start_dt.to('UTC').timestamp() * 1000),
            int(end_dt.to('UTC').timestamp() * 1000)
        )
    
    def get_file_suffix(self, key: str) -> str:
        """Get file suffix for partition key.
        
        Args:
            key: Formatted partition key string
            
        Returns:
            File suffix like '20240115.sqlite3'
        """
        return f'{key}.sqlite3'
    
    def to_meta(self) -> Dict[str, Any]:
        """Serialize to metadata dict."""
        meta = {
            'type': self.PARTITION_TYPE,
            'ts_col': self.TS_COL,
            'fmt': self.fmt,
            'tz': self.tz,
        }
        default_every = self.default_every_for_fmt(self.fmt)
        if self.every and self.every != default_every:
            meta['every'] = self.every
        if self.time_unit in ('ms', 's'):
            meta['time_unit'] = self.time_unit
        return meta
    
    @classmethod
    def from_meta(cls, meta: Dict[str, Any]) -> 'CalendarPartition':
        """Reconstruct from metadata.
        
        Args:
            meta: Metadata dictionary
            
        Returns:
            CalendarPartition instance

        Raises:
            PartitionSpecError: manifest 缺少必要字段或 fmt 不在白名单内
        """
        missing = [key for key in ('fmt', 'tz', 'ts_col') if not meta.get(key)]
        if missing:
            raise PartitionSpecError(
                f"calendar partition meta 缺少字段 {missing} (meta={meta!r})；"
                "修复: 更新 manifest 中的 partition 元数据，"
                "或删除 partition 后按原契约重建该表"
            )
        return cls(
            fmt=meta['fmt'],
            tz=meta['tz'],
            ts_col=meta['ts_col'],
            every=meta.get('every'),
            time_unit=meta.get('time_unit'),
        )
