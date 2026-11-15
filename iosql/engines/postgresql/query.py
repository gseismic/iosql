"""PostgreSQL 时序查询和原生计划，保证大结果在数据库端执行并流式返回。"""

from dataclasses import replace
from datetime import timedelta
import re

import arrow

from ...core.query.timeseries import TimeseriesQueryMixin
from ...exception import BadArgument
from ...coordination import guard_query
from ...spec import QueryPlan, QuerySpec, _natural_day_bounds
from ..sqlite.query_util import _quote_sql_identifier


def _quote(name):
    return _quote_sql_identifier(name).replace('%', '%%')


class _TimeColumn:
    """把逻辑时钟转换到物理日期列；谓词直接作用于列，保留分区裁剪能力。"""

    def __init__(self, table, logical, physical):
        self.name = logical.name
        self.unit = 'ms' if physical in ('TIMESTAMP', 'TIMESTAMPTZ') else logical.unit
        if physical == 'DATE':
            self.unit = 'date'
        self._logical = logical
        self._physical = physical
        self._table = table
        self._text_format = None  # 文本时间格式由样本决定，与分区周期分开
        if physical == 'TEXT':
            with table.connect().cursor() as cursor:
                cursor.execute(
                    f'SELECT {_quote(self.name)} FROM {_quote(table.table_name)} '
                    f'WHERE {_quote(self.name)} IS NOT NULL LIMIT 1', ()
                )
                sample = cursor.fetchone()
            self._text_format, has_time = _text_time_format(str(sample[0]) if sample else '2000-01-01')
            self.unit = 'ms' if has_time else 'date'

    def _value(self, value):
        if isinstance(value, (int, float)):
            value = value / (1000 if self._logical.unit == 'ms' else 1)
            return arrow.get(value).to(self._logical.timezone)
        aware = getattr(value, 'tzinfo', None) is not None
        if isinstance(value, str):
            aware = bool(re.search(r'(Z|[+-]\d{2}:?\d{2})$', value))
        if isinstance(value, str) and value.isdigit() and self._text_format:
            parsed = arrow.get(value, self._text_format)
        else:
            parsed = arrow.get(value)
        if aware:
            return parsed.to(self._logical.timezone)
        # 无偏移的原生日期/墙上时间按契约时区解释，不能先当 UTC 再移到前一天。
        return parsed.replace(tzinfo=self._logical.timezone)

    def normalize_value(self, value):
        value = self._value(value)
        if self._physical == 'DATE':
            return value.format('YYYY-MM-DD')
        if self._physical == 'TIMESTAMP':
            return value.format('YYYY-MM-DD HH:mm:ss.SSSSSS')
        if self._physical == 'TIMESTAMPTZ':
            return value.format('YYYY-MM-DD HH:mm:ss.SSSSSSZZ')
        return value.format(self._text_format)

    def normalize_bound(self, value, *, lower):
        normalized = self.normalize_value(value)
        if self._physical != 'TEXT' or self.unit == 'date' or not lower:
            return normalized
        local = self._value(value)
        rounded = arrow.get(normalized, self._text_format)
        if rounded.naive < local.naive:
            # 文本表示的是墙上时间，在无时区的日历上对齐，避免 DST 的 shift 改写边界。
            fraction = re.search(r'\.(S+)$', self._text_format)
            if fraction:
                step = timedelta(microseconds=10 ** (6 - len(fraction[1])))
            elif self._text_format.endswith('ss'):
                step = timedelta(seconds=1)
            elif self._text_format.endswith('mm'):
                step = timedelta(minutes=1)
            else:
                step = timedelta(hours=1)
            return arrow.get(rounded.naive + step).format(self._text_format)
        return normalized

    def normalize_date_range(self, value):
        start, next_day = _natural_day_bounds(self._value(value))
        end = next_day.to('UTC').shift(microseconds=-1)
        return self.normalize_value(start), self.normalize_value(end)


