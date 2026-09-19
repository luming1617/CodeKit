# Orchestration Contracts

本文件定义 Orchestrator 与各角色之间的最小数据契约。字段可以使用 YAML、JSON 或等价的结构化 Markdown 表达，但不得省略必填信息，也不得记录密钥、Token、Cookie 或完整凭据。

## 编码输入契约

```yaml
input_contract:
  feature_directory: absolute-path
  request:
    summary: string
    acceptance_criteria: [string]
  sources:
    product_requirements:
      locator: string
      revision: string | null
      content_digest: string
    technical_design:
      locator: string
      revision: string | null
      content_digest: string
    release_plan:
      locator: string
      revision: string | null
      content_digest: string
  repository:
    root: absolute-path
    base_sha: git-sha
    branch: string
    preexisting_changes: [repo-relative-path]
    preexisting_snapshot: artifact-reference
  permissions:
    read: [path-or-resource]
    write: [repo-relative-path-or-pattern]
    commands: [allowed-command-category]
    network: allowed | denied | approval-required
    git_commit: allowed | denied | approval-required
    git_push: allowed | denied | approval-required
    deployment: allowed | denied | approval-required
    external_mutations: [explicitly-authorized-action]
    forbidden: [string]
  repository_rules: [absolute-path]
```

`revision` 取不到时必须保留 `content_digest`。权限只记录类别和范围，不复制敏感值。

**控制器冻结形态（`init`）：**`init` 写出的 `<run>/input-contract.json` 中，每份文档的
条目固定为 `locator`（绝对路径）、`revision`（本地文件恒为 `unversioned`）、
`content_digest`、`size`、`last_modified`，非 UTF-8 时另有 `decoding`
（`lossy:<编码>` / `bom-stripped`）。`validate-contract --kind input` 会核对
`locator` 指向的文件真实存在且可读，因此冻结产物自身即可通过校验；引用远程或版本化来源
时，`locator` 允许 `http(s)://`、`s3://`、`gs://`、`rev:`、`git@` 形式的非文件定位符。

文档不是合法 UTF-8 时 `init` 以 `BLOCKED:` 结束并给出检测到的编码（gb18030/big5/
shift_jis/cp1252/utf-16），不会退化成 traceback；只有在人工确认可以接受有损解码后才可用
`init --allow-lossy-decoding` 重开一个 run，该选择必须出现在最终报告里。Git Bash 等
MSYS 环境把 `/c/...` 重写过的路径会被识别并提示改用 Windows 绝对路径。

## Task DAG

```yaml
task_dag:
  plan_version: 1
  goal: string
  requirement_trace:
    - source: product_requirements | technical_design | release_plan | user_request
      requirement: string
      covered_by: [task-id | human-acceptance-id]
  tasks:
    - id: T01
      title: string
      objective: string
      depends_on: [task-id]
      read_context: [repo-relative-path]
      allowed_write_files: [repo-relative-path]
      forbidden_changes: [string]
      implementation_notes: [string]
      acceptance_criteria: [string]
      validation:
        - command: string
          cwd: repo-relative-path
          proves: string
          required: true
      risks: [string]
      human_acceptance_candidates: [human-acceptance-id]
  human_acceptance:
    - id: H01
      requirement: string
      expected_behavior: string
      verification_method: string
      reason_not_automated: string
  overall_validation:
    - command: string
      cwd: repo-relative-path
      proves: string
      required: true
  unresolved: []
```

约束：

- `id` 唯一，依赖必须引用现有任务，图必须无环。
- `allowed_write_files` 必须精确到文件；无法提前确定的新文件可用一个窄目录加明确文件命名规则，但 Orchestrator 必须在派发前收敛为具体文件。
- 所有命令必须来自仓库规范、构建配置、CI 或已验证的工具帮助；不确定时放入 `unresolved`，不得编造。
- 每项验收标准必须映射到至少一个验证证据或人工验收候选。
- `unresolved` 非空时计划不能确认。

**机械校验（控制器 P1-1）：**计划确认前必须执行以下命令；校验失败时不得冻结 `plan_version`：

