# Release Plan Review Contract

本契约是 `release-doc` 独立发布方案评审的**唯一状态与严重度事实源**。SKILL.md、`check-standard.md`、`release-doc-template.md`、`agents/*.md` 与 `release-reviewer-prompt.md` 不得再定义其他结论枚举；需要人工表述时一律使用本文件的映射表。

本契约只适用于 `release-doc` 的独立发布方案评审，不替代 `code-review`，也不允许评审者审查代码实现。

## 输入封套

Release Plan Reviewer 必须收到并原样回执以下冻结输入；缺任一项时**必须**返回 `BLOCKED`，不得凭经验补齐：

```text
review_id:        RPD-R<round>，例如 RPD-R1；每轮只有一个全新独立 Reviewer
review_round:     1..5（由主控递增，Reviewer 不得自行推定）
perspective:      COMPREHENSIVE（全量轮） | RECHECK（增量复核轮）
scale:            S | M | L（与台账一致；S 级免除不适用基础设施小节逐行核对，仍核对折叠理由与高风险字段）
rpd_path:         <feature-directory>/release-doc.md 绝对路径
rpd_digest:       本轮被评审稿件正文的 sha256（不含文末「独立评审意见」回执区，一律取自 digest 命令）
prd:              <feature-directory>/product-doc.md
tdd:              <feature-directory>/technical-doc.md
source_ledger:    输入资料台账（source_id + sha256 + 读取状态，来自 release_state.py init）
release_template: $SKILL_DIR/references/release-doc-template.md
check_standard:   $SKILL_DIR/references/check-standard.md
previous_findings:上一轮未闭环 Finding（第 1 轮为空数组）
```

增量复核轮（`advance --mode delta` 开启）的封套只要求：未闭环 Finding 清单（含证据与修复动作）、被修改小节的完整文本、新旧 `rpd_digest`；RECHECK Reviewer 不重读全量 PRD/TDD/模板、不重跑完整 checklist，逐条判定 Finding 是否已修复，确认修复的写入 `verified_findings`，发现的新问题写入 `findings`。

`rpd_digest` 与主控台账中当前稿件摘要不一致时，本轮评审无效，必须返回 `BLOCKED`（防止复用旧稿结论）。

指纹只覆盖正文：`release_state.py` 用 `content_digest()` 计算，追加或修订文末回执区不改变指纹，因此“只归档回执不改稿”不能用来推进轮次。回执区按章节结构定位（与验证器同一标题判定：`#` 后必须有空格，且排除围栏）；正文中部的伪回执标题（含无空格变形）由验证器 `REVIEW005` 拒收，历史快照围栏里的旧回执标题不参与剔除，不得借伪标题在正文中制造指纹盲区。回执节必须是文档最后一章：其后只允许回执块标题，追加非强制章节由 `REVIEW006` 拒收，且该部分正文仍计入指纹、`verify` 同口径复查。

## 循环边界

`review_round` 由主控维护，从 1 开始，每启动一个新的独立 Reviewer 前递增。一次完整循环 = 「独立 Reviewer 审查 → Runbook Refiner 定向修改 → 主控核验稿件摘要」，**最多 5 个完整循环**：

- 每轮只接受一个全新只读 Reviewer 的一份报告；同一轮不得记录两次，不得复用上轮结论；
- 轮次分两种模式：**全量（full）**用 `COMPREHENSIVE` 视角读完整输入；**增量复核（delta）**只要求 1 个 `RECHECK` 视角，只复核未闭环 Finding 与被修改小节；
- 第 1 轮与最终交付轮必须是全量；增量复核不得连续进行（delta 轮的下一轮必须回到 full）；最后一轮不得是 delta；
- RECHECK 确认修复不引入新问题时给 `APPROVE`（此时台账未闭环项随后由 `close` 归档）；台账仍有未闭环项时 `APPROVE` 必须携带非空 `verified_findings`，不得空确认；发现修复引入新阻塞问题时给 `REQUEST_CHANGES`，下一轮强制全量；
- 存在增量复核报告时，`close --resolution fixed` 的 Finding 必须出现在其后某份 RECHECK 报告的 `verified_findings` 中，脚本会拒绝未经复核确认的自称修复；delta 轮内闭环必须先记录本轮 RECHECK 报告并获其确认，不得先闭环后记录；`verify` 交付前复查消费：提出后、闭环前存在 delta 轮的 fixed 必须被某轮 RECHECK 的 `verified_findings` 确认（闭环之后为其他 Finding 开的 delta 轮不追溯要求已闭环项）；
- `review_round < 5` 且存在未闭环阻塞项 → 允许进入下一轮；
- `review_round == 5` 结束后仍有未闭环 `blocking: true` 的 Finding → 最终状态必须为 `BLOCKED`，**不得**再派发 Reviewer；台账由 `release_state.py advance` 在第 5 轮自动置为 `BLOCKED`；
- 任何一轮返回 `BLOCKED` → 立即进入阻塞交付；
- 判定谓词只能读 `release_state.py` 台账中的 `review_round` 与 Finding 状态，不得由主控口头声明。

