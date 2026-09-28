"""PLAN-072：破坏性操作、分区覆盖与 Web 数据正确性回归。"""

import os
import sqlite3
import multiprocessing as mp

import pytest
from typer.testing import CliRunner

from iosql import Database, Partition, Segment, TableContract
from iosql.cli import app
from iosql.exception import BadArgument, UnsafeOperation
from iosql.utils.storage_identity import is_iosql_database


def _db(path):
    return Database(f"sqlite://{path}")


def _table(db, name, value=1):
    table = db.table(name, pk=["id"], columns={"id": "integer", "v": "integer"})
    table.write({"id": 1, "v": value})
    return table


def _concurrent_segment_writer(uri, row_id, ready, second_done, result):
    from iosql.engines.sqlite.table import Table

    try:
        db = Database(uri)
        table = db.table("t")
        if row_id == 2:
            original = Table._active_segment_for

            def wait_after_read(self, *args, **kwargs):
                active = original(self, *args, **kwargs)
                ready.set()
                second_done.wait(timeout=1)
                return active

            Table._active_segment_for = wait_after_read
        else:
            ready.wait(timeout=5)
        table.write({"id": row_id})
        db.close()
        result.put((row_id, "ok"))
    except Exception as exc:
        result.put((row_id, type(exc).__name__, str(exc)))
    finally:
        if row_id == 3:
            second_done.set()


def _compact_or_write(uri, role, compact_read, writer_done, result):
    try:
        db = Database(uri)
        table = db.table("t")
        if role == "compact":
            original = table._write_compact_chunks

            def wait_after_read(*args, **kwargs):
                compact_read.set()
                writer_done.wait(timeout=1)
                return original(*args, **kwargs)

            table._write_compact_chunks = wait_after_read
            table.compact()
        else:
            compact_read.wait(timeout=5)
            table.write({"id": 2})
        db.close()
        result.put((role, "ok"))
    except Exception as exc:
        result.put((role, type(exc).__name__, str(exc)))
    finally:
        if role == "write":
            writer_done.set()


def test_direct_rename_rolls_back_and_rejects_ordinary_directory(tmp_path, monkeypatch):
    path = tmp_path / "db.iosql"
    db = _db(path)
    try:
        source = _table(db, "source")
        _table(db, "target", 2)
        ordinary = path / "notes"
        ordinary.mkdir()
        (ordinary / "keep.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(UnsafeOperation):
            source.rename("notes", overwrite=True)
        with pytest.raises(UnsafeOperation):
            db.rename_table("source", "notes", overwrite=True)
        assert (ordinary / "keep.txt").read_text() == "keep"

        original = os.replace

        def fail_source_move(src, dst):
            if str(src).endswith("/source") and str(dst).endswith("/target"):
                raise OSError("move failed")
            return original(src, dst)

        monkeypatch.setattr(os, "replace", fail_source_move)
        with pytest.raises(BadArgument, match="重命名表失败"):
            source.rename("target", overwrite=True)
        monkeypatch.setattr(os, "replace", original)
        assert db.table("source").select()[0]["v"] == 1
        assert db.table("target").select()[0]["v"] == 2
        source.rename("new")
        assert db.table("new") is source
        assert db.table("source") is not source
    finally:
        db.close()


def test_direct_rename_overwrite_updates_owner_cache(tmp_path):
    db = _db(tmp_path / "overwrite.iosql")
    try:
        source = _table(db, "source", 10)
        target = _table(db, "target", 20)
        assert source.rename("target", overwrite=True) is source
        assert db.table("target") is source
        assert db.table("source") is not source
        assert db.table("target").select()[0]["v"] == 10
        assert target is not source
    finally:
        db.close()


def test_direct_rename_manifest_failure_rolls_back_both_tables(tmp_path, monkeypatch):
    db = _db(tmp_path / "manifest-failure.iosql")
    try:
        source = _table(db, "source", 10)
        _table(db, "target", 20)
        original = source._rename_table_manifest

        def fail_forward(old, new):
            if old == "source":
                raise OSError("manifest write failed")
            return original(old, new)

        monkeypatch.setattr(source, "_rename_table_manifest", fail_forward)
        with pytest.raises(BadArgument, match="重命名表失败"):
            source.rename("target", overwrite=True)
        assert db.table_names() == ["source", "target"]
        assert db.table("source").select()[0]["v"] == 10
        assert db.table("target").select()[0]["v"] == 20
    finally:
        db.close()


def test_restore_rejects_bad_backup_and_false_target(tmp_path):
    source_path = tmp_path / "source.iosql"
    db = _db(source_path)
    _table(db, "ticks")
    db.close()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "note.txt").write_text("original", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, [
        "restore", "--backup", str(ordinary), "--target", str(source_path),
        "--replace", "--yes",
    ])
    assert result.exit_code == 1
    assert is_iosql_database(source_path, deep=True)

    fake = ordinary / "fake"
    fake.mkdir()
    conn = sqlite3.connect(fake / "meta.sqlite3")
    conn.execute("CREATE TABLE unrelated(x)")
    conn.close()
    result = runner.invoke(app, [
        "restore", "--backup", str(source_path), "--target", str(ordinary),
        "--replace", "--yes",
    ])
    assert result.exit_code == 1
    assert (ordinary / "note.txt").read_text() == "original"

    missing = next((source_path / "ticks" / "data").glob("*.sqlite3"))
    missing.unlink()
    result = runner.invoke(app, [
        "restore", "--backup", str(source_path),
        "--target", str(tmp_path / "restored.iosql"),
    ])
    assert result.exit_code == 1
    assert not (tmp_path / "restored.iosql").exists()


