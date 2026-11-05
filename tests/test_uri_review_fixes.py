"""URI 自动识别后的并发、迁移、空库与 Parquet 图表回归。"""

import json
import threading
import time

import pytest
from typer.testing import CliRunner

from iosql import Database, Segment
from iosql.cli import app
from iosql.coordination import lease
from iosql.exception import BadArgument, MigrationLockTimeout, UnsafeOperation
from iosql.utils.storage_identity import storage_engine_lock


def _write_one(db, name='items'):
    table = db.table(name, pk=['id'], columns={'id': 'integer'})
    table.write({'id': 1})
    return table


def test_cached_table_lookup_allows_writes_to_another_table(tmp_path, monkeypatch):
    from iosql.engines.sqlite.table import Table

    started, second_started, release = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = Table._ensure_write_partition_compatible

    def block_first(self, *args, **kwargs):
        if self._table_name == 'first':
            started.set()
            if not release.wait(10):
                raise RuntimeError('测试未能释放第一个表的写入')
        elif self._table_name == 'second':
            second_started.set()
        return original(self, *args, **kwargs)

    with Database.create(tmp_path / 'db', engine='sqlite') as db:
        for name in ('first', 'second'):
            db.table(name, pk=['id'], columns={'id': 'integer'})
        monkeypatch.setattr(Table, '_ensure_write_partition_compatible', block_first)

        def write(name):
            try:
                db.table(name).write({'id': 1})
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=write, args=('first',))
        second = threading.Thread(target=write, args=('second',))
        first.start()
        try:
            assert started.wait(10)
            second.start()
            assert second_started.wait(1), '访问缓存表时等待了另一张表的写入'
        finally:
            release.set()
            first.join(10)
            if second.ident is not None:
                second.join(10)
        assert not first.is_alive() and not second.is_alive()
        assert not errors
        assert db.table('first').count_rows() == db.table('second').count_rows() == 1


def test_cached_table_lookup_rejects_changed_engine_marker(tmp_path):
    root = tmp_path / 'db'
    with Database.create(root, engine='sqlite') as db:
        _write_one(db)
        (root / '__iosql_database__.json').write_text(
            json.dumps({'version': 1, 'engine': 'parquet'}), encoding='utf-8'
        )
        with pytest.raises(UnsafeOperation, match='存储类型不匹配'):
            db.table('items')


def test_cached_table_lookup_rejects_replaced_table_symlink(tmp_path):
    root = tmp_path / 'db'
    with Database.create(root, engine='sqlite') as db:
        _write_one(db)
        (root / 'items').rename(tmp_path / 'original-table')
        (root / 'items').symlink_to(tmp_path / 'original-table', target_is_directory=True)
        with pytest.raises(UnsafeOperation, match='表路径不是普通目录'):
            db.table('items')


@pytest.mark.parametrize('lock_kind', ['lease', 'engine'])
def test_auto_target_waits_within_migration_timeout(tmp_path, lock_kind):
    source_path, target_path = tmp_path / 'source', tmp_path / 'target'
    with Database.create(target_path, engine='sqlite'):
        pass
    target_uri = f'sqlite://{target_path}'
    ready, release = threading.Event(), threading.Event()
    failures, results = [], []

    def hold_lock():
        try:
            context = (lease(target_uri, exclusive=True) if lock_kind == 'lease'
                       else storage_engine_lock(target_uri, exclusive=False))
            with context:
                ready.set()
                if not release.wait(10):
                    raise RuntimeError('测试未能释放目标库锁')
        except Exception as exc:
            failures.append(exc)

    with Database.create(source_path, engine='sqlite') as db:
        _write_one(db)
        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert ready.wait(10)

        def migrate():
            try:
                results.append(db.migrate_to(target_path, lock_timeout=3))
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=migrate)
        worker.start()
        try:
            worker.join(0.2)
            assert worker.is_alive(), '迁移未等待目标识别锁'
        finally:
            release.set()
            holder.join(10)
            worker.join(10)
        assert not holder.is_alive() and not worker.is_alive()
        assert not failures
        assert len(results) == 1 and results[0].published


