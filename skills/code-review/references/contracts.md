# Review Contracts

本文件定义审查角色之间的规范化数据契约。可以用 YAML、JSON 或结构化 Markdown 在角色间传递；步骤 20–21 的最终规范化负载必须使用 JSON。任何契约都不得保存 Token、Cookie、密码或完整凭据。

## Review Input Contract

```yaml
review_input:
  request:
    objective: string
    mode: comprehensive | focused
    focus: [string]
    exclusions: [string]
  target:
    type: pull_request | merge_request | branch | commit_range | local_diff
    locator: string
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
  review_mode: FULL | LITE | WAIVED | null
  documents_status: available | NOT_AVAILABLE
```

`review_mode` 和 `documents_status` 是上游 Skill（如 `codekit:bugfix`、`codekit:code-impl`）Handoff 中 `review_mode` 与 `materials.documents` 的归一化落点：调用方声明的值优先于本 Skill 自己的推断，不得另立判断。独立调用（无 Handoff）时，`review_mode` 写 `null`，`documents_status` 由步骤 01 的文档解析结果填写：至少一份有效 PRD/TDD/RPD 为 `available`，否则为 `NOT_AVAILABLE`。`WAIVED` 只可能来自调用方，收到该值时结论固定为 `BLOCKED`。

`external_mutations` 默认为空。审查请求不等于授权发布评论、批准 PR/MR 或改变远端状态。

## Review Snapshot

```yaml
review_snapshot:
  repository_root: absolute-path
  target_type: string
  target_locator: string
  branch: string | null
  merge_base_sha: git-sha | null
  base_sha: git-sha
  head_sha: git-sha
  commits: [git-sha]
  changed_files:
    - path: repo-relative-path
      status: added | modified | deleted | renamed | untracked
      old_path: repo-relative-path | null
  diff_digest: string
  diff_artifact: reference
  staged_digest: string | null
  unstaged_digest: string | null
  untracked_digest: string | null
  preexisting_changes: [repo-relative-path]
  target_metadata_digest: string | null
  manifest_digest: sha256
  material_versions:
    - locator: string
      revision: string | null
      content_digest: string
  frozen_at: timestamp
```

所有后续角色都必须回显 `diff_digest`。出现不同摘要时停止合并结果并重新冻结。

## Context Contract

```yaml
context_contract:
  diff_digest: string
  applicable_rules:
    - source: absolute-path
      scope: repo-relative-directory
      applies_to: [repo-relative-path]
      rule_summary: [string]
  instruction_scope_matrix:
    - changed_file: repo-relative-path
      rule_sources: [absolute-path]
      conflicts: [string]
  modules: [string]
  boundaries: [string]
  entry_points: [file:line]
  upstream_consumers: [file:line]
  downstream_dependencies: [file:line]
  tests: [repo-relative-path]
  build_and_validation_sources: [repo-relative-path]
  deployment_resources: [repo-relative-path]
  missing_context: [string]
```

## Traceability Matrix

```yaml
traceability:
  - id: REQ-001 | DES-001 | REL-001
    kind: requirement | design | release
    source: locator#section-or-line
    statement: string
    links:
      requirements: [REQ-id]
      designs: [DES-id]
      code: [repo-relative-path:line]
      tests: [repo-relative-path:test-name-or-line]
      releases: [REL-id]
    status: COVERED | PARTIAL | MISSING | CONFLICT | NOT_VERIFIED | NOT_APPLICABLE
    evidence: [string]
    gap: string | null
```

源文档已有 ID 时沿用；没有时分配本轮局部 ID，并在报告中声明其局部性质。

## Impact Surface

```yaml
impact_surface:
  - category: caller | consumer | api | dto | database | message | cache | config | job | build | deployment | observability
    item: string
    relationship: string
    evidence: file:line | tool-result-reference
    confidence: high | medium | low
    analysis_boundary: string | null
```

## Coverage Matrix

九个维度必须各出现一次：

```yaml
coverage:
  - dimension: correctness | security_access | data_transaction | distributed_reliability | compatibility_contract | performance_capacity | test_adequacy | observability | release_rollback
    required: true | false
    status: REVIEWED | VALIDATED | NOT_APPLICABLE | NOT_VERIFIED
    owner: specialist-id | local-validator | none
    evidence: [string]
    reason: string
```

`NOT_APPLICABLE` 需要不适用证据；工具实测支持时可用 `VALIDATED`，仅完成代码审查时使用 `REVIEWED`。

## Candidate Finding

```yaml
candidate_finding:
  id: CF-001
  source_reviewers: [specialist-id]
  dimension: coverage-dimension
  claim: string
  causal_link_to_change: string
  locations: [repo-relative-path:line]
  proposed_trigger_path: [string]
  proposed_impact: string
  counterevidence_to_check: [string]
  suggested_validation: [string]
```

## Evidence Verdict

```yaml
evidence_verdict:
  candidate_id: CF-001
  diff_digest: string
  status: CONFIRMED | REJECTED | UNVERIFIED
  evidence_level: E3 | E2 | E1 | E0
  causal_to_target: true | false | unknown
  verified_locations: [repo-relative-path:line]
  trigger_path: [string]
  guards_and_constraints_checked: [string]
  minimal_scenario: string | null
  impact: string | null
  evidence: [string]
  rejection_or_uncertainty_reason: string | null
```

