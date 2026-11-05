# Review Contracts

本文件定义审查角色之间的规范化数据契约。可以用 YAML、JSON 或结构化 Markdown 在角色间传递；步骤 20–21 的最终规范化负载必须使用 JSON。任何契约都不得保存 Token、Cookie、密码或完整凭据。

## 节锚点与按角色读取

每个契约节标题下都有机器可读锚点 `<!-- SECTION: <id> -->`。角色**只按锚点定位并读取自己需要的节**（例如 `grep -n "SECTION: evidence-verdict" contracts.md` 取行号后按区间读取），不要整读本文件——全文约 4.8k token，而单个角色只需要 2–4 节。下表与 SKILL.md「按阶段读取资源」一致，两侧由测试互相核对：

| 角色 / 步骤 | 必读锚点 |
|---|---|
| Context Analyst（03） | `review-input-contract`、`review-snapshot`、`context-contract` |
| Traceability Analyst（05） | `review-input-contract`、`review-snapshot`、`traceability-matrix` |
| Specialist Reviewer（08） | `review-input-contract`、`review-snapshot`、`context-contract`、`impact-surface`、`candidate-finding`、`coverage-matrix` |
| Evidence Verifier（10） | `review-snapshot`、`candidate-finding`、`evidence-verdict`、`formal-finding`、`open-question` |
| Failure Analyst（16） | `validation-record`、`failure-attribution`、`candidate-finding` |
| Evidence Verifier（18） | `traceability-matrix`、`coverage-matrix`、`validation-record`、`failure-attribution`、`release-gate` |
| Orchestrator（01/20/21） | `review-input-contract`、`review-snapshot`、`dispatch-ledger`、`normalized-report-json`、`markdown-delivery` |

## Review Input Contract
<!-- SECTION: review-input-contract -->

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

PRD/TDD/RPD 三类文档的版本判据以步骤 02 冻结的 `review_snapshot.documents[]` 为准——`--documents` 传入的路径就是这里 `materials` 中命中的同类条目，两边对不上时以签名的 manifest 为冻结输入，并把差异登记进 `limitations`。`repository_rule` 与 `other` 类资料不进 manifest（它们随仓库快照的 `diff_digest`/规则作用域一起核对）。

## Review Snapshot
<!-- SECTION: review-snapshot -->

由 `freeze_review_snapshot.py` 生成（独立分支目标用 `--auto-target branch`，Handoff 模式用显式 SHA 参数），字段集合与脚本输出严格一致，任何 Agent 不得手工填写或增删键：