@pytest.mark.parametrize('lock_kind', ['lease', 'engine'])
def test_auto_target_identification_respects_short_migration_timeout(tmp_path, lock_kind):
    source_path, target_path = tmp_path / 'source', tmp_path / 'target'
    with Database.create(target_path, engine='sqlite'):
        pass
    target_uri = f'sqlite://{target_path}'
    ready, release = threading.Event(), threading.Event()

    def hold_lock():
        context = (lease(target_uri, exclusive=True) if lock_kind == 'lease'
                   else storage_engine_lock(target_uri, exclusive=False))
        with context:
            ready.set()
            release.wait(10)

    with Database.create(source_path, engine='sqlite') as db:
        _write_one(db)
        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert ready.wait(10)
        try:
            started = time.monotonic()
            with pytest.raises(MigrationLockTimeout):
                db.migrate_to(target_path, lock_timeout=0.2)
            elapsed = time.monotonic() - started
            assert 0.14 <= elapsed < 1.5
        finally:
            release.set()
            holder.join(10)
        assert not holder.is_alive()


def test_source_table_preflight_obeys_migration_timeout(tmp_path):
    source = tmp_path / 'source'
    ready, release = threading.Event(), threading.Event()
    with Database.create(source, engine='sqlite') as db:
        _write_one(db)

        def hold_source_writer_lock():
            with lease(db.uri), storage_engine_lock(db.uri, exclusive=False):
                ready.set()
                release.wait(10)

        holder = threading.Thread(target=hold_source_writer_lock)
        holder.start()
        assert ready.wait(10)
        try:
            started = time.monotonic()
            with pytest.raises(MigrationLockTimeout):
                db.migrate_to(f'sqlite://{tmp_path / "target"}', lock_timeout=0.2)
            assert 0.14 <= time.monotonic() - started < 1.5
        finally:
            release.set()
            holder.join(10)
        assert not holder.is_alive()


def test_dry_run_count_does_not_hold_database_engine_lock(tmp_path, monkeypatch):
    started, release, written = threading.Event(), threading.Event(), threading.Event()
    failures = []
    with Database.create(tmp_path / 'source', engine='sqlite') as db:
        first = _write_one(db, 'first')
        _write_one(db, 'second')
        original = first.count_rows

        def slow_count():
            started.set()
            if not release.wait(10):
                raise RuntimeError('测试未能释放源表行数统计')
            return original()

        monkeypatch.setattr(first, 'count_rows', slow_count)

        def preview():
            try:
                db.migrate_to(f'sqlite://{tmp_path / "target"}', tables=['first'], dry_run=True)
            except Exception as exc:
                failures.append(exc)

        def write_second():
            try:
                db.table('second').write({'id': 2})
                written.set()
            except Exception as exc:
                failures.append(exc)

        preview_thread = threading.Thread(target=preview)
        writer = threading.Thread(target=write_second)
        preview_thread.start()
        try:
            assert started.wait(10)
            writer.start()
            assert written.wait(1), '预览行数统计期间阻塞了另一张表的写入'
        finally:
            release.set()
            preview_thread.join(10)
            if writer.ident is not None:
                writer.join(10)
        assert not preview_thread.is_alive() and not writer.is_alive()
        assert not failures


@pytest.mark.parametrize('lock_kind', ['lease', 'engine'])
def test_cli_source_detection_obeys_migration_timeout(tmp_path, lock_kind):
    source = tmp_path / 'source'
    with Database.create(source, engine='sqlite') as db:
        _write_one(db)
        uri = db.uri
    ready, release = threading.Event(), threading.Event()

    def hold_source_lock():
        context = (lease(uri, exclusive=True) if lock_kind == 'lease'
                   else storage_engine_lock(uri, exclusive=False))
        with context:
            ready.set()
            release.wait(10)

    holder = threading.Thread(target=hold_source_lock)
    holder.start()
    assert ready.wait(10)
    try:
        started = time.monotonic()
        result = CliRunner().invoke(app, ['migrate', '-s', str(source),
                                        '--target', f'parquet://{tmp_path / "target"}',
                                        '--lock-timeout', '0.2'])
        assert result.exit_code != 0
        assert 'lock_timeout' in result.output
        assert 0.14 <= time.monotonic() - started < 1.5
        assert not (tmp_path / 'target').exists()
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()


@pytest.mark.parametrize('lock_kind', ['lease', 'engine'])
def test_cli_source_detection_continues_after_lock_is_released(tmp_path, lock_kind):
    source = tmp_path / 'source'
    with Database.create(source, engine='sqlite') as db:
        _write_one(db)
        uri = db.uri
    ready, release = threading.Event(), threading.Event()

    def hold_source_lock():
        context = (lease(uri, exclusive=True) if lock_kind == 'lease'
                   else storage_engine_lock(uri, exclusive=False))
        with context:
            ready.set()
            release.wait(10)

    holder = threading.Thread(target=hold_source_lock)
    holder.start()
    assert ready.wait(10)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        result = CliRunner().invoke(app, ['migrate', '-s', str(source),
                                        '--target', f'parquet://{tmp_path / "target"}',
                                        '--verify', 'row-count', '--lock-timeout', '3'])
        assert result.exit_code == 0, result.output
        with Database(tmp_path / 'target') as target:
            assert target.engine == 'parquet'
            assert target.table('items').select() == [{'id': 1}]
    finally:
        release.set()
        timer.cancel()
        timer.join(10)
        holder.join(10)
    assert not holder.is_alive()


