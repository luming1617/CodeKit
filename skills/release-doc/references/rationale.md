# release-doc 设计动机与改造记录（rationale）

> 本文件只记录设计取舍与历史，运行时不需要读取；流程规则以 SKILL.md 与 `release-review-contract.md` 为准。

## 为什么是五阶段闭环

发布方案的核心风险不是“写不出来”，而是“把推断写成事实”和“回滚路径不可执行”。因此把创作（Writer）、故障审查（Critic）、定向补全（Refiner）与独立评审（Reviewer）分成四个只读/受限写入角色，由主控做输入门禁与事实合并：

- Dependency Resolver 前置：发布顺序错误是最常见的 Blocker 来源，先解析依赖再创作，避免初稿推倒重来；
- Chaos Critic 与 Reviewer 分离：Critic 面向“每一步会怎么坏”，Reviewer 面向“方案作为整体是否可放行”，两者视角不同，合并会互相稀释；
- Refiner 只按风险清单定向补全：防止补全阶段重新设计 TDD 或引入无来源事实。

## 2026-11 P0 提效改造（不降低严格度）

改造前的耗时根因（与 product-doc 改造前逐项同构）：

1. 每轮 Reviewer 全量重读约 68 KB 参考件 + PRD/TDD/RPD 全文，最多 5 轮无衰减；
2. 零脚本：冻结、digest、轮次、模板校验全靠 LLM 阅读推理，手工快照不一致即 BLOCKED 重跑；
3. 全文接力：Writer 会话返回全文 → Refiner 返回修订版全文 → 主控二次合并落盘 → Reviewer 再读全文，同一内容转录 4–5 遍；
4. 资料固有缺口（MR/产物/负责人/阈值）无分流规则，必然产生阻塞 Finding，Refiner 又无权补写事实，循环空转满 5 轮后 BLOCKED；
5. 评审回执与中间产物内嵌 RPD，文档逐轮膨胀。

P0 落地项：

