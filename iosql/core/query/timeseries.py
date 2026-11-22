"""共用时序条件与分组参数语义，避免各存储引擎的边界规则漂移。"""

import warnings

from .where import Where
from .case import CaseExpression
from ...exception import BadArgument, MissingTimeColumn


class TimeseriesQueryMixin:
    """只编译逻辑条件；物理执行、裁剪和资源管理由各引擎负责。"""

    def _normalize_latest_by(self, by, symbol_column):
        if by is None:
            return [symbol_column]
        if isinstance(by, str):
            fields = [item.strip() for item in by.split(',') if item.strip()]
        elif isinstance(by, (list, tuple)):
            fields = list(by)
        else:
            raise BadArgument('latest by must be a string/list/tuple')
        if not fields:
            raise BadArgument('latest by must not be empty')
        return fields

    def _resolve_latest_by_arg(self, by, group_by):
        if group_by is not None:
            warnings.warn(
                "latest(group_by=) 已废弃：group_by 是 by 的别名，请改用 by=",
                DeprecationWarning,
                stacklevel=3,
            )
            if by is not None and by != group_by:
                raise BadArgument('latest by and group_by conflict')
            return group_by
        return by

    def _normalize_symbols(self, symbols):
        if symbols is None:
            return None
        if isinstance(symbols, str) or not isinstance(symbols, (list, tuple, set)):
            return [symbols]
        return list(symbols)

    def _compile_query_where(self, query_spec):
        generated = []
        table_spec = self.spec()

        if query_spec.symbols is not None:
            symbol_column = table_spec.symbol_column if table_spec else None
            if not symbol_column:
                raise BadArgument(
                    'query(symbols=) 需要表契约声明 symbol_column。'
                    '修复: db.table(..., symbol_column="symbol") 或 '
                    'db.timeseries(..., symbol_column="symbol")'
                )
            symbols = self._normalize_symbols(query_spec.symbols)
            if symbols:
                # IN 中的每个元素都作为字面量绑定，包括单值 "[[other]]"。
                generated.append({
                    'field': symbol_column,
                    'op': 'in',
                    'value': symbols,
                })

        if query_spec.date is not None:
            time_column = self._query_time_column(table_spec)
            start, end = time_column.normalize_date_range(query_spec.date)
            if time_column.unit == 'date':
                generated.append({
                    'field': time_column.name,
                    'op': '=',
                    'value': start,
                })
            else:
                generated.extend([
                    {'field': time_column.name, 'op': '>=', 'value': start},
                    {'field': time_column.name, 'op': '<=', 'value': end},
                ])

        if query_spec.session is not None:
            session_column = self._query_session_column(table_spec)
            sessions = query_spec.session
            if isinstance(sessions, str) or not isinstance(sessions, (list, tuple, set)):
                sessions = [sessions]
            sessions = list(sessions)
            if len(sessions) == 1:
                generated.append({
                    'field': session_column,
                    'op': '=',
                    'value': sessions[0],
                })
            elif sessions:
                generated.append({
                    'field': session_column,
                    'op': 'in',
                    'value': sessions,
                })

        if query_spec.start is not None or query_spec.end is not None:
            time_column = self._query_time_column(table_spec)
            if query_spec.start is not None:
                generated.append({
                    'field': time_column.name,
                    'op': '>=',
                    'value': self._normalize_query_time_bound(time_column, query_spec.start, lower=True),
                })
            if query_spec.end is not None:
                generated.append({
                    'field': time_column.name,
                    'op': '<=',
                    'value': self._normalize_query_time_bound(time_column, query_spec.end, lower=False),
                })

        return self._combine_query_where(query_spec.where, generated)

    def _normalize_query_time_bound(self, time_column, value, *, lower):
        """默认沿用逻辑时钟；物理存储精度不同的后端可分别对齐上下界。"""
        return time_column.normalize_value(value)

    def _query_time_column(self, table_spec):
        if not table_spec or not table_spec.time:
            raise MissingTimeColumn(
                f"表 `{self._table_name}` 没有 time 契约，query(start/end/date) "
                "与 latest()/asof() 需要它。"
                "修复: 建表时传 time=\"ts\"，或使用 duration/calendar 分区"
                "（可自动反推 time）"
            )
        return table_spec.time

    def _query_session_column(self, table_spec):
        if not table_spec or not table_spec.session_column:
            raise BadArgument(
                'query session requires TableSpec.session_column'
            )
        return table_spec.session_column

    def _build_where(self, where):
        """Where 构造统一包装：非法条件统一转 BadArgument，避免裸 KeyError/TypeError。"""
        try:
            return Where(where)
        except BadArgument:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise BadArgument(
                f'无法解析 where 条件: {where!r}；'
                '合法形式: dict/Case/CaseExpression/文本/列表，'
                'dict 需包含 field/op/value'
                '（示例: {"field": "ts", "op": ">=", "value": 0}）'
            ) from e

    def _combine_query_where(self, where, generated):
        if not generated:
            return where
        if where is None:
            return generated
        if isinstance(where, str):
            parsed = self._build_where(where).or_and_cases or []
            if not parsed:
                return generated
            return [list(cases) + generated for cases in parsed]
        if isinstance(where, dict):
            return [where] + generated
        if isinstance(where, (list, tuple)):
            if any(isinstance(item, (list, tuple)) for item in where):
                combined = []
                for item in where:
                    if isinstance(item, (list, tuple)):
                        combined.append(list(item) + generated)
                    else:
                        combined.append([item] + generated)
                return combined
            return list(where) + generated
        if isinstance(where, CaseExpression):
            raise BadArgument(
                'query() cannot combine CaseExpression with symbols/start/end; '
                'use select() for complex expressions'
            )
        return [where] + generated
