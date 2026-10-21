#!/usr/bin/env python3
"""拆解 SQLite 写入成本；实验仅在当前进程替换方法，不修改存储引擎。

uv run --no-sync python benchmarks/diagnose_sqlite_write.py \
  --suite main --rows 20000 --repeats 3 --output /tmp/sqlite-write-main.json
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import io
import json
import platform
import pstats
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from iosql import Database, Segment  # noqa: E402
from iosql.utils.mysqlite import Database as MyDatabase  # noqa: E402
from iosql.utils.mysqlite import table as low_table  # noqa: E402
from iosql.utils.mysqlite.const import _DEFAULT_PRAGMA  # noqa: E402
from iosql.utils.mysqlite.sql import group_rows_by_keys  # noqa: E402


def fast_groups(rows):
    """同构批次走 itemgetter；异构批次保留现有顺序与 DEFAULT 语义。"""
    fields = list(rows[0])
    keys = rows[0].keys()
    if not all(row.keys() == keys for row in rows):
        return group_rows_by_keys(rows)
    if len(fields) == 1:
        values = [(row[fields[0]],) for row in rows]
    else:
        values = list(map(itemgetter(*fields), rows))
    return [(fields, values)]


@contextmanager
def prototype(variant):
    """无 schema 自动演进时跳过无用途的类型扫描，实验完成后恢复原方法。"""
    if variant == "baseline":
        yield
        return
    original = low_table.Table._insert_one_batch_on_connection

    def insert(self, cursor, conn, rows, replace=False, ignore=False,
               ensure_table=False, ensure_column=False, **kwargs):
        if ensure_table or ensure_column:
            return original(
                self, cursor, conn, rows, replace=replace, ignore=ignore,
                ensure_table=ensure_table, ensure_column=ensure_column, **kwargs,
            )
        grouping = fast_groups if variant == "fast-groups" else group_rows_by_keys
        statements = [
            (low_table.getsqlarg_dict_insertmany(
                self._table_name, fields, replace=replace, ignore=ignore,
            ), values)
            for fields, values in grouping(rows)
        ]
        self._executemany_statements_on_connection(cursor, conn, statements)

    with patch.object(low_table.Table, "_insert_one_batch_on_connection", insert):
        yield


def schema(columns):
    return {"id": "integer", **{f"v{i:03d}": "float" for i in range(1, columns)}}


def value(row_id, column_index):
    # 与既有宽表基准采用相同公式，避免整数值 float 的紧凑落盘影响比较。
    return float(((row_id * (column_index * 2 + 1) + column_index * 97) & 0xFFFF) / 100)


def make_batches(total, batch_size, columns):
    fields = list(schema(columns))[1:]
    for start in range(0, total, batch_size):
        yield [
            {"id": i, **{field: value(i, j) for j, field in enumerate(fields, 1)}}
            for i in range(start, min(total, start + batch_size))
        ]


def scenario(name, **kwargs):
    return {"name": name, "layer": "iosql", "columns": 8,
            "batch_size": 5000, "segment_rows": None, "stats": "id",
            "variant": "baseline", "partition": None, "mode": "append",
            "synchronous": "normal", **kwargs}


def scenarios(suite):
    cases = []
    if suite == "main":
        for columns in (8, 200):
            for layer in ("sqlite3", "mysqlite", "iosql"):
                cases.append(scenario(f"c{columns}-{layer}", layer=layer, columns=columns))
            for variant in ("baseline", "lazy-types", "fast-groups"):
                cases.append(scenario(
                    f"c{columns}-segment-{variant}", columns=columns,
                    segment_rows=5000, variant=variant,
                ))
            cases.append(scenario(
                f"c{columns}-segment-all-stats", columns=columns,
                segment_rows=5000, stats="all",
            ))
    elif suite == "batches":
        for batch in (1, 10, 100, 1000, 5000):
            cases.append(scenario(f"batch-{batch}", batch_size=batch))
    elif suite == "layout":
        cases.append(scenario("single-file"))
        cases.append(scenario("range-1000", partition="range(id,width=1000)"))
        for columns in (8, 200):
            for segment in (1000, 20000):
                cases.append(scenario(
                    f"c{columns}-segment-{segment}", columns=columns,
                    segment_rows=segment,
                ))
        cases.append(scenario("synchronous-full", synchronous="full"))
    elif suite == "ignore":
        for segment in (None, 5000):
            for mode in ("append", "ignore"):
                cases.append(scenario(
                    f"segment-{segment}-{mode}", segment_rows=segment, mode=mode,
                ))
    return cases


def run_case(config, rows, root, profile=False):
    columns = schema(config["columns"])
    pragmas = {**_DEFAULT_PRAGMA, "synchronous": config["synchronous"]}
    close = None
    # 先生成数据：API 耗时排除数据生成，避免把 dict 构造误算为存储开销。
    generation_started = time.perf_counter()
    batches = list(make_batches(rows, config["batch_size"], config["columns"]))
    generation_seconds = time.perf_counter() - generation_started
    profiler = cProfile.Profile() if profile else None
    with tempfile.TemporaryDirectory(prefix="sqlite-write-", dir=root) as temporary:
        directory = Path(temporary)
        with prototype(config["variant"]):
            setup_started = time.perf_counter()
            if config["layer"] == "sqlite3":
                db = sqlite3.connect(directory / "data.sqlite3")
                close = db.close
                for key, pragma_value in pragmas.items():
                    db.execute(f"PRAGMA {key}={pragma_value}")
                definition = ",".join(f'"{field}" {type_}' for field, type_ in columns.items())
                db.execute(f'CREATE TABLE data ({definition}, PRIMARY KEY ("id"))')
                db.commit()
                sql = f'INSERT INTO data VALUES ({",".join("?" for _ in columns)})'

                def write(batch):
                    values = list(map(itemgetter(*columns), batch))
                    db.execute("BEGIN IMMEDIATE")
                    db.executemany(sql, values)
                    db.commit()

            elif config["layer"] == "mysqlite":
                db = MyDatabase(str(directory / "data.sqlite3"), pragma_dict=pragmas,
                                max_connections=1, verbose=0)
                close = db.close
                table = db["data"]
                table.create_table(
                    [{"name": field, "type": type_} for field, type_ in columns.items()],
                    pk=["id"],
                )
                write = table.insert_all
            else:
                db = Database(f"sqlite://{directory / 'data.iosql'}", pragma_dict=pragmas)
                close = db.close
                segment = None
                if config["segment_rows"]:
                    segment = Segment.rows(
                        max_rows=config["segment_rows"],
                        stats_columns=["id"] if config["stats"] == "id" else None,
                    )
                table = db.table("data", pk=["id"], columns=columns,
                                 segment=segment, partition=config["partition"],
                                 schema_policy="strict")

                def write(batch):
                    result = table.write(batch, mode=config["mode"])
                    assert result.rows_seen == len(batch)
                    assert result.rows_written in (None, len(batch))

            setup_seconds = time.perf_counter() - setup_started
            latencies = []
            cpu_started = time.process_time()
            started = time.perf_counter()
            try:
                if profiler:
                    profiler.enable()
                for batch in batches:
                    call_started = time.perf_counter()
                    write(batch)
                    latencies.append(time.perf_counter() - call_started)
            finally:
                if profiler:
                    profiler.disable()
                write_seconds = time.perf_counter() - started
                cpu_seconds = time.process_time() - cpu_started
                close_started = time.perf_counter()
                close()
                close_seconds = time.perf_counter() - close_started

        # 绕开逻辑计数缓存，关闭所有连接后用原始 SQLite 检查真实文件。
        files = sorted(
            file for file in directory.rglob("*.sqlite3")
            if file.name == "data.sqlite3"
            or (file.parent.name == "data" and file.parent.parent.name == "data")
        )
        if not files:
            raise AssertionError("未找到物理数据文件")
        counted = 0
        id_sum = 0
        samples = {}
        checked_pragmas = None
        for file in files:
            with closing(sqlite3.connect(f"file:{file}?mode=ro", uri=True)) as conn:
                count, checksum = conn.execute('SELECT COUNT(*), SUM(id) FROM data').fetchone()
                counted += count
                id_sum += checksum or 0
                if checked_pragmas is None:
                    checked_pragmas = {"journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0]}
                samples.update({row[0]: row[1:] for row in conn.execute(
                    "SELECT * FROM data WHERE id IN (?, ?)", (0, rows - 1),
                )})
        assert counted == rows, (config["name"], counted, rows)
        assert id_sum == rows * (rows - 1) // 2
        for row_id in (0, rows - 1):
            assert samples[row_id] == tuple(
                value(row_id, j) for j in range(1, config["columns"])
            )
        result = {
            **config, "rows": rows, "write_calls": len(latencies),
            "write_seconds": write_seconds, "cpu_seconds": cpu_seconds,
            "rows_per_second": rows / write_seconds,
            "first_call_seconds": latencies[0],
            "subsequent_calls_median_seconds": statistics.median(latencies[1:]) if len(latencies) > 1 else None,
            "generation_seconds": generation_seconds, "setup_seconds": setup_seconds,
            "close_seconds": close_seconds, "data_files": len(files),
            "physical_rows": counted, "id_checksum": id_sum,
            "data_bytes": sum(file.stat().st_size for file in files),
            "pragmas_requested": pragmas, "pragmas_reopened": checked_pragmas,
        }
        if profiler:
            stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
            stats.print_stats(40)
            result["profile"] = stream.getvalue()
            result["profile_functions"] = [
                {"file": file, "line": line, "name": name, "calls": values[1],
                 "self_seconds": values[2], "cumulative_seconds": values[3]}
                for (file, line, name), values in stats.stats.items()
                if name in ("_union_fields_of_dictlist", "suggest_value_type",
                            "group_rows_by_keys", "_ensure_contract_type_compatible",
                            "_batch_column_stats", "_segment_infos", "resource", "operation",
                            "_find_existing_pk_segment", "_refresh_segment_info",
                            "_data_file_row_count", "_data_file_column_stats",
                            "get_table_meta", "table_parts")
                or "executemany" in name or "commit" in name
            ]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["main", "batches", "layout", "ignore"], default="main")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--root", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--only", help="只运行名称包含指定文本的场景")
    args = parser.parse_args()
    if args.rows <= 0 or args.repeats <= 0:
        parser.error("rows 和 repeats 必须为正数")
    args.root.mkdir(parents=True, exist_ok=True)
    cases = [case for case in scenarios(args.suite) if not args.only or args.only in case["name"]]
    if not cases:
        parser.error("未匹配到场景")
    report = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="minutes"),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip(),
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform(), "root": str(args.root.resolve()),
                        "mount": subprocess.check_output(
                            ["findmnt", "-no", "SOURCE,FSTYPE,TARGET", "-T", str(args.root)], text=True,
                        ).strip()},
        "profiled": args.profile, "suite": args.suite, "runs": [], "summary": [],
        "data_formula": "id + fractional floats; ((id*(column*2+1)+column*97)&0xFFFF)/100",
    }
    for repeat in range(args.repeats):
        # 固定种子打散，减少始终在最后执行的场景受机器状态变化影响。
        order = list(cases)
        random.Random(20261021 + repeat).shuffle(order)
        for config in order:
            gc.collect()
            result = run_case(config, args.rows, args.root, profile=args.profile)
            result["repeat"] = repeat + 1
            report["runs"].append(result)
            print(f'{config["name"]} #{repeat + 1}: {result["rows_per_second"]:,.0f} rows/s, '
                  f'wall={result["write_seconds"]:.3f}s cpu={result["cpu_seconds"]:.3f}s', flush=True)
    for config in cases:
        runs = [run for run in report["runs"] if run["name"] == config["name"]]
        report["summary"].append({
            **config, "rows": args.rows,
            **{key: statistics.median(run[key] for run in runs)
               for key in ("write_seconds", "rows_per_second", "cpu_seconds",
                           "first_call_seconds", "close_seconds", "data_files")},
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report={args.output}")


if __name__ == "__main__":
    main()