## 评审范围

只检查以下发布方案属性：

1. 模板章节、表头、状态值和条件章节是否完整且顺序一致；
2. 服务、产物、接口、基础设施、依赖和业务影响是否有来源证据；
3. 基础设施前置操作、服务发布顺序、灰度/滚动策略是否可执行且前后一致；
4. 发布前检查、发布后验证、观察指标、基线、阈值、时长和责任人是否覆盖；
5. 失败停止、隔离、降级、恢复、应用回滚、配置/数据/消息/任务回滚是否可执行；
6. 未确认信息是否被错误写成通过、正常、成功、已发布或不涉及；
7. 高风险业务、不可逆变更和无标准回滚路径是否被正确阻塞或升级。

明确不在范围内：代码正确性、代码 Diff、测试实现质量、架构设计重审、真实发布操作、外部系统修改和 `codekit:code-review` 调用。

## 规模与裁剪

规模由 `release_state.py init --scale` 的探针机械判定最低严格度，声明只能上调：命中资金/账务/交易链路/用户资产任一为“是”（带注解的肯定回答如“是（涉及订单金额）”同样计信号）、不可逆迁移/删表删字段（SQL 信号不限围栏；围栏内 SQL 去注释后整体扫描，DROP/**/TABLE、跨行书写与 TRUNCATE 省略 TABLE 形态同样命中）、无标准回滚路径任一信号，强制 L 级全矩阵；否定豁免只作用于短语所在子句（含括号/冒号切分），同行其余子句的真实信号照常计入。风险字段取值与验证器同口径：只认关键信息的项目/内容表且 first-wins，重复风险行由 `KEY003` 拒收、探针按未决 fail-closed；服务数取含服务名称表头表格的最大行数。S 级（单服务、无适用基础设施、风险全否）章节只折叠不删除：不涉及的基础设施小节与依赖变更可折叠为单行 `不涉及 + 原因`，发布策略只注入命中的一套；折叠理由与高风险字段仍须审查。Chaos 必查模式由台账推导（M/L 全 11 模式；S 按适用基础设施剪枝），经 `chaos` 命令入台账，Critic 不得自行缩小；Chaos 报告必须携带与当前稿一致的 `rpd_digest`，其 findings 与评审 Finding 走同一闭环台账（Blocker/High 未闭环同样禁止交付）。`verify` 重跑探针：正文风险信号超出台账规模、RPD 发布规模行与台账不一致、Chaos 未入台账或覆盖度指纹与交付稿不一致，均不得交付；交付轮始终全量。

## 状态与严重度枚举

`status`（四态，唯一值域）：

| status | 含义 | 成立条件 |
|---|---|---|
| `APPROVE` | 通过，可交付 | 无任何 Finding，且台账无未闭环项 |
| `APPROVE_WITH_FOLLOW_UPS` | 可交付但有非阻塞跟进 | 无 `blocking: true` 的 Finding，且存在非阻塞 Finding 或已登记跟进项 |
| `REQUEST_CHANGES` | 需修改 | 至少一条 `blocking: true` 的 Finding（仅 `Blocker`/`High`）；`Medium`/`Low` 不触发开轮，走非阻塞跟进 |
| `BLOCKED` | 无法完成评审 | 输入封套缺项、稿件摘要不一致、Reviewer 能力不可用、或本契约字段无法满足 |

`severity`（四档，全 Skill 唯一值域）：

| severity | 定义 | 默认 blocking |
|---|---|---|
| `Blocker` | 结论失真或方案不可用：把推断/示例写成已确认发布事实、必填章节缺失、需求变更覆盖历史、高风险变更被错误降级放行 | `true` |
| `High` | 阻塞发布执行或回滚：发布顺序与依赖矛盾、适用验证缺失、回滚路径不可执行、无证据的“通过/成功”结论 | `true` |
| `Medium` | 不阻塞当前交付但必须跟进：已登记的待人工补充字段、条件章节未写原因、观察阈值待确认 | `false` |
| `Low` | 表述与可读性问题：术语不一致、表格冗余、措辞含糊但不影响判定 | `false` |

