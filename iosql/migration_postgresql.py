"""PostgreSQL 迁移专用连接；正式数据只由一次事务提交。"""

from __future__ import annotations

import json
import math
import shutil
import uuid
from contextlib import closing
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

from .coordination_postgresql import task_lock
from .exception import BadArgument, MigrationLockTimeout, MigrationRecoveryRequired
from .migration import _verify, validate_contract
from .spec import TableContract


def _connect(uri, timeout):
    options = {'application_name': 'iosql-migration'}
    if 'connect_timeout' not in psycopg2.extensions.parse_dsn(uri):
        options['connect_timeout'] = max(1, math.ceil(timeout))
    return psycopg2.connect(uri, **options)


def _configure(connection, timeout):
    connection.autocommit = False
    with connection.cursor() as cursor:
        # PG 的零表示无限等待；API 的零表示立即尝试，最小取 1ms。
        cursor.execute("SELECT set_config('lock_timeout', %s, true)",
                       (str(max(1, math.ceil(timeout * 1000))),))
        cursor.execute('SELECT current_schema()')
        schema = cursor.fetchone()[0]
        if schema is None:
            raise BadArgument('PostgreSQL search_path 没有可用 schema')
        return schema


def _lock(connection, schema, names):
    if not names:
        return
    with connection.cursor() as cursor:
        try:
            cursor.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(
                sql.SQL(', ').join(sql.Identifier(schema, name) for name in sorted(names))))
        except psycopg2.errors.LockNotAvailable as exc:
            raise MigrationLockTimeout('等待 PostgreSQL 表锁超过 lock_timeout') from exc


class SourceSnapshot:
    def __init__(self, uri, plans, timeout):
        self.uri, self.plans, self.timeout = uri, plans, timeout
        self.connection = None

    def __enter__(self):
        try:
            self.connection = _connect(self.uri, self.timeout)
            self.schema = _configure(self.connection, self.timeout)
            _lock(self.connection, self.schema, [plan.name for plan in self.plans])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def count(self, plan):
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(
                sql.Identifier(self.schema, plan.name)))
            return cursor.fetchone()[0]

    def batches(self, plan, batch_size):
        with self.connection.cursor(name='iosql_source_' + uuid.uuid4().hex) as cursor:
            cursor.itersize = batch_size
            cursor.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(self.schema, plan.name)))
            names = [item[0] for item in cursor.description] if cursor.description else None
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                if names is None:
                    names = [item[0] for item in cursor.description]
                yield [dict(zip(names, row)) for row in rows]

    def __exit__(self, *args):
        if self.connection is not None:
            try:
                self.connection.rollback()
            finally:
                self.connection.close()


def _adapter(uri, connection, schema, name, contract):
    from .engines.postgresql.table import Table
    table = Table(uri, name)
    table._conn = connection
    table._schema = schema
    table._contract = contract
    table._partition = contract.to_partition()
    table._primary_key = list(contract.primary_key)
    table._declared_column_types = dict(contract.columns)
    table._in_explicit_tx = True
    table._iosql_migration_owned = True
    return table


def _stage_uri(uri, schema):
    parts = urlsplit(uri)
    params = list(parse_qsl(parts.query, keep_blank_values=True))
    options = ' '.join(value for key, value in params if key == 'options')
    params = [(key, value) for key, value in params if key != 'options']
    params.append(('options', (options + ' -csearch_path=' + schema).strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(params, quote_via=quote), parts.fragment))


