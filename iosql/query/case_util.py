from typing import List
from ..exception import BadArgument
from .case import Case, CaseExpression


def sorted_cases(cases):
    # https://stackoverflow.com/questions/1915376/is-pythons-sorted-function-guaranteed-to-be-stable
    return sorted(cases, key=lambda c: c.rank)


def get_case_range(case):
    """
    Get the range of a case
    """
    case_min, case_max = None, None
    op = case.op
    value = case.value['value']
    if op in ['between_and', 'range']:
        # [@2021-11-28 02:06:58] BUG FIXED
        # case_max = value[0]
        # case_min = value[1]
        case_min = value[0]
        case_max = value[1]
    elif op in ['gt', 'ge', '>', '>=']:
        case_min = value
    elif op in ['lt', 'le', '<', '<=']:
        case_max = value
    elif op in ['eq', '==', '=']:
        case_min = value
        case_max = value
    return op, case_min, case_max


def unique_or_and_cases(list_cases: 'List[List[Case]]'):
    """
    Remove duplicate cases from a list of cases
    """
    # Or[And[..]..]
    # 去除重复的
    # And[..] 已经通过unique_and_cases 去重

    # step1. Or 宏观去重
    # 可以省略，因为通过后续的superset包含了
    #out_and_cases = []
    #and_cids = set()
    #for cases in list_cases:
    #    and_id = tuple(sorted([c.cid() for c in cases]))
    #    if and_id not in out_and_cases:
    #        out_and_cases.append(cases)
    #        and_cids.add(and_id)

    # step2. 如果And条件是 另一个And条件的 superset，则在OR运算中被自动满足
    out_and_cases = list_cases
    list_and_sets = []
    for cases in out_and_cases:
        list_and_sets.append(set([c.cid() for c in cases]))

    to_del_index = set()
    n = len(out_and_cases)
    for i in reversed(range(n)):
        to_del = False
        # 会导致凡是出现相同的都被删除
        # for j in range(n)
        for j in range(i):
            if list_and_sets[i].issuperset(list_and_sets[j]):
                to_del = True
                break
        if to_del:
            to_del_index.add(i)

    final_out = []
    for i, cases in enumerate(out_and_cases):
        if i not in to_del_index:
            final_out.append(cases)
        else:
            print(f'\t**Removing {cases}')
            pass

    return final_out


def flatten_expression(expression):
    """
    Flatten an expression
    """
    # 内部使用，有必有吗？
    # note: 先运算，再统一排序，防止复杂表达式的过早排序
    # [@2021-11-26 05:52:28] cc
    # assert isinstance(expression, (Case, CaseExpression))
    if not expression:
        return None

    lili_cases = []
    if isinstance(expression, CaseExpression):
        op, left, right = expression.op, expression.left, expression.right
        lili_cases = _flatten_expression_tree(op, left, right)
        lili_cases = [sorted_cases(cases) for cases in lili_cases]
    #elif isinstance(expression, list):
    #    if expression and isinstance(expression[0], Case):
    #        lili_cases = [expression]
    #    #elif expression and isinstance(expression[0], dict):
    #    #    lili_cases = [Case.from_dict(expr) or expr in expression]
    #    else:
    #        lili_cases = expression
    elif isinstance(expression, Case):
        lili_cases = [[expression]]
    else:
        raise BadArgument(f'Not supported: {expression}')

    # 去重复
    lili_cases = [Case.unique_and_cases(cases) for cases in lili_cases]
    return lili_cases


def _flatten_expression_tree(op, left, right):
    """
    Flatten an expression tree
    Args:
        op: the operator of the expression
        left: the left operand of the expression
        right: the right operand of the expression
    Returns:
        A list of cases

    [or [and..] [and..]]

    c1*(c20+c21+c23) --> c1*c20 + c1*c21 + c1*c23
    更复杂的还有化简，这里不再做了，由用户层面自己控制
    """
    assert op in ['and', 'or']
    if isinstance(left, Case) and isinstance(right, Case):
        # cc
        if op == 'and':
            return [[left, right]]
        else:
            return [[left], [right]]
    elif isinstance(left, (list)) and isinstance(right, Case):
        # cc
        if op == 'and':
            # [[c1,c2]] -> c1, c2 是and关系
            # [[c1,c2], [c2,c5], [c1]] and c5
            rv = []
            for an in left:
                assert isinstance(an, list)
                rv.append(an + [right])
            return rv
        else:
            # or: [[c1,c2], [c2,c5], [c1]] or c5
            return left + [[right]]
    elif isinstance(left, Case) and isinstance(right, (list)):
        # cc
        if op == 'and':
            rv = []
            for an in right:
                assert isinstance(an, list)
                # 保序, bad: an + [left]
                rv.append([left] + an)
            return rv
        else:
            # or: [[c1,c2], [c2,c5], [c1]] or c5
            return [[left]] + right
    elif isinstance(left, (list)) and isinstance(right, (list)):
        # cc, cc
        n_left = len(left)
        n_right = len(right)
        rv = []
        if op == 'and':
            for i in range(n_left):
                assert isinstance(left[i], (list))
                for j in range(n_right):
                    assert isinstance(right[j], (list))
                    # c = left[i] + right[i] [@2021-11-26 06:27:23] fixed
                    c = left[i] + right[j]
                    rv.append(c)
        else:
            # [@2021-11-26 06:30:30] FIXED: added
            rv = left + right
        return rv
    else: 
        # cc, cc
        if isinstance(left, CaseExpression):
            _left = _flatten_expression_tree(left.op, left.left, left.right)
        else:
            _left = left
        # cc, cc
        if isinstance(right, CaseExpression):
            _right = _flatten_expression_tree(right.op, right.left, right.right)
        else:
            _right = right
        return _flatten_expression_tree(op, _left, _right)
    return rv
