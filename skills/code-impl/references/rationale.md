# 设计理由与历史决策（运行时不注入）

本文件收纳 code-impl 各门禁与预算的"为什么"，供维护者复盘与审查改造时引用。
**运行时不需要读取本文件**：主 Agent 与子代理的行为规则以 SKILL.md、
references/contracts.md 与 references/controller-contract.md 为准；本文件不新增、
不修改任何操作规则，只解释它们为何存在。

## 派发作废为什么不是 FAILED

Implementer 会话断连、宿主中断或 Subagent 无返回时，任务停在 `RUNNING`，既没有结论
也没有错误指纹。若把这种传输故障记作 `FAILED`：

1. `FAILED` 的语义是"任务被判失败"，一次传输故障会写进台账并占用一次派发预算；
2. 随后的重派会被 `gated` 重试门禁拦下——它要求一条 `record-failure`，而这里没有
   可诊断的东西。

因此控制器提供唯一的作废迁移 `RUNNING → PENDING`（必须带 `--reason`）：撤销本次派发
的预算记账，`attempts` 不增长，重派不需要诊断、也不消耗 `--retry-policy prose`；痕迹
保留在 `attempts_log`（标 `abandoned` 与原因）与任务的 `abandoned_dispatches` 计数里。

作废自身也有预算（`MAX_TASK_ABANDONS=3`）：作废是唯一不占派发预算的迁移，若没有上限，
"宣称会话没返回"就成了绕开 `MAX_TASK_ATTEMPTS` 与指纹重试预算的无限后门，所以第 4 次
作废被控制器直接拒绝。被拒后只剩两条诚实出路：`settle-task --status FAILED` 带
`dispatch-lost-repeatedly::<task-id>` 这类指纹并 `record-failure`，或转 `BLOCKED`
升级人工介入。这条边只用于"派发没有返回"；`VALIDATING`/`FAILED` 状态下作废会被拒绝，
不能用它绕开失败登记。

## 错误指纹为什么按归一化形式计数

重试预算若按指纹原文计数，改写日志中的时间戳、临时路径或行号漂移就能重置预算。
控制器按归一化形式计数：十六进制串、数字、引号内文本、路径分别折叠为
`<hex>`、`#`、`<str>`、`<path>`。因此同一条断言在 412 行和 517 行失败、或 token 值变了，
都记作同一个错误。`record-failure` 同时保存原文与归一化形式，`show` 显示该指纹已出现
几次。这也是 Debugger 角色正文要求"错误指纹写成归一化形式"的原因：换写法不会重置预算。

## 文档编码失败为什么给 BLOCKED 而不是 traceback

真实 PRD/TDD/RPD 常来自 Windows 编辑器，非 UTF-8（gb18030/big5/shift_jis/cp1252/utf-16）
或带 BOM 并不罕见。严格解码会把这类输入变成 exit 1 的 traceback，主 Agent 拿不到可行动
的原因。`init` 因此以 `BLOCKED:` 结束并给出检测到的编码；只有在人工确认可以接受有损解码
后才可用 `init --allow-lossy-decoding` 重开一个 run，该选择记录在 `materials.*.decoding`
并必须在最终报告披露。Git Bash 等 MSYS 环境把 `/c/...` 重写过的路径会被识别并提示改用
Windows 绝对路径，避免"文件明明存在却读不到"的假阻塞。

## 为什么下游"文档缺失"降级是 Handoff 不一致的信号

本 Skill 的输入门禁已在步骤 01 强校验三份文档，Handoff 一律显式声明 `review_mode: FULL`
且三份文档 `access: available`，不让下游自行推定。`codekit:code-review` 支持为空的
`feature-directory` 并在解析不到有效文档时自行降级（记录 `DOCUMENTS: NOT_AVAILABLE`，
结论最多 `APPROVE_WITH_FOLLOW_UPS`）。因此下游返回的任何"文档缺失"降级——带文档缺失
Open Question 的 `APPROVE_WITH_FOLLOW_UPS`，或因读不到 Handoff 所列文档而 `BLOCKED`——
都说明它没有拿到本 Skill 冻结的三份文档（目录漂移、文档被移动或 Handoff 漏写字段），
不得当作审查通过交付：能定位原因时重新冻结 Handoff 后重新调用，否则进入人工介入。
本 Skill 不使用 `WAIVED`（`codekit:code-review` 收到 `WAIVED` 会直接返回 `BLOCKED`）。

## 为什么结论与证据必须绑定冻结指纹

"审查通过"若只是会话里的一句话，旧快照的结论就能贴到新 Diff 上。`record-review` 的
digest 一律取自控制器自己的最后一次冻结，不由调用方填写；`gate` 要求整体证据与最新
结论的 `diff_digest` 都等于最后一次冻结。冻结后再写入任何文件都会让证据"早于交付
Diff"，必须重新验证并重新冻结——这不是仪式，而是"结论描述的就是被交付的这份 Diff"
的机械保证。同理，报告文件必须写在运行目录（`.codekit/`）内：写进仓库工作树会被范围
检查判为越界，也会污染下一次冻结的指纹。

## 为什么任务级验证整轮批量登记

