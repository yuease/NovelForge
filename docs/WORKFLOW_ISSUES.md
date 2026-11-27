# NovelForge 写作工作流待修问题清单

> 来源于 200 章 / 40.1 万字长篇实机压测（《我给古人做测评，秦始皇连夜退货长生药》，总耗 2,869 万 Token，终审 98 分放行但成书评估检出 **270 条问题：99 P0 + 150 P1 + 21 P2**）。按工作流阶段归档，逐阶段推进修复并勾选验收。

---

## 一、总纲阶段（`book_plan`）

- [x] **WF-01 `[P0]` 缺少“固定角色称呼/ID”与“永久核心资产/舞台空间”常驻锁**
  - **现象**：汉武帝全书串出 8 个弹幕 ID，李世民 ID 反复横跳；开篇“不足 60 平米工作室”后期无交代膨化成十几扇 12 米巨门基地；终章 6 件度量衡母器清单与第 150、185 章自相矛盾。
  - **根因**：`canonLocks` 仅含 `currencyAndEra` / `worldTaboos` / `keyAssets` 三栏且总长封顶 420 字（`novel_forge/arc_planning.py`、`novel_forge/prompts.py`），既没有常驻角色的唯一称呼/网名锁，也没有跨 200 章不随普通 `items` 膨胀而丢失的舞台空间与核心系列道具锁。
  - **已落地**：升级 `bookPlan` V2（`novel_forge/book_plan.py`、`pipeline.py`、`prompts.py`、`verifier.py`、`app.py`、`web-gemini`），扩展 `canonLocks` 为 7 类（新增 `identityAliases`、`boundaries` + `changeWhen`、`assetSets`），≤900 字紧凑文本常驻全链路；`boundaries` 支持经审校正文引证的 `boundaryChanges` 动态状态回传；总纲 P0 校验失败重试 1 次后 `BOOK_PLAN_INVALID` 安全停断点；前端「跨章事实」与「成稿阅读」同步展示。
- [x] **WF-02 `[P1]` 超长篇总纲缺少“分卷阻力升级与旧解法淘汰”约束**
  - **现象**：前 30 章爆款质感（开篇 4.5/5），后 150 章滑向零风险按按钮空投碾压，第 151–196 章沦为 15 轮机械 3 章刷副本（剧情节奏 2.5/5）。
  - **根因**：`bookPlan.volumes` 仅定粗粒度题材方向，未要求每卷写明本卷新增阻力/主角真实代价、淘汰的旧套路或必须升级的对抗层级，导致滚纲在中后段无压力可写、只能原地克隆旧模板。
  - **已落地**：V2 `volumes[].escalation` 引入每卷 `pressure` / `costAtRisk` / `retiredTactics` / `stageTransition`，≥30 章长篇强制逐卷校验递增并支持 `volumeLadderStatus="partial"` 软降级；写章通过弹性 `volumeCard` 槽注入当前卷阶梯，滚纲与归档接入当前卷阶梯与 `volumeProgress`，`extend_job` 同步续接 V2 阶梯并在前端完整展示。

---

## 二、滚动单元规划（`chapter_outlines` / `novel_forge/arc_planning.py`）

- [x] **WF-03 `[P0]` 开启滚纲后 10 章卷摘要直接停产，全书失去中远景记忆**
  - **现象**：200 章全书 `volumeSummaries = 0`，跨 20 章以上没有任何中远景主题地图。
  - **代码位置**：`novel_forge/pipeline.py`（`elif not rolling and options["long_memory"] and number % 10 == 0` 跳过卷摘要）。
  - **已落地**：解耦滚纲与 10 章卷摘要归档，新增 `_catch_up_volumes` 在章提交后（含终章）、断点续跑、滚纲前（计算 `arc_context_version` 之前）三处幂等补档；前端「跨章事实」同步在滚纲模式下展示「十章远景卷摘要」。
- [x] **WF-04 `[P0]` 滚纲只看最近 2 个单元（约 6 章），隔十几章整章失忆重演**
  - **现象**：第 51–52 章全员失忆，把第 30–32 章已演完的“新疆长绒棉保暖内衣 + 跪坐换高脚椅”原样重演一遍。
  - **代码位置**：`novel_forge/arc_planning.py` `compact_arc_summaries()` 写死 `archives[:2]`。
  - **已落地**：实现四层不重叠视野 `horizon_memory`（`near` 2 单元 + `capsules` ≤12 + `volumes` ≤20 + 全书 `themeLedger` ≤80）；修复写章 `_memory_slots` 清空卷摘要问题并新增写章 `horizon` 弹性槽（命中的旧题材 `theme_hits` + 远景卷摘要）。
