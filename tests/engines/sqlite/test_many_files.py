import shutil
import time
import arrow
import logging
import pytest
from iosql import Database

"""
测试数据表非常多时，handler是否能正常关闭 
"""
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')
logging.getLogger("iosql").setLevel(logging.INFO)

def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)


@pytest.mark.skip(reason="Deprecated: old string partition format no longer supported")
def test_many_files():
    cleanup()
    db = Database('sqlite://./database/demo.iosql')
    data = []
    now = arrow.now()
    for i in range(0, 1000):
        date = int(now.shift(seconds=10*i).timestamp()*1000)
        # date = int(1000*d.shift(hours=i).timestamp()) # format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'iii10': i*i*i})
    
    print(data[-1], len(data))
    # print(data) 

    # 5s
    print('insert ...')
    t = time.time()
    use_builtin_partition = True
    db['lrb2'].insert_all(data, pk=['id','date'], 
                         replace=True, 
                         partition='KTStoTSx10(date)', #
                         use_builtin_partition=use_builtin_partition)
    delta_t2 = time.time() - t
    print('Use builtin_partition end insert', delta_t2)


@pytest.mark.skip(reason="Deprecated: old string partition format no longer supported")
def test_multithread():
    cleanup()
    # 多线程容易timeout (默认timeout=5)

    import threading
    print('generating ...')
    data = []
    for i in range(0, 20000):
        d = arrow.now().shift(seconds=10)
        date = int(d.timestamp()*1000) # format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'iii10': i*i*i})

    def thread_insert():
        db = Database('sqlite://./database/demo.iosql')
        # print('current', threading.current_thread())
        # print('insert ...')
        t = time.time()
        use_builtin_partition = True
        db['lrb2'].insert_all(data, pk=['id','date'], 
                              replace=True, partition='KTStoTSx3600(date)', 
                              use_builtin_partition=use_builtin_partition)
        delta_t2 = time.time() - t
        print(f'{threading.current_thread().native_id}: time: {delta_t2}')

    threads = []
    n_threads = 10
    # https://docs.python.org/2/library/sqlite3.html#sqlite3.connect
    for i in range(n_threads):
        threads.append(threading.Thread(target=thread_insert))

    for t in threads:
        # time.sleep(0.1)
        t.start()

    for t in threads:
        # print('waiting ...')
        t.join()



if __name__ == '__main__':
    if 1:
        test_many_files()
    if 0:
        test_multithread()