```bash
<PYTHON> "$SKILL_DIR/scripts/code_impl_controller.py" validate-contract \
  --kind task-dag /path/to/task-dag.json
```

控制器校验内容：
- 任务 `id` 唯一性；
- `depends_on` 引用存在的任务；
- DAG 无环（DFS 着色）；
- `unresolved` 必须为空列表；
- `requirement_trace` 必须为非空列表。

## Task Contract

```yaml
task_contract:
  task_id: T01
  plan_version: 1
  feature_directory: absolute-path
  objective: string
  source_trace: [source-and-requirement]
  depends_on_completed: [task-id]
  dependency_outputs: [string]
  repo_root: absolute-path
  start_head_sha: git-sha
  start_diff_digest: string
  start_diff_snapshot: artifact-reference
  preexisting_changes: [repo-relative-path]
  preexisting_snapshot: artifact-reference
  repository_rules: [absolute-path]
  read_context: [repo-relative-path]
  allowed_write_files: [repo-relative-path]
  forbidden_actions: [string]
  acceptance_criteria: [string]
  validator_commands:
    - command: string
      cwd: repo-relative-path
      proves: string
      required: boolean
  implementer_self_checks: [string]
  permissions: scoped-permission-summary
  retry:
    error_fingerprint: string | null
    retries_used: integer
    retries_remaining: integer
  code_review_findings: []
```

修改范围扩大、权限变化或验收标准变化时，必须生成新的 Task Contract；不要口头追加隐含范围。写入范围的机械扩权只能由控制器的 `expand-scope`（`--task-id` + `--reason`，登记进任务台账）完成；`check-scope` 与 `settle-task` 按 Task Ledger 推导授权集合（任务登记范围 ∪ 已 `PASS` 任务范围），不接受调用方现场声明的文件。

## Implementation Result

```yaml
implementation_result:
  task_id: T01
  status: DONE | NEEDS_SCOPE | NEEDS_CONTEXT | PLAN_ISSUE | BLOCKED
  changed_files: [repo-relative-path]
  change_summary: [string]
  self_checks:
    - command: string
      exit_code: integer | null
      result: PASS | FAIL | NOT_RUN
      evidence: string
  assumptions: [string]
  blocking_items: [string]
```

只有 `DONE` 才进入常规任务验证；其他状态由 Orchestrator 处理。`DONE` 也不代表任务已经 PASS。

## Validation Record

```yaml
validation_record:
  scope: task-id | overall
  snapshot:
    head_sha: git-sha
    diff_digest: string
    changed_files: [repo-relative-path]
  checks:
    - name: string
      command: string
      cwd: repo-relative-path
      started_at: timestamp
      finished_at: timestamp
      exit_code: integer | null
      result: PASS | FAIL | NOT_RUN
      evidence: string
      not_run_reason: string | null
  introduced_failures: [string]
  baseline_failures: [string]
  verdict: PASS | FAIL
```

`NOT_RUN` 不能满足 `required: true` 的验证项。日志过长时保留稳定摘要和原始日志位置，不要只写“tests passed”。

**机械落盘（控制器 KW1-b）：**上面这份记录不能只存在于对话里。每跑完一条验证命令必须
执行一次 `record-validation`，由控制器把它写进 `state.json` 的 `validations`：

```bash
<PYTHON> "$SKILL_DIR/scripts/code_impl_controller.py" record-validation \
  --state "$state_path" --scope task --task-id 'T01' \
  --name 'pytest-T01' --command 'pytest -q tests/test_app.py' --cwd '<workdir>' \
  --exit-code 0 --result PASS --evidence '1 passed in 0.31s'
```

控制器拒绝与自报结论矛盾的记录：`PASS` 必须 `--exit-code 0` 且带证据（内联证据上限
4000 字符，更长日志用 `--evidence-file`）；`FAIL` 必须是非 0 退出码；`NOT_RUN` 不得带
退出码且必须给 `--not-run-reason`。`--scope task` 要求任务已登记且不得省略 `--task-id`，
`--scope overall` 反之。每条记录由控制器自动补 `head_sha` 与 `diff_digest`（当前工作树
指纹），`--cwd` 必须解析到仓库内；调用方无法声明证据属于别的树，这是步骤 16 门禁判断
“证据是否覆盖被交付的 Diff”的基础。

