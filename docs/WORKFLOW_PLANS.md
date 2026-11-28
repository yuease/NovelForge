# NovelForge 写作工作流七阶段优化方案与落地记录

> 本文档对应 [WORKFLOW_ISSUES.md](./WORKFLOW_ISSUES.md) 中的 16 项实机压测缺陷（WF-01 ～ WF-16），逐阶段记录 **GPT-6.2-Astra**、**Claude Opus 5.5** 与 **Gemini** 三方独立方案对比、**选方案与融合理由**、最终落地规范及代码验收结果。

---

## 阶段一：总纲阶段（`book_plan` · WF-01 / WF-02）

- **状态**：已完成并验收通过（本地提交 `d75dcc1`，后端 252/252 单测 + 前端编译 + 实机浏览器 0 报错验收）
- **三方思路与融合理由**：
  - **GPT-6.2-Astra**：提出显式 `planVersion: 2` 隔离新旧版本，避免触发旧 V1 SHA-256 提示词快照，并用严格结构校验 + 边界状态 `changeWhen` 防空间膨化。
  - **Claude Opus 5.5**：强调每卷必须显式声明 `escalation`（`pressure`、`costAtRisk`、`retiredTactics`、`stageTransition`），且总纲失败时严禁静默回退单卷壳（必须重试 1 次后安全停断点 `BOOK_PLAN_INVALID`）。
  - **Gemini**：聚焦预算可控（900 字紧凑文本投影 `canon_locks_text()` + 写章弹性 `volumeCard` 槽）与前后端全链路体验闭环（包括可变边界的审校引证生效、手动重审传递边界状态、前端「跨章事实」与「阅读页」完整可视化）。
  - **定稿理由**：融合三者优点——用 `planVersion: 2` 保 100% 兼容老书；7 类硬设定（新增 `identityAliases` / `boundaries` / `assetSets`）压进 ≤900 字紧凑文本常驻全链路；可变边界仅在审校通过且正文原文可定位时动态演进；≥30 章长篇强制校验分卷阶梯递增，P0 错停断点、P1 缺项允许 `partial` 软降级。
- **代码落实**：
  - 后端：`novel_forge/book_plan.py`、`pipeline.py`、`prompts.py`、`verifier.py`、`long_memory.py`、`arc_planning.py`、`storage.py`、`llm.py`、`app.py`、`tests/test_python_book_plan.py`（+20 项单测）。
  - 前端：`web-gemini/src/lib/types.ts`、`format.ts`、`pages/FactsPage.tsx`、`pages/ReaderPage.tsx`。

---

## 阶段二：滚动单元规划（`chapter_outlines` / `arc_planning` · WF-03 / WF-04 / WF-05 / WF-06）

- **状态**：已完成并验收通过（后端新增 `test_python_stage2_arc_memory.py` + 既有回归全绿，前端编译通过）

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **WF-03 恢复卷摘要** | 独立判断单元结束与 10 章块完整；`long_memory=True` 时含终章也归档；旧长书缺块支持本地 `derived` 兜底 | 新增 `_catch_up_volumes` 在提交后/续跑/滚纲前 3 处幂等补档，沿用现有 10 章结构与 `sourceHash`，零改表 | 解除 `rolling` 与 10 章归档互斥，滚纲下支持优先读取 `arcSummaries` 压缩成卷摘要 |
| **WF-04 解决 20 章失忆** | 保留近 2 单元 + 10 章回顾 + 逐章提取 `themeEvents` 本地索引；**规划前检索 + 规划后候选章纲本地反查全历史** | **四层不重叠视野 `horizon_memory`**（`near` 2 单元 + `capsules` ≤12 + `volumes` ≤20 + `themes` ≤80），每章只落一层；写章加 `horizon` 槽 | 三层记忆（近 2 单元 + 卷摘要 + `coveredTopics` 全书黑名单）；修复 `_memory_slots` 写章清空卷摘要的 Bug |
| **WF-05 防 3 章克隆** | 逐章提取最多 2 条 `themeEvents` 防挤占；3 维结构统计；连续 3 次同长度或连续 3 次同结构且事件重复触发重试 | `usedBeats` 轮流按章分配保底每章 1 条；**16 个全题材叙事骨架码 `SKELETON_CODES`**（只给统计与告警、绝不给可抄样例）；硬拦连续 3 次同长度，软拦连续同骨架与题材碰撞 | `usedBeats` 限窗 16 条斩断 Few-Shot；轻量 `arcPattern` + `recentRhythm` 提示与拦截 |
| **WF-06 滚纲预算器** | 滚纲预算 `16,384`（实发 ≤15,360），保护区超限报 `ARC_PROMPT_BUDGET_EXCEEDED` | 泛化 `fit_prompt`（滚纲默认 `20,480`，可配 `0` 回退），区分保护区与 8 个优先级弹性槽 | 泛化 `fit_prompt_budget`（默认 `16,384`），保护区含 Stage 1 锁/卷卡/纠偏 |

