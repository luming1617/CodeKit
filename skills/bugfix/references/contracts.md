# Bugfix Contracts

本文件定义 Bugfix 各阶段必须保留的最小数据契约。可以使用 YAML、JSON 或等价的结构化 Markdown，但不得省略关键字段或记录密钥、Token、Cookie 和完整个人数据。

## 0. 产物落盘与确定性门禁

所有契约都落在 `RUN_DIR = <仓库根目录>/.codekit/bugfix/<run-id>/`（自带 `.gitignore` 内容 `*`，因此不产生受版本控制的变化）：

| 文件 | 内容 | 写入者 |
|---|---|---|
| `state.json` | Input Contract 的仓库快照、`fix_proposal` 绑定、`approval_record` | `bugfix_state.py start / record-proposal / verify-approval` |
| `proposal.md` | Fix Proposal 正文（按 `assets/bugfix-proposal-template.md`） | 主 Agent |
| `approval.txt` | 用户批准原文（含审批口令） | 主 Agent 逐字保存 |
| `validation-record.json` | Validation Record（真实执行的命令、退出码、错误指纹） | `bugfix_state.py run-validation` |
| `delivery-report.md` | 最终交付报告（按 `assets/bugfix-delivery-report-template.md`） | 主 Agent |

标为“写入者：脚本”的契约不得由 Agent 手写或事后补写；脚本不可用时对应阶段为 `BLOCKED`，而不是改用自述文本。

## 1. Input Contract

```yaml
input_contract:
  invocation:
    feature_directory: absolute-path
    error_description: string | image | url | artifact-reference
  issue:
    summary: string
    observed_behavior: string
    expected_behavior: string | unknown
    occurrence_window: string | unknown
    affected_users_or_scope: string | unknown
    reproduction_steps: [string]
    supplied_evidence: [artifact-reference]
  sources:
    prd: {locator: string, revision: string | null, digest: string}
    tdd: {locator: string, revision: string | null, digest: string}
    rpd: {locator: string, revision: string | null, digest: string}
    project_standards: [artifact-reference]
    repository_instructions: [artifact-reference]
  repository:
    root: absolute-path
    branch: string
    head_sha: git-sha
    working_tree_digest: string
    preexisting_changes: [repo-relative-path]
  permissions:
    read: [path-or-resource]
    write: [repo-relative-path-or-pattern]
    commands: [allowed-command-category]
    network: allowed | denied | approval-required
    external_mutations: [explicitly-authorized-action]
    forbidden: [string]
```

## 2. Evidence levels

| 等级 | 定义 | 例子 |
|---|---|---|
| `E3 OBSERVED` | 在相关环境中实际执行、复现或测量。 | 失败测试、请求与响应、Trace、查询计划。 |
| `E2 DEMONSTRATED` | 从代码、配置、Schema 或数据流直接证明。 | 可达空指针、错误事务边界、缺失权限检查。 |
| `E1 INFERRED` | 合理但尚未充分验证的推断。 | 怀疑连接池耗尽但没有运行指标。 |
| `E0 UNKNOWN` | 关键证据不可用。 | 无法访问实际配置或下游契约。 |

`CONFIRMED_BUG` 的根因通常需要 E2 或 E3。E1 只能作为待验证假设。

## 3. Business Model

```yaml
business_model:
  actors: [string]
  permissions_and_tenancy: [string]
  goal: string
  entities: [string]
  state_transitions: [from-condition-action-to]
  invariants: [string]
  normal_paths: [string]
  boundary_paths: [string]
  exception_paths: [string]
  non_functional_constraints: [string]
  conflicts_or_unknowns: [string]
```

## 4. Hypothesis Ledger

```yaml
hypothesis_ledger:
  - id: H01
    hypothesis: string
    category: code | data | configuration | environment | upstream | downstream | specification | usage
    supporting_evidence: [evidence-reference]
    contradicting_evidence: [evidence-reference]
    test_or_inspection: string
    result: supported | rejected | pending
    confidence: high | medium | low
```

至少保留一个合理替代假设。只有一个未经挑战的假设不能直接成为高置信度根因。

## 5. Diagnosis Result

```yaml
diagnosis_result:
  status: CONFIRMED_BUG | NOT_A_BUG | INSUFFICIENT_EVIDENCE
  expected_behavior:
    statement: string
    authority: artifact-section
  observed_behavior:
    statement: string
    evidence: [evidence-reference]
  trigger_conditions: [string]
  causal_chain: [step]
  root_cause:
    statement: string | null
    location: [file-line-symbol-or-config]
    evidence_level: E3 | E2 | E1 | E0
  contributing_factors: [string]
  rejected_hypotheses: [hypothesis-id-and-reason]
  affected_scope: [string]
  missing_evidence: [string]
```

`NOT_A_BUG` 时必须用权威规格和实际行为证据证明两者一致，或证明问题位于调用、数据、配置、环境或其他责任边界。`INSUFFICIENT_EVIDENCE` 时列出最小补充证据和采集方法。

## 6. Fix Proposal