- [x] **WF-05 `[P0]` `usedBeats` 无上限直塞滚纲，从防重复反噬成“3 章流水线克隆模板”**
  - **现象**：全书 62 个单元中 **51 个是 3 章**，第 151–196 章连刷 15 轮“双内鬼 → 四机联检 → 读秒空投 → 卖 1.5 亿 → 撬密卷”；且第 29–32 章 4 条名额被仪器型号占满，第 31 章“高脚椅”压根未进入 `usedBeats`。
  - **代码位置**：`novel_forge/prompts.py` `build_arc_outlines_prompt()`（无上限塞入 243 条明细 `usedBeats`）。
  - **已落地**：`normalize_arc_summary` 实行按章轮流保底配额 `usedBeats`（上限 `min(6, max(4, 章数))`）并新增 `themes`/`skeleton`/`shape`；滚纲 `recentBeats` 严控在最近 2 单元 ≤12 条；引入 16 码全题材骨架 `SKELETON_CODES`、`arc_rhythm` 长度硬校验 + 骨架相似度/题材碰撞软拦截。
- [x] **WF-06 `[P1]` 滚纲绕开 Token 预算器，且缺少连续相同单元长度/套路的打断机制**
  - **现象**：76 次滚纲调用耗 262 万 Token，放任连续 15 轮选 3 章套用同一叙事骨架。
  - **已落地**：将 `prompt_budget.py` 泛化为 `fit_prompt`，新增滚纲预算 `DEFAULT_ARC_INPUT_BUDGET = 16_384`（`NOVEL_FORGE_ARC_INPUT_BUDGET` 可配，超保护区报 `ARC_PROMPT_BUDGET_EXCEEDED` 安全停断点）与 8 个优先级弹性槽，含重试均受预算约束。

---

## 三、正文撰写（`chapter_writing` / `novel_forge/prompts.py`）

- [x] **WF-07 `[P0]` 200 章长程下 `items` / `characters` / `subplots` 无限膨胀，挤掉核心旧设定**
  - **现象**：200 章跑完 `items = 643 件`、`characters = 213 人`、`characterStates = 126 人`、`subplots = 123 条`，一次性工业道具与龙套在 16,384 Token 预算下把早期世界观边界（如平行时空隔绝、工作室空间、前文母器清单）挤出可见槽位。
  - **已落地**：新建 `novel_forge/entity_salience.py` 实现存储无损的读取层五级分层（`core/active/dormant/ephemeral/retired`）与衰减打分、严格 `< current_chapter` 时序隔离、锚点区 `retiredRoster` 防退场人物复活、`themeLedger` 旧题材道具降权，并在 `arc_archive` 搭车支持 `subplotClosures` 收束与 35 章陈旧支线冷退。
- [x] **WF-08 `[P0]` 章标题写前锁死、写后不回校，导致多章标题与正文数值直接打架**
  - **现象**：第 10 章标题“十吨起拍” vs 正文“1 吨起拍”；第 30 章标题“百支精梳棉” vs 正文“80 支”；第 18 章标题“百吨级重工通道” vs 正文“50 吨”。
  - **已落地**：新建 `novel_forge/chapter_title.py`（中阿数字+通用量词+成语黑名单+同语境锚点冲突检测、零 LLM 本地替换 `local_retitle`、`strip_conflicting_number`），在提交/校准/终审复修前自动按已过审正文回校标题（含小模型重拟兜底与作者手动改题保护），并让滚纲优先消费定稿标题。
- [x] **WF-09 `[P1]` 考据/重算术题材未防止力写手关闭思考（`thinkingMode: disabled`）**
  - **现象**：主力写手关思考口算十六两制、多朝斤石与史实年号生卒，产出 49 条算术/换算 P0 + 30 条史实 P0，且写章共发生 367 次调用 / 140 次重试。
  - **已落地**：新建 `novel_forge/thinking_policy.py`（`writing_profile` 多信号识别、`thinking_capability` 保守能力矩阵、`resolve_writing_thinking` 区分用户显式与默认自动保护），对重考据/算术题材在写章与返修阶段自动开启受支持模型的思考并放宽输出预算，新增 HTTP 400 拒参一次回退缓存，前端创建页与监控页同步支持「自动（题材保护）」与重考据题材提示。

