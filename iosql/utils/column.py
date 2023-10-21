from collections import OrderedDict
import datetime
import numpy

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
        }
    )


def sqlite_type_of_value(v):
    return sqlite_type_map.get(type(v), 'text')


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
    kvalue_types = sqlite_types_of_data(data, n_check)
    for column, type_by_data in kvalue_types.items():
        if column in column_type.items():
            out_column_types[column] = column_type[column]
        else:
            out_column_types[column] = type_by_data

    return out_column_types


def suggest_sqlite_column_types(data, column_type=None, n_check=None):
    return suggest_column_types(data, column_type, n_check)
