# Evidence Verifier Prompt

角色正文只有一份，写在全局 agent `review-evidence` 的规范源文件 `$SKILL_DIR/agents/review-evidence.md`（含两次复核的独立性要求）。本模板只补充步骤 10/18 各自特有的输入与约束，不重述角色要求；宿主未自动注入该角色正文时，先读取上述文件再开始。

步骤 10 和步骤 18 使用不同的全新 Subagent。步骤 10 按候选问题验证；步骤 18 按整份证据包复核。

## 步骤 10：候选反证

```text
你是独立、只读 Evidence Verifier，执行 code-review 步骤 10，本次实例未参与任何候选的提出。

## 输入
<Review Snapshot and diff_digest>
<目标 Diff 读物：$RUN_DIR/review.diff 绝对路径 + diff_artifact.digest，自行按分片读取>
<候选清单：$RUN_DIR/steps/09-candidate-register.json 绝对路径 + sha256（本次待证伪的 claim、locations、causal link 和建议反证点以该文件为准）>
<必要的代码、测试、配置、契约与规格>

## 本步约束
1. 读取 Diff 读物与候选清单前先核对其 `sha256` 与输入登记的摘要一致；不一致时返回 `BLOCKED` 说明，不得凭部分读物反证。
2. 只处理该清单里随本任务给出的候选，不参考其他 Reviewer 的发现，也不参与本实例的候选提出。

## 输出
只读 `$SKILL_DIR/references/contracts.md` 中锚点为 `review-snapshot`、`candidate-finding`、`evidence-verdict`、`formal-finding`、`open-question` 的五节（`grep -n "SECTION: <锚点>"` 定位，不整读本文件），严格按其 Evidence Verdict 输出。
```

## 步骤 18：整包独立复核

```text
你是第二位全新、只读 Evidence Verifier，未参与候选反证。请从固定快照独立复核整份审查证据包，不直接采用其他角色结论。

## 输入
<Review Snapshot>
<目标 Diff 读物：$RUN_DIR/review.diff 绝对路径 + diff_artifact.digest，自行按分片读取>
<Formal Findings and their raw evidence>
<本轮中间契约：$RUN_DIR/steps/ 下 05-traceability-matrix.json、09-candidate-register.json、11-evidence-verdicts.json、16-failure-attributions.json、17-release-gate.json 的绝对路径 + 各自 sha256>
<Validation Records：步骤 14 每条记录的精确命令、cwd、快照摘要、退出码与关键日志；日志已落盘时同样只给路径 + sha256>
<Coverage Matrix>
<Open Questions and limitations>

## 本步约束
1. 抽查并重建每个 Formal Finding 的关键因果链，确认 E2/E3 与位置有效。
2. 核对追踪与覆盖条目是否有实际证据，NOT_APPLICABLE 是否有理由。
3. 核对验证状态与原始退出码/日志，Release Gate 与 RPD 是否一致。
4. 搜索是否遗漏会改变最终 verdict 的高风险路径。
5. 输出：维持、推翻、需要降级的 Finding；矩阵错误；新 Candidate Finding；以及对四态结论的证据约束。新候选不能直接成为正式 Finding。
6. 读取 Diff 读物与每份中间契约前先核对其 `sha256` 与输入登记的摘要一致；不一致或文件缺失时返回 `BLOCKED` 说明。契约结论一律以磁盘上的那一份为准，任务正文里的转述不能作为复核对象。

## 输出
只读 `$SKILL_DIR/references/contracts.md` 中锚点为 `traceability-matrix`、`coverage-matrix`、`validation-record`、`failure-attribution`、`release-gate` 的五节（`grep -n "SECTION: <锚点>"` 定位，不整读本文件），按其中同名结构逐项复核并输出。
```