### 选方案与融合理由
1. **记忆层级采纳 Opus 5.5 的“四层严格不重叠 `horizon_memory`”**：比同时塞多份全量摘要省 Token 且信息无冗余——每一章只落在 `near` / `capsules` / `volumes` 中的一层，再叠加一条轻量的全书 `themeLedger` 名词黑名单（每条 ~10 字，200 章也极短）。同时采纳 Gemini 对写章 `_memory_slots` 的修正：不再因存在 `arcSummaries` 就把更早的 `volumeSummaries` 清空，通过弹性 `horizon` 槽让写章同时看见远景与命中的旧题材。
2. **防克隆与节奏控制采纳 Opus 5.5 的“16 码骨架统计 + 轮流按章 `usedBeats`”并融入 Astra 的“候选章纲落盘前题材碰撞反查”**：
   - 200 章实测里 183 条明细 `usedBeats` 是催眠模型克隆 3 章模板的元凶，因此滚纲必须将明细 `usedBeats` 限死到最近 2 单元（≤12 条），并用统计数字替代完整历史序列。
   - `arc_archive` 按章轮流配额 `usedBeats`（先每章保底 1 条，上限 `min(6, max(4, 章数))`），直接解决第 31 章“高脚椅”被第 29 章 4 条仪器细节挤掉的问题。
   - 长度防单调做**硬校验**（连续 3 次同长或近 6 单元 ≥5 次 ≤3 章时在 `arc_chapter_span` 内排除重复长度，终局 ≤6 章豁免且绝不把合法集合剔空）；骨架相似度（`0.5·Jaccard + 0.5·LCS/max` ≥0.6 连续出现）与候选章纲题材碰撞反查做**软拦截**（第 1 次打回带原因重试，第 2 次放行记 `warn` 日志），既能打断 15 轮机械复制，又不会把合理剧情卡死。
3. **预算上限取 Astra / Gemini 的 `16,384`（预留 `1,024` → ≤15,360）并沿用 Opus 5.5 的通用 `fit_prompt` 与 `NOVEL_FORGE_ARC_INPUT_BUDGET` 开关**：比 20,480 更克制成本，76 次滚纲总输入可稳压在 ~1.1M 以内（原 2.62M 砍半以上），且设 `0` 可零风险回退旧路径。
4. **放弃项与理由**：不做 Astra 单独维护的高复杂逐章 `themeEvents` 重放状态机（`themes` 名词标签 + 轮流按章 `usedBeats` + 候选题材碰撞反查已能以 1/3 复杂度根治第 31→51 章失忆，且不额外膨胀归档输出）；卷摘要保持现有 `save_volume_summary` 与 10 章结构不变，避免破坏旧作业与历史测试。

### 最终定稿落地规范
- `novel_forge/prompt_budget.py`：泛化 `fit_prompt(...)`，保留 `fit_chapter_prompt` 兼容包装，新增 `DEFAULT_ARC_INPUT_BUDGET = 16_384` 与 `read_arc_input_budget()`（报错码 `ARC_PROMPT_BUDGET_EXCEEDED`）。
- `novel_forge/arc_planning.py`：新增 `SKELETON_CODES`（16 码）、`skeleton_similarity`、`derive_arc_themes`、`horizon_memory`、`theme_hits`、`arc_rhythm`、`find_theme_collisions`；扩展 `compact_arc_summaries(archives, limit=2, include_beats=True)` 与 `validate_arc_plan(..., allowed_lengths=None, rhythm=None, themes=(), soft=True)`。
- `novel_forge/long_memory.py`：`normalize_arc_summary` 清洗 `themes`（≤3×24 字）、`skeleton`（≤5 有效码）、`shape`（≤40 字），并实施 `usedBeats` 按章轮流保底配额（上限 `min(6, max(4, 章数))`）。
- `novel_forge/prompts.py`：`build_arc_outlines_prompt` 接入 `fit_prompt` 与三通道（`recentBeats` ≤12 / `themeLedger` ≤80 / `patternStats`）；`build_arc_summary_prompt` 增加 `themes`/`skeleton`/`shape` 与按章覆盖要求；`build_chapter_write_prompt` 滚纲时新增 `horizon` 弹性槽（share=3，含 `theme_hits` + 1 条非重叠远景卷摘要）。
- `novel_forge/pipeline.py` & `storage.py` & `llm.py`：新增 `_catch_up_volumes` 接入提交后/续跑/滚纲前（含终章所在 10 章块，且在 `arc_context_version` 前完成）；`_plan_arc` 接入 `arc_rhythm` 与软硬两级重试；`ARC_PROMPT_BUDGET_EXCEEDED` 加入 `_MANUAL_RESUME`；更新 mock 与前端 `types.ts` / `FactsPage.tsx` / `format.ts`。

---

## 阶段三：正文撰写（`chapter_writing` · WF-07 / WF-08 / WF-09）

- **状态**：已完成并验收通过（后端 136 项相关回归全绿，前端编译通过）

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **WF-07 实体防膨胀** | 存储无损，写章单一投影 `select_writing_entities`（上限 物品12/人物8/支线4）；严格按 `< N` 时序隔离防未来泄漏与同章重试虚涨；区分可见性（ephemeral/archived）与有据剧情态（retired） | 独立纯函数模块 `entity_salience.py`：扫描 `factHistory` 做五层分级（`core/active/dormant/ephemeral/retired`）与衰减打分公式；新增锚点区 `retiredRoster` 防死人复活；联动 Stage 2 `themeLedger` 对旧题材物品打 0.5 折；复用 `arc_archive` 输出 `subplotClosures` 收束僵死支线 | 存储无损 + 三级冷热分层（Pinned/Hot/Cold）+ `subplots` 25 章不活跃冷退；修补 `extract_relevant_facts` 全量复制 213 人物漏洞 |
| **WF-08 标题与正文一致性** | 强调写后面向**最终正文**（覆盖 `_commit_chapter`、`_calibrate`、`_repair_audit_chapters` 三处改稿路径），按主体+动作锚点防多数字误判，可在审校 JSON 附可选 `titleAdvice`，事务原子同步 | 纯函数 `title_body_conflicts` + **零 LLM 的本地 `local_retitle`**（单值匹配直接保风格替换，如十吨→一吨、百支→80支）→ 失败才走 128-token 辅助重拟 → 兜底删矛盾数字；`_plan_arc` 优先取已提交定稿标题去重 | 正文为真 + 本地双进制数量单位冲突检测（含成语黑名单）+ 冲突时轻量 `auxiliary` 重拟标题 |
| **WF-09 考据/算术题材思考保护** | 分离用户意图（`writerReasoningPolicy: auto/on/off/inherit`）与传输字段；书级+章级两层强弱信号；明确 400 拒参最多回退 1 次并按 `(provider, endpoint, model)` 缓存；高危降级时给写章注入精简精度提示 | `writing_profile(job, book_plan)` 多信号计分（≥4 分判 `reasoningHeavy`，含动态观测近期 10 章算术错 ≥3）；保守能力表 `thinking_capability`（`toggle/always/none`）；`thinkingModeSource` 区分用户显式与默认；仅提升写章与返修并放宽 `max_tokens` | `thinkingMode: "auto"` 智能默认 + 高危题材识别 + Provider 白名单 + 400 缓存降级 |

