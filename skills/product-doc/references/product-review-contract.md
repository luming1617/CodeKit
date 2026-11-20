# Product Review Contract

本契约是 `product-doc` 独立评审的**唯一状态与严重度事实源**。SKILL.md 与 `check-standard.md` 不得再定义其他结论枚举；需要人工表述时一律使用本文件的映射表。

适用范围：`product-doc` 的 `product-reviewer` Sub-agent 输出。不适用于 `codekit:code-review`（代码审查有自己的契约与报告校验器），也不得相互替代。

## 输入封套

每个 Reviewer 必须收到并原样回执以下冻结输入；缺任一项时**必须**返回 `BLOCKED`，不得凭经验补齐：

```text
review_id:        PR-R<round>-<perspective>，例如 PR-R1-NF；增量复核轮用 PR-R<round>-RC
review_round:     1..5（由主控递增，Reviewer 不得自行推定）
perspective:      COMPLETENESS | NON_FUNCTIONAL | COMPREHENSIVE | RECHECK
scale:            S | M | L
prd_path:         <feature-directory>/product-doc.md 绝对路径
prd_digest:       本轮被评审稿件正文的 sha256（不含文末「独立评审意见」回执区，一律取自 digest 命令）
source_ledger:    输入资料台账（source_id + 摘要 + 状态）
fact_ledger:      主 Agent 合并后的事实台账摘要
check_standard:   $SKILL_DIR/references/check-standard.md
template:         $SKILL_DIR/references/product-doc-template.md
previous_findings:上一轮未闭环 Finding（第 1 轮为空数组）
```

增量复核轮（`advance --mode delta` 开启）的封套只要求：未闭环 Finding 清单（含证据与修复动作）、被修改小节的完整文本、新旧 `prd_digest`；RECHECK Reviewer 不重读全量 PRD、不重跑完整 checklist，逐条判定 Finding 是否已修复，确认修复的写入 `verified_findings`，发现的新问题写入 `findings`。

`prd_digest` 与主控台账中当前稿件摘要不一致时，本轮评审无效，必须返回 `BLOCKED`（防止复用旧稿结论）。

指纹只覆盖正文：`review_state.py` 用 `content_digest()` 计算，追加或修订文末回执区不改变指纹，因此“只归档回执不改稿”不能用来推进轮次。

## 循环边界

`review_round` 由主控维护，从 1 开始，每启动一批新的独立 Reviewer 前递增。一次完整循环 = 「独立 Reviewer 审查 → 定向修改 → 主控核验稿件摘要」，**最多 5 个完整循环**：

- 轮次分两种模式：**全量（full）**按规模路由视角；**增量复核（delta）**只要求 1 个 `RECHECK` 视角，只复核未闭环 Finding 与被修改小节；
- 第 1 轮与最终交付轮必须是全量；增量复核不得连续进行（delta 轮的下一轮必须回到 full）；最后一轮不得是 delta；
- RECHECK 确认修复不引入新问题时给 `APPROVE`（此时台账未闭环项随后由 `close` 归档）；台账仍有未闭环项时 `APPROVE` 必须携带非空 `verified_findings`，不得空确认；发现修复引入新阻塞问题时给 `REQUEST_CHANGES`，下一轮强制全量；
- 存在增量复核报告时，`close --resolution fixed` 的 Finding 必须出现在其后某份 RECHECK 报告的 `verified_findings` 中，脚本会拒绝未经复核确认的自称修复；
- `review_round < 5` 且存在未闭环阻塞项 → 允许进入下一轮；
- `review_round == 5` 结束后仍有未闭环 `blocking: true` 的 Finding → 最终状态必须为 `BLOCKED`，**不得**再派发 Reviewer；台账由 `review_state.py advance` 在第 5 轮自动置为 `BLOCKED`；
- 任何一轮返回 `BLOCKED` → 立即进入阻塞交付；
- 判定谓词只能读 `review_state.py` 台账中的 `review_round`、轮次模式与 Finding 状态，不得由主控口头声明。

