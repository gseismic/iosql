from typing import Any, Dict
import arrow
from .base import Partition


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
    
    def __init__(self, fmt: str, tz: str, ts_col: str):
        """
        Args:
            fmt: Date format string (e.g., 'YYYYMMDD', 'YYYYMM', 'YYYYMMDDHH')
            tz: Timezone string (e.g., 'Asia/Shanghai', 'UTC')
            ts_col: Column name containing timestamp/datetime
        """
        self.fmt = fmt
        self.tz = tz
        self.TS_COL = ts_col
    
    def get_key(self, row: Dict[str, Any]) -> str:
        """Get partition key as formatted string.
        
        Args:
            row: Data row with ts_col containing datetime/timestamp
            
        Returns:
            Formatted partition key string
        """
        dt = arrow.get(row[self.TS_COL]).to(self.tz)
        return dt.format(self.fmt)
    
    def get_range(self, key: str) -> tuple[int, int]:
        """Get [start_ms, end_ms) range for partition key.
        
        Args:
            key: Formatted partition key string
            
        Returns:
            Tuple of (start_timestamp_ms, end_timestamp_ms)
        """
        start_dt = arrow.get(key, self.fmt, tzinfo=self.tz).to('UTC')
        end_dt = start_dt.shift(**self.FMT_SHIFT_MAP.get(self.fmt, {'days': 1}))
        return (
            int(start_dt.timestamp() * 1000),
            int(end_dt.timestamp() * 1000)
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
        return {
            'type': self.PARTITION_TYPE,
            'ts_col': self.TS_COL,
            'fmt': self.fmt,
            'tz': self.tz,
        }
    
    @classmethod
    def from_meta(cls, meta: Dict[str, Any]) -> 'CalendarPartition':
        """Reconstruct from metadata.
        
        Args:
            meta: Metadata dictionary
            
        Returns:
            CalendarPartition instance
        """
        return cls(
            fmt=meta['fmt'],
            tz=meta['tz'],
            ts_col=meta['ts_col'],
        )