### 选方案与融合理由
1. **WF-07 以 Opus 5.5 的 `entity_salience.py` 五层分级 + `retiredRoster` + `subplotClosures` 为骨架，融入 Astra 的严格 `< chapter_number` 时序隔离与同章去重**：
   - `retiredRoster`（≤30 人，~150 字放锚点区）用极小代价解决长篇写着写着“把第 20 章已死反派写复活”的顽疾；
   - `subplotClosures` 搭车现有 `arc_archive`（0 新增调用，随归档自动回滚），真正把 123 条僵死支线降下来；
   - 必须补上 Astra 指出的时序隔离：仅统计 `< current_chapter` 且按 `(entity, chapterNumber)` 去重，防止同章返修虚涨热度或回滚/补缺时泄漏未来章节状态。
2. **WF-08 采纳 Opus 5.5 的“四级零损耗渐进改题（检测 → 本地替换 → 极小辅助调用 → 删数字兜底）”并吸收 Astra 的“多出口覆盖 + 动作/实体锚点防误判”**：
   - 压测三案（「十吨起拍」vs「1 吨」、「百支精梳棉」vs「80 支」、「百吨级重工通道」vs「50 吨」）靠本地单值/锚点替换即可 **0 LLM 调用**瞬间修正，不改已过审正文、不伤导出格式；
   - 同时覆盖 `_commit_chapter`、`_calibrate`、`_repair_audit_chapters` 三处写正文入口，确保校准改稿后标题不会二次脱节；
   - 放弃把 `titleAdvice` 塞进 `verify_chapter` 主审校 JSON（避免干扰审校结构、且终版正文可能在返修后才定型，提交前独立核对最干净）。
3. **WF-09 融合 Opus 5.5 的 `writing_profile` + `thinkingModeSource` 与 Astra 的“保守能力表 + 400 拒参单次回退缓存 + 放宽输出预算”**：
   - 严格区分用户显式关闭与系统默认，绝不在后台违背用户显式 `disabled`（仅打一次 `warn` 日志和前端提示），旧任务缺省视为用户显式以保 100% 行为兼容；
   - 保守能力矩阵只给确证支持的 `deepseek` 非推理模型（及 Vertex 现有白名单）下发切换参数，未知网关/Anthropic/OpenAI 兼容接口绝不盲发非标字段，再叠加 HTTP 400 拒参一次回退缓存，零崩溃风险；
   - 开启思考时仅作用于 `chapter_writing` / `chapter_repair` 并通过 `protected_output_budget` 放宽 `max_tokens`，防止思考 Token 挤断正文。

### 最终定稿落地规范
- `novel_forge/entity_salience.py`（新建）：`entity_index(facts, *, before_chapter)`（按章去重严格 `< before_chapter`）、五层判定 `entity_tier`、衰减排序 `salience`、`retired_roster`（≤30）、`stale_subplots`（默认 35 章冷退 + 扣除 `subplotClosures`）。
- `novel_forge/chapter_title.py`（新建）：`title_body_conflicts(title, body)`（中阿数字+通用量词+成语黑名单+同主体/动作锚点）、`local_retitle(title, conflicts, body)`、`strip_conflicting_number(title, conflicts)`。
- `novel_forge/thinking_policy.py`（新建）：`writing_profile(job, book_plan, *, recent_chapters=())`、`thinking_capability(provider, model, base_url="")`、`resolve_writing_thinking(config, profile, capability)`。
- `novel_forge/prompts.py` & `long_memory.py` & `storage.py` & `pipeline.py` & `llm.py`：接入写章实体投影与 `retiredRoster`、`arc_archive` 可选 `subplotClosures`、提交/校准/终审修复前 `_reconcile_title`、`write_config` 思考保护 + 400 拒参缓存降级；保持所有 V1/旧快照测试 100% 兼容。
- 前端：`web-gemini/src/lib/types.ts`、`CreatePage.tsx`（增加「自动（题材保护）」默认思考策略）、`MonitorPage.tsx`（展示 `writingProfile` 风险提示）。

---

## 阶段四：写中审校与返修（`chapter_verify` / `chapter_repair` · WF-10 / WF-11 / WF-12 / WF-13）

