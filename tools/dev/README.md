# 开发宿主与依赖预检（只读）

`preflight.py` 是本地开发环境的先行诊断：真实 OCI/XFS/恢复验收对 root、cgroups v2、
containerd、PostgreSQL 与资源预留有严格前提，本工具在不满足时如实报告，不误报通过。
与 [`tools/acceptance/run03.py`](../acceptance/run03.py) 的 `preflight`（需要 kernel 配置、
面向正式验收）不同，本工具无需配置文件、无需 root，只做存在性与可读性检查。

## 边界

- 只读：PATH 查找、文件读取、stat/access 探测；不创建 cgroup、不启动容器、
  不连接 containerd 或数据库、不修改宿主、不读取环境变量。
- 普通用户可运行；权限不足/路径缺失逐项报告 `unavailable` 或 `unknown`，绝不假 success。
- 平台不提供 affinity 接口时报告资源未知或不可用；非 Linux 内核本身报告为不可用。
- `containerd_socket: detected` 仅表示 socket 存在（未连接），**不代表具备正式执行能力**。
- `resources.limit_scope` 为 `visible-hierarchy-upper-bound`：现有 `effective_*` 字段仅是可见
  cgroup 层级、affinity 和 MemTotal 的上界；cgroup 命名空间可能隐藏更严格的祖先，采样期间
  限制也可能变化，因此这些数值不保证进程实际可用容量。
- 输出不含环境变量秘密、DSN、Token、个人路径或命令参数明文；命令只报告存在性，不回显解析路径。
- `acceptance` 恒为 `not-assessed`：不声称 RUN-03、AWS 或生产认证通过（正式验收由 #50 跟踪）。
- 退出码只表示报告是否生成：0 = 已生成（缺失项见 checks），1 = 输入/意外错误。

## 用法与检查项

```bash
python3 tools/dev/preflight.py          # 文本摘要
python3 tools/dev/preflight.py --json   # 稳定 JSON（排序键，无本地时间戳）
python3 tools/dev/preflight.py --containerd-socket /run/containerd/containerd.sock
```

检查项：Python ≥ 3.11、Linux 内核、root 可用性、cgroup v2 控制器（cpu/memory/pids）、
cgroup 可写性、容器环境标记、containerd socket、资源信息（MemTotal、affinity 及可见祖先
cpu.max/memory.max 推导的上界；成员路径不可见、必要控制文件不可读或扫描中成员变化时，
上界为 `unknown`。根 cgroup 没有控制文件及未启用控制器是正常情况）、
必需命令（ctr/runc/ip/nft）、构建工具（cargo/rustc）、可选 PostgreSQL 工具（psql）。
命令存在性依据 [RUN-03 宿主手册](../../docs/implementation/run03-host.md)；存在不等于可执行该验收。

## 测试

```bash
python3 -m unittest discover -s tools/dev -p 'test_*.py' -v
```

离线夹具覆盖：缺失命令/socket、受限 cgroup、cgroup v1、祖先限额截断、部分资源不可见、
路径穿越拒绝、非 root/非 Linux、脱敏与确定性输出。
