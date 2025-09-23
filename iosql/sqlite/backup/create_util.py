from collections import OrderedDict
from ..logger import default_logger as logger
from ..utils.column import suggest_sqlite_column_types



def get_data_createtable_meta(one_data, pk, partition, **kwargs):
    # cc
    # 多个数据综合
    suggest_types = suggest_sqlite_column_types(data, n_check=n_check,
                                                    column_types=column_type)

    return build_createtable_meta(suggest_types, pk, partition, **kwargs)


def suggest_columns_types(data, column_types=column_types, n_check=None):
    return suggest_sqlite_column_types(data, n_check=n_check, column_types=column_types)
