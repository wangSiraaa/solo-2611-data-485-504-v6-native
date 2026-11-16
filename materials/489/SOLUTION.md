# CSV 外部表事务方案说明

## 交付文件

- `participant.py` — `TableTransactions`，直接（非 SQL）维护客户端与 SQL 适配器共用的事务参与者。
- `virtual.py` — `CsvModule` / `CsvTable`，真实的 APSW 可写虚表（`create_module(..., iVersion=2)`，启用 xSavepoint/xRollbackTo/xRelease）。
- `verify.py` — 验证套件（定向场景 + 与原生 SQLite 表的随机差分测试）。
- `probe.py` / `conflict_probe.py` — 原有观察脚本，现全部通过。

## 私有修改、保存点、发布边界如何划分

**三层数据，自下而上：**

1. **已提交 CSV 文件**：只在 `commit()` 内、且 `prepare()` 成功之后，通过
   临时文件 + `os.replace` 改写一次。事务进行中的任何语句失败、保存点回退、
   外层 ROLLBACK 都不会触碰该文件，因此重新打开文件永远只能看到此前提交版本。
2. **私有快照 `participant.private`**：`begin()` 时从磁盘复制一份独立 dict，
   `put/remove` 只改它。`rows()` 在事务内返回私有行，事务外返回磁盘行。
3. **保存点快照栈**：每个保存点保存一份**独立的 dict 副本**（修复了原实现
   `self.marks[number] = self.private` 的别名 bug——旧代码回退后再修改会反向
   污染已保存状态）。回滚到某层 = 用该层快照替换私有 dict 并丢弃更年轻层；
   release 只删标记不动外层数据；层号释放后可复用，因为新保存的是全新副本。

直接客户端的 `prepare()` 只做限额校验（value 之和 ≤ `total_limit`），不写盘；
拒绝时事务保持活跃、文件不变，可继续修改或 `rollback()`。

## SQL 虚表如何接入 SQLite 的真实事务结果

没有自写 SQL 解释器。SQLite 负责解析、普通表约束和全部事务/保存点通知，
虚表只实现真实回调：`Begin/Sync/Commit/Rollback` 与
`Savepoint/Release/RollbackTo`，以及三个 xUpdate 行回调。

调研 SQLite 3.54.1 源码后确认了两个非显而易见的契约，它们决定了实现方式：

1. **虚表不享受主键约束，也不享受语句日志（statement journal）。**
   `sqlite3VtabSavepoint` 用 `pVTab->iSavepoint > iSavepoint` 过滤回调：语句
   保存点只会收到 xSavepoint，其 xRollbackTo/xRelease 被吞掉（普通表靠 pager
   日志撤销，虚表必须自己撤销自己的 xUpdate）。因此：
   - 重复 id 语义在 xUpdate 内自行实现，用
     `connection.vtab_on_conflict()` 读取当前冲突模式：
     IGNORE 返回 `apsw.SQLITE_IGNORE` 只跳过该行；REPLACE 就地替换（id 搬移时
     先删除被顶替行）；ABORT 抛 `apsw.ConstraintError`，并由包装器
     `_aborting_update` 立即把私有行恢复到语句边界快照后再原样抛出。
   - 语句边界用连接 trace API 的 `SQLITE_TRACE_PROFILE` 事件识别（每条语句
     结束时必触发，成功/失败路径都触发，且早于 Python 重新拿到控制权）。
     成功语句只丢弃自己的语句标记；失败语句的恢复已在抛异常时完成。用户
     保存点标记不受影响，所以语句撤销永远不会扩大成事务撤销。纯
     `INSERT ... VALUES` 语句没有虚表游标，trace 信号同样覆盖它。

2. **虚表延迟加入事务。** 首次 xUpdate 时才 xBegin；此前已打开的用户
   SAVEPOINT 不产生任何 xSavepoint 回调，join 时 SQLite 只按当前深度合成一个。
   适配器用 `SQLITE_TRACE_STMT` 事件识别用户 SAVEPOINT 命令（区别于语句保存点），
   在收到合成回调时把缺失的外层用户标记补齐（快照即 join 时的已提交内容），
   从而保证之后的 `ROLLBACK TO 任意层` 都能找到正确快照。

**id 即 rowid**：UPDATE 改 id 时执行“删旧 rowid → 按新 id 插入并携带 value”的
搬移，目标 id 已存在时按冲突模式处理，与原生
`id INTEGER PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0)` 逐行一致。

**xSync 即两阶段提交的 prepare 阶段**：限额超限时把参与者的拒绝映射为
`apsw.ConstraintError`，SQLite 随即走正常的提交失败回滚路径（调用 xRollback），
普通表由 SQLite 自身回滚，虚表丢弃私有快照——文件自始未被写入。

## 为什么语句撤销不会扩大为整个事务撤销

- 语句中止只恢复**语句保存点**那一份快照，用户保存点标记和更早语句的成果
  原样保留；异常重新抛给 SQLite 后，外层事务是否继续完全遵循 SQLite 的真实
  状态（显式 BEGIN 的事务保持打开，可继续合法语句或显式 ROLLBACK）。
- 外层 ROLLBACK 才触发 xRollback，丢弃全部私有状态。
- 随机差分测试中每一步都同时比对：错误类型、`get_autocommit()` 事务状态、
  虚表 SQL 可见行、普通表行，以及（自动提交时）磁盘 CSV 内容。

## 验证

```
python3 verify.py
# 82352 checks passed, 0 failed
```

覆盖：普通提交；直接保存点回退与层号复用/嵌套；SQL 主键搬移与 ABORT/IGNORE/
REPLACE 三种策略；多行语句后行冲突（含 `executemany` 的逐行语句语义）；普通表
CHECK 失败后显式回滚（普通表与 CSV 均还原）；prepare/xSync 限额拒绝且无新 CSV
行；失败后外层事务继续合法操作；延迟 join 下四层保存点；自动提交 SAVEPOINT；
以及 120 条随机种子 × 150 步、与原生表逐语句对照的任意合法操作序列。
可见性分别经直接 `rows()`、SQL `SELECT` 和重新打开文件三路核对。
