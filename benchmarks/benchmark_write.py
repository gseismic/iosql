#!/usr/bin/env python3
"""iosql 大批量写入 benchmark。

示例：
python benchmarks/benchmark_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --rows 25000000 \
  --batch-size 50000
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from iosql import Database, Segment  # noqa: E402


PRAGMA_PROFILES = {
    "balanced": None,
    "durable": {
        "foreign_keys": "on",
        "journal_mode": "wal",
        "synchronous": "full",
        "cache_size": "-64000",
        "temp_store": "memory",
        "mmap_size": "268435456",
        "wal_autocheckpoint": "1000",
        "busy_timeout": "30000",
    },
    "replayable": {
        "foreign_keys": "on",
        "journal_mode": "wal",
        "synchronous": "off",
        "cache_size": "-64000",
        "temp_store": "memory",
        "mmap_size": "268435456",
        "wal_autocheckpoint": "10000",
        "busy_timeout": "30000",
    },
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=["daily", "orderbook"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument("--symbols", type=int, default=5_000)
    parser.add_argument("--segment-rows", type=int, default=1_000_000)
    parser.add_argument(
        "--profile",
        choices=sorted(PRAGMA_PROFILES),
        default="balanced",
    )
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="输出报告后删除本次数据库和 report.json",
    )
    return parser.parse_args()


def filesystem_type(path: Path) -> str:
    return filesystem_info(path)["filesystem"]


def _command_output(command):
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "unknown"


def filesystem_info(path: Path) -> dict:
    mount_line = _command_output(
        ["findmnt", "-no", "SOURCE,FSTYPE,OPTIONS", "-T", str(path)]
    )
    source = "unknown"
    filesystem = "unknown"
    mount_options = "unknown"
    if mount_line != "unknown":
        parts = mount_line.split(maxsplit=2)
        if parts:
            source = parts[0]
        if len(parts) >= 2:
            filesystem = parts[1]
        if len(parts) >= 3:
            mount_options = parts[2]

    block_device = {}
    if source.startswith("/dev/"):
        parent = _command_output(["lsblk", "-no", "PKNAME", source])
        physical_path = f"/dev/{parent}" if parent != "unknown" and parent else source
        block_json = _command_output(
            [
                "lsblk",
                "--json",
                "--nodeps",
                "--output",
                "NAME,ROTA,SIZE,MODEL,TRAN",
                physical_path,
            ]
        )
        if block_json != "unknown":
            try:
                devices = json.loads(block_json).get("blockdevices") or []
            except json.JSONDecodeError:
                devices = []
            if devices:
                device = devices[0]
                block_device = {
                    "name": device.get("name"),
                    "rotational": device.get("rota"),
                    "size": device.get("size"),
                    "model": device.get("model"),
                    "transport": device.get("tran"),
                }

    usage = shutil.disk_usage(path)
    return {
        "source": source,
        "filesystem": filesystem,
        "mount_options": mount_options,
        "block_device": block_device,
        "disk_total_bytes": usage.total,
        "disk_free_bytes_before": usage.free,
    }


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def daily_batches(total_rows: int, batch_size: int, symbols: int):
    start = date(2006, 1, 1)
    symbol_values = [f"S{index:06d}" for index in range(symbols)]
    base_values = [10.0 + (index % 1000) * 0.01 for index in range(symbols)]
    produced = 0
    cached_day_index = None
    cached_date = None
    while produced < total_rows:
        rows = []
        while len(rows) < batch_size and produced < total_rows:
            row_index = produced
            day_index = row_index // symbols
            symbol_index = row_index % symbols
            if day_index != cached_day_index:
                cached_day_index = day_index
                cached_date = (start + timedelta(days=day_index)).isoformat()
            base = base_values[symbol_index]
            rows.append({
                "symbol": symbol_values[symbol_index],
                "date": cached_date,
                "open": base,
                "high": base + 0.5,
                "low": base - 0.5,
                "close": base + (day_index % 11) * 0.01,
                "volume": 100_000 + row_index % 1_000_000,
                "amount": (100_000 + row_index % 1_000_000) * base,
            })
            produced += 1
        yield rows


def orderbook_batches(total_rows: int, batch_size: int, symbols: int):
    produced = 0
    base_ts = 1_767_225_600_000
    symbol_values = [f"S{index:06d}" for index in range(symbols)]
    price_values = [10.0 + (index % 1000) * 0.01 for index in range(symbols)]
    while produced < total_rows:
        size = min(batch_size, total_rows - produced)
        rows = []
        for offset in range(size):
            event_id = produced + offset
            symbol_index = event_id % symbols
            rows.append({
                "event_id": event_id,
                "symbol": symbol_values[symbol_index],
                "ts": base_ts + event_id,
                "sequence": event_id,
                "side": "B" if event_id % 2 == 0 else "A",
                "price": price_values[symbol_index],
                "size": 100 + event_id % 10_000,
            })
        produced += size
        yield rows


def create_table(db: Database, workload: str, segment_rows: int):
    if workload == "daily":
        return db.table(
            "daily",
            pk=["symbol", "date"],
            columns={
                "symbol": "text",
                "date": "text",
                "open": "real",
                "high": "real",
                "low": "real",
                "close": "real",
                "volume": "integer",
                "amount": "real",
            },
            partition="calendar(date,every=1year,tz=Asia/Shanghai)",
            order_key="date",
        )
    return db.table(
        "orderbook",
        pk=["event_id"],
        columns={
            "event_id": "integer",
            "symbol": "text",
            "ts": "integer",
            "sequence": "integer",
            "side": "text",
            "price": "real",
            "size": "integer",
        },
        partition="duration(ts,every=1day)",
        segment=Segment.rows(max_rows=segment_rows),
        order_key="ts",
        conflict="append",
    )


def validate_args(args):
    if args.rows <= 0 or args.batch_size <= 0 or args.symbols <= 0:
        raise ValueError("rows、batch-size、symbols 必须为正数")
    if args.segment_rows <= 0:
        raise ValueError("segment-rows 必须为正数")


def run_benchmark(args, emit=True):
    validate_args(args)

    args.root.mkdir(parents=True, exist_ok=True)
    storage = filesystem_info(args.root)
    run_name = (
        f"{args.workload}-{args.profile}-"
        f"r{args.rows}-b{args.batch_size}-{time.time_ns()}"
    )
    run_dir = args.root / run_name
    db_dir = run_dir / "benchmark.iosql"
    run_dir.mkdir()

    pragmas = PRAGMA_PROFILES[args.profile]
    batches = (
        daily_batches(args.rows, args.batch_size, args.symbols)
        if args.workload == "daily"
        else orderbook_batches(args.rows, args.batch_size, args.symbols)
    )

    written = 0
    commits = 0
    next_progress = args.progress_every
    started = time.perf_counter()
    write_elapsed = 0.0
    with Database(
        f"sqlite://{db_dir}",
        pragma_dict=pragmas,
    ) as db:
        table = create_table(db, args.workload, args.segment_rows)
        for rows in batches:
            write_started = time.perf_counter()
            result = table.write(rows)
            write_elapsed += time.perf_counter() - write_started
            written += result.rows_seen
            commits += 1
            if args.progress_every > 0 and written >= next_progress:
                elapsed = time.perf_counter() - started
                if emit:
                    print(
                        (
                            f"progress rows={written} "
                            f"write_rows_per_sec={written / write_elapsed:.0f} "
                            f"pipeline_rows_per_sec={written / elapsed:.0f}"
                        ),
                        flush=True,
                    )
                next_progress += args.progress_every

    elapsed = time.perf_counter() - started
    size_bytes = directory_size(run_dir)
    disk_free_after = shutil.disk_usage(args.root).free
    report = {
        "workload": args.workload,
        "root": str(args.root.resolve()),
        "run_dir": str(run_dir.resolve()),
        "run_retained": not args.cleanup,
        "environment": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            **storage,
            "disk_free_bytes_after": disk_free_after,
        },
        "filesystem": storage["filesystem"],
        "rows": written,
        "batch_size": args.batch_size,
        "write_calls": commits,
        "symbols": args.symbols,
        "segment_rows": args.segment_rows if args.workload == "orderbook" else None,
        "pragma_profile": args.profile,
        "pragma_dict": pragmas or "iosql-default-balanced",
        "elapsed_seconds": round(elapsed, 6),
        "write_elapsed_seconds": round(write_elapsed, 6),
        "rows_per_second": round(written / write_elapsed, 2),
        "pipeline_rows_per_second": round(written / elapsed, 2),
        "database_size_bytes": size_bytes,
        "database_size_mib": round(size_bytes / 1024 / 1024, 2),
        "write_mib_per_second": round(
            size_bytes / 1024 / 1024 / write_elapsed,
            2,
        ),
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
        run_benchmark(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
