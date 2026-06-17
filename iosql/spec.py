from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional

import arrow

from .exception import BadArgument, PartitionSpecError
from .partition import (
    CalendarPartition,
    DurationPartition,
    RangePartition,
    Partition as RuntimePartition,
)


DEFAULT_LOGICAL_PART = "__all__"


_TIME_UNIT_ALIASES = {
    "s": "s",
    "sec": "s",
    "second": "s",
    "seconds": "s",
    "ms": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
    "date": "date",
}


def _normalize_time_unit(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    key = str(unit).strip().lower()
    try:
        return _TIME_UNIT_ALIASES[key]
    except KeyError as e:
        raise PartitionSpecError(f"Unsupported time unit `{unit}`") from e


def _duration_to_ms(value: Any) -> int:
    if isinstance(value, int):
        if value <= 0:
            raise PartitionSpecError("partition duration must be positive")
        return value

    text = str(value).strip().lower()
    if text in DurationPartition.INTERVAL_ALIASES:
        return DurationPartition.INTERVAL_ALIASES[text]

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


def _calendar_fmt(every: Any) -> str:
    text = str(every).strip().lower()
    if text in ["1y", "1year", "1years", "year", "years"]:
        return "YYYY"
    if text in ["1mo", "1mon", "1month", "1months", "month", "months"]:
        return "YYYYMM"
    if text in ["1d", "1day", "1days", "day", "days"]:
        return "YYYYMMDD"
    if text in ["1h", "1hour", "1hours", "hour", "hours"]:
        return "YYYYMMDDHH"
    raise PartitionSpecError(f"Unsupported calendar partition `{every}`")


@dataclass
class TimeColumn:
    name: str
    unit: str = "ms"
    timezone: str = "UTC"

    def __post_init__(self):
        self.name = str(self.name)
        self.unit = _normalize_time_unit(self.unit) or "ms"
        self.timezone = str(self.timezone or "UTC")

    def normalize_value(self, value: Any) -> Any:
        if value is None:
            return None
        if self.unit == "date":
            return arrow.get(value).format("YYYY-MM-DD")
        if isinstance(value, (int, float)):
            return value

        timestamp = arrow.get(value).to(self.timezone).timestamp()
        if self.unit == "ms":
            return int(timestamp * 1000)
        if self.unit == "s":
            return int(timestamp)
        raise PartitionSpecError(f"Unsupported time unit `{self.unit}`")

    def normalize_date_range(self, value: Any) -> tuple[Any, Any]:
        if self.unit == "date":
            normalized = self.normalize_value(value)
            return normalized, normalized

        start, end = arrow.get(value).to(self.timezone).span("day")
        return self.normalize_value(start), self.normalize_value(end)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TimeColumn":
        return cls(
            name=data["name"],
            unit=data.get("unit", "ms"),
            timezone=data.get("timezone", "UTC"),
        )


@dataclass
class PhysicalLayout:
    target_rows_per_file: int = 400_000
    min_rows_per_file: int = 300_000
    max_rows_per_file: int = 500_000

    def __post_init__(self):
        self.target_rows_per_file = int(self.target_rows_per_file)
        self.min_rows_per_file = int(self.min_rows_per_file)
        self.max_rows_per_file = int(self.max_rows_per_file)
        if self.min_rows_per_file <= 0:
            raise BadArgument("min_rows_per_file must be positive")
        if self.target_rows_per_file <= 0:
            raise BadArgument("target_rows_per_file must be positive")
        if self.max_rows_per_file < self.target_rows_per_file:
            raise BadArgument("max_rows_per_file must be >= target_rows_per_file")
        if self.target_rows_per_file < self.min_rows_per_file:
            raise BadArgument("target_rows_per_file must be >= min_rows_per_file")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_rows_per_file": self.target_rows_per_file,
            "min_rows_per_file": self.min_rows_per_file,
            "max_rows_per_file": self.max_rows_per_file,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PhysicalLayout":
        if not data:
            return cls()
        return cls(
            target_rows_per_file=data.get("target_rows_per_file", 400_000),
            min_rows_per_file=data.get("min_rows_per_file", 300_000),
            max_rows_per_file=data.get("max_rows_per_file", 500_000),
        )


@dataclass
class Segment:
    kind: str = "rows"
    max_rows: int = 500_000
    target_rows: Optional[int] = None
    min_rows: Optional[int] = None

    def __post_init__(self):
        self.kind = str(self.kind or "rows").lower()
        if self.kind != "rows":
            raise BadArgument(f"Unsupported segment kind `{self.kind}`")
        self.max_rows = int(self.max_rows)
        self.target_rows = int(self.target_rows or self.max_rows)
        self.min_rows = int(self.min_rows or min(self.target_rows, self.max_rows))
        if self.max_rows <= 0:
            raise BadArgument("segment max_rows must be positive")
        if self.target_rows <= 0:
            raise BadArgument("segment target_rows must be positive")
        if self.min_rows <= 0:
            raise BadArgument("segment min_rows must be positive")
        if self.target_rows > self.max_rows:
            raise BadArgument("segment target_rows must be <= max_rows")
        if self.min_rows > self.target_rows:
            raise BadArgument("segment min_rows must be <= target_rows")

    @classmethod
    def rows(cls, max_rows: int = 500_000, target_rows: Optional[int] = None,
             min_rows: Optional[int] = None) -> "Segment":
        return cls(
            kind="rows",
            max_rows=max_rows,
            target_rows=target_rows,
            min_rows=min_rows,
        )

    def to_physical_layout(self) -> PhysicalLayout:
        return PhysicalLayout(
            target_rows_per_file=self.target_rows,
            min_rows_per_file=self.min_rows,
            max_rows_per_file=self.max_rows,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "max_rows": self.max_rows,
            "target_rows": self.target_rows,
            "min_rows": self.min_rows,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["Segment"]:
        if not data:
            return None
        return cls(
            kind=data.get("kind", "rows"),
            max_rows=data.get("max_rows", data.get("max_rows_per_file", 500_000)),
            target_rows=data.get("target_rows", data.get("target_rows_per_file")),
            min_rows=data.get("min_rows", data.get("min_rows_per_file")),
        )


@dataclass
class PartitionSpec:
    column: str
    every: Any = "1day"
    kind: str = "duration"
    time_unit: Optional[str] = None
    timezone: Optional[str] = None
    fmt: Optional[str] = None

    def __post_init__(self):
        self.column = str(self.column)
        self.kind = str(self.kind or "duration").lower()
        self.time_unit = _normalize_time_unit(self.time_unit)
        if self.timezone is not None:
            self.timezone = str(self.timezone)
        if self.fmt is not None:
            self.fmt = str(self.fmt)

    def with_time(self, time: Optional[TimeColumn]) -> "PartitionSpec":
        if time is None:
            return self
        return replace(
            self,
            time_unit=self.time_unit or time.unit,
            timezone=self.timezone or time.timezone,
        )

    def to_partition(self, time: Optional[TimeColumn] = None) -> RuntimePartition:
        spec = self.with_time(time)
        unit = spec.time_unit
        if spec.kind == "range":
            return RangePartition(width=spec.every, column=spec.column)
        if spec.kind == "calendar" or unit == "date":
            fmt = spec.fmt or _calendar_fmt(spec.every)
            return CalendarPartition(
                fmt=fmt,
                tz=spec.timezone or "UTC",
                ts_col=spec.column,
            )
        if unit not in [None, "ms"]:
            raise PartitionSpecError(
                "duration partitions currently require TimeColumn(unit='ms')"
            )
        return DurationPartition(interval_ms=_duration_to_ms(spec.every), ts_col=spec.column)

    def parts_for(self, rows: Iterable[Dict[str, Any]],
                  time: Optional[TimeColumn] = None) -> List[Any]:
        part_obj = self.to_partition(time)
        return sorted({part_obj.get_key(row) for row in rows})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "every": self.every,
            "kind": self.kind,
            "time_unit": self.time_unit,
            "timezone": self.timezone,
            "fmt": self.fmt,
        }

    @classmethod
    def by_time(cls, column: str, every: Any = "1day",
                timezone: Optional[str] = None,
                kind: str = "duration") -> "PartitionSpec":
        return cls(column=column, every=every, timezone=timezone, kind=kind)

    @classmethod
    def range(cls, column: str, every: Any) -> "PartitionSpec":
        return cls(column=column, every=every, kind="range")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PartitionSpec":
        return cls(
            column=data["column"],
            every=data.get("every", "1day"),
            kind=data.get("kind", "duration"),
            time_unit=data.get("time_unit"),
            timezone=data.get("timezone"),
            fmt=data.get("fmt"),
        )

    @classmethod
    def from_partition_meta(cls, meta: Optional[Dict[str, Any]]) -> Optional["PartitionSpec"]:
        if not meta:
            return None
        partition_type = meta.get("type")
        if partition_type == "duration":
            return cls(
                column=meta["ts_col"],
                every=meta["interval_ms"],
                kind="duration",
                time_unit="ms",
            )
        if partition_type == "range":
            return cls(
                column=meta["ts_col"],
                every=meta["width"],
                kind="range",
            )
        if partition_type == "calendar":
            return cls(
                column=meta["ts_col"],
                every="1day",
                kind="calendar",
                time_unit="date",
                timezone=meta.get("tz", "UTC"),
                fmt=meta.get("fmt"),
            )
        return None


@dataclass
class TableContract:
    primary_key: List[str] = field(default_factory=list)
    # time/symbol/session 仅为旧 timeseries helper 过渡字段，不属于核心契约。
    time: Optional[TimeColumn] = None
    partition: Optional[PartitionSpec] = None
    segment: Optional[Segment] = None
    columns: Dict[str, Any] = field(default_factory=dict)
    schema_policy: str = "evolve"
    order_key: Optional[str] = None
    conflict: str = "append"
    symbol_column: str = "symbol"
    session_column: Optional[str] = None
    physical_layout: PhysicalLayout = field(default_factory=PhysicalLayout)
    version: int = 1

    def __post_init__(self):
        if isinstance(self.primary_key, str):
            self.primary_key = [self.primary_key]
        else:
            self.primary_key = list(self.primary_key or [])
        if isinstance(self.time, dict):
            self.time = TimeColumn.from_dict(self.time)
        if isinstance(self.partition, RuntimePartition):
            self.partition = PartitionSpec.from_partition_meta(self.partition.to_meta())
        if isinstance(self.partition, dict):
            self.partition = PartitionSpec.from_dict(self.partition)
        if self.partition and self.time:
            self.partition = self.partition.with_time(self.time)
        if isinstance(self.segment, dict):
            self.segment = Segment.from_dict(self.segment)
        if isinstance(self.physical_layout, dict):
            self.physical_layout = PhysicalLayout.from_dict(self.physical_layout)
        if self.segment is not None:
            self.physical_layout = self.segment.to_physical_layout()
        self.columns = dict(self.columns or {})
        self.schema_policy = str(self.schema_policy or "evolve")
        if self.order_key is not None:
            self.order_key = str(self.order_key)
        self.conflict = str(self.conflict or "append").lower()
        if self.conflict not in ["append", "upsert", "replace", "ignore"]:
            raise BadArgument(f"Unsupported conflict policy `{self.conflict}`")
        if self.session_column is not None:
            self.session_column = str(self.session_column)

    @property
    def pk(self) -> List[str]:
        return self.primary_key

    @classmethod
    def ensure(cls, spec: Any) -> "TableContract":
        if isinstance(spec, cls):
            return spec
        if isinstance(spec, dict):
            return cls.from_dict(spec)
        raise BadArgument(f"Bad table spec `{spec}`")

    @classmethod
    def timeseries(cls, time: str, primary_key: List[str],
                   columns: Optional[Dict[str, Any]] = None,
                   partition_every: Any = "1day",
                   timezone: str = "UTC",
                   schema_policy: str = "evolve",
                   symbol_column: str = "symbol",
                   session_column: Optional[str] = None,
                   target_rows_per_file: int = 400_000,
                   min_rows_per_file: int = 300_000,
                   max_rows_per_file: int = 500_000,
                   time_unit: str = "ms",
                   partition_kind: str = "duration") -> "TableContract":
        time_column = TimeColumn(time, unit=time_unit, timezone=timezone)
        segment = Segment.rows(
            max_rows=max_rows_per_file,
            target_rows=target_rows_per_file,
            min_rows=min_rows_per_file,
        )
        return cls(
            primary_key=primary_key,
            time=time_column,
            partition=PartitionSpec.by_time(
                time,
                every=partition_every,
                timezone=timezone,
                kind=partition_kind,
            ),
            columns=columns or {},
            schema_policy=schema_policy,
            order_key=time,
            segment=segment,
            symbol_column=symbol_column,
            session_column=session_column,
            physical_layout=PhysicalLayout(
                target_rows_per_file=target_rows_per_file,
                min_rows_per_file=min_rows_per_file,
                max_rows_per_file=max_rows_per_file,
            ),
        )

    def with_partition(self, every: Any = None, kind: Optional[str] = None,
                       timezone: Optional[str] = None,
                       fmt: Optional[str] = None) -> "TableSpec":
        if not self.partition:
            raise PartitionSpecError("TableSpec has no partition to update")
        partition = replace(
            self.partition,
            every=every if every is not None else self.partition.every,
            kind=kind if kind is not None else self.partition.kind,
            timezone=timezone if timezone is not None else self.partition.timezone,
            fmt=fmt if fmt is not None else self.partition.fmt,
        )
        return replace(self, partition=partition.with_time(self.time))

    def to_partition(self) -> Optional[RuntimePartition]:
        if not self.partition:
            return None
        return self.partition.to_partition(self.time)

    def parts_for(self, rows: Iterable[Dict[str, Any]]) -> List[Any]:
        rows = list(rows)
        if not rows:
            return []
        partition = self.to_partition()
        if partition is None:
            return [DEFAULT_LOGICAL_PART if self.segment else None]
        return sorted({partition.get_key(row) for row in rows})

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "version": self.version,
            "primary_key": list(self.primary_key),
            "partition": self.partition.to_dict() if self.partition else None,
            "segment": self.segment.to_dict() if self.segment else None,
            "columns": dict(self.columns),
            "schema_policy": self.schema_policy,
            "order_key": self.order_key,
            "conflict": self.conflict,
            "physical_layout": self.physical_layout.to_dict(),
        }
        if self.time is not None:
            data["time"] = self.time.to_dict()
        if self.symbol_column != "symbol":
            data["symbol_column"] = self.symbol_column
        if self.session_column is not None:
            data["session_column"] = self.session_column
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TableSpec":
        return cls(
            primary_key=data.get("primary_key") or data.get("pk") or [],
            time=data.get("time"),
            partition=data.get("partition"),
            segment=data.get("segment"),
            columns=data.get("columns") or {},
            schema_policy=data.get("schema_policy", "evolve"),
            order_key=data.get("order_key"),
            conflict=data.get("conflict", "append"),
            symbol_column=data.get("symbol_column", "symbol"),
            session_column=data.get("session_column"),
            physical_layout=data.get("physical_layout") or {},
            version=data.get("version", 1),
        )

    @classmethod
    def from_table_meta(cls, meta: Optional[Dict[str, Any]]) -> Optional["TableContract"]:
        if not meta:
            return None
        if meta.get("table_contract"):
            return cls.from_dict(meta["table_contract"])
        if meta.get("table_spec"):
            return cls.from_dict(meta["table_spec"])

        partition = PartitionSpec.from_partition_meta(meta.get("partition"))
        time = None
        if partition and partition.column:
            time = TimeColumn(
                partition.column,
                unit=partition.time_unit or "ms",
                timezone=partition.timezone,
            )
        columns = {
            key: item.get("type")
            for key, item in (meta.get("column_info") or {}).items()
        }
        return cls(
            primary_key=meta.get("pk") or [],
            time=time,
            partition=partition,
            segment=Segment.from_dict(meta.get("segment")),
            columns=columns,
            schema_policy="evolve",
            symbol_column="symbol",
            session_column=None,
        )


