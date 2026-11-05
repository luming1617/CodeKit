# Bugfix Contracts

本文件定义 Bugfix 各阶段必须保留的最小数据契约。可以使用 YAML、JSON 或等价的结构化 Markdown，但不得省略关键字段或记录密钥、Token、Cookie 和完整个人数据。

## 0. 产物落盘与确定性门禁

所有契约都落在 `RUN_DIR = <仓库根目录>/.codekit/bugfix/<run-id>/`（自带 `.gitignore` 内容 `*`，因此不产生受版本控制的变化）：

| 文件 | 内容 | 写入者 |
|---|---|---|
| `state.json` | Input Contract 的仓库快照、`fix_proposal` 绑定、`approval_record` | `bugfix_state.py preflight / start / record-proposal / verify-approval` |
| `doc-map.md` | 每份 PRD/TDD/RPD 的 digest、行数、`approx_tokens` 与标题→行区间 | `bugfix_state.py preflight / start` |
| `investigation.md` | 步骤 03–07 的 Business Model、Hypothesis Ledger、Diagnosis Result 与反证结论 | Investigator / Challenger 按节追加 |
| `proposal.md` | Fix Proposal 正文（按 `assets/bugfix-proposal-template.md`） | 主 Agent |
| `approval.txt` | 用户批准原文（含审批口令） | 主 Agent 逐字保存 |
| `implementation-contract.json` | Implementation Contract（允许范围、禁止项、不变量、起始摘要、重试预算） | `bugfix_state.py freeze-contract` |
| `validation-plan.json` | 从提案第 5 节验证表渲染的命令清单 | `bugfix_state.py freeze-contract` |
| `validation-record.json` | Validation Record（真实执行的命令、退出码、错误指纹） | `bugfix_state.py run-plan / run-validation` |
| `delivery-report.md` | 最终交付报告（按 `assets/bugfix-delivery-report-template.md`） | 主 Agent |

标为“写入者：脚本”的契约不得由 Agent 手写或事后补写；脚本不可用时对应阶段为 `BLOCKED`，而不是改用自述文本。

### 0.1 读取与交接预算

契约在阶段之间按**路径**交接，不按内容搬运：

- 派发 Sub-agent 时，任务正文只给 `SKILL_DIR`/`RUN_DIR` 绝对路径、契约文件名和本轮要回答的问题；需要它读的规格文档，给 `doc-map.md` 里的标题区间，让它自己用 `Read` 的 `offset`/`limit` 取那一段。禁止把 PRD/TDD/RPD、提案或上一阶段的完整正文粘进任务正文。
- 主 Agent 自己读规格文档同样按区间取段；只有裁决冲突或写提案的相关章节才展开。
- `state.json`、`implementation-contract.json`、`validation-record.json` 的字段以对应子命令的 stdout 摘要为准，不为了“看一眼”而整份读入；需要逐条排查基线时才用 `--verbose` 或读文件。
- 每条门禁命令的输出面向决策：`preflight`、`verify-gates`、`freeze-contract`、`run-plan` 各一次调用即可完成对应步骤，不得拆成多次调用后由模型逐条复述。

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

本 Skill 写出的所有摘要（`working_tree_digest`、文档 `digest`、`contract_digest`、`gate_digest`）都是
`sha256/hex`：64 位小写十六进制、不带 `sha256:` 前缀，读取文本时不丢字节也不替换非法编码 —— 与
`code-review`、`code-impl` 同一套编码，否则三家的 `==` 永远不成立。`SCHEMA_VERSION` 因此升到 `2`，
旧 `state.json` 里带前缀的摘要值不可互换，脚本会拒绝加载并说明怎么办，而不是把两种编码混进一次比较。
跨 SKILL 的 `changed_files` 用 `STATUS:path` 记法；本 Skill 内部的 `preexisting_changes` 与
`verify-scope` 的 `changed_files` 是纯仓库相对路径（要拿去匹配允许写清单），两者不是一回事。

## 2. Evidence levels