```yaml
review_snapshot:
  schema_version: "1.5"
  repository_root: absolute-path
  target_type: string                # branch | local_diff | commit_range | ...
  target_locator: string             # 如 master...feature/x，记录实际选择的基线
  digest_scheme: "sha256/hex"        # CW2-a①：本 manifest 内所有摘要都是裸小写十六进制，无前缀
  target_id: "<absolute repository_root>@<head_sha>"   # 跨 SKILL 连接键，非摘要；三家各自可重算
  committed_range:                   # CW2-a②：base/head 固定端点，range_digest 固定端点之间的内容
    base_sha: git-sha                # branch 目标下为 merge-base，而非基线分支 tip
    head_sha: git-sha
    range_digest: sha256             # {base_sha, head_sha, committed 补丁摘要, 范围内 changed_files}
  changed_files:                     # "STATUS:path" 字符串；重命名/复制为 "R:old->new"（三家同一记法）
    - string
  changed_files_digest: sha256       # 全量 changed_files 规范 JSON 的独立摘要
  changed_files_path: absolute-path  # 可选：仅清单超过 60 项时存在，指向 changed-files.txt
  review_scale:                      # CW4-b 规模分档，档位与口径一起签名，报告侧照抄
    tier: S | M | L | XL             # 前三档上限见 tier_limits，XL 为兜底档
    changed_files: integer           # 四层变更文件数
    changed_lines: integer           # tracked 三层 numstat 增删行 + 未跟踪文件行数
    tier_limits: [[integer, integer]]  # [文件数上限, 行数上限]，与脚本 SCALE_TIER_LIMITS 同源
  worktree_state:                    # CW2-a②：1.5 起 diff_digest 只覆盖工作树三层，不含已提交范围
    staged_digest: sha256
    unstaged_digest: sha256
    untracked_digest: sha256         # 顶层 .codekit/ 运行时产物被固定排除（SKILL.md 步骤 02 披露）
    diff_digest: sha256              # {head_sha, 三层 digest, 工作树 changed_files, preexisting_changes}
  preexisting_changes: [string]      # 独立 branch 目标模式固定为 []
  documents:                         # 步骤 01 解析到的 PRD/TDD/RPD；无文档时为 []
    - locator: absolute-path         # 脚本解析后的绝对路径（相对路径按 repository_root 展开）
      revision: string               # --document-revision 给的版本标签；缺省用本次 content_digest
      content_digest: sha256         # 该文件字节的 sha256
  freeze_index: integer              # 同一 target 的第几次冻结（含首次）；上限 MAX_REFREEZE=3
  retry_policy: gated | prose        # gated 机械执行该上限；prose 是 CW4-e 回滚开关（步骤 02 的 --retry-policy）
  produced_by: "codekit:code-review/scripts/freeze_review_snapshot.py"   # 固定字面量
  generator_version: "1.5"
  git_probe_digest: sha256           # 脚本实际执行的 git 命令序列的规范摘要
  handoff:                           # 可选：仅 --handoff 时存在，是 CW2-a③ 机械比对的结果
    path: absolute-path              # 读入的上游 Handoff 文件
    sources: [string]                # 上游 produced_by 字面量集合
    digest_scheme: "sha256/hex"      # 本侧编码；与上游不等时记 HANDOFF_SCHEME_MISMATCH 限制
    target_id: string
    target_id_matched: true | false | "not_declared"
    compared_fields: [string]        # 两侧都带且 material 相同的共有字段
    uncomparable_digests: [string]   # "<producer>:<value>"；material 不同，比了没有意义
    limitations: [string]            # SCOPE_SUPERSET / HANDOFF_SCHEME_MISMATCH，逐字进报告 $.limitations
  diff_artifact:                     # 可选：仅 --emit-diff 时存在，存在则整段必填
    path: absolute-path              # 四段合一的 review.diff
    bytes: integer                   # 落盘字节数
    digest: sha256                   # 该文件字节的 sha256，派发时随路径传入
    sections: [committed, staged, unstaged, untracked]
    shard_index: absolute-path|null  # --diff-shard by-directory 时的 review.shards/index.json
  manifest_digest: sha256            # 对剔除本字段后的规范 JSON 计算签名
```

三项来源指纹只用于**提高手工伪造 manifest 的成本**（伪造者必须精确复现脚本内部的命令序列与字面量），本技能没有密钥体系，不声称密码学不可伪造。`validate_review_report.py --check-worktree` 提供另一侧核对：按 `repository_root` 重放快照逻辑并比对 `target_id`/`committed_range.base_sha`/`committed_range.head_sha`/`committed_range.range_digest`/`worktree_state.diff_digest`/`worktree_state` 的三个分层 digest/`changed_files`/`changed_files_digest`/`preexisting_changes`/`documents`（不比对 `git_probe_digest`），并额外重算 `diff_artifact.path` 与每份 `documents[].locator` 指向文件的摘要，任一处漂移即 `STALE_SNAPSHOT`；文档在冻结后被改写或删除同样会被抓出。该开关默认关闭，历史报告仍可无参复校验。

`digest_scheme` 与 `target_id` 是 CW2-a 的跨 SKILL 交接底座。三家（code-review、code-impl、bugfix）都自述 `"sha256/hex"`（裸小写十六进制，无 `sha256:` 前缀），因此任何一个摘要落到另一家手里都能按同一编码核对；`target_id` 不是摘要，而是 `<规范化的 repository_root>@<head_sha>`，每家都能从自己手上的 git 事实重算，所以它是"这份结论对应的就是我交给它的那个目标"的连接键，取代了旧规则里比对三个语义不同的 `diff_digest` 的做法。`changed_files` 的 `STATUS:path`（重命名 `R:old->new`）记法同样三家同源：权威实现是 `freeze_review_snapshot.py` 的 `parse_name_status`，code-impl 的 `_status_letter` 按同一口径镜像。

