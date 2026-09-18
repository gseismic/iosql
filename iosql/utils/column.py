from collections import OrderedDict
import datetime
import numbers

try:
    import numpy as np
    has_np = True
except ImportError:
    has_np = False


sqlite_type_map = {
    str: "text",
    int: "integer",
    float: "float",
    bool: "integer",
    bytes: "blob",
    datetime.datetime: "text"
}

if has_np:
    sqlite_type_map.update(
        {
            np.int8: "integer",
            np.int16: "integer",
            np.int32: "integer",
            np.int64: "integer",
            np.uint8: "integer",
            np.uint16: "integer",
            np.uint32: "integer",
            np.uint64: "integer",
            np.float16: "float",
            np.float32: "float",
            np.float64: "float",
            np.bool_: "integer",
        }
    )


def sqlite_type_of_value(v):
    # numpy 标量统一转成 Python 原生类型；未知数值类型走 numbers 兜底
    if has_np and isinstance(v, np.generic):
        v = v.item()
    type_ = sqlite_type_map.get(type(v))
    if type_ is not None:
        return type_
    if isinstance(v, bool):
        return 'integer'
    if isinstance(v, numbers.Integral):
        return 'integer'
    if isinstance(v, numbers.Real):
        return 'float'
    return 'text'


def sqlite_types_of_data(data, n_check=None):
    n_check = n_check or len(data)

    column_types = OrderedDict()
    dict_rank = {'integer': 0, 'float': 1, 'blob':2, 'text': 5}
    for i, dic in enumerate(data):
        if i >= n_check:
            break
        for k, v in dic.items():
            type_ = sqlite_type_of_value(v)
            if k in column_types and dict_rank[column_types[k]] > dict_rank[type_]:
                pass
            else:
                column_types[k] = type_

    return column_types


def suggest_column_types(data, column_type=None, n_check=None):
    out_column_types = OrderedDict()

    column_type = column_type or {}
    if column_type:
        data_fields = set()
        rows_to_check = data if n_check is None else data[:n_check]
        for row in rows_to_check:
            data_fields.update(row)
        if data_fields.issubset(column_type):
            return OrderedDict(column_type)

    # 先保留全部声明列（即使本批数据未出现），再补充数据中推断出的新列；
    # 旧实现走推断路径时会把「已声明但数据未出现」的列静默丢弃。
    out_column_types.update(column_type)

    kvalue_types = sqlite_types_of_data(data, n_check)
    for column, type_by_data in kvalue_types.items():
        if column not in out_column_types:
            out_column_types[column] = type_by_data

    return out_column_types


def suggest_sqlite_column_types(data, column_type=None, n_check=None):
    return suggest_column_types(data, column_type, n_check)
