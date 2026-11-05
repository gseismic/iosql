"""迁移命令的进度、输出隔离和帮助示例。"""

import json
import shlex
import threading

import pytest
import typer.core

from iosql import Database, Partition
from iosql._migration_progress import emit, observe
from iosql.cli import _MigrationProgress, app


def _source(path):
    with Database(f'sqlite://{path}') as db:
        table = db.table('ticks', pk=['ts'], columns={'ts': 'integer', 'v': 'text'},
                         partition=Partition.range('ts', every=10))
        table.write([{'ts': 1, 'v': 'a'}, {'ts': 2, 'v': 'b'}, {'ts': 11, 'v': 'c'}])


def test_migrate_progress_keeps_json_on_stdout(tmp_path, invoke_separate, monkeypatch):
    import iosql.cli as cli

    source, target = tmp_path / 'source', tmp_path / 'target'
    _source(source)
    progress_factory = cli._MigrationProgress
    monkeypatch.setattr(cli, '_MigrationProgress', lambda: progress_factory(update_interval=0))
    result = invoke_separate(app, 'migrate', '-s', source, '--target', f'parquet://{target}',
                             '--batch-size', '1', '--verify', 'row-count')
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    payload = json.loads(result.stdout)
    assert payload['published'] and payload['verified']
    assert payload['source_rows'] == payload['target_rows'] == payload['rows_copied'] == 3
    assert '统计源表 ticks 行数' in result.stderr
    assert '统计源表 ticks：已检查文件 1/2，累计 2 行' in result.stderr
    assert '统计源表 ticks：已检查文件 2/2，累计 3 行' in result.stderr
    assert '源表 ticks 统计完成：3 行' in result.stderr
    assert '复制表 ticks：已复制 1 行，源表总行数统计中' in result.stderr
    assert '复制表 ticks：3/3 行，复制完成' in result.stderr
    assert '暂存目标表 ticks 统计完成：3 行' in result.stderr
    assert '发布目标库' in result.stderr
    assert '迁移完成' in result.stderr
    assert '[迁移]' not in result.stdout


def test_dry_run_reports_count_without_creating_target(tmp_path, invoke_separate):
    source, target = tmp_path / 'source', tmp_path / 'target'
    _source(source)
    result = invoke_separate(app, 'migrate', '-s', source, '--target', f'parquet://{target}', '--dry-run')
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    payload = json.loads(result.stdout)
    assert payload['dry_run'] and payload['source_rows'] == 3
    assert '统计源表 ticks 行数' in result.stderr
    assert '预览完成' in result.stderr
    assert '复制表' not in result.stderr
    assert not target.exists()


def test_count_events_reflect_actual_files_and_observer_is_scoped(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    _source(source)
    events = []
    with Database(f'sqlite://{source}') as db:
        with observe(lambda event, **details: events.append((event, details))):
            result = db.migrate_to(f'parquet://{target}', batch_size=1, verify='row-count')
        emit('phase', message='订阅结束后不再收到')
        assert db['ticks'].count_rows() == 3
    assert result.published and result.verified
    counts = [data for event, data in events
              if event == 'count_progress' and data['phase'] == 'source']
    assert [(item['done'], item['total'], item['rows']) for item in counts] == [(1, 2, 2), (2, 2, 3)]
    first_copy = next(index for index, (event, _) in enumerate(events) if event == 'copy_progress')
    second_count = next(index for index, (event, data) in enumerate(events)
                        if event == 'count_progress' and data['phase'] == 'source' and data['done'] == 2)
    assert first_copy < second_count
    assert not any(data.get('message') == '订阅结束后不再收到' for _, data in events)


def test_broken_observer_does_not_affect_migration(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    _source(source)

    def broken(*args, **kwargs):
        raise RuntimeError('观察者失败')

    with Database(f'sqlite://{source}') as db, observe(broken):
        result = db.migrate_to(f'parquet://{target}', verify='row-count')
    assert result.published and result.verified and result.rows_copied == 3


def test_broken_stderr_does_not_affect_migration(tmp_path, invoke_separate, monkeypatch):
    import iosql.cli as cli

    source, target = tmp_path / 'source', tmp_path / 'target'
    _source(source)
    echo = cli.typer.echo

    def broken_stderr(message, *args, **kwargs):
        if kwargs.get('err'):
            raise OSError('标准错误不可写')
        return echo(message, *args, **kwargs)

    monkeypatch.setattr(cli.typer, 'echo', broken_stderr)
    result = invoke_separate(app, 'migrate', '-s', source, '--target', f'parquet://{target}',
                             '--verify', 'row-count')
    assert result.exit_code == 0, (result.stdout, result.exception)
    assert json.loads(result.stdout)['published']


def test_heartbeat_reports_slow_phase_and_stops(monkeypatch):
    import iosql.cli as cli

    heartbeat = threading.Event()
    messages = []

    def capture(message, *args, **kwargs):
        messages.append(message)
        if '仍在处理' in message:
            heartbeat.set()

    monkeypatch.setattr(cli.typer, 'echo', capture)
    with _MigrationProgress(heartbeat_interval=0.01) as progress:
        progress('phase', message='等待慢操作')
        assert heartbeat.wait(2)
    assert any('等待慢操作' in message and '仍在处理' in message for message in messages)
    assert not progress._thread.is_alive()


def test_interruption_stops_progress_without_reporting_success(tmp_path, invoke_separate, monkeypatch):
    import iosql.cli as cli

    source = tmp_path / 'source'
    _source(source)
    reporters = []

    def make_reporter():
        reporter = _MigrationProgress()
        reporters.append(reporter)
        return reporter

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, '_MigrationProgress', make_reporter)
    monkeypatch.setattr(Database, 'migrate_to', interrupt)
    result = invoke_separate(app, 'migrate', '-s', source, '--target', f'parquet://{tmp_path / "target"}')
    assert result.exit_code == 130
    assert '操作已中断' in result.stderr
    assert '迁移完成' not in result.stderr
    assert result.stdout == ''
    assert len(reporters) == 1 and not reporters[0]._thread.is_alive()


@pytest.mark.parametrize('rich_help', [True, False])
def test_help_examples_can_be_copied_and_executed(tmp_path, invoke_separate, monkeypatch, rich_help):
    if not rich_help:
        monkeypatch.setattr(typer.core, 'rich', None)
    monkeypatch.chdir(tmp_path)
    _source(tmp_path / 'src')
    help_result = invoke_separate(app, 'migrate', '--help')
    assert help_result.exit_code == 0, help_result.stdout
    commands = [line.strip() for line in help_result.stdout.splitlines()
                if line.strip().startswith('iosql migrate ')]
    assert len(commands) == 4, help_result.stdout
    for command in commands:
        result = invoke_separate(app, *shlex.split(command)[1:])
        assert result.exit_code == 0, (command, result.stdout, result.stderr, result.exception)
        payload = json.loads(result.stdout)
        if '--dry-run' in command:
            assert payload['dry_run'] and not payload['published']
        else:
            assert payload['published'] and payload['verified']
    with Database(f'sqlite://{tmp_path / "back"}') as db:
        assert db['ticks'].count_rows() == 3