## Retry Budget

重试预算由控制器持有，不靠 Orchestrator 记忆：

```yaml
retry_budget:
  fingerprint_occurrences: integer      # 归一化指纹在 failures 中的出现次数
  max_fingerprint_attempts: 4           # 初始 1 次 + 3 次重试
  task_attempts: integer                # dispatch-task 累加的派发次数
  max_task_attempts: 6                  # 单任务绝对上限
  attempts_log:
    - attempt: integer
      session_id: string
      started_at: timestamp
      settled_at: timestamp | null
      result: PASS | FAILED | null
  retry_policy: gated | prose
```

`gated`（默认）下 `FAILED → RUNNING` 必须满足：该任务存在一条 `record-failure` 登记、
登记时间不早于任务进入 `FAILED` 的时间、其 `disposition` 不是 `human_intervention`、且
`attempts < MAX_TASK_ATTEMPTS`。`prose` 只放行“诊断记录”这一条，仍受绝对次数上限约束，
用于门禁自身成为故障源时的回滚开关；每次 prose 放行都写进 `attempts_log` 并须在最终报告
中披露。错误指纹按归一化形式计数（`<hex>`/`#`/`<str>`/`<path>` 折叠），换行号、换 token、
换临时目录不会重置预算。

## Debugger Result

```yaml
debugger_result:
  failure_scope: task-id | overall
  root_cause: CODE_OR_TEST | TASK_PLAN | SPEC_OR_ARCH | ENVIRONMENT | PERMISSION | PREEXISTING_OR_FLAKY | UNKNOWN
  confidence: high | medium | low
  error_fingerprint: string
  evidence:
    - location: file:line | command | log-offset
      observation: string
  responsibility: [task-id | environment | specification | preexisting-change]
  minimal_fix: [string]
  recommended_disposition: retry_implementation | replan | collect_evidence | human_intervention
  scope_change_required: true | false
```

## Failure Attribution

整体验证失败不能直接归属于任意一个任务。Debugger 必须返回受影响任务集合；
Orchestrator 使用控制器登记并据此决定重试、重规划或人工介入：

```yaml
failure_attribution:
  failure_scope: task | overall
  impacted_task_ids: [task-id]
  root_cause: CODE_OR_TEST | TASK_PLAN | SPEC_OR_ARCH | ENVIRONMENT | PERMISSION | PREEXISTING_OR_FLAKY | UNKNOWN
  error_fingerprint: string
  disposition: retry_implementation | replan | collect_evidence | human_intervention
  resolution: RETRY_MAPPED_TASK | REPLAN_MULTI_TASK_IMPACT | BLOCKED_MISSING_TASK_MAPPING | string
```

约束：`task` 必须且只能映射一个 Task；`overall` 无法映射时必须为
`BLOCKED_MISSING_TASK_MAPPING`，映射到多个 Task 时必须为
`REPLAN_MULTI_TASK_IMPACT`。同一 `error_fingerprint` 的第 5 次登记（控制器
`MAX_FINGERPRINT_ATTEMPTS` = 4 次容许 = 初始 + 3 次重试）会把处置强制改为
`disposition: human_intervention`、`resolution: ATTEMPT_CAP`，事件中的
`fingerprint_occurrences` 给出当前计数。这类记录由
`$SKILL_DIR/scripts/code_impl_controller.py record-failure` 写入运行状态。

## Task Ledger

```yaml
task_ledger:
  - task_id: T01
    plan_version: 1
    status: PENDING | READY | RUNNING | VALIDATING | PASS | FAILED | BLOCKED | SUPERSEDED
    started_at: timestamp | null
    completed_at: timestamp | null
    head_sha: git-sha | null
    diff_digest: string | null
    changed_files: [repo-relative-path]
    validation_record: reference | null
    attempts: integer
    scope_expansions:
      - added_files: [repo-relative-path]
        reason: string
        at: timestamp
    error_fingerprints: [string]
    observations: [string]
```