- **状态**：已完成并验收通过（后端 139 项相关回归全绿，200 章压测审校 13,265 / 返修 11,162 Token，前端编译通过）

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **WF-10 审校返修预算** | 审校/返修均默认 `16,384`（实发 ≤15,360），复用 `fit_prompt`，保护区超限报专属错误码停断点；要求拆分 `verify` 与 `repair` 真实用量账目 | 审校默认 `14,336`、返修 `12,288`，新增 `on_overflow="expand"`（保护区超限时清空弹性槽并临时放宽记 `warn`，绝不卡死写作）；新增 `ReviewContext` 每章只算一次候选池、每轮仅按当前正文重排；全 `pretty=False` | 审校/返修均 `16,384` + `pretty=False` + 复用 Stage 3 实体裁剪；200 章从 18.63M 压至 ~3–4M |
| **WF-11 账目算术校验** | 三步对账（定账户 → 分别定位收支与余额 → 归一化验算）；**严格区分正文硬错（返修）vs 抽取错误（丢弃/修正 delta 不改好正文）vs 证据不足（告警）**；覆盖 `verify` 与 `extract_missing_facts` 双路径 | 新建纯模块 `ledger_checks.py`：支持中阿数字与书内声明的单位换算 `unit_conversions(book_plan)`（不硬编码十六两制）；A 增量自洽 / B 余额连续性断裂 / C 动宾方向 / D 同句加总四类检查，台词数字降为 `warning`；保护区注入 `numeric_claims` | 自然正文数字取证取代报表式正则；同系单位换算 + 方向/归零校验；无据 delta 拒绝入账 |
| **WF-12 本地确定性门禁** | 元文本必须**同时扫描叙述与对白**（覆盖第 80 章台词）；细化同句书名号/典籍白名单，防全章豁免；分离动作级别与展示级别 | `find_meta_text` 含 5 类精细白名单（书名号/古籍文献词/游戏面板/穿书降级/成语），距本章 ≤30 章元章号为 fatal `error`；`run_deterministic_checks` 复用 Stage 3 标题冲突输出 `title_mismatch` (`warning`)，不逼正文迁就错误标题 | `meta_chapter_leak` 叙述+对白硬拦（不可 `needs_review` 糊弄）+ 标题冲突 `warning` 复用 |
| **WF-13 长程查重与套语滑窗** | 基于当前正文查询全书归档（不先截 80 条），最多喂 4 张旧事件卡；`core_scene_repetition` 需双侧证据；身体反应按族+章数+语境（排除雨淋/字面搬运）判定 | `repeatRecall` 槽：保留近 12 条 `usedBeats`（保旧测试）+ 新增全书 4 字重合度 `recall_used_beats` Top-6 + Stage 2 `theme_hits` Top-4；10 组通用刻板反应族（仅扫叙述，近 5 章 ≥4 章命中为非致命 `error`，全书过热喂给写章冷却）；**修复新套语规则不短路模型事实审校** | `_review_notes` 接入 `theme_hits` + 远期相关 `usedBeats` Top-4；5 组高频反应 5 章滑窗拦截 |

### 选方案与融合理由
1. **WF-10 采纳 Opus 5.5 的“`ReviewContext` 单章单算 + `on_overflow='expand'` 不卡死 + 紧凑 `pretty=False`”并吸纳 Astra 的“分账统计”**：
   - 审校 `14,336`、返修 `12,288`（环保可配，`0` 兼容回退），正常 200 章把 `chapter_verify` 从 **18.63M 直接压到 ~4.5–5.5M（砍掉约 70%）**；
   - 相比保护区超限直接抛错停断点（Astra），Opus 5.5 的 `expand` 策略对偶发 4,500 字超长章节更友好——自动清空可选槽、保住完整正文与阻断项并打 `warn` 日志，避免无谓中断无人值守长跑；
   - 采纳 Astra 的单次调用分账建议，同时保持既有 `chapter_verify` 总计兼容，便于精准复盘。
2. **WF-11 融合 Opus 5.5 的 `ledger_checks.py`（A–D 四类检查 + 书内动态 `unit_conversions` + `numeric_claims`）与 Astra 的“正文硬错 vs 抽取错 delta 分流”**：
   - 绝不在代码里硬编码“一斤十六两”，而是从书自身的 `canonLocks.currencyAndEra` / `styleBible` 解析换算规则，全题材通用；
   - 最关键的防误伤护栏来自 Astra：如果正文没算错、只是模型把 `factsDelta` 抽错了符号，绝不能把好正文打回重写，而是**舍弃不可靠 delta 并打 `warning`**；只有正文叙述本身出现算术或余额断裂硬证时才报 `error` 返修，且 `verify_chapter` 与 `extract_missing_facts` 走同一检验函数。
3. **WF-12 融合 Opus 5.5 的 5 类精细白名单与 Astra 的“叙述+对白全扫 & 同句局部白名单”**：
   - 必须同时扫叙述和对白（否则抓不住第 80 章台词「把第78章大秦五原关樟木箱里……」），且白名单严格限定在命中词前后 8 字同句范围，不能整章出现一个《史记》就放过全章元文本；
   - `meta_text` 属于 fatal error，禁止经 `can_save_unverified_draft` 降级糊弄放行；`title_mismatch` 固定为 `warning`，交给 Stage 3 提交前改题。
4. **WF-13 采纳 Opus 5.5 的 `repeatRecall` 槽（保留近 12 条 `usedBeats` + 远期 4 字召回 `recall_used_beats` Top-6 + `theme_hits` Top-4）与 10 组刻板反应滑窗 + 取消新套语短路**：
   - 既 100% 兼容既有 `usedBeats` 恰好 12 条的快照测试，又能把第 30–32 章的“保暖内衣/高脚椅”在第 51 章直接拉到审校眼前；
   - 修正 `verifier.py` 风格检查直接短路跳过模型审校的隐患：新增滑窗反应规则绝不跳过模型事实审校，且仅在 `chapter_ai_gate=True` 时升级为非致命 `error`（关闭时为 `warning`）。