---

## 四、写中审校与返修（`chapter_verify` / `verifier.py` / `chapter_style.py`）

- [ ] **WF-10 `[P0]` `build_verify_prompt` 与 `build_repair_prompt` 未接入 Token 预算器（最大烧钱黑洞）**
  - **现象**：单 `chapter_verify` 阶段烧掉 **1,863 万 Token（占全书 65%，均摊每章输入 7.1 万 Token）**，却依然漏过 99 条 P0。
  - **代码位置**：`novel_forge/prompts.py` `build_verify_prompt()`、`build_repair_prompt()`（直接 `pretty=True` 全量拼装事实与记忆）。
- [ ] **WF-11 `[P0]` 账目算术硬校验 `ledger_arithmetic_conflicts` 正则过严形同虚设**
  - **现象**：49 条算术与账目冲突（含第 6 章售盐收入在第 10 章余额归零漏计）全数漏网。
  - **代码位置**：`novel_forge/verifier.py` `ledger_arithmetic_conflicts()`（同时要求账户全称 `label` + `本章净变动:` + `期末余额:` 逐字出现在同一条 120 字正文引文中）。
- [ ] **WF-12 `[P0]` 本地确定性门禁漏拦“破第四面墙元文本”与“标题-正文数值冲突”**
  - **现象**：上下文反复出现的 `第X章` 诱发模型直接写进正文（第 80 章台词“把第78章大秦五原关樟木箱里……”漏过；第 200 章“随着第199章因果闭环达成”幸好被模型审校截住）。
  - **代码位置**：`novel_forge/chapter_style.py` `run_deterministic_checks()`。
- [ ] **WF-13 `[P1]` 审校 `usedBeats` 窗口过窄（仅 12 条 ≈ 9 章）且缺高频反派反应滑窗拦截**
  - **现象**：超过 9 章的整段重演审校看不见；全书“瞳孔骤缩/微缩”出现 24 次、“裤裆湿透 / 拖死狗”连续多章复刻。
  - **代码位置**：`novel_forge/prompts.py`（`compact_used_beats(arc_views, limit=12)`）、`novel_forge/chapter_style.py`。

---

## 五、单元归档（`arc_archive`）

- [ ] **WF-14 `[P0]` `driftChecks` 只记笔记、不回头修章、过一单元即丢弃**
  - **现象**：`arc_archive` 实测精准抓出 **83 条硬伤**（含第 1 章金重算错、第 5 章霍去病/捕鱼儿海早逝早发、第 9/17 章李世民 ID 串号、第 12 章秦无“钱”等），但工作流禁止回修已提交章且下单元仅传 4 条，导致坏正文留在库里持续向后传染。
  - **代码位置**：`novel_forge/prompts.py`（`previousArcDriftChecks`）、`novel_forge/pipeline.py`（`_archive_arc`）。

---

## 六、完本终审（`full_audit`）

- [ ] **WF-15 `[P0]` 终审视野残缺且未接入 `driftChecks`，沦为“98 分橡皮图章”**
  - **现象**：99 条 P0 硬伤的成稿在 `full_audit` 获评 **98 分、0 阻塞直接完本**；同期逐批读正文的 `evaluator` 判 `revise_recommended`、世界观与节奏仅 2.5 分。
  - **代码位置**：`novel_forge/prompts.py` `build_full_book_audit_prompt()`（仅看摘要、`items[-30:]`、头 5 尾 10 条时间线、全量 213 角色表，未读取 83 条 `driftChecks`）。

---

## 七、运行时与限流调度（`pipeline.py` / `llm.py`）

- [ ] **WF-16 `[P2]` 未识别“长周期配额耗尽（如 15 小时重置）”类 429，仍在 300s 间隔空转**
  - **现象**：上游明确返回 `resets in 15 hours, 3 minutes` 后仍连续每 300 秒自动冷却重试十余次，刷屏日志且无谓唤醒。
