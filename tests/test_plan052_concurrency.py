"""PLAN-052 并发与多场景压力测试。

覆盖：多线程写同一表（不同/相同分区）、并发 evolve 加不同新列、
读并发于演进、首次分区创建竞争、LRU 驱逐下的并发写入、
latest 并发读、Case 归一化线程安全、独立 buffered_writer。
"""

import threading

import pytest

from iosql import Database, Partition, Segment
from iosql.core.query import Case
from iosql.exception import BadArgument


DAY = 86_400_000
BASE_TS = 1735689600000


def _uri(tmp_path, name="conc.iosql"):
    return f"sqlite://{tmp_path}/{name}"


def _join_all(threads, timeout=60):
    for thread in threads:
        thread.join(timeout=timeout)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, "threads deadlocked"


def _collect_errors(errors):
    assert not errors, f"worker threads raised: {errors[:3]}"


# ---------------------------------------------------------------
# 多线程写：不同分区
# ---------------------------------------------------------------

def test_concurrent_writers_different_partitions(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
        )
        barrier = threading.Barrier(8)
        errors = []

        def writer(day):
            try:
                barrier.wait(timeout=15)
                ticks.write([{
                    "symbol": f"S{day}",
                    "ts": BASE_TS + day * DAY,
                    "price": float(day),
                }])
            except Exception as e:  # pragma: no cover - 诊断用
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(day,)) for day in range(8)
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        assert len(ticks.table_parts()) == 8
        assert len(ticks.select()) == 8


# ---------------------------------------------------------------
# 多线程写：相同分区（SQLite 串行化）
# ---------------------------------------------------------------

def test_concurrent_writers_same_partition(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
        )
        barrier = threading.Barrier(6)
        errors = []

        def worker(index):
            try:
                barrier.wait(timeout=15)
                ticks.write([{
                    "symbol": f"S{index}",
                    "ts": BASE_TS + index,
                    "price": float(index),
                }])
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(6)
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        assert len(ticks.select()) == 6
        assert len(ticks.segments()) == 1  # 未配置 segment，单文件属正确行为


# ---------------------------------------------------------------
# 并发 evolve：两个线程同时给不同新分区带各自新列
# ---------------------------------------------------------------

def test_concurrent_evolve_add_distinct_columns(tmp_path):
    for round_index in range(3):
        uri = _uri(tmp_path, f"evolve_race_{round_index}.iosql")
        with Database(uri) as db:
            factors = db.table(
                "factors", pk=["symbol", "ts"],
                columns={"symbol": "text", "ts": "integer"},
                partition=Partition.duration("ts", every="1day"),
            )
            factors.write([{"symbol": "S", "ts": BASE_TS}])

            barrier = threading.Barrier(2)
            errors = []

            def writer(column_name, day):
                try:
                    barrier.wait(timeout=15)
                    factors.write([{
                        "symbol": "S",
                        "ts": BASE_TS + day * DAY,
                        column_name: 1.5,
                    }])
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            threads = [
                threading.Thread(target=writer, args=("f1", 1)),
                threading.Thread(target=writer, args=("f2", 2)),
            ]
            for t in threads:
                t.start()
            _join_all(threads)
            _collect_errors(errors)

            rows = factors.select(
                columns=["symbol", "ts", "f1", "f2"],
                to_dict=True,
                order_by="ts",
            )
            by_ts = {row["ts"]: row for row in rows}
            assert len(by_ts) == 3
            assert by_ts[BASE_TS]["f1"] is None
            assert by_ts[BASE_TS]["f2"] is None
            assert by_ts[BASE_TS + DAY]["f1"] == 1.5
            assert by_ts[BASE_TS + DAY]["f2"] is None
            assert by_ts[BASE_TS + 2 * DAY]["f1"] is None
            assert by_ts[BASE_TS + 2 * DAY]["f2"] == 1.5
            assert {"f1", "f2"} <= set(factors.spec().columns)

        with Database(uri) as db:
            columns = db.table("factors").spec().columns
            assert {"f1", "f2"} <= set(columns)


def test_concurrent_evolve_same_partition_new_columns(tmp_path):
    """同分区 NoSuchColumn 路径的并发加列。"""
    with Database(_uri(tmp_path)) as db:
        table = db.table(
            "t", pk=["id"], columns={"id": "integer"},
            partition=Partition.range("id", every=10),
        )
        table.write([{"id": 1}])

        barrier = threading.Barrier(2)
        errors = []

        def writer(column_name, value):
            try:
                barrier.wait(timeout=15)
                table.write([{"id": 100 + value, column_name: value}])
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("a", 1)),
            threading.Thread(target=writer, args=("b", 2)),
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        rows = {r["id"]: r for r in table.select(to_dict=True)}
        assert rows[101]["a"] == 1 and rows[101].get("b") is None
        assert rows[102]["b"] == 2 and rows[102].get("a") is None
        assert {"a", "b"} <= set(table.spec().columns)


# ---------------------------------------------------------------
# 读并发于演进
# ---------------------------------------------------------------

