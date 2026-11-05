"""复用目录表契约与 manifest，业务数据由 DuckDB 读写 Parquet。"""

from __future__ import annotations

import os
import numbers
import math
import tempfile
import threading
import time
import json
import uuid
import warnings
from contextlib import contextmanager

from ...migration import OMITTED
from ...coordination import atomic_json, sync_directory
from ...exception import BadArgument, SchemaConflict, TableSpecConflict, UnsafeOperation
from ...spec import PhysicalLayout, SegmentInfo, TableContract
from ...utils.createtable import build_createtable_meta
from ...utils.mysqlite.connection_pool import reserve_process_connections, release_process_connections
from ..sqlite.table import Table as DirectoryTable, _canon_sqlite_type
from ..sqlite.query_util import build_delete_query, build_select_count_query


def _duckdb():
    try:
        import duckdb
    except ImportError as exc:
        raise BadArgument(
            'Parquet 后端需要 DuckDB；请安装或更新 iosql: '
            'python -m pip install --upgrade iosql'
        ) from exc
    return duckdb


def _sql_type(value):
    type_ = _canon_sqlite_type(value)
    if type_ is None:
        raise BadArgument(f'Parquet 不支持列类型 `{value}`；支持 integer/float/text/blob')
    return {'integer': 'BIGINT', 'float': 'DOUBLE', 'text': 'VARCHAR', 'blob': 'BLOB'}[type_]


class _Row:
    """兼容目录查询的按列名/序号访问，以及 dict(row)/tuple(row)。"""

    def __init__(self, names, values):
        self._names = names
        self._values = values

    def keys(self):
        return self._names

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._names.index(key)]
        except ValueError as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        return iter(self._values)


class _Scan:
    """只读文件查询适配器，不持有跨请求连接或可变数据缓存。"""

    def __init__(self, conn):
        self.conn = conn

    def __getitem__(self, name):
        if name != 'data':
            raise BadArgument('Parquet 文件仅包含 data 表')
        return self

    def query(self, sql, params=None, to_dict=False):
        cursor = self.conn.execute(sql, params or [])
        names = ['COUNT(*)' if d[0] == 'count_star()' else d[0] for d in cursor.description]
        rows = [_Row(names, values) for values in cursor.fetchall()]
        return [dict(row) for row in rows] if to_dict else rows

    def count(self):
        return self.conn.execute('SELECT COUNT(*) FROM data').fetchone()[0]

    def select(self, fields='*', where=None, to_dict=True):
        if fields != '*' or where is not None:
            raise BadArgument('内部 Parquet 全表读取仅支持 select("*")')
        return self.query('SELECT * FROM data', to_dict=to_dict)


