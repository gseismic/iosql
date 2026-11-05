"""本地库身份、URI 兼容和跨引擎写入保护。"""

import json
import multiprocessing
import os
import shutil
import threading
import pytest
from typer.testing import CliRunner

from iosql import Database
from iosql.cli import app
from iosql.exception import BadArgument, MigrationError, UnsafeOperation


def _write(db, name='items', value=1):
    table = db.table(name, pk=['id'], columns={'id': 'integer', 'value': 'float'})
    table.write({'id': value, 'value': float(value)})
    return table


def _race_writer(path, engine, barrier, results):
    try:
        with Database(f'{engine}://{path}') as db:
            barrier.wait(timeout=15)
            _write(db, name=engine)
        results.put((engine, 'ok'))
    except Exception as exc:
        results.put((engine, type(exc).__name__))


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_new_and_legacy_explicit_uris_and_auto_open(tmp_path, engine):
    root = tmp_path / 'mixed case.iosql'
    with Database(f'iosql+{engine}://{root}') as db:
        _write(db)
        assert db.engine == engine
    assert json.loads((root / '__iosql_database__.json').read_text())['engine'] == engine
    forms = [root, str(root), f'iosql://{root}', f'{engine}://{root}',
             f'iosql+{engine}://{root}']
    if engine == 'sqlite':
        forms.extend([f'sqlite3://{root}', f'iosql+sqlite3://{root}'])
    for location in forms:
        with Database(location) as db:
            assert db.engine == engine
            assert db.table_names() == ['items']
            assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_create_empty_database_persists_engine_and_rejects_existing(tmp_path, engine):
    root = tmp_path / 'empty.iosql'
    with Database.create(f'iosql://{root}', engine=engine) as db:
        assert db.engine == engine
        assert db.table_names() == []
    with Database(root) as db:
        assert db.engine == engine
        assert db.table_names() == []
    with pytest.raises(UnsafeOperation, match='已存在'):
        Database.create(root, engine=engine)