- P0-1 `release_state.py`（台账：init/digest/record/close/advance/verify/show/render-appendix/render-report/diff）+ `validate_release_doc.py`（结构门禁 + `--check-template-refs` 漂移检查）；
- P0-2 Writer 初稿直接写 `release-doc.draft.md`、Refiner 定向补丁直改正式文件（>30% 行改动才允许整篇重写，由 `diff` 子命令判定），会话只传路径 + 摘要；
- P0-3 完整评审意见外置只追加到 `release-reviews.md`，RPD 文末只留脚本渲染的紧凑回执块；Chaos 台账与补全记录同样外置；
- P0-4 契约新增资料固有缺口分流（已登记缺口记 Medium 非阻塞）与 severity/blocking 对应表；
- P0-5 SKILL.md 瘦身（420 行 → 约 260 行）：删除重复的 ASCII 架构图与第二张流程图、与 agents/*.md 重复的角色段落；叙事外置到本文件。

## 2026-11 P1-1 门停放宽与增量复核轮（经用户拍板接受）

- 门停放宽：`REQUEST_CHANGES` 仅由 blocking（Blocker/High）触发；Medium/Low（含已登记固有缺口）走 `APPROVE_WITH_FOLLOW_UPS` 非阻塞跟进，`record` 机械拒收无 blocking 的 REQUEST_CHANGES。至此固有缺口不再产生空转轮。
- 增量复核轮：`advance --mode delta` 只派 1 个全新 RECHECK Reviewer，封套只含未闭环 Finding、被修改小节与新旧指纹；报告新增 `perspective`（COMPREHENSIVE/RECHECK）与 `verified_findings` 字段。
- 机械安全边界（全部代码谓词）：第 1 轮与交付轮必须 full；delta 不得连续；delta 不得开启最终轮；RECHECK 的 APPROVE 在台账有未闭环项时不得空确认；存在 RECHECK 报告时 close fixed 必须消费其 verified_findings；Blocker/High 不得标 blocking=false。
- 对抗用例（16 条新增，76 测试全绿）：空确认、连续 delta、delta 交付、未经确认的 fixed、封套变形（perspective 缺失/非法）、High 伪装非阻塞、Medium/Low 试图开轮、RECHECK 新阻塞强制全量、单轮带缺口跟进交付等。
- 最坏后果设计：放宽的最坏结果是“多花一轮全量”，而非漏检。

## 2026-11 P1-2 机械补丁白名单（经用户拍板接受）

- 白名单以 `validate_release_doc.py` 的稳定错误码为唯一来源（全部错误已码化：STRUCT/PLACE/STATUS/RESULT/HIST/REVIEW/EMPTY）；仅 `STRUCT002`（章节顺序）与 `STATUS003`（状态枚举空白变形）可直修。
- 语义中性谓词：STRUCT002 由脚本确定性重排（主控不提供新文本，块内容逐字节不变，行多重集守恒，嵌套章节按物理位置切块避免重复搬运）；STATUS003 仅允许去空白归一化（去空白后必须等于新值且新值在合法枚举内）。
- 门禁必须实际报出该错误码（STATUS003 还须点名目标行，锚点唯一）；补丁不得引入新缺陷（错误签名集合包含关系判定）；每处直修写入台账 mechanical_patches（码/行/旧→新/前后指纹），show 与交付报告强制照抄。
- 交付轮仍全量：补丁发生在最后一轮评审之后时，verify 指纹绑定强制补一轮全量评审；绕过通道的手改同样被指纹绑定拦下。
- 对抗用例（15 条新增，91 测试全绿）：结论改换夹带（通 过→不涉及、正常→通过）、未点名行、白名单外码、模糊锚点、缺章重排、未报出预修、评审后补丁强制全量、未留痕手改等。

## 2026-11 P1-3 规模裁剪与 Chaos 剪枝（经用户拍板接受）

- 规模探针：`init --scale S|M|L` 机械判定最低严格度（服务数、适用基础设施、高风险信号、未决风险字段），声明只能上调；资金/账务/交易/资产=是、DROP TABLE/DROP COLUMN/TRUNCATE、不可逆迁移/删表删字段、无标准回滚任一信号强制 L；引语与否定句不算信号，风险字段未决不得判 S（fail-closed）。
- S 级裁剪：17 章骨架全保留，不适用基础设施小节与依赖变更只折叠为单行“不涉及+原因”（COLLAPSE001 判定，只折叠不删除）；发布策略只注入命中的一套；Reviewer 免除不适用小节逐行核对但仍核折叠理由与高风险字段。
- Chaos 剪枝：必查模式由 required_chaos_modes 从台账推导（M/L 全 11；S 始终 8 + 按适用基础设施追加消息积压/数据异常/配置未生效）；新增 chaos 子命令入台账，缺必查模式或规模不符即拒收；verify 要求 Chaos 已入台账。
- 交付联动：verify 重跑探针（正文风险升级拦下）、RPD 发布规模行与台账一致、交付轮仍全量；模板关键信息新增发布规模行，验证器新增 KEY001/KEY002/SCALE001/COLLAPSE001。
- 对抗用例（21 条新增，112 测试全绿）：高风险字段/破坏性 SQL 强制 L、引语与否定句不误判、未决风险不得 S、多服务不得 S、规模行与台账联动（init/verify）、init 后风险升级、Chaos 缩小必查/规模不符、M 全矩阵、S+MQ 必查积压、报告 scale 不符、S 级端到端交付等。

## 2026-11 代码审查缺陷修复（#1-#10 全部落地）

独立代码审查在已拍板的放宽中发现 10 处谓词绕过/表述漂移，全部修复并配对抗用例（134 测试全绿）：

- #1（Blocker）指纹盲区：without_appendix 改为按章节结构定位文末回执节（与 parse_headings 同一标题判定：# 后必须有空格、排除围栏）；正文中部伪回执标题（含无空格变形）由新增 REVIEW005 拒收；历史快照围栏里的旧回执标题不再参与剔除。
- #2（High）fixed 时序绕过：delta 轮内 close fixed 必须先记录本轮 RECHECK 报告并获其 verified_findings 确认；verify 交付前复查消费（提出后存在 delta 轮的 fixed 必须被某轮 RECHECK 确认），堵死先闭环后记录与 pre-close 清空后空确认。
- #3（High）探针信号绕过：否定豁免改为短语所在子句级（同行混排不再吞信号）；SQL 信号不限围栏；风险字段 startswith("是") 计信号（带注解肯定不再降为未决）。
- #4（High）围栏状态机：fence_flags 按 CommonMark 配对（同字符、长度不小于开启、缩进≤3 空格、闭合无信息串），混用 ```/~~~ 与深缩进伪围栏不再翻转状态。
- #5 history_line_count 改用围栏感知的章节边界，快照内标题不再截断计数，删快照正文必被历史倒退门禁拦下。
- #6 Chaos 报告新增 rpd_digest 绑定（记录时与 verify 交付时双重校验，改稿后旧覆盖度失效）；findings 经 validate_finding 入同一闭环台账（登记为上一轮提出，评审前=0），Blocker/High 未闭环禁止交付；编号与台账冲突拒收。
- #7 deferred --q-ref 改词边界匹配且只在遗留问题节（围栏外）查找，前缀碰撞与历史快照旧编号不再算登记。
- #8 重复强制章节报新增 STRUCT004（不入机械补丁白名单）；_reorder_sections 遇重复标题拒收重排。
- #9 STRUCT002/STATUS003 补丁改按原始字节读入 + splitlines(keepends=True)，CRLF 行尾逐字节守恒。
- #10 reviewer-prompt 改为用只读 digest 命令输出核对指纹，不再要求手工计算 sha256（归一化算法无法手工复现）。
- 同步修正契约/SKILL/critic 角色中“承诺了但未实现”的门禁表述；新增静态一致性用例锁定 REVIEW005/STRUCT004/先闭环后记录/复查消费等表述。

## 2026-11 第二轮复审缺陷修复（N1-N9 全部落地）

第二轮独立复审确认第一轮 10 项修复中 6 项完全堵死，4 项存在残留绕过或修复引入的新问题；全部修复并配对抗用例（152 测试全绿）。同时更正上一节三处过度承诺：#4 当时未按 CommonMark 拒绝信息串含反引号的开启行；#5 当时模板同款 ```markdown 快照仍可删尾部；#9 当时写回侧 write_text 在 Windows 上破坏 CRLF。

- N1（High）回执节后追加非强制章节是新指纹盲区：新增 REVIEW006（回执节必须是文档最后一章，其后只允许回执块标题）；without_appendix 只剔除到首个非回执块标题为止，后续章节仍计入指纹；verify 同口径机械复查。
- N2（High，第一轮修复引入的死锁）：verify 消费复查的 delta 轮范围加 closed_in_round 上界（提出后、闭环前），闭环后为其他 Finding 开的 delta 轮不追溯要求已 CLOSED 项；新增 full 轮合法闭环→后续 delta 的正向流程用例证明不死锁，篡改对抗用例仍命中。
- N3（High）探针 last-wins 与验证器 first-wins 不一致：统一只认关键信息项目/内容表且 first-wins；重复风险行报新增 KEY003、探针按未决 fail-closed；服务数取含服务名称表头表格的最大行数，异表头/空诱饵表不得降级。
- N4（High）SQL 逐行扫描漏检：围栏内 SQL 去注释（/*…*/、--、#）后整体扫描，DROP/**/TABLE 与跨行书写命中；正则补 TRUNCATE 省略 TABLE 形态；prose 保留逐行扫描。
- N5 否定豁免吞括号/定语结构：子句切分集加入括号与冒号。
- N6 开启围栏信息串含反引号未按 CommonMark 拒绝：fence_flags 对反引号开启行检查信息串，含反引号则不开围栏，伪围栏不得藏检查表。
- N7 模板 ```markdown 快照无法嵌套内层围栏：模板改 ````markdown 并静态锁定；新增 HIST002（历史节内围栏开启必须至少 4 个标记字符）；history_line_count 对节末仍在围栏内（未闭合/标题泄漏）fail-closed 拒收。
- N8 STRUCT004 前缀匹配误报合法子节：重复判定改为与首个匹配章节同级且在其范围之外（duplicate_sections 助手，重排拒收同口径）。
- N9 patch 写回侧 write_text 在 Windows 上把 CRLF 变 \r\r\n：三处写入改 write_bytes，新增混合行尾逐字节守恒用例。
- 同步契约/SKILL/模板：消费复查加“闭环前”限定、REVIEW006/KEY003/HIST002 表述、探针同口径与 SQL 变形、快照围栏形态；新增静态一致性用例锁定上述门禁与模板形态。

## 未落地项

无。P0/P1 全部提案项均已落地；后续若新增放宽，仍须逐项拍板并配对抗用例。

每项放宽必须配「变形输入 + 试图绕过」的对抗用例后才具备合入条件；三条底线不放宽：独立评审、来源可追溯、不得编造发布事实。

## 跨文件一致性不变量

`release-review-contract.md` 是评审流程唯一事实源；SKILL.md、check-standard.md、release-doc-template.md、agents/*.md、release-reviewer-prompt.md 均为下游表述。改任一语义（状态/严重度/分类枚举、回执落盘位置、章节清单）必须同步各处，`validate_release_doc.py --check-template-refs` 会机械判定漂移。角色常设要求只写在 agents/*.md，prompt 文件只放派发封套与本步约束（布局门禁拒绝两处重复正文）。