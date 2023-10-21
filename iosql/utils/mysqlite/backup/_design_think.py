

# 循环实现不好
def insert(self, kv, replace=False, ignore=False, commit=None, 
           ensure_table=False, ensure_column=False, **kwargs):
    if not kv: return
    sql, _ = getsqlarg_dict_insert(self._table_name, kv, replace=replace, ignore=ignore)
    done = False
    n_err_createtable = 0
    n_err_nosuchcolumn = 0
    while True:
        #i += 1
        #print('-'*20, i)
        #print(n_err_createtable, n_err_nosuchcolumn)
        if n_err_createtable + n_err_nosuchcolumn > 100000:
            raise TooManyDynamicCreate
        try:
            self.execute(sql, commit=commit)
            done = True
            break
        except NoSuchTable as e:
            n_err_createtable += 1
            if ensure_table:
                if self.verbose >= 1:
                    logger.info(f'Table {self._table_name} Not Exist, try-to-create by Data')
                self.create_table_like(kv, **kwargs)
            else:
                raise e
        except NoSuchColumn as e:
            n_err_nosuchcolumn += 1
            if ensure_column:
                column_name = str(e)
                dic = kv
                type_ = suggest_value_type(dic[column_name])
                self.add_column(column_name, type_)
                if self.verbose >= 1:
                    logger.info(
                        f'{self._table_name}: Added column'
                        f' {column_name} {type_}'
                    )
            else:
                raise e