## 状态与严重度枚举

`status`（四态，唯一值域）：

| status | 含义 | 成立条件 |
|---|---|---|
| `APPROVE` | 通过，可交付 | 无任何 Finding，或仅有已闭环的历史 Finding |
| `APPROVE_WITH_FOLLOW_UPS` | 可交付但有非阻塞跟进 | 无 `blocking: true` 的 Finding，且存在非阻塞 Finding |
| `REQUEST_CHANGES` | 需修改 | 至少一条 `blocking: true` 的 Finding |
| `BLOCKED` | 无法完成评审 | 输入封套缺项、稿件摘要不一致、Reviewer 能力不可用、或本契约字段无法满足 |

`severity`（四档，全 Skill 唯一值域）：

| severity | 定义 | 默认 blocking |
|---|---|---|
| `Blocker` | 结论失真或文档不可用：把推断/示例写成已确认事实、必填章节缺失、需求变更覆盖历史、评审意见被删改 | `true` |
| `High` | 阻塞下游实现或验收：P0 功能无正常/边界/异常验收、编号冲突或悬挂引用、目标无可计算指标、权限/资金/隐私要求缺失 | `true` |
| `Medium` | 不阻塞当前交付但必须跟进：口径或负责人待确认、条件章节未写原因、埋点未追溯到目标 | `false` |
| `Low` | 表述与可读性问题：术语不一致、表格冗余、措辞含糊但不影响判定 | `false` |

`blocking` 由 Reviewer 显式填写，缺省按上表推导；`Blocker`/`High` 不得标为 `blocking: false`。

**资料固有缺口分流**：基线、目标值、负责人、截止日期等资料中天然不存在的信息，只要已按契约登记为 Q/`待补充` 且未伪装成已确认，一律记 `Medium`（非阻塞）；只有缺口未登记、被写成已确认、或导致下游无法追溯时才升 `High`/`Blocker`。文档自身的结构、逻辑与追踪缺陷仍按上表定级，不适用本分流。

## Finding 分类

`category` 只能取：`TEMPLATE`（结构与裁剪）、`EVIDENCE`（来源与可信度）、`TRACEABILITY`（编号与追踪）、`CONTENT`（业务/交互/设计逻辑）、`NON_FUNCTIONAL`（性能可用性安全隐私兼容本地化可访问性数据质量）、`METRICS`（指标埋点看板告警）、`ACCEPTANCE`（验收可执行性）、`CHANGE_HISTORY`（变更台账与历史）、`RISK`（依赖风险未决问题）。

## 输出契约

输出必须是 JSON，或与本字段树完全等价的 Markdown 列表；字段不得缺失、不得改名：

```text
review_id: <与输入封套一致>
contract_version: product-review-v1
review_round: <与输入封套一致>
perspective: <与输入封套一致>
scale: <与输入封套一致>
prd_digest: <与输入封套一致>
status: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
summary: <审查范围、实际依据的输入、结论>
findings:
  - id: PR-<序号>
    severity: Blocker | High | Medium | Low
    category: TEMPLATE | EVIDENCE | TRACEABILITY | CONTENT | NON_FUNCTIONAL | METRICS | ACCEPTANCE | CHANGE_HISTORY | RISK
    location: <章节号 + 表格/字段/编号>
    evidence: <资料或稿件中的事实依据；未发现问题时写"未发现依据">
    impact: <对需求确认、实现、验收或上线的影响>
    remediation: <可执行的修复动作；不得在 PRD 中新增业务事实>
    blocking: true | false
    verification: <修复后如何验证>
    first_seen_round: <整数>
verified_findings: <仅 RECHECK 视角：确认已修复的既有 Finding id 数组；其他视角省略或为空>
open_questions: <需要产品/业务决策的条目，引用 9.2 的 Q 编号>
prd_appendix_markdown: <可直接追加到 prd-reviews.md 的中文 Markdown 完整意见>
```