# 旧名称保留为过渡别名；新核心代码应使用 TableContract。
TableSpec = TableContract


@dataclass
class WriteResult:
    rows_seen: int = 0
    rows_written: Optional[int] = 0
    partitions_touched: List[Any] = field(default_factory=list)
    partitions_created: List[Any] = field(default_factory=list)
    schema_changed: bool = False
    layout_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "partitions_touched": list(self.partitions_touched),
            "partitions_created": list(self.partitions_created),
            "schema_changed": self.schema_changed,
            "layout_warnings": list(self.layout_warnings),
        }


@dataclass
class QuerySpec:
    table: Optional[str] = None
    columns: Any = "*"
    symbols: Optional[Any] = None
    start: Optional[Any] = None
    end: Optional[Any] = None
    date: Optional[Any] = None
    session: Optional[Any] = None
    where: Optional[Any] = None
    order_by: Optional[Any] = None
    limit: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "columns": self.columns,
            "symbols": self.symbols,
            "start": self.start,
            "end": self.end,
            "date": self.date,
            "session": self.session,
            "where": str(self.where) if self.where is not None else None,
            "order_by": self.order_by,
            "limit": self.limit,
        }


@dataclass
class QueryPlan:
    table: Optional[str] = None
    columns: Any = "*"
    where: Optional[Any] = None
    order_by: Optional[Any] = None
    limit: Optional[int] = None
    candidate_partitions: List[Any] = field(default_factory=list)
    scanned_partitions: List[Any] = field(default_factory=list)
    skipped_partitions: List[Any] = field(default_factory=list)
    skip_reasons: Dict[Any, str] = field(default_factory=dict)
    candidate_segments: List[Any] = field(default_factory=list)
    scanned_segments: List[Any] = field(default_factory=list)
    skipped_segments: List[Any] = field(default_factory=list)
    segment_skip_reasons: Dict[Any, str] = field(default_factory=dict)
    requires_global_sort: bool = False
    requires_python_limit: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "columns": self.columns,
            "where": str(self.where) if self.where is not None else None,
            "order_by": self.order_by,
            "limit": self.limit,
            "candidate_partitions": list(self.candidate_partitions),
            "scanned_partitions": list(self.scanned_partitions),
            "skipped_partitions": list(self.skipped_partitions),
            "skip_reasons": dict(self.skip_reasons),
            "candidate_segments": list(self.candidate_segments),
            "scanned_segments": list(self.scanned_segments),
            "skipped_segments": list(self.skipped_segments),
            "segment_skip_reasons": dict(self.segment_skip_reasons),
            "requires_global_sort": self.requires_global_sort,
            "requires_python_limit": self.requires_python_limit,
        }


