# 控制器命令契约（`scripts/tech_doc_controller.py`）

主控只需要这一页。每条命令给：必填参数、一条可复制的命令、以及它拒绝时的语义——拒绝语义只在这一页说一次，`SKILL.md` 的命令表只留用途与该步需要主控拿的主意，两边各抄一份就会互相漂移。

**不要在 run 中途回读控制器源码去凑参数**：那是一份四千余行的实现，而下面 23 条命令的必填项已经列全。上一次运行里这类回读发生了 14 次，每次都是同一个问题（“这一步还缺什么”），而 `show --next` 就是为这个问题准备的答复。

## 统一约定

- 调用形式：`<PYTHON> "$SKILL_DIR/scripts/tech_doc_controller.py" <子命令> …`。下文示例省略该前缀，写成 `CTRL …`；`<PYTHON>` 与 `$SKILL_DIR` 按 `skills/_shared/prompts/path-convention.md` 解析为字面量后替换，一个 run 只解析一次。
- `--state` 一律填 `init` 在 stdout 打印的 `STATE=` 那一行里的绝对路径；不要用 `$(...)`/`$PWD` 去接。
- 退出码只有两种结局：`0` 通过（含 `locate` 的 `NOT_LOCATED`——它是一个答案，不是门禁失败）；`2` 拒绝，`BLOCKED: <原因>` 在 stderr，stdout 可能仍有 JSON（`gate` 的报告、批量面的逐条结果）。
- 成功输出是 JSON：单行为主，`gate`/`show`/`confirmations`/`record-findings`/`record-fixes` 为缩进 JSON。字段含义见 `SKILL.md` 的命令表，本文件不重复。
- 需要“把一批东西一次交回去”的只有两处：Finding 清单与处置清单，见 `record-findings`/`record-fixes`；其余命令都是单条语义。

## 阶段 0：输入冻结

```text
CTRL init --repo-root "<仓库根绝对路径>" --feature-directory "<feature 目录绝对路径>" --run-id "<run id>"
```

可选：`--context-window <tokens>`、`--review-stop-policy severity|fixed`、`--scale-enforcement enforce|record-only`。feature 目录已有 `technical-doc.md` 即为 `UPDATE` 运行；确认文件损坏会使 `init` 失败且不留半个运行目录。

```text
CTRL record-source --state "<state>" --source-id SRC-1 --kind PRD --locator "<磁盘上真实存在的路径>" \
  --status READ --revision "<版本>" --summary "<一句话>" --confidence HIGH
```

`--kind` 取 `PRD|PRD_CHANGELOG|REPOSITORY_RULES|SYSTEM_EVIDENCE|SKILL_RESOURCE|HUMAN_DECISION|EXISTING_TDD`；指向文件的类别（除 `HUMAN_DECISION`/`PRD_CHANGELOG`）会被磁盘核对，声明与磁盘不符即拒（`NOT_FOUND`/`NOT_ACCESSIBLE` 也要如实登记，那是降级判定的依据）。

```text
CTRL finalize-inputs --state "<state>" --status PASS --reason "<依据>"        # UPDATE 运行再加 --prd-changes "3/1/0"
```

`PARTIAL` 用于“有资料读不到但足以继续”；`--prd-changes` 是修改/新增/删除三类行数，`UPDATE` 不给即拒。顺带跑规模探针，返回值里的 `scale`/`signals`/`greenfield`/`basis` 原样交给 Context Loader 与 Architect。

## 阶段推进与派发证据

```text
CTRL start-stage --state "<state>" --to CONTEXT
CTRL show --state "<state>" --next        # 卡住时先看这一条：它回的就是下一步该跑的完整命令
```

`--to` 取 `CONTEXT|ARCHITECT|CRITIC|REFINER|REVIEW|DELIVERY|CLOSED`，只允许相邻推进；两处回退都指向 `REFINER`：`REVIEW --to REFINER` 是 `REQUEST_CHANGES` 的正常下一轮，判据是**台账上还有没有未登记处置的 Finding**（往轮遗留的也算，不要求是本轮 Reviewer 报的——曾经要求过，结果是带着旧 Finding 进终审的 run 三面碰壁：`--to DELIVERY` 拒未闭环、`--to REFINER` 说不是本轮报的、`record-fix` 说不在 `REFINER` 阶段）；`DELIVERY --to REFINER` 是落地回退，只受轮次预算约束（见“交付”一节）。`--to DELIVERY` 在任何一条 Finding 没有处置时拒绝，报错同时点名两条出口：本轮新报的非阻断项当场 `record-fix` 收口，`must_fix` 与往轮遗留项先回 `REFINER`。证据不足即 `BLOCKED`，报错会点名缺哪个角色的哪个产物。`--review-scope full|incremental` 只用于 `REVIEW`，且只能复述控制器门停后自己推导的范围。