### 最终定稿落地规范
- `novel_forge/ledger_checks.py`（新建）：`parse_amount`、`unit_conversions`、`ledger_label_aliases`、`assess_ledger_delta`（A 增量自洽 / B 余额连续性 / C 动宾方向 / D 同句加总 + 区分正文硬错与抽取错 delta 清洗）、`numeric_claims`。
- `novel_forge/prompt_budget.py`：`fit_prompt` 增加 `on_overflow="raise"|"expand"`；新增 `read_review_input_budget(kind="verify"|"repair")`（默认 `14_336` / `12_288`，`0` 回退）。
- `novel_forge/chapter_style.py`：新增 `find_meta_text`（全扫描叙述+对白 + 5 类同句白名单）、`reaction_counts`、`reaction_window_issues`（10 组反应族、仅扫叙述、前 5 章豁免）；`run_deterministic_checks` 支持 `title` / `book_plan` / `reaction_history`，`meta_text` 阻断 `can_save_unverified_draft`。
- `novel_forge/prompts.py` & `verifier.py` & `pipeline.py`：新增 `build_review_context`、`recall_used_beats`；`build_verify_prompt` / `build_repair_prompt` 接入 `fit_prompt(on_overflow="expand")` 与 `pretty=False`；`ledger_arithmetic_conflicts` 包装 `assess_ledger_delta` 并于补提路径共享；`pipeline.py` 维护内存 `reaction_cache` 传给 `review_chapter_stages`。

---

## 阶段五：单元归档闭环（`arc_archive` · WF-14）

- **状态**：✅ 已落地并验收通过（`gpt-6.2-sol` 完成后端，`113/113` 单测与回归测试全绿；前端 `FactsPage.tsx` 跨章偏差台账与人工忽略/恢复已补齐并通过 `tsc + vite build`）

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **持久化与防丢失** | 独立 SQLite 表 `arc_drift_runs` / `arc_drift_findings` / `arc_drift_steps`；带 `evidenceState`、`operationId` 幂等回执与书级硬额度（200 章默认最多尝试 20 章） | 复用 `canonicalFacts.driftLedger[]` 列表（天然随 `facts:updated` 推送、随 `_rollback_facts` 截断、不被 `_invalidate_archives` 误删）；每条带 `id/status/reason/autoRepairAttempts` | 在 `arcSummaries[].driftChecks[]` 扩展 `status` 与 `driftRepairAttempted`，配纯函数 `unresolved_drift_checks` |
| **分级与准入** | 区分优先当场修（唯一原文+单段+唯一正解：`alias/value/clock`）/ 持久待复核 / 人工驳回；每单元 ≤2 章、全书 ≤20 章 | 严格五重准入（仅刚完成的新单元、引文唯一定位、非 `needs_review`、未自动回修过、`value/alias/clock/v2 canon`），每单元 ≤2 章、每章终生最多 1 次自动回修 | 高置信 `canon/value/clock` 单单元 ≤2 章、每章 1 次定向修复 |
| **局部修补 vs 整章重写** | 服务端切出单段（≤160 字、净变 ≤40 字）局部替换 + 一次整章 `verify_chapter` + 按章序重放 `factHistory` + 单元内后续章本地影响检测 | **精简窗口局部补丁 `build_drift_patch_prompt`（仅引文前后 600 字，输入 4–6k）+ `apply_text_patches` 八重本地护栏**（唯一命中、字数 ±3%、禁改章末 380 字钩子、禁新元文本）+ 事前 `cascade_risk` 预判 + 事后单元内本地下游复核（失败整章回退）+ 连续历史 `rebuild_canonical_facts` | 定向返修提示词 + 1 次 `verify_chapter` + `save_verified_chapter` |
| **防死循环闭环** | 批次唯一键 `(jobId, arcIndex, start, end)` + 候选工作区 + 事务内刷新摘要并保留 `driftRepairAttempted=true` | **四重终止锁**：原子事务 `apply_arc_drift_repairs` 绕过失效逻辑原地刷新本单元 `sourceHash`（仅失效相交 10 章卷摘要）+ `driftRepair.state: pending→done` + `autoRepairAttempts <= 1` + 仅限新单元 | 先落盘归档，回修后本地刷新 `sourceHash` 并置 `driftRepairAttempted=True` |

### 选方案与融合理由
1. **持久化存储采纳 Opus 5.5 的 `canonicalFacts.driftLedger[]`（而非 Astra 新建三张 SQLite 表）**：
   - `canonicalFacts` 已经天然接入了全链路 SSE 推送（`facts:updated`）、回滚截断（`_rollback_facts`）与 JSON 导出，而且 `_invalidate_archives` 不会触碰 `driftLedger`，工程复杂度只有建三张新表的 1/4，同时完好覆盖老书兼容（无 `driftLedger` 时由已有 `arcSummaries[].driftChecks` 按需派生 `deferred` 视图，不触发旧章自动回修）。
2. **局部修补与防连锁崩坏采纳 Opus 5.5 的“`build_drift_patch_prompt` 局部补丁 + `apply_text_patches` 八重本地护栏 + 事前 `cascade_risk` / 事后本地下游校验”并融入 Astra 的“全书 10% 章节硬配额 + 刷新 `committed` 内存视图”**：
   - 局部窗口补丁只传引文前后 600 字（4–6k Token，不传全章），绝不改动章末 380 字悬念钩子与段落结构，成本极低且不伤已过审的好剧情；
   - 修补前预判同单元后续章是否已沿错误余额继续推演（`cascade_risk` 直接转 `deferred` 不白花调用），修补后本地对单元内后续章跑 Stage 4 `ledger_conflicts` 与别名/时钟校验，发现连锁冲突立刻整章回退；
   - 融合 Astra 的两点关键补强：① 增加**全书自动回修上限（默认 `max(4, total_chapters // 10)` 章，200 章封顶 20 章）**，彻底锁死最坏成本；② 原子提交后必须同步刷新 `_run_once` 内存中的 `committed`、`running_words` 与被失效的 10 章卷摘要，防止后续写章或卷摘要读到旧稿。
