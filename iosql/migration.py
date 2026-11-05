"""统一迁移：先在隔离空间转换，再一次发布目标视图。"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import stat
import time
import uuid
import warnings
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

from ._migration_progress import counting, emit
from .coordination import (
    after_leases, atomic_json, ensure_directory, exclusive_resources, lease, operation, physical_root, resource,
    sync_directory, timeout_value,
)
from .exception import (
    BadArgument, DatabaseBusy, IosqlError, MigrationError, MigrationRecoveryRequired,
    MigrationVerificationError, TableSpecConflict, UnsafeOperation,
)
from .spec import MigrationResult, TableContract, TableMigrationResult
from .utils.storage_identity import is_iosql_database, is_iosql_table


class _Omitted:
    def __repr__(self):
        return '<omitted>'


OMITTED = _Omitted()
_LAYOUT_FIELDS = {'partition', 'segment', 'physical_layout', 'force_partition'}


@dataclass
class PlannedTable:
    source: object
    name: str
    target_name: str
    source_spec: TableContract
    target_spec: TableContract
    result: TableMigrationResult


def _batch_size(value):
    if isinstance(value, bool):
        raise BadArgument('batch_size must be a positive integer')
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadArgument('batch_size must be a positive integer') from exc
    if number <= 0 or isinstance(value, float) and value != number:
        raise BadArgument('batch_size must be a positive integer')
    return number


def _mapping(owner, tables):
    from .database import Database
    if isinstance(owner, Database):
        return owner._migration_table_mapping(tables)
    name = owner.table_name
    if tables is None:
        mapping = {name: name}
    elif isinstance(tables, str):
        # 单表旧第二位置参数表示目标表名。
        mapping = {name: tables}
    elif isinstance(tables, dict):
        mapping = dict(tables)
    elif isinstance(tables, (list, tuple, set)):
        mapping = {item: item for item in tables}
    else:
        raise BadArgument('tables 必须是列表或源表名到目标表名的字典')
    if not mapping or set(mapping) != {name}:
        raise BadArgument('Table.migrate_to 的 tables 只能选择当前表')
    return mapping


def _target_spec(source, value):
    if value is OMITTED:
        return TableContract.from_dict(source.to_dict())
    if isinstance(value, dict):
        unknown = set(value) - _LAYOUT_FIELDS
        if unknown:
            raise BadArgument(f'specs 布局覆盖含未知字段: {sorted(unknown)}')
        wire = source.to_dict()
        wire.update(value)
        # 显式改变 segment 时应让它决定 physical_layout；移除时保留行数建议。
        target = TableContract.from_dict(wire)
        if 'physical_layout' in value and target.segment is not None:
            from .spec import PhysicalLayout
            requested = value['physical_layout']
            requested = (requested if isinstance(requested, PhysicalLayout)
                         else PhysicalLayout.from_dict(requested))
            if requested.to_dict() != target.physical_layout.to_dict():
                raise BadArgument('physical_layout 与 segment 冲突，请用 segment 指定文件行数')
    else:
        target = TableContract.ensure(value)
        before, after = source.to_dict(), target.to_dict()
        if any(before.get(key) != after.get(key) for key in set(before) | set(after)
               if key not in _LAYOUT_FIELDS):
            raise BadArgument('迁移 specs 只能改变分区与物理布局，不能改变列、主键或时间含义')
    if target.partition and target.partition.column not in source.columns:
        raise BadArgument(f'目标分区列未声明: {target.partition.column}')
    return target


def validate_contract(stored, requested):
    """已有正式目标必须兼容最终契约；不得把 replace 解释成重建。"""
    from .database import Database
    Database._ensure_contract_core_compatible(stored, requested)
    old = stored.to_partition()
    new = requested.to_partition()
    if (old.to_meta() if old else None) != (new.to_meta() if new else None):
        raise TableSpecConflict('目标分区与 specs 不兼容；请指定新的目标库或目标表')


def _plan(owner, mapping, specs, target_uri, verify, dry_run, operation_id):
    from .database import Database
    if specs is None:
        specs = {}
    if not isinstance(specs, dict) or set(specs) - set(mapping):
        raise BadArgument('specs 必须按本次已选源表名指定目标契约')
    plans = []
    for name, target_name in mapping.items():
        table = owner[name] if isinstance(owner, Database) else owner
        source_spec = table.spec()
        if source_spec is None:
            raise BadArgument(f'migrate_to requires a TableContract: {name}')
        target_spec = _target_spec(source_spec, specs.get(name, OMITTED))
        # 预览和正式迁移都先检查运行时布局，不能发布不可使用的空表。
        target_spec.to_partition()
        import arrow
        try:
            if target_spec.time:
                arrow.get(0).to(target_spec.time.timezone)
            if target_spec.partition and target_spec.partition.timezone:
                arrow.get(0).to(target_spec.partition.timezone)
        except (ValueError, TypeError) as exc:
            raise BadArgument(f'目标分区或时间契约的时区无效: {name}') from exc
        if not target_spec.columns or any(v is None for v in target_spec.columns.values()):
            raise BadArgument(f'迁移需要完整的列类型（空表也必须声明）: {name}')
        result = TableMigrationResult(
            table=name, target_table=target_name, source_uri=owner.uri,
            target_uri=target_uri, verify=verify, dry_run=dry_run,
            operation_id=operation_id, source_spec=source_spec.to_dict(),
            target_spec=target_spec.to_dict(), rows_written=None if dry_run else 0,
            published=False,
        )
        plans.append(PlannedTable(table, name, target_name, source_spec, target_spec, result))
    return plans


def _check_names(mapping, engine):
    if not mapping:
        raise BadArgument('migrate_to requires at least one table')
    if any(not isinstance(k, str) or not isinstance(v, str) or not k or not v
           for k, v in mapping.items()):
        raise BadArgument('源表名和目标表名必须是非空字符串')
    if len(set(mapping.values())) != len(mapping):
        raise BadArgument('多个源表不能映射到同一个目标表')
    for name in mapping.values():
        if engine in ('sqlite', 'sqlite3', 'parquet'):
            from .engines.sqlite.table import Table
            if Table._validate_table_name(name) != name:
                raise BadArgument('目标表名不能包含首尾空白')
        elif name.startswith('__iosql_') or len(name.encode('utf-8')) > 63 or '\x00' in name:
            raise BadArgument('目标表名为保留名称或超过 PostgreSQL 标识符限制')


def _default_workspace_root(source_item, target_item):
    if target_item.path is not None:
        path = target_item.path
        if path.is_dir() and path.stat().st_dev != path.parent.stat().st_dev:
            # 目标根是独立挂载点时，父目录不在目标文件系统中。
            return path / '__iosql_migrations__'
        return target_item.control_dir / (target_item.token + '.workspaces')
    if source_item.path is None:
        import tempfile
        return Path(tempfile.gettempdir()) / 'iosql-migrations'
    return source_item.path / '__iosql_migrations__'


def _workspace(owner, target_item, temp_dir, operation_id):
    item = resource(owner.uri)
    if temp_dir is None:
        root = _default_workspace_root(item, target_item)
    else:
        root = Path(temp_dir).absolute()
        if item.path and root.is_relative_to(physical_root(owner.uri)):
            relative = root.relative_to(physical_root(owner.uri))
            if relative.parts and not relative.parts[0].startswith('__'):
                raise BadArgument('temp_dir 不能放在业务表目录中')
    if root.is_symlink():
        raise UnsafeOperation('temp_dir 不能是符号链接')
    root.mkdir(parents=True, exist_ok=True)
    directory = root / operation_id
    directory.mkdir(mode=0o700)
    atomic_json(directory / 'task.json', {'operation_id': operation_id, 'state': 'preparing'})
    return directory


def _sync_tree(path):
    for base, directories, files in os.walk(path, topdown=False):
        for name in files:
            candidate = Path(base) / name
            if candidate.is_symlink():
                raise UnsafeOperation('暂存数据不能包含符号链接')
            with candidate.open('rb') as handle:
                os.fsync(handle.fileno())
        for name in directories:
            if (Path(base) / name).is_symlink():
                raise UnsafeOperation('暂存目录不能包含符号链接')
        sync_directory(base)


def _snapshot_stat(path):
    """不跟随快照项；Windows junction 需检查重解析属性，不能只看 symlink。"""
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise UnsafeOperation(f'数据库含符号链接或重解析点，拒绝复制: {path}')
    return value


def copy_snapshot(source, destination):
    """复制已锁定的目录快照，SQLite 必须包含已提交 WAL 页。"""
    source, destination = Path(source), Path(destination)
    _snapshot_stat(source)
    destination.mkdir(parents=True, exist_ok=False)
    for child in source.iterdir():
        info = _snapshot_stat(child)
        if child.name in ('__iosql_generations__', '__iosql_migrations__', '__generation__.json'):
            continue
        regular_file = stat.S_ISREG(info.st_mode)
        if regular_file and (child.name.endswith(('.sqlite3-wal', '.sqlite3-shm'))
                             or child.name == '.iosql.write.lock'):
            continue
        target = destination / child.name
        if stat.S_ISDIR(info.st_mode):
            copy_snapshot(child, target)
        elif regular_file and child.suffix == '.sqlite3':
            with closing(sqlite3.connect(child.resolve().as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(str(target))) as dst:
                    src.backup(dst)
                    dst.commit()
        elif regular_file:
            shutil.copy2(child, target)
        else:
            raise UnsafeOperation(f'数据库含非普通文件: {child}')


def _materialize_empty(table, contract):
    if table.get_table_meta() is not None:
        return
    from .utils.createtable import build_createtable_meta
    meta = build_createtable_meta(contract.columns, contract.primary_key, None,
                                  table_contract=contract.to_dict())
    part = contract.to_partition()
    meta['partition'] = part.to_meta() if part else None
    Path(table.table_part_path).mkdir(parents=True, exist_ok=True)
    table._overwrite_table_meta(meta)
    table._persist_contract_metadata(contract)


class LocalPublisher:
    def __init__(self, target_uri, coordinator, workspace, factory):
        self.uri = target_uri
        self.item = resource(target_uri)
        self.coordinator = coordinator
        self.workspace = workspace
        self.factory = factory
        self.backend = 'parquet' if self.item.backend == 'parquet' else 'sqlite'
        self.previous = physical_root(target_uri)
        self.stage_db = None
        self.candidate = None
        self.old_names = []
        self.old_entries = []
        self.retired = None
        self.previous_parts = {}
        self.decided = False

    def preflight(self, plans, verify):
        root = self.item.path
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise UnsafeOperation('目标必须是普通 iosql 库目录')
        if self.previous.exists() and any(self.previous.iterdir()):
            if not is_iosql_database(root, allow_empty=True, backend=self.backend, deep=True):
                raise UnsafeOperation('目标不是完整的 iosql 数据库，拒绝接管')
        if self.previous.is_dir():
            self.old_entries = [p.name for p in self.previous.iterdir()
                                if p.name not in ('__iosql_generations__', '__iosql_migrations__')]
        with self.factory(self.uri) as db:
            names = set(db.table_names())
            self.old_names = sorted(names)
            for plan in plans:
                if plan.target_name in names:
                    table = db[plan.target_name]
                    self.previous_parts[plan.name] = set(table.table_parts())
                    validate_contract(table.spec(), plan.target_spec)
                    if verify and table.count_rows() != 0:
                        raise BadArgument('row-count 校验失败：目标已存在表且非空；请使用空目标表')

    def prepare(self, plans):
        payload = self.workspace / 'payload'
        if self.previous.is_dir():
            copy_snapshot(self.previous, payload)
        else:
            payload.mkdir()
        uri = self.item.backend + '://' + str(payload)
        self.stage_db = self.factory(uri)
        for plan in plans:
            table = self.stage_db.table(plan.target_name, spec=plan.target_spec)
            _materialize_empty(table, plan.target_spec)

    def table(self, plan):
        return self.stage_db[plan.target_name]

    def finish_stage(self, plans):
        for plan in plans:
            target = self.table(plan)
            plan.result.target_rows = _count_rows(plan, 'target', target.count_rows)
            previous = self.previous_parts.get(plan.name, set())
            plan.result.partitions_created = [part for part in target.table_parts() if part not in previous]
            if plan.result.verify:
                _verify(plan, plan.result.target_rows)
        self.stage_db.close()
        self.stage_db = None
        payload = self.workspace / 'payload'
        emit('phase', message='检查暂存数据完整性')
        for plan in plans:
            if not is_iosql_table(payload / plan.target_name, backend=self.backend, deep=True):
                raise MigrationVerificationError(f'目标暂存表完整性校验失败: {plan.target_name}')

    def publish(self, operation_id):
        root = self.item.path
        generations = root / '__iosql_generations__'
        root.mkdir(parents=True, exist_ok=True)
        if generations.is_symlink():
            raise UnsafeOperation('代际目录不能是符号链接')
        generations.mkdir(exist_ok=True)
        self.candidate = generations / operation_id
        payload = self.workspace / 'payload'
        if payload.stat().st_dev == generations.stat().st_dev:
            os.rename(payload, self.candidate)
        else:
            copy_snapshot(payload, self.candidate)
        atomic_json(self.candidate / '__generation__.json', {'operation_id': operation_id})
        if not is_iosql_database(self.candidate, deep=True, backend=self.backend):
            raise MigrationVerificationError('目标代际完整性校验失败，尚未发布')
        _sync_tree(self.candidate)
        sync_directory(generations)
        sync_directory(root)
        sync_directory(root.parent)
        stat = self.candidate.stat()
        transaction = {
            'state': 'COMMIT_DECIDED', 'operation_id': operation_id,
            'physical_root': '__iosql_generations__/' + operation_id,
            'device': stat.st_dev, 'inode': stat.st_ino,
        }
        state = self.coordinator.state()
        # 即使写记录的 fsync 报错，也不能误删可能已经持久化的决定。
        self.decided = True
        self.coordinator.write_state(state.get('generation'), transaction)
        recover_local(self.coordinator, self.coordinator.state())
        # 仍持独占租约，先隔离旧内容；解锁后绝不再按逻辑 URI 删除。
        try:
            self.retire_previous(operation_id)
        except OSError as exc:
            warnings.warn(f'已发布，旧内容退休未完成，保留残留: {exc}', RuntimeWarning)

    def retire_previous(self, operation_id):
        parent = self.item.control_dir / (self.item.token + '.retired')
        if parent.is_symlink():
            raise OSError('退休目录不能是符号链接，旧内容保留在原位置')
        ensure_directory(parent)
        retired = parent / operation_id
        retired.mkdir(mode=0o700)
        self.retired = retired
        if self.previous != self.item.path:
            os.rename(self.previous, retired / 'generation')
        else:
            for name in self.old_entries:
                os.rename(self.previous / name, retired / name)
        sync_directory(retired)
        sync_directory(parent)

    def close(self):
        if self.stage_db is not None:
            self.stage_db.close()
            self.stage_db = None

    def cleanup(self, published):
        if published:
            if self.retired is not None:
                shutil.rmtree(self.retired)
            shutil.rmtree(self.workspace)


def recover_local(coordinator, state):
    """唯一恢复动作是重放完整代际指针；业务数据没有逐表替换窗口。"""
    record = state.get('transaction') or {}
    if record.get('state') != 'COMMIT_DECIDED':
        return
    operation_id = record.get('operation_id')
    relative = '__iosql_generations__/' + str(operation_id)
    if (not isinstance(operation_id, str) or len(operation_id) != 32
            or any(c not in '0123456789abcdef' for c in operation_id)
            or record.get('physical_root') != relative):
        raise MigrationRecoveryRequired('迁移恢复记录非法，拒绝发布')
    candidate = coordinator.resource.path / relative
    try:
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise ValueError('symlink')
        stat = candidate.stat()
        marker_path = candidate / '__generation__.json'
        if marker_path.is_symlink():
            raise ValueError('symlink marker')
        with marker_path.open(encoding='utf-8') as handle:
            marker = json.load(handle)
        if (stat.st_dev != record.get('device') or stat.st_ino != record.get('inode')
                or marker.get('operation_id') != operation_id):
            raise ValueError('identity')
    except (OSError, ValueError, AttributeError) as exc:
        raise MigrationRecoveryRequired('待发布代际缺失或身份不匹配，需要人工恢复',
                                        operation_id=operation_id) from exc
    coordinator.write_state(operation_id, {'state': 'COMMITTED', 'operation_id': operation_id},
                            physical_root=relative)


def _verify(plan, actual):
    if actual != plan.result.source_rows:
        plan.result.verified = False
        raise MigrationVerificationError(
            f'row-count verification failed: {plan.name}: '
            f'source={plan.result.source_rows}, target={actual}')
    plan.result.verified = True


def _count_rows(plan, phase, count):
    emit('count_start', phase=phase, table=plan.name)
    with counting(phase, plan.name):
        rows = count()
    emit('count_done', phase=phase, table=plan.name, rows=rows)
    return rows


def _copy(plans, publisher, source, batch_size, mode):
    for plan in plans:
        plan.result.source_rows = _count_rows(
            plan, 'source', lambda: source.count(plan) if source else plan.source.count_rows())
        target = publisher.table(plan)
        if (getattr(target, '_storage_engine', None) == 'sqlite'
                and hasattr(plan.source, '_logical_indexes')):
            for definition in plan.source._logical_indexes():
                target.create_index(definition['column_name'], definition['sort_order'])
        emit('copy_start', table=plan.name, total=plan.result.source_rows)
        iterator = source.batches(plan, batch_size) if source else plan.source.iter_batches(batch_size=batch_size)
        try:
            with closing(iterator):
                for rows in iterator:
                    if not rows:
                        continue
                    written = target.write(rows, mode=mode)
                    plan.result.rows_copied += len(rows)
                    plan.result.batches += 1
                    count = getattr(written, 'rows_written', None)
                    if count is None or plan.result.rows_written is None:
                        plan.result.rows_written = None
                    else:
                        plan.result.rows_written += count
                    emit('copy_progress', table=plan.name, rows=plan.result.rows_copied,
                         total=plan.result.source_rows)
        except Exception as exc:
            exc.source_table = plan.name
            exc.target_table = plan.target_name
            exc.batch = plan.result.batches + 1
            raise
        emit('copy_done', table=plan.name, rows=plan.result.rows_copied,
             total=plan.result.source_rows)


def migrate(owner, target_uri, tables=None, mode='append', batch_size=10_000,
            verify=None, dry_run=False, *, specs=None, temp_dir=None, lock_timeout=30.0,
            _same_database=False):
    from .database import Database, _normalize_migration_verify
    source_uri = owner.uri
    batch_size = _batch_size(batch_size)
    verify = _normalize_migration_verify(verify)
    mode = Database._normalize_migration_mode(mode)
    timeout = timeout_value(lock_timeout)
    if temp_dir is not None:
        try:
            temp_dir = os.fspath(temp_dir)
        except TypeError as exc:
            raise BadArgument('temp_dir 必须是路径字符串或 PathLike') from exc
        if not isinstance(temp_dir, str) or not temp_dir:
            raise BadArgument('temp_dir 必须是非空路径字符串')
    names = list(tables) if isinstance(tables, (dict, list, tuple, set)) else []
    if any(not isinstance(name, str) or not name for name in names):
        raise BadArgument('tables 中的源表名必须是非空字符串')
    Database._validate_migration_target(target_uri)
    target_item, source_item = resource(target_uri), resource(source_uri)
    from .utils.schema import parse_db_schema
    for location, item in ((target_uri, target_item), (owner.uri, source_item)):
        if item.path is not None:
            _, options = parse_db_schema(location)
            if Path(os.path.join(options.get('path') or '', options.get('db') or '')).is_symlink():
                raise UnsafeOperation('源/目标库目录不能是符号链接')
    if (temp_dir is not None and target_item.path is not None
            and Path(temp_dir).resolve().is_relative_to(target_item.path)):
        raise BadArgument('temp_dir 不能位于目标库内，请选择源库管理目录或独立目录')
    if source_item.backend == 'memory' or target_item.backend == 'memory':
        raise BadArgument('安全迁移需要持久数据库 URI')
    if source_item.key == target_item.key and not _same_database:
        raise BadArgument('源库与目标库不能相同；请指定新的目标库')
    if source_item.path and target_item.path:
        common = Path(os.path.commonpath((source_item.path, target_item.path)))
        if common in (source_item.path, target_item.path) and not (
                _same_database and source_item.key == target_item.key):
            raise BadArgument('目标库不能位于源库内部，源库也不能位于目标库内部')
    operation_id = uuid.uuid4().hex
    aggregate = MigrationResult(source_uri, target_uri, verify=verify, dry_run=dry_run, published=False,
                                operation_id=operation_id)
    publisher = None
    published = False
    stage = 'waiting-locks'
    lock_deadline = time.monotonic() + timeout
    def plan_sources():
        if owner.uri != source_uri:
            raise UnsafeOperation('源对象 URI 在迁移预检后发生变化，请重新发起迁移')
        mapping = _mapping(owner, tables)
        if source_item.key == target_item.key and set(mapping) & set(mapping.values()):
            raise BadArgument('repartition_to requires a different target uri or table')
        _check_names(mapping, target_item.backend)
        planned = _plan(owner, mapping, specs, target_uri, verify, dry_run, operation_id)
        aggregate.tables = [plan.result for plan in planned]
        return planned

    try:
        try:
            emit('phase', message='等待源库锁并检查迁移计划')
            with lease(source_uri, recover=not dry_run, timeout=(
                    0.0 if dry_run else max(0.0, lock_deadline - time.monotonic()))):
                if owner.uri != source_uri:
                    raise UnsafeOperation('源对象 URI 在迁移预检后发生变化，请重新发起迁移')
                with operation(owner, uri=source_uri, recover=not dry_run):
                    stage = 'planning'
                    plans = plan_sources()
                    if dry_run:
                        for plan in plans:
                            plan.result.source_rows = _count_rows(plan, 'source', plan.source.count_rows)
                            plan.result.batches = math.ceil(plan.result.source_rows / batch_size)
                        return aggregate if isinstance(owner, Database) else aggregate.tables[0]
        except DatabaseBusy as exc:
            if dry_run:
                raise
            from .exception import MigrationLockTimeout
            raise MigrationLockTimeout('等待源库预检锁超过 lock_timeout') from exc
        stage = 'waiting-locks'
        emit('phase', message='等待源库和目标库的迁移锁')
        with ExitStack() as stack:
            entries = stack.enter_context(exclusive_resources((source_uri, target_uri),
                max(0.0, lock_deadline - time.monotonic())))
            if owner.uri != source_uri:
                raise UnsafeOperation('源对象 URI 在迁移预检后发生变化，请重新发起迁移')
            stack.enter_context(operation(owner, uri=source_uri))
            stage = 'planning'
            emit('phase', message='检查迁移计划与源表')
            plans = plan_sources()
            source = None
            if source_item.backend == 'postgresql' and not dry_run:
                stage = 'locking-source'
                from .migration_postgresql import SourceSnapshot
                source = stack.enter_context(SourceSnapshot(
                    source_uri, plans, max(0.0, lock_deadline - time.monotonic())))
            coordinator = entries[target_item.key].coordinator
            if hasattr(owner, '_new_migration_target'):
                factory = owner._new_migration_target
            else:
                def factory(uri):
                    options = {'verbose': owner.verbose}
                    if resource(uri).path is not None:
                        for key, attr in (('max_open_partitions', '_num_opened_max_allowed'),
                                          ('max_connections_per_file', '_max_connections_per_file'),
                                          ('max_process_connections', '_max_process_connections'),
                                          ('max_ordered_read_connections', '_max_ordered_read_connections')):
                            if hasattr(owner, attr):
                                options[key] = getattr(owner, attr)
                        if resource(uri).backend in ('sqlite', 'sqlite3') and hasattr(owner, '_pragma_dict'):
                            options['pragma_dict'] = owner._pragma_dict
                    return Database(uri, **options)
            if target_item.backend == 'postgresql':
                from .migration_postgresql import PostgreSQLPublisher
                workspace = _workspace(owner, target_item, temp_dir, operation_id)
                publisher = PostgreSQLPublisher(target_uri, coordinator, workspace, factory,
                                                max(0.0, lock_deadline - time.monotonic()))
            else:
                # 先验证目标身份，才能安全地在目标根挂载点内创建管理目录。
                publisher = LocalPublisher(target_uri, coordinator, None, factory)
            publisher.mode = mode
            publisher.batch_size = batch_size
            stack.callback(publisher.close)
            stage = 'preflight'
            emit('phase', message='检查目标库')
            publisher.preflight(plans, verify)
            stage = 'staging'
            emit('phase', message='准备目标暂存空间')
            if target_item.path is not None:
                publisher.workspace = _workspace(owner, target_item, temp_dir, operation_id)
            publisher.prepare(plans)
            stage = 'copying'
            _copy(plans, publisher, source, batch_size, mode)
            stage = 'verification'
            emit('phase', message='统计并校验目标数据')
            publisher.finish_stage(plans)
            stage = 'publishing'
            emit('phase', message='发布目标库')
            publisher.publish(operation_id)
            published = True
            aggregate.published = True
            for plan in plans:
                plan.result.published = True
    except MigrationError as exc:
        if publisher is not None and getattr(publisher, 'decided', False) and not published:
            aggregate.published = None
            for item in aggregate.tables:
                item.published = None
        exc.operation_id = exc.operation_id or operation_id
        exc.recovery_path = getattr(exc, 'recovery_path', None) or (
            str(target_item.state_path) if target_item.path else '__iosql_control.migrations')
        exc.stage = exc.stage or stage
        exc.partial_result = aggregate if isinstance(owner, Database) else (
            aggregate.tables[0] if aggregate.tables else None)
        raise
    except IosqlError as exc:
        if publisher is not None and getattr(publisher, 'decided', False) and not published:
            aggregate.published = None
            for item in aggregate.tables:
                item.published = None
        exc.operation_id = operation_id
        exc.recovery_path = getattr(exc, 'recovery_path', None) or (
            str(target_item.state_path) if target_item.path else '__iosql_control.migrations')
        exc.partial_result = aggregate if isinstance(owner, Database) else (
            aggregate.tables[0] if aggregate.tables else None)
        exc.stage = stage
        raise
    except Exception as exc:
        if publisher is not None and getattr(publisher, 'decided', False):
            aggregate.published = None
            for item in aggregate.tables:
                item.published = None
        raise MigrationError('迁移未完成，正式目标不会暴露部分结果；请检查原始异常',
                             recovery_path=(str(target_item.state_path) if target_item.path
                                            else '__iosql_control.migrations'),
                             stage=stage, source_table=getattr(exc, 'source_table', None),
                             target_table=getattr(exc, 'target_table', None),
                             batch=getattr(exc, 'batch', None),
                             operation_id=operation_id, partial_result=(aggregate if isinstance(owner, Database)
                             else aggregate.tables[0] if aggregate.tables else None)) from exc
    finally:
        # 此处源事务、目标事务与所有独占租约均已释放。
        if publisher is not None:
            def cleanup():
                try:
                    publisher.cleanup(published)
                except Exception as exc:
                    warnings.warn(f'迁移临时数据清理失败（不影响已确认的提交）: {exc}', RuntimeWarning)
            after_leases(cleanup)
    return aggregate if isinstance(owner, Database) else aggregate.tables[0]


def migrate_table(owner, target_uri, tables=OMITTED, mode='append', batch_size=10_000,
                  verify=None, dry_run=False, *, specs=None, temp_dir=None,
                  lock_timeout=30.0, target_table=OMITTED):
    if target_table is not OMITTED:
        if tables is not OMITTED:
            raise BadArgument('tables 与 target_table 不能同时传入')
        tables = {owner.table_name: target_table or owner.table_name}
    return migrate(owner, target_uri, tables=None if tables is OMITTED else tables,
                   mode=mode, batch_size=batch_size, verify=verify, dry_run=dry_run,
                   specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout)


def publish_snapshot(source_uri, target_uri, *, lock_timeout=30.0):
    """维护入口：完整快照替换目标，同样通过代际指针发布。"""
    from .database import Database
    if resource(source_uri).backend not in ('sqlite', 'sqlite3', 'parquet'):
        raise BadArgument('目录快照仅支持本地数据库')
    source_item, target_item = resource(source_uri), resource(target_uri)
    if source_item.key == target_item.key or Path(os.path.commonpath(
            (source_item.path, target_item.path))) in (source_item.path, target_item.path):
        raise BadArgument('快照源和目标目录不能相同或互相包含')
    operation_id = uuid.uuid4().hex
    publisher = None
    published = False
    try:
        with exclusive_resources((source_uri, target_uri), lock_timeout) as entries:
            if not is_iosql_database(source_item.path, deep=True,
                                     backend='parquet' if source_item.backend == 'parquet' else 'sqlite'):
                raise UnsafeOperation('快照源不是完整的 iosql 库')
            import tempfile
            parent = Path(tempfile.mkdtemp(prefix='iosql-snapshot-'))
            workspace = parent / operation_id
            workspace.mkdir()
            publisher = LocalPublisher(target_uri, entries[target_item.key].coordinator,
                                       workspace, Database)
            try:
                publisher.preflight([], None)
                copy_snapshot(physical_root(source_uri), workspace / 'payload')
                publisher.publish(operation_id)
                published = True
            finally:
                publisher.close()
    finally:
        if publisher is not None:
            def cleanup():
                try:
                    publisher.cleanup(published)
                    if published:
                        publisher.workspace.parent.rmdir()
                except OSError as exc:
                    warnings.warn(f'快照发布后的清理未完成: {exc}', RuntimeWarning)
            after_leases(cleanup)


def backup_snapshot(source_uri, destination):
    """备份仅含已发布数据，SQLite WAL 使用 backup API 合并。"""
    with lease(source_uri, exclusive=True, timeout=30.0):
        source = physical_root(source_uri)
        if not is_iosql_database(resource(source_uri).path, deep=True):
            raise UnsafeOperation('源库不是完整的 iosql 库')
        copy_snapshot(source, destination)
        _sync_tree(destination)
        sync_directory(Path(destination).parent)


def rename_published_database(database, target_uri):
    """有代际控制时复制完整视图，避免移动路径后丢失外置路由记录。"""
    from .coordination import _held
    source_uri = database.uri
    source_item = resource(source_uri)
    with database._tables_lock:
        cached = list(database._tables.values())
    for table in cached:
        table.close()
    publish_snapshot(source_uri, target_uri, lock_timeout=0.0)
    # 提交后的目标已有完整独立副本，旧路径不会再作为新库物理路径使用。
    source_coordinator = _held()[source_item.key].coordinator
    previous_state = source_coordinator.state()
    retired = source_item.path.with_name(source_item.path.name + '.__retired__' + uuid.uuid4().hex)
    os.rename(source_item.path, retired)
    source_coordinator.write_state(uuid.uuid4().hex, physical_root=None, previous_state=previous_state)

    def cleanup_retired():
        try:
            shutil.rmtree(retired)
        except OSError as exc:
            warnings.warn(f'重命名后旧目录清理未完成: {retired}: {exc}', RuntimeWarning)

    after_leases(cleanup_retired)
    database._uri = target_uri
    database._iosql_generation = _held()[resource(target_uri).key].generation
    for table in cached:
        table._iosql_explicitly_closed = False
        table._rebind_uri(target_uri)
        table._iosql_generation = database._iosql_generation
        if hasattr(table, '_load_partition'):
            table._partition = table._load_partition()
    return database


def repartition(owner, target_uri, table_name=None, spec=None, mode='append', batch_size=10_000):
    """旧接口沿用返回类型，所有数据转换复用安全迁移。"""
    from .spec import RepartitionResult
    target_name = table_name or owner.table_name
    from .database import Database
    with exclusive_resources((owner.uri, target_uri)):
        try:
            result = migrate(owner, target_uri, tables={owner.table_name: target_name}, mode=mode,
                             batch_size=batch_size,
                             specs={owner.table_name: spec} if spec is not None else None,
                             _same_database=True)
        except MigrationError as exc:
            if type(exc) is MigrationError and exc.__cause__ is not None:
                raise exc.__cause__ from exc
            raise
        # 兼容旧 RepartitionResult：其分区列表一直是目标全部分区。
        with Database(target_uri) as target:
            partitions = list(target[target_name].table_parts())
    return RepartitionResult(source_uri=owner.uri, target_uri=target_uri, table=target_name,
                             rows_copied=result.rows_copied, partitions_created=partitions)