```text
CTRL record-agent --state "<state>" --role tech-refiner --name "<实例名>" --session-id "<会话 id>" \
  --input-digest "sha256:…" --status DISPATCHED --scope "7.2,4.1"
CTRL record-agent --state "<state>" --role tech-refiner --name "<实例名>" --session-id "<会话 id>" --status COMPLETED
```

开窗用 `DISPATCHED`（必填 `--input-digest`），收口用同一 `--session-id`（`--input-digest` 可省略）；什么都没产出也必须用 `ABORTED` 收口，否则时间账不闭合。子代理按 `CONFIRM_NEEDED` 提前收口（见 `SKILL.md`“人工确认循环”）时**收口状态是 `COMPLETED`**：它落了部分产物并交回了确认条目，那是下一阶段要查的证据；`CONFIRM_NEEDED` 只是回报里的约定，控制器状态集合里没有这一项，标成 `ABORTED` 等于宣布那半份草稿不存在，主控就会被推回去重做已完成的一半。角色确实做不了这件事（缺输入、缺派发能力）才用 `BLOCKED`。

`--scope` 只在开窗那次声明，读取量由索引算出。**`tech-refiner` 的派发必须带 `--scope`**——它是唯一会改 TDD 正文的角色，而“处置只能落在本轮所有 `--scope` 并集内”这条门禁用的就是这份声明；缺声明的 Refiner 派发在开窗时即被拒绝。整篇注入时写 `--scope all`：合法、按整篇度量、照样回报 `over_budget`，不是豁免通道，也不能与章节号混写（`all, 7.2` 被拒——混写只会让度量看起来比实际小）。`ABORTED` 的派发不计入那个并集：中断的派发没有改过正文，把它读过的章节算进范围，等于允许另一条处置落在它没落过的地方。只读角色不强制声明（Critic 与 Reviewer 本就是全量审查）。

Reviewer 占的是**名额**而不是会话：一个实例一旦以 `DISPATCHED` 开窗，同一轮的第二个 `tech-reviewer` 开窗就会被拒（“a tech-reviewer instance already ran in round N”），无论第一个有没有产出。中断不烧掉这一轮：对那个 `--session-id` 用 `ABORTED` 收口即释放名额，同一轮可以重派一个全新实例——丢连接时不必把整轮降级成 `degrade`。被拒的从来只是同一轮的第二个并行 Reviewer。

```text
CTRL write-artifact --state "<state>" --kind draft --file "<该角色回报的文件绝对路径>"
```

`--kind` 取 `context-pack|draft|critic-findings|refiner-map|review-report`；`--role`/`--round` 只在需要覆盖默认（当前阶段角色、当前 `review_round`）时才给。产物必须已经由对应角色 `COMPLETED`，主控不能先写产物再补派发记录。**产物文件由角色自己写盘并回报路径，主控只 `--file` 登记那个路径**（只读角色带 `Write` 就是为了让它落自己的产物，`Write` 不授权它改 TDD 或任何仓库文件）——主控把子代理正文重新誊一遍，既会漂移又会把整段吞进自己的上下文。`draft` 与 `refiner-map` 是多两件事的那两类——它们是唯二会改动 TDD 正文的角色（其余角色也写文件，写的是各自的产物，不碰 TDD）：正文里存在手抄的 Finding 表（生成台账标记之外的 `|` 行，其最近的表头同时具备“Finding / CRITIC / REVIEW”一列与处置台账的那组列名）即 `BLOCKED`，不等交付；模板自带的人工评审表（如 `评审人意见`：序号/意见/级别/提出人/负责人/处理结论/状态）不是镜像，它没有引用 Finding ID 的那一列，检测按表头而不是按“有没有出现『处理结论』字样”判定；`draft` 还把此刻的正文行数冻结成本次运行的体积参照（`UPDATE` 运行的参照在 `init` 读入旧版时就已定，不会被草稿改写）。

## Finding 与处置

```text
# 单条
CTRL record-finding --state "<state>" --source CRITIC --id CRITIC-001 --severity High \
  --location "9.3" --evidence "<问题与证据>"
# 整份报告：Critic/Reviewer 的回报原文即可注册，见下节 marker 格式
CTRL record-findings --state "<state>" --from-file "<回报文件>"
```

`--source` 取 `CRITIC|REVIEWER`，ID 形式分别是 `CRITIC-<NNN>` 与 `REVIEW-<轮次>-<NNN>`；`--must-fix` 省略时由严重度推导（`Blocker`/`High` 即 must_fix）。批量面只是运输方式，每条仍走同一套校验：已同值登记入 `already_registered`（重跑整份报告是安全的），同 ID 不同严重度/位置则进 `failed` 并整批退出码 `2`——分歧必须被看见，不能被后写的覆盖。

