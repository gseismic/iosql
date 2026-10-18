# PLAN-085 Windows 库锁文件重解析点防护

- 日期：2026-10-18（Asia/Shanghai）。
- 状态：已完成（WORKTREE），Windows 原机验证未执行。
- 范围：修复 `LocalCoordinator.acquire()` 在 Windows 跟随 `.lock` 重解析点导致库级独占屏障失效的问题。
- 工作区：沿用 PLAN-084 尚未提交的改动，保留已有改动；本任务不提交或推送。

## 实施方案

1. 库锁文件统一通过内部打开函数创建描述符。POSIX 延续 `O_NOFOLLOW`、`O_CLOEXEC`；Windows 使用 `CreateFileW(OPEN_ALWAYS, FILE_FLAG_OPEN_REPARSE_POINT)` 打开最终路径本身。
2. Windows 通过 `GetFileInformationByHandleEx(FileAttributeTagInfo)` 检查已打开句柄的属性，拒绝所有 `FILE_ATTRIBUTE_REPARSE_POINT`，不能仅依赖打开前的路径检查。
3. 句柄允许共享读写，禁止共享删除，使现有共享/独占字节锁继续协调，同时阻止持锁期间删除或重命名锁文件。检查通过后转为不可继承的 CRT 描述符，沿用现有释放和描述符登记逻辑。
4. 打开、属性检查、重解析点拒绝、CRT 转换失败均明确失败；转换前释放原生句柄，转换后由描述符拥有句柄，避免泄漏或重复关闭。
5. 增加确定性的 Win32 API 替身测试及 Windows 条件原机测试，覆盖宽字符路径、指针宽度、所有重解析标签、失败清理、协调器拒绝路径及真实锁文件替换防护。

## 步骤与验收

- [x] 实施安全打开并接入库级协调器。
- [x] 补充并运行相关锁、迁移和库协调回归，记录 Windows 原机测试的执行边界。
- [x] 自审错误路径、句柄所有权和锁兼容性，运行静态及差异检查。
- [x] 更新开发索引并生成本计划的 [OUTCOME](PLAN-085-windows-lock-reparse-points-OUTCOME.md)。

## 依据

- [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)：不跟随最终重解析点；未设置 `FILE_SHARE_DELETE` 时不允许删除或重命名访问。
- [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex) 与 [FILE_ATTRIBUTE_TAG_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_attribute_tag_info)：按句柄读取属性和重解析标签。
- [msvcrt.open_osfhandle](https://docs.python.org/3/library/msvcrt.html#msvcrt.open_osfhandle)：转换 CRT 描述符并设置 `O_NOINHERIT`。

## 验证限制

当前主机为 Linux，未发现 Wine；Win32 调用契约由替身测试验证，原机用例仅在 Windows 执行。本次防护针对最终 `.lock` 文件，不能将结果扩大解释为全部父目录的安全保证或断电持久性保证。
