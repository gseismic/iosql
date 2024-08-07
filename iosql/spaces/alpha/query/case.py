import re
from typing import List
from ..exception import BadArgument


class Case(object):
    """
    用于描述单个查询条件
    example:
        case = {
            'field': 'date'
            'op': 'between_and',
            'value': ['2020-03-08', '2021-10-10']
            'rank': 0,
        }
        case = {
            'field': 'close'
            'op': '=',
            'value': '[[open]]'
        }
        case = {
            'field': 'date'
            'op': 'between_and',
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
        self.op = op
        self.value = self._parse_value(value)
        self.check_field_tvalue(op, self.value['value'])
        self.rank = self._default_rank if rank < 0 else rank

    def cid(self):
        # 用于判断一组cases是否相等
        return str(self)

    def _parse_value(self, value):
        o_value = None
        if isinstance(value, (int, float, bool, str, tuple, list)):
            o_value = self._parse_nondict_value(value)
        elif isinstance(value, dict):
            assert 'value' in value
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
            assert isinstance(value, (tuple, list))
            assert len(value) == 2
        elif op == 'in':
            assert isinstance(value, (tuple, list))
            assert len(value) >= 1
        else:
            assert isinstance(value, (int, float, str))

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

    def __init__(self, op, left, right):
        assert op in ['or', 'and']
        assert isinstance(left, (Case, CaseExpression))
        assert isinstance(right, (Case, CaseExpression))
        self.op = op
        self.left = left
        self.right = right

    def __rmul__(self, other):
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
