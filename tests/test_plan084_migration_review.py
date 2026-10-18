"""审查缺陷的真实目录与确定性交错回归。"""

import builtins
import os
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from iosql import Database, Segment
from iosql.coordination import lease, physical_root
from iosql.exception import DatabaseBusy, UnsafeOperation
from iosql.migration import LocalPublisher, backup_snapshot


def uri(path, backend='sqlite'):
    return f'{backend}://{path}'


def make(db, name='ticks', rows=None):
    table = db.table(name, pk=['id'], columns={'id': 'integer'})
    if rows:
        table.write([{'id': value} for value in rows])
    return table


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('name', ['events-wal', 'events-shm', '.iosql-locks', '.iosql.write.lock'])
def test_snapshot_merge_preserves_legal_table_names(tmp_path, backend, name):
    with Database(uri(tmp_path / 'source', backend)) as source, Database(uri(tmp_path / 'target', backend)) as target:
        make(source, rows=[2])
        make(target, rows=[1])
        make(target, name, rows=[99])
        source.migrate_to(target.uri)
        assert target.table_names() == sorted(['ticks', name])
        assert target[name].select_all('*') == [{'id': 99}]


@pytest.mark.parametrize('name', ['events-wal', 'events-shm', '.iosql-locks', '.iosql.write.lock'])
def test_backup_preserves_legal_table_names(tmp_path, name):
    with Database(uri(tmp_path / 'source')) as source:
        make(source, name, rows=[99])
        backup_snapshot(source.uri, tmp_path / 'backup')
    with Database(uri(tmp_path / 'backup')) as backup:
        assert backup.table_names() == [name]
        assert backup[name].select_all('*') == [{'id': 99}]


def test_cross_device_publication_preserves_named_tables(tmp_path):
    import tempfile
    shared = Path('/dev/shm')
    if not shared.is_dir() or shared.stat().st_dev == tmp_path.stat().st_dev or not os.access(shared, os.W_OK):
        pytest.skip('没有独立可写的内存文件系统')
    with tempfile.TemporaryDirectory(prefix='iosql-review-', dir=shared) as temp:
        with Database(uri(tmp_path / 'source')) as source:
            make(source, rows=[1])
            make(source, 'events-wal', rows=[2])
            result = source.migrate_to(uri(tmp_path / 'target'), temp_dir=temp, verify='row-count')
            assert result.published
        with Database(uri(tmp_path / 'target')) as target:
            assert target.table_names() == ['events-wal', 'ticks']
            assert target['events-wal'].select_all('*') == [{'id': 2}]


def test_old_cleanup_cannot_delete_recreated_database(tmp_path, monkeypatch):
    """固定交错：首迁移解锁→别的调用者改名并复用 URI→首迁移清理。"""
    original = LocalPublisher.cleanup
    target_uri = uri(tmp_path / 'target')
    observed = []
    triggered = False

    def delayed_cleanup(self, published):
        nonlocal triggered
        if self.uri == target_uri and published and not triggered:
            triggered = True
            failures = []

            def reuse():
                try:
                    with Database(target_uri) as moved:
                        moved.rename('renamed')
                    with Database(target_uri) as replacement:
                        make(replacement, rows=[777])
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=reuse)
            worker.start()
            worker.join(10)
            assert not worker.is_alive() and not failures
            original(self, published)
            with Database(target_uri) as replacement:
                observed.append(replacement.table_names())
                if 'ticks' in observed[-1]:
                    observed.append(replacement['ticks'].select_all('*'))
        else:
            original(self, published)

    monkeypatch.setattr(LocalPublisher, 'cleanup', delayed_cleanup)
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[2])
        with Database(target_uri) as target:
            make(target, rows=[1])
        source.migrate_to(target_uri)
    assert observed == [['ticks'], [{'id': 777}]]
    with Database(uri(tmp_path / 'renamed')) as moved:
        assert moved['ticks'].select_all('*', order_by='id') == [{'id': 1}, {'id': 2}]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_migrate_into_valid_scaffold_only_target(tmp_path, backend):
    with Database(uri(tmp_path / 'source', backend)) as source, Database(uri(tmp_path / 'target', backend)) as target:
        make(source, rows=[1])
        empty = make(target)
        assert empty.count_rows() == 0
        if backend == 'sqlite':
            assert empty.get_table_meta() is None
        result = source.migrate_to(target.uri, verify='row-count')
        assert result.published and result.verified
        assert empty.select_all('*') == [{'id': 1}]


def test_empty_target_validation_still_rejects_unrelated_directories(tmp_path):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        make(source, rows=[1])
        make(target)
        (tmp_path / 'target' / 'unrelated').mkdir()
        with pytest.raises(UnsafeOperation):
            source.migrate_to(target.uri)


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('database_first', [False, True])
def test_unwritten_declaration_survives_generation_refresh(tmp_path, backend, database_first):
    with Database(uri(tmp_path / 'source', backend)) as source, Database(uri(tmp_path / 'target', backend)) as target:
        make(source, rows=[1])
        make(target, 'keep', rows=[99])
        draft = make(target, 'draft')
        expected = draft.spec().to_dict()
        source.migrate_to(target.uri)
        if database_first:
            target.table_names()
        assert draft.spec() is not None
        assert draft.spec().to_dict() == expected
        assert target.table('draft').spec().to_dict() == expected
        draft.write({'id': 2})
        assert target['draft'].select_all('*') == [{'id': 2}]


