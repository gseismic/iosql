import math
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
from .partition.duration import parse_interval_ms


DEFAULT_LOGICAL_PART = "__all__"


_PARTITION_KINDS = {"duration", "calendar", "range"}

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
    return parse_interval_ms(value)


def _to_int_every(value: Any, label: str = "range partition width") -> int:
    if isinstance(value, bool):
        raise PartitionSpecError(f"{label} must be an integer, got bool `{value}`")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise PartitionSpecError(
                f"{label} must be an integer, got `{value}`"
            )
        number = int(value)
    else:
        try:
            number = int(str(value).strip())
        except (ValueError, TypeError) as e:
            raise PartitionSpecError(
                f"{label} must be an integer, got `{value}`"
            ) from e
    if number <= 0:
        raise PartitionSpecError(f"{label} must be positive, got `{value}`")
    return number


def _calendar_fmt(every: Any) -> str:
    try:
        return CalendarPartition.format_for_every(every)
    except (ValueError, PartitionSpecError) as e:
        raise PartitionSpecError(f"Unsupported calendar partition `{every}`") from e


def _calendar_every_from_fmt(fmt: Optional[str]) -> Optional[str]:
    if fmt == "YYYY":
        return "1year"
    if fmt == "YYYYMM":
        return "1month"
    if fmt == "YYYYMMDD":
        return "1day"
    if fmt == "YYYYMMDDHH":
        return "1hour"
    if fmt == "YYYYMMDDHHmm":
        return "1minute"
    return None


_DURATION_MS_TO_ALIAS = {
    60_000: "1min",
    300_000: "5min",
    900_000: "15min",
    1_800_000: "30min",
    3_600_000: "1hour",
    14_400_000: "4hour",
    86_400_000: "1day",
    604_800_000: "1week",
    2_592_000_000: "1month",
    5_184_000_000: "2month",
    7_776_000_000: "3month",
    15_552_000_000: "6month",
    31_536_000_000: "1year",
    63_072_000_000: "2year",
    157_680_000_000: "5year",
    315_360_000_000: "10year",
}


def _split_partition_expr_args(body: str) -> List[str]:
    args: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    escape = False

    for char in body:
        if escape:
            current.append(char)
            escape = False
            continue
        if quote:
            if char == "\\":
                escape = True
                continue
            if char == quote:
                quote = None
            current.append(char)
            continue
        if char in ["'", '"']:
            quote = char
            current.append(char)
            continue
        if char == ",":
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    if quote:
        raise PartitionSpecError("Unclosed quote in partition expression")

    args.append("".join(current).strip())
    return args


def _unquote_partition_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ["'", '"']:
        return text[1:-1]
    return text


