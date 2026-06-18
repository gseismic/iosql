import re
from typing import Dict, Tuple, Optional, Union

__all__ = ['parse_db_schema']

def _raise_if_not(flag: bool, schema: Optional[str], uri: str) -> None:
    """Raise ValueError if flag is False.
    
    Args:
        flag: Boolean flag to check
        schema: Database schema name (can be None)
        uri: URI string for error message
    
    Raises:
        ValueError: If flag is False
    """
    if not flag:
        raise ValueError(f'{schema}: BadSchemaURI: {uri}')


def _parse_user_pass(user_pass: str) -> Tuple[str, Optional[str]]:
    """Parse username and password from user:pass format.
    
    Args:
        user_pass: String in format 'user:password' or 'user'
    
    Returns:
        Tuple of (username, password). Password is None if not provided.
    """
    parts = user_pass.split(':', 1)  # Split only on first ':'
    user = parts[0]
    password = parts[1] if len(parts) > 1 else None
    return user, password


def _parse_host_port_db(host_port_db: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Parse host, port, and database from host:port/db format.
    
    Args:
        host_port_db: String in format 'host:port/db'
    
    Returns:
        Tuple of (host, port, db). All can be None if parsing fails.
    """
    # Use more efficient regex pattern
    match = re.match(r'^([^:]+):(\d+)/(.+)$', host_port_db)
    if match:
        host, port_str, db = match.groups()
        return host, int(port_str), db
    return None, None, None


def parse_db_schema(uri: str) -> Tuple[str, Dict[str, Union[str, int, bool, None]]]:
    """Parse database schema URI into schema type and options.
    
    Supported formats:
    - sqlite3:///home/lsl/database/astock
    - mysql://root:password@localhost:6379/astock
    - redis://:password@localhost:6379/0
    - redis://localhost:6379/0
    
    Args:
        uri: Database URI string
    
    Returns:
        Tuple of (schema_type, options_dict)
    
    Raises:
        ValueError: If URI format is invalid
    """
    # Parse version parameter
    if '?' in uri:
        uri, version_part = uri.strip().split('?', 1)
        if '=' in version_part:
            version = version_part.split('=', 1)[1]
        else:
            version = '1'
    else:
        version = '1'
    
    uri = uri.strip().rstrip('/')
    options = {'version': version}
    
    # Split schema and detail parts
    parts = uri.split('://', 1)
    _raise_if_not(len(parts) == 2, None, uri)
    schema, detail = parts

    # Parse based on schema type
    if schema in ('mysql', 'postgresql'):
        if '@' in detail:
            auth_part, host_part = detail.split('@', 1)
            options['user'], options['password'] = _parse_user_pass(auth_part)
        else:
            host_part = detail
            options['user'] = None
            options['password'] = None

        host, port, db = _parse_host_port_db(host_part)
        _raise_if_not(host is not None and port is not None and db is not None, schema, uri)
        options.update({'host': host, 'port': port, 'db': db})

    elif schema == 'redis':
        # Handle both with and without authentication
        if '@' in detail:
            auth_part, host_part = detail.split('@', 1)
            _, password = _parse_user_pass(auth_part)
            options['password'] = password
        else:
            host_part = detail
            options['password'] = None
        
        host, port, db = _parse_host_port_db(host_part)
        _raise_if_not(host is not None and port is not None and db is not None, schema, uri)
        options.update({'host': host, 'port': port, 'db': int(db)})
        
    elif schema in ('sqlite', 'sqlite3'):
        if detail == ':memory':
            options['memory'] = True
        else:
            options['memory'] = False
            # Split path and database name
            last_slash = detail.rfind('/')
            if last_slash == -1:
                options['path'] = ''
                options['db'] = detail
            else:
                options['path'] = detail[:last_slash]
                options['db'] = detail[last_slash + 1:]
    else:
        _raise_if_not(False, schema, uri)  # Unknown schema

    return schema, options


def _test_parse_db_schema() -> None:
    """Test function for parse_db_schema with various URI formats."""
    test_cases = [
        'redis://:password@localhost:6379/0',
        'redis://localhost:6379/0',
        'mysql://user:password@localhost:6379/astock',
        'sqlite3:///home/lsl/database/astock',
        'sqlite3://./database/astock',
        'sqlite3:///home/lsl/database/astock?version=2',
        'sqlite3://:memory',
    ]
    
    for uri in test_cases:
        try:
            schema, options = parse_db_schema(uri)
            print(f'URI: {uri}')
            print(f'Schema: {schema}, Options: {options}')
            print('-' * 50)
        except ValueError as e:
            print(f'Error parsing {uri}: {e}')


if __name__ == '__main__':
    _test_parse_db_schema()
