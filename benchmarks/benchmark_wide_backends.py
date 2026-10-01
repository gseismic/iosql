#!/usr/bin/env python3
"""比较 iosql SQLite 与 Parquet 后端在 200 列宽表上的性能。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from iosql import Database, Segment  # noqa: E402
from benchmarks.benchmark_write import filesystem_info  # noqa: E402


TABLE_NAME = "wide"
PRIMARY_KEY = "id"
COLUMN_COUNT = 200
DATA_COLUMN_COUNT = COLUMN_COUNT - 1
PROJECTION_COLUMNS = [PRIMARY_KEY, "value_001", "value_100", "value_199"]


def _positive_int_list(text: str, label: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in text.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} 必须是逗号分隔的整数") from error
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError(f"{label} 必须全部为正数")
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(f"{label} 不应包含重复值")
    return values


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rows",
        type=lambda value: _positive_int_list(value, "rows"),
        default=[1_000, 10_000, 50_000, 100_000],
        help="每个后端测试的行数，逗号分隔",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument(
        "--segment-rows",
        type=int,
        default=None,
        help="Segment.rows 上限；默认等于 batch-size",
    )
    parser.add_argument("--read-batch-size", type=int, default=1_000)
    parser.add_argument("--selective-rows", type=int, default=1_000)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent / ".runtime" / "wide-backends",
        help="临时测试数据库目录；每轮结束后默认清理",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="报告 JSON 路径，Markdown 写到同名 .md；默认写入 benchmarks/results",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留每轮生成的数据库目录",
    )
    return parser.parse_args()


def _validate_args(args):
    if args.repeats <= 0:
        raise ValueError("repeats 必须为正数")
    if args.batch_size <= 0 or args.read_batch_size <= 0:
        raise ValueError("batch-size 和 read-batch-size 必须为正数")
    if args.selective_rows <= 0:
        raise ValueError("selective-rows 必须为正数")
    if args.segment_rows is None:
        args.segment_rows = args.batch_size
    if args.segment_rows <= 0:
        raise ValueError("segment-rows 必须为正数")


def _column_types() -> dict[str, str]:
    columns = {PRIMARY_KEY: "integer"}
    columns.update(
        {f"value_{index:03d}": "float" for index in range(1, DATA_COLUMN_COUNT + 1)}
    )
    if len(columns) != COLUMN_COUNT:
        raise AssertionError(f"schema 应为 {COLUMN_COUNT} 列，实际 {len(columns)} 列")
    return columns


def _value(row_id: int, column_index: int) -> float:
    # 可重复生成、跨行变化的数据，且避免引入随机数生成开销。
    return float(((row_id * (column_index * 2 + 1) + column_index * 97) & 0xFFFF) / 100)


def _rows(start: int, stop: int):
    for row_id in range(start, stop):
        row = {PRIMARY_KEY: row_id}
        for column_index in range(1, DATA_COLUMN_COUNT + 1):
            row[f"value_{column_index:03d}"] = _value(row_id, column_index)
        yield row


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _scan(table, fields, batch_size, where=None) -> tuple[float, int, float]:
    started = time.perf_counter()
    row_count = 0
    checksum = 0.0
    for batch in table.iter_select_batches(
        fields=fields,
        where=where,
        batch_size=batch_size,
    ):
        row_count += len(batch)
        if fields != "*":
            checksum += sum(row["id"] for row in batch)
    return time.perf_counter() - started, row_count, checksum


def _check_samples(table, total_rows: int):
    for row_id in sorted({0, total_rows - 1}):
        rows = table.select(
            fields=PROJECTION_COLUMNS,
            where=f"id == {row_id}",
            limit=1,
        )
        if len(rows) != 1 or rows[0]["id"] != row_id:
            raise AssertionError(f"抽样主键校验失败: id={row_id}")
        for column_index in (1, 100, 199):
            field = f"value_{column_index:03d}"
            if rows[0][field] != _value(row_id, column_index):
                raise AssertionError(f"抽样值校验失败: id={row_id}, column={field}")


def run_case(
    *,
    root: Path,
    backend: str,
    total_rows: int,
    repeat: int,
    batch_size: int,
    segment_rows: int,
    read_batch_size: int,
    selective_rows: int,
    keep_data: bool,
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    case_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{backend}-{total_rows}-{repeat}-",
            dir=root,
        )
    )
    database_path = case_dir / "database.iosql"
    database = None
    data_paths = []
    result = {
        "backend": backend,
        "rows": total_rows,
        "repeat": repeat,
        "column_count": COLUMN_COUNT,
        "batch_size": batch_size,
        "segment_rows": segment_rows,
        "read_batch_size": read_batch_size,
        "database_bytes": None,
        "data_bytes": None,
        "data_file_count": None,
        "database_path": str(database_path.resolve()) if keep_data else None,
    }

    try:
        started = time.perf_counter()
        database = Database(f"{backend}://{database_path}")
        table = database.table(
            TABLE_NAME,
            pk=[PRIMARY_KEY],
            columns=_column_types(),
            segment=Segment.rows(
                max_rows=segment_rows,
                stats_columns=[PRIMARY_KEY],
            ),
            schema_policy="strict",
        )

        write_call_seconds = 0.0
        rows_seen = 0
        rows_written = 0
        write_calls = 0
        for batch_start in range(0, total_rows, batch_size):
            batch_stop = min(batch_start + batch_size, total_rows)
            batch = list(_rows(batch_start, batch_stop))
            write_started = time.perf_counter()
            write_result = table.write(batch)
            write_call_seconds += time.perf_counter() - write_started
            rows_seen += write_result.rows_seen
            rows_written += write_result.rows_written
            write_calls += 1
        ingest_seconds = time.perf_counter() - started
        if rows_seen != total_rows or rows_written != total_rows:
            raise AssertionError(
                f"写入计数错误: seen={rows_seen}, written={rows_written}, expected={total_rows}"
            )

        count_started = time.perf_counter()
        counted_rows = table.count_rows()
        count_seconds = time.perf_counter() - count_started
        if counted_rows != total_rows:
            raise AssertionError(f"count_rows={counted_rows}, expected={total_rows}")

        full_scan_seconds, full_scan_rows, _ = _scan(
            table, "*", read_batch_size
        )
        if full_scan_rows != total_rows:
            raise AssertionError(
                f"全列扫描行数错误: {full_scan_rows}, expected={total_rows}"
            )

        projection_seconds, projection_rows, projection_checksum = _scan(
            table,
            PROJECTION_COLUMNS,
            read_batch_size,
        )
        if projection_rows != total_rows:
            raise AssertionError(
                f"投影扫描行数错误: {projection_rows}, expected={total_rows}"
            )

        filter_row_count = min(selective_rows, max(1, total_rows // 10))
        filter_start_id = total_rows - filter_row_count
        where = f"id >= {filter_start_id}"
        selective_seconds, selective_result_rows, selective_checksum = _scan(
            table,
            PROJECTION_COLUMNS,
            read_batch_size,
            where=where,
        )
        if selective_result_rows != filter_row_count:
            raise AssertionError(
                "条件扫描行数错误: "
                f"{selective_result_rows}, expected={filter_row_count}"
            )
        plan = table.explain_select(fields=PROJECTION_COLUMNS, where=where)
        segment_count = len(table.segments())
        _check_samples(table, total_rows)

        segment_paths = [
            Path(segment["path"])
            for segment in table.segments()
            if segment.get("path")
        ]
        data_paths = segment_paths
        result.update(
            {
                "rows_seen": rows_seen,
                "rows_written": rows_written,
                "write_calls": write_calls,
                "write_call_seconds": write_call_seconds,
                "ingest_seconds": ingest_seconds,
                "write_rows_per_second": (
                    rows_written / write_call_seconds if write_call_seconds else None
                ),
                "ingest_rows_per_second": (
                    rows_written / ingest_seconds if ingest_seconds else None
                ),
                "count_rows_seconds": count_seconds,
                "full_scan_seconds": full_scan_seconds,
                "full_scan_rows": full_scan_rows,
                "full_scan_rows_per_second": (
                    full_scan_rows / full_scan_seconds if full_scan_seconds else None
                ),
                "projection_seconds": projection_seconds,
                "projection_rows": projection_rows,
                "projection_checksum": projection_checksum,
                "projection_rows_per_second": (
                    projection_rows / projection_seconds
                    if projection_seconds
                    else None
                ),
                "selective_where": where,
                "selective_rows": selective_result_rows,
                "selective_seconds": selective_seconds,
                "selective_checksum": selective_checksum,
                "selective_rows_per_second": (
                    selective_result_rows / selective_seconds
                    if selective_seconds
                    else None
                ),
                "segment_count": segment_count,
                "filter_candidate_segments": len(plan.candidate_segments),
                "filter_scanned_segments": len(plan.scanned_segments),
                "filter_skipped_segments": len(plan.skipped_segments),
            }
        )
    finally:
        if database is not None:
            database.close()
        result["database_bytes"] = _directory_size(database_path) if database_path.exists() else 0
        # SQLite 可能先把数据留在 WAL；关闭数据库后再统计主数据文件体积。
        existing_data_paths = [path for path in data_paths if path.is_file()]
        result["data_file_count"] = len(existing_data_paths)
        result["data_bytes"] = sum(path.stat().st_size for path in existing_data_paths)
        if not keep_data:
            shutil.rmtree(case_dir, ignore_errors=True)

    return result


def _median_run(runs: list[dict], backend: str, rows: int, field: str):
    values = [
        run[field]
        for run in runs
        if run["backend"] == backend and run["rows"] == rows
    ]
    return statistics.median(values) if values else None


def _ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def markdown_report(summary: dict) -> str:
    lines = [
        "# SQLite 与 Parquet：200 列宽表基准",
        "",
        f"- 日期：`{summary['generated_at']}`",
        f"- 表结构：`{summary['column_count']}` 列（1 个整数主键、199 个浮点列）",
        f"- 行数：`{', '.join(f'{rows:,}' for rows in summary['row_sizes'])}`",
        f"- 重复次数：`{summary['repeats']}`；展示值为重复运行的中位数",
        f"- 写入批次 / segment 上限：`{summary['batch_size']:,}` / `{summary['segment_rows']:,}` 行",
        f"- 读取批次：`{summary['read_batch_size']:,}` 行；过滤读取最多返回 `min({summary['selective_rows']:,}, 行数的 10%)` 行",
        "",
        "## 查询耗时中位数",
        "",
        "查询耗时包含流式读取并构造返回行的时间。过滤查询读取 `id` 命中的最多 1,000 行或该规模的 10%，并记录 segment 裁剪。",
        "",
        "| 行数 | 后端 | 全部 200 列（秒） | 4 列投影（秒） | 条件读取（秒） | count_rows（秒） | segment 裁剪（扫描/候选） |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]

    latency_metrics = [
        "full_scan_seconds",
        "projection_seconds",
        "selective_seconds",
        "count_rows_seconds",
        "filter_scanned_segments",
        "filter_candidate_segments",
    ]
    for row_count in summary["row_sizes"]:
        for backend in ("sqlite", "parquet"):
            values = {
                field: _median_run(summary["runs"], backend, row_count, field)
                for field in latency_metrics
            }
            lines.append(
                "| "
                f"{row_count:,} | {backend} | "
                f"{values['full_scan_seconds']:.4f} | "
                f"{values['projection_seconds']:.4f} | "
                f"{values['selective_seconds']:.4f} | "
                f"{values['count_rows_seconds']:.4f} | "
                f"{values['filter_scanned_segments']:.0f}/"
                f"{values['filter_candidate_segments']:.0f} |"
            )

    lines.extend(
        [
            "",
            "## 查询与写入吞吐中位数",
            "",
            "写入吞吐按 `write()` 调用耗时计算；端到端吞吐包含 200 列数据生成。",
            "",
            "| 行数 | 后端 | 全列读取行/秒 | 4 列投影行/秒 | 条件读取行/秒 | 写入行/秒 | 端到端写入行/秒 | 数据文件 MB | 数据库 MB | segment |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    metrics = [
        "write_rows_per_second",
        "ingest_rows_per_second",
        "full_scan_rows_per_second",
        "projection_rows_per_second",
        "selective_rows_per_second",
        "data_bytes",
        "database_bytes",
        "segment_count",
    ]
    for row_count in summary["row_sizes"]:
        for backend in ("sqlite", "parquet"):
            values = {
                field: _median_run(summary["runs"], backend, row_count, field)
                for field in metrics
            }
            lines.append(
                "| "
                f"{row_count:,} | {backend} | "
                f"{values['full_scan_rows_per_second']:,.0f} | "
                f"{values['projection_rows_per_second']:,.0f} | "
                f"{values['selective_rows_per_second']:,.0f} | "
                f"{values['write_rows_per_second']:,.0f} | "
                f"{values['ingest_rows_per_second']:,.0f} | "
                f"{values['data_bytes'] / 1_000_000:.2f} | "
                f"{values['database_bytes'] / 1_000_000:.2f} | "
                f"{values['segment_count']:.0f} |"
            )

    lines.extend(
        [
            "",
            "## Parquet 相对 SQLite",
            "",
            "吞吐倍率大于 1 表示 Parquet 更快；数据体积倍率小于 1 表示 Parquet 占用更少。",
            "",
            "| 行数 | 全列读取吞吐倍率 | 4 列投影吞吐倍率 | 条件读取吞吐倍率 | 写入吞吐倍率 | 数据体积倍率 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row_count in summary["row_sizes"]:
        rates = {
            field: {
                backend: _median_run(summary["runs"], backend, row_count, field)
                for backend in ("sqlite", "parquet")
            }
            for field in (
                "write_rows_per_second",
                "full_scan_rows_per_second",
                "projection_rows_per_second",
                "selective_rows_per_second",
                "data_bytes",
            )
        }
        ratios = {
            "full_scan_rows_per_second": _ratio(
                rates["full_scan_rows_per_second"]["parquet"],
                rates["full_scan_rows_per_second"]["sqlite"],
            ),
            "projection_rows_per_second": _ratio(
                rates["projection_rows_per_second"]["parquet"],
                rates["projection_rows_per_second"]["sqlite"],
            ),
            "selective_rows_per_second": _ratio(
                rates["selective_rows_per_second"]["parquet"],
                rates["selective_rows_per_second"]["sqlite"],
            ),
            "write_rows_per_second": _ratio(
                rates["write_rows_per_second"]["parquet"],
                rates["write_rows_per_second"]["sqlite"],
            ),
            "data_bytes": _ratio(
                rates["data_bytes"]["parquet"],
                rates["data_bytes"]["sqlite"],
            ),
        }
        lines.append(
            f"| {row_count:,} | "
            + " | ".join(f"{ratios[field]:.2f}x" for field in ratios)
            + " |"
        )

    lines.extend(
        [
            "",
            "## 环境",
            "",
            f"- Python：`{summary['environment']['python']}`",
            f"- 平台：`{summary['environment']['platform']}`",
            f"- SQLite：`{summary['environment']['sqlite']}`；DuckDB：`{summary['environment']['duckdb']}`",
            f"- CPU 逻辑核心数：`{summary['environment']['cpu_count']}`",
            f"- 文件系统：`{summary['environment']['filesystem'].get('filesystem', 'unknown')}`；挂载源：`{summary['environment']['filesystem'].get('source', 'unknown')}`",
            "",
            "## 说明",
            "",
            "- 写入批次默认与 `Segment.rows` 上限相同，减少未封存 Parquet 文件因小批写入而被反复重写的影响。",
            "- 全列读取和投影读取都通过 `iter_select_batches()` 流式消费；条件读取使用 `id` 统计执行 segment 裁剪。",
            "- 查询依次执行 `count_rows`、全列扫描、投影扫描和条件扫描；前序操作可能预热系统页缓存，结果应视为当前顺序下的热/温缓存表现。",
            "- 数据为确定性合成数值，列值有重复模式；Parquet 压缩比和吞吐会随真实数据分布变化。",
            "- 结果描述当前机器、当前 DuckDB/SQLite 版本和本次数据生成模式下的相对表现，不代表所有磁盘和数据分布。",
            "",
        ]
    )
    return "\n".join(lines)


def run_benchmark(args) -> dict:
    _validate_args(args)
    run_root = args.root.expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError('缺少 Parquet 可选依赖，请运行 pip install "iosql[parquet]"') from error

    runs = []
    total = len(args.rows) * args.repeats * 2
    current = 0
    for repeat in range(1, args.repeats + 1):
        for row_index, total_rows in enumerate(args.rows):
            backends = ("sqlite", "parquet")
            if (repeat + row_index) % 2 == 0:
                backends = tuple(reversed(backends))
            for backend in backends:
                current += 1
                print(
                    f"[{current}/{total}] backend={backend} rows={total_rows:,} repeat={repeat}",
                    flush=True,
                )
                report = run_case(
                    root=run_root,
                    backend=backend,
                    total_rows=total_rows,
                    repeat=repeat,
                    batch_size=args.batch_size,
                    segment_rows=args.segment_rows,
                    read_batch_size=args.read_batch_size,
                    selective_rows=args.selective_rows,
                    keep_data=args.keep_data,
                )
                runs.append(report)
                print(
                    f"  write={report['write_rows_per_second']:,.0f} rows/s, "
                    f"full_scan={report['full_scan_rows_per_second']:,.0f} rows/s, "
                    f"data={report['data_bytes'] / 1_000_000:.2f} MB",
                    flush=True,
                )

    summary = {
        "generated_at": generated_at,
        "column_count": COLUMN_COUNT,
        "row_sizes": args.rows,
        "repeats": args.repeats,
        "batch_size": args.batch_size,
        "segment_rows": args.segment_rows,
        "read_batch_size": args.read_batch_size,
        "selective_rows": args.selective_rows,
        "keep_data": args.keep_data,
        "root": str(run_root),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "duckdb": duckdb.__version__,
            "cpu_count": os.cpu_count(),
            "filesystem": filesystem_info(run_root),
        },
        "runs": runs,
    }

    output = args.output or (
        Path(__file__).resolve().parent
        / "results"
        / f"wide-backends-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text(markdown_report(summary), encoding="utf-8")
    print(f"json={output}")
    print(f"markdown={markdown_path}")
    return summary


def main():
    args = parse_args()
    try:
        run_benchmark(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