def test_restore_rejects_manifest_row_count_mismatch(tmp_path):
    source_path = tmp_path / "bad-backup.iosql"
    target_path = tmp_path / "target.iosql"
    for path in (source_path, target_path):
        db = _db(path)
        db.table(
            "t", pk=["id"], columns={"id": "integer"}, segment=Segment.rows(2),
        ).write({"id": 1})
        db.close()
    conn = sqlite3.connect(source_path / "t" / "meta.sqlite3")
    conn.execute('UPDATE "_iosql_segments" SET row_count = 0')
    conn.commit()
    conn.close()
    result = CliRunner().invoke(app, [
        "restore", "--backup", str(source_path), "--target", str(target_path),
        "--replace", "--yes",
    ])
    assert result.exit_code == 1
    db = _db(target_path)
    try:
        assert db.table("t").count_rows() == 1
    finally:
        db.close()


def test_partition_upsert_rejected_before_write(tmp_path):
    db = _db(tmp_path / "parts.iosql")
    try:
        table = db.table(
            "t", pk=["id"], partition=Partition.range("bucket", every=10),
            columns={"id": "integer", "bucket": "integer", "v": "integer"},
        )
        table.write({"id": 1, "bucket": 1, "v": 10})
        with pytest.raises(BadArgument, match="分区列属于主键"):
            table.write({"id": 1, "bucket": 11, "v": 20}, mode="upsert")
        with pytest.raises(BadArgument, match="分区列属于主键"):
            table.insert_all([{"id": 1, "bucket": 11, "v": 20}], replace=True)
        assert table.count_rows() == 1
    finally:
        db.close()


def test_spec_force_partition_preserved_and_conflict_rejected(tmp_path):
    db = _db(tmp_path / "force.iosql")
    contract = TableContract(
        primary_key=["id"], columns={"id": "integer"}, force_partition=True,
    )
    try:
        table = db.table("t", spec=contract)
        assert table.spec().force_partition is True
        with pytest.raises(BadArgument, match="force_partition.*冲突"):
            db.table("t", spec=contract, force_partition=False)
    finally:
        db.close()