def test_unknown_path_does_not_create_database_or_guess_engine(tmp_path):
    missing = tmp_path / 'missing.iosql'
    with pytest.raises(BadArgument, match='数据库不存在'):
        Database(missing)
    assert not missing.exists()
    missing.mkdir()
    with pytest.raises(UnsafeOperation, match='无法识别'):
        Database(missing)
    assert list(missing.iterdir()) == []


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_explicit_engine_can_initialize_an_existing_empty_table_directory(tmp_path, engine):
    root = tmp_path / 'empty-root'
    (root / 'items').mkdir(parents=True)
    with Database(f'{engine}://{root}') as db:
        _write(db)
    with Database(root) as db:
        assert db.engine == engine
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_generic_uri_decodes_percent_escapes_only_once(tmp_path):
    root = tmp_path / 'literal%2F and space.iosql'
    with Database(f'sqlite://{root}') as db:
        _write(db)
    uri = root.as_uri().replace('file://', 'iosql://', 1)
    with Database(uri) as db:
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_relative_auto_path_stays_bound_after_working_directory_changes(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    with Database('data.iosql', engine='sqlite') as db:
        _write(db)
        monkeypatch.chdir(second)
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
    assert not (second / 'data.iosql').exists()


def test_local_paths_with_uri_reserved_characters_do_not_change_location(tmp_path):
    root = tmp_path / 'ticks?raw%2F#2026.iosql'
    with Database.create(root, engine='sqlite') as db:
        _write(db)
    uri = root.as_uri().replace('file://', 'iosql://', 1)
    for location in (root, uri):
        with Database(location) as db:
            assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
    assert not (tmp_path / 'ticks').exists()


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_local_path_with_trailing_space_does_not_open_trimmed_directory(tmp_path, engine):
    root = tmp_path / 'trailing-space '
    with Database.create(root, engine=engine) as db:
        _write(db)
    with Database(root) as db:
        assert db.engine == engine
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
    assert not (tmp_path / 'trailing-space').exists()


def test_migration_and_rename_preserve_reserved_characters_in_parent_path(tmp_path):
    parent = tmp_path / 'parent?raw%2F'
    parent.mkdir()
    source, target, renamed = (parent / name for name in ('source', 'target', 'renamed?db'))
    with Database.create(source, engine='sqlite') as db:
        _write(db)
        result = db.migrate_to(target, target_engine='parquet', verify='row-count')
        assert result.verified
        uri = renamed.as_uri().replace('file://', 'iosql://', 1)
        db.rename(uri)
    assert not source.exists()
    with Database(renamed) as db:
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
    with Database(target) as db:
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_legacy_unmarked_database_is_detected_without_writing_on_read(tmp_path, engine):
    root = tmp_path / 'old.iosql'
    with Database(f'{engine}://{root}') as db:
        _write(db)
    marker = root / '__iosql_database__.json'
    marker.unlink()
    with Database(root) as db:
        assert db.engine == engine
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
        assert not marker.exists()
        db.table('items').write({'id': 2, 'value': 2.0})
        assert json.loads(marker.read_text())['engine'] == engine


@pytest.mark.parametrize(('first', 'other'), [('sqlite', 'parquet'), ('parquet', 'sqlite')])
def test_open_handles_cannot_claim_same_or_different_tables_with_other_engine(tmp_path, first, other):
    root = tmp_path / 'race.iosql'
    with Database(f'{first}://{root}') as winner, Database(f'{other}://{root}') as loser:
        original = winner.table('items', pk=['id'], columns={'id': 'integer', 'value': 'float'})
        for name in ('items', 'another'):
            with pytest.raises(UnsafeOperation, match='存储类型不匹配'):
                _write(loser, name)
        original.write({'id': 1, 'value': 1.0})
    with Database(root) as db:
        assert db.engine == first
        assert db.table_names() == ['items']
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_simultaneous_different_backends_initialize_exactly_one_engine(tmp_path):
    root = tmp_path / 'parallel.iosql'
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [ctx.Process(target=_race_writer, args=(str(root), engine, barrier, results))
                 for engine in ('sqlite', 'parquet')]
    for process in processes:
        process.start()
    try:
        outcomes = dict(results.get(timeout=25) for _ in processes)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    winners = [engine for engine, result in outcomes.items() if result == 'ok']
    assert len(winners) == 1
    assert sorted(outcomes.values()) == ['UnsafeOperation', 'ok']
    with Database(root) as db:
        assert db.engine == winners[0]
        assert db.table_names() == winners


def test_auto_detection_waits_for_table_metadata_creation(tmp_path, monkeypatch):
    from iosql.engines.sqlite.table import Table

    root = tmp_path / 'scan-race'
    with Database.create(root, engine='sqlite'):
        pass
    started, release, reader_done = threading.Event(), threading.Event(), threading.Event()
    errors, names = [], []
    original = Table._overwrite_table_meta

    def wait_inside_write(self, metadata):
        if self._table_name == 'pending':
            started.set()
            if not release.wait(10):
                raise RuntimeError('测试未能释放写入')
        return original(self, metadata)

    monkeypatch.setattr(Table, '_overwrite_table_meta', wait_inside_write)

    def writer():
        try:
            with Database(root) as db:
                _write(db, 'pending')
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            with Database(root) as db:
                names.extend(db.table_names())
        except Exception as exc:
            errors.append(exc)
        finally:
            reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    try:
        assert started.wait(10)
        reader_thread.start()
        assert not reader_done.wait(0.2)
    finally:
        release.set()
        writer_thread.join(timeout=10)
        if reader_thread.ident is not None:
            reader_thread.join(timeout=10)
    assert not writer_thread.is_alive() and not reader_thread.is_alive()
    assert not errors
    assert names == ['pending']


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='仅 POSIX fork 继承锁描述符')
def test_fork_child_does_not_release_or_close_reused_engine_lock_fd(tmp_path):
    import select

    from iosql.coordination import resource
    from iosql.file_lock import FileLock, open_lock_file
    from iosql.utils.storage_identity import _engine_fds, storage_engine_lock

    uri = f'sqlite://{tmp_path / "fork-lock"}'
    item = resource(uri)
    read_end, write_end = os.pipe()
    pid = None
    try:
        try:
            with storage_engine_lock(uri, exclusive=True):
                lock_fd = next(iter(_engine_fds))
                pid = os.fork()
                if pid == 0:
                    os.close(read_end)
                    replacement = os.open(tmp_path / 'replacement', os.O_CREAT | os.O_RDWR, 0o600)
                    if replacement != lock_fd:
                        os.dup2(replacement, lock_fd)
                        os.close(replacement)
                else:
                    os.close(write_end)
                    write_end = None
                    readable, _, _ = select.select([read_end], [], [], 10)
                    assert readable, 'fork 子进程未能退出继承的锁上下文'
                    assert os.read(read_end, 1024) == b'ok'
                    probe_fd = open_lock_file(item.control_dir / (item.token + '.engine.lock'))
                    try:
                        with pytest.raises(BlockingIOError):
                            FileLock(probe_fd).acquire(True)
                    finally:
                        os.close(probe_fd)
        except BaseException as exc:
            if pid == 0:
                os.write(write_end, repr(exc).encode())
                os._exit(1)
            raise
        if pid == 0:
            try:
                os.write(lock_fd, b'replacement is still open')
                os.write(write_end, b'ok')
                os._exit(0)
            except BaseException as exc:
                os.write(write_end, repr(exc).encode())
                os._exit(1)
    finally:
        os.close(read_end)
        if write_end is not None:
            os.close(write_end)
        if pid:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0


