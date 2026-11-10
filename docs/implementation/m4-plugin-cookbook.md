# M4 插件作者教程：固定依赖、HostService 与事件闭环

[实施索引](README.md) · [依赖规范](../plugins/dependencies.md) · [依赖实现](m4-dependencies.md) ·
[Host 实现](m4-host.md) · [事件实现](m4-events.md) · [参考 fixture](../../examples/m4-consumer/README.md)

> 教程基线：main `44aac50407fb420252023121afde4a2146f6918c`。所有字段取自
> `examples/m4-consumer/` 参考实现、`docs/contracts/proto/xelab/v1/` 与上述实现文档。
> 文末给出本机实际运行证据；依赖特权 OCI 宿主而未运行的命令明确标记。

## 前置条件

- 提供者插件 `org.xelab.echo`（能力 `1.0.0`）已安装、签名并启用，其候选 Skill 已注册；
  消费者镜像已构建、签名并导入，发布者已受信。
- 管理客户端持有效凭证，并被显式授予安装、部署、审批、启用、部署读取和 Operation 读取
  等管理权限，以及能力调用、Skill 读取/调用及精确主题的发布/订阅权限。默认拒绝；
  主题无账户 owner，`owned_by` 不匹配主题。
- 插件实例由内核签发 mTLS 工作负载证书（`/run/xelab/identity`）。插件不持有用户令牌，
  不能自选 URL；Host 端点来自可信运行时环境 `XELAB_HOST_RPC_ENDPOINT`。

## 1. 签名清单声明

以下片段来自 `examples/m4-consumer/build_artifact.py` 生成的真实清单结构：

```json
{
  "dependencies": [{"name": "echo", "required": true, "target_kind": "capability",
    "target": "org.xelab.echo", "version_range": "1.0.0",
    "binding_scope": "deployment", "failure_policy": "wait", "features": []}],
  "requested_rules": [{"required": false, "rule": {"action": "capability.invoke",
    "resource": {"kind": "capability", "match": "all"}}}],
  "event_topics": [{"topic": "org.xelab.m4-consumer.completed", "version": "1.0.0",
    "payload_schema": {"type": "object", "required": ["message"],
      "properties": {"message": {"type": "string", "maxLength": 1024}},
      "additionalProperties": false}}]
}
```

- 代理动作共六个：`capability.discover`、`capability.invoke`、`skill.read`、
  `skill.invoke`、`event.publish`、`event.subscribe`。fixture 全部声明为可选
  （`required=false`），仍须逐条独立审批后才可调用；清单声明只是上限，不是授权。
- 主题必须位于 `plugin_id.` 命名空间；`schema_digest` 由 RFC 8785 规范化字节计算
  （builder 已处理），Schema 摘要与内容不可漂移。
- 能力声明 `side_effect=non_repeatable`：失败或中断的调用可能已产生下游副作用，
  平台与插件都不得自动重放。

## 2. 管理面正向路径

按顺序使用公开 v1 接口（每步写操作都需要 `Idempotency-Key` 头）：

| 步 | 操作 | 关键字段 |
|---|---|---|
| 1 | [installPlugin](../api/operations/plugins/installPlugin.md) `POST /api/platform/v1/installations` | `image`、`publisher_id`、`signature`；等待安装 Operation 成功 |
| 2 | [createDeployment](../api/operations/plugins/createDeployment.md) `POST /api/platform/v1/deployments` | `installation_id`、`display_name`、`limits`、`config={}`、`volume_ids=[]`；`enabled` 默认 false；等待创建 Operation 成功 |
| 3 | [approvePlugin](../api/operations/plugins/approvePlugin.md) `PUT /api/platform/v1/deployments/{deployment_id}/approval` | `rules` 覆盖上述六个代理动作，`egress=[]`；`If-Match` 使用部署当前强 ETag |
| 4 | [setDeploymentEnabled](../api/operations/plugins/setDeploymentEnabled.md) `PUT /api/platform/v1/deployments/{deployment_id}/enabled` | `{"enabled":true}`；重新读取部署的强 ETag 后设置 `If-Match`，等待启用 Operation 成功；依赖绑定失败则拒绝 |
| 5 | [createSubscription](../api/operations/events/createSubscription.md) `POST /api/platform/v1/subscriptions` | `{"topic":"org.xelab.m4-consumer.completed","name":"run-results","start":"latest"}` |
| 6 | [invokeCapability](../api/operations/capabilities/invokeCapability.md) `POST /api/platform/v1/calls` | `contract` 为含 `name`、`version`、`schema_digest` 的对象；`input` 包含 `message`、`skill_id`、`subscription_id` |

步骤 1、2、4 的 `202` 返回的是 Operation，并非安装或部署对象本身。使用原 Operation
UUID 调用 [getOperation](../api/operations/system/getOperation.md)，确认 `state=succeeded`
后再使用 `result` 中的安装或部署 ID；失败或结果未知时先核实原操作，不换键盲目重试。
步骤 3、4 均需从 [getDeployment](../api/operations/plugins/getDeployment.md) 取得最新
部署 ETag，原样作为 `If-Match`；审批会更新部署 revision，因此启用前须重新读取。

第 6 步的 `contract` 从已安装消费者清单中选取 `org.xelab.m4-consumer.run` 的完整
`ContractRef`（包括 `schema_digest`），不是只发送名称和版本。`skill_id` 是已注册 echo 候选 Skill 的不可变 UUID（见
[listSkills](../api/operations/skills/listSkills.md)），`subscription_id` 用第 5 步新建的
订阅。[resolveDependencies](../api/operations/plugins/resolveDependencies.md) 的计划只是
预览，不能作为后续执行的授权票据。