def test_table_names_ignore_ordinary_directory_without_writing(tmp_path):
    path = tmp_path / "db.iosql"
    db = _db(path)
    try:
        _table(db, "real")
        notes = path / "notes"
        notes.mkdir()
        (notes / "readme.txt").write_text("x")
        assert db.table_names() == ["real"]
        assert db.table("real").table_names() == ["real"]
        with pytest.raises(UnsafeOperation, match="拒绝在其中创建元数据"):
            db.table("notes")
        assert not (notes / "meta.sqlite3").exists()
    finally:
        db.close()


def test_unwritten_table_scaffold_can_be_reopened(tmp_path):
    path = tmp_path / "scaffold.iosql"
    db = _db(path)
    db.table("t", pk=["id"], columns={"id": "integer"})
    db.close()
    db = _db(path)
    try:
        assert db.table_names() == []
        db.table("t", pk=["id"], columns={"id": "integer"}).write({"id": 1})
        assert db.table_names() == ["t"]
    finally:
        db.close()


def test_numeric_text_buckets_and_overflow(tmp_path):
    db = _db(tmp_path / "chart.iosql")
    try:
        table = db.table(
            "t", pk=["id"], order_key="x",
            columns={"id": "integer", "x": "text", "v": "integer"},
        )
        table.write([
            {"id": 1, "x": "2", "v": 2},
            {"id": 2, "x": "10", "v": 10},
            {"id": 3, "x": "3", "v": 3},
        ])
        agg = table.aggregate_by_bucket("x", ["v"], 4)
        assert (agg["x_min"], agg["x_max"]) == ("2", "10")
        assert sum(agg["counts"]["v"]) == 3
        try:
            from fastapi.testclient import TestClient
            from iosql.web import create_app
        except ImportError:
            pass
        else:
            with TestClient(create_app(f"sqlite://{tmp_path / 'chart.iosql'}"), base_url="http://localhost") as client:
                response = client.get("/api/table/t/chart", params={
                    "col": "v", "kind": "line", "buckets": 4,
                })
                assert response.status_code == 200, response.text
                assert response.json()["y"] == [2.5, 10.0]
        table.write({"id": 5, "x": "-4", "v": 5})
        negative = table.aggregate_by_bucket("x", ["v"], 4)
        assert negative["x_min"] == "-4"
        assert sum(negative["counts"]["v"]) == 4
        table.write({"id": 4, "x": "9223372036854775808", "v": 4})
        with pytest.raises(BadArgument, match="64 位"):
            table.aggregate_by_bucket("x", ["v"], 4)
    finally:
        db.close()