`handoff` 段是步骤 02 带 `--handoff` 时对上游产物的机械比对结果，四条规则全部可判：①`target_id` 不同即拒绝（退出码 `3`）；②只比两侧都带且 material 相同的共有字段，material 不同的 `diff_digest` 进 `uncomparable_digests` 而不算失败；③`digest_scheme` 不等时不比对摘要，记 `HANDOFF_SCHEME_MISMATCH` 限制；④上游不含 `committed_range` 而本轮审查确实含已提交内容时，这是范围超集，记 `SCOPE_SUPERSET` 限制后继续审查，**不得**判 BLOCKED。只有目标不同、上游声明的已提交范围与本轮回放出的范围互斥、以及工作树在上游冻结后又动了这三类才抛 `HandoffError`（退出码 `3`）。`limitations` 里的每条标记由 `validate_signed_disclosures` 要求逐字出现在报告 `$.limitations` 中，非空限制使结论最多为 `APPROVE_WITH_FOLLOW_UPS`——被压低的可见性正是这套机制的目的。

`diff_artifact.digest` 是派发时传给每个只读角色的读物指纹：角色自己按 `shard_index` 取相关分片，读前核对摘要，产物被换掉或改过时不得继续出结论。`manifest_digest` 在登记 `diff_artifact`、`changed_files_path` 与 `documents` 之后重算，因此这三段受签名保护。

`freeze_index` 是 CW4-a 的重冻结预算判据：脚本按 `target_locator` + `base_sha` + `head_sha` 三元组在 `<repository_root>/.codekit/code-review/*/review-snapshot.json` 里查同一目标已冻结过的摘要谱系，同一份差异重复冻结复用原编号，内容变化才推进；编号超过 `MAX_REFREEZE=3` 时 `--emit-manifest` 以退出码 `2` 拒绝写入，校验器同样拒绝超预算的 manifest。计数只认按 SKILL 落点写出的产物，删档或换到别处写 manifest 会让谱系重来——那是人工另起一次审查的动作，不是绕闸手段。

`retry_policy` 是 CW4-e 的回滚开关落点，与 code-impl 的 `--retry-policy gated|prose` 同名同义：`gated`（默认）下超预算的快照由脚本以退出码 `2` 拒绝、校验器同样拒绝；`prose` 下脚本只打 `WARN:` 并照常签名落盘，校验器因此不再拒绝该 `freeze_index`，但改而要求 `$.limitations` 里逐字出现脚本给出的 `MAX_REFREEZE=<freeze_index>` 标记（缺该标记即 `$.limitations` 报错），并且非空 `limitations` 使结论最多为 `APPROVE_WITH_FOLLOW_UPS`。因为该键在 `manifest_digest` 的签名范围内，"声称人工放行过"必须真的以 prose 冻结过一次才成立。`MAX_DISPATCHES=14` 是 policy-independent 的绝对预算，两种策略下都不放松。

报告负载的 `snapshot` 键恰好复制其中 12 个字段（`base_sha`、`head_sha`、`target_id`、`diff_digest`、`staged_digest`、`unstaged_digest`、`untracked_digest`、`changed_files`、`preexisting_changes`、`documents`、`review_scale`、`manifest_digest`），`scope.target_type`/`target_locator` 与 manifest 一致；这些机械字段由 `--emit-skeleton` 预填，不得手工转录。报告侧的形状故意保持扁平——`SNAPSHOT_REPORT_FIELDS` 是唯一声明"报告字段 -> manifest 路径"的表，骨架预填与校验器的逐字段核对共用它，所以拆分 manifest 不需要历史报告改形。文档版本清单同样由脚本落进签名 manifest，报告侧照抄即为可核对的冻结输入——"读过哪份方案"不再是模型自述。

`changed_files` 有两种合法形状，由 `--emit-skeleton` 按阈值自动选择，角色与 Orchestrator 都不做判断：

- 清单 ≤ 60 项：逐字复制 manifest 的字符串数组。
- 清单 > 60 项：`{count, digest, first, full_list_path}`，其中 `digest` 等于 manifest 的 `changed_files_digest`、`first` 是全量清单的前缀（≤ 20 项）、`full_list_path` 等于 manifest 的 `changed_files_path`；校验器重读该文件并核对行数、摘要与前缀。需要全量清单时读 `changed-files.txt`，不要往报告里抄。

摘要形状只在超阈值时合法：清单不长却改用摘要形状，或 `count`/`first`/`digest` 与 manifest 对不上，都是校验错误。截断的只是读物，`diff_digest` 与 `changed_files_digest` 仍覆盖全部变更条目，范围判据不因压缩而变弱。

所有后续角色都必须回显 `diff_digest`。出现不同摘要时停止合并结果并重新冻结。

