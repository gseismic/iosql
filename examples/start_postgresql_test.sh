#!/usr/bin/env bash
# 在本文件所在目录执行 bash start_postgresql_test.sh。
# 需要本机 PostgreSQL 14+；可用 POSTGRES_BIN 指定其程序目录。
# 启动 127.0.0.1:55478 的无密码测试实例，并在 runtime/ 保留数据与连接配置。
set -euo pipefail
umask 077

example_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
runtime_dir="$example_dir/runtime"
cluster_dir="$runtime_dir/postgresql-test"
data_dir="$cluster_dir/data"

if [[ $(id -u) == 0 ]]; then
    printf '%s\n' '请使用普通用户运行，PostgreSQL 不支持以 root 初始化测试库。' >&2
    exit 1
fi

# POSTGRES_BIN 可指定其他安装位置；本机优先复用已验证的 PostgreSQL 14。
if [[ -n ${POSTGRES_BIN:-} ]]; then
    candidates=("$POSTGRES_BIN")
else
    candidates=(/usr/lib/postgresql/{14,15,16,17,18}/bin)
    if command -v initdb >/dev/null 2>&1; then
        candidates+=("$(dirname -- "$(command -v initdb)")")
    fi
    if command -v pg_config >/dev/null 2>&1; then
        candidates+=("$(pg_config --bindir)")
    fi
fi
postgres_bin=''
for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate/initdb" && -x "$candidate/pg_ctl" && -x "$candidate/psql" ]]; then
        version_text=$("$candidate/initdb" --version)
        if [[ $version_text =~ ([0-9]+)\. && ${BASH_REMATCH[1]} -ge 14 ]]; then
            postgres_bin="$candidate"
            major=${BASH_REMATCH[1]}
            break
        fi
    fi
done
if [[ -z $postgres_bin ]]; then
    printf '%s\n' '未找到 PostgreSQL 14+ 程序，请通过 POSTGRES_BIN 指定包含 initdb/pg_ctl/psql 的目录。' >&2
    exit 1
fi

mkdir -p -- "$cluster_dir"
if [[ -f "$data_dir/PG_VERSION" ]]; then
    if [[ $(cat "$data_dir/PG_VERSION") != "$major" ]]; then
        printf '%s\n' '测试数据目录与程序主版本不符，请用 POSTGRES_BIN 指定匹配的已安装版本。' >&2
        exit 1
    fi
else
    "$postgres_bin/initdb" -D "$data_dir" -A trust -U iosql --no-locale -E UTF8
    # 仅回环 TCP 连接，关闭 Unix socket，避免长仓库路径超过 socket 限制。
    cat >> "$data_dir/postgresql.conf" <<'EOF'
listen_addresses = '127.0.0.1'
port = 55478
unix_socket_directories = ''
EOF
fi

if ! "$postgres_bin/pg_ctl" -D "$data_dir" status >/dev/null 2>&1; then
    "$postgres_bin/pg_ctl" -D "$data_dir" -l "$cluster_dir/server.log" -w start
fi
dsn='postgresql://iosql@127.0.0.1:55478/postgres'
"$postgres_bin/psql" -X "$dsn" -v ON_ERROR_STOP=1 -Atc 'SELECT version()'
# 文件由示例按自身路径读取；其他示例/基准可 source 环境文件。
printf '%s\n' "$dsn" > "$runtime_dir/postgresql-test.dsn"
printf "export POSTGRES_DSN='%s'\n" "$dsn" > "$runtime_dir/postgresql-test.env"
printf '%s\n' '本机测试库已就绪，请加载环境配置后运行 PostgreSQL 示例。'
printf '连接：%s\n' "$dsn"
printf '其他脚本的环境配置：source "%s/postgresql-test.env"\n' "$runtime_dir"
