from types import SimpleNamespace

from benchmarks.benchmark_write import daily_batches, run_benchmark
from benchmarks.sweep_write import (
    _markdown_report,
    _positive_int_list,
    _profiles,
)


def test_daily_batches_reuse_trading_date_per_symbol_group():
    rows = next(daily_batches(total_rows=4, batch_size=4, symbols=2))

    assert [row["date"] for row in rows] == [
        "2006-01-01",
        "2006-01-01",
        "2006-01-02",
        "2006-01-02",
    ]


def test_sweep_argument_parsers():
    assert _positive_int_list("20000,50000", "batch-sizes") == [20_000, 50_000]
    assert _profiles("balanced,durable") == ["balanced", "durable"]


def test_markdown_report_orders_fastest_run_first():
    summary = {
        "workload": "daily",
        "rows": 100,
        "symbols": 10,
        "segment_rows": None,
        "runs": [
            {
                "root": "/slow",
                "filesystem": "ntfs",
                "pragma_profile": "balanced",
                "batch_size": 20,
                "rows_per_second": 100.0,
                "pipeline_rows_per_second": 90.0,
                "write_mib_per_second": 1.0,
            },
            {
                "root": "/fast",
                "filesystem": "ext4",
                "pragma_profile": "balanced",
                "batch_size": 50,
                "rows_per_second": 200.0,
                "pipeline_rows_per_second": 180.0,
                "write_mib_per_second": 2.0,
            },
        ],
    }

    markdown = _markdown_report(summary)

    assert markdown.index("`/fast`") < markdown.index("`/slow`")


def test_run_benchmark_reports_environment_and_cleans_up(tmp_path):
    report = run_benchmark(
        SimpleNamespace(
            workload="daily",
            root=tmp_path,
            rows=100,
            batch_size=50,
            symbols=10,
            segment_rows=1000,
            profile="balanced",
            progress_every=0,
            cleanup=True,
        ),
        emit=False,
    )

    assert report["rows"] == 100
    assert report["run_retained"] is False
    assert report["environment"]["python_version"]
    assert report["environment"]["sqlite_version"]
    assert not tmp_path.joinpath(report["run_dir"]).exists()
