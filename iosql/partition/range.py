from typing import Any, Dict

from .base import Partition


class RangePartition(Partition):
    """Fixed-width numeric partitioning for any sortable numeric column."""

    PARTITION_TYPE = 'range'

    def __init__(self, width: int, column: str):
        width = int(width)
        if width <= 0:
            raise ValueError('range partition width must be positive')
        self.width = width
        self.TS_COL = str(column)

    def get_key(self, row: Dict[str, Any]) -> int:
        value = row[self.TS_COL]
        return (value // self.width) * self.width

    def get_range(self, key: int) -> tuple[int, int]:  # type: ignore[override]
        return (key, key + self.width)

    def get_file_suffix(self, key: int) -> str:  # type: ignore[override]
        return f'{key}.sqlite3'

    def to_meta(self) -> Dict[str, Any]:
        return {
            'type': self.PARTITION_TYPE,
            'ts_col': self.TS_COL,
            'width': self.width,
        }

    @classmethod
    def from_meta(cls, meta: Dict[str, Any]) -> 'RangePartition':
        return cls(width=meta['width'], column=meta['ts_col'])
