# 条件写与请求组数据服务

`store.py` 是基于真实 SQLite 的条件数据服务，保留 `read/create/update/delete`
入口；`batch.py` 保留 `batch.run(store, requests)` 入口，按组做原子编排。
无第三方依赖，Python 3.11。

```sh
python3 demo.py      # 演示单条修改与无失败批次的既有路径，并展示失败组语义
python3 verify.py    # 真实 SQLite 文件上的完整核验（含两个连接的同 etag 交错）
```

## 行生存期与版本身份（etag 为什么不能复活）

* 表结构：`records(key TEXT PRIMARY KEY, value INTEGER CHECK(0..int64max),
  ver INTEGER)`，另有单行表 `meta(next_ver)` 与 `props(db_id)`。
* 每次成功的 create/update 都在写事务内对全局单调计数器
  `meta.next_ver` 做一次 `UPDATE ... RETURNING`，取得一个全新的 `ver`。
  etag 是不透明 token `W/"<db_id>-<ver>"`，只编码“某一行的某一个版本”。
* etag 匹配的充要条件是：行存在且当前 `ver` 等于 token 内的版本号。
  因此同值 update 也会换版本、删除后同 key 重建得到新 `ver`、关闭重开后
  旧 token 依旧失效；随机 `db_id` 还让不同数据库文件的 token 天然不可互换。
* 计数器在事务内递增，组回滚会把计数器一并回滚；但被回滚的版本号从未附着
  在任何已提交行上，所以已提交 etag 不受影响。

## 原子条件决定与事务参与

* 单条写：连接加锁后 `BEGIN IMMEDIATE` 取得 SQLite 保留写锁，在同一事务里
  先 `SELECT ver` 判定 NOT_FOUND / PRECONDITION_FAILED，再用单条条件语句
  `UPDATE/DELETE ... WHERE key=? AND ver=?` 完成比较与写入，并在同一事务内
  读回结果行。不存在“先查询、后无条件改行”的窗口；异常时必定 ROLLBACK，
  连接回到 autocommit，可继续独立读写。
* 批次参与：`Store.begin()` 返回调用方持有的 `Txn`（同时持有连接锁直到
  commit/rollback）。组内每个操作以 `txn=` 加入该事务，Store 自己不发
  COMMIT/ROLLBACK，所以组内前缀只对同组后续请求可见，绝不偷偷提交。
* 两个直接客户是两个独立连接、同一个数据库文件：SQLite 的写锁串行化两个
  `BEGIN IMMEDIATE`，持锁方的条件写提交后版本号已变，后到者的条件语句
  匹配 0 行而失败——至多一项成功，且各自拿到的都是自己事务内读回的结果，
  不会被对方随后的写入偷换。

## 批次语义

1. 先做整包外形检查（重复 id、组不连续、未知 op、前向/未知/自依赖、
   引用未在 depends 声明、引用外形错误、body/if_match 缺失、超过 10 组或
   40 请求），任一问题抛 `BAD_BATCH`，此前不发任何数据库语句。
2. 运行时先解析字面值/`{"$result":...}` 引用，再做类型与条件检查；引用取
   该请求结果的快照（批次自己维护的结果表），不是数据库当前行。同组可见
   本组未提交结果，跨组只能见已提交组的结果；引用已回滚/未执行的请求或
   结果上不存在的字段（如 delete 结果的 etag）报 `REFERENCE_FAILED`，
   不补 NULL、不猜下一版本。
3. 每组一个事务：全成功才 COMMIT；任一操作失败则 ROLLBACK，先前成功项标
   `rolled_back`（保留暂定结果）并移出可用结果表，故障项保留具体错误，
   组内后续项标 `not_executed`。依赖失败组成员的组整体标
   `failed_dependency` 且不发数据库命令；无此依赖的后续组照常提交，
   已提交组不会被撤回。响应按原请求顺序返回。

## 核验覆盖（verify.py，全部针对真实 .db 文件）

单条 CRUD 与全部错误码；布尔/浮点/越界值与 key 码点长度；删后重建与
重开后旧 etag 失效；回滚不使已提交 etag 失效；未提交前缀对第二连接不可见；
结果快照在行被后续修改后保持原值；失败组真实库里无 A、依赖组读不到临时
结果、无依赖的 Z 照常提交；跨组失败引用的传递阻断；11 类 BAD_BATCH 且
零写入；8 个独立连接竞争同一 etag 恰有一个成功外加严格双客户交错；
60 个随机合法批次与内存参考后端逐请求比对状态/错误/最终行；100 行规模与
10 组 40 请求上限批次。