def test_reader_during_evolve(tmp_path):
    with Database(_uri(tmp_path)) as db:
        events = db.table(
            "events", pk=["id"],
            columns={"id": "integer"},
            segment=Segment.rows(max_rows=50),
        )
        events.write([
            {"id": i} for i in range(40)
        ])

        stop = threading.Event()
        errors = []
        reads = []

        def reader():
            try:
                while not stop.is_set():
                    rows = events.select(to_dict=True)
                    reads.append(len(rows))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

        try:
            for batch in range(6):
                events.write([
                    {"id": 1000 + batch * 10 + i, f"col_{batch}": batch}
                    for i in range(10)
                ])
        finally:
            stop.set()
            reader_thread.join(timeout=30)

        _collect_errors(errors)
        assert reads, "reader made no progress"
        assert all(count >= 40 for count in reads)
        assert len(events.select()) == 100
        for batch in range(6):
            assert f"col_{batch}" in events.spec().columns


# ---------------------------------------------------------------
# 首次分区创建竞争 + LRU 驱逐
# ---------------------------------------------------------------

def test_concurrent_first_partition_creation(tmp_path):
    with Database(_uri(tmp_path)) as db:
        items = db.table(
            "items", pk=["id"],
            columns={"id": "integer"},
            partition=Partition.range("id", every=100),
        )
        barrier = threading.Barrier(10)
        errors = []

        def writer(index):
            try:
                barrier.wait(timeout=15)
                items.write([{"id": index * 100 + 1}])
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        assert len(items.table_parts()) == 10
        assert len(items.select()) == 10


def test_lru_eviction_under_concurrent_writes(tmp_path):
    with Database(
        _uri(tmp_path), max_open_partitions=2,
    ) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
        )
        days = 16
        barrier = threading.Barrier(4)
        errors = []

        def writer(worker):
            try:
                barrier.wait(timeout=15)
                for day in range(worker, days, 4):
                    ticks.write([{
                        "symbol": f"W{worker}",
                        "ts": BASE_TS + day * DAY,
                        "price": float(day),
                    }])
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(w,)) for w in range(4)
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        assert len(ticks.table_parts()) == days
        assert len(ticks.select()) == days


# ---------------------------------------------------------------
# latest/asof 并发只读 + 独立 buffered_writer
# ---------------------------------------------------------------

def test_latest_concurrent_reads_during_writes(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.timeseries(
            "ticks", "ts", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
        )
        ticks.write([
            {"symbol": "AAPL", "ts": BASE_TS, "price": 1.0},
        ])

        stop = threading.Event()
        errors = []

        def reader():
            try:
                while not stop.is_set():
                    snapshot = ticks.latest(
                        symbols="AAPL", by=["symbol"],
                    ).to_list()
                    assert snapshot, "latest returned no rows"
            except Exception as e:  # pragma: no cover
                errors.append(e)

        readers = [threading.Thread(target=reader) for _ in range(3)]
        for t in readers:
            t.start()
        try:
            for i in range(20):
                ticks.write([{
                    "symbol": "AAPL",
                    "ts": BASE_TS + (i + 1) * 1000,
                    "price": float(i + 2),
                }])
        finally:
            stop.set()
            for t in readers:
                t.join(timeout=30)
        _collect_errors(errors)


def test_buffered_writer_separate_threads(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
        )
        barrier = threading.Barrier(3)
        errors = []

        def buffered_writer(worker):
            try:
                barrier.wait(timeout=15)
                with ticks.buffered_writer(max_rows=5) as buffer:
                    for i in range(12):
                        buffer.append({
                            "symbol": f"W{worker}",
                            "ts": BASE_TS + worker * DAY + i,
                            "price": float(i),
                        })
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=buffered_writer, args=(w,)) for w in range(3)
        ]
        for t in threads:
            t.start()
        _join_all(threads)
        _collect_errors(errors)

        assert len(ticks.select()) == 36


# ---------------------------------------------------------------
# Case 归一化线程安全 + 场景边界
# ---------------------------------------------------------------

def test_case_normalization_thread_safety():
    barrier = threading.Barrier(16)
    errors = []

    def builder(index):
        try:
            barrier.wait(timeout=15)
            if index % 5 == 4:
                with pytest.raises(BadArgument):
                    Case("f", "bad_op", 1)
                return
            samples = [("=", 1), ("gt", 1), ("between", [1, 2]), ("in", [1, 2])]
            op, value = samples[index % 4]
            case = Case("f", op, value)
            assert case.op in ("==", ">", "between_and", "in")
        except Exception as e:  # pragma: no cover - 诊断用
            errors.append(e)

    threads = [
        threading.Thread(target=builder, args=(i,)) for i in range(16)
    ]
    for t in threads:
        t.start()
    _join_all(threads)
    _collect_errors(errors)


def test_iter_select_limit_and_columns_combo(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
        )
        ticks.write([
            {"symbol": f"S{i}", "ts": i, "price": float(i)}
            for i in range(7)
        ])
        rows = list(ticks.iter_select(
            columns=["symbol"], to_dict=True, batch_size=3, limit=5,
        ))
        assert len(rows) == 5
        assert all(set(row) == {"symbol"} for row in rows)


def test_select_empty_table_and_none_where(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
        )
        assert ticks.select() == []
        assert ticks.select(where=None) == []
        plan = ticks.explain_select()
        assert plan.scanned_partitions == []


def test_timeseries_calendar_partition_tz_consistency(tmp_path):
    with Database(_uri(tmp_path)) as db:
        events = db.timeseries(
            "events", "event_date", pk=["event_date"],
            columns={"event_date": "text", "v": "float"},
            partition=Partition.calendar(
                "event_date", every="1day", timezone="Asia/Shanghai",
            ),
        )
        spec = events.spec()
        assert spec.time.timezone == "Asia/Shanghai"
        assert spec.time.unit == "date"