步骤 07 的一轮检查（格式/编译/单测/Lint/静态检查）逐条 `record-validation` 意味着每条
一次状态读写加一次模型往返，而同一轮内工作树不变、指纹相同。批量面
`record-validations --from-file` 让整轮一次落盘：逐条仍走同一套 `_validation_record`
规则（PASS 须 exit 0 且有证据、FAIL 须非 0、NOT_RUN 须理由），任一条不合法整份拒绝并
点名，不留"半轮"证据。等价性由测试看守：同一工作树下批量与逐条的台账终态除时间戳外
逐字段相等。单条面保留给轮中即时登记（如某条失败后中止本轮、先落失败证据）。

## 为什么审查修复轮可以用 DELTA 增量复核（P1-1）

改造前，`REQUEST_CHANGES` 后哪怕只修两行，也要重新完整调用 code-review（每轮 7–9 次
子代理派发、9 维度 + 双独立反证），最多 3 轮——跨轮全量重审是最大的单项耗时来源。
DELTA 轮把修复复核缩为 1 个全新 review-evidence 实例 + 新鲜机械检查，但放宽的只是
派发覆盖面，不是判据，安全边界全部机械化：

1. 首轮必须 FULL，DELTA 只紧跟 FULL 的 REQUEST_CHANGES（record_review 拒绝其他序列，
   因此 DELTA 不会连续、也不会是首轮）；
2. DELTA 轮不得 APPROVE：下游校验器把 DELTA 归入受限审查（与 LITE 同一通道），结论
   封顶 APPROVE_WITH_FOLLOW_UPS；未复核维度标 NOT_VERIFIED 不强制 BLOCKED，但必须
   逐字披露 DELTA_RECHECK/DELTA_CARRY_FORWARD 两条限制；
3. 交付轮必须 FULL：gate 新增 review-delivery-round-full 检查，DELTA 通过后仍须一次
   FULL 交付确认轮——最坏后果是多花一轮，而不是漏检；典型序列 FULL→DELTA→FULL
   恰好消耗 MAX_REVIEW_FREEZES=3，预算不变；
4. 不得借增量轮改写结论：traceability 与 release_gate 必须逐字继承上一轮 FULL 报告
   （校验器读磁盘上的上轮报告逐项比对），NOT_FIXED/REGRESSION 必须仍是阻塞 Finding，
   FIXED 不得仍挂阻塞，复核者必须是台账里真实派发过的实例；上轮报告路径 + 裸 sha256
   digest 在 Handoff 校验与报告校验两侧都核。
5. 不得“不登记即降级”：results 的 finding_id 必须属于上轮阻塞 Finding 集合，上轮
   每个阻塞项要么被复核、要么仍在本报告阻塞清单中；上轮报告自身必须是 FULL
   （DELTA-of-DELTA 不合法）；至少一个维度 REVIEWED 且归属复核实例（全维度
   NOT_VERIFIED 的空转 DELTA 不合法）。
6. 模式不是自报标签：record-review 读报告 JSON 交叉校验 review_context.review_mode
   与 --review-mode 一致，把 DELTA 报告改标 FULL 再登记会被拒绝，堵死绕过
   review-delivery-round-full 的路径。

风险取舍：修复可能引入跨模块回归，DELTA 轮只看被修 Finding 与受影响维度，这类回归
由每轮仍新鲜执行的机械检查（编译/全量测试）与最终 FULL 轮兜底。这是用户批准的
严格度换耗时取舍，对抗用例覆盖每一条绕过路径（见两侧测试的 DELTA 用例组）。

## 为什么整体检查可以复用任务级证据（P1-3）

改造前同一套全量测试要跑 4–5 遍（基线/自检/任务级/整体/审查侧）。若某项整体检查的
命令已在任务级验证中逐字跑过，且此后工作树一个字节都没变，重跑买不到新证据，只买到
墙钟时间。reuse-validation 把这类重复免掉，但"同一命令、同一棵树"必须是测出来的：

1. 源必须是任务级 PASS（FAIL/NOT_RUN 不可复用，整体记录不可再复用——禁止复用链）；
2. 命令与解析后 cwd 必须逐字命中 record-tasks --from-dag 落盘的 overall_validation
   计划：任务级子集命令（如只跑某个测试文件）永远无法冒充整体检查，白名单来自
   Planner 登记的整体计划而不是调用方自报；
3. 当前工作树 head_sha/diff_digest 必须与源记录采集时完全一致：任务验证后写过任何
   文件，复用即被拒，必须新鲜重跑；
4. 复用记录带 reused_from 溯源，show 计数，最终报告必须披露；gate 的指纹判据不被
   豁免（复用记录的指纹就是当前树，与冻结天然一致）。

风险取舍：任务级通过不等于组合通过（任务间交互效应），因此只有"命令逐字就是整体
命令"的项才可复用；跨模块/集成/契约/Migration/构建等未被任务级逐字跑过的检查仍须
新鲜执行。对抗用例覆盖：树漂移、FAIL/NOT_RUN 源、复用链、子集命令、空白变体、
cwd 不匹配、无计划、越界索引（见 EfficiencyRelaxationTests）。