| 等级 | 定义 | 例子 |
|---|---|---|
| `E3 OBSERVED` | 在相关环境中实际执行、复现或测量。 | 失败测试、请求与响应、Trace、查询计划。 |
| `E2 DEMONSTRATED` | 从代码、配置、Schema 或数据流直接证明。 | 可达空指针、错误事务边界、缺失权限检查。 |
| `E1 INFERRED` | 合理但尚未充分验证的推断。 | 怀疑连接池耗尽但没有运行指标。 |
| `E0 UNKNOWN` | 关键证据不可用。 | 无法访问实际配置或下游契约。 |

`CONFIRMED_BUG` 的根因通常需要 E2 或 E3。E1 只能作为待验证假设。

## 3. Business Model

第 3、4、5 节的契约统一写入 `RUN_DIR/investigation.md`，按节追加，不另建文件，也不在派发时复述正文。

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

`record-proposal` 对提案冻结两份摘要：`gate_digest` 覆盖提案里的承重标签与两张表（首列为 `文件/符号` 的计划修改表、首列为 `检查` 的验证计划表）的集合语义——`方案 ID`、`方案版本`、`状态`、`推荐方案`、`绑定快照`、`允许修改文件`、`明确不修改`、`必须保持的行为`、`备选方案及未选择原因`、`回归测试`、`人工验收`、`审批状态`、`审批范围`、`审批口令`，以及 `--review-mode`；全文 `digest` 只做审计留痕。叙述性章节的改写不会推翻 `gate_digest`，因此不必整轮重新审批（见第 7 节）。缺少关键标签时脚本退化为全文绑定，只会更严。

## 7. Approval Record

```yaml
approval_record:
  proposal_id: string
  proposal_version: integer
  approved_by_user_message: message-reference
  approval_file: repo-relative-path   # RUN_DIR/approval.txt，保存用户批准原文
  approved_snapshot: {head_sha: git-sha, working_tree_digest: string}
  approved_write_files: [repo-relative-path]
  approved_solution_digest: string      # state.json: proposal.digest（全文摘要，审计留痕）
  approved_gate_digest: string          # state.json: proposal.gate_digest（审批真正绑定的对象）
  editorial_revisions:                  # 仅叙述修订的放行记录，空数组表示从未豁免
    - {from_digest: string, to_digest: string, from_gate: string, to_gate: string, at: timestamp, note: string}
  verified_by: bugfix_state.py verify-approval | verify-gates
  verdict: OK | FAIL
  approved_at: timestamp
```

用户的批准必须明确指向当前提案，审批口令逐字包含 `批准执行 <proposal_id> v<proposal_version>`，并由 `verify-approval` 校验承重字段摘要、快照和口令绑定。若快照或方案发生实质变化，旧 Approval Record 失效。只有全文 `digest` 变化、`gate_digest` 与章节骨架（标题序列与篇幅）都未变时，才允许用 `--editorial` 放行一次编辑性修订并写入 `editorial_revisions`；`--editorial` 不能救回任何 gate、快照或口令失败，也不构成用户对新方案的批准。

## 8. Implementation Contract

```yaml
implementation_contract:
  approval_record: reference          # 提案 ID/版本/全文 digest/gate digest/审批时间
  objective: string
  repo_root: absolute-path
  review_mode: FULL | LITE
  start_head_sha: git-sha
  start_diff_digest: string
  preexisting_changes: [repo-relative-path]
  allowed_write_files: [repo-relative-path]
  forbidden_changes: [string]         # 提案『明确不修改』
  behavior_to_preserve: [string]      # 提案『必须保持的行为』
  planned_changes: [string]           # 提案「文件/符号」表逐行
  acceptance_criteria: [string]
  validation_commands: [plan-item]    # 提案第 5 节渲染出的验证计划
  retry: {fingerprint: string | null, used: integer, remaining: integer}   # fingerprint 由 run-plan / run-validation 计算，不得自行估计
  run_dir: absolute-path
  doc_map: absolute-path
  frozen_at: timestamp
  contract_digest: string             # 除本字段外整份合同的 canonical-json 摘要
```