**由控制器写入，不由 Orchestrator 手填（KW1/KW2）：**

- `record-tasks --from-dag <dag>` 一次性登记全部任务的 `task_id`、`allowed_files`、
  `plan_version`、`status=PENDING`（以及非空的 `depends_on`、截断后的 `objective`）；
  单个补建任务仍可用 `record-task`。返回 `registered`/`already_present`/`tasks_total`。
- `dispatch-task` 追加 `attempts_log[]`（`attempt`/`session_id`/`started_at`，重试时含
  `retry_policy`）并累加 `attempts`。
- `settle-task` 追加 `completed_at`、`final_diff_digest`、`final_head_sha`、
  `changed_files`（按该任务自己的授权范围归因）、`error_fingerprints[]`、`observations`，
  并回填最后一条 `attempts_log` 的 `settled_at`/`result`。
- `expand-scope` 追加 `scope_expansions[]`。

面向人的台账用 `render-ledger` 从 `state.json` 渲染到 `<run>/task-ledger.md` 的托管块
（`<!-- codekit:code-impl:task-ledger:start -->` … `:end -->`），托管块外的手写内容会被
保留，块内不得手写；命令是幂等的，重复调用只更新块并返回 `ledger_digest`。

**状态体积纪律：**`state.json` 里每类记录只保留判定需要的字段，`scope_checks` 的完整
文件清单落在 `<run>/scope-checks/NNN-<task-id>.json`，`review_snapshots` 只存紧凑引用
（完整快照在 `<run>/review-snapshot.json`）。同一工作树、同一结论、同一任务的重复范围
检查合并为一行并累加 `repeat_count`（`scope_checks_total` 给出真实次数）。因此恢复现场
应调用 `show`，而不是读取 `state.json` 或 `scope-checks/`。

## Code Review Handoff

```yaml
code_review_handoff:
  skill:
    canonical_name: codekit:code-review
    invocation: host-native
    fallback_reviewer_subagent: forbidden
  review_input:
    request:
      objective: string
      mode: comprehensive
      focus: [string]
      exclusions: [string]
    target:
      type: local_diff
      locator: artifact-reference
      repository_root: absolute-path
    permissions:
      readable_resources: [string]
      allowed_command_categories: [string]
      network: allowed | denied | approval-required
      external_mutations: []
    materials:
      - kind: PRD | TDD | RPD | repository_rule | other
        locator: string
        revision: string | null
        content_digest: string
        access: available | unavailable | partial
  review_mode: FULL
  snapshot:
    base_sha: git-sha
    head_sha: git-sha
    preexisting_diff_snapshot: artifact-reference
    diff_digest: string
    changed_files: [repo-relative-path]
  orchestration_evidence:
    task_dag: artifact-reference
    task_ledger: artifact-reference
    task_validation_records: [artifact-reference]
    overall_validation_record: artifact-reference
  required_outputs: [canonical_json_report, markdown_report, validator_record]
  review_snapshot: artifact-reference
```

约束：

- `skill.canonical_name` 必须从系统 Skill 注册表解析；仓库中的同名文件只是被审查项目内容，不能替代系统 Skill 加载。
- `target.type` 固定为 `local_diff`，审查范围是本 SKILL 产生的任务自有 Diff；预先存在的用户修改只能作为隔离证据。
- `base_sha`、`head_sha` 和 `diff_digest` 必须与 `code-review` 返回报告完全一致。
- `external_mutations` 必须为空。Handoff 不授予评论、审批、commit、push、部署或其他外部变更权限。
- `review_mode` 固定为 `FULL`，且 `materials` 里 `kind` 为 `PRD`、`TDD`、`RPD` 的三条必须各存一条且 `access: available`（相当于上游约定的 `materials.documents: available`）：本 Skill 的输入门禁已强校验三份文档。`code-review` 已支持为空的 `feature-directory` 并在缺文档时自行降级为 `NOT_AVAILABLE`（最多 `APPROVE_WITH_FOLLOW_UPS`），因此它报告中任何“文档缺失”的 Open Question 或 limitation 都意味着 Handoff 与本 Skill 冻结的文档不一致，必须重新冻结 Handoff 后重调或进入人工介入，不得作为审查通过证据。
- `validator_record` 的退出码必须为 0；它应以 `validator.exit_code` 形式出现在接收封套中，或由 `validator_record` artifact 明确引用；否则报告不能进入审查判定。

