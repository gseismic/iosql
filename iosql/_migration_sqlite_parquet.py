"""SQLite 到 Parquet 的逐物理文件读取及独立源行数统计。"""

from contextlib import closing

from ._migration_progress import emit
from .engines.sqlite.query_util import SqlQuery
from .exception import UnsafeOperation


class CountedSQLiteReader:
    """在每个 SQLite 文件的同一语句快照中计数并读取，不信任历史行数缓存。"""

    def __init__(self, table, batch_size, table_name, *, align_partitions=False):
        self.table = table
        self.batch_size = batch_size
        self.table_name = table_name
        self.align_partitions = align_partitions
        self.source_rows = 0
        self.complete = False

    def __iter__(self):
        refs = self.table._physical_part_refs(lightweight=True)
        pending = []
        # LEFT JOIN 保证空文件也返回包含 COUNT(*) 的哨兵行，且计数与数据
        # 使用同一个 SQLite 语句快照。最后一列仅供迁移使用，不进入目标。
        query = SqlQuery(
            'SELECT data.*, counts.n FROM '
            '(SELECT COUNT(*) AS n FROM data) AS counts LEFT JOIN data ON 1=1', []
        )
        for index, ref in enumerate(refs, 1):
            self.table._ensure_ref_file_present(ref)
            path = self.table._get_part_dbfile(ref.part_key)
            batches = self.table._iter_part_query_batches(path, query, self.batch_size)
            part_count = None
            with closing(batches):
                for batch in batches:
                    if not batch:
                        continue
                    if part_count is None:
                        part_count = int(batch[0][-1])
                        self.source_rows += part_count
                        emit('count_progress', phase='source', table=self.table_name,
                             done=index, total=len(refs), rows=self.source_rows)
                        if self.align_partitions and part_count >= self.batch_size and pending:
                            yield pending
                            pending = []
                    if part_count == 0:
                        if len(batch) != 1:
                            raise UnsafeOperation(f'源分区空表计数结果无效: {path}')
                        continue
                    for row in batch:
                        if row[-1] != part_count:
                            raise UnsafeOperation(f'源分区计数结果不一致: {path}')
                        pending.append(dict(zip(row.keys()[:-1], row[:-1])))
                        if len(pending) == self.batch_size:
                            yield pending
                            pending = []
            if part_count is None:
                raise UnsafeOperation(f'源分区没有返回独立计数结果: {path}')
            if self.align_partitions and part_count >= self.batch_size and pending:
                yield pending
                pending = []
        if pending:
            yield pending
        self.complete = True
