import re
from typing import List
from ...exception import BadArgument


# 用户语言 → 内部规范形式。归一化在 Case 构造入口完成，
# 保证下游（分区裁剪 get_case_range / SQL 编译 _OP_SQL）不再收到未知 op
_OP_ALIASES = {
    'eq': '==',
    '=': '==',
    'neq': '!=',
    'gt': '>',
    'ge': '>=',
    'lt': '<',
    'le': '<=',
    'between': 'between_and',
    'range': 'between_and',
}

_CANONICAL_OPS = ('==', '!=', '>', '>=', '<', '<=', 'between_and', 'in')

_LEGAL_OPS_DISPLAY = '==, !=, >, >=, <, <=, between, in'

_BAD_OP_EXAMPLE = '{"field": "ts", "op": "between", "value": [1, 10]}'


def normalize_case_op(op):
    key = str(op).strip()
    canonical = _OP_ALIASES.get(key, key)
    if canonical not in _CANONICAL_OPS:
        raise BadArgument(
            f"不支持的 op `{op}`。合法 op: {_LEGAL_OPS_DISPLAY}。"
            f"示例: {_BAD_OP_EXAMPLE}"
        )
    return canonical


class Case(object):
    """
    用于描述单个查询条件
    example:
        case = {
            'field': 'date'
            'op': 'between',
            'value': ['2020-03-08', '2021-10-10']
            'rank': 0,
        }
        case = {
            'field': 'close'
            'op': '==',
            'value': '[[open]]'
        }
        case = {
            'field': 'date'
            'op': 'between',
            'value': {
                'value': 'open',
                'type': 'variable/const'
            }
        }
    """

    _default_rank = 999

    def __init__(self, field, op, value, rank=0):
        # rank: -1，优先级最低 [[open ]]
        self.field = field
        self.op = normalize_case_op(op)
        self.value = self._parse_value(value)
        self.check_field_tvalue(self.op, self.value['value'])
        self.rank = self._default_rank if rank < 0 else rank

    def cid(self):
        # 用于判断一组cases是否相等
        return str(self)

    def _parse_value(self, value):
        o_value = None
        if isinstance(value, (int, float, bool, str, tuple, list)):
            o_value = self._parse_nondict_value(value)
        elif isinstance(value, dict):
            if 'value' not in value:
                raise BadArgument(
                    f"Case value dict 需要 `value` 键，得到 {value}"
                )
            __value = value['value']
            if 'type' not in value:
                o_value = self._parse_nondict_value(__value)
            else:
                # remove `[[ ]]``
                o_value = self._parse_nondict_value(__value)
                o_value['type'] = value['type']
        else:
            raise BadArgument(f'Error Parse Case value `{value}`')
        return o_value

    def _parse_nondict_value(self, value):
        if isinstance(value, (int, float, bool, tuple, list)):
            o_value = {'value': value, 'type': 'const'}
        elif isinstance(value, str):
            o = re.match(r'\[\[(.*?)\]\]', value)
            if o:
                o_value = {
                    'value': o.group(1).strip(),
                    'type': 'variable'
                }
            else:
                o_value = { 'value': value, 'type': 'const' }
        else:
            raise BadArgument(f'Error Parse Case value {value}')
        return o_value

    def check_field_tvalue(self, op, value):
        if op == 'between_and':
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                raise BadArgument(
                    'op `between` 需要长度为 2 的列表值，'
                    f'示例: {_BAD_OP_EXAMPLE}'
                )
        elif op == 'in':
            if not isinstance(value, (tuple, list)) or len(value) < 1:
                raise BadArgument(
                    'op `in` 需要非空列表值，'
                    '示例: {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]}'
                )
        else:
            if not isinstance(value, (int, float, str)):
                raise BadArgument(
                    f'op `{op}` 的值必须是 int/float/str，'
                    f'得到 {type(value).__name__}: {value}'
                )

    def __rmul__(self, other):
        return CaseExpression('and', other, self)

    def __mul__(self, other):
        return CaseExpression('and', self, other)

    def __radd__(self, other):
        return CaseExpression('or', other, self)

    def __add__(self, other):
        return CaseExpression('or', self, other)

    def __eq__(self, other):
        return self.cid() == other.cid()

    @classmethod
    def from_dict(cls, dic):
        return Case(dic['field'], dic['op'], dic['value'])

    @classmethod
    def __from_tuple(cls, tu):
        # 废弃，容易歧义
        return Case(*tu)

    @classmethod
    def unique_and_cases(cls, cases: 'List[Case]'):
        out_cases = []
        out_cids = set()
        for case in cases:
            cid = case.cid()
            if cid not in out_cids:
                out_cases.append(case)
                out_cids.add(cid)
        return out_cases

    def to_dict(self):
        return {
            'field': self.field, 'op': self.op, 'value': self.value
        }

    def __repr__(self):
        return f'Case(field={self.field}, op={self.op}, value={self.value}'


class CaseExpression(object):
    """
    An expression of cases
    """
    def __init__(self, op, left, right):
        assert op in ['or', 'and']
        assert isinstance(left, (Case, CaseExpression))
        assert isinstance(right, (Case, CaseExpression))
        self.op = op
        self.left = left
        self.right = right

    def __rmul__(self, other):
        """
        Right multiply
        """
        return CaseExpression('and', other, self)

    def __mul__(self, other):
        return CaseExpression('and', self, other)

    def __radd__(self, other):
        return CaseExpression('or', other, self)

    def __add__(self, other):
        return CaseExpression('or', self, other)

    def __repr__(self):
        def format_(expr):
            if isinstance(expr, Case):
                return str(expr)
            else:
                return f'({format_(expr.left)} {expr.op.upper()} {format_(expr.right)})'
                # return '(' + format_(expr.left) + expr.op.upper() + str(expr.right) + ')'
        return f'({format_(self.left)} {self.op.upper()} {format_(self.right)})'