def test_auto_target_uses_one_budget_for_both_locks(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database.create(target, engine='sqlite'):
        pass
    uri = f'sqlite://{target}'
    lease_ready, engine_ready = threading.Event(), threading.Event()
    release_lease, release_engine = threading.Event(), threading.Event()

    def hold_lease():
        with lease(uri, exclusive=True):
            lease_ready.set()
            release_lease.wait(10)

    def hold_engine():
        with storage_engine_lock(uri, exclusive=False):
            engine_ready.set()
            release_engine.wait(10)

    with Database.create(source, engine='sqlite') as db:
        _write_one(db)
        holders = [threading.Thread(target=hold_lease), threading.Thread(target=hold_engine)]
        for holder in holders:
            holder.start()
        assert lease_ready.wait(10) and engine_ready.wait(10)
        timer = threading.Timer(0.35, release_lease.set)
        timer.start()
        try:
            started = time.monotonic()
            with pytest.raises(MigrationLockTimeout):
                db.migrate_to(target, lock_timeout=0.6)
            assert 0.5 <= time.monotonic() - started < 0.85
        finally:
            release_lease.set()
            release_engine.set()
            timer.cancel()
            timer.join(10)
            for holder in holders:
                holder.join(10)
        assert all(not holder.is_alive() for holder in holders)


@pytest.mark.parametrize('engine,options,exc', [
    ('sqlite', {'max_open_partitions': 0}, BadArgument),
    ('parquet', {'pragma_dict': {'journal_mode': 'WAL'}}, BadArgument),
    ('sqlite', {'unknown_option': True}, TypeError),
])
def test_create_invalid_options_do_not_claim_database(tmp_path, engine, options, exc):
    root = tmp_path / 'db'
    with pytest.raises(exc):
        Database.create(root, engine=engine, **options)
    assert not root.exists()
    with Database.create(root, engine=engine) as db:
        assert db.engine == engine and db.table_names() == []


def test_backup_and_restore_support_marked_empty_database(tmp_path):
    empty, backup, restored = (tmp_path / name for name in ('empty', 'backup', 'restored'))
    with Database.create(empty, engine='sqlite'):
        pass
    runner = CliRunner()
    result = runner.invoke(app, ['backup', '-s', str(empty), '--target', str(backup)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ['restore', '--backup', str(backup), '--target', str(restored)])
    assert result.exit_code == 0, result.output
    with Database(restored) as db:
        assert db.engine == 'sqlite' and db.table_names() == []

    with Database.create(tmp_path / 'populated', engine='sqlite') as db:
        _write_one(db)
    populated_backup = tmp_path / 'populated-backup'
    result = runner.invoke(app, ['backup', '-s', str(tmp_path / 'populated'),
                                 '--target', str(populated_backup)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ['restore', '--backup', str(populated_backup),
                                 '--target', str(empty), '--replace', '--yes'])
    assert result.exit_code == 0, result.output
    with Database(empty) as db:
        assert db.table('items').select() == [{'id': 1}]


@pytest.mark.parametrize('target_kind', ['plain', 'parquet', 'corrupt'])
def test_restore_still_rejects_unrecognized_or_wrong_engine_empty_directory(tmp_path, target_kind):
    source, backup, target = tmp_path / 'source', tmp_path / 'backup', tmp_path / 'target'
    with Database.create(source, engine='sqlite') as db:
        _write_one(db)
    runner = CliRunner()
    assert runner.invoke(app, ['backup', '-s', str(source), '--target', str(backup)]).exit_code == 0
    if target_kind == 'parquet':
        with Database.create(target, engine='parquet'):
            pass
    else:
        target.mkdir()
        if target_kind == 'corrupt':
            (target / '__iosql_database__.json').write_text('{broken', encoding='utf-8')
    result = runner.invoke(app, ['restore', '--backup', str(backup), '--target', str(target),
                                 '--replace', '--yes'])
    assert result.exit_code != 0
    assert target.is_dir()
    if target_kind == 'plain':
        assert list(target.iterdir()) == []
    elif target_kind == 'corrupt':
        assert (target / '__iosql_database__.json').read_text(encoding='utf-8') == '{broken'


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
@pytest.mark.parametrize('kind', ['line', 'candle'])
def test_compat_chart_numeric_text_axes_use_64_bit_and_keep_order(tmp_path, engine, kind):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from iosql.web import create_app

    root = tmp_path / 'db'
    with Database.create(root, engine=engine) as db:
        bars = db.table(
            'bars', pk=['symbol', 'ts'], symbol_column='symbol', order_key='ts',
            columns={'symbol': 'text', 'ts': 'text', 'open': 'float', 'high': 'float',
                     'low': 'float', 'close': 'float'},
            segment=Segment.rows(max_rows=2),
        )
        bars.write([
            {'symbol': 'BTC', 'ts': str(ts), 'open': i, 'high': i + 1,
             'low': i - 1, 'close': i + 0.5}
            for i, ts in enumerate((1735689602000, 1735689600000, 1735689601000), 1)
        ] + [{'symbol': 'ETH', 'ts': '1735689600000', 'open': 9, 'high': 10,
              'low': 8, 'close': 9.5}])
        daily = db.table('daily', pk=['d'], order_key='d',
                         columns={'d': 'text', 'price': 'float'})
        daily.write([{'d': d, 'price': price} for d, price in
                     [('20250103', 3.0), ('20250101', 1.0), ('20250102', 2.0)]])
    with TestClient(create_app(str(root), 'readonly'), base_url='http://localhost') as client:
        response = client.get('/api/table/bars/chart', params={
            'col': 'close', 'kind': kind, 'symbol': 'BTC',
            'start': '1735689600000', 'end': '1735689602000',
        })
        assert response.status_code == 200, response.text
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]['type'] == 'done', events
        points = [point for event in events if event['type'] == 'data' for point in event['points']]
        assert [point[0] for point in points] == [1735689600000, 1735689601000, 1735689602000]
        assert len(points) == 3 and all(event.get('symbol') == 'BTC'
                                         for event in events if event['type'] == 'data')
        if kind == 'line':
            response = client.get('/api/table/daily/chart', params={'col': 'price'})
            assert response.status_code == 200, response.text
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]['type'] == 'done', events
            points = [point for event in events if event['type'] == 'data' for point in event['points']]
            assert points == [[20250101, 1.0], [20250102, 2.0], [20250103, 3.0]]


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_compat_line_chart_rejects_invalid_numeric_text_before_stream(tmp_path, engine):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from iosql.web import create_app

    root = tmp_path / 'db'
    with Database.create(root, engine=engine) as db:
        table = db.table('items', pk=['d'], order_key='d', columns={'d': 'text', 'v': 'float'})
        table.write([{'d': '2025-01-01', 'v': 1.0}])
    with TestClient(create_app(str(root), 'readonly'), base_url='http://localhost') as client:
        response = client.get('/api/table/items/chart', params={'col': 'v'})
    assert response.status_code == 422
    assert '非数字文本' in response.json()['detail']


@pytest.mark.parametrize('kind', ['line', 'candle'])
def test_parquet_chart_validates_only_filtered_rows_and_rejects_overflow(tmp_path, kind):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from iosql.web import create_app

    root = tmp_path / 'db'
    with Database.create(root, engine='parquet') as db:
        table = db.table('bars', pk=['symbol', 'ts'], symbol_column='symbol', order_key='ts',
                         columns={'symbol': 'text', 'ts': 'text', 'open': 'float',
                                  'high': 'float', 'low': 'float', 'close': 'float'})
        table.write([
            {'symbol': symbol, 'ts': ts, 'open': 1.0, 'high': 2.0, 'low': 0.0, 'close': 1.5}
            for symbol, ts in [('BTC', '1735689600000'), ('BAD', '2025-01-01'),
                               ('BIG', '9223372036854775808')]
        ])
    with TestClient(create_app(str(root), 'readonly'), base_url='http://localhost') as client:
        response = client.get('/api/table/bars/chart', params={
            'col': 'close', 'kind': kind, 'symbol': 'BTC',
        })
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]['type'] == 'done' and events[-1]['rows'] == 1
        for symbol in ('BAD', 'BIG'):
            response = client.get('/api/table/bars/chart', params={
                'col': 'close', 'kind': kind, 'symbol': symbol,
            })
            assert response.status_code == 422
            assert '非数字文本' in response.json()['detail']
