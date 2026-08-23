"""iosql 命令行工具。"""

from __future__ import annotations

import csv
import errno
import io
import json
import os
import shutil
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, NoReturn, Optional

import typer

from iosql import Database, PartitionSpec, format_partition_spec
from iosql.exception import PartitionSpecError
from iosql.utils.mysqlite import Database as MyDatabase


app = typer.Typer(help="iosql SQLite 目录库管理工具")
index_app = typer.Typer(help="索引检查和维护")
app.add_typer(index_app, name="index")

_PART_DATA_TABLE_NAME = "data"
_ROW_FORMATS = {"json", "jsonl", "csv"}
_WRITE_MODES = {"append", "ignore", "upsert", "replace"}


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _parse_uri(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        _fail("数据库路径不能为空")
    if value.startswith(("sqlite://", "sqlite3://")):
        return value
    if "://" in value:
        _fail("CLI 目前只支持 sqlite://、sqlite3:// 或本地路径")
    return f"sqlite://{value}"


def _parse_migration_target_uri(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        _fail("目标 URI 不能为空")
    if value.startswith(("sqlite://", "sqlite3://", "postgresql://")):
        return value
    if "://" in value:
        _fail("migrate 目标只支持 sqlite://、sqlite3:// 或 postgresql://")
    return f"sqlite://{value}"


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("sqlite://"):
        return Path(uri[len("sqlite://"):])
    if uri.startswith("sqlite3://"):
        return Path(uri[len("sqlite3://"):])
    _fail(f"不支持的 SQLite URI: {uri}")
    raise AssertionError("unreachable")


def _require_source(source: str) -> tuple[str, Path]:
    source_uri = _parse_uri(source)
    source_path = _uri_to_path(source_uri)
    if not source_path.exists():
        _fail(f"数据库不存在: {source_path}")
    if not source_path.is_dir():
        _fail(f"iosql SQLite 数据库必须是目录: {source_path}")
    return source_uri, source_path


def _backup_path(source_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return source_path.with_name(f"{source_path.name}.backup_{ts}")


def _is_iosql_like_database(path: Path) -> bool:
    return (path / "meta.sqlite3").is_file()


def _rmtree_robust(path: Path) -> None:
    """递归删除目录树，处理 FUSE 文件系统的 .fuse_hidden 残留文件。

    在 FUSE 挂载的文件系统上，已删除但仍有进程持有的文件会被重命名为
    .fuse_hidden*。这些残留文件会导致 shutil.rmtree 内部 os.rmdir 时
    触发 ENOTEMPTY 错误。
    """
    path_str = str(path)
    for attempt in range(6):
        # 每次重试前清理 .fuse_hidden* 残留文件
        for root_str, dirs, files in os.walk(path_str, topdown=False):
            for name in files:
                if name.startswith(".fuse_hidden"):
                    try:
                        (Path(root_str) / name).unlink()
                    except OSError:
                        pass
        try:
            shutil.rmtree(path_str)
            return
        except OSError as e:
            if e.errno != errno.ENOTEMPTY or attempt == 5:
                raise
            time.sleep(0.5 * (attempt + 1))


def _comma(n: int) -> str:
    return f"{n:,}"


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def _parse_json_or_text(value: str) -> Any:
    if not value:
        return None
    text = value.strip()
    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parse_partition_value(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _require_table(db: Database, table: str):
    if table not in db.table_names():
        _fail(f"表不存在: {table}")
    return db.table(table)


def _normalize_row_format(format_: str, path: Optional[Path] = None) -> str:
    value = str(format_ or "json").strip().lower()
    if value == "auto":
        if path is None:
            _fail("format=auto 需要文件路径")
        suffix = path.suffix.lower()
        if suffix in [".jsonl", ".ndjson"]:
            return "jsonl"
        if suffix == ".json":
            return "json"
        if suffix == ".csv":
            return "csv"
        _fail(f"无法根据扩展名识别格式: {path}")
    if value not in _ROW_FORMATS:
        _fail(f"不支持的格式: {format_}")
    return value


def _normalize_write_mode(mode: str) -> str:
    value = str(mode or "append").strip().lower()
    if value not in _WRITE_MODES:
        _fail(f"不支持的写入模式: {mode}")
    return value


def _normalize_verify_mode(value: Optional[str]) -> Optional[str]:
    if value in [None, ""]:
        return None
    verify = str(value).strip().lower()
    if verify in ["none", "false", "0"]:
        return None
    if verify != "row-count":
        _fail(f"不支持的校验模式: {value}")
    return verify


def _parse_table_mapping(values: List[str]) -> Optional[Any]:
    if not values:
        return None
    mapping = {}
    names = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if ":" in text:
            source, target = [item.strip() for item in text.split(":", 1)]
            if not source or not target:
                _fail(f"无效表映射: {value}")
            mapping[source] = target
        else:
            names.append(text)
    if mapping and names:
        _fail("--table 不能混用普通表名和 source:target 映射")
    return mapping if mapping else names


def _read_rows(path: Path, format_: str) -> List[Dict[str, Any]]:
    return list(_iter_read_rows(path, format_))


def _iter_read_rows(path: Path, format_: str) -> Iterable[Dict[str, Any]]:
    fmt = _normalize_row_format(format_, path)
    if not path.exists():
        _fail(f"输入文件不存在: {path}")

    if fmt == "json":
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            yield data
            return
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            for item in data:
                yield dict(item)
            return
        _fail("JSON 输入必须是对象或对象数组")

    if fmt == "jsonl":
        with path.open("r", encoding="utf-8") as file:
            for line_no, line in enumerate(file, 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    _fail(f"JSONL 第 {line_no} 行不是对象")
                yield item
        return

    with path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            yield dict(row)


def _iter_row_batches(
    path: Path,
    format_: str,
    batch_size: int,
) -> Iterable[List[Dict[str, Any]]]:
    batch: List[Dict[str, Any]] = []
    for row in _iter_read_rows(path, format_):
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _coerce_import_batch(
    batch: List[Dict[str, Any]],
    table_spec,
) -> List[Dict[str, Any]]:
    column_types = dict(getattr(table_spec, "columns", None) or {})
    if not column_types:
        return batch
    return [
        {
            key: _coerce_import_value(value, column_types.get(key), key)
            for key, value in row.items()
        }
        for row in batch
    ]


def _coerce_import_value(value: Any, type_: Any, column: str) -> Any:
    if type_ is None or not isinstance(value, str):
        return value
    if value == "":
        return None

    normalized = str(type_).strip().lower()
    try:
        if normalized in {"integer", "int", "bigint", "smallint"}:
            return int(value)
        if normalized in {"float", "real", "double", "numeric", "decimal"}:
            return float(value)
        if normalized in {"boolean", "bool"}:
            return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    except ValueError as exc:
        _fail(f"字段 {column} 无法转换为 {type_}: {value}")
        raise AssertionError("unreachable") from exc
    return value


def _row_fields(rows: Iterable[Dict[str, Any]]) -> List[str]:
    fields: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fields.append(key)
                seen.add(key)
    return fields


def _rows_to_csv(rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    fields = _row_fields(rows)
    if not fields:
        return ""
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _emit_rows(rows: List[Dict[str, Any]], format_: str) -> None:
    fmt = _normalize_row_format(format_)
    if fmt == "json":
        _echo_json(rows)
        return
    if fmt == "jsonl":
        for row in rows:
            typer.echo(json.dumps(row, ensure_ascii=False, default=_json_default))
        return
    typer.echo(_rows_to_csv(rows), nl=False)


def _write_rows(path: Path, rows: List[Dict[str, Any]], format_: str) -> None:
    fmt = _normalize_row_format(format_, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        return
    if fmt == "jsonl":
        lines = [
            json.dumps(row, ensure_ascii=False, default=_json_default)
            for row in rows
        ]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return
    path.write_text(_rows_to_csv(rows), encoding="utf-8")


def _write_row_batches(
    path: Path,
    batches: Iterable[List[Dict[str, Any]]],
    format_: str,
) -> int:
    fmt = _normalize_row_format(format_, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0

    with path.open("w", encoding="utf-8", newline="") as file:
        if fmt == "json":
            file.write("[\n")
            first = True
            for batch in batches:
                for row in batch:
                    if not first:
                        file.write(",\n")
                    file.write(
                        json.dumps(row, ensure_ascii=False, default=_json_default)
                    )
                    first = False
                    rows_written += 1
            file.write("\n]\n" if rows_written else "]\n")
            return rows_written

        if fmt == "jsonl":
            for batch in batches:
                for row in batch:
                    file.write(
                        json.dumps(row, ensure_ascii=False, default=_json_default)
                    )
                    file.write("\n")
                    rows_written += 1
            return rows_written

        writer = None
        for batch in batches:
            if not batch:
                continue
            if writer is None:
                writer = csv.DictWriter(
                    file,
                    fieldnames=_row_fields(batch),
                    extrasaction="ignore",
                )
                writer.writeheader()
            writer.writerows(batch)
            rows_written += len(batch)
        return rows_written


def _part_db_paths(source_path: Path, table_name: str) -> List[Path]:
    data_dir = source_path / table_name / "data"
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("*.sqlite3"))


def _indexes_for_part(path: Path) -> List[str]:
    db = MyDatabase(str(path), verbose=0)
    try:
        return db.get_table_indices(_PART_DATA_TABLE_NAME, exclude_autoindex=True)
    finally:
        db.close()


def _index_report(source_path: Path, table_name: str) -> List[Dict[str, Any]]:
    report = []
    for path in _part_db_paths(source_path, table_name):
        report.append({
            "path": str(path),
            "indexes": _indexes_for_part(path),
        })
    return report


def _missing_indexes(source_path: Path, table_name: str, pk_columns: List[str]) -> List[Dict[str, Any]]:
    missing = []
    expected = {column: f"{_PART_DATA_TABLE_NAME}_{column}_index" for column in pk_columns}
    for item in _index_report(source_path, table_name):
        indexes = set(item["indexes"])
        missing_columns = [
            column for column, index_name in expected.items()
            if index_name not in indexes
        ]
        if missing_columns:
            missing.append({
                "path": item["path"],
                "missing": missing_columns,
            })
    return missing


def _missing_index_columns(missing: List[Dict[str, Any]]) -> List[str]:
    columns = set()
    for item in missing:
        columns.update(item["missing"])
    return sorted(columns)


def _partition_desc(partition) -> str:
    return format_partition_spec(partition)


def _repartition_hint(
    source: str,
    table: str,
    partition_expr: str,
    hint: str = "",
    indent: str = "    ",
) -> str:
    return (
        f"{indent}→ iosql repartition --source {source} --table {table} "
        f"--partition '{partition_expr}' --replace-source --yes{hint}"
    )


def _suggest_range_every_up(avg_rows: int, target: int, current_every: int = 1) -> int:
    ratio = target / max(avg_rows, 1)
    return max(int(current_every * ratio), current_every + 1)


def _suggest_range_every_down(avg_rows: int, target: int, current_every: int = 1) -> int:
    if current_every <= 1:
        return 1
    ratio = avg_rows / max(target, 1)
    new_every = max(int(current_every / ratio), 1)
    if new_every >= current_every:
        new_every = current_every - 1
    return new_every


def _suggest_period_up(avg_rows: int, target: int) -> str:
    ratio = target / max(avg_rows, 1)
    if ratio >= 24:
        return "2year"
    if ratio >= 8:
        return "3month"
    if ratio >= 2:
        return "1month"
    return "2week"


def _suggest_period_down(avg_rows: int, target: int) -> str:
    ratio = avg_rows / max(target, 1)
    if ratio >= 24:
        return "1hour"
    if ratio >= 8:
        return "4hour"
    if ratio >= 2:
        return "1day"
    return "1week"


_DATE_COLUMN_NAMES = {
    "date",
    "day",
    "trade_date",
    "trading_date",
    "event_date",
    "biz_date",
}
_TIMESTAMP_COLUMN_NAMES = {
    "ts",
    "time",
    "timestamp",
    "datetime",
    "event_time",
    "created_at",
    "updated_at",
}
_INTERNAL_PARTITION_COLUMNS = {"partition_key", "_partition_key", "part_key"}
_NATURAL_CALENDAR_PERIODS = [
    (5, "1week"),
    (10, "2week"),
    (21, "1month"),
    (63, "3month"),
    (126, "6month"),
    (252, "1year"),
    (504, "2year"),
    (1260, "5year"),
    (2520, "10year"),
]


def _column_names(table_spec) -> List[str]:
    names: List[str] = []
    for name in [
        table_spec.order_key,
        *(table_spec.primary_key or []),
        *list((table_spec.columns or {}).keys()),
    ]:
        if name and name not in names:
            names.append(str(name))
    return names


def _is_date_column(column: str) -> bool:
    name = column.lower()
    return (
        name in _DATE_COLUMN_NAMES
        or name.endswith("_date")
        or name.endswith("_day")
    )


def _is_timestamp_column(column: str) -> bool:
    name = column.lower()
    return (
        name in _TIMESTAMP_COLUMN_NAMES
        or name.endswith("_ts")
        or name.endswith("_time")
        or name.endswith("_at")
    )


def _infer_time_partition_base(table_spec) -> Optional[Dict[str, str]]:
    partition = table_spec.partition
    timezone = (
        table_spec.time.timezone
        if table_spec.time and table_spec.time.timezone
        else (partition.timezone if partition else None)
    ) or "UTC"

    if table_spec.time and table_spec.time.name:
        unit = table_spec.time.unit or "ms"
        kind = "calendar" if unit == "date" else "duration"
        return {
            "kind": kind,
            "column": table_spec.time.name,
            "timezone": timezone,
        }

    for column in _column_names(table_spec):
        if column.lower() in _INTERNAL_PARTITION_COLUMNS:
            continue
        if _is_date_column(column):
            return {"kind": "calendar", "column": column, "timezone": timezone}
        if _is_timestamp_column(column):
            return {"kind": "duration", "column": column, "timezone": timezone}
    return None


def _merge_factor(avg_rows: int, target_rows: int) -> int:
    if avg_rows <= 0:
        return 1
    return max(int(target_rows // avg_rows), 2)


def _natural_calendar_period(factor: int) -> Optional[tuple[str, int]]:
    candidates = [
        (days, period)
        for days, period in _NATURAL_CALENDAR_PERIODS
        if days <= factor
    ]
    if not candidates:
        return None
    days, period = candidates[-1]
    return period, days


def _recommended_calendar_every(factor: int) -> tuple[str, int]:
    if factor <= 20:
        return f"{factor}day", factor
    natural = _natural_calendar_period(factor)
    if natural:
        period, period_factor = natural
        return period, period_factor
    return f"{factor}day", factor


def _partition_expr(kind: str, column: str, every: Any, timezone: str = "UTC") -> str:
    if kind == "calendar":
        return PartitionSpec(
            column=column,
            every=every,
            kind="calendar",
            timezone=timezone,
        ).to_string()
    if kind == "duration":
        return PartitionSpec(
            column=column,
            every=every,
            kind="duration",
            time_unit="ms",
        ).to_string()
    return PartitionSpec.range(column, every).to_string()


def _display_partition_every(kind: str, every: Any) -> str:
    if kind != "duration":
        return str(every)
    expr = PartitionSpec(
        column="_",
        every=every,
        kind="duration",
        time_unit="ms",
    ).to_string()
    return expr.removeprefix("duration(_,every=").removesuffix(")")


def _add_suggestion(
    suggestions: List[Dict[str, Any]],
    label: str,
    expr: str,
    note: str,
    estimated_rows: Optional[int] = None,
) -> None:
    if any(item["expr"] == expr for item in suggestions):
        return
    suggestions.append({
        "label": label,
        "expr": expr,
        "note": note,
        "estimated_rows": estimated_rows,
    })


def _compatible_undersized_suggestion(table_spec, avg: int, factor: int):
    partition = table_spec.partition
    if not partition:
        return None
    estimated = avg * factor
    if partition.kind == "range":
        width = _to_positive_int(partition.every, 1) * factor
        return {
            "label": "兼容",
            "expr": PartitionSpec.range(partition.column, width).to_string(),
            "note": (
                f"保留当前数值分区键 {partition.column}，width={width} "
                f"(扩大 {factor} 倍)"
            ),
            "estimated_rows": estimated,
        }
    if partition.kind == "calendar":
        every, estimated_factor = _recommended_calendar_every(factor)
        expr = _partition_expr(
            "calendar",
            partition.column,
            every,
            partition.timezone or "UTC",
        )
        return {
            "label": "兼容",
            "expr": expr,
            "note": (
                f"保持当前 calendar 字段 {partition.column}，"
                f"按 {every} 合并 (max/avg≈{factor})"
            ),
            "estimated_rows": avg * estimated_factor,
        }
    if partition.kind == "duration":
        every = _scaled_duration_every(partition.every, factor)
        expr = _partition_expr("duration", partition.column, every)
        return {
            "label": "兼容",
            "expr": expr,
            "note": f"保持当前 duration 字段 {partition.column}，扩大 {factor} 倍",
            "estimated_rows": estimated,
        }
    return None


def _to_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _scaled_duration_every(every: Any, factor: int) -> Any:
    try:
        base_ms = PartitionSpec(
            column="_",
            every=every,
            kind="duration",
            time_unit="ms",
        ).to_partition().interval_ms
        return base_ms * factor
    except Exception:
        return f"{factor}day"


def _undersized_repartition_suggestions(table_spec, report, avg: int):
    target = report.max_rows_per_file
    factor = _merge_factor(avg, target)
    suggestions: List[Dict[str, Any]] = []

    time_base = _infer_time_partition_base(table_spec)
    if time_base:
        every: Any
        estimated_factor = factor
        every = f"{factor}day"
        if time_base["kind"] == "calendar":
            every, estimated_factor = _recommended_calendar_every(factor)
        if time_base["kind"] == "duration":
            baseline = "1day"
            if table_spec.partition and table_spec.partition.kind == "duration":
                baseline = table_spec.partition.every
            every = _scaled_duration_every(
                baseline,
                factor,
            )
        expr = _partition_expr(
            time_base["kind"],
            time_base["column"],
            every,
            time_base["timezone"],
        )
        every_text = _display_partition_every(time_base["kind"], every)
        _add_suggestion(
            suggestions,
            "推荐",
            expr,
            (
                f"按时间列 {time_base['column']} 使用 {every_text} "
                f"(max/avg≈{factor}, 目标<={_comma(target)})"
            ),
            avg * estimated_factor,
        )

        if time_base["kind"] == "calendar":
            natural = _natural_calendar_period(factor)
            if natural:
                period, period_factor = natural
                expr = _partition_expr(
                    "calendar",
                    time_base["column"],
                    period,
                    time_base["timezone"],
                )
                _add_suggestion(
                    suggestions,
                    "自然周期",
                    expr,
                    (
                        f"按时间列 {time_base['column']} 使用 {period} "
                        f"(约 {period_factor} 个交易日)"
                    ),
                    avg * period_factor,
                )

    compatible = _compatible_undersized_suggestion(table_spec, avg, factor)
    if compatible:
        _add_suggestion(
            suggestions,
            compatible["label"],
            compatible["expr"],
            compatible["note"],
            compatible["estimated_rows"],
        )

    return suggestions


def _emit_repartition_suggestions(source: str, table: str, suggestions) -> None:
    if not suggestions:
        return
    typer.echo("  suggestions:")
    for idx, item in enumerate(suggestions, 1):
        estimated = item.get("estimated_rows")
        estimate_text = (
            f", 估算~{_comma(estimated)} 行/分区"
            if estimated is not None
            else ""
        )
        typer.echo(
            f"    {idx}. {item['label']}: {item['note']}{estimate_text}"
        )
        typer.echo(
            _repartition_hint(
                source,
                table,
                item["expr"],
                indent="       ",
            )
        )


def _query_rows(
    source: str,
    table: str,
    fields: str,
    where: str,
    order_by: str,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        t = _require_table(db, table)
        return t.select(
            fields,
            where=_parse_json_or_text(where),
            order_by=order_by or None,
            limit=limit,
            to_dict=True,
        )
    finally:
        db.close()


@app.command()
def tables(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """列出逻辑表。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        names = db.table_names()
    finally:
        db.close()
    if json_output:
        _echo_json({"tables": names})
    else:
        for name in names:
            typer.echo(name)


@app.command("rename-database")
def rename_database(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    target: str = typer.Option(..., "--target", help="新的数据库 URI、路径或同级目录名"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已有目标目录"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认覆盖操作"),
):
    """重命名 SQLite 目录库。"""
    source_uri, source_path = _require_source(source)
    if overwrite and not yes:
        _fail("覆盖已有目标目录需要同时传 --overwrite --yes")
    db = Database(source_uri)
    try:
        db.rename(target, overwrite=overwrite)
        _echo_json({
            "source": str(source_path),
            "target_uri": db.uri,
            "overwritten": bool(overwrite),
        })
    finally:
        db.close()


@app.command("rename-table")
def rename_table(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="原表名"),
    new_table: str = typer.Option(..., "--new-table", help="新表名"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已有目标表目录"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认覆盖操作"),
):
    """重命名逻辑表。"""
    source_uri, _ = _require_source(source)
    if overwrite and not yes:
        _fail("覆盖已有目标表需要同时传 --overwrite --yes")
    db = Database(source_uri)
    try:
        renamed = db.rename_table(table, new_table, overwrite=overwrite)
        _echo_json({
            "table": table,
            "new_table": renamed.table_name,
            "overwritten": bool(overwrite),
        })
    finally:
        db.close()


@app.command()
def spec(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
):
    """输出表契约。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        t = _require_table(db, table)
        table_spec = t.spec()
        if not table_spec:
            _fail(f"表无契约: {table}")
        _echo_json(table_spec.to_dict())
    finally:
        db.close()


@app.command()
def layout(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    summary: bool = typer.Option(False, "--summary", help="只输出摘要"),
):
    """输出布局诊断报告。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        report = _require_table(db, table).layout_report()
        if summary:
            _echo_json({
                "table": report.table,
                "total_rows": report.total_rows,
                "total_partitions": report.total_partitions,
                "oversized_partitions": report.oversized_partitions,
                "undersized_partitions": report.undersized_partitions,
                "recommendation": report.recommendation,
            })
        else:
            _echo_json(report.to_dict())
    finally:
        db.close()


@app.command()
def parts(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """列出逻辑分区。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        items = _require_table(db, table).table_parts()
    finally:
        db.close()
    if json_output:
        _echo_json({"table": table, "parts": items})
    else:
        for item in items:
            typer.echo(item)


@app.command()
def segments(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    include_obsolete: bool = typer.Option(
        False,
        "--include-obsolete",
        help="同时输出 compact 后的 obsolete segment",
    ),
):
    """列出物理 segment。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        _echo_json(
            _require_table(db, table).segments(
                include_obsolete=include_obsolete,
            )
        )
    finally:
        db.close()


@app.command()
def compact(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    partition: str = typer.Option(
        "",
        "--partition",
        help="只整理指定 logical partition；默认整理全表",
    ),
    target_rows: Optional[int] = typer.Option(
        None,
        "--target-rows",
        help="新 segment 目标行数，默认使用表契约 target_rows",
    ),
):
    """整理 row segment，生成新 generation 并标记旧 segment 为 obsolete。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        result = _require_table(db, table).compact(
            partition=_parse_partition_value(partition),
            target_rows=target_rows,
        )
        _echo_json(result.to_dict())
    finally:
        db.close()


@app.command()
def validate(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    deep: bool = typer.Option(False, "--deep", help="执行 SQLite 文件和统计深度校验"),
    include_obsolete: bool = typer.Option(
        False,
        "--include-obsolete",
        help="同时校验 compact 后的 obsolete segment",
    ),
):
    """校验表 manifest 与物理 segment 文件。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        result = _require_table(db, table).validate(
            deep=deep,
            include_obsolete=include_obsolete,
        )
        _echo_json(result.to_dict())
        if not result.ok:
            raise typer.Exit(2)
    finally:
        db.close()


@app.command()
def cleanup_obsolete(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    partition: str = typer.Option(
        "",
        "--partition",
        help="只清理指定 logical partition；默认清理全表",
    ),
    yes: bool = typer.Option(False, "--yes", help="确认删除 obsolete segment 文件"),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="删除前不执行当前可见 segment 深度校验",
    ),
):
    """安全清理 compact 后的 obsolete segment 文件。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        result = _require_table(db, table).cleanup_obsolete(
            partition=_parse_partition_value(partition),
            dry_run=not yes,
            validate=not no_validate,
        )
        _echo_json(result.to_dict())
    finally:
        db.close()


@app.command()
def migrate(
    source: str = typer.Option(..., "--source", "-s", help="SQLite 源库 URI 或路径"),
    target: str = typer.Option(
        ...,
        "--target",
        help="目标库 URI，支持 postgresql:// 或 sqlite://",
    ),
    table: List[str] = typer.Option(
        None,
        "--table",
        "-t",
        help="迁移指定表；可用 source:target 重命名，可重复传入",
    ),
    batch_size: int = typer.Option(10_000, "--batch-size", help="迁移批大小"),
    mode: str = typer.Option("append", "--mode", help="append/ignore/upsert/replace"),
    verify: Optional[str] = typer.Option(
        None,
        "--verify",
        help="迁移后校验模式；目前支持 row-count",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览迁移计划，不连接目标库"),
):
    """把 SQLite iosql 库迁移到 PostgreSQL 或另一个 iosql 目标库。"""
    source_uri, _ = _require_source(source)
    target_uri = _parse_migration_target_uri(target)
    write_mode = _normalize_write_mode(mode)
    verify_mode = _normalize_verify_mode(verify)
    if batch_size <= 0:
        _fail("batch-size 必须大于 0")

    db = Database(source_uri)
    try:
        result = db.migrate_to(
            target_uri,
            tables=_parse_table_mapping(table or []),
            mode=write_mode,
            batch_size=batch_size,
            verify=verify_mode,
            dry_run=dry_run,
        )
        _echo_json(result.to_dict())
    finally:
        db.close()


@app.command()
def explain(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    fields: str = typer.Option("*", "--fields", help="查询字段"),
    where: str = typer.Option("", "--where", help="JSON where、文本 where 或 @file"),
    order_by: str = typer.Option("", "--order-by", help="排序字段"),
    limit: Optional[int] = typer.Option(None, "--limit", help="限制返回行数"),
):
    """输出查询计划。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        plan = _require_table(db, table).explain_select(
            fields=fields,
            where=_parse_json_or_text(where),
            order_by=order_by or None,
            limit=limit,
        )
        _echo_json(plan.to_dict())
    finally:
        db.close()


@app.command()
def query(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    fields: str = typer.Option("*", "--fields", help="查询字段"),
    where: str = typer.Option("", "--where", help="JSON where、文本 where 或 @file"),
    order_by: str = typer.Option("", "--order-by", help="排序字段"),
    limit: Optional[int] = typer.Option(None, "--limit", help="限制返回行数"),
    format_: str = typer.Option("json", "--format", help="json/jsonl/csv"),
):
    """查询小规模结果。"""
    rows = _query_rows(source, table, fields, where, order_by, limit)
    _emit_rows(rows, format_)


@app.command(name="export")
def export_data(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    output: Path = typer.Option(..., "--output", "-o", help="输出文件"),
    fields: str = typer.Option("*", "--fields", help="查询字段"),
    where: str = typer.Option("", "--where", help="JSON where、文本 where 或 @file"),
    order_by: str = typer.Option("", "--order-by", help="排序字段"),
    limit: Optional[int] = typer.Option(None, "--limit", help="限制返回行数"),
    format_: str = typer.Option("auto", "--format", help="json/jsonl/csv/auto"),
    batch_size: int = typer.Option(10_000, "--batch-size", help="流式导出批大小"),
):
    """流式导出查询结果。"""
    if batch_size <= 0:
        _fail("batch-size 必须大于 0")

    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        t = _require_table(db, table)
        batches = t.iter_select_batches(
            fields,
            where=_parse_json_or_text(where),
            order_by=order_by or None,
            limit=limit,
            batch_size=batch_size,
            to_dict=True,
        )
        rows = _write_row_batches(output, batches, format_)
    finally:
        db.close()
    _echo_json({"rows": rows, "output": str(output), "batch_size": batch_size})


@app.command(name="import")
def import_data(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    file: Path = typer.Option(..., "--file", "-f", help="输入文件"),
    format_: str = typer.Option("auto", "--format", help="json/jsonl/csv/auto"),
    mode: str = typer.Option("append", "--mode", help="append/ignore/upsert/replace"),
    batch_size: int = typer.Option(10_000, "--batch-size", help="写入批大小"),
):
    """导入 JSON、JSONL 或 CSV 到既有表。"""
    write_mode = _normalize_write_mode(mode)
    if batch_size <= 0:
        _fail("batch-size 必须大于 0")

    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    rows_written = 0
    try:
        t = _require_table(db, table)
        table_spec = t.spec()
        if not table_spec:
            _fail(f"表无契约，无法使用 table.write(): {table}")
        rows_seen = 0
        for batch in _iter_row_batches(file, format_, batch_size):
            if not batch:
                continue
            batch = _coerce_import_batch(batch, table_spec)
            rows_seen += len(batch)
            result = t.write(batch, mode=write_mode)
            if result.rows_written is None:
                rows_written = None
            elif rows_written is not None:
                rows_written += result.rows_written
    finally:
        db.close()
    _echo_json({
        "rows_seen": rows_seen,
        "rows_written": rows_written,
        "mode": write_mode,
        "batch_size": batch_size,
    })


@app.command()
def doctor(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option("", "--table", "-t", help="表名，默认检查所有表"),
    fix: bool = typer.Option(False, "--fix", help="自动修复缺失主键索引"),
):
    """诊断数据库健康状态。"""
    source_uri, source_path = _require_source(source)
    db = Database(source_uri)
    issues = 0
    unresolved = 0
    fixed = 0
    try:
        table_names = [table] if table else db.table_names()
        if not table_names:
            typer.echo("数据库中没有表")
            return

        for tname in sorted(table_names):
            t = _require_table(db, tname)
            table_spec = t.spec()
            if not table_spec or not table_spec.primary_key:
                typer.echo(f"[{tname}] 无表契约或无主键，跳过")
                continue

            report = t.layout_report()
            typer.echo(
                f"\n[{tname}] rows={_comma(report.total_rows)}  "
                f"partitions={report.total_partitions}"
            )

            if table_spec.partition:
                typer.echo(f"  partition: {_partition_desc(table_spec.partition)}")
            else:
                typer.echo("  partition: none")

            pk = table_spec.primary_key
            missing_cols = _missing_index_columns(_missing_indexes(source_path, tname, pk))
            existing_cols = [c for c in pk if c not in missing_cols]
            if existing_cols:
                parts = [f"{c} ✓" for c in existing_cols]
                if missing_cols:
                    parts += [f"{c} ✗" for c in missing_cols]
                typer.echo(f"  pk indexes: {', '.join(parts)}")
            else:
                typer.echo(f"  pk indexes: {', '.join(f'{c} ✗' for c in pk)}")

            oversized = report.oversized_partitions
            undersized = report.undersized_partitions
            avg = report.total_rows // report.total_partitions if report.total_partitions else 0

            if oversized:
                issues += 1
                unresolved += 1
                if table_spec.partition and table_spec.partition.kind == "range":
                    cur_every = int(table_spec.partition.every or 1)
                    suggestion = _suggest_range_every_down(avg, report.target_rows_per_file, cur_every)
                    estimated = avg * suggestion
                    hint = f"  (~{_comma(estimated)} 行/分区)"
                else:
                    suggestion = _suggest_period_down(avg, report.target_rows_per_file)
                    hint = ""
                suggestion_expr = table_spec.with_partition(every=suggestion).partition.to_string()
                typer.echo(
                    f"  oversized: {len(oversized)} 个分区行数过多 "
                    f"(max={_comma(report.max_rows_per_file)}, avg={_comma(avg)})"
                )
                typer.echo(_repartition_hint(source, tname, suggestion_expr, hint))
            elif undersized:
                issues += 1
                unresolved += 1
                typer.echo(
                    f"  undersized: {len(undersized)} 个分区行数不足 "
                    f"(min={_comma(report.min_rows_per_file)}, avg={_comma(avg)})"
                )
                suggestions = _undersized_repartition_suggestions(
                    table_spec,
                    report,
                    avg,
                )
                _emit_repartition_suggestions(source, tname, suggestions)
            else:
                typer.echo("  layout: ok")

            if missing_cols:
                issues += 1
                if fix:
                    for column in missing_cols:
                        t.create_index(column)
                        fixed += 1
                    remaining = _missing_indexes(source_path, tname, pk)
                    if remaining:
                        unresolved += 1
                        typer.echo(f"    fix incomplete: {_missing_index_columns(remaining)}")
                    else:
                        typer.echo("    fix: ok")
                else:
                    unresolved += 1
                    for col in missing_cols:
                        typer.echo(
                            f"    → iosql index create --source {source} --table {tname} --column {col}"
                        )
    finally:
        db.close()

    if issues:
        typer.echo(f"\n发现 {issues} 个问题" + (f"，已修复 {fixed} 个索引" if fix else ""))
        raise typer.Exit(1 if unresolved else 0)
    typer.echo("\n数据库健康状态正常")


@app.command()
def repartition(
    source: str = typer.Option(..., "--source", "-s", help="源数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    partition: str = typer.Option("", "--partition", help="新分区表达式，如 duration(ts,every=1hour)、range(id,width=100000)"),
    every: str = typer.Option("", "--every", "-e", help="兼容选项：只替换旧分区粒度，推荐改用 --partition"),
    kind: str = typer.Option("", "--kind", help="兼容选项：duration / calendar / range，不指定则保持原有类型"),
    timezone: str = typer.Option("", "--timezone", help="兼容选项：新时区，不指定则保持原有时区"),
    target: str = typer.Option("", "--target", help="写入目标数据库，不修改源库"),
    target_table: str = typer.Option("", "--target-table", help="目标表名，默认同源表"),
    batch_size: int = typer.Option(10_000, "--batch-size", help="复制批大小"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出计划，不写文件"),
    replace_source: bool = typer.Option(False, "--replace-source", help="用重分区结果替换源库；实际执行还必须 --yes"),
    backup: str = typer.Option("", "--backup", help="替换源库前备份到指定路径（默认自动生成）"),
    no_backup: bool = typer.Option(False, "--no-backup", help="替换源库时不备份"),
    yes: bool = typer.Option(False, "--yes", help="确认替换源库"),
):
    """按新分区策略重写表数据。

    指定 --target 时写入新库，不修改源库。
    指定 --replace-source 时替换源库，实际执行还必须传 --yes。"""

    if batch_size <= 0:
        _fail("batch-size 必须大于 0")
    if partition and (every or kind or timezone):
        _fail("--partition 不能与 --every/--kind/--timezone 同时使用")
    if target and replace_source:
        _fail("--target 与 --replace-source 不能同时使用")
    if not target and not replace_source:
        _fail("请提供 --target 写入新库，或提供 --replace-source 明确替换源库")
    if not replace_source and backup:
        _fail("--backup 只能与 --replace-source 同时使用")
    if not replace_source and no_backup:
        _fail("--no-backup 只能与 --replace-source 同时使用")
    if backup and no_backup:
        _fail("--backup 与 --no-backup 不能同时使用")
    if replace_source and not yes and not dry_run:
        _fail("替换源库必须同时提供 --replace-source --yes")

    source_uri, source_path = _require_source(source)
    db = Database(source_uri)
    try:
        t = _require_table(db, table)
        old_spec = t.spec()
        if not old_spec:
            _fail(f"表不存在或无契约: {table}")
        if partition:
            try:
                new_spec = old_spec.with_partition(partition=partition)
            except PartitionSpecError as exc:
                _fail(f"分区表达式错误: {exc}")
        else:
            if not old_spec.partition:
                _fail(f"表没有逻辑分区，请使用 --partition 指定新分区: {table}")
            new_spec = old_spec.with_partition(
                every=every or old_spec.partition.every,
                kind=kind or old_spec.partition.kind,
                timezone=timezone or old_spec.partition.timezone,
            )
        new_spec.force_partition = True
        report = t.layout_report()
    finally:
        db.close()

    if target:
        final_target_uri = _parse_uri(target)
        final_target_path = _uri_to_path(final_target_uri)
    else:
        tmp_path = source_path.with_name(
            f"{source_path.name}.repartition_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        final_target_uri = f"sqlite://{tmp_path}"
        final_target_path = tmp_path

    source_replaced = bool(replace_source)
    plan = {
        "source_uri": source_uri,
        "source_path": str(source_path),
        "table": table,
        "target_uri": final_target_uri,
        "target_path": str(final_target_path),
        "final_uri": source_uri if source_replaced else final_target_uri,
        "final_path": str(source_path if source_replaced else final_target_path),
        "target_table": target_table or table,
        "source_replaced": source_replaced,
        "batch_size": batch_size,
        "old_partition": old_spec.partition.to_dict() if old_spec.partition else None,
        "new_partition": new_spec.partition.to_dict() if new_spec.partition else None,
        "old_partition_expr": (
            old_spec.partition.to_string() if old_spec.partition else None
        ),
        "new_partition_expr": (
            new_spec.partition.to_string() if new_spec.partition else None
        ),
        "source_layout": {
            "total_rows": report.total_rows,
            "total_partitions": report.total_partitions,
            "recommendation": report.recommendation,
        },
    }
    if dry_run:
        _echo_json({"dry_run": True, "plan": plan})
        return

    if final_target_path.exists():
        _fail(f"目标数据库已存在: {final_target_path}")

    db2 = Database(source_uri)
    try:
        t = _require_table(db2, table)
        start = time.monotonic()
        result = t.repartition_to(
            final_target_uri,
            table_name=target_table or table,
            spec=new_spec,
            batch_size=batch_size,
        )
        elapsed = time.monotonic() - start
    finally:
        db2.close()

    backup_path = None
    if source_replaced:
        if no_backup:
            moved_old_path = source_path.with_name(
                f"{source_path.name}.repartition_old_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            os.replace(source_path, moved_old_path)
            try:
                os.replace(final_target_path, source_path)
            except Exception:
                os.replace(moved_old_path, source_path)
                raise
            _rmtree_robust(moved_old_path)
        else:
            backup_path = Path(backup) if backup else _backup_path(source_path)
            if backup_path.exists():
                _fail(f"备份目标已存在: {backup_path}")
            try:
                os.replace(source_path, backup_path)
            except OSError as e:
                if e.errno == errno.EXDEV:
                    _fail(
                        f"--backup 与源库不在同一文件系统，原子备份不支持跨盘: {backup_path}"
                    )
                raise
            try:
                os.replace(final_target_path, source_path)
            except Exception:
                os.replace(backup_path, source_path)
                raise

    _echo_json({
        "result": result.to_dict(),
        "plan": plan,
        "elapsed_seconds": round(elapsed, 3),
        "backup": str(backup_path) if backup_path is not None else None,
        "source_replaced": source_replaced,
    })


@app.command()
def backup(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    target: str = typer.Option("", "--target", help="备份目录，默认自动生成"),
):
    """备份 SQLite 目录库。"""
    _, source_path = _require_source(source)
    target_path = Path(target) if target else _backup_path(source_path)
    if target_path.exists():
        _fail(f"备份目标已存在: {target_path}")
    shutil.copytree(source_path, target_path, dirs_exist_ok=False)
    _echo_json({"source": str(source_path), "backup": str(target_path)})


@app.command()
def restore(
    backup_path: Path = typer.Option(..., "--backup", help="备份目录"),
    target: str = typer.Option(..., "--target", help="恢复目标 URI 或本地路径"),
    replace: bool = typer.Option(False, "--replace", help="覆盖已有目标"),
    yes: bool = typer.Option(False, "--yes", help="确认覆盖"),
):
    """从备份恢复 SQLite 目录库。"""
    if not backup_path.exists() or not backup_path.is_dir():
        _fail(f"备份目录不存在: {backup_path}")
    target_uri = _parse_uri(target)
    target_path = _uri_to_path(target_uri)
    if target_path.exists():
        if not replace or not yes:
            _fail("目标已存在，覆盖恢复必须同时提供 --replace --yes")
        target_real = target_path.resolve()
        if not target_real.is_dir():
            _fail(f"覆盖目标不是目录，拒绝删除: {target_path}")
        if target_real in (Path("/").resolve(), Path.home().resolve()):
            _fail(f"拒绝覆盖特殊路径: {target_path}")
        if not _is_iosql_like_database(target_real):
            _fail(f"目标目录不像 iosql 库（缺少 meta.sqlite3），拒绝覆盖: {target_path}")
        tmp_path = target_real.with_name(
            f"{target_real.name}.restore_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        os.replace(target_real, tmp_path)
        try:
            _rmtree_robust(tmp_path)
        except Exception:
            os.replace(tmp_path, target_real)
            raise
    shutil.copytree(backup_path, target_path, dirs_exist_ok=False)
    _echo_json({"backup": str(backup_path), "target": str(target_path)})


@app.command()
def ui(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    write: bool = typer.Option(False, "--write", help="开启可修改模式（默认只读）"),
    host: str = typer.Option("127.0.0.1", "--host", help="服务绑定地址"),
    port: int = typer.Option(8686, "--port", help="服务端口"),
    no_browser: bool = typer.Option(False, "--no-browser", help="启动后不自动打开浏览器"),
):
    """启动本地 Web 管理台。"""
    source_uri = _parse_uri(source)
    source_path = _uri_to_path(source_uri)
    if not source_path.exists():
        raise typer.BadParameter(f"数据库不存在: {source_path}")

    # 惰性导入：UI 依赖属于 [ui] extra，未安装时给出安装指引
    try:
        from iosql.ui import create_app
        import uvicorn
    except ImportError:
        _fail('UI 依赖未安装，请先执行: pip install "iosql[ui]"')

    mode = "write" if write else "readonly"
    web_app = create_app(source_uri, mode=mode)
    url = f"http://{host}:{port}"
    mode_label = "WRITABLE" if write else "READ-ONLY"
    typer.secho(f"iosql UI: {url} ({mode_label})", fg=typer.colors.GREEN)
    if not no_browser:
        # 延迟打开浏览器，等 uvicorn 完成监听
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(web_app, host=host, port=port)


@index_app.command("list")
def index_list(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
):
    """列出所有物理分区文件上的索引。"""
    source_uri, source_path = _require_source(source)
    db = Database(source_uri)
    try:
        _require_table(db, table)
    finally:
        db.close()
    _echo_json({"table": table, "parts": _index_report(source_path, table)})


@index_app.command("check")
def index_check(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
):
    """检查主键索引是否完整。"""
    source_uri, source_path = _require_source(source)
    db = Database(source_uri)
    try:
        table_obj = _require_table(db, table)
        table_spec = table_obj.spec()
        if not table_spec or not table_spec.primary_key:
            _fail(f"表无契约或无主键: {table}")
        missing = _missing_indexes(source_path, table, table_spec.primary_key)
    finally:
        db.close()

    _echo_json({"table": table, "missing": missing})
    if missing:
        raise typer.Exit(1)


@index_app.command("create")
def index_create(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    column: str = typer.Option(..., "--column", "-c", help="列名"),
    order: str = typer.Option("asc", "--order", help="asc/desc"),
):
    """为表的所有物理分区文件创建列索引。"""
    order_value = str(order or "asc").lower()
    if order_value not in ["asc", "desc"]:
        _fail("order 必须是 asc 或 desc")
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        _require_table(db, table).create_index(column, order=order_value)
    finally:
        db.close()
    _echo_json({"table": table, "column": column, "order": order_value, "created": True})


@index_app.command("drop")
def index_drop(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI 或本地路径"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    column: str = typer.Option(..., "--column", "-c", help="列名"),
):
    """删除表的所有物理分区文件上的列索引。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        dropped = _require_table(db, table).drop_index(column)
    finally:
        db.close()
    _echo_json({"table": table, "column": column, "dropped": dropped})


if __name__ == "__main__":
    app()
