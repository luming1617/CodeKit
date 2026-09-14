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

修改范围扩大、权限变化或验收标准变化时，必须生成新的 Task Contract；不要口头追加隐含范围。

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
`REPLAN_MULTI_TASK_IMPACT`。这类记录由
`$SKILL_DIR/scripts/code_impl_controller.py record-failure` 写入运行状态。

## Task Ledger

```yaml
task_ledger:
  - task_id: T01
    plan_version: 1
    status: PENDING | READY | RUNNING | VALIDATING | PASS | FAILED | BLOCKED
    started_at: timestamp | null
    completed_at: timestamp | null
    head_sha: git-sha | null
    diff_digest: string | null
    changed_files: [repo-relative-path]
    validation_record: reference | null
    attempts: integer
    error_fingerprints: [string]
    observations: [string]
```

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

控制器校验内容：
- `review_mode` 必须等于 `"FULL"`；
- `review_input.materials` 中 `PRD`、`TDD`、`RPD` 各一条且 `access: "available"`；
- `snapshot.base_sha`、`snapshot.head_sha`、`snapshot.diff_digest` 均为非空字符串。

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
