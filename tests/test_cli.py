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
    assert "index_fix=ok" in result.output

    result = _invoke("index", "check", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["missing"] == []

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parts"]
    assert "data_symbol_index" in payload["parts"][0]["indexes"]


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
    assert "替换源库必须同时提供 --yes" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--backup", str(tmp_path / "backup.iosql"),
    )
    assert result.exit_code == 1, result.output
    assert "--target 与 --backup 不能同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--no-backup",
    )
    assert result.exit_code == 1, result.output
    assert "--target 与 --no-backup 不能同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--backup", str(tmp_path / "backup.iosql"), "--no-backup", "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "--backup 与 --no-backup 不能同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["plan"]["source_replaced"] is True


def test_cli_repartition_replace_source_with_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--yes",
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
        "--no-backup", "--yes",
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
        "--backup", str(custom_backup), "--yes",
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
    assert target_path.exists()

    db = Database(_db_uri(target_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
        assert len(target_table.select("*", where=None, to_dict=True)) == 4
    finally:
        db.close()