def _text_time_format(sample):
    """查询精度匹配文本值，不因按月分区就把每日记录的谓词截成月份。"""
    if sample.isdigit():
        fmt = {4: 'YYYY', 6: 'YYYYMM', 8: 'YYYYMMDD', 10: 'YYYYMMDDHH',
               12: 'YYYYMMDDHHmm', 14: 'YYYYMMDDHHmmss'}.get(len(sample))
        if fmt:
            return fmt, len(sample) > 8
    if len(sample) in (4, 7, 10):
        return {4: 'YYYY', 7: 'YYYY-MM', 10: 'YYYY-MM-DD'}[len(sample)], False
    match = re.fullmatch(r'\d{4}-\d{2}-\d{2}([ T])\d{2}(:\d{2})?(:\d{2})?(\.\d{1,6})?', sample)
    if not match:
        raise BadArgument('calendar 文本时间须统一 compact 或无偏移 ISO 格式；带偏移时间请使用 timestamptz')
    fmt = 'YYYY-MM-DD' + match[1] + 'HH'
    if match[2]:
        fmt += ':mm'
    if match[3]:
        fmt += ':ss'
    if match[4]:
        fmt += '.' + 'S' * (len(match[4]) - 1)
    return fmt, True


@guard_query
class _Query:
    """惰性查询对象；同一 SQL 同时用于读取和 explain，防止诊断与执行脱节。"""

    def __init__(self, table, spec, by=None, latest=False):
        self._table = table
        self._spec = spec
        self._by = by
        self._latest = latest

    def _sql(self):
        if self._latest:
            return self._table._latest_query(self._spec, self._by)
        where = self._table._compile_query_where(self._spec)
        return self._table._select_query(
            self._spec.columns, where, self._spec.order_by, self._spec.limit
        )

    def to_list(self):
        return self._table._execute_query(*self._sql(), to_dict=True)

    def one(self):
        rows = self.to_list()
        if len(rows) > 1:
            raise BadArgument('query.one() requires at most one row')
        return rows[0] if rows else None

    def iter_batches(self, batch_size=10_000, timeout_ms=None):
        return self._table._iter_query_batches(
            *self._sql(), batch_size=batch_size, timeout_ms=timeout_ms
        )

    def iter_rows(self, batch_size=10_000, timeout_ms=None):
        batches = self.iter_batches(batch_size, timeout_ms)
        try:
            for batch in batches:
                yield from batch
        finally:
            batches.close()

    def explain(self, *, analyze=False, raw_plan=False):
        return self._table._explain_query(
            self._spec, *self._sql(), analyze=analyze, raw_plan=raw_plan
        )

    def spec(self):
        return self._spec