```text
CTRL record-fix --state "<state>" --finding-id CRITIC-001 --status FIXED --location "9.3" --change "<改了什么>"
CTRL record-fixes --state "<state>" --from-file "<Refiner 回报文件>"
```

`--status` 取 `FIXED|PENDING_CONFIRMATION|REJECTED`（后两者不得用于 must_fix 条目）。阶段规则不因批量而放松：常规入口是 `REFINER` 阶段，且落点必须落在本轮某个 `--scope` 并集内（并集只由 `COMPLETED` 的派发构成）。两条登记命令都先做镜像检测：处置声称“正文已改”，所以此刻正是手抄台账唯一可能被写进来的时刻，TDD 里有 `|` 行在生成块之外引用 Finding ID 即整条拒绝（判据是表头两半同时命中，模板的人工评审表不误伤，见 `write-artifact`；`--from-file` 对整批只判一次并报一个原因，不会 20 条 `failed` 复读同一句话）。

```text
CTRL record-review --state "<state>" --status APPROVE --report "<终审报告路径>" \
  --tdd-file "<TDD 绝对路径>" --session-id "<本轮 Reviewer 会话 id>"
```

结论与被审内容绑定：`--tdd-file` 取当前正文快照，`--session-id` 必须是本轮已 `COMPLETED` 的 Reviewer。`degrade --role <角色> --reason "<为何派发不了>"` 是降级登记口，登记过才可能在**无 `failures`** 时把 `gate` 的结论从 `PASS` 换成 `PARTIAL_DEGRADED`；降级不影响其余检查是否通过。

## 人工确认

```text
CTRL sync-confirmations --state "<state>" --file "<feature 目录>/confirmation-log.md"
CTRL skip-to-default --state "<state>" --conf-id CONF-007 --by "<用户原话>"      # 仅显式指令
CTRL mark-confirmation-applied --state "<state>" --conf-id CONF-007 --location "7.2" --summary "<落在哪、改了什么>"
CTRL confirmations --state "<state>"                                            # 只读对账
```

`sync-confirmations` 是唯一分配 `CONF-<NNN>` 的地方，退出码非零 + `blocking_open` 清单就是“还没确认完”的判定；阶段限制：sync 只在 `ARCHITECT/REFINER/REVIEW` 可跑，`mark-confirmation-applied` 只在 `ARCHITECT/REFINER` 可跑（阻断项要在 `start-stage --to CRITIC` 之前落地；已进 `DELIVERY` 的决定要先用落地回退回到 `REFINER`，见“交付”）。条目格式与暂停/恢复协议见 `SKILL.md`“人工确认循环”。

## 交付

```text
CTRL archive-history --state "<state>" --version "v1.0"          # UPDATE 运行必须在覆写前调用
CTRL render-ledger --state "<state>" --tdd-file "<TDD 绝对路径>"
CTRL gate --state "<state>" --tdd-file "<TDD 绝对路径>"
```

`render-ledger` 重写第 16 章的生成台账（含历史版本索引）并顺带重建索引；台账只能由它生成，手抄会被 `ledger-matches-state` 拒（前移到 `record-fix`/`write-artifact` 后，多数镜像在入账前就被拒了，这里兜的是漏到交付前的那一份）。`gate` 的 `BLOCKED` 报告 `failures` 逐条点名；`status` 的推导只有一条路——**`failures` 非空即 `BLOCKED`（退出码 2），降级不参与这一步**，`degrade` 只把一次干净的门禁从 `PASS` 改名成 `PARTIAL_DEGRADED`：它照样算通过，但交付报告必须把降级角色如实列出来，且不得写成“已通过独立评审”。降级掩盖真实失败的那条路已经封掉，因为它是全 skill 里唯一一处“退出码 0 但结论不成立”。

`gate` 报告里 `warnings` 与 `failures` 是两套东西：`warnings` 只收 `ok: true` 但带 `warn: true` 的软检查，永不改变 `status`，也永远不是 `BLOCKED` 的理由。目前只有一项 `document-growth`——正文（不含生成台账）相对冻结参照增长超过 `GROWTH_WARN_PERCENT`（8%）且本轮 Finding 里没有 `Blocker`/`High`，也就是“这一轮涨的字数没有阻断项替它买单”。它存在的意义是让每轮灌水变成一个被记录下来的代价，因为角色每多读 15% 就是每一轮多花 15% 的时间；交付报告照抄，不需要主控解释成通过或不通过。

进入 `DELIVERY` 的顺序是固定的：`render-ledger`（先让第 16 章的生成块存在，否则 `gate` 必拒）→ `gate` → 通过后再 `render-ledger` 一次（把门禁结论刷进台账，`ledger-matches-state` 要求台账不早于门禁）→ `start-stage --to CLOSED`。`show --next` 就是按这个顺序回命令的。

