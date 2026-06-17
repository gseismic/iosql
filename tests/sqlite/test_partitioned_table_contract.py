import pytest

from iosql import Database, Partition, Segment
from iosql.exception import BadArgument


def _uri(tmp_path, name="partitioned.iosql"):
    return f"sqlite://{tmp_path}/{name}"


def test_range_partition_contract_has_no_time_key_and_prunes(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "metrics",
        pk=["seq", "symbol"],
        columns={"seq": "integer", "symbol": "text", "value": "real"},
        partition=Partition.range("seq", every=1000),
    )

    result = table.insert_all([
        {"seq": 10, "symbol": "A", "value": 1.0},
        {"seq": 1010, "symbol": "A", "value": 2.0},
        {"seq": 2010, "symbol": "A", "value": 3.0},
    ])

    assert result.rows_seen == 3
    assert table.table_parts() == [0, 1000, 2000]
    assert "time" not in table.get_table_meta()["table_contract"]

    rows = table.select(
        "*",
        where={"field": "seq", "op": "between_and", "value": [1000, 1999]},
        order_by="seq",
        to_dict=True,
    )
    assert rows == [{"seq": 1010, "symbol": "A", "value": 2.0}]

    projected = table.select(
        ["symbol", "value"],
        where=None,
        order_by="seq desc",
        to_dict=True,
    )
    assert projected == [
        {"symbol": "A", "value": 3.0},
        {"symbol": "A", "value": 2.0},
        {"symbol": "A", "value": 1.0},
    ]
    assert "seq" not in projected[0]

    plan = table.explain_select(
        where={"field": "seq", "op": "between_and", "value": [1000, 1999]},
    ).to_dict()
    assert plan["candidate_partitions"] == [1000]
    assert plan["scanned_partitions"] == [1000]
    assert plan["skipped_partitions"] == [0, 2000]
    assert [item["logical_part"] for item in plan["candidate_segments"]] == [1000]

    db.close()


def test_row_segment_delete_does_not_reopen_sealed_segment(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2),
        order_key="seq",
    )

    table.write([
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 2, "seq": 2, "kind": "base"},
    ])
    assert table.segments()[0]["sealed"] is True

    deleted = table.delete({"field": "event_id", "op": "==", "value": 1})
    assert deleted == 1
    assert table.segments()[0]["row_count"] == 1
    assert table.segments()[0]["sealed"] is True

    table.write({"event_id": 99, "seq": 0, "kind": "late"})
    segments = table.segments()
    assert [item["row_count"] for item in segments] == [1, 1]
    assert [item["sealed"] for item in segments] == [True, False]

    db.close()


def test_row_segment_rejects_upsert_and_replace_until_pk_index(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        segment=Segment.rows(max_rows=2),
        order_key="seq",
    )

    table.write({"event_id": 1, "seq": 1})
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.write({"event_id": 1, "seq": 2}, mode="upsert")
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.write({"event_id": 1, "seq": 3}, mode="replace")
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.insert_all({"event_id": 1, "seq": 4}, replace=True)

    assert table.select("*", where=None, to_dict=True) == [
        {"event_id": 1, "seq": 1},
    ]

    db.close()


def test_calendar_partition_contract_without_time_prunes_equality(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "schedule",
        pk=["event_date", "title"],
        columns={"event_date": "text", "title": "text", "room": "text"},
        partition=Partition.calendar("event_date", every="1day", timezone="UTC"),
    )

    table.insert_all([
        {
            "event_date": "2026-01-01T09:00:00+00:00",
            "title": "check-in 1",
            "room": "A",
        },
        {
            "event_date": "2026-01-03T09:00:00+00:00",
            "title": "check-in 3",
            "room": "B",
        },
    ])

    rows = table.select(
        "*",
        where={
            "field": "event_date",
            "op": "==",
            "value": "2026-01-03T09:00:00+00:00",
        },
        to_dict=True,
    )
    assert rows == [{
        "event_date": "2026-01-03T09:00:00+00:00",
        "title": "check-in 3",
        "room": "B",
    }]

    plan = table.explain_select(
        where={
            "field": "event_date",
            "op": "==",
            "value": "2026-01-03T09:00:00+00:00",
        },
    ).to_dict()
    assert plan["candidate_partitions"] == ["20260103"]
    assert plan["scanned_partitions"] == ["20260103"]
    assert plan["skipped_partitions"] == ["20260101"]
    assert "time" not in table.get_table_meta()["table_contract"]

    db.close()


def test_row_segment_late_data_scans_overlapping_segments_and_skips_others(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=3),
        order_key="seq",
    )

    table.insert_all([
        {"event_id": index, "seq": index, "kind": "base"}
        for index in range(6)
    ])
    table.insert_all([
        {"event_id": 99, "seq": 1, "kind": "late"},
    ])

    assert table.table_parts() == ["__all__"]
    segments = table.segments()
    assert [item["row_count"] for item in segments] == [3, 3, 1]
    assert [item["stats"]["seq"] for item in segments] == [
        {"min": 0, "max": 2},
        {"min": 3, "max": 5},
        {"min": 1, "max": 1},
    ]

    rows = table.select(
        "*",
        where={"field": "seq", "op": "between_and", "value": [1, 1]},
        order_by="event_id",
        to_dict=True,
    )
    assert rows == [
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 99, "seq": 1, "kind": "late"},
    ]

    plan = table.explain_select(
        where={"field": "seq", "op": "between_and", "value": [1, 1]},
        order_by="seq",
    ).to_dict()
    assert plan["candidate_partitions"] == ["__all__"]
    assert plan["scanned_partitions"] == ["__all__"]
    assert {
        item["segment_id"] for item in plan["scanned_segments"]
    } == {0, 2}
    assert {
        item["segment_id"] for item in plan["skipped_segments"]
    } == {1}
    assert plan["segment_skip_reasons"] == {
        "__all____000001": "pruned_by_segment_stats",
    }
    assert plan["requires_global_sort"] is True

    db.close()