3. **防死循环采纳 Opus 5.5 的四重终止锁 + 原子事务 `apply_arc_drift_repairs`**：
   - 在同一个 SQLite 事务内校验 `expected_source_hash` → 写修补章节 → 替换/重建连续历史事实 → **原地刷新本单元 `arcSummary.sourceHash` 并置 `driftRepair.state="done"`** → 仅失效相交 `volumeSummaries` → 更新 `driftLedger`。四道锁任意一道都能保证 0 死循环，且不需要为同一单元重跑一遍 `arc_archive` 模型调用。

### 最终定稿落地规范
- `novel_forge/drift_repair.py`（新建纯逻辑模块）：`drift_id`、`classify_drifts`（五重准入 + `cascade_risk` 预判 + 单元 ≤2 章 & 全书 `max(4, total // 10)` 硬帽）、`apply_text_patches`（八重护栏，含 Stage 4 `find_meta_text` 检查）、`downstream_light_check`。
- `novel_forge/long_memory.py`：`drift_ledger(facts)`（旧书自动派生）、`merge_drift_entries`、`unresolved_drifts(facts, *, before_chapter=None)`、`drift_stats(facts)`。
- `novel_forge/storage.py`：`save_arc_summary` 同步登记 `driftLedger` 与 `driftRepair={state:"pending", tries:0}`；新增 `apply_arc_drift_repairs` 原子事务与连续历史 `rebuild_canonical_facts`；`_save_chapter` 与 `apply_manuscript_revision` 调用 `_refresh_drift_ledger` 更新引文失效状态；`_rollback_facts` 按章截断 `driftLedger`；新增 `set_drift_status`。
- `novel_forge/prompts.py` & `pipeline.py` & `llm.py` & `app.py`：新增 `build_drift_patch_prompt`；`previousArcDriftChecks` 改为从 `driftLedger` 选相关未决项（≤6 条，过滤已修复）；`_archive_arc` 末尾接入 `_repair_arc_drifts`（支持 `NOVEL_FORGE_DRIFT_REPAIR=0` 关闭，限流连续 2 次失败安全转 `deferred` 不卡主流程），提交后刷新内存 `committed` 并触发 Stage 2 `_catch_up_volumes`；`app.py` 新增 `PATCH /api/jobs/{job_id}/drifts/{drift_id}`；前端 `FactsPage.tsx` 展示持久化硬伤状态并支持人工标记/驳回。

---

## 阶段六：完本终审（`full_audit` · WF-15）

- **状态**：✅ 已落地并验收通过（`gpt-6.2-sol` 完成后端，`139/139` 必跑套件 + `185/185` 扩展回归全绿；前端 `MonitorPage.tsx` 完本检查升级与 `audit/accept` 人工兜底出口已补齐并通过 `tsc + vite build`）

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **全书确定性体检** | `collect_book_diagnostics(snapshot)` 从校准后一致快照单次顺序扫全书；强调 `coverage` 覆盖率可见；区分仅白名单风格 `needs_review`（兼容既有测试放行）vs 硬事实 `needs_review` | `book_diagnostics.py` 将全书信号分 `hard / cap / advisory` 三级（覆盖未解 `driftLedger`、元文本、账目断裂、反应过热、连续骨架相似、旧套路复用、未回收伏笔与退场角色复活）；终审前本地自动改题；有 `hard` 项封顶 69 分 | `audit_diagnostics.py` 聚合未解 `driftChecks`、元文本、标题冲突与节奏统计，有 `hardBlockers` 封顶 75 分 |
| **终审视野与预算** | `NOVEL_FORGE_AUDIT_INPUT_BUDGET` 默认 `24,576`（实发 ≤23,552），保护区包含“确证总数 vs 展示数”与 `coverage`；高危证据占 40% 成对保留双侧证据；砍掉 200 章首尾摘录/213 龙套/`items[-30:]` | 默认 `28,672` + `on_overflow="expand"` 不卡死 + `pretty=False`；8 个高信噪比弹性槽（`volumes`、`riskEvidence` 12 章窗口、`planEvidence`、`foreshadowing`、`finaleChapters`、`coreCast+retiredRoster`、`rhythm`、`deferredDrifts`） | 默认 `24,576` 复用 `fit_prompt`，以 `volumeSummaries` + 高危证据 + 核心实体替代全量噪音 |
| **双层裁决与防橡皮图章** | 严格校验模型响应字段与引文可定位；`auditPassed = localHard==0 && modelVerdict=="pass"`；硬阻塞上限 59、系统克隆上限 79、广泛告警上限 89；单轮最多修 2 章、整次 ≤4 章，传结构化 finding 而非纯章号 | `audit_judge.py` 校验模型阻断项须正文引文定位（无据降级 `advisory`，防幻觉死循环）；`score = min(rawScore, scoreCap)`；三态 `verdict: ready / revise_recommended / blocked`；按 `manuscriptHash` 缓存续跑 0 调用；每轮 ≤6 章、单章终生 ≤1 次，用局部补丁+本地下游检验替代昂贵 LLM 下游复核；新增 `POST /api/jobs/{id}/audit/accept` 确认带问题完本 | 合并本地 `hardBlockers` 与模型报告，强制钳位 `scoreCap`，每轮 ≤6 章，失败保留 `AUDIT_GATE_FAILED` 与结构化诊断 |