class PostgreSQLQueryMixin(TimeseriesQueryMixin):
    """提供原生时序查询；只共享逻辑参数语义，不依赖目录表布局。"""

    def query(self, symbols=None, start=None, end=None, date=None, session=None,
              columns='*', where=None, order_by=None, limit=None):
        return _Query(self, QuerySpec(
            table=self.table_name, columns=columns, symbols=symbols, start=start,
            end=end, date=date, session=session, where=where, order_by=order_by,
            limit=limit,
        ))

    def latest(self, symbols=None, as_of=None, columns='*', where=None,
               session=None, by=None, group_by=None):
        time = self._query_time_column(self.spec())
        by = self._resolve_latest_by_arg(by, group_by)
        return _Query(self, QuerySpec(
            table=self.table_name, columns=columns, symbols=symbols, end=as_of,
            session=session, where=where, order_by=f'{time.name} desc',
        ), by=by, latest=True)

    def asof(self, symbols=None, as_of=None, columns='*', where=None,
             session=None, by=None, group_by=None):
        return self.latest(symbols, as_of, columns, where, session, by, group_by)

    def _query_time_column(self, table_spec):
        logical = super()._query_time_column(table_spec)
        physical = self._pg_column_type(self._column_types()[logical.name])
        calendar = table_spec.partition and table_spec.partition.kind == 'calendar'
        if physical in ('DATE', 'TIMESTAMP', 'TIMESTAMPTZ') or (
            physical == 'TEXT' and calendar
        ):
            return _TimeColumn(self, logical, physical)
        return logical

    def _normalize_query_time_bound(self, time_column, value, *, lower):
        if isinstance(time_column, _TimeColumn):
            return time_column.normalize_bound(value, lower=lower)
        return super()._normalize_query_time_bound(time_column, value, lower=lower)

    def _latest_query(self, spec, by):
        contract = self.spec()
        time = self._query_time_column(contract)
        symbol = contract.symbol_column
        if by is None and symbol is None:
            raise BadArgument('latest 需要 symbol_column 或显式 by=[...]')
        groups = self._normalize_latest_by(by, symbol)
        types = self._column_types()
        for field in groups:
            if field not in types:
                raise BadArgument(f'latest grouping field missing from row: {field}')
        groups = list(dict.fromkeys(groups))
        tie = [(col, 'asc') for col in contract.primary_key if col != time.name]
        latest_order = [(time.name, 'desc')] + tie
        distinct = ', '.join(_quote(field) for field in groups)
        inner_order = [(col, 'asc') for col in groups] + [
            item for item in latest_order if item[0] not in groups
        ]
        where = self._compile_query_where(spec)
        where_sql, params = self._where_sql(where, types)
        requested = self._normalize_symbols(spec.symbols)
        if requested == []:
            where_sql = 'FALSE'
            params = []
        if requested and groups == [symbol] and self._has_latest_seek_index(symbol, time.name):
            # 仅在目录确认有前缀索引时逐个取最新行，避免无索引时重复全表扫描。
            return self._latest_seek_query(spec, requested, symbol, latest_order, types)
        inner = f'SELECT DISTINCT ON ({distinct}) * FROM {_quote(self.table_name)}'
        if where_sql:
            inner += f' WHERE {where_sql}'
        inner += ' ORDER BY ' + self._order_by_sql(inner_order, types)
        field_sql = self._select_fields_sql(spec.columns)
        if field_sql == '*':
            projection = 'q.*'
        else:
            names = spec.columns if isinstance(spec.columns, (list, tuple)) else [
                part.strip().strip('"').replace('""', '"') for part in spec.columns.split(',')
            ]
            projection = ', '.join('q.' + _quote(name) for name in names)
        query = f'SELECT {projection} FROM ({inner}) AS q'
        prefix = ''
        if requested and symbol in groups:
            # VALUES 保留 symbol 的请求顺序及重复值，结果仍在服务端排序。
            kind = self._pg_column_type(types[symbol])
            query += ' JOIN (VALUES ' + ', '.join(f'(%s::{kind}, %s)' for _ in requested)
            query += f') AS requested(value, ordinal) ON q.{_quote(symbol)} = requested.value'
            params.extend(v for i, value in enumerate(requested) for v in (value, i))
            prefix = 'requested.ordinal ASC, '
        query += ' ORDER BY ' + prefix + ', '.join(
            'q.' + self._order_by_sql([item], types) for item in latest_order
        )
        return query, params

    def _has_latest_seek_index(self, symbol, time):
        for definition in self._index_definitions():
            _, keys, _, _, _, valid, method, plain, _, _ = definition
            if valid and method == 'btree' and plain and list(keys[:2]) == [symbol, time]:
                return True
        return False

    def _latest_seek_query(self, spec, requested, symbol, order, types):
        """有索引的单分组最新值以 LIMIT 1 读取，成本按请求组数增长。"""
        where = self._compile_query_where(replace(spec, symbols=None))
        where_sql, where_params = self._where_sql(where, types)
        predicate = f'{_quote(symbol)} = requested.value'
        if where_sql:
            predicate += f' AND ({where_sql})'
        field_sql = self._select_fields_sql(spec.columns)
        if field_sql == '*':
            projection = 'q.*'
        else:
            names = spec.columns if isinstance(spec.columns, (list, tuple)) else [
                part.strip().strip('"').replace('""', '"') for part in spec.columns.split(',')
            ]
            projection = ', '.join('q.' + _quote(name) for name in names)
        # VALUES 会把纯字符串参数推断成 text；显式类型让数值 symbol 仍能匹配索引。
        kind = self._pg_column_type(types[symbol])
        values = ', '.join(f'(%s::{kind}, %s)' for _ in requested)
        query = (f'SELECT {projection} FROM (VALUES {values}) AS requested(value, ordinal) '
                 f'CROSS JOIN LATERAL (SELECT * FROM {_quote(self.table_name)} '
                 f'WHERE {predicate} ORDER BY {self._order_by_sql(order, types)} LIMIT 1) q '
                 'ORDER BY requested.ordinal')
        params = [v for i, value in enumerate(requested) for v in (value, i)]
        return query, params + where_params

    def explain_select(self, fields='*', where=None, order_by=None, limit=None,
                       columns=None, symbols=None, start=None, end=None,
                       date=None, session=None, *, analyze=False, raw_plan=False):
        from ...table import resolve_select_fields
        fields = resolve_select_fields(fields, columns, 'explain_select')
        return self.query(symbols, start, end, date, session, fields, where,
                          order_by, limit).explain(analyze=analyze, raw_plan=raw_plan)

    def _explain_query(self, spec, query, params, *, analyze=False, raw_plan=False):
        if not isinstance(analyze, bool) or not isinstance(raw_plan, bool):
            raise BadArgument('analyze/raw_plan must be bool')
        options = 'ANALYZE, BUFFERS, FORMAT JSON' if analyze else 'FORMAT JSON'
        with self.connect().cursor() as cursor:
            cursor.execute(f'EXPLAIN ({options}) {query}', params)
            payload = cursor.fetchone()[0][0]
        root = payload['Plan']
        candidates = self.table_parts() if self.partition else []
        relations, indexes = [], []

        def visit(node):
            active = not analyze or node.get('Actual Loops', 0) > 0
            relation = node.get('Relation Name')
            if active and relation in candidates and relation not in relations:
                relations.append(relation)
            if node.get('Index Name'):
                indexes.append({key: node[key] for key in (
                    'Node Type', 'Index Name', 'Relation Name', 'Plan Rows',
                    'Actual Rows', 'Actual Loops',
                ) if key in node})
            for child in node.get('Plans', []):
                visit(child)
        visit(root)
        skipped = [name for name in candidates if name not in relations]
        return QueryPlan(
            table=self.table_name, columns=spec.columns, where=spec.where if raw_plan else None,
            order_by=spec.order_by, limit=spec.limit, engine='postgresql',
            candidate_partitions=candidates, scanned_partitions=relations,
            skipped_partitions=skipped,
            skip_reasons={name: 'not executed' if analyze else 'pruned from plan' for name in skipped},
            engine_plan=payload if raw_plan else _sanitize_plan(payload),
            planning_time_ms=payload.get('Planning Time'),
            execution_time_ms=payload.get('Execution Time'), index_scans=indexes,
        )


_STRUCTURE_KEYS = {'Node Type', 'Parent Relationship', 'Relation Name', 'Schema',
                   'Alias', 'Index Name', 'Scan Direction', 'Join Type', 'Strategy',
                   'Partial Mode', 'Operation', 'Sort Method', 'Sort Space Type'}


def _sanitize_plan(value):
    """只保留结构及数值；白名单防止新版本添加含业务常量的表达式字段。"""
    if isinstance(value, dict):
        return {key: _sanitize_plan(item) for key, item in value.items()
                if isinstance(item, (dict, list, int, float, bool)) or key in _STRUCTURE_KEYS}
    if isinstance(value, list):
        return [_sanitize_plan(item) for item in value if isinstance(item, (dict, int, float, bool))]
    return value
