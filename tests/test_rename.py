import json

from typer.testing import CliRunner

from iosql import Database, Partition, Segment
from iosql.cli import app
from iosql.exception import BadArgument


runner = CliRunner()


def _uri(path):
    return f"sqlite://{path}"


def _sample_rows():
    return [
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689601000, "price": 420.3},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776001000, "price": 421.0},
    ]


def _make_segmented_table(db, table_name="ticks"):
    table = db.table(
        table_name,
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    table.write(_sample_rows())
    return table


def test_database_rename_moves_directory_and_rebinds_open_table(tmp_path):
    source = tmp_path / "market.iosql"
    target = tmp_path / "renamed.iosql"
    db = Database(_uri(source))
    table = db.table(
        "kv",
        pk=["key"],
        columns={"key": "text", "val": "integer"},
    )
    table.write({"key": "a", "val": 1})

    db.rename("renamed.iosql")

    assert not source.exists()
    assert target.exists()
    assert db.uri == _uri(target.resolve())
    assert db.table_names() == ["kv"]
    assert table.select("*", where=None, to_dict=True) == [{"key": "a", "val": 1}]
    assert db.table("kv").select("*", where=None, to_dict=True) == [
        {"key": "a", "val": 1},
    ]
    db.close()

    reopened = Database(_uri(target))
    try:
        assert reopened.table("kv").select("*", where=None, to_dict=True) == [
            {"key": "a", "val": 1},
        ]
    finally:
        reopened.close()


def test_table_rename_preserves_partition_segment_manifest(tmp_path):
    db_path = tmp_path / "market.iosql"
    db = Database(_uri(db_path))
    table = _make_segmented_table(db)
    old_path = db_path / "ticks"
    new_path = db_path / "bars"

    renamed = db.rename_table("ticks", "bars")

    assert renamed is table
    assert renamed.table_name == "bars"
    assert not old_path.exists()
    assert new_path.exists()
    assert db.table_names() == ["bars"]
    assert renamed.spec().primary_key == ["symbol", "ts"]
    assert len(renamed.table_parts()) == 2
    assert len(renamed.segments()) == 2
    assert renamed.validate(deep=True).ok is True
    assert {
        row["table_name"]
        for row in renamed.get_part_column_ranges()
    } == {"bars"}

    rows = renamed.select("*", where=None, order_by="ts", to_dict=True)
    assert [row["symbol"] for row in rows] == ["AAPL", "MSFT", "AAPL", "MSFT"]
    db.close()

    reopened = Database(_uri(db_path))
    try:
        assert reopened.table_names() == ["bars"]
        reopened_table = reopened.table("bars")
        assert reopened_table.validate(deep=True).ok is True
        assert len(reopened_table.segments()) == 2
    finally:
        reopened.close()


def test_table_rename_overwrite_existing_target(tmp_path):
    db = Database(_uri(tmp_path / "market.iosql"))
    source = db.table(
        "source",
        pk=["id"],
        columns={"id": "integer", "name": "text"},
    )
    source.write({"id": 1, "name": "source"})
    target = db.table(
        "target",
        pk=["id"],
        columns={"id": "integer", "name": "text"},
    )
    target.write({"id": 2, "name": "target"})

    renamed = db.rename_table("source", "target", overwrite=True)

    assert renamed.table_name == "target"
    assert db.table_names() == ["target"]
    assert renamed.select("*", where=None, to_dict=True) == [
        {"id": 1, "name": "source"},
    ]
    db.close()


def test_table_rename_missing_table_does_not_create_directory(tmp_path):
    db_path = tmp_path / "market.iosql"
    db = Database(_uri(db_path))

    try:
        try:
            db.rename_table("missing", "renamed")
        except BadArgument as exc:
            assert "does not exist" in str(exc)
        else:
            raise AssertionError("rename_table should reject a missing table")
        assert not (db_path / "missing").exists()
        assert not (db_path / "renamed").exists()
    finally:
        db.close()


def test_cli_rename_database_and_table(tmp_path):
    source = tmp_path / "market.iosql"
    db = Database(_uri(source))
    _make_segmented_table(db)
    db.close()

    result = runner.invoke(
        app,
        [
            "rename-table",
            "-s",
            str(source),
            "-t",
            "ticks",
            "--new-table",
            "bars",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["new_table"] == "bars"

    target = tmp_path / "renamed.iosql"
    result = runner.invoke(
        app,
        [
            "rename-database",
            "-s",
            str(source),
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target_uri"] == _uri(target.resolve())
    assert not source.exists()
    assert target.exists()

    reopened = Database(_uri(target))
    try:
        assert reopened.table_names() == ["bars"]
        assert reopened.table("bars").count_rows() == 4
    finally:
        reopened.close()