`blocking` 由 Reviewer 显式填写，缺省按上表推导；`Blocker`/`High` 不得标为 `blocking: false`。

**资料固有缺口分流**：MR、构建产物版本、负责人、监控阈值/基线、发布窗口、依赖 SLA 等资料中天然不存在的信息，只要已按模板登记为 `【填写】`/`需人工补充` 且未伪装成已确认，一律记 `Medium`（非阻塞），走 `APPROVE_WITH_FOLLOW_UPS` 跟进，不据此单独开轮（门停谓词由 `release_state.py record` 机械执行）；只有缺口未登记、被写成已确认、或导致下游无法追溯时才升 `High`/`Blocker`。方案自身的结构、顺序、验证与回滚逻辑缺陷仍按上表定级，不适用本分流。

## Finding 分类

`category` 只能取：`TEMPLATE`（结构与章节）、`EVIDENCE`（来源与可信度）、`ORDER`（依赖与发布顺序）、`VERIFICATION`（发布前检查与发布后验证）、`OBSERVATION`（观察指标、基线与阈值）、`ROLLBACK`（停止与回滚可执行性）、`RISK`（高风险范围与遗留风险）。

## 输出契约

输出必须是 JSON，或与本字段树完全等价的 Markdown 列表；字段不得缺失、不得改名。主控负责规范化为 JSON 后交 `release_state.py record`：

```text
review_id: <与输入封套一致>
contract_version: release-review-v2
review_round: <与输入封套一致>
perspective: <与输入封套一致：COMPREHENSIVE | RECHECK>
scale: <与输入封套一致：S | M | L>
rpd_digest: <与输入封套一致>
status: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
scope: release_plan_only
summary: <评审范围、证据覆盖和结论>
findings:
  - id: RPR-<序号>
    severity: Blocker | High | Medium | Low
    category: TEMPLATE | EVIDENCE | ORDER | VERIFICATION | OBSERVATION | ROLLBACK | RISK
    location: <章节、表格、步骤或字段>
    evidence: <输入中的事实依据；无依据时明确写未发现>
    impact: <对发布、人、数据、可用性、安全或成本的影响>
    remediation: <可执行修复或人工补充>
    blocking: true | false
    verification: <修复后的验证方式>
    first_seen_round: <整数>
verified_findings: <仅 RECHECK 视角：确认已修复的既有 Finding id 数组；其他视角省略或为空>
open_questions:
  - id: RQ-<序号>
    question: <待人工确认事项>
    impact: <影响的放行判断>
    owner: <未知时写需人工指定>
blocked_reason: <仅 status=BLOCKED 时必填>
appendix_markdown: <可直接追加到 release-reviews.md 的中文 Markdown 完整意见>
```

`appendix_markdown` 只允许指出方案问题、引用 Finding ID 与位置，**不得**引入资料之外的新发布事实，也不得给出通过结论而无证据；不得包含一级或二级标题（`# ` / `## `），小节用三级标题、加粗文本或列表表达——release-reviews.md 的节标题由 `render-report` 统一生成。完整意见由主 Agent 用 `render-report` 渲染后只追加到同目录 `release-reviews.md`；RPD 文末只保留 `render-appendix` 生成的紧凑回执块（含指向 release-reviews.md 的引用）。

## 人工评审结论映射

`check-standard.md` 的人工结论只用于人工把关，映射关系固定：

| 人工结论 | 本契约 status |
|---|---|
| 通过 | `APPROVE` |
| 需人工补充 | `APPROVE_WITH_FOLLOW_UPS` |
| 请求修改 | `REQUEST_CHANGES` |
| 不通过 | `REQUEST_CHANGES`（证据本身不可得时为 `BLOCKED`） |
| 阻塞 | `BLOCKED` |

## 机器校验

主控必须把 Reviewer 的输出规范化为 JSON 后通过台账脚本推进状态，而不是凭记忆判断轮次；派发封套的 `rpd_digest` 一律取自 `digest` 命令输出，RPD 回执块由 `render-appendix` 生成、完整意见由 `render-report` 生成后分别只追加到 RPD 与 `release-reviews.md`，不得手拼：

