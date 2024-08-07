

    def executemany(self, sql, list_values, commit=True):
        '''SQL简单封装
        # 废弃，插入一般失败，不好处理
        '''
        if self.verbose >= 2:
            logger.info('SQL: %s' % sql)

        if not list_values:
            return

        if batch_size < 1:
            batch_size = 1

        n = len(list_values)
        n_batch = math.ceil(n/float(batch_size))
        for i in range(n_batch):
            if self.verbose:
                logger.debug(f'write batch_size: {i+1}/{n_batch} ..')
            self.cursor.executemany(
                sql, 
                list_values[i*batch_size: min((i+1)*batch_size, n)]
            )

        if commit or self.auto_commit:
            self.commit()