```yaml
fix_proposal:
  proposal_id: BF-YYYYMMDD-short-name
  proposal_version: integer
  status: WAITING_APPROVAL | APPROVED | REJECTED | SUPERSEDED
  proposal_file: repo-relative-path   # RUN_DIR/proposal.md，由 record-proposal 冻结摘要
  bound_snapshot:
    head_sha: git-sha
    working_tree_digest: string
    document_digests: [string]
  review_mode: FULL | LITE | WAIVED
  diagnosis_reference: string
  selected_solution:
    summary: string
    ownership_layer: string
    rationale: string
  alternatives:
    - solution: string
      rejected_because: string
  allowed_write_files: [repo-relative-path]
  forbidden_changes: [string]
  behavior_to_preserve: [string]
  planned_changes: [file-symbol-change]
  validation_plan:
    regression_test: string
    commands: [command-cwd-proves]
    manual_acceptance: [step-expected-result]
  impact:
    api: string
    data_and_schema: string
    transaction_and_concurrency: string
    messaging_and_cache: string
    security_and_privacy: string
    performance_and_capacity: string
    observability: string
    release_and_rollback: string
  risks_and_mitigations: [risk-mitigation]
  estimated_cost: string
  approval_phrase: string
```

## 7. Approval Record

```yaml
approval_record:
  proposal_id: string
  proposal_version: integer
  approved_by_user_message: message-reference
  approval_file: repo-relative-path   # RUN_DIR/approval.txt，保存用户批准原文
  approved_snapshot: {head_sha: git-sha, working_tree_digest: string}
  approved_write_files: [repo-relative-path]
  approved_solution_digest: string
  verified_by: bugfix_state.py verify-approval
  verdict: OK | FAIL
  approved_at: timestamp
```

用户的批准必须明确指向当前提案，审批口令逐字包含 `批准执行 <proposal_id> v<proposal_version>`，并由 `verify-approval` 校验提案摘要、快照和口令绑定。若快照或方案发生实质变化，旧 Approval Record 失效。

## 8. Implementation Contract

```yaml
implementation_contract:
  approval_record: reference
  objective: string
  repo_root: absolute-path
  start_head_sha: git-sha
  start_diff_digest: string
  preexisting_changes: [repo-relative-path]
  allowed_write_files: [repo-relative-path]
  forbidden_actions: [string]
  required_changes: [file-symbol-change]
  behavior_to_preserve: [string]
  acceptance_criteria: [string]
  validation_commands: [command-cwd-proves]
  retry: {fingerprint: string | null, used: integer, remaining: integer}   # fingerprint 由 run-validation 计算，不得自行估计
```

## 9. Validation Record

```yaml
validation_record:
  snapshot: {head_sha: git-sha, diff_digest: string, changed_files: [path]}
  scope_check: PASS | FAIL
  checks:
    - command: [argv]
      cwd: repo-relative-path
      exit_code: integer | null
      result: PASS | FAIL | TIMEOUT | RETRY_BUDGET_EXCEEDED | NOT_AVAILABLE | NOT_RUN
      proves: string
      fingerprint: string          # 归一化后的错误指纹，驱动重试预算
      output_tail: string
      source: executed | declared   # 只有 executed 可作为通过证据
      reason: string | null
  baseline_failures: [string]
  introduced_failures: [string]
  verdict: PASS | FAIL
```

本记录由 `bugfix_state.py run-validation` 追加到 `RUN_DIR/validation-record.json`；`source: declared` 只用于登记确实无法执行的项目，不能被当作通过。`NOT_RUN`、`NOT_AVAILABLE` 和 `TIMEOUT` 不能满足必需验证项。不得把部分测试写成“全部测试通过”。

## 10. System Code Review Result

```yaml
review_result:
  snapshot: {base_sha: git-sha, head_sha: git-sha, diff_digest: string}
  root_cause_resolved: true | false
  approved_scope_respected: true | false
  preserved_behavior_assessment: string
  regression_assessment: string
  findings:
    - severity: critical | high | medium | low
      blocking: boolean
      location: repo-relative-path:line
      evidence: string
      remediation: string
  verdict: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
```

系统审查结果必须带有与 Handoff 一致的 `base_sha`、`head_sha` 和 `diff_digest`，并由系统 `codekit:code-review` 自带的结构校验器验证。不得将普通 Subagent 的 `APPROVED` 或 `NEEDS_CHANGES` 摘要转换为系统审查通过。

## 11. System Code Review Handoff

Bugfix 实施后的独立审查必须交给宿主已注册的 `codekit:code-review` Skill，不得由本目录的 Reviewer Prompt 或普通 Subagent 模拟。Handoff 至少包含：

```yaml
code_review_handoff:
  target: local_diff
  repository_root: absolute-path
  base_sha: git-sha
  head_sha: git-sha
  diff_digest: string
  preexisting_changes: [repo-relative-path]
  review_mode: FULL | LITE | WAIVED
  materials:
    documents: available | NOT_AVAILABLE
    prd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    tdd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    rpd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    repository_rules: [artifact-reference]
  validation_record: reference        # RUN_DIR/validation-record.json
```

`review_mode` 表达本次审查能拿到多完整的规格输入，与 SKILL.md「审查模式与降级门禁」一致：`FULL` 接受 `APPROVE` 或 `APPROVE_WITH_FOLLOW_UPS`；`LITE`（`documents: NOT_AVAILABLE`）只接受 `APPROVE_WITH_FOLLOW_UPS`；`WAIVED` 仅在用户明确批准豁免时使用，并必须在交付报告里写明未独立复核。

只有系统 Skill 返回的、快照与 `diff_digest` 完全一致且通过其结构校验的可接受结论才能作为交付门禁证据；`REQUEST_CHANGES` 和 `BLOCKED` 不得改写成通过。