## Context Contract
<!-- SECTION: context-contract -->

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
<!-- SECTION: traceability-matrix -->

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
<!-- SECTION: impact-surface -->

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
<!-- SECTION: coverage-matrix -->

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
<!-- SECTION: candidate-finding -->

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
<!-- SECTION: evidence-verdict -->

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
<!-- SECTION: formal-finding -->

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
<!-- SECTION: open-question -->

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
<!-- SECTION: validation-record -->

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
<!-- SECTION: failure-attribution -->

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
<!-- SECTION: release-gate -->

```yaml
release_gate:
  status: READY | READY_WITH_CONDITIONS | NOT_READY | NOT_VERIFIED | NOT_APPLICABLE
  checks:
    - item: deployment_order | dependencies | configuration | feature_flag | rollout | stop_conditions | monitoring | observation_window | data_repair | rollback
      status: PASS | CONDITIONAL | FAIL | NOT_VERIFIED | NOT_APPLICABLE
      criticality: critical | non_critical
      evidence: [string]
      condition_or_gap: string | null
  blocking_reasons: [string]
  conditions: [string]
```

十项 `item` 在任何顶层 `status` 下都必须逐条出现（顶层 `NOT_APPLICABLE` 也不能省略清单），每项都要有非空 `evidence`，非 `PASS` 状态还必须有非空 `condition_or_gap`——`NOT_APPLICABLE` 同样要写"为什么不适用"，它不再是逃避 `NOT_VERIFIED` 门禁的出口。

顶层 `status` 按检查项的最高优先级裁决：存在 `FAIL` 必须 `NOT_READY`；否则存在 `critical` 的 `NOT_VERIFIED` 必须 `NOT_VERIFIED`；否则存在 `non_critical` 的 `NOT_VERIFIED` 必须 `READY_WITH_CONDITIONS`（要升级为整道 Gate 未核实也允许，此时为 `NOT_VERIFIED`）；否则存在 `CONDITIONAL` 必须 `READY_WITH_CONDITIONS`；全部 `PASS`/`NOT_APPLICABLE` 才允许 `READY`。低优先级检查项可以与 `FAIL` 共存于 `NOT_READY`，其条件与缺口写在各自的 `condition_or_gap` 中，不得为迁就顶层状态而改写检查项结果。

`critical` 的判定标准是"该缺口未核实是否会让发布决策本身不可信"：回滚不可执行、数据修复无方案、停止条件缺失属于 `critical`；观察窗口配置、灰度比例这类可交付后跟进的属于 `non_critical`。只有 `critical` 项的 `NOT_VERIFIED` 才会把结论推向 `BLOCKED`。

## Dispatch Ledger
<!-- SECTION: dispatch-ledger -->

`$RUN_DIR/dispatch-ledger.jsonl` 由 `record_review_dispatch.py` 逐条追加，一行一个 JSON 对象，只增不改：

```yaml
dispatch_record:
  step: integer                      # SKILL 步骤号（01-22）
  role: review-context | review-traceability | review-specialist | review-evidence | review-failure
  instance_id: string                # 与报告 coverage[].owner 使用同一个 ID
  input_diff_digest: sha256          # 必须是冻结快照的 diff_digest
  inputs:                            # 可选：本次真正交给该角色的中间契约（CW2-c）
    - path: absolute-path            # $RUN_DIR/steps/<nn>-<contract>.json 之一
      digest: sha256                 # 由 record_review_dispatch.py 读文件算出，不是转录值
  returned_status: COMPLETED | PARTIAL | FAILED
  started_at: utc-timestamp          # 2026-01-02T03:04:05Z
  ended_at: utc-timestamp
  recorded_at: utc-timestamp         # 脚本写入，用于事后核对登记时机
```

`inputs` 缺键是 CW2-c 之前的历史台账，合法；一旦声明就必须逐项成形状（绝对路径、不重复、64-hex 摘要），路径不存在时 `record_review_dispatch.py` 在写盘前以退出码 `2` 失败。`--check-worktree` 会额外重算每份声明过的输入文件的摘要，派发后契约被改写或删除即 `STALE_SNAPSHOT`——"把契约交给了角色"从此是可核对的，而不是自述。