def test_different_tables_can_still_write_concurrently(tmp_path, monkeypatch):
    from iosql.engines.sqlite.table import Table

    root = tmp_path / 'write-race'
    first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = Table._ensure_write_partition_compatible

    def block_first(self, *args, **kwargs):
        if self._table_name == 'first':
            first_started.set()
            if not release.wait(10):
                raise RuntimeError('测试未能释放写入')
        elif self._table_name == 'second':
            second_started.set()
        return original(self, *args, **kwargs)

    with Database(f'sqlite://{root}') as db:
        first = db.table('first', pk=['id'], columns={'id': 'integer'})
        second = db.table('second', pk=['id'], columns={'id': 'integer'})
        monkeypatch.setattr(Table, '_ensure_write_partition_compatible', block_first)

        def write(table, value):
            try:
                table.write({'id': value})
            except Exception as exc:
                errors.append(exc)

        one = threading.Thread(target=write, args=(first, 1))
        two = threading.Thread(target=write, args=(second, 2))
        one.start()
        try:
            assert first_started.wait(10)
            two.start()
            assert second_started.wait(5)
        finally:
            release.set()
            one.join(timeout=10)
            if two.ident is not None:
                two.join(timeout=10)
        assert not one.is_alive() and not two.is_alive()
        assert not errors
        assert first.count_rows() == second.count_rows() == 1


def test_wrong_prefix_and_mixed_legacy_tables_are_rejected(tmp_path):
    sqlite = tmp_path / 'sqlite.iosql'
    parquet = tmp_path / 'parquet.iosql'
    with Database(f'sqlite://{sqlite}') as db:
        _write(db, 'sqlite_table')
    with Database(f'parquet://{parquet}') as db:
        _write(db, 'parquet_table')
    with pytest.raises(UnsafeOperation, match='实际为 sqlite，请求为 parquet'):
        Database(f'iosql+parquet://{sqlite}')
    shutil.move(str(parquet / 'parquet_table'), str(sqlite / 'parquet_table'))
    for location in (str(sqlite), f'sqlite://{sqlite}', f'parquet://{sqlite}'):
        with pytest.raises(UnsafeOperation, match='混有不同存储类型'):
            Database(location)


