"""iosql 命令行工具。"""

from __future__ import annotations

import csv
import io
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import typer

from iosql import Database
from iosql.utils.mysqlite import Database as MyDatabase


app = typer.Typer(help="iosql SQLite 目录库管理工具")
index_app = typer.Typer(help="索引检查和维护")
app.add_typer(index_app, name="index")

_PART_DATA_TABLE_NAME = "data"
_ROW_FORMATS = {"json", "jsonl", "csv"}
_WRITE_MODES = {"append", "ignore", "upsert", "replace"}


def _fail(message: str, code: int = 1) -> None:
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


def _read_rows(path: Path, format_: str) -> List[Dict[str, Any]]:
    fmt = _normalize_row_format(format_, path)
    if not path.exists():
        _fail(f"输入文件不存在: {path}")

    if fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return [dict(item) for item in data]
        _fail("JSON 输入必须是对象或对象数组")

    if fmt == "jsonl":
        rows = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                _fail(f"JSONL 第 {line_no} 行不是对象")
            rows.append(item)
        return rows

    with path.open("r", encoding="utf-8", newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


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
    kind = partition.kind
    every = partition.every
    tz = partition.timezone
    tz_str = f", tz={tz}" if tz else ""
    return f"{kind}({partition.column}, every={every}{tz_str})"


def _existing_pk_indexes(source_path: Path, table_name: str, pk_columns: List[str]) -> set:
    expected = {f"{_PART_DATA_TABLE_NAME}_{col}_index" for col in pk_columns}
    found: set = set()
    for item in _index_report(source_path, table_name):
        found.update(item["indexes"])
    return expected & found


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
):
    """列出物理 segment。"""
    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    try:
        _echo_json(_require_table(db, table).segments())
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
):
    """导出查询结果。"""
    rows = _query_rows(source, table, fields, where, order_by, limit)
    _write_rows(output, rows, format_)
    _echo_json({"rows": len(rows), "output": str(output)})


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
    rows = _read_rows(file, format_)
    write_mode = _normalize_write_mode(mode)
    if batch_size <= 0:
        _fail("batch-size 必须大于 0")

    source_uri, _ = _require_source(source)
    db = Database(source_uri)
    rows_written = 0
    try:
        t = _require_table(db, table)
        if not t.spec():
            _fail(f"表无契约，无法使用 table.write(): {table}")
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            if not batch:
                continue
            result = t.write(batch, mode=write_mode)
            rows_written += result.rows_seen
    finally:
        db.close()
    _echo_json({"rows_seen": len(rows), "rows_written": rows_written, "mode": write_mode})


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
            existing = _existing_pk_indexes(source_path, tname, pk)
            existing_cols = [c for c in pk if f"{_PART_DATA_TABLE_NAME}_{c}_index" in existing]
            missing_cols = [c for c in pk if c not in existing_cols]
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
                suggestion = _suggest_period_down(avg, report.target_rows_per_file)
                typer.echo(
                    f"  oversized: {len(oversized)} 个分区行数过多 "
                    f"(max={_comma(report.max_rows_per_file)}, avg={_comma(avg)})"
                )
                typer.echo(
                    f"    → iosql repartition -s {source} -t {tname} -e {suggestion} --yes"
                )
            elif undersized:
                issues += 1
                unresolved += 1
                suggestion = _suggest_period_up(avg, report.target_rows_per_file)
                typer.echo(
                    f"  undersized: {len(undersized)} 个分区行数不足 "
                    f"(min={_comma(report.min_rows_per_file)}, avg={_comma(avg)})"
                )
                typer.echo(
                    f"    → iosql repartition -s {source} -t {tname} -e {suggestion} --yes"
                )
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
                        typer.echo(f"    fix: {'ok'}")
                else:
                    unresolved += 1
                    for col in missing_cols:
                        typer.echo(
                            f"    → iosql index create -s {source} -t {tname} -c {col}"
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
    every: str = typer.Option("", "--every", "-e", help="新分区周期，空值表示沿用旧周期"),
    kind: str = typer.Option("", "--kind", help="duration/calendar/range，空值表示沿用旧类型"),
    timezone: str = typer.Option("", "--timezone", help="新时区，空值表示沿用旧时区"),
    target: str = typer.Option("", "--target", help="目标数据库 URI 或本地路径（不提供则默认替换源库）"),
    target_table: str = typer.Option("", "--target-table", help="目标表名，默认同源表"),
    batch_size: int = typer.Option(10_000, "--batch-size", help="复制批大小"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出计划，不写文件"),
    backup: str = typer.Option("", "--backup", help="备份路径，替换源库前备份（默认自动生成 .backup.{datetime}）"),
    no_backup: bool = typer.Option(False, "--no-backup", help="替换源库时不备份（危险）"),
    yes: bool = typer.Option(False, "--yes", help="确认替换源库（未指定 --target 时必须提供）"),
):
    """按新分区策略重写表数据。"""
    if batch_size <= 0:
        _fail("batch-size 必须大于 0")
    if target and backup:
        _fail("--target 与 --backup 不能同时使用")
    if target and no_backup:
        _fail("--target 与 --no-backup 不能同时使用")
    if backup and no_backup:
        _fail("--backup 与 --no-backup 不能同时使用")
    if not target and not yes and not dry_run:
        _fail("替换源库必须同时提供 --yes")

    source_uri, source_path = _require_source(source)
    db = Database(source_uri)
    try:
        t = _require_table(db, table)
        old_spec = t.spec()
        if not old_spec:
            _fail(f"表不存在或无契约: {table}")
        if not old_spec.partition:
            _fail(f"表没有逻辑分区，无法通过 --every 重分区: {table}")

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

    source_replaced = not bool(target)
    plan = {
        "source_uri": source_uri,
        "source_path": str(source_path),
        "table": table,
        "target_uri": final_target_uri,
        "target_path": str(final_target_path),
        "target_table": target_table or table,
        "source_replaced": source_replaced,
        "batch_size": batch_size,
        "old_partition": old_spec.partition.to_dict() if old_spec.partition else None,
        "new_partition": new_spec.partition.to_dict() if new_spec.partition else None,
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
        if not no_backup:
            backup_path = Path(backup) if backup else _backup_path(source_path)
            if backup_path.exists():
                _fail(f"备份目标已存在: {backup_path}")
            shutil.copytree(source_path, backup_path, dirs_exist_ok=False)
        shutil.rmtree(source_path)
        shutil.move(str(final_target_path), str(source_path))

    _echo_json({
        "result": result.to_dict(),
        "elapsed_seconds": round(elapsed, 3),
        "backup": None if no_backup else str(backup_path),
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
        shutil.rmtree(target_path)
    shutil.copytree(backup_path, target_path, dirs_exist_ok=False)
    _echo_json({"backup": str(backup_path), "target": str(target_path)})


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