校验器在发现 manifest 同目录的台账文件时自动核对：`REVIEWED`/`VALIDATED` 维度的 `owner` 必须出现在 `instance_id` 集合里（`local-validator` 除外）；同一 `role`+`instance_id` 不得重复派发；步骤 10 与步骤 18 的 `review-evidence` 实例必须互不重叠且 18 不早于 10；步骤 10 不早于步骤 08；每条记录的 `input_diff_digest` 必须等于报告 `snapshot.diff_digest`；台账条数不得超过 `MAX_DISPATCHES=14`（CW4-a 的派发预算，`record_review_dispatch.py` 在写入侧同样拒绝第 15 条）。台账缺失时这些核对跳过，交付说明必须如实写明未启用台账，不得把"跳过"当成"通过"。

## 规范化报告 JSON
<!-- SECTION: normalized-report-json -->

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
    "target_id": "...",              // 跨 SKILL 连接键，取自 manifest.target_id
    "diff_digest": "...",
    "changed_files": ["..."],        // 清单 >60 项时是 {count,digest,first,full_list_path} 摘要形状
    "staged_digest": "...",
    "unstaged_digest": "...",
    "untracked_digest": "...",
    "preexisting_changes": [],
    "documents": [],                  // 逐字复制 manifest.documents；无文档时固定为 []
    "review_scale": {                 // 逐字复制 manifest.review_scale；档位由脚本算出，不得改标
      "tier": "M",
      "changed_files": 12,
      "changed_lines": 480,
      "tier_limits": [[10, 300], [40, 1500], [150, 6000]]
    },
    "manifest_digest": "..."
  },
  "summary": "...",
  "findings": [],
  "open_questions": [],
  "traceability": [],
  "coverage": [],
  "validation": [],
  "release_gate": {
    "status": "READY_WITH_CONDITIONS",
    "checks": [
      {
        "item": "deployment_order",
        "status": "PASS",
        "criticality": "critical",
        "evidence": ["..."],
        "condition_or_gap": null
      },
      {
        "item": "observation_window",
        "status": "NOT_VERIFIED",
        "criticality": "non_critical",
        "evidence": ["..."],
        "condition_or_gap": "..."
      }
    ],
    "blocking_reasons": [],
    "conditions": ["..."]
  },
  "rejected_candidates": [],
  "limitations": [],
  "residual_risks": []
}
```

`checks` 示例只列了两项：实际负载必须逐条包含契约中列出的全部十个 `item`，骨架已预填清单，只需填 `status`/`criticality`/`evidence`/`condition_or_gap`。

详细数组元素分别遵循上方同名契约。`rejected_candidates` 的每项至少包含 `candidate_id` 和 `reason`；`limitations` 与 `residual_risks` 使用非空字符串。

`review_context` 是最终报告的有效降级上下文：`WAIVED` 必须得到 `BLOCKED`；`LITE` 或 `documents_status: NOT_AVAILABLE` 不得得到 `APPROVE`，并必须记录文档缺失限制或 Open Question。`snapshot` 必须逐字段复制 `freeze_review_snapshot.py` 生成的 manifest（`changed_files` 按上文两种形状之一，两种形状都由脚本预选），校验器核对 `manifest_digest`、三项来源指纹、`changed_files_digest` 和 `documents` 的清单形状，抬高手写 manifest 的成本，并可用 `--check-worktree` 重放工作树核对新鲜度。

步骤 22 的修复循环读校验器的 `--errors-json` 输出，形状固定为：

```json
{
  "valid": false,
  "gate_result": true,
  "filtered_by": null,
  "matched_errors": 2,
  "total_errors": 2,
  "errors": [
    {"path": "$.summary", "message": "must be a non-empty string"},
    {"path": "$.coverage", "message": "missing dimensions: [...]"}
  ]
}
```

`errors` 恒为**全量**（按 `": "` 首个分隔符切成 `path` 与 `message` 两段，与 stderr 逐条对应），`path` 就是报告负载里的 JSON path，可直接用于定位待修字段。`--only` 只过滤 stderr 视图，不过滤检查：带 `--only` 时 `gate_result` 恒为 `false`、`filtered_by` 列出前缀、`matched_errors` 是过滤后的条数，stderr 打印 `FILTERED: n of m error(s)` 且永不打印 `VALID:`，因此那一轮的结论无效，收尾必须再跑一次不带 `--only` 的完整校验。交付时把剩余错误按 `errors[].path` 逐条抄进 `blockers`。

## Markdown 交付结构
<!-- SECTION: markdown-delivery -->

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
