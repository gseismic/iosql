import time
import arrow
import logging
from iosql import Database
from iosql.query.where import Where
from iosql.query.case import Case
from iosql.query.case_util import flatten_expression, unique_or_and_cases


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')


def test_case():
    case1 = Case(field='date', op='>', value=111)
    print(case1)
    
    case2 = Case(field='date', op='>', value='[[open]]')
    print(case2)
    
    case3 = Case(field='date', op='>', value={'value': 'open', 'type':
                                             'variable'})
    print(case3)
    assert case2 == case3

    case5 = Case(field='date', op='>', value={'value': '[[open]]', 'type':
                                             'variable'})
    print(case5)

    # print(f'{Case.unique_cases([case1,case1])=}')   
    # print(f'{Case.unique_cases([case1,case1, case2,case2, case3])=}')
    print(f'{unique_or_and_cases([[case1],[case2]])=}')
    print(f'{unique_or_and_cases([[case1],[case1,case3], [case1]])=}')
    print(f'{unique_or_and_cases([[case1],[case1,case3], [case2,case3], [case1]])=}')

    where = Where(case1)
    print(f'{where.or_and_cases=}')
    print(where)
    where = Where([case1, case1])
    print(f'{where.or_and_cases=}')

    where = Where([[case1, case1]])
    print(f'{where.or_and_cases=}')


def test_case_expr():
    c1 = Case(field='date', op='>', value=3)
    c2 = Case(field='date', op='<', value=8)
    c3 = Case(field='date', op='>', value={'value': '[[open]]', 'type':
                                             'variable'})
    print('c1', c1)
    print('c2', c2)
    print('c3', c3)
    print('c1*c2', c1*c2)
    print('c1*c2*c3', c1*c2*c3)


def test_case_flatten():
    c1 = Case(field='date', op='>', value=111)
    c2 = Case(field='date', op='<', value=222)
    c3 = Case(field='date2', op='<', value=333)
    c5 = Case(field='date2', op='<', value=555)
    c6 = Case(field='date2', op='<', value=666)
    c7 = Case(field='date2', op='<', value=777)

    print('='*20)
    print(f'{flatten_expression(c1)=}')
    print('='*20)
    print(f'{flatten_expression(c1*c2)=}')
    print('='*20)
    print(f'{flatten_expression(c1*c2+c3)=}')
    print('='*20)
    print(f'{flatten_expression(c1*(c2+c3))=}')
    print('='*20)
    print(f'{(c2+c3)*(c5+c6)=}')
    print(f'{flatten_expression((c2+c3)*(c5+c6))=}')
    print('='*20)
    print(f'{(c2+c3)+(c5+c6)=}')
    print(f'{flatten_expression((c2+c3)+(c5+c6))=}')



if __name__ == '__main__':
    if 1:
        test_case()
    if 0:
        test_case_expr()
    if 0:
        test_case_flatten()