## 3. 插件内 Host 调用闭环

参考实现：`examples/m4-consumer/server.py` 的 `execute()`。每个子调用新建
`RpcContext`（`common.proto`）：`request_id` 新 UUID、`delegation_token` 取父调用、
`deadline_unix_ms` 不超父期限、`idempotency_key` 绑定父 Call（fixture 用
`call_id + ":" + phase`）、`actor_instance_id` 为实际实例。正向顺序：

1. `Resolve(ResolveRequest{context, contract, binding_scope_id=deployment_id})` →
   `ResolveResponse{binding_id, provider_deployment_id, contract, expires_unix_ms}`。
   校验契约相等、期限不超过父调用、未过期；绑定是不透明句柄，不含内网地址。
2. `Invoke(InvokeRequest{context, call_id=新UUID, contract, input_json, input_digest,
   binding_id})` → 校验返回 `call_id` 与输出摘要。
3. `ResolveSkill{skill_id}` → 校验 `definition_json.target` 固定为已批 echo 契约；
   `CheckSkillAccess{skill_id, expected_definition_digest}` 防定义漂移；
   `InvokeSkill{skill_id, input_json, input_digest}`。
4. `PublishEvent{topic, version, payload_json, dedupe_key}` → `{event_id, duplicate}`。
   同键同载荷重发返回相同 `event_id` 且 `duplicate=true`（24h 去重窗口）。
5. `Subscribe{subscription_id}` → 流式 `EventDelivery{delivery_id, event_id, topic,
   version, payload_json, attempt, lease_token, lease_expires_unix_ms}`；校验交付的正是
   刚发布的事件；租约 30s，每个订阅最多一个未确认交付。
6. `AckEvent{subscription_id, delivery_id, lease_token, outcome=OUTCOME_ACK}` →
   `Ack.accepted`。同一已确认租约重复 ACK 仍成功；读完务必 `cancel()` 流。

## 4. 负向路径

### 4.1 未审批的 Host 代理权限

可选规则未获批准时，对应 Host RPC 返回 `PERMISSION_DENIED`；必需代理请求未完整
批准时部署不能启用。正确处理：把失败 surfaced 给调用方并停止该动作；不换键重试、
不尝试其他身份。原授权来源撤销后，新增同权授权不能复活原审批或原调用。
依据：[m4-host.md](m4-host.md) 授权与来源；内核把 403 映射为 gRPC `PERMISSION_DENIED`。

### 4.2 过期或旧 lease 的 ACK

租约过期、或消息已被重投接管后，用旧 `delivery_id`/`lease_token` 调 `AckEvent` 得到
`FAILED_PRECONDITION`（公共 HTTP 侧为 `409 STATE_CONFLICT`，见
[ackDelivery](../api/operations/events/ackDelivery.md)）。重投生成**新** delivery_id 与
**新** lease_token，`attempt` 加一（内核测试断言了令牌轮换）。正确处理：绝不重试旧
令牌；等待重投并按 `event_id` 或自身业务幂等键去重，防止重复副作用。依据：
`crates/kernel/src/host_events_tests.rs` 旧租约断言、`crates/kernel/src/events.rs` 的 ack。

### 4.3 同 dedupe_key 不同载荷

同 `(publisher, topic, version, dedupe_key)` 发布不同内容返回 `FAILED_PRECONDITION`
（HTTP `409`），不静默覆盖。正确处理：作为业务冲突 surfaced；去重键必须绑定稳定的
业务意图，而不是每次重试新生成。

## 5. UNKNOWN、撤权与结果核实

- `non_repeatable` 调用失败或中断后结果可能是 UNKNOWN：子调用、发布或确认可能已
  提交。用原 Call UUID 经 [getCall](../api/operations/capabilities/getCall.md) 或
  `QueryResult`（须原业务键）核实持久记录；不得换新键自动重放。
- fixture 的 `xelab-m4-fixture-diag-bin` 诊断元数据是该签名参考镜像的私有协议，
  **不是公开 SDK**；生产客户端与 HostService 不消费它。
- 撤权实时作用于完整祖先链：后续发现、调用及排队中的流数据交付都被拒绝；
  恢复来源不复活旧委托，旧句柄不能跨进程当持久授权。

## 6. 运行证据与边界

| 命令 | 状态 | 证据 |
|---|---|---|
| `python3 examples/m4-consumer/build_artifact.py --publisher-id <uuid> --output <dir>` | 本机已运行 | 生成 `xelab/plugin.json`、`dependency-contract.json`、`contracts/` |
| `python3 examples/m4-consumer/test_fixture.py`（需 grpcio-tools、jsonschema） | 本机已运行 | 16 tests OK |
| `python3 docs/tools/validate_docs.py --strict` | 本机已运行 | PASS |
| `docker build -f examples/m4-consumer/Dockerfile -t xelab-m4-consumer .` | 未运行 | 需 OCI 构建与签名导入宿主 |
| `XELAB_M4_OCI_REQUIRED=1` OCI 生命周期验收 | 未运行 | 需特权宿主与不可变测试镜像；见 #50 |
| `cargo test` 系列 | 未运行 | 本机无 Rust 工具链 |

fixture 测试生成真实 protobuf 绑定并校验 Schema 与模拟协议响应；不证明 OCI 执行、
mTLS 网络身份、cgroup 隔离或生产宿主验收。AWS 继续 DEFERRED；正式生产宿主验收由
[#50](https://github.com/jyxxe/XeLab/issues/50) 独立跟踪。