本契约由 `bugfix_state.py freeze-contract` 从 `state.json` 的审批绑定与提案正文渲染成 `RUN_DIR/implementation-contract.json`，并同步生成 `RUN_DIR/validation-plan.json`（其 `contract_digest` 与合同自摘要一致）；主 Agent 只提供覆盖项，不手写摘要或允许范围。`verify-scope --contract` 直接读取该文件的 `allowed_write_files`，并用同样的 canonical-json 摘要复核合同是否被事后改写，结果写入 `contract_integrity`：`OK` / `TAMPERED`（硬失败）/ `MISSING_DIGEST`（升级前的旧合同）/ `NOT_APPLICABLE`（未用 `--contract`）。事后改合同只会让越界判定变得更宽松，因此 `out_of_scope` 为空并不代表范围合法，必须同时看 `contract_integrity`。

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
  snapshot:                          # 逐字复制 code-review 报告 $.snapshot 的 12 个扁平字段
    base_sha: git-sha
    head_sha: git-sha
    target_id: "<absolute repository_root>@<head_sha>"
    diff_digest: string
    staged_digest: string
    unstaged_digest: string
    untracked_digest: string
    changed_files: ["STATUS:path"]
    preexisting_changes: [string]
    documents: object
    review_scale: string
    manifest_digest: string
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

系统审查结果必须带有与 Handoff 同一 `target_id` 的快照，并由系统 `codekit:code-review` 自带的结构校验器验证。不得将普通 Subagent 的 `APPROVED` 或 `NEEDS_CHANGES` 摘要转换为系统审查通过。

## 11. System Code Review Handoff

Bugfix 实施后的独立审查必须交给宿主已注册的 `codekit:code-review` Skill，不得由本目录的 Reviewer Prompt 或普通 Subagent 模拟。Handoff 至少包含：

```yaml
code_review_handoff:
  target: local_diff
  repository_root: absolute-path
  snapshot_handoff: reference        # RUN_DIR/review-handoff.json：下游 `--handoff` 的唯一输入，不得手写
  review_mode: FULL | LITE | WAIVED
  materials:
    documents: available | NOT_AVAILABLE
    prd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    tdd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    rpd: {locator: string, revision: string | null, digest: string} | NOT_AVAILABLE
    repository_rules: [artifact-reference]
  validation_record: reference        # RUN_DIR/validation-record.json
```

`review-handoff.json` 由 `freeze-contract` 与实现合同同时写出，形状即 CW2-a 的统一 Handoff 快照：

```yaml
# RUN_DIR/review-handoff.json
schema_version: 2
produced_by: "codekit:bugfix/scripts/bugfix_state.py"
digest_scheme: "sha256/hex"           # 裸小写 sha256 hex，无 `sha256:` 前缀（三家同一声明）
target_id: "<absolute repository_root>@<start_head_sha>"
committed_range: null                 # 本 Skill 从不 commit：显式声明"不含已提交范围"，而非缺键
worktree_state:
  head_sha: git-sha                   # 只声明 head_sha
```

只声明 `head_sha` 是有意的：批准时冻结的 `working_tree_digest` 描述的是"动手前"的树，把它写成审查目标的摘要会是错的。

比对规则（与 `code-review` 的 `--handoff` 实现一致，四条全部机械可判）：同一 `target_id` 才继续；只比两侧都声明了的字段（这里即 `head_sha`）；`digest_scheme` 不等时不比摘要，记 `HANDOFF_SCHEME_MISMATCH`；上游不含 `committed_range` 而本轮含已提交内容时记 `SCOPE_SUPERSET` 限制并继续审查，**不得**判 `BLOCKED`。

`review_mode` 表达本次审查能拿到多完整的规格输入，与 SKILL.md「审查模式与降级门禁」一致：`FULL` 接受 `APPROVE` 或 `APPROVE_WITH_FOLLOW_UPS`；`LITE`（`documents: NOT_AVAILABLE`）只接受 `APPROVE_WITH_FOLLOW_UPS`；`WAIVED` 仅在用户明确批准豁免时使用，并必须在交付报告里写明未独立复核。

只有系统 Skill 返回的、`--handoff` 比对未以退出码 3 报 `BLOCKED`（即 `target_id` 相同且共有字段相等）、且通过其结构校验的可接受结论才能作为交付门禁证据；`SCOPE_SUPERSET`、`HANDOFF_SCHEME_MISMATCH` 与 `uncomparable_digests` 是随报告登记的限制，不是不匹配，也不是驳回理由。`REQUEST_CHANGES` 和 `BLOCKED` 不得改写成通过。