class Table(DirectoryTable):
    _storage_engine = 'parquet'
    _data_suffix = '.parquet'
    _reader_counts = {}
    _reader_counts_lock = threading.RLock()

    def __init__(self, *args, **kwargs):
        _duckdb()
        super().__init__(*args, **kwargs)

    def _overwrite_table_meta(self, meta):
        meta['storage_engine'] = self._storage_engine
        return super()._overwrite_table_meta(meta)

    def _use_contract_locked(self, contract):
        # 空表会在本层立即落盘，必须与父类使用同一个日期路由契约。
        contract = self._date_key_contract(TableContract.ensure(contract))
        for type_ in contract.columns.values():
            _sql_type(type_)
        result = super()._use_contract_locked(contract)
        # 空表也持久化契约，重开无需重复声明，且能识别正确的后端。
        if self.get_table_meta() is None:
            meta = build_createtable_meta(
                contract.columns, contract.primary_key, None,
                table_contract=contract.to_dict(),
            )
            partition = contract.to_partition()
            meta['partition'] = partition.to_meta() if partition else None
            self._overwrite_table_meta(meta)
            self._persist_contract_metadata(contract)
        os.makedirs(self.table_part_path, exist_ok=True)
        return result

    def insert_all(self, data, pk=None, extra_data=None, partition=None,
                   replace=False, ignore=False, commit=True, **kwargs):
        if not commit:
            raise BadArgument('Parquet 不支持 commit=False；请用批量 write 或 buffered_writer')
        if isinstance(data, dict):
            data = [data]
        elif data is not None:
            data = list(data)
        for index, row in enumerate(data or []):
            if not isinstance(row, dict):
                raise BadArgument(f'row[{index}] 必须是 dict')
        return super().insert_all(
            data, pk=pk, extra_data=extra_data, partition=partition,
            replace=replace, ignore=ignore, commit=commit, **kwargs,
        )

    @contextmanager
    def _mutation_lock(self):
        with self._db_parts_condition:
            if self._db_parts_closed or self._db_parts_closing:
                raise UnsafeOperation('表已关闭，请重新打开')
        key = (os.path.realpath(self.table_path), threading.get_ident())
        with self._reader_counts_lock:
            if self._reader_counts.get(key, 0):
                raise UnsafeOperation('当前线程仍持有 Parquet 读取流；请先耗尽或关闭流再写入')
        with super()._mutation_lock():
            yield

    @contextmanager
    def _read_lease(self, path, deadline=None):
        thread_id = threading.get_ident()
        key = (os.path.realpath(self.table_path), thread_id)
        with self._db_parts_condition:
            if self._db_parts_closed or self._db_parts_closing:
                raise UnsafeOperation('表已关闭，请重新打开')
            self._db_parts_leases[path] = self._db_parts_leases.get(path, 0) + 1
            self._db_parts_lease_threads[thread_id] = self._db_parts_lease_threads.get(thread_id, 0) + 1
        with self._reader_counts_lock:
            self._reader_counts[key] = self._reader_counts.get(key, 0) + 1
        fd = None
        try:
            if getattr(self._mutation_lock_state, 'depth', 0):
                yield
                return
            try:
                import fcntl
            except ImportError:
                # Windows 沿用目录后端的互斥文件锁；同线程可重入。
                with super()._mutation_lock():
                    yield
                return
            fd = os.open(os.path.join(self.table_path, '.iosql.write.lock'),
                         os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('等待 Parquet 读取锁超过 timeout_ms')
                    time.sleep(0.01)
            yield
        finally:
            if fd is not None:
                os.close(fd)
            with self._reader_counts_lock:
                self._reader_counts[key] -= 1
                if not self._reader_counts[key]:
                    del self._reader_counts[key]
            with self._db_parts_condition:
                self._db_parts_leases[path] -= 1
                if not self._db_parts_leases[path]:
                    del self._db_parts_leases[path]
                self._db_parts_lease_threads[thread_id] -= 1
                if not self._db_parts_lease_threads[thread_id]:
                    del self._db_parts_lease_threads[thread_id]
                self._db_parts_condition.notify_all()

    @contextmanager
    def _connection(self, deadline=None, reserved=False):
        reservation = None if reserved else reserve_process_connections(1, self._max_process_connections)
        try:
            with tempfile.TemporaryDirectory(prefix='iosql-parquet-') as temp_dir:
                conn = _duckdb().connect(config={
                    'threads': 1, 'memory_limit': '256MB', 'temp_directory': temp_dir,
                })
                timer = None
                try:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Parquet 查询超过 timeout_ms')
                        timer = threading.Timer(remaining, conn.interrupt)
                        timer.daemon = True
                        timer.start()
                    yield conn
                except _duckdb().InterruptException as exc:
                    raise TimeoutError('Parquet 查询超过 timeout_ms') from exc
                finally:
                    if timer is not None:
                        timer.cancel()
                        timer.join()
                    conn.close()
        finally:
            release_process_connections(reservation)

    def _create_view(self, conn, path, columns=None):
        if not os.path.isfile(path) or os.path.islink(path):
            raise UnsafeOperation(f'Parquet 数据文件缺失或为符号链接: {path}')
        try:
            relation = conn.read_parquet(self._parquet_files(path), union_by_name=True)
            physical = set(relation.columns)
            if columns is None:
                columns = (self.get_table_meta() or {}).get('column_info') or {}
            projections = []
            for column, info in columns.items():
                field = self._quote_identifier(column)
                projections.append(
                    field if column in physical
                    else f'CAST(NULL AS {_sql_type(info["type"])}) AS {field}'
                )
            relation.project(', '.join(projections) if projections else '*').create_view('data')
        except _duckdb().InterruptException:
            raise
        except _duckdb().Error as exc:
            raise UnsafeOperation(f'无法读取 Parquet 文件 {path}: {exc}') from exc

    @contextmanager
    def _part_db(self, part_dbfile, auto_commit=True):
        columns = (self.get_table_meta() or {}).get('column_info') or {}
        with self._read_lease(part_dbfile), self._connection() as conn:
            self._create_view(conn, part_dbfile, columns)
            try:
                yield _Scan(conn)
            except _duckdb().BinderException as exc:
                raise BadArgument(f'Parquet 查询字段或类型不兼容: {exc}') from exc
            except _duckdb().Error as exc:
                raise UnsafeOperation(f'Parquet 查询失败 {part_dbfile}: {exc}') from exc

    def _mutable_table(self, conn, path, columns, pk):
        definitions = [
            f'{self._quote_identifier(column)} {_sql_type(type_)}'
            for column, type_ in columns.items()
        ]
        if pk:
            definitions.append('PRIMARY KEY (' + ', '.join(
                self._quote_identifier(column) for column in pk
            ) + ')')
        conn.execute('CREATE TABLE data (' + ', '.join(definitions) + ')')
        if path is not None and os.path.exists(path):
            if os.path.islink(path):
                raise UnsafeOperation(f'Parquet 数据文件不能是符号链接: {path}')
            relation = conn.read_parquet(self._parquet_files(path), union_by_name=True)
            physical = set(relation.columns)
            relation.create_view('source_data')
            fields = [self._quote_identifier(column) for column in columns]
            expressions = [field if column in physical else 'NULL'
                           for column, field in zip(columns, fields)]
            conn.execute('INSERT INTO data SELECT ' + ', '.join(expressions) + ' FROM source_data')

    def _parquet_files(self, path):
        """清单是物理集合的唯一发布点；缺文件或越界引用不能退化为空数据。"""
        manifest = path + '.manifest.json'
        if not os.path.exists(manifest):
            return [path]
        if os.path.islink(manifest):
            raise UnsafeOperation(f'Parquet 清单不能是符号链接: {manifest}')
        try:
            with open(manifest, encoding='utf-8') as handle:
                payload = json.load(handle)
            files = payload['files']
            if payload.get('format') != 'iosql-parquet-parts-v1' or not isinstance(files, list) or not files:
                raise ValueError('invalid manifest')
            directory = self._fragment_directory(path)
            if os.path.islink(directory):
                raise ValueError('symlink directory')
            resolved = []
            for name in files:
                if name == '@base':
                    candidate = path
                elif (isinstance(name, str) and len(name) == 40 and name.endswith('.parquet')
                      and all(char in '0123456789abcdef' for char in name[:-8])):
                    candidate = os.path.join(directory, name)
                else:
                    raise ValueError('invalid fragment name')
                if not os.path.isfile(candidate) or os.path.islink(candidate):
                    raise ValueError('missing or unsafe fragment')
                resolved.append(candidate)
            if len(resolved) != len(set(resolved)):
                raise ValueError('duplicate fragment')
            return resolved
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UnsafeOperation(f'Parquet 数据清单损坏: {manifest}') from exc

    @staticmethod
    def _fragment_directory(path):
        return os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.parts')

    def _publish(self, conn, path, append=False):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.islink(path):
            raise UnsafeOperation(f'Parquet 数据文件不能是符号链接: {path}')
        initial = not os.path.exists(path)
        directory = os.path.dirname(path) if initial else self._fragment_directory(path)
        if os.path.islink(directory):
            raise UnsafeOperation(f'Parquet 数据片目录不能是符号链接: {directory}')
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.iosql-', suffix='.tmp', dir=directory)
        os.close(fd)
        try:
            conn.table('data').write_parquet(temporary, compression='zstd')
            with open(temporary, 'rb') as file:
                os.fsync(file.fileno())
            if initial:
                os.replace(temporary, path)
                sync_directory(directory)
            else:
                previous = self._parquet_files(path)
                name = uuid.uuid4().hex + '.parquet'
                fragment = os.path.join(directory, name)
                os.replace(temporary, fragment)
                sync_directory(directory)
                sync_directory(os.path.dirname(path))
                files = [('@base' if item == path else os.path.basename(item)) for item in previous] if append else []
                atomic_json(path + '.manifest.json', {
                    'format': 'iosql-parquet-parts-v1', 'files': files + [name],
                })
                visible = set(files + [name])
                for candidate_name in os.listdir(directory):
                    if (candidate_name not in visible and len(candidate_name) == 40
                            and candidate_name.endswith('.parquet')
                            and all(char in '0123456789abcdef' for char in candidate_name[:-8])):
                        candidate = os.path.join(directory, candidate_name)
                        try:
                            if os.path.islink(candidate):
                                raise UnsafeOperation('清理目标是符号链接')
                            os.remove(candidate)
                        except (OSError, UnsafeOperation) as exc:
                            warnings.warn(f'Parquet 发布后的旧数据片清理未完成: {candidate}: {exc}', RuntimeWarning)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def _do_insert_part(self, part, part_column, part_data, pk, partition=None,
                        replace=False, ignore=False, commit=True, logical_part=None, **kwargs):
        if not commit:
            raise BadArgument('Parquet 不支持 commit=False')
        if 'columns' in kwargs:
            raise BadArgument('insert_all 请用 column_type 声明列类型；推荐通过 Database.table() 建表')
        unsupported = [key for key in ('default', 'not_null', 'unique', 'index') if kwargs.get(key)]
        if unsupported:
            raise BadArgument(f'Parquet 不支持低层列约束参数: {unsupported}')
        current = self.get_table_meta()
        pk = [pk] if isinstance(pk, str) else list(pk or [])
        if current and current['pk'] != pk:
            raise TableSpecConflict('Parquet 表的主键不能变更')
        previous_partition = (current or {}).get('partition')
        new_partition = partition.to_meta() if partition else None
        if current is not None and previous_partition != new_partition:
            raise TableSpecConflict('Parquet 表的分区契约不能变更，请使用 repartition_to')
        for index, row in enumerate(part_data):
            if not isinstance(row, dict):
                raise BadArgument(f'row[{index}] 必须是 dict')
            if any(row.get(column) is None for column in pk):
                raise BadArgument(f'row[{index}] 的主键不能为空')
        column_types = {column: info['type'] for column, info in
                        ((current or {}).get('column_info') or {}).items()}
        declared = kwargs.get('column_type') or {}
        for column, type_ in declared.items():
            if column in column_types and _sql_type(type_) != _sql_type(column_types[column]):
                raise SchemaConflict(f'列 `{column}` 的持久化类型不能变更')
        proposed = self.suggest_column_types(part_data, {**column_types, **declared})
        new_columns = set(proposed) - set(column_types)
        if current and self.table_parts() and new_columns and not kwargs.get('alter', False):
            raise SchemaConflict(f'严格 schema 禁止新增列: {sorted(new_columns)}')
        column_types.update(proposed)
        for type_ in column_types.values():
            _sql_type(type_)
        meta = build_createtable_meta(column_types, pk, None, **kwargs)
        if current:
            # 保留既有列说明及约束，不能因本批次未声明而丢失。
            for column, info in current['column_info'].items():
                meta['column_info'][column] = info
        self._sync_contract_columns(meta)
        path = self._get_part_dbfile(part)
        if (self._part_meta_value(part) in self.table_parts() or self._manifest_references_part_key(part)) and not os.path.isfile(path):
            raise UnsafeOperation(f'manifest 引用的 Parquet 数据文件缺失: {path}')
        fields = ', '.join(self._quote_identifier(column) for column in column_types)
        placeholders = ', '.join('?' for _ in column_types)
        action = ' OR REPLACE' if replace else ' OR IGNORE' if ignore else ''
        try:
            with self._connection() as conn:
                conn.execute('BEGIN TRANSACTION')
                self._mutable_table(conn, path if replace else None, column_types, pk)
                conn.executemany(
                    f'INSERT{action} INTO data ({fields}) VALUES ({placeholders})',
                    [[row.get(column) for column in column_types] for row in part_data],
                )
                if not replace and os.path.exists(path) and pk:
                    conn.read_parquet(self._parquet_files(path), union_by_name=True).create_view('existing_data')
                    predicate = ' AND '.join(
                        f'data.{self._quote_identifier(column)} = existing_data.{self._quote_identifier(column)}'
                        for column in pk
                    )
                    if ignore:
                        conn.execute(f'DELETE FROM data USING existing_data WHERE {predicate}')
                    elif conn.execute(f'SELECT 1 FROM data JOIN existing_data ON {predicate} LIMIT 1').fetchone():
                        raise BadArgument('Parquet 主键冲突；append 不允许覆盖已有行')
                conn.execute('COMMIT')
                self._publish(conn, path, append=not replace)
        except _duckdb().ConstraintException as exc:
            raise BadArgument(f'Parquet 主键冲突或约束失败: {exc}') from exc
        except _duckdb().ConversionException as exc:
            raise SchemaConflict(f'Parquet 列类型不兼容: {exc}') from exc
        except _duckdb().Error as exc:
            raise UnsafeOperation(f'Parquet 文件写入失败 {path}: {exc}') from exc
        self._update_meta(part if logical_part is None else logical_part,
                          part_column, partition, meta)

    def do_insert_all(self, data, pk, partition=None, replace=False,
                      ignore=False, commit=True, **kwargs):
        runtime_partition = partition or self._partition
        if runtime_partition is not None:
            column = runtime_partition.TS_COL
            columns = kwargs.get('column_type') or (self.spec().columns if self.spec() else {})
            storage_type = _canon_sqlite_type(columns.get(column))
            for row in data:
                value = row.get(column)
                if not isinstance(value, numbers.Real):
                    continue
                if storage_type in ('integer', 'float'):
                    try:
                        finite = math.isfinite(value)
                    except OverflowError:
                        finite = False
                    if not finite:
                        raise BadArgument(f'分区列 `{column}` 必须是有限且未溢出的数值')
                if storage_type == 'integer' and value != int(value):
                    raise BadArgument(f'整数分区列 `{column}` 不能写入非整数，避免类型转换后分区错位')
                if storage_type == 'float' and value != float(value):
                    raise BadArgument(f'分区列 `{column}` 转为浮点会丢失精度，请使用 integer 类型')
        return super().do_insert_all(
            data, pk, partition=partition, replace=replace, ignore=ignore,
            commit=commit, **kwargs,
        )

    def _order_by_sql(self, order_by, fetch_fields, column_types):
        # 与全局 Python 归并一致：ASC 的 NULL 在前，DESC 的 NULL 在后。
        super()._order_by_sql(order_by, fetch_fields, column_types)
        return ', '.join(
            f'{self._quote_identifier(column)} ' + ('DESC NULLS LAST' if reverse else 'ASC NULLS FIRST')
            for column, reverse in self._parse_order_by(order_by)
        )

    def _normalize_pk_keys(self, rows, pk, table_spec):
        with self._connection() as conn:
            fields = [f'k{i}' for i in range(len(pk))]
            definitions = [f'{field} {_sql_type(table_spec.columns[column])}' for field, column in zip(fields, pk)]
            conn.execute('CREATE TABLE keys (' + ','.join(definitions) + ', ordinal BIGINT)')
            conn.executemany('INSERT INTO keys VALUES (' + ','.join('?' for _ in range(len(pk) + 1)) + ')',
                             [tuple(row[column] for column in pk) + (index,) for index, row in enumerate(rows)])
            return [tuple(item[:-1]) for item in conn.execute('SELECT * FROM keys ORDER BY ordinal').fetchall()]

    def _scan_rows(self, path, query, batch_size, deadline=None, columns=None):
        with self._read_lease(path, deadline), self._connection(deadline, reserved=True) as conn:
            self._create_view(conn, path, columns)
            try:
                cursor = conn.execute(query.sql, query.params)
                names = [d[0] for d in cursor.description]
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('Parquet 查询超过 timeout_ms')
                    values = cursor.fetchmany(batch_size)
                    if not values:
                        return
                    yield [_Row(names, row) for row in values]
            except _duckdb().InterruptException:
                raise
            except _duckdb().BinderException as exc:
                raise BadArgument(f'Parquet 查询字段或类型不兼容: {exc}') from exc
            except _duckdb().Error as exc:
                raise UnsafeOperation(f'Parquet 查询失败 {path}: {exc}') from exc

    def _iter_part_query_batches(self, part_dbfile, query, batch_size, deadline=None):
        yield from super()._iter_part_query_batches(part_dbfile, query, batch_size, deadline)

    def _part_ordered_rows(self, part_dbfile, query, chunk_size, deadline=None):
        columns = (self.get_table_meta() or {}).get('column_info') or {}
        def rows():
            for batch in self._scan_rows(part_dbfile, query, chunk_size, deadline, columns):
                yield from batch
        return rows()

    def iter_batches(self, batch_size=10_000):
        yield from self.iter_select_batches(batch_size=batch_size)

    def _data_file_column_bounds(self, part_key, column):
        path = self._get_part_dbfile(part_key)
        field = self._quote_identifier(column)
        with self._part_db(path, auto_commit=False) as db:
            row = db['data'].query(f'SELECT MIN({field}) AS low, MAX({field}) AS high FROM data', to_dict=True)[0]
            return {'min': row['low'], 'max': row['high']}

    @staticmethod
    def _serializable_stats(stats):
        # manifest 的 stats_json 不保存二进制或非有限浮点值，禁用相应列裁剪。
        return {
            column: ({'min': None, 'max': None, 'complete': False} if any(
                isinstance(value, (bytes, bytearray, memoryview))
                or (isinstance(value, float) and not math.isfinite(value))
                for value in (bounds.get('min'), bounds.get('max'))
            ) else bounds)
            for column, bounds in stats.items()
        }

    def _batch_column_stats(self, rows, table_spec):
        return self._serializable_stats(super()._batch_column_stats(rows, table_spec))

    def _data_file_stat_key(self, path):
        manifest = path + '.manifest.json'
        try:
            info = os.stat(manifest)
        except FileNotFoundError:
            return super()._data_file_stat_key(path)
        return (path, info.st_mtime_ns, info.st_size)

    def _data_file_column_stats(self, part_key):
        return self._serializable_stats(super()._data_file_column_stats(part_key))

    def _write_segment_rows(self, segment, logical_part, part_column, rows, pk,
                            partition, replace, ignore, commit, layout, part_kwargs):
        self._do_insert_part(
            segment.part_key, part_column, rows, pk, partition=partition,
            replace=replace, ignore=ignore, commit=commit,
            logical_part=logical_part, **part_kwargs,
        )
        if replace or ignore:
            return self._refresh_segment_info(logical_part, segment.segment_id, segment.part_key, layout)
        # 新增行计数精确，无法证明与持久类型一致的列统计会保守禁用。
        return self._increment_segment_info(segment, rows, layout, TableContract.ensure(part_kwargs['table_contract']))

    def _segment_cleanup_files(self, ref):
        files = [path for path in (ref.path, ref.path + '.manifest.json') if os.path.isfile(path)]
        directory = self._fragment_directory(ref.path)
        if os.path.islink(directory):
            raise UnsafeOperation(f'Parquet 数据片目录不能是符号链接: {directory}')
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if not os.path.isfile(path) or os.path.islink(path):
                    raise UnsafeOperation(f'Parquet 数据片不是普通文件: {path}')
                files.append(path)
        return files

    def _remove_orphan_part_file(self, path):
        if not self._is_safe_part_path(path) or not self._close_part_db_if_idle(path):
            raise UnsafeOperation(f'不能清理被占用或越界的 Parquet 文件: {path}')
        for candidate in self._segment_cleanup_files(SegmentInfo(path=path, logical_part=None, segment_id=-1, part_key=None)):
            os.remove(candidate)

    def _legacy_segment_info(self, logical_part, lightweight=False):
        path = self._get_part_dbfile(logical_part)
        try:
            info = super()._legacy_segment_info(logical_part, lightweight=lightweight)
            if info is not None:
                return info
        except (UnsafeOperation, _duckdb().Error):
            # 损坏文件仍保留为候选引用，查询抛错，validate 可以报告损坏。
            pass
        segmented = self._segment_infos([logical_part], include_obsolete=True)
        if not segmented:
            return SegmentInfo(
                logical_part=logical_part, segment_id=-1, part_key=logical_part,
                path=path, sealed=True, state='sealed',
            )
        return None

    def _validate_segment_ref_deep(self, ref, result):
        try:
            with self._part_db(ref.path, auto_commit=False) as db:
                # 读取所有列与行以检查压缩页，COUNT(*) 可能只消费 footer。
                cursor = db.conn.execute('SELECT * FROM data')
                while cursor.fetchmany(10_000):
                    pass
            super()._validate_segment_row_count(ref, result)
            self._validate_segment_time_bounds(ref, result)
            self._validate_segment_session_bounds(ref, result)
            self._validate_segment_stats(ref, result)
        except (UnsafeOperation, _duckdb().Error, OSError) as exc:
            self._add_segment_issue(result, ref, 'parquet_integrity_failed', str(exc))

    def _delete_and_cases(self, cases, column_types):
        total = 0
        with self._mutation_lock():
            refs = self._get_select_part_refs(cases)
            spec = self.spec()
            layout = spec.physical_layout if spec else PhysicalLayout()
            for ref in refs:
                self._ensure_ref_file_present(ref)
                path = self._get_part_dbfile(ref.part_key)
                count_query = build_select_count_query('data', cases, column_types)
                delete_query = build_delete_query('data', cases, column_types)
                try:
                    with self._connection() as conn:
                        self._create_view(conn, path, {column: {'type': type_} for column, type_ in column_types.items()})
                        count = conn.execute(count_query.sql, count_query.params).fetchone()[0]
                        if not count:
                            continue
                        conn.execute('DROP VIEW data')
                        self._mutable_table(conn, path, column_types, spec.primary_key if spec else [])
                        conn.execute(delete_query.sql, delete_query.params)
                        self._publish(conn, path)
                except _duckdb().Error as exc:
                    raise UnsafeOperation(f'Parquet 删除失败 {path}: {exc}') from exc
                total += count
                if ref.segment_id >= 0:
                    self._refresh_segment_info(ref.logical_part, ref.segment_id, ref.part_key, layout)
        return total

    def pragma(self, key, value=None):
        raise BadArgument('Parquet 不支持 SQLite PRAGMA')

    def create_index(self, column, order='asc', exist_ok=True):
        raise BadArgument('Parquet 不支持持久化 SQL 索引；请使用分区和 segment 裁剪')

    def drop_index(self, column):
        raise BadArgument('Parquet 不支持持久化 SQL 索引')

    def aggregate_by_bucket(self, x_col, value_cols, n_buckets, where=None, group_col=None):
        raise BadArgument('Parquet 暂不支持 aggregate_by_bucket')

    def create_table(self, column_type, pk, partition=None, **kwargs):
        return self.use_contract(TableContract(
            primary_key=[pk] if isinstance(pk, str) else list(pk),
            columns=column_type, partition=partition,
        ))

    def do_create_table(self, part_dbfile, part_table_name, createtable_meta, alter):
        raise BadArgument('Parquet 请通过 Database.table() 声明表结构')

    def _check_target(self, target_uri):
        from ...database import Database
        Database._validate_migration_target(target_uri)
        Database._reject_migration_self_target(self, target_uri)

    def migrate_to(self, target_uri, tables=OMITTED, mode='append',
                   batch_size=10_000, verify=None, dry_run=False, *,
                   specs=None, temp_dir=None, lock_timeout=30.0, target_table=OMITTED):
        """保留源表并复制到目标；tables 与 Database 相同，specs 支持目标重新分区。"""
        from ...migration import migrate_table
        return migrate_table(self, target_uri, tables, mode, batch_size, verify, dry_run,
                             specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout,
                             target_table=target_table)

    def repartition_to(self, target_uri, table_name=None, spec=None,
                       mode='append', batch_size=10_000):
        from ...migration import repartition
        return repartition(self, target_uri, table_name=table_name, spec=spec,
                           mode=mode, batch_size=batch_size)
