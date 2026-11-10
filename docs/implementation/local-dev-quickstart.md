# 本地开发 Quickstart（M1–M4）

[实施索引](README.md) · [文档总目录](../README.md) · [Kernel 构建与主机要求](../../crates/kernel/README.md) · [贡献指南](../../CONTRIBUTING.md)

面向新贡献者的短路径：先跑通一组**不需要宿主特权**的检查，再按需要分流到
PostgreSQL、真实 OCI/XFS CI 或正式宿主验收。本页所有命令都引用仓库现存入口；
没有一键启动，也不构成任何验收承诺。

## 0. 三层环境，结论不能互借

| 层 | 环境 | 入口 | 能证明什么 |
|---|---|---|---|
| rootless 本地检查 | 普通用户，无 PostgreSQL/containerd/root | 本页第 2–4 节 | 文档契约自洽、离线单元行为、编译通过 |
| 真实 OCI/XFS CI 组件 | 一次性 Linux 主机，sudo + containerd + PostgreSQL | [kernel.yml](../../.github/workflows/kernel.yml) | 真实容器/网关/恢复组件行为；托管 CI 用合成节点预算 |
| 正式宿主验收 | 独占合规宿主（内核预留 ≥4C16G） | [RUN-03 手册](run03-host.md)、[验收矩阵](acceptance.md) | 生产门槛；由 [#50](https://github.com/jyxxe/XeLab/issues/50) 跟踪，AWS 仍 DEFERRED |

组件测试通过不等于宿主认证；测试已编写不等于已执行。逐项状态以
[M3 持续进度](m3-progress.md) 与目标 [#38](https://github.com/jyxxe/XeLab/issues/38) 的最新证据为准。

## 1. 准备

```bash
git clone https://github.com/jyxxe/XeLab.git
cd XeLab
python3 --version   # 需要 3.11+（文档工具使用 tomllib）
```

- 文档/离线检查只需要 Python 标准库；`tools/issues` 另需
  [PyYAML](../../tools/issues/requirements.txt)。
- Rust 编译需要稳定版 cargo/rustc（CI 使用 ubuntu-24.04 runner 工具链），
  依赖解析由提交的 `Cargo.lock` 固定，命令一律带 `--locked`。
- WebUI/桥组件测试需要 Node.js；真实浏览器测试另需 Playwright/Chromium（见 kernel.yml）。
- 缺少的工具按第 7 节记为“环境不支持”，不是通过。

## 2. 第一组无特权检查（成功/跳过/不支持要分清）

```bash
# 1) 文档契约交叉检查（非 strict，纯标准库）
python3 docs/tools/validate_docs.py

# 2) 宿主验收工具的离线单元行为（不触碰真实宿主）
python3 -m unittest discover -s tools/acceptance -p 'test_*.py' -v

# 3) Issue 表单/标签/信任边界（需要 PyYAML）
python3 -m unittest discover -s tools/issues -p 'test_*.py' -v
python3 tools/issues/validate.py

# 4) 离线生成参考插件清单与契约输入（不是镜像，更不是签名制品）
python3 examples/echo/build_artifact.py \
  --publisher-id 00000000-0000-4000-8000-000000000001 --output /tmp/xelab-quickstart
```

判读结果：

| 现象 | 含义 |
|---|---|
| 命令退出码 0 且输出 PASS/OK | 命令未报告失败；仍须检查依赖与提前返回，确认实际验证范围 |
| 存在忽略、跳过或提前返回的用例 | 对应能力未验证；不能宣称完成 |
| 缺 binary/依赖报错 | 环境不支持；补齐后重跑，或如实记录未执行 |

第 4 步只写出 `/tmp/xelab-quickstart/xelab/plugin.json` 与契约文件；
镜像构建、导入与签名属于第 6 节的特权流程，占位 UUID 不代表受信发布者。

## 3. 严格文档检查（venv）

```bash
python3 -m venv /tmp/xelab-docs
/tmp/xelab-docs/bin/pip install -r docs/tools/requirements.txt
/tmp/xelab-docs/bin/python docs/tools/validate_docs.py --strict
```

strict 额外校验 Schema/OpenAPI/Proto/SQL。无法安装依赖时，非 strict 仍可运行，
但必须把 strict 记为“未执行”，不能写成通过。

## 4. Rust 编译与无特权测试

```bash
cargo build --workspace --locked
cargo test --workspace --all-targets --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo fmt --all -- --check
```

- 未设置 `XELAB_TEST_DATABASE_URL` 时，部分数据库用例会提前返回，Cargo 仍将它们
  计为 `ok`，而非 `ignored`。默认输出会捕获部分用例打印的跳过提示，另有用例
  没有提示。`cargo test ... -- --nocapture` 可显示已有提示，但不能证明所有用例都
  执行了；须记录 DSN 是否设置和实际测试范围，不能据此宣称数据库验证完成。
- 真实 OCI、XFS、浏览器等测试默认 `ignored`，需要显式环境变量与一次性主机（第 6 节），
  本地 `cargo test` 不会执行它们。
- 本机没有 cargo/rustc 时，以上全部记为未执行；不要用文档检查冒充编译证据。

## 5. 需要 PostgreSQL 时

设置 `XELAB_TEST_DATABASE_URL` 后重跑 `cargo test`。DSN 要求：

- 仅 Unix socket 或数字回环地址连接；测试账号须能创建一次性数据库。
- CI 的示例 DSN 使用一次性测试密码（见 kernel.yml 的 postgres service），
  真实凭证不写入仓库、不进命令行历史。
- 数据库测试覆盖 AUTH-01～09、会话撤销、重复迁移、并发准入等；
  通过仍只是组件证据，不是 RUN-03。

## 6. 需要 containerd/cgroup/XFS/root 时

- **CI 组件**：kernel.yml 在一次性主机以 sudo 运行真实 containerd + Rust/Python OCI
  镜像、cgroup、mTLS、XFS 配额与恢复注入；`XELAB_OCI_TEST=1` 等显式开关的测试
  **只允许在可销毁测试主机上手动运行**。托管 CI 容量低于生产预留，使用小的真实
  容器 cgroup 与合成节点预算，不等价于合规宿主验收。
- **生产门槛**：内核预留 ≥4000 mCPU / 16 GiB，系统预留另算；示例配置再预留
  2 CPU / 4 GiB，宿主须高于 6 CPU / 20 GiB 才有插件池。cgroups v2
  （cpu/memory/pids）、独立 containerd namespace、`/sys/fs/cgroup/xelab` 独占、
  独立 IPv4 CIDR、0600 秘密文件等完整清单见
  [Kernel README](../../crates/kernel/README.md) 与 [RUN-03 手册](run03-host.md)。
- 正式 RUN-03/off_host 备份与独立宿主证据由 [#50](https://github.com/jyxxe/XeLab/issues/50)
  跟踪；兼容对象存储不等于正式 off_host。

## 7. 故障定位

| 症状 | 处理 |
|---|---|
| strict 报缺 jsonschema/openapi_spec_validator 等 | 用第 3 节 venv 安装 requirements；否则记录未执行 |
| `tools/issues` 报缺 yaml | 安装 `tools/issues/requirements.txt` 的 PyYAML |
| 未设置 DSN 或不确定数据库测试是否实际执行 | 第 5 节设置 DSN 并重跑；`ok` 和 `--nocapture` 都不能单独证明数据库路径已执行 |
| OCI/XFS 测试被 ignored | 需要一次性特权主机与显式开关；不要在共享宿主尝试 |
| `--locked` 报锁文件漂移 | 不要手改 Cargo.lock；核对基线 commit 与工具链 |
| Fork PR 的 CI 显示 `action_required` | 首次外部贡献需维护者批准工作流运行；批准前按第 2–4 节本地执行并在 PR 记录实际输出，未运行的 CI 不得写成通过 |
| 启动报容量不足 | 生产预留不可降低；换合规宿主或仅跑组件层 |
| 文档校验报不可达页面/断链 | 新页面必须从文档索引可达，链接指向现存文件 |

## 边界

本页是开发入口，不是验收记录：不改变 M2/M3/M4 的验收标准，不授予任何宿主操作权限。
涉及存储/迁移/恢复的验收须分别列出静态/单元、PostgreSQL、OCI/XFS、正式 off_host
与独立宿主证据（见 [Issue 流程](issue-workflow.md)）。密钥、Token 与真实 DSN 不入仓库。
