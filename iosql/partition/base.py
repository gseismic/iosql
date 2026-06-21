from abc import ABC, abstractmethod
from typing import Any, Dict, Union


class Partition(ABC):
    """Abstract base class for logical partition strategies."""
    
    TS_COL: str
    PARTITION_TYPE: str

    @classmethod
    def range(cls, column: str, every: int) -> 'Partition':
        from .range import RangePartition

        return RangePartition(width=every, column=column)

    @classmethod
    def duration(cls, column: str, every: Any = '1day') -> 'Partition':
        from .duration import DurationPartition

        return DurationPartition(interval_ms=every, ts_col=column)

    @classmethod
    def calendar(
        cls,
        column: str,
        every: Any = '1day',
        timezone: str = 'UTC',
    ) -> 'Partition':
        from .calendar import CalendarPartition

        fmt = cls._calendar_format(every)
        return CalendarPartition(fmt=fmt, tz=timezone, ts_col=column)

    @staticmethod
    def _calendar_format(every: Any) -> str:
        from .calendar import CalendarPartition

        return CalendarPartition.format_for_every(every)
    
    @abstractmethod
    def get_key(self, row: Dict[str, Any]) -> Union[int, str]:
        """Get partition key for a row.
        
        Args:
            row: Dictionary representing a data row
            
        Returns:
            Partition key (int for duration, str for calendar)
        """
        pass
    
    @abstractmethod
    def get_range(self, key: Union[int, str]) -> tuple[int, int]:
        """Get [start_ms, end_ms) range for a partition key.
        
        Args:
            key: Partition key
            
        Returns:
            Tuple of (start_timestamp_ms, end_timestamp_ms)
        """
        pass
    
    @abstractmethod
    def get_file_suffix(self, key: Union[int, str]) -> str:
        """Get file suffix for partition key.
        
        Args:
            key: Partition key
            
        Returns:
            File suffix like '1705276800000.sqlite3' or '20240115.sqlite3'
        """
        pass
    
    def to_meta(self) -> Dict[str, Any]:
        """Serialize partition config to metadata dict.
        
        Returns:
            Dictionary suitable for JSON storage
        """
        return {
            'type': self.PARTITION_TYPE,
            'ts_col': self.TS_COL,
        }
    
    @classmethod
    def from_meta(cls, meta: Dict[str, Any]) -> 'Partition':
        """Reconstruct partition from metadata.
        
        Args:
            meta: Metadata dictionary
            
        Returns:
            Partition instance
        """
        from .duration import DurationPartition
        from .calendar import CalendarPartition
        from .range import RangePartition
        
        partition_type = meta.get('type')
        if partition_type == 'duration':
            return DurationPartition.from_meta(meta)
        elif partition_type == 'range':
            return RangePartition.from_meta(meta)
        elif partition_type == 'calendar':
            return CalendarPartition.from_meta(meta)
        else:
            raise ValueError(f"Unknown partition type: {partition_type}")
