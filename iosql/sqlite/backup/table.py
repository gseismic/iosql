import os
import time
import arrow
from collections import OrderedDict
# from sqlite_utils import Database
from ..table import BaseTable
from ..logger import default_logger as logger
from ..exception import InvalidUriSchema
from ..utils.partition import partition_data, get_part_range
from ..utils.column import suggest_sqlite_column_types
from .case_util import build_select_cases_sql


class Table(BaseTable):
    '''
    定位：有状态的Table
    分区模式，因为不知道在哪个分区，插入时才创建.sqlite3文件
    '''

    def __init__(self, uri):
        super().__init__(uri)
        # connect called by super()
        self._table_meta_file = 'meta.sqlite3'

    def connect(self):
        '''连接
        '''
        # cc
        logger.info('Connecting ...')
        path = self.uri_options.get('path')
        db = self.uri_options.get('db')
        if path is None or not db:
            raise InvalidUriSchema()

        # path 可以为''
        self.db_path = os.path.join(path, db)
        if not os.path.exists(self.db_path):
            logger.info(f'Creating database path: {self.db_path}')
            os.makedirs(self.db_path, exist_ok=True)

        logger.info('Database Connected.')
        
    def get_data_createtable_meta(self, one_data, pk, partition, **kwargs):
        pass

    def get_createtable_meta(columns, pk, partition=None, **kwargs):
        '''
            db["authors"].insert_all(
                {"id": 1, str: "Sally", "score": int},
                pk="id",
                not_null={"name", "score"},
                defaults={"score": 1},
            )
        '''
        not_null = kwargs.get('not_null')
        table_meta = [
        ]

        meta_columns = []
        for column, dtype in columns.items():
            pass

    def insert_all(self, table_name, data, pk, replace=False,
                   partition=None, **kwargs):
        '''
        db["authors"].insert_all(
            [{"id": 1, "name": "Sally", "score": 2}],
            pk="id",
            not_null={"name", "score"},
            defaults={"score": 1},
        )

        当前: 不同partition原则上允许不同schema

        click-house 是内部自动partition的
        所以应区分内部partion 和显式partition
        partition: 必须是内置的代码，因为可能退出程序后多次插入
            这里不应该再向用户请求partition方法（而且可能冲突)
            而是应该系统查找
        '''
        t0 = time.time()
        partitioned_data, part_column = partition_data(data, partition)
        t1 = time.time()
        updated_at = arrow.now().format('YYYY-MM-DD HH:mm:ss')
        for part, part_data in partitioned_data.items():
            # 一个table 一个文件夹
            # 因为database可能要查询table的状态，所以table的meta
            # 信息也放在 /database/meta.sqlite3中
            table_path = os.path.join(self.db_path, table_name)
            os.makedirs(table_path, exist_ok=True)
            part_dbfile = os.path.join(table_path, f'{part}.sqlite3')
            part_table_name = 'data'

            # alter=kwargs.get('alter')
            self._insert_all(
                part_dbfile, table_name, part_table_name, part_data, pk=pk,
                replace=replace, **kwargs
            )

            #----------------------------------------------------------
            # 数据写完后再写meta信息
            # check table exist
            # 创建分区表格时写入1次即可，因为不变
            # 表格包含了该表格有哪些分区，分区的列
            start_dt, end_dt = get_part_range(part, partition)
            column_min = start_dt.format('YYYY-MM-DD HH:mm:ss')
            column_max = end_dt.format('YYYY-MM-DD HH:mm:ss')

            # 增加table_part信息
            self._add_table_part_info(table_name, part, updated_at)

            # 各个列的最大最小数值
            self._add_table_part_column_info(
                table_name, part, part_column, column_min, 
                column_max, replace=True
            )
        t2 = time.time()
        logger.debug(f'partition time: {t1 - t0}')
        logger.debug(f'insert time: {t2 - t1}')

    def ensure_table(self, columns, pk, partition=None, alter=False):
        pass

    def all_table_parts(self, table_name):
        _dbfile = os.path.join(self.db_path, 'meta.sqlite')
        db_meta = Database(_dbfile)
        parts = set()
        for row in db_meta['table_part'].rows:
            print(row, table_name)
            if row['table_name'] == table_name:
                parts.add(row['part'])
        return parts

    def _add_table_part_info(self, table_name, part, updated_at):
        '''每个表有哪些分区
        '''
        # 某个表格的各分区最大最小数值
        _dbfile = os.path.join(self.db_path, 'meta.sqlite')
        db_meta = Database(_dbfile)
        db_meta.enable_wal()
        info = {
            'table_name': table_name, 'part': part, 'updated_at': updated_at,
        }
        db_meta['table_part'].insert(info, pk=['table_name', 'part'], replace=True)

    def _add_table_part_column_info(self, table_name, part, column, 
                                column_min, column_max, replace=True):
        '''某个表格的各分区: 各个字段允许的的最大/最小数值
        '''
        _dbfile = os.path.join(self.db_path, 'meta.sqlite')
        db_meta = Database(_dbfile)
        db_meta.enable_wal()
        # 增加part的编号: 不必要
        # 未来可以更新，实际的最大数值/最小数值，方便快速查找
        info = {
            'table_name': table_name, 'part': part, 'column': column,
            'column_min': column_min, 'column_max': column_max
        }
        db_meta['table_part_column'].insert(info, pk=['table_name', 'part', 'column'], replace=replace)

    def _get_part_column_meta(self, table_name):
        _dbfile = os.path.join(self.db_path, 'meta.sqlite')
        db_meta = Database(_dbfile)
        info = [dict(row) for row in db_meta['table_part_column'].rows if row['table_name'] == table_name]
        return info
        dict_info = {}
        for inf in info:
            dict_info[(inf['table_name'], inf['part'], inf['column'])] = inf
        return dict_info

    def _add_table_column_meta(self, table_name, column, column_type,
                               column_dtype, column_name=None, comment=None):
        # 用于转换用户数据和数据库数据, e.g. datetime -> 转化
        _dbfile = os.path.join(self.db_path, 'meta.sqlite')
        db_meta = Database(_dbfile)
        db_meta.enable_wal()
        infos = []
        for column, dic in columns.items():
            infos.append({
                'table_name': table_name,
                'column': column,
                'column_type': dic.get('type', 'text'), # 存储类型
                'column_dtype': dic.get('dtype', 'text'), # 真实数据类型
                'column_name': dic.get('name'),
                'comment': dic.get('comment')
            })
        # XXX 先查询字段是否一致
        table_db['table_column_info'].insert_all(infos, pk=['table_name', 'column'], replace=True)

    def create_table(self, table_name, columns, pk, partition=None, **kwargs):
        '''
        混乱的原因是：没有显式创建table
        因为columns可能有缺省数值
        采用sqlite-utils设计:
            pass
        ref:
            https://www.zhihu.com/question/40099426
        '''
        db_meta_path = os.path.join(self.db_path, 'meta.sqlite3')
        db_meta = Database(db_meta_path)

        table_info = {
            'table_name': table_name, 'columns': ','.join(list(columns.keys())), 
            'pk': ','.join(pk)
        }
        db_meta['table_info'].insert_all(infos, pk=['table_name', 'column'], replace=True)
        table_path = os.path.join(self.db_path, table_name)
        os.makedirs(table_path, exist_ok=True)
        dbfile_table_meta = os.path.join(table_path, self._table_meta_file)
        raise NotImplementedError()

    def insert(self, data, pk, replace=False, 
               commit=True, partition=None,
               fallback='rollback', **kwargs):
        pass

    def select(self, table_name, columns, where, limit=None, cluster=None, **kwargs):
        '''
        where = {
            'field': 'date'
            'op': 'between_and',
            'value': [f'"{start_date}"', f'"{end_date}"'],
            'value': {
                'value': [f'"{start_date}"', f'"{end_date}"'],
                'value': [f'[{start_date}]', f'[{end_date}]'],
                'value': [f'[close]', f'[{end_date}]'],
                'type': 'parameter'
            },
            'rank': 1
        }
        '''
        table_path = os.path.join(self.db_path, table_name)
        os.makedirs(table_path, exist_ok=True)
        w = Where(where)
        column_types = xxx

        full_data = []
        for cases in w.or_and_cases:
            data = self._select_and_cases(columns, cases, column_types)
            full_data.append((cases, data))
        # step1. 找到各part记录到的字段最大最小数值, 排除不需要查询的part
        # step2. 查找各个And([case..])的数据
        #       处理where以后, [And([case..])]之间不应有包含关系，否则可以直接去掉被包含的
        # step3. 数据分组cluster，组内排序，组间排序
        #        cluster=None, 返回: [dict..]
        #        cluster='symbol', 返回: OrderedDict[('symbol1', [dict..]), ..]
        # 一次性全部读出内存? 多个表，涉及到排序问题

    def select_and_cases(self, columns, cases, column_types):
        pass

    def _filter_parts_by_cases(self, cases):
        infos = self._get_part_column_meta(table_name)
        print('info', infos)
        # filter parts
        excluded_parts = []
        for info in infos:
            if info['column'] == where['field']:
                if where['op'] == 'between_and':
                    if (where['value'][0] > info['column_max'] or 
                        where['value'][1] < info['column_min']):
                        excluded_parts.append(info['part'])

        all_parts = set([info['part'] for info in infos])
        print('all_parts', all_parts)
        print('excluded_parts', excluded_parts)
        print('need_parts', all_parts - set(excluded_parts))
        need_parts = all_parts - set(excluded_parts)

    def _select_and_cases(self, columns, cases, column_types):
        # table field必须的，涉及到查询是否需要加 "
        # 分成独立文件是为了同步数据方便，也导致数据容易被盗
        for part in need_parts:
            part_table_name = 'data'
            sql = build_select_cases_sql(part_table_name, columns, cases)
            sql = self._build_select(part_table_name, columns, where)
            print('SQL', sql)
        # part_dbfile = os.path.join(table_path, f'{part}.sqlite3')
        # part_table_name = 'data'

    def get_all_tables(self):
        raise NotImplementedError()

    def update(self, kv, where):
        pass

    def delete(self, where):
        pass

    def set_option(self, key, value):
        raise NotImplementedError()

    def alter_all_partition_like(self, table_name, data):
        # 如果没有显示的创建表格，则以遇到的第一个数据为准
        # 
        # 不更改也可以，只在头文件里增加field描述即可
        # for parts
        kvalue_types = get_kvalue_types(data)
        print(kvalue_types)
        # text, inteter 同时出现，选integer
        rank = {'integer': 0, 'text': 5, 'float': 2}
        max_type = {}
        for column, types in kvalue_types.items():
            for t in types:
                if column not in max_type:
                    max_type[column] = t
                elif rank[t] > rank[max_type[column]]:
                    max_type[column] = t

        print('max_type', max_type)
        parts = self.all_table_parts(table_name)
        print('parts', parts)
        for part in parts:
            dbfile = os.path.join(self.db_path, table_name, part+'.sqlite3')
            print(dbfile)
            _db = Database(dbfile)
            print('max_type', max_type)
            for key, _type in max_type.items():
                print('*****', key, _type)
                try:
                    _db['data'].add_column(key, _type)
                except Exception as e: 
                    import traceback
                    # traceback.print_exc()
                    print('#'*20, e)
                    if str(e).find('duplicate column') != -1:
                        print(f'{key} already exist, ignore')
                        pass
                    else:
                        raise e


    def _insert_all(self, dbfile, full_table_name, table_name, data, pk, replace=False, **kwargs):
        # full_table_name: 全表
        alter = False
        if 'alter' in kwargs:
            alter = kwargs.pop('alter')
        try:
            db = Database(dbfile)
            db[table_name].insert_all(data, pk=pk, replace=replace, **kwargs)
        except Exception as e:
            print('alter', alter)
            if alter and str(e).find('has no column named') != -1:
                # alter all column
                self.alter_all_partition_like(full_table_name, data)
                # insert again
                db = Database(dbfile)
                db[table_name].insert_all(data, pk=pk, replace=replace, **kwargs)
                print('*'*50, 'should alter all partition')
            else:
                raise e

    def _get_db(self, dbfile):
        return Database(dbfile)
