import json
import os

from typer.testing import CliRunner

from iosql.cli import app
from iosql import Database, Partition, Segment


runner = CliRunner()


def _db_uri(path):
    return f"sqlite://{path}"


def _make_sample_db(tmp_path):
    path = tmp_path / "market.iosql"
    db = Database(_db_uri(path))
    table = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    table.write([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689601000, "price": 420.3},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776001000, "price": 421.0},
    ])
    db.close()
    return path


def _invoke(*args):
    return runner.invoke(app, list(args))


def test_cli_help_lists_operational_commands():
    result = _invoke("--help")
    assert result.exit_code == 0, result.output
    assert "tables" in result.output, result.output
    assert "repartition" in result.output
    assert "index" in result.output


def test_cli_rejects_non_sqlite_uri():
    result = _invoke("tables", "-s", "postgresql://user:pass@localhost:5432/db")
    assert result.exit_code == 1, result.output
    assert "只支持" in result.output


def test_cli_read_only_inspection_commands(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("tables", "-s", str(db_path))
    assert result.exit_code == 0, result.output
    assert "ticks" in result.output

    result = _invoke("spec", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    spec = json.loads(result.output)
    assert spec["primary_key"] == ["symbol", "ts"]
    assert spec["partition"]["every"] == 86_400_000

    result = _invoke("layout", "-s", str(db_path), "-t", "ticks", "--summary")
    assert result.exit_code == 0, result.output
    layout = json.loads(result.output)
    assert layout["total_rows"] == 4
    assert layout["total_partitions"] == 2

    result = _invoke("parts", "-s", str(db_path), "-t", "ticks", "--json")
    assert result.exit_code == 0, result.output
    parts = json.loads(result.output)
    assert len(parts["parts"]) == 2

    result = _invoke("segments", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    segments = json.loads(result.output)
    assert len(segments) == 2


def test_cli_explain_query_export_and_import(tmp_path):
    db_path = _make_sample_db(tmp_path)
    where = json.dumps({"field": "symbol", "op": "==", "value": "AAPL"})

    result = _invoke(
        "explain",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        where,
        "--order-by",
        "ts",
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["table"] == "ticks"
    assert plan["scanned_partitions"]

    result = _invoke(
        "query",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        where,
        "--order-by",
        "ts",
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["symbol"] for row in rows] == ["AAPL", "AAPL"]

    export_path = tmp_path / "aapl.jsonl"
    result = _invoke(
        "export",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-o",
        str(export_path),
        "--where",
        where,
    )
    assert result.exit_code == 0, result.output
    assert export_path.exists()
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 2

    import_path = tmp_path / "new_rows.jsonl"
    import_path.write_text(
        json.dumps({"symbol": "GOOG", "ts": 1735862400000, "price": 150.0}) + "\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(import_path),
        "--mode",
        "append",
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["rows_seen"] == 1

    result = _invoke(
        "query",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        json.dumps({"field": "symbol", "op": "==", "value": "GOOG"}),
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["price"] == 150.0


def test_cli_doctor_fix_and_index_check(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("doctor", "-s", str(db_path), "-t", "ticks", "--fix")
    assert result.exit_code == 0, result.output
    assert "fix: ok" in result.output

    result = _invoke("index", "check", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["missing"] == []

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parts"]
    assert "data_symbol_index" in payload["parts"][0]["indexes"]


def test_cli_index_create_and_drop(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "index", "create", "-s", str(db_path), "-t", "ticks", "-c", "symbol"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["created"] is True

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    payload = json.loads(result.output)
    assert "data_symbol_index" in payload["parts"][0]["indexes"]

    result = _invoke(
        "index", "drop", "-s", str(db_path), "-t", "ticks", "-c", "symbol"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dropped"] > 0

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    payload = json.loads(result.output)
    assert "data_symbol_index" not in payload["parts"][0]["indexes"]


def test_cli_backup_and_restore(tmp_path):
    db_path = _make_sample_db(tmp_path)
    backup_path = tmp_path / "backup.iosql"
    restore_path = tmp_path / "restored.iosql"

    result = _invoke("backup", "-s", str(db_path), "--target", str(backup_path))
    assert result.exit_code == 0, result.output
    assert backup_path.exists()

    result = _invoke("restore", "--backup", str(backup_path), "--target", str(restore_path))
    assert result.exit_code == 0, result.output
    assert restore_path.exists()

    result = _invoke("tables", "-s", str(restore_path))
    assert result.exit_code == 0
    assert "ticks" in result.output


def test_cli_repartition_default_behavior_and_flags(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour")
    assert result.exit_code == 1, result.output
    assert "请提供 --target 写入新库，或提供 --replace-source 明确替换源库" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--backup", str(tmp_path / "backup.iosql"),
    )
    assert result.exit_code == 1, result.output
    assert "--backup 只能与 --replace-source 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--no-backup",
    )
    assert result.exit_code == 1, result.output
    assert "--no-backup 只能与 --replace-source 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--backup", str(tmp_path / "backup.iosql"), "--no-backup",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "--backup 与 --no-backup 不能同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks",
        "--partition", "duration(ts,every=1hour)", "-e", "1day", "--dry-run",
    )
    assert result.exit_code == 1, result.output
    assert "--partition 不能与 --every/--kind/--timezone 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["plan"]["source_replaced"] is True
    assert payload["plan"]["new_partition_expr"] == "duration(ts,every=1hour)"


def test_cli_repartition_replace_source_with_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert payload["backup"] is not None
    assert os.path.isdir(payload["backup"])

    db = Database(_db_uri(db_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
        assert len(target_table.select("*", where=None, to_dict=True)) == 4
    finally:
        db.close()


def test_cli_repartition_replace_source_no_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--no-backup", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert payload["backup"] is None

    db = Database(_db_uri(db_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
    finally:
        db.close()


def test_cli_repartition_replace_source_custom_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)
    custom_backup = tmp_path / "my_backup.iosql"

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--backup", str(custom_backup), "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert str(custom_backup) in payload["backup"]
    assert custom_backup.exists()
    backup_db = Database(_db_uri(custom_backup))
    try:
        t = backup_db.table("ticks")
        assert t.spec().partition.every == 86400000
    finally:
        backup_db.close()


def test_cli_repartition_dry_run_and_target(tmp_path):
    db_path = _make_sample_db(tmp_path)
    target_path = tmp_path / "hourly.iosql"

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        str(target_path),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert not target_path.exists()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        str(target_path),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backup"] is None
    assert target_path.exists()

    db = Database(_db_uri(target_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
        assert len(target_table.select("*", where=None, to_dict=True)) == 4
    finally:
        db.close()


def test_cli_repartition_partition_expr_dry_run_and_target(tmp_path):
    db_path = _make_sample_db(tmp_path)
    target_path = tmp_path / "partition_expr.iosql"

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--partition",
        "duration(ts,every=1hour)",
        "--target",
        str(target_path),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["plan"]["old_partition_expr"] == "duration(ts,every=1day)"
    assert payload["plan"]["new_partition_expr"] == "duration(ts,every=1hour)"
    assert not target_path.exists()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--partition",
        "duration(ts,every=1hour)",
        "--target",
        str(target_path),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backup"] is None
    assert target_path.exists()

    db = Database(_db_uri(target_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.to_string() == "duration(ts,every=1hour)"
    finally:
        db.close()


def test_cli_repartition_range_partition(tmp_path):
    db_path = tmp_path / "range.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "items", "-e", "5",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True

    db = Database(_db_uri(db_path))
    try:
        t = db.table("items")
        assert t.spec().partition.kind == "range"
        rows = t.select("*", where=None, to_dict=True)
        assert len(rows) == 20
    finally:
        db.close()


def test_cli_repartition_range_partition_expr(tmp_path):
    db_path = tmp_path / "range_expr.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "items",
        "--partition",
        "range(id,width=5)",
        "--replace-source",
        "--yes",
    )
    assert result.exit_code == 0, result.output

    db = Database(_db_uri(db_path))
    try:
        t = db.table("items")
        assert t.spec().partition.to_string() == "range(id,width=5)"
        assert len(t.select("*", where=None, to_dict=True)) == 20
    finally:
        db.close()


def test_cli_doctor_recommends_partition_expr_for_range(tmp_path):
    db_path = tmp_path / "range_doctor.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke("doctor", "-s", str(db_path), "-t", "items")
    assert result.exit_code == 1, result.output
    assert "partition: range(id,width=1)" in result.output
    assert "--partition 'range(id,width=400000)' --replace-source --yes" in result.output


def test_cli_doctor_range_partition_suggestion(tmp_path):
    from iosql.cli import _suggest_range_every_up, _suggest_range_every_down

    assert _suggest_range_every_up(avg_rows=316, target=350_000, current_every=1) > 1
    assert _suggest_range_every_down(avg_rows=500_000, target=200_000, current_every=100) < 100
    assert _suggest_range_every_down(avg_rows=500_000, target=200_000, current_every=1) == 1