def _validate_partition_token(name: str, label: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise PartitionSpecError(f"partition {label} must not be empty")
    if any(char in value for char in ["(", ")", ",", "="]):
        raise PartitionSpecError(f"Bad partition {label} `{name}`")
    if any(char in value for char in ["'", '"', "\\"]):
        # 含引号/反斜杠的列名无法在 partition 表达式里安全往返，直接拒绝
        raise PartitionSpecError(
            f"partition {label} `{name}` 不能包含引号或反斜杠；"
            "修复: 改用普通列名"
        )
    return value


def _parse_partition_expr_options(kind: str, tokens: List[str]) -> tuple[str, Dict[str, str]]:
    if not tokens or not tokens[0]:
        raise PartitionSpecError("partition expression requires a column")
    column = _validate_partition_token(_unquote_partition_value(tokens[0]), "column")
    options: Dict[str, str] = {}
    for token in tokens[1:]:
        if not token:
            continue
        if "=" not in token:
            raise PartitionSpecError(f"Bad partition option `{token}`")
        key, raw_value = token.split("=", 1)
        option_key = key.strip().lower()
        if option_key == "tz":
            option_key = "timezone"
        if option_key in options:
            raise PartitionSpecError(f"Duplicate partition option `{key.strip()}`")
        value = _unquote_partition_value(raw_value)
        if value == "":
            raise PartitionSpecError(f"partition option `{key.strip()}` must not be empty")
        options[option_key] = value

    allowed = {
        "duration": {"every"},
        "calendar": {"every", "timezone", "fmt"},
        "range": {"width"},
    }[kind]
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise PartitionSpecError(
            f"Unsupported option(s) for {kind} partition: {', '.join(unknown)}"
        )
    return column, options


def _parse_partition_expr(text: str) -> "PartitionSpec":
    expr = str(text or "").strip()
    open_at = expr.find("(")
    if open_at <= 0 or not expr.endswith(")"):
        raise PartitionSpecError(f"Bad partition expression `{text}`")

    kind = expr[:open_at].strip().lower()
    if kind not in _PARTITION_KINDS:
        raise PartitionSpecError(f"Unsupported partition kind `{kind}`")

    body = expr[open_at + 1:-1].strip()
    tokens = _split_partition_expr_args(body)
    column, options = _parse_partition_expr_options(kind, tokens)

    if kind == "range":
        if "width" not in options:
            raise PartitionSpecError("range partition requires width")
        return PartitionSpec.range(column, _to_int_every(options["width"]))

    if kind == "calendar":
        return PartitionSpec(
            column=column,
            every=options.get("every", "1day"),
            kind="calendar",
            timezone=options.get("timezone"),
            fmt=options.get("fmt"),
        )

    every: Any = options.get("every", "1day")
    if isinstance(every, str) and every.isdigit():
        every = int(every)
    return PartitionSpec(column=column, every=every, kind="duration", time_unit="ms")


def _format_duration_every(every: Any) -> str:
    if isinstance(every, bool):
        every = str(every)
    elif isinstance(every, float) and math.isfinite(every) and every.is_integer():
        # 整数值 float 归一为 int，保证输出可被 parse_interval_ms 回读
        every = int(every)
    if isinstance(every, int):
        alias = _DURATION_MS_TO_ALIAS.get(every)
        if alias:
            return alias
        for scale, unit in [
            (31_536_000_000, "year"),
            (2_592_000_000, "month"),
            (604_800_000, "week"),
            (86_400_000, "day"),
            (3_600_000, "hour"),
            (60_000, "min"),
            (1_000, "s"),
        ]:
            if every % scale == 0:
                return f"{every // scale}{unit}"
        return f"{every}ms"
    text = str(every).strip()
    if text.isdigit():
        value = int(text)
        return _format_duration_every(value)
    try:
        return _format_duration_every(_duration_to_ms(text))
    except PartitionSpecError:
        pass
    return text


def _format_partition_value(value: Any) -> str:
    text = str(value)
    if any(char in text for char in ["'", '"', "\\"]):
        # 当前转义规则无法保证 format→parse 往返，拒绝而不是输出坏表达式
        raise PartitionSpecError(
            f"partition 值 `{text}` 含引号或反斜杠，无法安全格式化；"
            "修复: 改用不含引号的列名/时区"
        )
    if any(char in text for char in [",", "(", ")", "=", " "]):
        return "'" + text + "'"
    return text


def parse_partition_spec(value: Any) -> "PartitionSpec":
    """把用户可见的 partition 表示规范化为 PartitionSpec。"""
    if isinstance(value, PartitionSpec):
        return value
    if isinstance(value, RuntimePartition):
        spec = PartitionSpec.from_partition_meta(value.to_meta())
        if spec is None:
            raise PartitionSpecError(f"Unsupported partition `{value}`")
        return spec
    if isinstance(value, dict):
        return PartitionSpec.from_dict(value)
    if isinstance(value, str):
        return PartitionSpec.from_string(value)
    raise PartitionSpecError(f"Bad partition spec `{value}`")


def format_partition_spec(value: Any) -> str:
    """输出可复制到 CLI 或 Python API 的规范 partition 字符串。"""
    spec = parse_partition_spec(value)
    if spec.kind == "range":
        return (
            f"range({_format_partition_value(spec.column)},"
            f"width={_format_partition_value(_to_int_every(spec.every))})"
        )
    if spec.kind == "calendar":
        every = spec.every or _calendar_every_from_fmt(spec.fmt) or "1day"
        parts = [
            _format_partition_value(spec.column),
            f"every={_format_partition_value(every)}",
        ]
        if spec.timezone:
            # 规范输出用 timezone=（与对象 API 一致）；解析层 tz= 仍兼容
            parts.append(f"timezone={_format_partition_value(spec.timezone)}")
        if spec.fmt and _calendar_every_from_fmt(spec.fmt) is None:
            parts.append(f"fmt={_format_partition_value(spec.fmt)}")
        return f"calendar({','.join(parts)})"
    if spec.kind == "duration":
        return (
            f"duration({_format_partition_value(spec.column)},"
            f"every={_format_partition_value(_format_duration_every(spec.every))})"
        )
    raise PartitionSpecError(f"Unsupported partition kind `{spec.kind}`")


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
            # date 语义必须先按契约时区换算，否则 UTC 晚间的值在
            # Asia/Shanghai 下会被归一成前一天
            return arrow.get(value).to(self.timezone).format("YYYY-MM-DD")
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
        if not isinstance(data, dict):
            raise BadArgument(f"Bad time column spec `{data}`")
        name = data.get("name")
        if not name:
            raise BadArgument(
                f"time column spec 缺少 name: {data!r}；修复: 补全 time 列定义"
            )
        return cls(
            name=name,
            unit=data.get("unit", "ms"),
            timezone=data.get("timezone", "UTC"),
        )


@dataclass
class PhysicalLayout:
    target_rows_per_file: int = 400_000
    min_rows_per_file: int = 300_000
    max_rows_per_file: int = 500_000

    def __post_init__(self):
        self.target_rows_per_file = self._positive_rows(
            self.target_rows_per_file, "target_rows_per_file"
        )
        self.min_rows_per_file = self._positive_rows(
            self.min_rows_per_file, "min_rows_per_file"
        )
        self.max_rows_per_file = self._positive_rows(
            self.max_rows_per_file, "max_rows_per_file"
        )
        if self.max_rows_per_file < self.target_rows_per_file:
            raise BadArgument("max_rows_per_file must be >= target_rows_per_file")
        if self.target_rows_per_file < self.min_rows_per_file:
            raise BadArgument("target_rows_per_file must be >= min_rows_per_file")

    @staticmethod
    def _positive_rows(value: Any, label: str) -> int:
        """显式校验行数配置；None/非法类型给出 BadArgument 而不是 TypeError。"""
        if isinstance(value, bool):
            raise BadArgument(f"{label} must be a positive integer, got bool `{value}`")
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise BadArgument(
                    f"{label} must be an integer, got `{value}`"
                )
            value = int(value)
        elif not isinstance(value, int):
            try:
                value = int(str(value).strip())
            except (ValueError, TypeError) as e:
                raise BadArgument(
                    f"{label} must be a positive integer, got `{value}`"
                ) from e
        if value <= 0:
            raise BadArgument(f"{label} must be positive, got `{value}`")
        return value

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
    stats_columns: Optional[List[str]] = None

    def __post_init__(self):
        self.kind = str(self.kind or "rows").lower()
        if self.kind != "rows":
            raise BadArgument(f"Unsupported segment kind `{self.kind}`")
        self.max_rows = self._positive_rows(self.max_rows, "segment max_rows")
        # target/min 显式传入时必须校验；None 才回落默认值，
        # 避免 target_rows=0 被 `or` 静默替换成 max_rows
        if self.target_rows is None:
            self.target_rows = self.max_rows
        else:
            self.target_rows = self._positive_rows(
                self.target_rows, "segment target_rows"
            )
        if self.min_rows is None:
            self.min_rows = min(self.target_rows, self.max_rows)
        else:
            self.min_rows = self._positive_rows(
                self.min_rows, "segment min_rows"
            )
        if self.stats_columns is not None:
            if isinstance(self.stats_columns, str):
                self.stats_columns = [self.stats_columns]
            self.stats_columns = list(dict.fromkeys(
                str(column).strip() for column in self.stats_columns
            ))
            if any(not column for column in self.stats_columns):
                raise BadArgument("segment stats_columns must not contain empty names")
        if self.target_rows > self.max_rows:
            raise BadArgument("segment target_rows must be <= max_rows")
        if self.min_rows > self.target_rows:
            raise BadArgument("segment min_rows must be <= target_rows")

    @staticmethod
    def _positive_rows(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise BadArgument(f"{label} must be a positive integer, got bool `{value}`")
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise BadArgument(f"{label} must be an integer, got `{value}`")
            value = int(value)
        elif not isinstance(value, int):
            try:
                value = int(str(value).strip())
            except (ValueError, TypeError) as e:
                raise BadArgument(
                    f"{label} must be a positive integer, got `{value}`"
                ) from e
        if value <= 0:
            raise BadArgument(f"{label} must be positive, got `{value}`")
        return value

    @classmethod
    def rows(cls, max_rows: int = 500_000, target_rows: Optional[int] = None,
             min_rows: Optional[int] = None,
             stats_columns: Optional[List[str]] = None) -> "Segment":
        return cls(
            kind="rows",
            max_rows=max_rows,
            target_rows=target_rows,
            min_rows=min_rows,
            stats_columns=stats_columns,
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
            "stats_columns": (
                list(self.stats_columns)
                if self.stats_columns is not None
                else None
            ),
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
            stats_columns=data.get("stats_columns"),
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
        self.column = str(self.column or "").strip()
        if not self.column:
            raise PartitionSpecError("partition column must not be empty")
        _validate_partition_token(self.column, "column")
        self.kind = str(self.kind or "duration").lower()
        if self.kind not in _PARTITION_KINDS:
            raise PartitionSpecError(
                f"Unsupported partition kind `{self.kind}`；可选: "
                f"{', '.join(sorted(_PARTITION_KINDS))}"
            )
        self.time_unit = _normalize_time_unit(self.time_unit)
        if self.timezone is not None:
            self.timezone = str(self.timezone)
        if self.fmt is not None:
            self.fmt = str(self.fmt)
            if self.kind == "calendar" and self.fmt not in CalendarPartition.FMT_SHIFT_MAP:
                raise PartitionSpecError(
                    f"不支持的 calendar fmt `{self.fmt}`；可选: "
                    f"{', '.join(CalendarPartition.FMT_SHIFT_MAP)}。"
                    "修复: 更新 manifest 中的 fmt，或删除该表 partition 元数据后重建"
                )
        # every 按 kind 统一在构造期校验，避免 KeyError/ValueError 泄漏到调用方
        if self.kind == "range":
            self.every = _to_int_every(self.every)
        elif self.kind == "calendar":
            if self.every is not None:
                _calendar_fmt(self.every)
            elif self.fmt is None:
                raise PartitionSpecError(
                    "calendar partition requires `every` or `fmt`"
                )
        else:
            _duration_to_ms(self.every)

    def with_time(self, time: Optional[TimeColumn]) -> "PartitionSpec":
        if time is None:
            return self
        if self.kind == "range":
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
            width = _to_int_every(spec.every)
            return RangePartition(width=width, column=spec.column)
        if spec.kind == "calendar" or unit == "date":
            fmt = spec.fmt or _calendar_fmt(spec.every)
            return CalendarPartition(
                fmt=fmt,
                tz=spec.timezone or "UTC",
                ts_col=spec.column,
                every=spec.every,
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
    def from_string(cls, text: str) -> "PartitionSpec":
        return _parse_partition_expr(text)

    def to_string(self) -> str:
        return format_partition_spec(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PartitionSpec":
        if not isinstance(data, dict):
            raise PartitionSpecError(
                f"Bad partition spec `{data}`；修复: 检查 manifest partition 字段"
            )
        column = data.get("column")
        if not column:
            raise PartitionSpecError(
                f"partition spec 缺少 column: {data!r}；"
                "修复: 补全 partition.column 或删除 partition 元数据后重建"
            )
        return cls(
            column=column,
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
            interval_ms = meta.get("interval_ms")
            ts_col = meta.get("ts_col")
            if interval_ms is None or not ts_col:
                raise PartitionSpecError(
                    f"duration partition meta 缺少 interval_ms/ts_col: {meta!r}；"
                    "修复: 更新 manifest 中的 partition 元数据，"
                    "或删除 partition 后按原契约重建该表"
                )
            return cls(
                column=ts_col,
                every=interval_ms,
                kind="duration",
                time_unit="ms",
            )
        if partition_type == "range":
            width = meta.get("width")
            ts_col = meta.get("ts_col")
            if width is None or not ts_col:
                raise PartitionSpecError(
                    f"range partition meta 缺少 width/ts_col: {meta!r}；"
                    "修复: 更新 manifest 中的 partition 元数据，"
                    "或删除 partition 后按原契约重建该表"
                )
            return cls(
                column=ts_col,
                every=width,
                kind="range",
            )
        if partition_type == "calendar":
            fmt = meta.get("fmt")
            ts_col = meta.get("ts_col")
            if not fmt or not ts_col:
                raise PartitionSpecError(
                    f"calendar partition meta 缺少 fmt/ts_col: {meta!r}；"
                    "修复: 更新 manifest 中的 partition 元数据，"
                    "或删除 partition 后按原契约重建该表"
                )
            return cls(
                column=ts_col,
                every=meta.get("every") or _calendar_every_from_fmt(fmt) or "1day",
                kind="calendar",
                time_unit="date",
                timezone=meta.get("tz", "UTC"),
                fmt=fmt,
            )
        raise PartitionSpecError(
            f"Unknown partition type `{partition_type}`；"
            "修复: 更新 manifest 中的 partition.type，"
            "或删除 partition 后按原契约重建该表"
        )


@dataclass
class TableContract:
    primary_key: List[str] = field(default_factory=list)
    time: Optional[TimeColumn] = None
    partition: Optional[PartitionSpec] = None
    segment: Optional[Segment] = None
    columns: Dict[str, Any] = field(default_factory=dict)
    schema_policy: str = "evolve"
    order_key: Optional[str] = None
    conflict: str = "append"
    symbol_column: Optional[str] = None
    session_column: Optional[str] = None
    physical_layout: PhysicalLayout = field(default_factory=PhysicalLayout)
    version: int = 1
    force_partition: bool = False

    def __post_init__(self):
        if isinstance(self.primary_key, str):
            self.primary_key = [self.primary_key]
        else:
            self.primary_key = list(self.primary_key or [])
        time_from_bare_str = False
        if isinstance(self.time, str):
            self.time = TimeColumn(self.time)
            time_from_bare_str = True
        if isinstance(self.time, dict):
            self.time = TimeColumn.from_dict(self.time)
        if isinstance(self.partition, RuntimePartition):
            self.partition = PartitionSpec.from_partition_meta(self.partition.to_meta())
        if isinstance(self.partition, str):
            self.partition = PartitionSpec.from_string(self.partition)
        if isinstance(self.partition, dict):
            self.partition = PartitionSpec.from_dict(self.partition)
        if (
            self.partition
            and self.time is None
            and self.partition.kind in ('duration', 'calendar')
        ):
            # duration/calendar 分区列即时间语义，反推 time 契约；
            # range 分区列不一定是时间，明确不反推
            self.time = TimeColumn(
                self.partition.column,
                unit=(
                    'date'
                    if self.partition.kind == 'calendar'
                    else (self.partition.time_unit or 'ms')
                ),
                timezone=(
                    self.partition.timezone
                    if self.partition.kind == 'calendar'
                    else 'UTC'
                ),
            )
        elif (
            time_from_bare_str
            and self.partition
            and self.partition.kind in ('duration', 'calendar')
            and self.time.name == self.partition.column
        ):
            # 裸字符串 time 只指定列名，unit/tz 以分区语义为准，
            # 与上方 time is None 的反推规则保持一致；
            # 列名与分区列不同时不做假设，保留 TimeColumn 默认值
            self.time = TimeColumn(
                self.time.name,
                unit=(
                    'date'
                    if self.partition.kind == 'calendar'
                    else (self.partition.time_unit or 'ms')
                ),
                timezone=(
                    self.partition.timezone
                    if self.partition.kind == 'calendar'
                    else 'UTC'
                ),
            )
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

    def with_partition(self, partition: Any = None, every: Any = None,
                       kind: Optional[str] = None,
                       timezone: Optional[str] = None,
                       fmt: Optional[str] = None) -> "TableSpec":
        if partition is not None:
            return replace(
                self,
                partition=parse_partition_spec(partition).with_time(self.time),
            )
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
            "force_partition": self.force_partition,
        }
        if self.time is not None:
            data["time"] = self.time.to_dict()
        if self.symbol_column is not None:
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
            symbol_column=data.get("symbol_column"),
            session_column=data.get("session_column"),
            physical_layout=data.get("physical_layout") or {},
            version=data.get("version", 1),
            force_partition=data.get("force_partition", False),
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
        # 与 __post_init__ 的反推规则保持一致：range 分区列不一定是时间，不反推
        time = None
        if partition and partition.column and partition.kind in ("duration", "calendar"):
            time = TimeColumn(
                partition.column,
                unit=(
                    "date"
                    if partition.kind == "calendar"
                    else (partition.time_unit or "ms")
                ),
                timezone=partition.timezone if partition.kind == "calendar" else "UTC",
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
            symbol_column=None,
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
    state: str = "active"
    created_at: Optional[str] = None

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
            "state": self.state,
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
class CompactResult:
    table: str
    partitions: List[Any] = field(default_factory=list)
    rows_read: int = 0
    rows_written: int = 0
    old_segments: int = 0
    new_segments: int = 0
    obsolete_segments: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "partitions": list(self.partitions),
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "old_segments": self.old_segments,
            "new_segments": self.new_segments,
            "obsolete_segments": self.obsolete_segments,
        }


@dataclass
class ValidationIssue:
    severity: str
    code: str
    message: str
    logical_part: Optional[Any] = None
    segment_id: Optional[int] = None
    part_key: Optional[str] = None
    path: Optional[str] = None
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "logical_part": self.logical_part,
            "segment_id": self.segment_id,
            "part_key": self.part_key,
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
        }
        return {key: value for key, value in data.items() if value is not None}


@dataclass
class SegmentValidationResult:
    table: str
    deep: bool = False
    include_obsolete: bool = False
    checked_segments: int = 0
    errors: List[ValidationIssue] = field(default_factory=list)
    warnings: List[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, code: str, message: str, **kwargs) -> None:
        self.errors.append(
            ValidationIssue(
                severity="error",
                code=code,
                message=message,
                **kwargs,
            )
        )

    def add_warning(self, code: str, message: str, **kwargs) -> None:
        self.warnings.append(
            ValidationIssue(
                severity="warning",
                code=code,
                message=message,
                **kwargs,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "deep": self.deep,
            "include_obsolete": self.include_obsolete,
            "ok": self.ok,
            "checked_segments": self.checked_segments,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


@dataclass
class CleanupResult:
    table: str
    dry_run: bool = True
    partitions: List[Any] = field(default_factory=list)
    candidate_segments: int = 0
    files: List[str] = field(default_factory=list)
    files_deleted: int = 0
    bytes_reclaimable: int = 0
    bytes_deleted: int = 0
    manifest_rows_deleted: int = 0
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "dry_run": self.dry_run,
            "partitions": list(self.partitions),
            "candidate_segments": self.candidate_segments,
            "files": list(self.files),
            "files_deleted": self.files_deleted,
            "bytes_reclaimable": self.bytes_reclaimable,
            "bytes_deleted": self.bytes_deleted,
            "manifest_rows_deleted": self.manifest_rows_deleted,
            "skipped": list(self.skipped),
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


@dataclass
class TableMigrationResult:
    table: str
    target_table: Optional[str] = None
    source_uri: Optional[str] = None
    target_uri: Optional[str] = None
    rows_copied: int = 0
    batches: int = 0
    source_rows: Optional[int] = None
    target_rows: Optional[int] = None
    verify: Optional[str] = None
    verified: Optional[bool] = None
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "target_table": self.target_table or self.table,
            "source_uri": self.source_uri,
            "target_uri": self.target_uri,
            "rows_copied": self.rows_copied,
            "batches": self.batches,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "verify": self.verify,
            "verified": self.verified,
            "dry_run": self.dry_run,
        }


@dataclass
class MigrationResult:
    source_uri: str
    target_uri: str
    tables: List[TableMigrationResult] = field(default_factory=list)
    verify: Optional[str] = None
    dry_run: bool = False

    @property
    def rows_copied(self) -> int:
        return sum(item.rows_copied for item in self.tables)

    @property
    def batches(self) -> int:
        return sum(item.batches for item in self.tables)

    @property
    def source_rows(self) -> Optional[int]:
        if any(item.source_rows is None for item in self.tables):
            return None
        return sum(item.source_rows or 0 for item in self.tables)

    @property
    def target_rows(self) -> Optional[int]:
        if any(item.target_rows is None for item in self.tables):
            return None
        return sum(item.target_rows or 0 for item in self.tables)

    @property
    def verified(self) -> Optional[bool]:
        values = [item.verified for item in self.tables if item.verified is not None]
        if not values:
            return None
        return all(values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "target_uri": self.target_uri,
            "rows_copied": self.rows_copied,
            "batches": self.batches,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "verify": self.verify,
            "verified": self.verified,
            "dry_run": self.dry_run,
            "tables": [item.to_dict() for item in self.tables],
        }