class PostgreSQLPublisher:
    def __init__(self, uri, coordinator, workspace, factory, timeout):
        self.uri, self.coordinator, self.workspace = uri, coordinator, workspace
        self.factory, self.timeout = factory, timeout
        self.operation_id = workspace.name
        self.stage_schema = '__iosql_migration_' + self.operation_id
        self.connection = None
        self.stage_db = None
        self.adapters = {}
        self.plans = []
        self.begun = False
        self.committed = False
        self.decided = False
        self.existed = {}
        self.previous_parts = {}
        self.batch_size = 10_000
        self.mode = 'append'

    def preflight(self, plans, verify):
        self.plans = plans
        self.coordinator.ensure_control()
        self.connection = _connect(self.uri, self.timeout)
        self.schema = _configure(self.connection, self.timeout)
        existing = []
        for plan in plans:
            table = _adapter(self.uri, self.connection, self.schema, plan.target_name, plan.target_spec)
            table._validate_contract_metadata(plan.target_spec)
            for type_ in plan.target_spec.columns.values():
                table._pg_column_type(type_)
            part = table._partition
            if part is not None and (part.TS_COL not in plan.target_spec.columns or
                                     table._primary_key and part.TS_COL not in table._primary_key):
                raise BadArgument('PostgreSQL 目标分区列必须已声明并包含在主键中')
            if plan.target_spec.segment:
                # PG 不按本地段分文件；保留契约用于迁回目录后端。
                pass
            table._table_created = table._relation_exists(plan.target_name)
            self.existed[plan.name] = table._table_created
            self.adapters[plan.name] = table
            if table._table_created:
                existing.append(plan.target_name)
        _lock(self.connection, self.schema, existing)
        for plan in plans:
            table = self.adapters[plan.name]
            if table._table_created:
                table._verify_existing_table(plan.target_spec.columns, table._partition)
                self._verify_layout(table, plan)
                self.previous_parts[plan.name] = set(table.table_parts())
                if verify and table.count_rows() != 0:
                    raise BadArgument('row-count 校验仅支持不存在或为空的目标表')

    def _verify_layout(self, table, plan):
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT contract FROM __iosql_control.table_contracts '
                           'WHERE schema_name=%s AND table_name=%s AND relation_oid=to_regclass(%s)',
                           (self.schema, plan.target_name, table._qualified_relation()))
            row = cursor.fetchone()
            if row:
                stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                validate_contract(TableContract.from_dict(stored), plan.target_spec)
                return
            if table._partition is None:
                return
            cursor.execute('''SELECT pg_get_expr(c.relpartbound, c.oid)
                FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
                WHERE i.inhparent=to_regclass(%s)''', (table._qualified_relation(),))
            expressions = [item[0] for item in cursor.fetchall()]
        # 旧表没有 iosql 持久契约时，对每个已有子分区验证边界。
        from .engines.postgresql.table import _PARTITION_BOUNDS_RE
        for expression in expressions:
            match = _PARTITION_BOUNDS_RE.search(expression)
            if match is None:
                raise BadArgument('目标包含无法确认布局的分区，请改用新的目标表')
            start = match.group(1).strip().split('::')[0].strip().strip("'")
            try:
                value = float(start) if '.' in start else int(start)
            except ValueError:
                value = start
            sample = {table._partition.TS_COL: value}
            try:
                key = table._native_partition_key(table._partition, sample)
                bounds = table._partition_bounds(table._partition, key, [sample])
            except (TypeError, ValueError) as exc:
                raise BadArgument('无法确认目标已有分区布局，请改用新的目标表') from exc
            if not table._partition_bounds_equal(expression, *bounds):
                raise BadArgument('目标已有分区边界与最终 specs 不兼容，请改用新的目标表')

    def prepare(self, plans):
        self.coordinator.begin_operation(self.operation_id, self.stage_schema)
        self.begun = True
        with self.coordinator.connection.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.stage_schema)))
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', (task_lock(self.operation_id),))
        self.stage_db = self.factory(_stage_uri(self.uri, self.stage_schema))
        for plan in plans:
            self.stage_db.table(plan.target_name, spec=plan.target_spec)

    def table(self, plan):
        return self.stage_db[plan.target_name]

    def finish_stage(self, plans):
        for plan in plans:
            table = self.table(plan)
            actual = table.count_rows()
            if plan.result.verify:
                _verify(plan, actual)
            plan.result.target_rows = actual

    def publish(self, operation_id):
        for plan in self.plans:
            official = self.adapters[plan.name]
            if not self.existed[plan.name] and official._relation_exists(plan.target_name):
                raise BadArgument('迁移期间目标关系被外部创建，请确认目标后重试')
            # 新表也只在正式事务中建立，不能在转换时创建正式空表。
            official.create_table(plan.target_spec.columns, plan.target_spec.primary_key,
                                  partition=official._partition)
            written_count = 0
            with closing(self.table(plan).iter_batches(batch_size=self.batch_size)) as batches:
                for batch in batches:
                    result = official.write(batch, mode=self.mode, commit=False)
                    if result.rows_written is None or written_count is None:
                        written_count = None
                    else:
                        written_count += result.rows_written
            plan.result.rows_written = written_count
            plan.result.target_rows = official.count_rows()
            if plan.result.verify:
                _verify(plan, plan.result.target_rows)
            previous = self.previous_parts.get(plan.name, set())
            plan.result.partitions_created = [part for part in official.table_parts() if part not in previous]
            with self.connection.cursor() as cursor:
                cursor.execute('''INSERT INTO __iosql_control.table_contracts
                    (schema_name,table_name,relation_oid,contract) VALUES (%s,%s,to_regclass(%s),%s)
                    ON CONFLICT (schema_name,table_name) DO UPDATE SET
                    relation_oid=excluded.relation_oid,contract=excluded.contract''',
                    (self.schema, plan.target_name, official._qualified_relation(),
                     json.dumps(plan.target_spec.to_dict())))
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE __iosql_control.migrations SET state='committed' WHERE operation_id=%s",
                           (operation_id,))
            cursor.execute('UPDATE __iosql_control.database_state SET generation=%s,operation_id=NULL '
                           'WHERE singleton=TRUE', (operation_id,))
        try:
            self.decided = True
            self.connection.commit()
            self.committed = True
        except Exception as exc:
            # 不能根据断连断言回滚。关闭旧会话后，任务锁确认结果已稳定。
            self.connection.close()
            self.connection = None
            self.coordinator.recover({'transaction': {'operation_id': operation_id}})
            with self.coordinator.connection.cursor() as cursor:
                cursor.execute('SELECT state FROM __iosql_control.migrations WHERE operation_id=%s',
                               (operation_id,))
                self.committed = cursor.fetchone()[0] == 'committed'
            if not self.committed:
                self.decided = False
                raise MigrationRecoveryRequired('PostgreSQL 迁移事务未提交，可重新发起迁移',
                                                operation_id=operation_id) from exc
        from .coordination import _held
        _held()[self.coordinator.resource.key].generation = operation_id

    def close(self):
        # 不让一次关闭失败阻断其余专用连接的释放。
        try:
            if self.connection is not None:
                try:
                    if not self.committed:
                        self.connection.rollback()
                finally:
                    self.connection.close()
                    self.connection = None
        finally:
            try:
                if self.stage_db is not None:
                    self.stage_db.close()
                    self.stage_db = None
            finally:
                if self.begun and not self.committed:
                    self.coordinator.recover({'transaction': {'operation_id': self.operation_id}})

    def cleanup(self, published):
        if not published:
            return
        # 锁外清理只处理本次拥有的唯一 schema，不删除稳定提交记录。
        with closing(_connect(self.uri, self.timeout)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
                    sql.Identifier(self.stage_schema)))
        shutil.rmtree(self.workspace)