### 选方案与融合理由
1. **确定性体检与分数封顶融合 Opus 5.5 的 `hard / cap / advisory` 三级分账与 Astra 的“`coverage` 覆盖率 + 白名单 `needs_review` 兼容”**：
   - 仅含可降级风格问题的 `needs_review` 章节必须继续放行（保住 `test_python_pipeline.py:404` 既有契约），只把硬事实/账目/元文本/未解 `value/clock/alias` 硬伤划进 `hardBlockers`；
   - 任何 `hardBlocker` 存在即令 `passed = False` 且 `scoreCap <= 69`（即使模型客气打 98 分也会被代码强制封顶），彻底消灭“98 分橡皮图章”；
   - 无 `hardBlocker` 但存在系统性节奏克隆（连续高相似骨架 ≥6）/刻板反应过热/大量旧套路复用时，封顶分落到 ~72–79、输出 `verdict: "revise_recommended"`，**允许完本并给出清晰的修订入口**，绝不让 200 章死卡在终审循环。
2. **终审预算采纳 Astra/Gemini 的 `24,576` 上限 + Opus 5.5 的 `on_overflow="expand"` 与 8 槽结构**：
   - 彻底淘汰全量 213 角色、`items[-30:]` 和 200 章全量首尾摘录，单次终审从 >15 万 Token 压到 ≤24.6k（砍掉 84%）；保护区额外写明 `coverage` 和“总硬伤数 vs 已展示样本数”，避免模型误以为未展示的就不存在。
3. **防死循环与有限复修采纳 Opus 5.5 的“`manuscriptHash` 缓存 + 局部补丁优先 + 本地下游检验 + `audit/accept` 人工兜底出口”并采纳 Astra 的“传结构化 finding 而不是纯章号”**：
   - `_repair_audit_chapters` 接收结构化 finding，有 `quote` 的硬伤走 Stage 5 局部补丁（1 次小调用），下游验证改走 Stage 5 本地 `downstream_light_check`（省去原来每章额外 3 次 LLM 下游复核！），单轮上限 4 章、每章全生命周期上限 1 次；
   - 新增 `POST /api/jobs/{id}/audit/accept` 允许作者在完整性与篇幅均达标时显式确认带问题完本并留痕 `override`，作者永远不会被困在终审环节。

### 最终定稿落地规范
- `novel_forge/book_diagnostics.py`（新建）：`collect_book_diagnostics(job, chapters, facts, outlines, arc_plans, *, book_plan)`（`hardBlockers` / `capItems` / `advisories` / `scoreCap` / `coverage` / `stats` / `highRiskChapters`）。
- `novel_forge/audit_judge.py`（新建）：`ground_blockers(report, chapters)`、`adjudicate_audit(report, diagnostics, chapters)`（`verdict: ready | revise_recommended | blocked`、`rawScore`、`score`、`scoreCap`）、`select_audit_repairs(blockers, repair_ledger, *, limit=4)`。
- `novel_forge/prompt_budget.py`：`DEFAULT_AUDIT_INPUT_BUDGET = 24_576`、`read_audit_input_budget()`。
- `novel_forge/prompts.py`：`build_full_book_audit_prompt` 支持 `diagnostics` 与 `input_budget`（`on_overflow="expand"`、`pretty=False`），无 `diagnostics` 且无 `input_budget` 时兼容老调用。
- `novel_forge/pipeline.py` & `storage.py` & `app.py` & `llm.py`：`_audit` 增加终审前本地改题、同版 `auditCache` 复用、结构化有限复修（优先 Stage 5 局部补丁 + 本地下游检验）、双层 `_completion_gate` 字段升级；新增 `POST /api/jobs/{id}/audit/accept`；更新前端 `MonitorPage.tsx` 完本检查区展示 `verdict`、原分→封顶分、硬阻断列表与「前往修订 / 确认带问题完本」按钮。

---

## 阶段七：运行时与长周期配额限流调度（`llm.py` / `pipeline.py` · WF-16）

- **状态**：三方方案已收束定稿，待阶段六完成后由 `gpt-6.2-sol` 落实

### 三版方案对比

| 维度 | 方案 A（GPT-6.2-Astra） | 方案 B（Claude Opus 5.5） | 方案 C（Gemini） |
|---|---|---|---|
| **统一解析与 300s 分界** | `rate_limits.py` 纯函数 `classify_rate_limit`，以 300s 为自动等待上限；**仅在 `x-ratelimit-reset-*` 对应 `remaining=0` 时采信该维重置时间**（防有余额的日窗口误杀 30s RPM 限流）；拒绝无时区裸时间，账单不足附带的 `Retry-After: 2` 不显示“2秒恢复” | `rate_limits.py`（`ResetHint`、`parse_duration`、`parse_reset_hint`、`retry_at_iso`、`humanize_wait`、`is_long_quota`），按「响应头 > JSON（含 Gemini `retryDelay`）> 正文 8000 字」覆盖英文复合时长/中文时长/RFC 7231 日期；计费词优先于短时数字保老测试兼容 | `_parse_reset_delay_seconds`（4000 字 + `Retry-After` 秒数/HTTP-date + 复合/中文时长）+ 300s 阈值 |
| **SIWC 与全通道对齐** | 打通 `authorized()` HTTP 429 与 `_stream_responses()` SSE 流事件两路，保留原生 `SIWC_USAGE_LIMIT` 且禁止对已收流事件自动重放 | `responses_error(status, payload, headers=None)` 接管响应头；`SIWC_USAGE_LIMIT` 进 `_MANUAL_RESUME`，短时 `SIWC_RATE_LIMIT` 进 `_TRANSIENT_ERRORS`；新增进程内 `_quota_blocks[(circuit_key, model)]` 快速失败 | `SIWC_USAGE_LIMIT` 进 `_MANUAL_RESUME`，透传 `retry_at` |
| **运行时调度与重启保护** | **发现 `recover_interrupted_jobs()` 漏排除 `QUOTA_EXCEEDED`**，要求与 `run_pipeline()` 共享手动恢复判据；长等待不污染 `_rate_slot` 共享冷却也不计入网络熔断；同断点连续 3 次作业级 429 转 `QUOTA_SUSPECTED` 手动断点 | 去掉 `Retry-After` 的 60s 硬截断；内层遇 >60s 直接上抛交外层 `_cooldown` 尊重服务端提示 + 抖动；同进度连续 4 次且累计冷却 ≥15 分钟转 `QUOTA_SUSPECTED`；`/resume` 对未到点同模型续跑返 `quotaPending` 提醒 | `_can_auto_retry` 增加 `retry_after_ms > 300_000` 硬拒绝，短时 `_cooldown` 取 `max(step, retry_after_ms)` |
| **前端体验** | 修复 `format.ts` 裸 `/quota/i` 误匹配分钟限流的问题；完整本地日期时间展示 `retryAt` | `format.ts` 统一识别 `QUOTA_EXCEEDED`/`SIWC_USAGE_LIMIT`/`QUOTA_SUSPECTED` 并展示“预计 M月D日 HH:mm 恢复（约 X 小时 Y 分）”；监控页支持倒计时与换模续跑 | `format.ts` 拼出本地预计恢复时间与切换模型提示 |

