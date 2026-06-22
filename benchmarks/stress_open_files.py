#!/usr/bin/env python3
"""验证多分区场景的 SQLite 文件描述符预算。"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import resource
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from iosql import Database  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--partitions", type=int, default=500)
    parser.add_argument("--abandoned-instances", type=int, default=150)
    parser.add_argument("--max-open-partitions", type=int, default=5)
    parser.add_argument("--max-connections-per-file", type=int, default=1)
    parser.add_argument("--gc-every", type=int, default=10)
    parser.add_argument("--cleanup", action="store_true")
    return parser.parse_args()


def _positive(name, value):
    if value <= 0:
        raise ValueError(f"{name} 必须为正数")


def validate_args(args):
    _positive("partitions", args.partitions)
    _positive("abandoned-instances", args.abandoned_instances)
    _positive("max-open-partitions", args.max_open_partitions)
    _positive("max-connections-per-file", args.max_connections_per_file)
    _positive("gc-every", args.gc_every)


def fd_count():
    for path in (Path("/proc/self/fd"), Path("/dev/fd")):
        if path.is_dir():
            return len(list(path.iterdir()))
    return None


class FdMonitor:
    def __init__(self, interval=0.01):
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self.baseline = fd_count()
        self.peak = self.baseline

    def _sample(self):
        while not self._stop.is_set():
            current = fd_count()
            if current is not None:
                self.peak = current if self.peak is None else max(self.peak, current)
            self._stop.wait(self._interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self._stop.set()
        self._thread.join()
        current = fd_count()
        if current is not None:
            self.peak = current if self.peak is None else max(self.peak, current)


def _connection_count(table):
    databases = [table._meta_db, table._table_meta_db, *table._db_parts.values()]
    return sum(
        database.get_connection_pool_status()["created_connections"]
        for database in databases
        if database is not None and not database.is_closed()
    )


def run_stress(args, emit=True):
    validate_args(args)
    args.root.mkdir(parents=True, exist_ok=True)
    run_dir = args.root / f"open-files-{time.time_ns()}"
    run_dir.mkdir()

    baseline = fd_count()
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    peak_open_partitions = 0
    peak_connections = 0
    started = time.perf_counter()

    with FdMonitor() as monitor:
        db = Database(
            f"sqlite://{run_dir}/many-parts.iosql",
            max_open_partitions=args.max_open_partitions,
            max_connections_per_file=args.max_connections_per_file,
        )
        table = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "real"},
            partition="duration(ts,every=1day)",
        )
        rows = [
            {
                "symbol": "AAPL",
                "ts": partition * 86_400_000,
                "price": float(partition),
            }
            for partition in range(args.partitions)
        ]
        table.write(rows)
        peak_open_partitions = max(
            peak_open_partitions,
            table._open_part_db_count(),
        )
        peak_connections = max(peak_connections, _connection_count(table))

        selected = table.query(order_by="ts").to_list()
        peak_open_partitions = max(
            peak_open_partitions,
            table._open_part_db_count(),
        )
        peak_connections = max(peak_connections, _connection_count(table))
        if len(selected) != args.partitions:
            raise RuntimeError(
                f"多分区扫描行数不匹配: {len(selected)} != {args.partitions}"
            )
        if peak_open_partitions > args.max_open_partitions:
            raise RuntimeError(
                f"打开分区数超限: {peak_open_partitions} > "
                f"{args.max_open_partitions}"
            )
        db.close()

        for index in range(args.abandoned_instances):
            abandoned_db = Database(
                f"sqlite://{run_dir}/abandoned-{index}.iosql",
                max_open_partitions=args.max_open_partitions,
                max_connections_per_file=args.max_connections_per_file,
            )
            abandoned_table = abandoned_db.table(
                "ticks",
                pk=["symbol", "ts"],
                columns={"symbol": "text", "ts": "integer"},
                partition="duration(ts,every=1day)",
            )
            abandoned_table.write({"symbol": "AAPL", "ts": 0})
            del abandoned_table, abandoned_db
            if (index + 1) % args.gc_every == 0:
                gc.collect()

        gc.collect()

    final = fd_count()
    elapsed = time.perf_counter() - started
    report = {
        "root": str(args.root.resolve()),
        "run_dir": str(run_dir.resolve()),
        "run_retained": not args.cleanup,
        "environment": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "fd_soft_limit": soft_limit,
            "fd_hard_limit": hard_limit,
        },
        "partitions": args.partitions,
        "abandoned_instances": args.abandoned_instances,
        "max_open_partitions": args.max_open_partitions,
        "max_connections_per_file": args.max_connections_per_file,
        "peak_open_partitions": peak_open_partitions,
        "peak_connections": peak_connections,
        "fd_baseline": baseline,
        "fd_peak": monitor.peak,
        "fd_final": final,
        "elapsed_seconds": round(elapsed, 6),
    }
    report_path = run_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if emit:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.cleanup:
        shutil.rmtree(run_dir)
        if emit:
            print(f"removed={run_dir}")
    return report


def main():
    args = parse_args()
    try:
        run_stress(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