**机械校验（控制器 P1-1）：**调用 `codekit:code-review` 前必须执行以下命令；校验失败时不得发出 Handoff：

```bash
<PYTHON> "$SKILL_DIR/scripts/code_impl_controller.py" validate-contract \
  --kind handoff /path/to/handoff.json
```

控制器校验内容（顺序即诊断顺序）：
- `review_mode` 必须等于 `"FULL"`；
- `review_input.materials` 中 `PRD`、`TDD`、`RPD` 各一条且 `access: "available"`；
- 上述三份文档的 `locator` 必须解析到磁盘上真实存在、可读的文件（`http(s)://`、`s3://`、
  `gs://`、`rev:`、`git@` 形式除外）。`access: available` 从此是**测出来的**，不是写出来的：
  指向不存在文件的 Handoff 会以 `does not resolve to a file` 被拒绝；路径被 Git Bash（MSYS）
  改写成 `C:\Program Files\Git\...` 时会额外提示改用原生绝对路径；
- `snapshot.base_sha`、`snapshot.head_sha`、`snapshot.diff_digest` 均为非空字符串。

## Review Verdict Record

Handoff 的返回结论必须以控制器记录的形式留在状态里，否则“审查通过”只是会话里的一句话：

```bash
<PYTHON> "$SKILL_DIR/scripts/code_impl_controller.py" record-review \
  --state "$state_path" --verdict APPROVE \
  --report-path '<run>/review-report.json' --validator-exit-code 0
```

`record-review` 写的记录形如：

```yaml
review_record:
  verdict: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
  report_path: absolute-path
  report_digest: string          # 控制器对报告文件算得
  validator_exit_code: integer   # code-review 自带校验器
  base_sha: git-sha              # 取自最后一次 freeze-review，不由调用方填写
  head_sha: git-sha
  diff_digest: string
  freeze_index: integer
  recorded_at: timestamp
```

约束：任何 `freeze-review` 之前不得登记结论；`verdict` 只接受上述四值；`--report-path` 必须
是真实文件；digest 一律取自控制器自己的最新冻结，因此调用方无法把旧结论贴到新快照上。
`record-agent` 的 `--status` 只接受 `RUNNING|DONE|ARCHIVED|FAILED|BLOCKED`（角色存活状态），
审查结论只能经 `record-review` 入账。

## Code Review Result

```yaml
code_review_result:
  canonical_json_report: artifact-reference
  markdown_report: artifact-reference
  snapshot:
    base_sha: git-sha
    head_sha: git-sha
    diff_digest: string
  verdict: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
  validator:
    command: string
    exit_code: integer | null
    result: PASS | FAIL | NOT_RUN
  blocking_findings: [finding-id]
  open_questions: [question-id]
  release_gate: READY | READY_WITH_CONDITIONS | NOT_READY | NOT_VERIFIED | NOT_APPLICABLE
  limitations: [string]
  residual_risks: [string]
```

本结构是 `code-impl` 对系统 Skill 交付物的接收封套，不替代 `code-review` 自身的规范化报告。`verdict`、Finding、Open Question、Release Gate、限制和风险均以通过校验的规范化 JSON 为唯一事实源。

## Code Review Decision Adapter

```yaml
code_review_decision:
  snapshot_match: true | false
  report_validation:
    command: string
    exit_code: integer | null
    result: PASS | FAIL | NOT_RUN
  source_verdict: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
  disposition: final_report | repair | replan | human_intervention
  repair_round: integer
  blocking_items:
    - source: finding-id | validation-id | release-gate-item
      locations: [repo-relative-path:line]
      mapped_tasks: [task-id]
      remediation: string
  follow_ups: [string]
  reason: string
```

