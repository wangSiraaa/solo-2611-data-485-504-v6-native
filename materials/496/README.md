# 可写块视图

`quota.py` 是独立容量账户，其他本地客户也可持有预约或分配；`volume.py` 使用它管理真实的16字节块文件及可写clone。当前普通创建、单块COW和删除可运行。

Python 3.11，无必装第三方包。在本目录执行 `python3 demo.py`。
`Volume` 的directory必须不存在；演示在临时目录中创建它。
故障回调收到 `after_block_written, ordinal`，ordinal从1开始，表示本次write的第几个受影响块已做真实写入。回调可抛出OSError；普通文件的后续恢复I/O仍可使用。

`layout()`仅返回拷贝，`Quota.stats()`返回capacity/used/held/free，`allocated`为当前已分配ID集合。验收会将它们与实际块文件、每个view的完整字节及独立预约客户一起核对。无需保持当前私有数据结构或UUID生成方式。