@pytest.mark.parametrize('action', ['table', 'database', 'reconnect'])
def test_internal_reconnect_does_not_mark_live_handle_closed(tmp_path, action):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        make(source, rows=[2])
        handle = make(target, rows=[1])
        if action == 'table':
            handle.rename('archive')
        elif action == 'database':
            target.rename('renamed')
        else:
            handle.close()
            handle.connect()
        source.migrate_to(target.uri, tables={'ticks': handle.table_name})
        assert handle.select_all('*', order_by='id') == [{'id': 1}, {'id': 2}]
        handle.write({'id': 3})
        assert handle.count_rows() == 3


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('segmented', [False, True])
def test_seconds_calendar_query_pruning_and_reopen(tmp_path, backend, segmented):
    with Database(uri(tmp_path / 'source')) as source:
        table = source.table('ticks', pk=['ts'], columns={'ts': 'integer'}, time={'name': 'ts', 'unit': 's'})
        table.write([{'ts': value} for value in [1, 3601, 7201]])
        specs = {'partition': 'calendar(ts,every=1hour,timezone=UTC)'}
        if segmented:
            specs['segment'] = Segment.rows(max_rows=2).to_dict()
        table.migrate_to(uri(tmp_path / 'target', backend), specs={'ticks': specs}, verify='row-count')
    with Database(uri(tmp_path / 'target', backend)) as target:
        ticks = target['ticks']
        assert ticks.query(start=3600, end=7200).to_list() == [{'ts': 3601}]
        ticks.write({'ts': 3602})
        assert ticks.query(start=3600, end=7200, order_by='ts').to_list() == [{'ts': 3601}, {'ts': 3602}]


@pytest.mark.parametrize('command', ['ui', 'web'])
def test_server_alias_does_not_hold_command_lifetime_lease(tmp_path, monkeypatch, command):
    import sys
    from types import SimpleNamespace
    import iosql.web
    from iosql.cli import app
    source_uri = uri(tmp_path / 'source')
    with Database(source_uri) as source:
        make(source, rows=[1])
    observed = []

    def run(*args, **kwargs):
        def check():
            try:
                with lease(source_uri, exclusive=True):
                    observed.append('unlocked')
            except DatabaseBusy:
                observed.append('busy')
        worker = threading.Thread(target=check)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()

    monkeypatch.setattr(iosql.web, 'create_app', lambda *args, **kwargs: object())
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(run=run))
    result = CliRunner().invoke(app, [command, '-s', source_uri, '--no-browser'])
    assert result.exit_code == 0, result.output
    assert observed == ['unlocked']


def test_local_operations_without_fcntl_use_portable_lock(tmp_path, monkeypatch):
    fcntl = pytest.importorskip('fcntl')
    try:
        from iosql import file_lock
    except ImportError:
        file_lock = None
    calls = []

    class FakeWindowsLock:
        """以真实 flock 提供内核互斥，隔离本机没有的 Win32 调用。"""
        def __init__(self, fd):
            self.fd = fd
        def acquire(self, exclusive):
            calls.append(exclusive)
            fcntl.flock(self.fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        def release(self):
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    original_import = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl':
            raise ImportError('模拟没有 fcntl 的平台')
        return original_import(name, *args, **kwargs)

    # 只模拟库级锁的导入；已有表写锁另有 msvcrt 后备，不在此替换。
    if file_lock is not None:
        monkeypatch.setattr(file_lock, 'WindowsFileLock', FakeWindowsLock)
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[1])
        with monkeypatch.context() as scope:
            scope.setattr(builtins, '__import__', no_fcntl)
            assert source.table_names() == ['ticks']
            assert source.table('ticks').count_rows() == 1
            with lease(source.uri, exclusive=True):
                assert source.table_names() == ['ticks']
    assert False in calls and True in calls


def test_published_root_is_current_after_old_retirement(tmp_path):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        make(source, rows=[2])
        make(target, rows=[1])
        source.migrate_to(target.uri)
        first = physical_root(target.uri)
        make(source, 'extra', rows=[3])
        source.migrate_to(target.uri, tables=['extra'])
        assert not first.exists()
        assert physical_root(target.uri).is_dir()
        assert target['ticks'].count_rows() == 2


def test_retirement_failure_keeps_committed_view_and_never_deletes_old_paths(tmp_path, monkeypatch):
    def cannot_retire(self, operation_id):
        raise OSError('模拟退休目录移动失败')
    monkeypatch.setattr(LocalPublisher, 'retire_previous', cannot_retire)
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        make(source, rows=[2])
        old = make(target, rows=[1])
        old_path = Path(old.table_path)
        with pytest.warns(RuntimeWarning, match='退休未完成'):
            result = source.migrate_to(target.uri)
        assert result.published
        assert old_path.is_dir()
        assert target['ticks'].select_all('*', order_by='id') == [{'id': 1}, {'id': 2}]


def test_selected_empty_target_uses_published_contract_not_previous_declaration(tmp_path):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        incoming = source.table('ticks', pk=['id'], columns={'id': 'integer', 'v': 'text'})
        incoming.write({'id': 1, 'v': 'new'})
        handle = make(target, 'draft')
        source.migrate_to(target.uri, tables={'ticks': 'draft'}, verify='row-count')
        assert handle.spec().columns == {'id': 'integer', 'v': 'text'}
        handle.write({'id': 2, 'v': 'after'})
        assert handle.count_rows() == 2


def test_legacy_parquet_empty_manifest_without_data_directory_is_valid_target(tmp_path):
    with Database(uri(tmp_path / 'source', 'parquet')) as source, Database(uri(tmp_path / 'target', 'parquet')) as target:
        make(source, rows=[1])
        empty = make(target)
        Path(empty.table_part_path).rmdir()
        assert source.migrate_to(target.uri, verify='row-count').verified
        assert empty.count_rows() == 1
