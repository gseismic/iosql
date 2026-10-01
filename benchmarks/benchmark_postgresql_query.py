#!/usr/bin/env python3
"""百万/千万行 PostgreSQL 查询基准；随机表名、逐规模清理，只测读取与计划。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402
from iosql import Database, Partition  # noqa: E402


BASE = 1735689600000
DAY = 86_400_000
COLUMNS = {'id': 'integer', 'ts': 'integer', 'symbol': 'text',
           'price': 'float', 'volume': 'float', 'venue': 'text'}


def contract(partitioned):
    """主键按时间/id 排列，使 symbol/time 索引实验与主键索引独立。"""
    return dict(pk=['ts', 'id'], columns=COLUMNS, time='ts', symbol_column='symbol',
                partition=Partition.duration('ts', every='1day') if partitioned else None)


def rss_mb():
    # Linux 实时 RSS；独立工作进程避免前序 to_list 的高水位污染。
    pages = int(Path('/proc/self/statm').read_text().split()[1])
    return pages * os.sysconf('SC_PAGE_SIZE') / 1024**2


def stream_worker(name, partitioned, batch_size):
    with Database(os.environ['POSTGRES_DSN']) as db:
        table = db.table(name, **contract(partitioned))
        baseline = rss_mb()
        peak = baseline
        started = time.perf_counter()
        first = None
        count = 0
        previous = None
        # 全量排序只投影三列，吞吐包含解码、dict 构造和调用方遍历。
        for batch in table.query(columns=['id', 'ts', 'price'], order_by='ts').iter_batches(batch_size):
            if first is None:
                first = time.perf_counter() - started
            peak = max(peak, rss_mb())
            for row in batch:
                if previous is not None and row['ts'] < previous:
                    raise AssertionError('全局排序失败')
                previous = row['ts']
            count += len(batch)
        elapsed = time.perf_counter() - started
        peak = max(peak, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        return {'rows': count, 'seconds': elapsed, 'first_batch_seconds': first,
                'rows_per_second': count / elapsed, 'batch_size': batch_size,
                'baseline_rss_mb': baseline, 'peak_rss_mb': peak,
                'rss_delta_mb': peak - baseline}


def measure(query, repeats, expected_rows):
    """预热一次后计时，检查结果数，保存实际计划而非断言必须使用索引。"""
    assert len(query.to_list()) == expected_rows
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        rows = query.to_list()
        timings.append(time.perf_counter() - started)
        assert len(rows) == expected_rows
    plan = query.explain(analyze=True)
    return {'seconds': statistics.median(timings), 'returned_rows': expected_rows,
            'plan': plan.to_dict()}


def prepare(db, n, names):
    step = max(1, 32 * DAY // max(1, n // 1000))
    flat = db.table(names[0], **contract(False))
    partitioned = db.table(names[1], **contract(True))
    with flat.connect().cursor() as cur:
        # 在服务端生成确定性行情，避免 Python 装载千万行影响后续内存测量。
        cur.execute(sql.SQL(
            "INSERT INTO {} SELECT i, %s+(i/1000)*%s, 'S'||(i%%1000)::text, "
            "(i%%100)::float8/10, (i%%500)::float8, 'X' "
            "FROM generate_series(0::bigint, %s::bigint-1) AS i"
        ).format(sql.Identifier(names[0])), (BASE, step, n))
    last = BASE + ((n - 1) // 1000) * step
    with partitioned.connect().cursor() as cur:
        for lower in range(BASE, last + 1, DAY):
            name = partitioned._partition_table_name(lower)
            cur.execute(sql.SQL('CREATE TABLE {} PARTITION OF {} FOR VALUES FROM (%s) TO (%s)').format(
                sql.Identifier(name), sql.Identifier(names[1])), (lower, lower + DAY))
        cur.execute(sql.SQL('INSERT INTO {} SELECT * FROM {}').format(
            sql.Identifier(names[1]), sql.Identifier(names[0])))
    for table in (flat, partitioned):
        with table.connect().cursor() as cur:
            cur.execute(sql.SQL('ANALYZE {}').format(sql.Identifier(table.table_name)))
        assert table.count_rows() == n
    return flat, partitioned, step


def workload(table, n, step, repeats):
    middle = n // 2000
    start = BASE + middle * step
    end = start + 9 * step
    kwargs = dict(start=start, end=end, columns=['id', 'ts', 'price'])
    queries = {
        'symbol_window': (table.query(symbols='S7', **kwargs), 10),
        'symbol_window_ordered': (table.query(symbols='S7', order_by='ts', **kwargs), 10),
        'all_symbols_window': (table.query(**kwargs), 10_000),
        'symbol_only': (table.query(symbols='S7', columns=['id', 'ts', 'price']), n // 1000),
        'latest_small': (table.latest(symbols=['S7', 'S9', 'S11'], columns=['symbol', 'ts', 'price']), 3),
        'asof_small': (table.asof(symbols=['S7', 'S9', 'S11'], as_of=end,
                                 columns=['symbol', 'ts', 'price']), 3),
        'latest_large': (table.latest(symbols=[f'S{i}' for i in range(1000)],
                                     columns=['symbol', 'ts', 'price']), 1000),
        'asof_large': (table.asof(symbols=[f'S{i}' for i in range(1000)], as_of=end,
                                 by=['symbol', 'venue'], columns=['symbol', 'venue', 'ts']), 1000),
    }
    result = {name: measure(query, repeats, expected) for name, (query, expected) in queries.items()}
    # 验证 latest 实际返回该截止时点，不只检查行数。
    assert all(row['ts'] == end for row in queries['asof_small'][0].to_list())
    started = time.perf_counter()
    count = sum(len(batch) for batch in table.query().iter_batches(batch_size=10_000))
    assert count == n
    result['full_scan'] = {'returned_rows': count, 'seconds': time.perf_counter() - started}
    result['ordered_stream'] = []
    for batch_size in (1000, 10_000):
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--stream-worker', table.table_name,
             '--partitioned-worker', str(bool(table.partition)).lower(), '--batch-size', str(batch_size)],
            capture_output=True, text=True, check=True,
        )
        metrics = json.loads(completed.stdout)
        assert metrics['rows'] == n
        result['ordered_stream'].append(metrics)
    return result


def environment(dsn):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT version(), current_setting(%s), current_setting(%s), current_setting(%s)',
                    ('shared_buffers', 'work_mem', 'enable_partition_pruning'))
        pg, buffers, work_mem, pruning = cur.fetchone()
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith('model name')), 'unknown')
    return {'postgresql': pg, 'python': platform.python_version(), 'platform': platform.platform(),
            'cpu': cpu, 'cpu_count': os.cpu_count(),
            'memory_gb': os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / 1024**3,
            'shared_buffers': buffers, 'work_mem': work_mem, 'enable_partition_pruning': pruning,
            'cache': 'ANALYZE 后预热一次，重复热/温缓存；流式任务按报告顺序执行',
            'storage': '由运行环境提供的 PostgreSQL 数据目录；本次报告补充实际介质'}


def write_report(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['# PostgreSQL 时序查询基准', '', f'- 日期：{report["date"]}',
             '- 六列：id/ts BIGINT、symbol/venue TEXT、price/volume DOUBLE PRECISION；主键 (ts,id)。',
             '- 1000 个 symbol，约 32 天；可选索引 (symbol,ts)，单次时间窗返回 10 个时间点。',
             f'- 小查询预热后重复 {report["repeats"]} 次，表中为中位数；完整计划见同名 JSON。', '',
             '| 行数 | 布局 | 复合索引 | symbol 时间窗 ms | 时间窗全部 symbol ms | 仅 symbol ms | latest 3/1000 symbol ms | asof 3/1000 symbol ms | 窗口扫描分区/候选 |',
             '|---:|---|---|---:|---:|---:|---:|---:|---|']
    for item in report['results']:
        q = item['queries']
        def ms(name):
            return f'{q[name]["seconds"] * 1000:.2f}'
        plan = q['symbol_window']['plan']
        lines.append(f'| {item["rows"]:,} | {item["layout"]} | {item["index"]} | {ms("symbol_window")} | '
                     f'{ms("all_symbols_window")} | {ms("symbol_only")} | {ms("latest_small")}/{ms("latest_large")} | '
                     f'{ms("asof_small")}/{ms("asof_large")} | {len(plan["scanned_partitions"])}/{len(plan["candidate_partitions"])} |')
    lines += ['', '## 流式排序与内存', '',
              '| 行数 | 布局 | 索引 | 批次 | 首批 ms | 总秒数 | 行/秒 | RSS 增量 MB | 全列扫描秒 |',
              '|---:|---|---|---:|---:|---:|---:|---:|---:|']
    for item in report['results']:
        for stream in item['queries']['ordered_stream']:
            lines.append(f'| {item["rows"]:,} | {item["layout"]} | {item["index"]} | {stream["batch_size"]} | '
                         f'{stream["first_batch_seconds"]*1000:.2f} | {stream["seconds"]:.2f} | '
                         f'{stream["rows_per_second"]:,.0f} | {stream["rss_delta_mb"]:.2f} | '
                         f'{item["queries"]["full_scan"]["seconds"]:.2f} |')
    lines += ['', '## 环境', '', '```json', json.dumps(report['environment'], ensure_ascii=False, indent=2),
              '```', '', '## 测量边界', '',
              '- 客户端 RSS 由每次独立工作进程逐批读取 /proc/self/statm，并用 ru_maxrss 覆盖瞬时峰值；包含启动高水位，不计 PostgreSQL 服务端排序/缓存内存。',
              '- 首批时间包含命名游标声明、规划、排序和首次 FETCH；大范围排序可能在首批之前处理大量数据。',
              '- 基准在服务端生成数据，不包含写入性能；所有表随机命名，每个规模结束即删除。',
              '- 计划反映当前版本/统计/缓存。顺序扫描可合理胜过索引，索引/分区效果必须按负载解读。',
              '- 指定 symbol 且有 symbol/time 前缀索引时，单分组 latest 使用 LATERAL LIMIT 1；其他分组使用 DISTINCT ON。',
              '- 多维分组、无索引及跨组历史扫描仍可能较慢，应结合实际计划确定索引。']
    output.with_suffix('.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', default='1000000,10000000')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'benchmarks/results/postgresql-query-20261002.json')
    parser.add_argument('--stream-worker')
    parser.add_argument('--partitioned-worker', default='false')
    parser.add_argument('--batch-size', type=int, default=10_000)
    args = parser.parse_args()
    if args.stream_worker:
        print(json.dumps(stream_worker(args.stream_worker, args.partitioned_worker == 'true', args.batch_size)))
        return
    if not os.environ.get('POSTGRES_DSN'):
        parser.error('请设置专用测试数据库 POSTGRES_DSN')
    sizes = [int(value) for value in args.rows.split(',')]
    if args.repeats < 1 or any(n < 20_000 or n % 2000 for n in sizes):
        parser.error('rows 必须是 >=20000 的 2000 倍数，repeats 必须为正数')
    dsn = os.environ['POSTGRES_DSN']
    report = {'date': datetime.now().astimezone().isoformat(), 'environment': environment(dsn),
              'schema': COLUMNS, 'repeats': args.repeats, 'results': []}
    for n in sizes:
        names = ['iosql_bench078_' + uuid.uuid4().hex[:16] for _ in range(2)]
        try:
            with Database(dsn) as db:
                print(f'准备 {n:,} 行', flush=True)
                flat, partitioned, step = prepare(db, n, names)
                for indexed in (False, True):
                    for table in (flat, partitioned):
                        if indexed:
                            table.create_index(columns=['symbol', 'ts'])
                        with table.connect().cursor() as cur:
                            cur.execute(sql.SQL('SELECT avg(pg_column_size(sample)) FROM '
                                                '(SELECT * FROM {} LIMIT 1000) sample').format(
                                sql.Identifier(table.table_name)))
                            width = float(cur.fetchone()[0])
                            if table.partition:
                                cur.execute('SELECT sum(pg_total_relation_size(relid)) '
                                            'FROM pg_partition_tree(%s) WHERE isleaf', (table.table_name,))
                            else:
                                cur.execute('SELECT pg_total_relation_size(%s)', (table.table_name,))
                            size_bytes = int(cur.fetchone()[0])
                        layout = 'partitioned' if table.partition else 'flat'
                        print(f'测量 {n:,} {layout} index={indexed}', flush=True)
                        queries = workload(table, n, step, args.repeats)
                        report['results'].append({'rows': n, 'layout': layout, 'index': indexed,
                                                  'partition_count': len(table.table_parts()),
                                                  'sample_row_bytes': width, 'relation_bytes': size_bytes,
                                                  'step_ms': step, 'queries': queries})
                        write_report(args.output, report)
        finally:
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                for name in names:
                    cur.execute(sql.SQL('DROP TABLE IF EXISTS {} CASCADE').format(sql.Identifier(name)))
    print(f'结果：{args.output}', flush=True)


if __name__ == '__main__':
    main()
