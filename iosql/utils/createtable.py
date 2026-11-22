from collections import OrderedDict
from ..exception import BadArgument
from ..logger import default_logger as logger


def reorder_by_column_order(column_infos, column_order):
    '''对表column排序
    '''
    if not column_order:
        return column_infos

    # column_order 必须已经存在
    ordered_column_infos = OrderedDict()
    for column in column_order:
        if column not in column_infos:
            raise BadArgument(f'column_order 包含未知列: {column!r}')
        ordered_column_infos[column] = column_infos[column]

    # [@2021-12-01 15:34:18] FIXED
    for column, item in column_infos.items():
        if column not in ordered_column_infos:
            ordered_column_infos[column] = item

    return ordered_column_infos


def build_createtable_meta(data_suggested_types, pk, partition, **kwargs):
    '''
    kwargs: 统一用单数
    '''
    assert isinstance(data_suggested_types, (dict, OrderedDict))
    # [@2021-11-30 12:47:22] FIXED 
    # pk = pk if isinstance(pk, (list, tuple, None)) else [pk]
    pk = [pk] if isinstance(pk, str) else pk or []

    # 这里跟sqlite-utils 不一样，这里统一用单数
    if 'defaults' in kwargs:
        logger.warning('Param `defaults` ignored: please use `default`')
    defaults = kwargs.get('default', {})

    not_nulls = kwargs.get('not_null', set())
    column_order = kwargs.get('column_order') or []
    index = kwargs.get('index') or {}
    comments = kwargs.get('comment') or {}
    column_type = kwargs.get('column_type') or {}
    unique = kwargs.get('unique') or {}
    # n_check = kwargs.get('n_suggest_check')

    column_infos = OrderedDict()
    for key, sugg_type in data_suggested_types.items():
        # dtype = get_dtype(value) # python data type
        if key in column_type:
            type_ = column_type[key]
        else:
            type_ = sugg_type
        dtype = None # 无法保证数据类型一致，仅仅为保留字段
        # type_ = sqlite_dtype_to_type(dtype)
        default = defaults.get(key, None)
        not_null = True if key in not_nulls else False
        is_pk = True if key in pk else False
        should_index = True if key in index else False
        index_created = False

        dic = { 
            'name': key,
            'type': type_,
            'is_pk': is_pk,
            'default': default,
            'unique': unique.get(key, False),
            'not_null': not_null,
            'should_index': should_index,
            'index_created': index_created,
            'dtype': dtype,
            'comment': comments.get(key)
        }
        column_infos[key] = dic

    column_infos = reorder_by_column_order(column_infos, column_order)

    meta = {'column_info': column_infos, 'pk': pk, 'partition': partition}
    if 'table_contract' in kwargs:
        meta['table_contract'] = kwargs.get('table_contract')
    elif 'table_spec' in kwargs:
        meta['table_contract'] = kwargs.get('table_spec')
    return meta