```bash
<PYTHON> "$SKILL_DIR/scripts/release_state.py" init --state <feature-directory>/release-review.json \
  --rpd <feature-directory>/release-doc.md --mode new|change --scale S|M|L --input SRC-001=<已落盘资料路径>...
<PYTHON> "$SKILL_DIR/scripts/release_state.py" chaos --state <feature-directory>/release-review.json \
  --report <chaos-coverage.json>
<PYTHON> "$SKILL_DIR/scripts/release_state.py" digest --rpd <feature-directory>/release-doc.md
<PYTHON> "$SKILL_DIR/scripts/release_state.py" render-appendix --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/release_state.py" render-report --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/release_state.py" record --state <feature-directory>/release-review.json \
  --report <normalized-report.json>
<PYTHON> "$SKILL_DIR/scripts/release_state.py" close --state <feature-directory>/release-review.json \
  --finding RPR-001 --resolution fixed|deferred|rejected --evidence <说明> [--q-ref RQ-001]
<PYTHON> "$SKILL_DIR/scripts/release_state.py" close --state <feature-directory>/release-review.json \
  --batch <closures.json>
<PYTHON> "$SKILL_DIR/scripts/release_state.py" advance --state <feature-directory>/release-review.json [--mode full|delta]
<PYTHON> "$SKILL_DIR/scripts/release_state.py" patch --state <feature-directory>/release-review.json \
  --code STRUCT002|STATUS003 [--line <n> --old <原单元格> --new <归一化后>]
<PYTHON> "$SKILL_DIR/scripts/release_state.py" verify --state <feature-directory>/release-review.json
```

**机械补丁白名单（P1-2）**：`patch` 只接受 `validate_release_doc.py` 错误码中的 `STRUCT002`（章节顺序：脚本确定性重排，块内容逐字节不变含 CRLF 行尾，主控不提供新文本；重复章节 STRUCT004 须先去重，重排拒收）与 `STATUS003`（状态枚举空白归一化：去空白后必须等于新值且新值在合法枚举内，不得改换结论）。门禁必须实际报出该错误码（STATUS003 还须点名目标行），补丁不得引入新缺陷，每处直修写入台账 `mechanical_patches` 并在交付报告照抄。白名单外的缺陷保持原价：Refiner 定向修改 + 新一轮独立评审。补丁发生在最后一轮评审之后时，`verify` 的指纹绑定会强制补一轮全量评审；交付轮始终是全量独立评审。

`close --batch` 的清单是 JSON 数组，每项含 `finding/resolution/evidence`（deferred 另需 `q_ref`）；逐条校验与单条模式一致，任一条失败整批不落盘。`fixed` 必须在 `advance` 到下一轮之后闭环，且交付前必须有看到新稿件的复核轮次；Chaos findings 登记为上一轮提出（评审前=0），修复后可在本轮闭环，但交付前仍须有看到新稿件的全量评审轮；`deferred` 的 `--q-ref` 必须按词边界匹配到 RPD 遗留问题节（围栏外）或台账中已登记的 RQ 编号，前缀碰撞与历史快照里的旧编号不算登记。

`verify` 未通过（退出码非 0）时不得进入交付，不得把发布方案自检结论写为“已确认”或宣称可放行。

## 禁止事项

- 不得由主 Agent 自检、普通串行复查或 `codekit:code-review` 替代本契约的独立 Reviewer 输出。
- 不得删除、改写或合并 Reviewer 的原始意见；RPD 回执块必须保留 `review_id`、`review_round` 与 `rpd_digest`，完整意见必须只追加归档在 `release-reviews.md`，两处都不得删改；回执节必须是文档最后一章，不得在其后追加章节藏匿未经评审的发布口径。
- 不得为了推进轮次把未闭环的 `Blocker`/`High` 降级为 `Medium`/`Low`；资料固有缺口分流只适用于定级时的判定，不适用于已登记 Finding 的事后降级。
- 不得在无 `evidence` 的情况下给出 `APPROVE`。
- 不得以增量复核轮作为最终交付轮；不得连续使用增量复核逃避全量审查；不得在台账仍有未闭环项时由 RECHECK 空确认 `APPROVE`；不得抢在 RECHECK 报告记录前自称 fixed 闭环，不得用正文中部的伪回执标题制造指纹盲区。
- 不得绕过 `patch` 通道手改正文，不得借机械补丁夹带语义修改，不得省略补丁台账；机械补丁不豁免交付轮的全量独立评审。
- 不得下调规模绕过探针或把高风险字段伪报为否；不得用重复风险行、异表头诱饵表或空服务诱饵表降级规模；章节只折叠不删除；不得缩小 Chaos 必查范围或绕过 `chaos` 门禁交付；正文变更后不得拿旧稿的 Chaos 覆盖度冒充交付稿的审查；历史快照围栏必须至少 4 个标记字符（HIST002），不得用同长度围栏嵌套快照。
- 评审者不得把“无法访问”转换为“通过”，不得删除问题来降低严重度，不得输出代码审查结论。
- 同一 Reviewer 会话不得承担修复写入；修复只能由主 Agent 依据 Finding 协调 Runbook Refiner 完成。