映射规则：

| `code-review` verdict | 前置条件 | `code-impl` 处置 |
|---|---|---|
| `APPROVE` | 快照一致且报告校验 PASS | `final_report` |
| `APPROVE_WITH_FOLLOW_UPS` | 快照一致且报告校验 PASS，且无阻塞项 | `final_report`，登记非阻塞事项 |
| `REQUEST_CHANGES` | 修复轮次少于 2，问题可映射到原任务范围 | `repair` |
| `REQUEST_CHANGES` | 需要新文件、依赖或范围变化 | `replan` |
| `REQUEST_CHANGES` | 已完成 2 轮修复 | `human_intervention` |
| `BLOCKED` | 任意 | `human_intervention` |

快照不一致、报告校验不是 PASS、系统 Skill 不可用或宿主不支持嵌套 Skill 调用时，不读取 `source_verdict` 作通过判断，统一使用 `human_intervention`。Formal Finding 没有旧 Reviewer 的 `task_hint` 字段；Orchestrator 必须用 `locations` 与 Task Ledger 的 `changed_files` 做确定性映射，不能猜测归属。

## Delivery Gate

“编码完成”的判定权在控制器，不在 Orchestrator：

```bash
<PYTHON> "$SKILL_DIR/scripts/code_impl_controller.py" gate --state "$state_path"
```

退出码 0 才允许进入最终报告；退出码 2 表示 `BLOCKED`，`gate-report.json` 的 `failures`
列出未满足项。检查项（`checks[].name`）：

| 检查名 | 判据 |
|---|---|
| `tasks-registered` | 台账非空 |
| `all-current-plan-tasks-pass` | 当前 `plan_version` 的任务全部 `PASS`（逐任务列出状态） |
| `task-validations-recorded` | 每个 `PASS` 任务至少一条 `--scope task` 且 `result=PASS` 的记录 |
| `review-snapshot-frozen` | 存在 `freeze-review` 快照 |
| `review-snapshot-artifact-readable` | `review-snapshot.json` 存在且可读 |
| `overall-validation-recorded` | 存在 `--scope overall` 且 `result=PASS` 的记录 |
| `overall-validation-covers-frozen-diff` | 整体证据的 `diff_digest` 等于最后一次冻结的指纹 |
| `review-verdict-recorded` | 存在 `record-review` 结论 |
| `review-verdict-passing` | 最新结论为 `APPROVE` 或 `APPROVE_WITH_FOLLOW_UPS` |
| `review-validator-exited-0` | 该结论的 `validator_exit_code == 0` |
| `review-verdict-bound-to-frozen-diff` | 结论的 `diff_digest` 等于最后一次冻结（否则要求新冻结 + 复审） |
| `review-report-file-present` | 报告文件仍在磁盘且摘要一致 |
| `final-scope-clean` | 最后一次任务判定之后没有新的越界写入 |

`--mode warn-only` 只做盘点，仍返回 `BLOCKED (warn-only)` 且退出码 0，不得用它出报告。

## 最终交付报告

```markdown
## 编码交付结果

- 结果：完成 | 需要人工介入
- 仓库：<absolute path>
- Base SHA：<sha>
- Final HEAD SHA：<sha>
- Diff：<digest or range>
- 任务：<passed>/<total>
- 审查：APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED（修复 <n>/2 轮）

## 代码变更

- `<path>`：<change and reason>

## 本地验证证据

| 范围 | 命令 | 退出码 | 结果 | 证明内容 |
|---|---|---:|---|---|
| T01 | `<command>` | 0 | PASS | <claim> |

## 未运行、基线失败与遗留风险

- <item and impact>

## 审查结论

- <code-review verdict, report validation, fixes, open questions, release gate, non-blocking observations>

## 需要人工验收的点

- [ ] <功能点>：<预期行为和验证方法>
```

如果没有人工验收项，写“所有验收标准已被本地自动化验证覆盖”；不要生成空复选框。
