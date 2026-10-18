from abc import ABCMeta, abstractmethod
from .exception import BadArgument
from .utils.schema import parse_db_schema


def resolve_select_fields(fields, columns, func_name):
    """select 家族共用：columns= 是 fields= 的别名，两者同传冲突时报错。"""
    if columns is None:
        return fields
    if fields not in ('*', None):
        raise BadArgument(
            f'{func_name}: fields 与 columns 只能传一个'
        )
    return columns


class BaseTable(metaclass=ABCMeta):

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        from .coordination import guard_table_class
        guard_table_class(cls)

    def _reload_after_migration(self):
        """更换物理目录后重建连接，旧缓存不能跨代继续使用。"""
        from .exception import UnsafeOperation

        if getattr(self, '_iosql_explicitly_closed', False):
            raise UnsafeOperation('表已经关闭，请重新通过 Database.table() 打开')
        if getattr(self, '_in_explicit_tx', False):
            raise UnsafeOperation('调用方事务仍未结束，不能重新绑定发布后的表')
        if self._uri_schema in ('sqlite', 'sqlite3', 'parquet'):
            from .coordination import physical_root
            if not (physical_root(self._uri) / self._table_name).is_dir():
                raise UnsafeOperation('表已被移除或重命名，请重新打开对应表')
        constructor = getattr(self, '_iosql_constructor', None)
        if constructor is None:
            return
        owner = getattr(self, '_owner_db', None)
        pg_contract = getattr(self, '_contract', None) if self._uri_schema == 'postgresql' else None
        local_declaration = getattr(self, '_declared_spec', None)
        args, kwargs = list(constructor[0]), dict(constructor[1])
        if self._uri_schema in ('sqlite', 'sqlite3'):
            import copy
            kwargs['pragma_dict'] = copy.deepcopy(self._pragma_dict)
        if self._uri_schema in ('sqlite', 'sqlite3', 'parquet'):
            for key, attribute in (('max_open_partitions', '_num_opened_max_allowed'),
                                   ('max_connections_per_file', '_max_connections_per_file'),
                                   ('max_process_connections', '_max_process_connections'),
                                   ('max_ordered_read_connections', '_max_ordered_read_connections')):
                if hasattr(self, attribute):
                    kwargs[key] = getattr(self, attribute)
        for index, (key, value) in enumerate((('uri', self._uri), ('table_name', self._table_name))):
            if len(args) > index:
                args[index] = value
            else:
                kwargs[key] = value
        self._iosql_invalidating = True
        try:
            self.close()
            type(self).__init__(self, *args, **kwargs)
            if pg_contract is not None:
                self._restore_contract_after_migration(pg_contract)
            elif local_declaration is not None and self.get_table_meta() is None:
                # 未物化表没有落盘契约；保留仍在新视图中的当前实例声明。
                self.use_contract(local_declaration)
            if owner is not None:
                self._owner_db = owner
        finally:
            self._iosql_invalidating = False

    def __init__(self, uri, table_name, verbose=0, **engine_options):
        self._uri = uri
        self._table_name = table_name
        self._uri_schema, self._uri_options = parse_db_schema(uri)
        self.verbose = verbose
        self._engine_options = dict(engine_options)
        self.connect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()

    def _rebind_uri(self, uri):
        self._uri = uri
        self._uri_schema, self._uri_options = parse_db_schema(uri)
        self.connect()
        return self

    @abstractmethod
    def connect(self):
        raise NotImplementedError()

    @abstractmethod
    def close(self):
        raise NotImplementedError()

    @abstractmethod
    def rename(self, new_name, exist_ok=False, overwrite=False):
        raise NotImplementedError()

    @abstractmethod
    def pragma(self, key, value=None):
        raise NotImplementedError()

    def create_table(self, column_type, pk, partition=None, **kwargs):
        '''手动创建table

        不被insert_all调用
        params:
            columns 包含创建该类型数据库所需要的type
        '''
        raise NotImplementedError(
            'BaseTable.create_table must be overridden by the engine subclass'
        )

    def describe(self):
        """返回可供程序与 LLM 消费的表结构和说明，不读取业务数据。"""
        spec = self.spec()
        if spec is None:
            return {
                "name": self.table_name,
                "description": None,
                "primary_key": [],
                "columns": [],
            }

        wire_spec = spec.to_dict()
        descriptions = spec.column_descriptions or {}
        ranges = wire_spec.get("column_ranges") or {}
        return {
            "name": self.table_name,
            "description": spec.description,
            "primary_key": list(spec.primary_key),
            "columns": [
                {
                    "name": column,
                    "type": str(type_) if type_ is not None else None,
                    "description": descriptions.get(column),
                    "range": dict(ranges[column]) if column in ranges else None,
                }
                for column, type_ in (spec.columns or {}).items()
            ],
        }

    def update_metadata(self, **kwargs):
        """更新持久化元信息；仅支持此能力的后端会覆盖此方法。"""
        raise BadArgument(
            "update_metadata is only supported by the SQLite engine"
        )

    def insert(self, dic, pk=None, extra_data=None, partition=None, 
               replace=False, ignore=False, commit=True, **kwargs):
        if not dic: 
            return
        return self.insert_all(
            [dic], pk=pk, extra_data=extra_data, partition=partition,
            replace=replace, ignore=ignore, commit=commit, **kwargs
        )

    def insert_all(self, data, pk=None, extra_data=None, partition=None, 
                   replace=False, ignore=False, commit=True, **kwargs):
        ''' 
        如果表已经存在
        click-house 是内部自动partition的
        所以应区分内部partion 和显式partition
        '''
        if not data: 
            return

        # add data
        data_with_extra = self._add_extra_data(data, extra_data)

        # insert
        rv = self.do_insert_all(
            data_with_extra, 
            pk=pk, partition=partition,
            replace=replace, ignore=ignore, 
            commit=commit, **kwargs
        )
        return rv

    def select(self, fields, where, limit=None, **kwargs):
        # 返回yield对象: 适合读取所有数据
        raise NotImplementedError()

    def select_all(self, fields, where, limit=None, **kwargs):
        '''查询完所有 And([case..]) 然后组合
            And([case..])为一个基本查询单位[向数据库请求]
        '''
        raise NotImplementedError()

    @abstractmethod
    def upsert_all(self, kv_list, pk, partition=None, commit=True, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def delete(self, where):
        raise NotImplementedError()

    @abstractmethod
    def do_create_table(self, createtable_meta, alter):
        raise NotImplementedError()

    @abstractmethod
    def create_index(self, column, order, exist_ok=True):
        raise NotImplementedError()

    @abstractmethod
    def drop_index(self, column):
        raise NotImplementedError()

    @abstractmethod
    def do_insert_all(self, data, pk, partition=None, replace=False, 
                      ignore=False, commit=True, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def suggest_column_types(self, data, column_type, n_suggest_check=None):
        raise NotImplementedError()

    @property
    def uri(self):
        return self._uri

    @property
    def uri_schema(self):
        return self._uri_schema

    @property
    def uri_options(self):
        return self._uri_options

    @property
    def table_name(self):
        return self._table_name

    def _add_extra_data(self, data, extra_data):
        if extra_data:
            return [{**item, **extra_data} for item in data]
        else:
            return data
