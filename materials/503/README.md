# 离线双站容量权利 SDK 与真实 SQLite WarehouseRows

Python 3.11 + 标准库 SQLite。公开入口保留：`rights.Rights`、
`warehouse_rows.WarehouseRows`。新增 `transport.py`（实际 JSON 文件传输）、
`audit.py`（只读事后守恒核对）、`verify.py`（独立验证）、`demo.py`（分阶段证据）。

## 模块

- `rights.py`：每站独立库，`spend/restore/transfer/receive/available/inspect`。
  - 首次打开只在本地事务内发放一次 `initial_grant`；同站同 grant 重开保留余额，
    换站点或换 grant 拒绝。
  - `spend` 只接受正整数；同 usage 同 units 重试不重复消费，异文或已归还拒绝。
  - `restore` 只归还未归还 usage，重复归还无效果，未知 usage 拒绝。
  - `transfer` 在一个本地事务里“扣本地余额 + 持久化出站消息”。消息 ID 为
    `source + ':' + identity`，跨站不碰撞。同 ID 同内容重试返回原消息且不再扣额，
    异文拒绝。只能 A/B 互转、不得转给自己。
  - `receive` 以规范内容（键顺序无关、字段值全参与）去重；同 ID 同文只入账一次，
    同 ID 异文拒绝且无任何状态改变。字段非法、目的错误、非正整数、源=目的均拒绝。
  - 未接收前权利在途：源端已扣不可再花，目的端尚未入账。一旦目的端接受，
    即使源端未知也已不在途。
- `warehouse_rows.py`：真实行表，与本地 Rights 同库同事务。
  - 行身份 `(site,id)`，本地 `id` 非空文本，`value` 为非 bool 的非负 int64；
    SQLite CHECK 兜底。
  - `import_rows(operation_id, rows)`：按数组顺序 create-only；本地已有 id 或
    批内重复 id 整批失败，不做 upsert。每行对应独立未归还的 1 单位 usage。
    消费、全部写入与幂等回执同事务提交；任一验证/CHECK/插入/余额失败全部回滚。
  - `delete(operation_id, identities)`：原子删除现存行并仅归还其 usage；不存在
    id 无效果；重复 id 或重放删除不多退；行删除后新操作 ID 可重建同一 id，
    但已归还 usage 不复用。
  - import/delete 共用 operation_id 命名空间，规范请求含操作种类与完整数组，
    键顺序无关、数组顺序保留；同 ID 同规范请求成功重试无重复效果，异文拒绝，
    失败操作不占用成功回执。输入错误不留部分状态。
- `audit.py`：只读打开两库，按初始 grant、原始消息与成功行操作独立核算。
  - 集成场景：`live_rows + unspent + inflight == K`，并逐一核查未归还 usage
    与存活行一一对应。
  - 独立 SDK 试验（无行表）：以实际未归还 spent units 代替 live_rows，
    明示为 spent units 而非真实行。
  - `inflight` 按逐消息身份计算：源端出站消息若未出现在目的端接收账本，
    其 units 计入在途；不依据源端是否收到 ACK。伪造的接收消息会被审计标出。

## 运行

```
python3 materials/503/verify.py   # 16 项独立检查
python3 materials/503/demo.py     # 两真实库 + 真实 JSON 文件分阶段证据
```

`verify.py` 内的 `LocalQuotaFixture` 只是遵守同一本地事务合同的隔离试验设施，
不是生产模块，也没有跨站转移实现。

## 设计取舍

采用逐转移身份的出站/接收账本（而非按站点对的单调累计量）。代价是保留全部
历史转移身份；好处是乱序、重复、在途核算都可逐消息核对，且重复 ID 异文能精确
拒绝。消息 ID 由源站前缀保证全局不碰撞；人为伪造若绕过前缀约定，审计会以
“目的端接收记录在源端出站账本中不存在”报出。