### 选方案与融合理由
1. **解析器以 Opus 5.5 的 `rate_limits.py` 为实现骨架，严格植入 Astra 的三项防误报护栏**：
   - ① 仅在 `x-ratelimit-reset-*` 对应的 `x-ratelimit-remaining-* == 0`（或错误文案明确点名该维）时才采信该头，绝不让仍有余量的 15 小时日窗口误判 30 秒 RPM 限流；
   - ② 计费词（`insufficient_quota`/`余额不足`）附带的 `Retry-After: 2` 仍归为不可自动重试的计费类 `QUOTA_EXCEEDED`（100% 兼容 `test_python_llm.py:299`），且不生成误导性的“2 秒后恢复” `retryAt`；
   - ③ 覆盖 `Retry-After` 秒数与 RFC 7231 HTTP-date、JSON `retryDelay`/`resets_at`、8,000 字符正文中的 `15 hours, 3 minutes` / `15h 3m` / `90 minutes` / `54000s` / 中文「15小时3分钟后重置」，彻底去掉原有 60 秒截断。
2. **运行时防空转与重启防复发融合 Opus 5.5 与 Astra**：
   - `QUOTA_EXCEEDED`、`SIWC_USAGE_LIMIT`、`QUOTA_SUSPECTED` 统一进入 `_MANUAL_RESUME` **且共用一套判据接入 `recover_interrupted_jobs()`**，确保服务重启后绝不悄悄拉起还在 15 小时冷却期的任务；
   - 长周期错误（`is_long_quota` 或 `retry_after_ms > 300_000`）内外两层均 0 重试、不污染 `_rate_slot` 共享节流与网络熔断、进程内 `_quota_blocks[(circuit_key, model)]` 快速失败（`test_connection` 与换模型均不受阻）；
   - 无提示普通 429 设同断点连续 **4 次**作业级 429 止损转 `QUOTA_SUSPECTED` 手动断点（有进度推进即清零计数），彻底杜绝伪装成短限流的长周期配额空转；
   - 短时 429（≤300s）：内层仅等 ≤60s，>60s 上抛给外层 `_cooldown` 按服务端建议值 + 抖动冷却，绝不提前撞墙。
3. **前端精准文案与去误判**：修复 `format.ts` 对分钟级 `quota` 的误匹配，在监控页清晰展示本地恢复时间（`预计 M月D日 HH:mm 恢复`）与一键换模续跑入口。

### 最终定稿落地规范
- `novel_forge/rate_limits.py`（新建纯逻辑模块）：`ResetHint`、`parse_duration`、`parse_reset_hint(*, headers=None, text="", payload=None, now=None)`（含 `remaining == 0` 校验）、`retry_at_iso`、`humanize_wait`、`is_long_quota(error)`。
- `novel_forge/llm.py`、`openai_siwc.py`、`openai_responses.py`：`_http_error` 与 `responses_error` 接入 `parse_reset_hint`（去掉 60s 截断，透传 `headers`、`retry_after_ms`、`retry_at`、`quota_kind`、`reset_source`）；保留 SSE 流禁止重放保护；`call_llm` 对长周期或 >60s 直接上抛；长周期错误不污染 `_rate_slot` 与熔断器，并增加进程内 `_quota_blocks` 快速失败（可设 `NOVEL_FORGE_QUOTA_FAST_FAIL=0` 关闭）。
- `novel_forge/pipeline.py`：`run_pipeline` 与 `recover_interrupted_jobs` 共享手动恢复集合（含 `QUOTA_EXCEEDED`、`SIWC_USAGE_LIMIT`、`QUOTA_SUSPECTED`）；`_can_auto_retry` 拒绝 `is_long_quota(error)`；同断点连续 4 次无进展 429 转 `QUOTA_SUSPECTED`；短时 `_cooldown` 尊重服务端 `retry_after_ms`；`_failed` 持久化 `retryAt`、`quotaScope`、`quotaKind`、`resetSource`、`resumePolicy`。
- 前端：`web-gemini/src/lib/types.ts`、`format.ts`、`pages/MonitorPage.tsx` 补齐类型、精准恢复时间提示与换模续跑引导。

---


---
