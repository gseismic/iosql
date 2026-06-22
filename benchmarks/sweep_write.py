#!/usr/bin/env python3
"""扫描 iosql 写入参数并比较一个或多个目标磁盘。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.benchmark_write import PRAGMA_PROFILES, run_benchmark  # noqa: E402


def _positive_int_list(text: str, label: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in text.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} 必须是逗号分隔的整数") from error
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError(f"{label} 必须全部为正数")
    return values


def _profiles(text: str) -> list[str]:
    values = [item.strip() for item in text.split(",") if item.strip()]
    unknown = sorted(set(values) - set(PRAGMA_PROFILES))
    if not values or unknown:
        raise argparse.ArgumentTypeError(
            f"profiles 仅支持 {', '.join(sorted(PRAGMA_PROFILES))}"
        )
    return values


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=["daily", "orderbook"])
    parser.add_argument(
        "--root",
        type=Path,
        action="append",
        required=True,
        help="可重复传入，用于比较多个磁盘",
    )
    parser.add_argument("--rows", type=int, default=2_000_000)
    parser.add_argument("--symbols", type=int, default=5_000)
    parser.add_argument(
        "--batch-sizes",
        type=lambda value: _positive_int_list(value, "batch-sizes"),
        default=[20_000, 50_000, 100_000],
    )
    parser.add_argument(
        "--profiles",
        type=_profiles,
        default=["balanced", "durable"],
    )
    parser.add_argument("--segment-rows", type=int, default=1_000_000)
    parser.add_argument("--progress-every", type=int, default=0)
    parser.add_argument(
        "--keep-runs",
        action="store_true",
        help="保留每个参数组合的测试数据库；默认只保留汇总",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="汇总 JSON 路径；默认写到当前目录",
    )
    return parser.parse_args()


def _markdown_report(summary: dict) -> str:
    lines = [
        f"# iosql 写入参数扫描：{summary['workload']}",
        "",
        (
            f"- rows: `{summary['rows']}`"
            f"，symbols: `{summary['symbols']}`"
            f"，segment_rows: `{summary['segment_rows']}`"
        ),
        "",
        "| 排名 | 根目录 | 文件系统 | profile | batch | rows/s | pipeline rows/s | MiB/s |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    ranked = sorted(
        summary["runs"],
        key=lambda item: item["rows_per_second"],
        reverse=True,
    )
    for index, report in enumerate(ranked, 1):
        lines.append(
            "| "
            f"{index} | `{report['root']}` | {report['filesystem']} | "
            f"{report['pragma_profile']} | {report['batch_size']} | "
            f"{report['rows_per_second']:.0f} | "
            f"{report['pipeline_rows_per_second']:.0f} | "
            f"{report['write_mib_per_second']:.2f} |"
        )

    lines.extend(["", "## 每个磁盘/profile 的最佳批次", ""])
    best = {}
    for report in ranked:
        key = (report["root"], report["pragma_profile"])
        best.setdefault(key, report)
    for (root, profile), report in best.items():
        lines.append(
            f"- `{root}` / `{profile}`: batch `{report['batch_size']}`，"
            f"`{report['rows_per_second']:.0f}` rows/s"
        )
    return "\n".join(lines) + "\n"


def run_sweep(args):
    if args.rows <= 0 or args.symbols <= 0 or args.segment_rows <= 0:
        raise ValueError("rows、symbols、segment-rows 必须为正数")

    reports = []
    total = len(args.root) * len(args.profiles) * len(args.batch_sizes)
    current = 0
    for profile in args.profiles:
        for batch_size in args.batch_sizes:
            for root in args.root:
                current += 1
                print(
                    f"[{current}/{total}] root={root} profile={profile} "
                    f"batch={batch_size}",
                    flush=True,
                )
                report = run_benchmark(
                    SimpleNamespace(
                        workload=args.workload,
                        root=root,
                        rows=args.rows,
                        batch_size=batch_size,
                        symbols=args.symbols,
                        segment_rows=args.segment_rows,
                        profile=profile,
                        progress_every=args.progress_every,
                        cleanup=not args.keep_runs,
                    ),
                    emit=False,
                )
                reports.append(report)
                print(
                    f"  rows/s={report['rows_per_second']:.0f} "
                    f"MiB/s={report['write_mib_per_second']:.2f}",
                    flush=True,
                )

    summary = {
        "workload": args.workload,
        "rows": args.rows,
        "symbols": args.symbols,
        "segment_rows": (
            args.segment_rows if args.workload == "orderbook" else None
        ),
        "batch_sizes": args.batch_sizes,
        "profiles": args.profiles,
        "roots": [str(root.resolve()) for root in args.root],
        "runs": reports,
    }
    output = args.output or Path(
        f"iosql-{args.workload}-sweep-{int(time.time())}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text(_markdown_report(summary), encoding="utf-8")
    print(f"json={output.resolve()}")
    print(f"markdown={markdown_path.resolve()}")
    return summary


def main():
    args = parse_args()
    try:
        run_sweep(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
