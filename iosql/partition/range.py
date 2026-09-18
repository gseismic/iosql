import math
from typing import Any, Dict

from .base import Partition, normalize_part_value
from ..exception import PartitionSpecError


class RangePartition(Partition):
    """Fixed-width numeric partitioning for any sortable numeric column."""

    PARTITION_TYPE = 'range'

    def __init__(self, width: int, column: str):
        if isinstance(width, bool):
            raise PartitionSpecError(
                f'range partition width must be a positive integer, got bool `{width}`'
            )
        if isinstance(width, float):
            if not math.isfinite(width) or not width.is_integer():
                raise PartitionSpecError(
                    'range partition width must be an integer, '
                    f'got `{width}`'
                )
            width = int(width)
        elif not isinstance(width, int):
            try:
                width = int(str(width).strip())
            except (ValueError, TypeError) as e:
                raise PartitionSpecError(
                    f'range partition width must be an integer, got `{width}`'
                ) from e
        if width <= 0:
            raise PartitionSpecError('range partition width must be positive')
        self.width = width
        self.TS_COL = str(column)

    def get_key(self, row: Dict[str, Any]) -> int:
        value = normalize_part_value(
            row[self.TS_COL], f"range partition field `{self.TS_COL}`"
        )
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
        missing = [key for key in ('width', 'ts_col') if meta.get(key) is None]
        if missing:
            raise PartitionSpecError(
                f"range partition meta 缺少字段 {missing} (meta={meta!r})；"
                "修复: 更新 manifest 中的 partition 元数据，"
                "或删除 partition 后按原契约重建该表"
            )
        return cls(width=meta['width'], column=meta['ts_col'])