`prd_appendix_markdown` 只允许指出文档问题、引用 Finding ID 与位置，**不得**引入资料之外的新业务事实，也不得给出通过结论而无证据；不得包含一级或二级标题（`# ` / `## `），小节用三级标题、加粗文本或列表表达——prd-reviews.md 的节标题由 `render-report` 统一生成。完整意见由主 Agent 用 `render-report` 渲染后只追加到同目录 `prd-reviews.md`；PRD 文末只保留 `render-appendix` 生成的紧凑回执块（含指向 prd-reviews.md 的引用）。

## 人工评审结论映射

`check-standard.md` 的三档人工结论只用于人工把关，映射关系固定：

| 人工结论 | 本契约 status |
|---|---|
| 通过 | `APPROVE` |
| 需补充 | `APPROVE_WITH_FOLLOW_UPS` |
| 不通过 | `REQUEST_CHANGES`（证据本身不可得时为 `BLOCKED`） |

## 机器校验

主控必须把每个 Reviewer 的输出规范化为 JSON 后写入 `<feature-directory>/prd-review.json`，并通过台账脚本推进状态，而不是凭记忆判断轮次；派发封套的 `prd_digest` 一律取自 `digest` 命令输出，PRD 回执块由 `render-appendix` 生成、完整意见由 `render-report` 生成后分别只追加到 PRD 与 `prd-reviews.md`，不得手拼：

```bash
<PYTHON> "$SKILL_DIR/scripts/review_state.py" init --state <feature-directory>/prd-review.json \
  --prd <feature-directory>/product-doc.md --mode new|change --scale S|M|L --input <source-file>...
<PYTHON> "$SKILL_DIR/scripts/review_state.py" digest --prd <feature-directory>/product-doc.md
<PYTHON> "$SKILL_DIR/scripts/review_state.py" render-appendix --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/review_state.py" render-report --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/review_state.py" record --state <feature-directory>/prd-review.json \
  --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/review_state.py" close --state <feature-directory>/prd-review.json \
  --finding PR-001 --resolution fixed|deferred|rejected --evidence <说明> [--q-ref Q-001]
<PYTHON> "$SKILL_DIR/scripts/review_state.py" close --state <feature-directory>/prd-review.json \
  --batch <closures.json>
<PYTHON> "$SKILL_DIR/scripts/review_state.py" advance --state <feature-directory>/prd-review.json [--mode full|delta]
<PYTHON> "$SKILL_DIR/scripts/review_state.py" verify --state <feature-directory>/prd-review.json
```

`close --batch` 的清单是 JSON 数组，每项含 `finding/resolution/evidence`（deferred 另需 `q_ref`）；逐条校验与单条模式一致，任一条失败整批不落盘。

`verify` 未通过（退出码非 0）时不得进入交付，不得把 PRD 自检结论写为"已确认"。

## 禁止事项

- 不得由主 Agent 自检、普通串行复查或 `codekit:code-review` 替代本契约的独立 Reviewer 输出。
- 不得删除、改写或合并 Reviewer 的原始意见；PRD 回执块必须保留 `review_id`、`perspective`、`review_round` 与 `prd_digest`，完整意见必须只追加归档在 `prd-reviews.md`，两处都不得删改。
- 不得为了推进轮次把未闭环的 `Blocker`/`High` 降级为 `Medium`/`Low`；资料固有缺口分流只适用于定级时的判定，不适用于已登记 Finding 的事后降级。
- 不得在无 `evidence` 的情况下给出 `APPROVE`。
- 不得以增量复核轮作为最终交付轮；不得连续使用增量复核逃避全量审查。
- 同一 Reviewer 会话不得承担修复写入；修复只能由主 Agent 依据 Finding 协调 Writer 完成。