def test_web_candle_order_pagination_and_sort_validation(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from iosql.web import create_app

    path = tmp_path / "web.iosql"
    db = _db(path)
    table = db.table(
        "bars", pk=["id"], order_key="ts",
        columns={
            "id": "integer", "ts": "integer", "open": "integer",
            "high": "integer", "low": "integer", "close": "integer",
        },
    )
    table.write([
        {"id": 3, "ts": 3, "open": 30, "high": 30, "low": 30, "close": 30},
        {"id": 1, "ts": 1, "open": 10, "high": 10, "low": 10, "close": 10},
        {"id": 2, "ts": 2, "open": 20, "high": 20, "low": 20, "close": 20},
        {"id": 4, "ts": 3, "open": 31, "high": 31, "low": 31, "close": 31},
    ])
    # 无主键表只能由底层入口构造，Web 不得给出会漏行的游标。
    db.table("nopk").insert_all([
        {"ts": 1, "v": "a"}, {"ts": 1, "v": "b"},
    ])
    db.close()
    with TestClient(create_app(f"sqlite://{path}"), base_url="http://localhost") as client:
        response = client.get("/api/table/bars/chart", params={
            "col": "close", "kind": "candle", "buckets": 1,
        })
        assert response.status_code == 200, response.text
        assert response.json()["open"] == [10]
        assert response.json()["close"] == [31]
        assert client.post("/api/table/nopk/rows", json={
            "order_by": "ts", "limit": 1,
        }).status_code == 422
        assert client.post("/api/table/bars/rows", json={
            "order_by": ",", "limit": 1,
        }).status_code == 422
        assert client.post("/api/table/bars/rows", json={
            "order_by": "ts desc extra", "limit": 1,
        }).status_code == 422


def test_explicit_nonloopback_trusted_host(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from iosql.web import create_app

    path = tmp_path / "host.iosql"
    db = _db(path)
    _table(db, "t")
    db.close()
    app = create_app(f"sqlite://{path}", allowed_hosts=["192.0.2.5"])
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/", headers={"host": "192.0.2.5"}).status_code == 200
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_cli_web_host_sets_host_allowlist(tmp_path, monkeypatch):
    path = tmp_path / "host-cli.iosql"
    db = _db(path)
    _table(db, "t")
    db.close()
    seen = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: seen.append((app, kwargs)))
    runner = CliRunner()
    invalid = runner.invoke(app, [
        "web", "--source", str(path), "--host", "0.0.0.0", "--no-browser",
    ])
    assert invalid.exit_code != 0
    assert not seen
    valid = runner.invoke(app, [
        "web", "--source", str(path), "--host", "192.0.2.5", "--no-browser",
    ])
    assert valid.exit_code == 0, valid.output
    from fastapi.testclient import TestClient
    with TestClient(seen[0][0], base_url="http://localhost") as client:
        assert client.get("/", headers={"host": "192.0.2.5"}).status_code == 200
    assert seen[0][1]["host"] == "192.0.2.5"
    wildcard = runner.invoke(app, [
        "web", "--source", str(path), "--host", "0.0.0.0",
        "--allowed-host", "example.internal", "--no-browser",
    ])
    assert wildcard.exit_code == 0, wildcard.output
    with TestClient(seen[1][0], base_url="http://localhost") as client:
        assert client.get("/", headers={"host": "example.internal"}).status_code == 200
        assert client.get("/", headers={"host": "unlisted.internal"}).status_code == 400


def test_two_process_segment_writes_keep_manifest_consistent(tmp_path):
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("需要 fork 进程测试表级文件锁")
    path = tmp_path / "concurrent.iosql"
    uri = f"sqlite://{path}"
    db = Database(uri)
    table = db.table(
        "t", pk=["id"], columns={"id": "integer"}, segment=Segment.rows(2),
    )
    table.write({"id": 1})
    db.close()

    context = mp.get_context("fork")
    ready = context.Event()
    second_done = context.Event()
    result = context.Queue()
    workers = [context.Process(
        target=_concurrent_segment_writer,
        args=(uri, row_id, ready, second_done, result),
    ) for row_id in (2, 3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert sorted(result.get(timeout=2) for _ in workers) == [(2, "ok"), (3, "ok")]
    db = Database(uri)
    try:
        table = db.table("t")
        assert {row["id"] for row in table.select()} == {1, 2, 3}
        assert table.count_rows() == 3
        assert table.validate(deep=True).ok
    finally:
        db.close()


def test_two_process_compact_and_write_do_not_lose_rows(tmp_path):
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("需要 fork 进程测试表级文件锁")
    path = tmp_path / "compact-concurrent.iosql"
    uri = f"sqlite://{path}"
    db = Database(uri)
    db.table("t", pk=["id"], columns={"id": "integer"}, segment=Segment.rows(2)).write({"id": 1})
    db.close()
    context = mp.get_context("fork")
    compact_read = context.Event()
    writer_done = context.Event()
    result = context.Queue()
    workers = [context.Process(
        target=_compact_or_write,
        args=(uri, role, compact_read, writer_done, result),
    ) for role in ("compact", "write")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert sorted(result.get(timeout=2) for _ in workers) == [
        ("compact", "ok"), ("write", "ok"),
    ]
    db = Database(uri)
    try:
        table = db.table("t")
        assert {row["id"] for row in table.select()} == {1, 2}
        assert table.validate(deep=True).ok
    finally:
        db.close()