证据等级：

- `E3`：在固定快照上用测试、可重复命令或确定性运行轨迹复现。
- `E2`：代码、配置、契约与完整可达路径形成静态证明。
- `E1`：推断合理，但有动态行为、环境、外部契约或上下文缺口。
- `E0`：没有足够证据判断。

## Formal Finding

```yaml
finding:
  id: F-001
  candidate_id: CF-001
  title: string
  dimension: coverage-dimension
  severity: BLOCKER | HIGH | MEDIUM | LOW
  blocking: true | false
  blocking_reason: string
  confidence: integer-0-to-100
  evidence_level: E3 | E2
  locations: [repo-relative-path:line]
  causal_chain: [string]
  trigger_scenario: string
  impact: string
  affected_scope: [string]
  evidence: [string]
  remediation: string
  regression_test: string
```

`confidence` 是对已验证证据强度的摘要，不得替代 E2/E3 门槛。Finding 可引用未修改行，但 `causal_chain` 必须证明风险由审查目标引入或暴露。

## Open Question

```yaml
open_question:
  id: Q-001
  question: string
  reason: string
  required_evidence: string
  owner_type: author | product | architect | qa | sre | security | data | external-team
  impact_if_unanswered: string
  blocking: true | false
```

## Validation Record

```yaml
validation:
  - id: VAL-001
    target_claim: string
    command: string
    cwd: repo-relative-path
    diff_digest: string
    started_at: timestamp
    finished_at: timestamp
    exit_code: integer | null
    status: PASS | FAIL_INTRODUCED | FAIL_BASELINE | ENVIRONMENT_ERROR | PERMISSION_DENIED | FLAKY | NOT_RUN
    evidence: string
    not_run_reason: string | null
```

## Failure Attribution

```yaml
failure_attribution:
  validation_id: VAL-001
  root_cause: CODE | TEST | SPEC | ENVIRONMENT | PERMISSION | BASELINE | FLAKY | UNKNOWN
  confidence: high | medium | low
  error_fingerprint: string
  evidence: [string]
  responsibility: [string]
  linked_candidate_or_finding: string | null
```

## Release Gate

```yaml
release_gate:
  status: READY | READY_WITH_CONDITIONS | NOT_READY | NOT_VERIFIED | NOT_APPLICABLE
  checks:
    - item: deployment_order | dependencies | configuration | feature_flag | rollout | stop_conditions | monitoring | observation_window | data_repair | rollback
      status: PASS | CONDITIONAL | FAIL | NOT_VERIFIED | NOT_APPLICABLE
      evidence: [string]
      condition_or_gap: string | null
  blocking_reasons: [string]
  conditions: [string]
```

## 规范化报告 JSON

步骤 20 输出以下 JSON 对象；键名和枚举必须保持不变：

```json
{
  "schema_version": "1.0",
  "review_context": {
    "review_mode": "FULL",
    "documents_status": "available"
  },
  "verdict": "APPROVE",
  "verdict_basis": ["..."],
  "blockers": [],
  "scope": {
    "target_type": "branch",
    "target_locator": "feature/example",
    "included": ["..."],
    "excluded": ["..."]
  },
  "snapshot": {
    "base_sha": "...",
    "head_sha": "...",
    "diff_digest": "...",
    "changed_files": ["..."],
    "staged_digest": "...",
    "unstaged_digest": "...",
    "untracked_digest": "...",
    "preexisting_changes": [],
    "manifest_digest": "..."
  },
  "summary": "...",
  "findings": [],
  "open_questions": [],
  "traceability": [],
  "coverage": [],
  "validation": [],
  "release_gate": {
    "status": "NOT_APPLICABLE",
    "checks": [],
    "blocking_reasons": [],
    "conditions": []
  },
  "rejected_candidates": [],
  "limitations": [],
  "residual_risks": []
}
```

详细数组元素分别遵循上方同名契约。`rejected_candidates` 的每项至少包含 `candidate_id` 和 `reason`；`limitations` 与 `residual_risks` 使用非空字符串。

`review_context` 是最终报告的有效降级上下文：`WAIVED` 必须得到 `BLOCKED`；`LITE` 或 `documents_status: NOT_AVAILABLE` 不得得到 `APPROVE`，并必须记录文档缺失限制或 Open Question。`snapshot` 必须逐字段复制 `freeze_review_snapshot.py` 生成的 manifest；校验器使用 `manifest_digest` 拒绝手工伪造或漂移的快照。

## Markdown 交付结构

从通过校验的 JSON 渲染以下章节，不添加负载之外的新事实：

```markdown
# 代码审查报告

## 结论与依据
## 审查快照与范围
## Findings
## Open Questions
## PRD-TDD-代码-测试-RPD 追踪矩阵
## 审查覆盖矩阵
## 本地验证证据
## Release Gate
## 已排除候选与未验证限制
## 剩余风险
```