**落地回退**：`start-stage --to DELIVERY` 返回的 `pending_landing` 是“人类已经答完、正文里还没有”的条目。要落它们不必重放前面的阶段——`DELIVERY` 允许退回 `REFINER`；这条回退并不只服务于它们，任何“必须改正文才能收口”的门禁失败（例如 `scale-chapters` 太薄、`project-structure-branch` 走错分支）也只有这一条合法出路：

```text
CTRL start-stage --state "<state>" --to REFINER                 # 落地回退，返回值列出回退理由与代价
CTRL record-fix --state "<state>" --finding-id REVIEW-1-002 --status FIXED --location "13" --change "<按决定改了什么>"
CTRL mark-confirmation-applied --state "<state>" --conf-id CONF-007 --location "13" --summary "<落在哪、改了什么>"
CTRL start-stage --state "<state>" --to REVIEW                  # 回退的那轮必须补一次全新终审
```

它的代价是死的、理由是活的：不再审查“为什么要回退”，只审查“付不付得起”——轮次预算已耗尽时回退被拒，挂账交付是那几条唯一合法的出口。代价本身由状态机强制：`mark-confirmation-applied` 仍在 `REFINER` 阶段，`--to REVIEW` 在任何一条已收口决定未落地前拒绝，落地仍受本轮 `--scope` 并集约束，`--to DELIVERY` 只认新轮次的结论，所以回退的标价是一整轮终审——放宽的是理由，不是审计强度。`show --next` 在 `DELIVERY` 的每一步都会把这些条目和这一条备选命令一起回给你（`rewind`），但它不替你决定值不值。

## 定位与回读

```text
CTRL index --state "<state>"                                                   # 通常不必手动跑
CTRL locate --state "<state>" --finding CRITIC-007                             # 单条：含被编号引用的章节
CTRL locate --state "<state>" --batch "7.2,9.3,CRITIC-004"                     # 一簇：一次读索引，合并区间 + 一条预算结论
CTRL dump --state "<state>" --finding CRITIC-001                               # 一条 Finding 的当前状态
CTRL dump --state "<state>" --finding CRITIC-001 --fields all
CTRL show --state "<state>"                                                    # 整程摘要（耗时/派发/索引/门停/待办清单）
```

`locate`/`index`/`dump` 的 `--tdd-file` 默认为 `init` 冻结的路径。`--batch` 刻意比单条面窄：不做引用展开、不返回符号表——选定簇之后仍用单条 `locate` 看某一节引用了谁。`dump` 默认不回 `evidence`（那是唯一会把整段文字送回主控上下文的大字段），只给 `evidence_chars`；需要原文就 `--fields evidence` 或 `all`。`show --next` 是只读状态机的一眼结论，不推进任何东西：它给出的命令仍可能被拒。

这一节是**主控侧不整读原文**的唯一通道：PRD、`context-pack`、TDD 的整篇读取只发生在角色自己的上下文里。上一次运行主控整读 `product-doc.md` 3 次（58K 字符）、`context-pack` 2 次（35K 字符），而主控上下文里的这些东西没有导出机制，会一直占着窗口直到 run 结束——这才是它比角色慢的原因，不是命令太多。要核对 PRD 里的一句话，两条路：把它写成确认文件的待答条目（`sync-confirmations` 后由 `confirmations` 回它），或让已经在读那份资料的角色在回报里引用原文位置；都不是自己翻页。

## 回报格式：`@@FIND` / `@@FIX`

角色回报的正文照常写，额外为每条结论加一行机器行，正文跟在下面。Critic/Reviewer 用 `@@FIND`，Refiner 用 `@@FIX`：

```text
@@FIND id=CRITIC-001 sev=Blocker loc=9.3
会话超时与容量目标都没有定义，非功能需求无法验收。证据：PRD 验收标准 3。

@@FIND id=CRITIC-002 sev=Medium must_fix=false loc=7.2,4.2
重试次数与幂等键的口径不一致。

@@FIX id=CRITIC-001 status=FIXED loc=9.3
在 9.3 补写会话超时 30s、容量目标与压测口径。
```

- `@@FIND` 必填 `id`、`sev`、`loc`，可选 `must_fix=true|false`；`@@FIX` 必填 `id`、`status`、`loc`。键名写错、缺必填键、标记下没有正文，都是解析错误而不是跳过。
- `sev` 取 `Blocker|High|Medium|Low`；`status` 取 `FIXED|PENDING_CONFIRMATION|REJECTED`。
- `loc` 写 TDD 里真实存在的章节号；多章节用逗号分隔，`@@FIND` 按第一条登记，其余在 `multi_location` 里回显。
- 一份文件只能出现一种标记；注册后 `show` 的 `findings` 三个清单就是还欠的那批。