def test_corrupt_marker_is_not_treated_as_empty(tmp_path):
    root = tmp_path / 'broken.iosql'
    root.mkdir()
    (root / '__iosql_database__.json').write_text('{invalid', encoding='utf-8')
    with pytest.raises(UnsafeOperation, match='标记无法读取'):
        Database(root)
    (root / '__iosql_database__.json').write_text('{"version":1,"engine":[]}', encoding='utf-8')
    with pytest.raises(UnsafeOperation, match='标记无效'):
        Database(root)


def test_engine_marker_does_not_conflict_with_legal_dot_table_name(tmp_path):
    root = tmp_path / 'db'
    with Database(f'sqlite://{root}') as db:
        _write(db, '.iosql.json')
    with Database(root) as db:
        assert db.table_names() == ['.iosql.json']
        assert db.table('.iosql.json').select() == [{'id': 1, 'value': 1.0}]


def test_migration_auto_target_and_explicit_aliases(tmp_path):
    source, target, restored = (tmp_path / name for name in ('source', 'target', 'restored'))
    with Database(f'iosql+sqlite://{source}') as db:
        _write(db)
        result = db.migrate_to(target, target_engine='parquet', verify='row-count')
        assert result.published and result.verified
    with Database(target) as db:
        assert db.engine == 'parquet'
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]
        result = db.table('items').save_as(f'iosql+sqlite://{restored}', verify='row-count')
        assert result.published and result.verified
    with Database(restored) as db:
        assert db.engine == 'sqlite'
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_migration_to_existing_target_auto_detects_and_rejects_mismatch(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        _write(db)
        with Database(f'parquet://{target}') as dst:
            _write(dst, 'existing', value=3)
        result = db.migrate_to(target, verify='row-count')
        assert result.published and result.verified
        with pytest.raises(UnsafeOperation, match='存储类型不匹配'):
            db.migrate_to(f'sqlite://{target}')
    with Database(target) as db:
        assert db.table_names() == ['existing', 'items']
        assert db.table('existing').select() == [{'id': 3, 'value': 3.0}]


@pytest.mark.parametrize(('source_engine', 'target_engine'), [
    ('sqlite', 'parquet'), ('parquet', 'sqlite'),
])
def test_migration_into_explicitly_initialized_empty_database(tmp_path, source_engine, target_engine):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database.create(target, engine=target_engine):
        pass
    with Database(f'{source_engine}://{source}') as db:
        _write(db)
        result = db.migrate_to(target, verify='row-count')
        assert result.published and result.verified
    with Database(target) as db:
        assert db.engine == target_engine
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_repartition_to_existing_target_accepts_untyped_path(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'parquet://{target}') as db:
        _write(db, 'existing', value=3)
    with Database(f'sqlite://{source}') as db:
        result = _write(db).repartition_to(target, table_name='copy')
        assert result.rows_copied == 1
    with Database(target) as db:
        assert db.table_names() == ['copy', 'existing']
        assert db.table('copy').select() == [{'id': 1, 'value': 1.0}]


def test_migration_dry_run_with_target_engine_does_not_touch_target(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'new'
    with Database(f'sqlite://{source}') as db:
        _write(db)
        result = db.migrate_to(target, target_engine='parquet', dry_run=True)
        assert result.dry_run and not result.published
    assert not target.exists()
    from iosql.coordination import resource
    item = resource(f'parquet://{target}')
    assert not (item.control_dir / (item.token + '.lock')).exists()


def test_auto_open_recovers_parquet_publish_before_identification(tmp_path, monkeypatch):
    import iosql._parquet_flat_publish as flat

    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        _write(db)
        with monkeypatch.context() as patch:
            patch.setattr(flat, '_write_marker', lambda *args: (_ for _ in ()).throw(OSError('中断')))
            with pytest.raises(MigrationError):
                db.migrate_to(f'parquet://{target}', verify='row-count')
    assert not (target / '__iosql_database__.json').exists()
    from iosql.coordination import resource
    state_path = resource(f'parquet://{target}').state_path
    before = state_path.read_bytes()
    preview = CliRunner().invoke(app, [
        'migrate', '-s', str(target), '--target', f'sqlite://{tmp_path / "preview"}', '--dry-run',
    ])
    assert preview.exit_code != 0
    assert '需要先恢复' in preview.output
    assert state_path.read_bytes() == before
    assert not (target / '__iosql_database__.json').exists()
    with Database(target) as recovered:
        assert recovered.engine == 'parquet'
        assert recovered.table('items').select() == [{'id': 1, 'value': 1.0}]
    assert json.loads((target / '__iosql_database__.json').read_text())['engine'] == 'parquet'


def test_cli_auto_source_and_target_engine(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        _write(db)
    result = CliRunner().invoke(app, [
        'migrate', '-s', str(source), '--target', str(target),
        '--target-engine', 'parquet', '--verify', 'row-count',
    ])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(app, ['tables', '-s', f'iosql://{target}', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {'tables': ['items']}


def test_cli_rejects_wrong_backend_and_sqlite_only_commands_for_parquet(tmp_path):
    root = tmp_path / 'parquet'
    with Database(f'parquet://{root}') as db:
        _write(db)
    runner = CliRunner()
    for command in (
        ['web', '-s', f'iosql+sqlite://{root}', '--no-browser'],
        ['doctor', '-s', str(root)],
        ['index', 'list', '-s', str(root), '-t', 'items'],
        ['backup', '-s', str(root), '--target', str(tmp_path / 'backup')],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code != 0
        assert '存储类型不匹配' in result.output or '只支持 SQLite' in result.output


def test_cli_parquet_auto_detects_for_query_and_import(tmp_path):
    root = tmp_path / 'parquet'
    with Database(f'parquet://{root}') as db:
        _write(db)
    incoming = tmp_path / 'rows.jsonl'
    incoming.write_text('{"id": 2, "value": 2.0}\n', encoding='utf-8')
    runner = CliRunner()
    result = runner.invoke(app, ['import', '-s', str(root), '-t', 'items', '-f', str(incoming)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ['query', '-s', str(root), '-t', 'items', '--order-by', 'id'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{'id': 1, 'value': 1.0}, {'id': 2, 'value': 2.0}]


def test_cli_new_untyped_target_requires_engine(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        _write(db)
    result = CliRunner().invoke(app, ['migrate', '-s', str(source), '--target', str(target)])
    assert result.exit_code != 0
    assert '指定 sqlite 或 parquet' in result.output
    assert not target.exists()


def test_cli_migration_accepts_explicit_iosql_plus_prefixes(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'iosql+sqlite://{source}') as db:
        _write(db)
    result = CliRunner().invoke(app, [
        'migrate', '-s', f'iosql+sqlite://{source}',
        '--target', f'iosql+parquet://{target}', '--verify', 'row-count',
    ])
    assert result.exit_code == 0, result.output
    with Database(f'iosql+parquet://{target}') as db:
        assert db.table('items').select() == [{'id': 1, 'value': 1.0}]


def test_postgresql_alias_is_normalized_without_connecting():
    uri = 'postgresql://user:password@localhost:5432/db'
    with Database('iosql+' + uri) as db:
        assert db.engine == 'postgresql'
        assert db.uri == uri


def test_postgresql_alias_can_query_when_test_server_is_configured():
    uri = os.environ.get('POSTGRES_DSN')
    if not uri:
        pytest.skip('需要独立 PostgreSQL 测试实例')
    with Database('iosql+' + uri) as db:
        assert isinstance(db.table_names(), list)
