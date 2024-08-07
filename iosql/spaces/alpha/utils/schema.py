import re


def _raise_if_not(flag, schema, uri):
    if not flag:
        raise ValueError(f'{schema}: BadSchemaURI: {uri}')


def _parse_user_pass(user_pass):
    # [@2021-11-24 21:04:51] cc
    _33 = user_pass.split(':')
    user = _33[0]
    if len(_33) >= 2:
        password = _33[1]
    else:
        password = None
    return user, password


def _parse_host_port_db(host_port_db):
    # localhost:6379/astock
    # localhost:6379/0
    # [@2021-11-24 21:05:27] cc
    host, port, db = None, None, None
    print('xxx', host_port_db)
    o = re.match(r'(.*?):(\d+)/(.*?)$', host_port_db)
    if o:
        host = o.group(1)
        port = int(o.group(2))
        db = o.group(3)
    return host, port, db


def parse_db_schema(uri):
    # sqlite3:///home/lsl/database/astock
    # mysql://root:password@localhost:6379/astock
    # redis://:password@localhost:6379/0
    # redis://localhost:6379/0
    uri_version = uri.strip().split('?')
    if len(uri_version) == 1:
        uri = uri_version[0]
        version = '1'
    else:
        uri, version = uri.strip().split('?')
        version = version.split('=')[1]

    uri = uri.strip().rstrip('/')
    schema, options = None, {}

    options['version'] = version

    parts = uri.strip().split('://')
    _raise_if_not(len(parts) == 2, schema, uri)
    schema, detail = parts

    if schema in ['mysql']:
        _raise_if_not('@' in detail, schema, uri)
        parts22 = detail.split('@')
        _raise_if_not(len(parts22) == 2, schema, uri)
        user_pass, host_port_db = parts22
        options['user'], options['password'] = _parse_user_pass(user_pass)
        options['host'], options['port'], options['db'] = _parse_host_port_db(host_port_db)
    elif schema in ['redis']:
        # redis://:password@localhost:6379/0
        # redis://localhost:6379/0
        if '@' in detail:
            parts22 = detail.split('@')
            _raise_if_not(len(parts22) == 2, schema, uri)
            user_pass, host_port_db = parts22
        else:
            user_pass, host_port_db = None, detail

        if user_pass:
            _, options['password'] = _parse_user_pass(user_pass)
        else:
            options['password'] = None

        options['host'], options['port'], db = _parse_host_port_db(host_port_db)
        options['db'] = int(db)
    elif schema in ['sqlite', 'sqlite3']:
        # sqlite3:///home/lsl/database/astock
        # sqlite3://temp/astock
        # sqlite3://astock
        # sqlite3://:memory
        if detail == ':memory':
            options['memory'] = True
        else:
            options['memory'] = False
            pos = detail.rfind('/')
            if pos == -1:
                path, db = '', detail
            else:
                path, db = detail[:pos], detail[pos+1:]
            options['path'] = path
            options['db'] = db

    return schema, options


if __name__ == '__main__':
    if 1:
        # 
        uri = 'redis://:password@localhost:6379/0'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)

        uri = 'redis://localhost:6379/0'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)
    if 0:
        # pass
        uri = 'mysql://user:password@localhost:6379/astock'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)
    if 1:
        # pass
        uri = 'sqlite3:///home/lsl/database/astock'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)

        uri = 'sqlite3://./database/astock'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)

        uri = 'sqlite3:///home/lsl/database/astock?version=2'
        schema, options = parse_db_schema(uri)
        print(uri)
        print('r', schema, options)
