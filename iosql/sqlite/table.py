import json
import math
import os
import time

# from itertools import chain
from collections import Counter, OrderedDict

import arrow

from ..exception import InvalidUriSchema
from ..logger import default_logger as logger
from ..partition import CalendarPartition, DurationPartition, Partition
from ..query import Where, get_case_range
from ..table import BaseTable
from ..utils.column import suggest_sqlite_column_types
from ..utils.createtable import build_createtable_meta
from ..utils.mysqlite import Database as MyDatabase

# from mysqlite import Database as MyDatabase
from ..utils.mysqlite.exception import DuplicateColumn, NoSuchColumn, NoSuchTable
from .const import (
    _DB_META_FILE,
    _DB_META_TABLE_NAME,
    _DEFAULT_BATCH_SIZE,
    _DEFAULT_N_SUGGEST_CHECK,
    _PART_DATA_TABLE_NAME,
    _TABLE_META_FILE,
    _TABLE_META_TABLE_NAME,
    _TABLE_PART_COLUMN_RANGE_TABLE_NAME,
    _TABLE_PART_TABLE_NAME,
    _TABLE_PARTS_SUBDIR,
)
from .query_util import build_delete_sql, build_select_cases_sql


class Table(BaseTable):
    """
    定位：有状态的Table
    分区模式，因为不知道在哪个分区，插入时才创建.sqlite3文件
    """

    def __init__(self, *args, **kwargs):
        self._pragma_dict = {}  # for all database
        self._meta_db = None
        self._table_meta_db = None
        self._db_parts = OrderedDict()
        self._db_parts_status = OrderedDict()
        # 一般情况下，这个part写完，一般就意味这写完了，可以关闭了
        self._num_opened_max_allowed = 5
        # New partition system
        self._partition = None
        super().__init__(*args, **kwargs)
        # Load partition from metadata after initialization
        self._partition = self._load_partition()
        self.logger = logger

    def connect(self):
        """连接
        connect called by super().__init__
        """
        path = self.uri_options.get("path")
        db = self.uri_options.get("db")
        # path 可以为''
        if path is None or not db:
            raise InvalidUriSchema()

        self.db_path = os.path.join(path, db)
        self.db_meta_path = os.path.join(self.db_path, _DB_META_FILE)

        self.table_path = os.path.join(self.db_path, self._table_name)
        self.table_meta_path = os.path.join(self.table_path, _TABLE_META_FILE)
        self.table_part_path = os.path.join(self.table_path, _TABLE_PARTS_SUBDIR)
        logger.debug("Database Connected")

        self._table_meta_db = self.get_table_meta_db()
        self._meta_db = self.get_meta_db()

    def close(self):
        if self._table_meta_db and not self._table_meta_db.is_closed():
            self._table_meta_db.close()
        if self._meta_db and not self._meta_db.is_closed():
            self._meta_db.close()
        for _, db in self._db_parts.items():
            if db and not db.is_closed():
                db.close()

    def _load_partition(self) -> Partition | None:
        """Load partition from table metadata.

        Returns:
            Partition object if partition info exists, None otherwise
        """
        meta = self.get_table_meta()
        if meta and "partition" in meta and meta["partition"]:
            partition_meta = meta["partition"]
            if isinstance(partition_meta, dict) and "type" in partition_meta:
                return Partition.from_meta(partition_meta)
        return None

    @property
    def partition(self) -> Partition | None:
        """Get the partition object for this table."""
        return self._partition

    def _get_part_dbfile(self, part_key):
        """Get the file path for a partition key.

        Args:
            part_key: Partition key (int for DurationPartition, str for CalendarPartition)

        Returns:
            Full path to the partition file
        """
        return os.path.join(self.table_part_path, f"{part_key}.sqlite3")

    def select_all(self, fields, where, limit=None, to_dict=False, **kwargs):
        return self.select(fields, where, limit=limit, to_dict=True, **kwargs)

    def select(self, fields, where, limit=None, to_dict=False, **kwargs):
        """查询完所有 And([case..]) 然后组合
            And([case..])为一个基本查询单位[向数据库请求]
            where = {'field': 'date',..}
        where = {
            'field': 'date'
            'op': 'between_and',
            'value': '2020'
            'rank': 1
        }
        """
        # step1. 找到各part记录到的字段最大最小数值, 排除不需要查询的part
        # step2. 查找各个And([case..])的数据
        #       处理where以后, [And([case..])]之间不应有包含关系，否则可以直接去掉被包含的
        # step3. 数据分组cluster，组内排序，组间排序
        #        cluster=None, 返回: [dict..]
        #        cluster='symbol', 返回: OrderedDict[('symbol1', [dict..]), ..]
        # 一次性全部读出内存? 多个表，涉及到排序问题
        meta = self.get_table_meta()
        if meta:
            column_types = {
                key: item["type"] for key, item in meta["column_info"].items()
            }
        else:
            # No such table
            column_types = None
            return None

        w = Where(where)

        full_data = []
        if not w.or_and_cases:
            # select all
            data = self._select_and_cases(fields, None, column_types)
            # full_data.append(data)
            if data:
                full_data.extend(data)
        else:
            for cases in w.or_and_cases:
                # 一个基本查询单元
                data = self._select_and_cases(fields, cases, column_types)
                # full_data.append((cases, data))
                # full_data.append(data)
                if data:
                    full_data.extend(data)
        # full_data = list(chain(*full_data))
        if to_dict:
            full_data = [dict(item) for item in full_data]
        return full_data

    def _excluded_parts_by_case(self, infos, case):
        op, case_min, case_max = get_case_range(case)
        excluded_parts = set()
        if case_min is not None and isinstance(case_min, str):
            return excluded_parts
        if case_max is not None and isinstance(case_max, str):
            return excluded_parts
        for info in infos:
            part = info["part"]
            if info["column"] == case.field:
                if case_max and case_max < info["column_min"]:
                    excluded_parts.add(part)
                if case_min and case_min > info["column_max"]:
                    excluded_parts.add(part)
        return excluded_parts

    def _excluded_parts_by_cases(self, infos, cases):
        excluded_parts = set()
        for case in cases:
            excluded = self._excluded_parts_by_case(infos, case)
            excluded_parts = excluded_parts.union(excluded)
        return excluded_parts

    def _get_select_parts(self, cases):
        """case And[Case..]"""
        infos = self.get_part_column_ranges()
        if cases:
            excluded_parts = self._excluded_parts_by_cases(infos, cases)
        else:
            excluded_parts = set()
        all_parts = self.table_parts()
        need_parts = sorted(set(all_parts) - set(excluded_parts))
        return need_parts

    def _select_and_cases(self, fields, cases, column_types):
        """获得And[Case..] 的查询结果
        And[Case..] 为一个基本查询单元
        """
        # table field必须的，涉及到查询是否需要加 "
        # 分成独立文件是为了同步数据方便，也导致数据容易被盗
        need_parts = self._get_select_parts(cases)
        data = []
        for part in need_parts:
            part_table_name = _PART_DATA_TABLE_NAME
            sql = build_select_cases_sql(part_table_name, fields, cases, column_types)
            part_dbfile = self._get_part_dbfile(part)

            logger.debug(f"{part_dbfile}: {part}: SQL: {sql}")
            part_table_name = _PART_DATA_TABLE_NAME
            db = self.get_part_db(part_dbfile, auto_commit=False)
            # part_data = db[part_table_name].query(sql, to_dict=True)

            part_data = db[part_table_name].query(sql)
            data.extend(part_data)

        return data

    def create_index(self, column, order="asc", exist_ok=True):
        table_parts = self.table_parts()
        for table_part in table_parts:
            part_dbfile = self._get_part_dbfile(table_part)
            part_db = self.get_part_db(part_dbfile, auto_commit=True)
            part_db[_PART_DATA_TABLE_NAME].create_index(
                column, order, exist_ok=exist_ok
            )

    def upsert_all(
        self, kv_list, pk, where=None, partition=None, commit=True, **kwargs
    ):
        """Update or insert rows based on primary key.

        Logic:
        1. SELECT existing rows matching WHERE clause
        2. Build a dict keyed by pk for fast lookup
        3. For each item in kv_list:
           - If pk exists in existing data: MERGE (update fields with new values)
           - If pk is NEW: INSERT the new row
        4. INSERT all rows with REPLACE (upsert behavior)

        Args:
            kv_list: List of dicts with values to update/insert
            pk: Primary key columns
            where: Optional WHERE clause to filter existing rows
            partition: Partition object (auto-loaded if not provided)
            commit: Whether to commit transaction
        """
        if isinstance(kv_list, dict):
            kv_list = [kv_list]
        elif not isinstance(kv_list, (list, tuple, set)):
            raise Exception(f"upsert_all: `list/tuple/set/dict` required for `kv_list`")

        if not kv_list:
            return

        # Step 1: SELECT existing rows matching WHERE
        data = self.select("*", where=where, to_dict=True)

        # Step 2: Build dict keyed by pk for O(1) lookup
        # Format: {(pk1, pk2, ...): {full_row_dict}}
        data = {tuple([item[k] for k in pk]): item for item in data}

        # Step 3: Merge kv_list into existing data
        # - If pk exists: update only the fields present in kv_list (partial update)
        # - If pk is new: insert the complete new row
        for dic in kv_list:
            key = tuple([dic[k] for k in pk])
            if key in data:
                # Existing row: merge fields (new values override old)
                # Using {**existing, **new} ensures new values take precedence
                data[key] = {**data[key], **dic}
            else:
                # New row: insert the complete row
                data[key] = dic

        # Step 4: INSERT with REPLACE (SQLite upsert behavior)
        # - Existing rows: UPDATE based on pk
        # - New rows: INSERT
        self.insert_all(
            list(data.values()),
            pk=pk,
            replace=True,
            alter=True,
            partition=partition,
            commit=commit,
            **kwargs,
        )
        # def insert_all(self, data, pk=None, extra_data=None, partition=None,
        #                replace=False, ignore=False, commit=True, **kwargs):
        # raise NotImplementedError()

    def delete(self, where):
        """Delete rows matching the where clause from all relevant partitions.

        Args:
            where: Dict format where clause, e.g. {'field': 'id', 'op': '>', 'value': 10}
        """
        meta = self.get_table_meta()
        if not meta:
            return 0

        column_types = {key: item["type"] for key, item in meta["column_info"].items()}

        w = Where(where)

        total_deleted = 0
        for cases in w.or_and_cases:
            deleted = self._delete_and_cases(cases, column_types)
            total_deleted += deleted

        return total_deleted

    def _delete_and_cases(self, cases, column_types):
        """Delete rows matching And[Case..] from all relevant partitions."""
        need_parts = self._get_select_parts(cases)
        total_deleted = 0

        for part in need_parts:
            part_dbfile = self._get_part_dbfile(part)
            db = self.get_part_db(part_dbfile, auto_commit=True)
            part_table = db[_PART_DATA_TABLE_NAME]

            select_sql = build_select_cases_sql(
                _PART_DATA_TABLE_NAME, "COUNT(*)", cases, column_types
            )
            count_before = part_table.query(select_sql)
            count = dict(count_before[0])["COUNT(*)"] if count_before else 0

            if count > 0:
                delete_sql = build_delete_sql(
                    _PART_DATA_TABLE_NAME, cases, column_types
                )
                logger.debug(f"{part_dbfile}: DELETE: {delete_sql}")
                db.execute(delete_sql)
                total_deleted += count

        return total_deleted

    def do_insert_all(
        self,
        data,
        pk,
        partition=None,
        replace=False,
        ignore=False,
        commit=True,
        **kwargs,
    ):
        # Use new partition system
        _t0 = time.time()

        # Determine which partition to use:
        # 1. If partition is explicitly passed, use it
        # 2. If _partition is already loaded from metadata, use it
        # 3. If neither, no partitioning (single table)
        part_obj = partition
        if part_obj is None:
            part_obj = self._partition

        if part_obj is None:
            # No partitioning - put all data in a single partition
            partitioned_data = {None: data}
        else:
            # Partition data by key
            partitioned_data = {}
            for row in data:
                key = part_obj.get_key(row)
                if key not in partitioned_data:
                    partitioned_data[key] = []
                partitioned_data[key].append(row)

        _t1 = time.time()
        logger.debug(f"Partition Time: {_t1 - _t0}")

        # Get ts_col for range calculation (needed for metadata)
        ts_col = part_obj.TS_COL if part_obj else None

        for part_key, part_data in partitioned_data.items():
            self._do_insert_part(
                part_key,
                ts_col,
                part_data,
                pk,
                partition=part_obj,
                replace=replace,
                ignore=ignore,
                commit=commit,
                **kwargs,
            )
        _t2 = time.time()
        logger.debug(f"Insert Time: {_t2 - _t1}")

    def _do_insert_part(
        self,
        part,
        part_column,
        part_data,
        pk,
        partition=None,
        replace=False,
        ignore=False,
        commit=True,
        **kwargs,
    ):
        # 一个table 一个文件夹
        # 因为database可能要查询table的状态，所以table的meta
        # 信息也放在 /database/meta.sqlite3中
        column_type = kwargs.get("column_type", None)  # 用户指定的类型
        if "columns" in kwargs:
            self.logger.error("`columns` deprecated, please use `column_type` instead")
            raise Exception("Deprecated")

        alter = kwargs.get("alter")
        batch_size = kwargs.get("batch_size", _DEFAULT_BATCH_SIZE)
        n_suggest_check = kwargs.get("n_suggest_check", _DEFAULT_N_SUGGEST_CHECK)
        # 当前记录的大表结构
        current_meta = self.get_table_meta()
        if current_meta and set(current_meta["pk"] or []) != set(pk or []):
            raise Exception(
                (
                    f"PrimaryKeyCanNotChange: supplied pk={pk}, "
                    f"table {current_meta['pk']}"
                )
            )
        # Compare partition meta dicts - only check if table already exists
        prev_partition_meta = current_meta.get("partition") if current_meta else None
        curr_partition_meta = partition.to_meta() if partition else None
        # Only raise if previous partition existed and is different from current
        if (
            prev_partition_meta is not None
            and prev_partition_meta != curr_partition_meta
        ):
            raise Exception(
                f"PartitionCanNotChange: prev={prev_partition_meta} curr={curr_partition_meta}"
            )

        part_dbfile = self._get_part_dbfile(part)
        part_db = self.get_part_db(part_dbfile, auto_commit=True)
        # part_db = MyDatabase(part_dbfile, auto_commit=True, pragma_dict=self._pragma_dict)
        try:
            part_table_name = _PART_DATA_TABLE_NAME
            part_db[part_table_name].insert_all(
                part_data,
                replace=replace,
                ignore=ignore,
                batch_size=batch_size,
                commit=commit,
                # ensure_table=True # [@2024-03-30 21:34:30] FIXED
                ensure_table=False,
            )
        except NoSuchTable:
            # part的表不存在
            #   第一次创建，主表可能也不存在
            #   主表存在，只是该part的表不存在
            # 所以此种情况下，写入主表数据，且part表
            # alter = True 时，还需要检查column是否一致
            data_suggested_types = self.suggest_column_types(
                part_data, column_type=column_type, n_suggest_check=n_suggest_check
            )
            createtable_meta = build_createtable_meta(
                data_suggested_types, pk=pk, partition=partition, **kwargs
            )
            # 确保表格建好后，才写meta，确保不删列(不支持)情况下，meta中有的信息, 表中都有
            self.do_create_table(part_dbfile, part_table_name, createtable_meta, alter)

            # update meta
            self._update_meta(part, part_column, partition, createtable_meta)

            # write data
            # db = MyDatabase(part_dbfile, auto_commit=True, pragma_dict=self._pragma_dict)
            part_db[part_table_name].insert_all(
                part_data,
                replace=replace,
                ignore=ignore,
                batch_size=batch_size,
                commit=commit,
                ensure_table=False,  # [@2024-03-30 21:34:46]
                # ensure_table=False
            )
        except NoSuchColumn as e:
            # add column for all partition-table
            # alter table
            # 如果该part_data
            if not alter:
                raise e

            data_suggested_types = self.suggest_column_types(
                part_data, column_type=column_type, n_suggest_check=n_suggest_check
            )
            self._add_columns_for_all_partitions(data_suggested_types)

            createtable_meta = build_createtable_meta(
                data_suggested_types, pk=pk, partition=partition, **kwargs
            )

            # 数据库中没有的列增加，但已经有的，且insert数据没有的字段，并不会删除
            # 所以要更新 createtable_meta
            if current_meta:
                new_column_info = current_meta["column_info"]
                for col, item in createtable_meta["column_info"].items():
                    if col not in new_column_info:
                        new_column_info[col] = item
                createtable_meta["column_info"] = new_column_info

            # update meta
            self._update_meta(part, part_column, partition, createtable_meta)

            # write data
            # db = MyDatabase(part_dbfile, auto_commit=True, pragma_dict=self._pragma_dict)
            part_db[part_table_name].insert_all(
                part_data,
                replace=replace,
                ignore=ignore,
                batch_size=batch_size,
                commit=commit,
                ensure_table=False,
            )

    def _update_meta(self, part_key, ts_col, partition, createtable_meta):
        # 更新table创建的meta信息，直接覆盖
        # Store partition info in createtable_meta if it exists
        if partition is not None:
            createtable_meta["partition"] = partition.to_meta()
        self._overwrite_table_meta(createtable_meta)

        # 增加table_part信息, 设置更新时间，方便今后缓存访问
        _t0 = time.time()
        updated_at = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        self.upsert_part_info(part_key, updated_at)
        _t1 = time.time()
        logger.debug(f"upsert_part_info Time: {_t1 - _t0}")

        # Update partition column ranges using new partition system
        column_min, column_max = None, None
        if partition is not None:
            start_ms, end_ms = partition.get_range(part_key)
            column_min = start_ms
            column_max = end_ms
        self.upsert_part_column_range(part_key, ts_col, column_min, column_max)
        _t2 = time.time()
        logger.debug(f"upsert_part_column_range Time: {_t2 - _t1}")

    def get_table_meta(self):
        try:
            # meta = self.meta_db[_DB_META_TABLE_NAME].select_one(
            meta = self.table_meta_db[_TABLE_META_TABLE_NAME].select_one(
                "meta", where=f'table_name="{self._table_name}"'
            )
            if meta:
                meta = json.loads(meta["meta"])
                meta["column_info"] = OrderedDict(meta["column_info"])
            return meta
        except NoSuchTable:
            return None

    def _table_columns(self):
        pass

    def table_names(self):
        return os.listdir(self.db_path)
        try:
            items = self.table_meta_db[_TABLE_META_TABLE_NAME].select(
                "table_name", where=None, to_dict=True
            )
        except NoSuchTable:
            return []
        return sorted(list(set([item["table_name"] for item in items])))

    def _overwrite_table_meta(self, createtable_meta):
        """table 创建信息"""
        # 某个表格的各分区最大最小数值
        # table_meta_db = MyDatabase(self.table_meta_path, pragma_dict=self._pragma_dict)
        # table_meta_db.enable_wal()

        # OrderDict
        createtable_meta["column_info"] = list(createtable_meta["column_info"].items())
        meta = json.dumps(createtable_meta).replace('"', '""')

        updated_at = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        info = {
            "table_name": self._table_name,
            "meta": meta,
            "updated_at": updated_at,
        }
        self.table_meta_db[_TABLE_META_TABLE_NAME].insert(
            info, pk=["table_name"], replace=True, ensure_table=True
        )
        # decoupled
        # self.meta_db[_DB_META_TABLE_NAME].insert(
        #    info, pk=['table_name'], replace=True,
        #    ensure_table=True
        # )

    def upsert_part_info(self, part, updated_at):
        """每个表有哪些分区

        更新每个partition的时间，方便后续缓存访问
        """
        # 某个表格的各分区最大最小数值
        info = {
            "table_name": self._table_name,
            "part": part,
            "updated_at": updated_at,
        }
        # table_meta_db = MyDatabase(self.table_meta_path)
        # table_meta_db.enable_wal()
        self.table_meta_db[_TABLE_PART_TABLE_NAME].insert(
            info,
            pk=["table_name", "part"],
            replace=True,
            ensure_table=True,
            commit=True,
        )

    def table_parts(self):
        # table_meta_db = MyDatabase(self.table_meta_path)
        parts = set()
        parts = self.table_meta_db[_TABLE_PART_TABLE_NAME].select(
            "*", where=f'table_name="{self._table_name}"', to_dict=True
        )
        return sorted([item["part"] for item in parts])

    def upsert_part_column_range(self, part, column, column_min, column_max):
        """某个表格的各分区: 各个字段允许的的最大/最小数值"""
        # table_meta_db = MyDatabase(self.table_meta_path)
        # table_meta_db.enable_wal()
        # 增加part的编号: 不必要
        # 未来可以更新，实际的最大数值/最小数值，方便快速查找
        # column_max, column_min: None 表示没有最大/小
        info = {
            "table_name": self._table_name,
            "part": part,
            "column": column,
            "column_min": column_min,
            "column_max": column_max,
        }
        _tblname = _TABLE_PART_COLUMN_RANGE_TABLE_NAME
        self.table_meta_db[_tblname].insert(
            info, pk=["table_name", "part", "column"], ensure_table=True, replace=True
        )

    def get_part_column_ranges(self, table_name=None):
        # table_meta_db = MyDatabase(self.table_meta_path)
        _tblname = _TABLE_PART_COLUMN_RANGE_TABLE_NAME
        if table_name is None:
            table_name = self._table_name
        infos = self.table_meta_db[_tblname].select(
            "*", where=f'table_name="{table_name}"', to_dict=True
        )
        return infos

    def _add_columns_for_all_partitions(self, data_suggested_types):
        """
        params:
            n_suggest_check 使用多少条数据检查类型
        """
        # createtable_meta = build_createtable_meta(data_suggested_types,
        #                                          pk=pk,
        #                                          partition=partition, **kwargs)

        # 每个part都手动增加字段
        parts = self.table_parts()
        for part in parts:
            logger.info(f"Altering Table Schema: {part} ..")
            part_dbfile = self._get_part_dbfile(part)
            part_db = self.get_part_db(part_dbfile, auto_commit=True)
            for column, type_ in data_suggested_types.items():
                try:
                    part_db[_PART_DATA_TABLE_NAME].add_column(column, type_)
                    logger.info(f"Added {column} {type_}")
                except DuplicateColumn:
                    pass

    def suggest_column_types(self, data, column_type, n_suggest_check=None):
        return suggest_sqlite_column_types(
            data, column_type=column_type, n_check=n_suggest_check
        )

    def do_create_table(self, part_dbfile, part_table_name, createtable_meta, alter):
        """
        ensure创建主表meta信息，创建part, 写入part数据
        {'column_info': OrderedDict([('id',
                                {'comment': None, 'default': None,
                               'dtype': None,
                               'index_created': False,
                               'is_pk': True,
                               'name': 'id',
                               'not_null': False,
                               'should_index': False,
                               'type': 'integer'}),
                               )
        }

        mysqlite
        [{
            'name': 'column1',
            'type': 'text'
            'default': default,
            'unique': False
            'not_null': not_null,
            'dtype': 'datetime'
            'comment': '姓名'
        }]
        """
        mysqlite_columns = []
        for column, item in createtable_meta["column_info"].items():
            dic = {
                "name": column,
                "type": item["type"],
                "default": item.get("default"),
                "unique": item.get("unique"),
                "not_null": item.get("not_null"),
                "dtype": item.get("dtype"),
                "comment": item.get("comment"),
            }
            mysqlite_columns.append(dic)

        db = MyDatabase(part_dbfile, verbose=self.verbose)
        try:
            # sqlite3.OperationalError: unable to open database file
            db[part_table_name].create_table(
                mysqlite_columns,
                pk=createtable_meta["pk"],
                save_meta=True,
                table_comment=None,
            )
        except OSError as e:
            if str(e).find("Too many open files") != -1:
                raise e
            raise e

    @property
    def table_meta_db(self):
        return self.get_table_meta_db(auto_commit=True)

    def get_table_meta_db(self, auto_commit=True):
        # self._table_meta_db = 3
        if not self._table_meta_db or self._table_meta_db.is_closed():
            self._table_meta_db = MyDatabase(
                self.table_meta_path,
                auto_commit=auto_commit,
                verbose=self.verbose,
                pragma_dict=self._pragma_dict,
            )
        else:
            pass
        return self._table_meta_db

    @property
    def meta_db(self):
        return self.get_meta_db(auto_commit=True)

    def get_meta_db(self, auto_commit=True):
        if not self._meta_db or self._meta_db.is_closed():
            self._meta_db = MyDatabase(
                self.db_meta_path,
                auto_commit=auto_commit,
                verbose=self.verbose,
                pragma_dict=self._pragma_dict,
            )
        return self._meta_db

    def get_part_db(self, part_dbfile, auto_commit=True):
        # 只有查询时，auto_commit 才为False
        c = Counter(self._db_parts_status.values())
        # print(self._db_parts_status)

        if c.get(True, 0) >= self._num_opened_max_allowed:
            # logger.debug('TOO MANY open Handles, safe closing..')
            n_close = self._num_opened_max_allowed // 2
            for key in list(self._db_parts.keys())[:n_close]:
                logger.debug(f"Clean up, closing {key} ..")
                # print('close ..............')
                self._db_parts[key].force_close_all_connections()
                del self._db_parts[key]
                del self._db_parts_status[key]

        if part_dbfile not in self._db_parts or self._db_parts[part_dbfile].is_closed():
            db = MyDatabase(
                part_dbfile, pragma_dict=self._pragma_dict, auto_commit=auto_commit
            )
            self._db_parts[part_dbfile] = db
            self._db_parts_status[part_dbfile] = True
        else:
            self._db_parts[part_dbfile].auto_commit = auto_commit

        return self._db_parts[part_dbfile]

    def pragma(self, key, value=None):
        self._pragma_dict[key] = value

    def create_table(self, column_type, pk, partition=None, **kwargs):
        pass
