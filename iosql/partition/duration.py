import math
from typing import Any, Dict, Union
from .base import Partition, normalize_part_value
from ..exception import PartitionSpecError


def parse_interval_ms(value: Any) -> int:
    """把分区周期统一解析为毫秒，支持别名与任意 `Nunit` 形式。

    接受 int、整数值 float 与纯数字字符串；拒绝 bool/非整数 float/非正值。
    month/year 为固定近似：1month = 30day，1year = 365day，与
    `_format_duration_every` 的反推口径一致，保证 format→parse 往返稳定。

    与 `PartitionSpec`/`timeseries(partition_every=...)` 的解析口径一致，
    保证 `Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')`
    行为等价（PLAN-054 N2 统一间隔词汇表）。
    """
    if isinstance(value, bool):
        raise PartitionSpecError(
            f"partition duration must be a positive integer, got bool `{value}`"
        )
    if isinstance(value, int):
        if value <= 0:
            raise PartitionSpecError("partition duration must be positive")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value <= 0:
            raise PartitionSpecError(
                "partition duration must be an integer number of milliseconds, "
                f"got `{value}`"
            )
        return int(value)

    text = str(value).strip().lower()
    if text in DurationPartition.INTERVAL_ALIASES:
        return DurationPartition.INTERVAL_ALIASES[text]
    if text.isdigit():
        count = int(text)
        if count <= 0:
            raise PartitionSpecError("partition duration must be positive")
        return count

    units = [
        ("milliseconds", 1),
        ("millisecond", 1),
        ("msecs", 1),
        ("msec", 1),
        ("ms", 1),
        ("seconds", 1_000),
        ("second", 1_000),
        ("secs", 1_000),
        ("sec", 1_000),
        ("s", 1_000),
        ("minutes", 60_000),
        ("minute", 60_000),
        ("mins", 60_000),
        ("min", 60_000),
        ("hours", 3_600_000),
        ("hour", 3_600_000),
        ("hrs", 3_600_000),
        ("hr", 3_600_000),
        ("h", 3_600_000),
        ("days", 86_400_000),
        ("day", 86_400_000),
        ("d", 86_400_000),
        ("weeks", 604_800_000),
        ("week", 604_800_000),
        ("w", 604_800_000),
        # 月/年按固定近似换算（30d/365d），与 _format_duration_every 的分母一致
        ("months", 2_592_000_000),
        ("month", 2_592_000_000),
        ("years", 31_536_000_000),
        ("year", 31_536_000_000),
    ]
    for suffix, scale in sorted(units, key=lambda item: len(item[0]), reverse=True):
        if text.endswith(suffix):
            number = text[:-len(suffix)] or "1"
            try:
                count = int(number)
            except ValueError as e:
                raise PartitionSpecError(f"Bad partition duration `{value}`") from e
            if count <= 0:
                raise PartitionSpecError("partition duration must be positive")
            return count * scale
    raise PartitionSpecError(f"Unsupported partition duration `{value}`")


class DurationPartition(Partition):
    """Duration-based partitioning by fixed integer intervals.
    
    Timestamp is in milliseconds. Partition boundaries are aligned to UTC.
    
    Example:
        partition = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        # timestamp 1705276800000 (2024-01-15 00:00:00 UTC) 
        # → partition_key = 1705276800000
        # files: /table/1705276800000.sqlite3
        
    Common intervals:
        1min = 60_000
        5min = 300_000
        15min = 900_000
        30min = 1_800_000
        60min/1hour = 3_600_000
        1day = 86_400_000
        1week = 604_800_000
        1month ≈ 2_592_000_000
        1year = 31_536_000_000
    """
    
    PARTITION_TYPE = 'duration'
    
    INTERVAL_ALIASES: Dict[str, int] = {
        '1min': 60_000,
        '1minute': 60_000,
        '1minutes': 60_000,
        '5min': 300_000,
        '15min': 900_000,
        '30min': 1_800_000,
        '60min': 3_600_000,
        '1hour': 3_600_000,
        '1hours': 3_600_000,
        '4hour': 14_400_000,
        '4hours': 14_400_000,
        '1day': 86_400_000,
        '1days': 86_400_000,
        '1week': 604_800_000,
        '1weeks': 604_800_000,
        '1month': 2_592_000_000,
        '1months': 2_592_000_000,
        '2month': 5_184_000_000,
        '2months': 5_184_000_000,
        '3month': 7_776_000_000,
        '3months': 7_776_000_000,
        '6month': 15_552_000_000,
        '6months': 15_552_000_000,
        '1year': 31_536_000_000,
        '1years': 31_536_000_000,
        '2year': 63_072_000_000,
        '2years': 63_072_000_000,
        '5year': 157_680_000_000,
        '5years': 157_680_000_000,
        '10year': 315_360_000_000,
        '10years': 315_360_000_000,
    }
    
    def __init__(self, interval_ms: int, ts_col: str):
        """
        Args:
            interval_ms: Duration of each partition in milliseconds, or alias like '1day'
            ts_col: Column name containing timestamp in milliseconds
        """
        self.TS_COL = ts_col
        self.interval_ms = parse_interval_ms(interval_ms)
    
    def get_key(self, row: Dict[str, Any]) -> int:
        """Get partition key (aligned to interval boundary).
        
        Args:
            row: Data row with ts_col containing timestamp in milliseconds
            
        Returns:
            Integer partition key aligned to interval boundary
        """
        ts = normalize_part_value(
            row[self.TS_COL], f"duration partition field `{self.TS_COL}`"
        )
        return (ts // self.interval_ms) * self.interval_ms
    
    def get_range(self, key: int) -> tuple[int, int]:  # type: ignore[override]
        """Get [start_ms, end_ms) range for partition key.
        
        Args:
            key: Integer partition key
            
        Returns:
            Tuple of (start_timestamp_ms, end_timestamp_ms)
        """
        return (key, key + self.interval_ms)
    
    def get_file_suffix(self, key: int) -> str:  # type: ignore[override]
        """Get file suffix for partition key.
        
        Args:
            key: Integer partition key
            
        Returns:
            File suffix like '1705276800000.sqlite3'
        """
        return f'{key}.sqlite3'
    
    def to_meta(self) -> Dict[str, Any]:
        """Serialize to metadata dict."""
        return {
            'type': self.PARTITION_TYPE,
            'ts_col': self.TS_COL,
            'interval_ms': self.interval_ms,
        }
    
    @classmethod
    def from_meta(cls, meta: Dict[str, Any]) -> 'DurationPartition':
        """Reconstruct from metadata.
        
        Args:
            meta: Metadata dictionary
            
        Returns:
            DurationPartition instance

        Raises:
            PartitionSpecError: manifest 缺少必要字段或 interval_ms 非法
        """
        missing = [key for key in ('interval_ms', 'ts_col') if meta.get(key) is None]
        if missing:
            raise PartitionSpecError(
                f"duration partition meta 缺少字段 {missing} (meta={meta!r})；"
                "修复: 更新 manifest 中的 partition 元数据，"
                "或删除 partition 后按原契约重建该表"
            )
        return cls(
            interval_ms=meta['interval_ms'],
            ts_col=meta['ts_col'],
        )