@dataclass
class SegmentInfo:
    logical_part: Any
    segment_id: int
    part_key: str
    path: str
    row_count: int = 0
    time_min: Optional[Any] = None
    time_max: Optional[Any] = None
    session_min: Optional[Any] = None
    session_max: Optional[Any] = None
    stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sealed: bool = False
    generation: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logical_part": self.logical_part,
            "segment_id": self.segment_id,
            "part_key": self.part_key,
            "path": self.path,
            "row_count": self.row_count,
            "time_min": self.time_min,
            "time_max": self.time_max,
            "session_min": self.session_min,
            "session_max": self.session_max,
            "stats": dict(self.stats or {}),
            "sealed": self.sealed,
            "generation": self.generation,
        }


@dataclass
class PartitionStats:
    partition_id: Any
    path: str
    row_count: int
    time_min: Optional[Any] = None
    time_max: Optional[Any] = None
    oversized: bool = False
    undersized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "path": self.path,
            "row_count": self.row_count,
            "time_min": self.time_min,
            "time_max": self.time_max,
            "oversized": self.oversized,
            "undersized": self.undersized,
        }


@dataclass
class LayoutReport:
    table: str
    target_rows_per_file: int
    min_rows_per_file: int
    max_rows_per_file: int
    partitions: List[PartitionStats] = field(default_factory=list)
    recommendation: str = ""

    @property
    def total_rows(self) -> int:
        return sum(item.row_count for item in self.partitions)

    @property
    def total_partitions(self) -> int:
        return len(self.partitions)

    @property
    def oversized_partitions(self) -> List[Any]:
        return [item.partition_id for item in self.partitions if item.oversized]

    @property
    def undersized_partitions(self) -> List[Any]:
        return [item.partition_id for item in self.partitions if item.undersized]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "target_rows_per_file": self.target_rows_per_file,
            "min_rows_per_file": self.min_rows_per_file,
            "max_rows_per_file": self.max_rows_per_file,
            "total_rows": self.total_rows,
            "total_partitions": self.total_partitions,
            "oversized_partitions": list(self.oversized_partitions),
            "undersized_partitions": list(self.undersized_partitions),
            "recommendation": self.recommendation,
            "partitions": [item.to_dict() for item in self.partitions],
        }


@dataclass
class RepartitionResult:
    source_uri: str
    target_uri: str
    table: str
    rows_copied: int = 0
    partitions_created: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "target_uri": self.target_uri,
            "table": self.table,
            "rows_copied": self.rows_copied,
            "partitions_created": list(self.partitions_created),
        }
