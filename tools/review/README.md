# PR 验收证据报告（只读）

`pr_status.py` 按仓库与 PR 号汇总验收证据：精确 base/head SHA、Draft/冲突状态、
绑定当前 head 的 check runs、commit statuses 与 Actions workflow runs、可读取时的
必需检查、Review（区分当前 head 批准与旧提交批准）以及未解决线程。供多 Agent
协作时人工核对，不代替独立审查与合并门禁。

## 边界

- 全程只读：REST 仅 GET；线程计数使用 GraphQL 只读 `query`（工具拒绝 mutation）。
  不调用 approve/merge/rerun/评论，不更改仓库任何状态。
- 仅读取本机 `GITHUB_TOKEN`（公开仓库可匿名）；不输出、记录或提交 Token，
  报告不含 PR/Review 正文、标题等不可信文本，重定向时拒绝转发凭证。
- 读不到分支保护/必需检查配置时标记 `unknown`，绝不猜测；GraphQL 线程读取失败同样标 `unknown`。
- Actions workflow runs 按 `head_sha` 精确查询并逐条核对；403/404（Actions 不可读或未启用）
  记为 `workflow_runs_state=unknown` 并只用 check runs/statuses 判定，其他错误直接停止。
- `merge_authorization` 恒为 `not-assessed`：检查完成不等于获准合并，合并由维护者按真实门禁决定。
- 只观察分支保护的可读取部分，不评估 Rulesets、合并提交 SHA、审查者权限或完整 GitHub 门禁；
  必须在 GitHub 与维护者处另行核验。Workflow runs 包含查询到的历史事件，整体分类是保守
  证据聚合，历史失败可使其持续显示失败，不等同当前 GitHub 门禁结果。
- `commit_statuses` 为按 API 逆序每个 context 取最新的值；`commit_status_history_count`
  是读取的历史事件数。独立读取的 combined status 与这些值不一致、或出现不能安全归一化
  的非 ASCII context 时，`commit_status_snapshot=unknown`，整体及必需项均标 `unknown`。
  API 读取非原子；相同 aggregate 状态不证明明细未改变，PR 本身在收集期间也可能变化。
- check run 展示 `app_id`；必需规则为正 App ID 时，只接受可核验相同来源的 check，
  同名 commit status 的 creator 用户 ID 不可推断为 App ID，因此标 `unknown`。
  `-1` 允许任意来源；缺失/null/仅 legacy context 无法确定选择的 App 时不猜为通过。
  同名 check 和 status 都存在时，两者都须成功；未知 status/conclusion 保守为 `unknown`。
  该分类不模拟 Rulesets 或 GitHub 全部细节。
- 退出码只表示报告是否生成：0 = 已生成（无论检查通过与否），1 = API/输入错误。

## 用法

```bash
python3 tools/review/pr_status.py --repo jyxxe/XeLab --pr 59        # 人类可读
python3 tools/review/pr_status.py --repo jyxxe/XeLab --pr 59 --json # 稳定 JSON（排序键，无本地时间戳）
```

checks 状态：`passed` / `pending` / `failed` / `action-required` / `unknown` / `none-reported`
（无任何 check run、commit status 与 workflow run 时如实报告，不误报通过）。
Workflow run 的 `completed/action_required`（常见于 Fork PR 等待维护者批准运行）同样
归为 `action-required`，不再误报 `none-reported`。必需检查逐项为
`passed/pending/failed/action-required/missing/unknown`；当必需检查尚无结果但存在待批准的
workflow run 时记为 `action-required` 而非 `missing`。

## 样例与测试

`example-report.json` 为离线夹具生成的**合成样例**（example/project，非真实仓库数据），
展示完整 JSON 结构。人工复核方式：对照报告中的 head SHA 打开 PR 页面与检查链接逐项确认。
JSON schema 从 2 升至 3：check run/required 项增加 `app_id`，checks 增加
`commit_status_history_count` 和 `commit_status_snapshot`；`commit_statuses` 从历史列表改为
每个 context 的最新值。调用方须按 schema 版本适配。

```bash
python3 -m unittest discover -s tools/review -p 'test_*.py' -v
```

流程边界见 [Issue 工作流](../../docs/implementation/issue-workflow.md) 与
[标签与自动化边界](../../docs/implementation/issue-automation.md)；贡献规则见 [贡献指南](../../CONTRIBUTING.md)。
