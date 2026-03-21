from typing import Any, Dict, Union
from .base import Partition


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
        '5min': 300_000,
        '15min': 900_000,
        '30min': 1_800_000,
        '60min': 3_600_000,
        '1hour': 3_600_000,
        '4hour': 14_400_000,
        '1day': 86_400_000,
        '1week': 604_800_000,
        '1month': 2_592_000_000,
        '2month': 5_184_000_000,
        '3month': 7_776_000_000,
        '6month': 15_552_000_000,
        '1year': 31_536_000_000,
        '2year': 63_072_000_000,
        '5year': 157_680_000_000,
        '10year': 315_360_000_000,
    }
    
    def __init__(self, interval_ms: int, ts_col: str):
        """
        Args:
            interval_ms: Duration of each partition in milliseconds, or alias like '1day'
            ts_col: Column name containing timestamp in milliseconds
        """
        self.TS_COL = ts_col
        if isinstance(interval_ms, str):
            if interval_ms not in self.INTERVAL_ALIASES:
                interval_ms = int(interval_ms)
            else:
                interval_ms = self.INTERVAL_ALIASES[interval_ms]
        self.interval_ms = interval_ms
    
    def get_key(self, row: Dict[str, Any]) -> int:
        """Get partition key (aligned to interval boundary).
        
        Args:
            row: Data row with ts_col containing timestamp in milliseconds
            
        Returns:
            Integer partition key aligned to interval boundary
        """
        ts = row[self.TS_COL]
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
        """
        return cls(
            interval_ms=meta['interval_ms'],
            ts_col=meta['ts_col'],
        )
