# 幂等事件消费者参考示例

[事件语义](../../docs/tasks/events.md) · [M4 实现设计](../../docs/implementation/m4-events.md) · [投递契约](../../docs/api/operations/events/streamDeliveries.md) · [确认契约](../../docs/api/operations/events/ackDelivery.md)

对应 Issue #68。stdlib-only 离线参考实现，演示平台事件契约下消费者侧的正确
姿势：**按 event_id 去重、业务结果与确认意图先同事务提交、再发送 ACK**。
不修改 Kernel，不依赖特权生产环境，不发起网络请求。

> 本示例不承诺端到端 exactly-once。平台契约是至少一次投递；本示例通过本地
> 持久化去重保证「同一 event_id 的业务副作用至多提交一次」，并诚实暴露
> 「副作用已提交但 ACK 结果未知」的窗口，由重投 + 去重收敛。

## 运行

```sh
# 离线契约测试
python3 -m unittest discover -s examples/event-consumer -p 'test_*.py' -v

# 场景演示（6 个场景，打印逐场景 PASS/FAIL）
python3 examples/event-consumer/drive.py
```

要求 Python 3.10+，仅标准库。测试与演示均使用临时目录，无残留状态。

## 文件

| 文件 | 作用 |
|---|---|
| `consumer.py` | 参考消费者 + SQLite 持久化（processed / side_effects / ack_journal） |
| `fake_service.py` | 离线契约模拟：租约、重投、ACK 幂等、退避、死信；**不是内核** |
| `drive.py` | 六个验收场景的离线演示驱动 |
| `test_event_consumer.py` | 契约与安全性测试 |

## 协议子集与故障语义

模拟与消费者共同覆盖的契约行为（与 docs/api、docs/tasks/events.md 一致）：

- 每个订阅最多一个未确认 delivery，租约 30s；过期后按从租约到期时刻起算的
  退避等待重投，生成**新** delivery_id / lease_token，attempt 加一。
- 同一已 ack 租约重复确认返回 204；过期租约即使未被接管也返回 409。
- `Idempotency-Key`：同键同请求返回已记录结果；同键不同内容返回
  `409 IDEMPOTENCY_CONFLICT`。传输超时只允许用**原键**重试。
- retry 按 1/2/4/8/16/32s 指数退避、上限 60s（模拟默认零抖动以便确定性
  测试；真实平台加 ≤25% 抖动）；达到 maximum_attempts 或显式 dead_letter
  写死信并推进游标。

消费者侧决策：

| 情况 | 行为 |
|---|---|
| 首投 | 业务副作用 + processed 标记 + prepared 确认意图**同事务**提交，然后 ACK |
| 相同 event_id 重投 | 不重复副作用，直接对新 delivery 确认 |
| ACK 204 | 日志记 confirmed |
| ACK 超时（结果未知） | 仅以原键重试一次；仍未知则保留 prepared，等重投去重收敛 |
| 409 STATE_CONFLICT | 记 state_conflict；**绝不**用旧令牌重试，先核对订阅与投递状态；订阅仍活动时才等待重投 |
| 409 IDEMPOTENCY_CONFLICT | 记 conflict 并抛出，交人工核查，不自动重放 |
| 401/403（失权） | 记 rejected 并抛出 NotAuthorized，停止消费 |
| 主题不在允许列表 | dead_letter，不产生副作用 |

## 持久化与并发假设

- 单进程、单订阅消费者。SQLite 文件以 0600 创建；`with connection` 事务
  保证副作用、去重标记、确认意图三者原子提交。多消费者实例并行同一订阅
  不在本示例范围内（平台本身也只允许一个未确认租约）。
- lease_token 是秘密：只保存在内存，不落盘、不写入报告或日志。进程崩溃
  后令牌丢失没有关系——租约到期后平台重投，消费者用 event_id 去重。
- prepared 确认意图**不会**在重启后自动重放：重启只恢复去重状态，未确认
  的消息由平台重投驱动收敛，避免旧 delivery/旧键推进新游标。
- `STATE_CONFLICT` 既可能是旧租约，也可能是订阅已停用；仅凭错误码无法区分。
  消费循环应暂停并核对订阅，不能直接假定一定会重投。
- 业务副作用在本示例中是本地 ledger 行（side_effects 表，event_id 唯一）。
  真实消费者应把此处替换为自己的业务事务，并保持「先提交、后 ACK」顺序。

## 真实平台与模拟的边界

`fake_service.py` 只模拟消费者依赖的契约行为，用于离线测试与演示；它不是
内核实现，也不覆盖：SSE 传输、权限对象/祖先链校验、主题授权、发布去重、
保留期与配额、PostgreSQL 持久化。真实语义以 docs/tasks/events.md、
docs/implementation/m4-events.md 与 OpenAPI 契约为准；端到端执行证据由
M4 阶段验收另行补齐。

## 安全

- 载荷与 lease_token 不写入 SQLite、报告或日志（有测试断言）。
- 存储文件权限 0600；确认键为随机 UUID，不含业务数据。
- 演示 payload 中的 `secret_field` 仅用于「不落盘」断言，无真实秘密。
