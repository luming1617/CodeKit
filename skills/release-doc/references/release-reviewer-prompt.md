# Release Plan Reviewer Sub-agent Prompt

> 本文件只是派发封套与本步约束；常设角色要求以 `agents/release-reviewer.md` 为唯一来源，评审范围、枚举与输出字段以 `references/release-review-contract.md` 为唯一事实源，此处不重复。

## 输入封套（主控逐项填写后随任务传入）

```text
review_id:        RPD-R<round>
review_round:     <1..5>
perspective:      COMPREHENSIVE（全量轮）
scale:            <S|M|L，与台账一致>
rpd_path:         <feature-directory>/release-doc.md 绝对路径
rpd_digest:       <取自 release_state.py digest 命令输出>
prd:              <feature-directory>/product-doc.md 绝对路径
tdd:              <feature-directory>/technical-doc.md 绝对路径
source_ledger:    <source_id + sha256 + 读取状态>
release_template: $SKILL_DIR/references/release-doc-template.md
check_standard:   $SKILL_DIR/references/check-standard.md
contract:         $SKILL_DIR/references/release-review-contract.md
previous_findings:<上一轮未闭环 Finding；第 1 轮为空数组>
SKILL_DIR:        <绝对路径>
输出语言规则:      <$SKILLS_ROOT/_shared/prompts/output-language.md 正文>
```

增量复核轮（delta）改用以下封套，不传全量 PRD/TDD/模板路径：

```text
review_id:        RPD-R<round>
review_round:     <2..4>
perspective:      RECHECK
scale:            <S|M|L，与台账一致>
rpd_path:         <feature-directory>/release-doc.md 绝对路径
rpd_digest:       <新稿指纹，取自 digest 命令>
previous_rpd_digest: <上一轮送审指纹>
open_findings:    <未闭环 Finding 清单，含证据与修复动作>
changed_sections: <被修改小节的完整文本与定位>
contract:         $SKILL_DIR/references/release-review-contract.md
SKILL_DIR:        <绝对路径>
输出语言规则:      <$SKILLS_ROOT/_shared/prompts/output-language.md 正文>
```

## 本步约束

1. 开始审查前先核对 `rpd_digest`：用只读命令 `release_state.py digest --rpd <rpd_path>` 的输出与封套比对（指纹含专有的回执区剔除与归一化，不得手工计算 sha256），不一致时立即返回 `BLOCKED`，不得继续审查旧稿。
2. 封套缺任一项、资料不可读或独立评审无法完成时返回 `BLOCKED` 并写明 `blocked_reason`；不得凭经验补齐输入。
3. `review_round` 不得大于 5；你不负责发起下一轮，也不得修改 RPD、执行发布或调用 `codekit:code-review`。
4. 严重度定级执行契约的「资料固有缺口分流」：已登记为 `【填写】`/`需人工补充` 且未伪装成已确认的固有缺口记 `Medium`（blocking=false）；不得把未登记缺口或无证据的“通过/成功”结论降级。
5. 没有发现问题也必须输出检查范围、证据覆盖和限制，`findings` 为空数组。
6. RECHECK 视角逐条判定 `open_findings` 是否已修复：确认修复的 id 写入 `verified_findings`（台账仍有未闭环项时 `APPROVE` 不得空确认）；修复引入新阻塞问题时给 `REQUEST_CHANGES`；不得把增量结论说成全量结论，不得自行发起下一轮。
7. 门停规则：只有 `Blocker`/`High`（blocking=true）能支撑 `REQUEST_CHANGES`；`Medium`/`Low` 一律作为非阻塞跟进项输出，不得据此要求开轮。
8. 规模路由：S 级免除不适用基础设施小节的逐行核对，但仍核对折叠理由与高风险字段；发现高风险信号（资金/账务/交易/资产=是、删表删字段、无标准回滚）而台账规模不足时，按 Blocker 上报，不得自行降级。

## 输出锚点

严格按契约「输出契约」字段树输出 JSON（`contract_version: release-review-v2`、`scope: release_plan_only`、`perspective` 与 `scale` 与封套一致）；完整意见放入 `appendix_markdown`，其中不得出现一级或二级标题（`# ` / `## `），小节用三级标题、加粗文本或列表表达。回执与归档由主控用脚本渲染，你不写任何文件。