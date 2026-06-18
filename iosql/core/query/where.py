from ...exception import BadArgument, IosqlBadArgument
from .case import Case, CaseExpression
from .case_util import flatten_expression
from .text_where import parse_text_where


def ensure_case(c):
    case = None
    if isinstance(c, dict):
        case = Case.from_dict(c)
    elif isinstance(c, Case):
        case = c
    else:
        raise IosqlBadArgument(f'Error Parsing Case: `{str(c)}`')
    return case


class Where(object):
    '''
    一组查询条件
    分成两次查询
        Case('field', 'in', [1,2])*Case(field, 'in', [3,5]) + Case('field2', 'in', [1,2])
    转化成: 求and, 再or
        Case('field', 'in', [1,2]) * ( Case(field, 'in', [3,5]) + Case('field2', 'in', [1,2]) )
    '''

    def __init__(self, where):
        '''
        where:
            Case
            CaseExpression: Case(xx)*Case(xx)
            [Case..] 不好区分 [[xx], [xx]], [xx, [xx]]
            {Case(xx), Case(xxx)}
            {{'field':...}, [{Case(xxx)}]}
            因为每个AndCase单独查询，所以这种表示没有问题，但容易有歧义，
        {c1, c2, c3}
        [c1, c2, c3]
        [[c1, c2], [c2, c1]]
        [c1, c2]
        [c1, [c2]]
        [[c1], [c2]]
        [[c1, c2], c2] <=> [[c1,c2], [c2]]
        case_or_expr
        AndCase: [case_or_expr..]
        OrCase: {case_or_expr..}
        '''
        if where is None:
            self.where = None
        elif isinstance(where, str):
            self.where = parse_text_where(where)
        elif isinstance(where, Case):
            self.where = where
        elif isinstance(where, dict):
            self.where = Case.from_dict(where)
        elif isinstance(where, (list, tuple)):
            self.where = where
            self._or_and_cases = self._parse_sequence_where(where)
            return
        elif isinstance(where, CaseExpression):
            self.where = where
        else:
            raise BadArgument(f'{type(where)} Not supported')

        self._or_and_cases = flatten_expression(self.where)

    @property
    def or_and_cases(self):
        return self._or_and_cases

    def _parse_sequence_where(self, where):
        """把列表条件解析为 Or[And[Case]]。"""
        if not where:
            return []
        if all(not isinstance(item, (list, tuple)) for item in where):
            cases = [ensure_case(item) for item in where]
            return [Case.unique_and_cases(cases)]

        parsed = []
        for item in where:
            if isinstance(item, (list, tuple)):
                cases = [ensure_case(case) for case in item]
            else:
                cases = [ensure_case(item)]
            parsed.append(Case.unique_and_cases(cases))
        return parsed
