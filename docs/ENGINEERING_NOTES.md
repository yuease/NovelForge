# Novel Forge 工程实践与故障复盘笔记

本文记录 **Novel Forge**（FastAPI + LangGraph + SQLite + `httpx` 纯本地长篇小说生成与修订工作台）在 50～100 章长篇实机压测中遇到的核心工程问题、根因定位、方案权衡与最终落地实现，便于回顾架构决策与复盘演进脉络。

---

## 速查总览

| # | 核心问题 | 解决思路 | 核心代码位置 |
| --- | --- | --- | --- |
| 1 | 写中 AI 味跨章传染 + 审校“幻觉挑刺” | 事前风格宪法 + 写后确定性去重 + 本地规则与原文引证双层门禁 | `chapter_style.py` / `draft_quality.py` / `verifier.py` |
| 2 | 长篇中后期上下文失控爆窗 | 保护区与优先级槽位分离 + 完整渲染计量 + 二分句末截断与回补 | `prompt_budget.py` / `prompts.py` |
| 3 | 跨 10 章记忆遗忘 + 回滚残留脏状态 | 事件溯源增量重放 + 幂等每 10 章卷归档 + 活跃上限与生命周期衰减 | `long_memory.py` / `storage.py` / `pipeline.py` |
| 4 | 强思考模型吃光输出预算与思维链泄露 | 过滤 `thought` 片段 + 65k 统一预算 + 截断扩容与拒限折半防震荡 | `llm.py` / `evaluator.py` / `revision_v2.py` |
| 5 | 100 章 135 条修订台账单次 34k+ tokens 断流 | 超过阈值自动分流为「核心台账 + 分批裁决 + 合并终检」与逐批缓存 | `revision_v2.py` / `revision_v2_checks.py` |
| 6 | 自动升版作废全书改写 + 锚点/校验过严频繁中断 | 局部增量升版保缓存 + 全局三层分级容错防线（自愈 / 精准重试 / 安全降级） | `revision.py` / `revision_checks.py` / `revision_v2_checks.py` |
| 7 | 并发熔断器半开单探测“自杀” | 冷却期快败 + 半开期并发异步挂起等单探测结果 + `epoch` 防过期响应 | `llm.py` |
| 8 | 高频轮询淹没控制台日志，故障无法排查 | 应用级结构化阶段日志 + `uvicorn.access` 只读轮询 200/304 过滤器 | `__main__.py` / `revision.py` / `revision_v2.py` |
| 9 | 长篇批跑中途崩掉导致丢章、重写或脏事实 | LangGraph 双层断点 + SQLite `BEGIN IMMEDIATE` 单事务原子提交 | `workflow.py` / `storage.py` / `pipeline.py` |
| 10 | 初稿依赖事后修订救火（审校失明、桥段撞车、数值/人设漂移、热梗复读与终局压瘪） | 数据驱动四阶段前移治理：修盲 + 329 章校准风格门禁 + 弹性单元滚动章纲 + 随章台账/人物锁与零额外调用单元快检 | `arc_planning.py` / `chapter_style.py` / `long_memory.py` / `verifier.py` / `pipeline.py` |
| 11 | 评估“强行挑刺”误报 + 修订返修 `RF-*`/`L-*` 跨编号误杀中断 | 评估去凑数分级铁律与同章不跨章合并口径 + 返修校验器兼容本批辅助编号/裸裁决号对齐并输出精准诊断 | `evaluator.py` / `revision_v2.py` / `revision_v2_checks.py` |
| 12 | 总编室深查百章爆窗 + 故障自动抢跑篡改现场 | 原生 Tool Loop 与模拟 JSON 平滑降级 + 75k 回合内/跨回合双层压缩 + 显式人工诊断与令牌恢复 | `editor_desk.py` / `manuscript_tools.py` / `app.py` |
| 13 | 本地 ChatGPT 免 Key 接入的凭证安全与流式稳定性 | 回环限定 OAuth/PKCE + Windows DPAPI / POSIX `0600` 原子保险库 + 字节缓冲 Responses 流式与 `providerState` 保真 | `openai_siwc.py` / `siwc_vault.py` / `openai_responses.py` |
| 14 | 多面板动效割裂、折叠容器裁切下拉与拖拽挤叠 | 统一动效令牌 + 视口感知 `PortalPopover` 解耦裁切 + `340–640px` 1:1 拖拽与 `@container` 容器查询 | `motion.ts` / `Overlay.tsx` / `ModelPicker.tsx` / `EditorDesk.tsx` |

---

## 一、写中 AI 味跨章传染与审校“幻觉挑刺”

### 现象
- 早期流水线中，审校只拦截硬事实冲突（伤势/秘密/字数），AI 味问题（旁白重复解释、高频身体套话、连续同主语、单段到底）只做记录不触发返修，导致不良句式经「上一章结尾片段」向后续章节持续传染。
- 尝试直接把 AI 味改成阻断门禁后，又暴露两个新问题：① 审校模型容易“幻觉挑刺”（编造原文里不存在的引文），导致返修陷入死循环；② 审校提示词仍写着“P1 不阻塞”，模型标注口径与代码门禁打架。

### 根因
1. **事后拦不如事前防**：写章提示词只有两行泛化文风预设，缺乏针对本书的语域约束与反 AI 禁令。
2. **门禁缺少“原文证据核验”**：直接信任 LLM 返回的 `aiIssues` 优先级，没有核实引文是否真存在于当章正文。
3. **审校响应异常被吞掉**：审校返回无效 JSON 时默认兜底为 `passed=True`，或误触发正文重写（修正文解决不了审校抽风）。

### 解决方案与设计权衡
- **事前预防（零新增节点、零额外调用）**：
  - 在总纲阶段（`build_book_plan_prompt`）紧凑产出本书风格宪法 `styleBible`（叙事语域、角色对白差异、考据禁区、禁用套话，封顶 300 字符，接入预算器高优槽位）；
  - 在写章系统铁律前置精炼的反 AI 味禁令。
- **写后确定性清洗（零模型成本）**：
  - 在写章后、审校前执行 `clean_chapter_draft()`：统一换行空行、剔除重复章标题，并检测裁剪与上章末尾高度重复的开头（严格护栏：仅查上章尾 480 字 vs 本章头 240 字，至少连续 2 句且 ≥40 有效字完全匹配，不碰对白，裁剪上限 `min(160字, 10%)` 且不跌破字数下限）。
- **写中双层门禁**：
  - **本地确定性规则先行**（`find_chapter_style_issues`）：检查叙述句破折号密度、心理引导套语密度、连续 5 句同主语起笔、≥800 字单段到底（排除对白、按 `\n+` 分段）。命中后直接携带已有硬错误进返修，**省掉一次无效的首次模型审校调用**；
  - **模型 P0/P1 原文核实**（`verified_ai_blockers`）：对 `quote` 做 NFKC、去空白、常见全半角标点归一化及省略号跨段匹配（段间限 320 字符），只有真正在当章命中原文的 P0/P1 才转阻断，P2 仅作建议；
  - **同步提示词口径 + 独立异常**：同一开关同步控制审校/返修提示词与门禁；审校 JSON 不可解析时直接抛 `CHAPTER_REVIEW_INVALID_JSON` 保留断点，不误触发正文返修。

### 核心代码
- `novel_forge/chapter_style.py`：`verified_ai_blockers()`、`find_chapter_style_issues()`、`can_save_unverified_draft()`
- `novel_forge/draft_quality.py`：`normalize_style_bible()`、`clean_chapter_draft()`
- `novel_forge/verifier.py`：`verify_chapter()`、`review_chapter_stages()`

---

## 二、长篇中后期上下文膨胀与爆窗（自研 Token 预算器）

### 现象
写到 30～50 章后，角色列表全量注入，加上伤势、物品、秘密和伏笔累积，单次写章 prompt 不可控膨胀，挤压模型输出空间甚至触发超窗。

### 根因
- 原事实筛选只按**条数**裁剪（如 22 条秘密、28 件物品），未按 **Token** 计量；单条长描述或百名角色仍会静默突破上限。
- 简单按字符长度硬截断会砍断句子、破坏 JSON 结构，甚至误砍本章章纲或世界观硬规则。

### 解决方案与设计权衡
- **保护区 vs 可分配槽位分离**：
  - **恒定核心保护区**：系统写作铁律、总纲核心锚点（含 `worldview.coreRules`）、完整本章章纲、终章完结铁律。体量恒定可控，绝不被误砍；若保护区自身超限则在发请求前立即报 `CHAPTER_PROMPT_BUDGET_EXCEEDED`（不可自动重试），保留断点、零浪费调用。
  - **优先级槽位**（默认总预算 16,384 估算 Token，预留 1,024 缓冲）：依次按 `硬约束/期限承诺 → 伤势/物品/秘密/时间线 → 卷摘要 → 本书风格指南 → 上章末尾 → 滚动摘要 → 角色卡 → 活跃支线 → 伏笔 → 梗样例` 排序。
- **渲染后计量 + 二分句末截断 + 两轮回补**：
  - 每次计量都针对**实际渲染后的完整 `system + "\n" + prompt`**（覆盖 JSON 括号、分隔符与标题开销）；
  - 文本槽位采用**二分查找**寻找最大可容纳长度，再回退到句末标点或换行（`from_end=True` 保留上章最末完整句），无标点极端文本按完整 Unicode 码点兜底，绝不劈开 emoji 或产生非法 JSON；
  - 第一轮按份额装入，第二轮按优先级将低优槽位未用完的余量全部回补给高优槽位。
- **为什么不引入 `tiktoken` 或外部二进制分词器？** 保持零重型二进制依赖、纯本地轻量部署，使用统一的中英文混合估算公式并在全链路同一口径验收，再叠加 1,024 Token 安全缓冲即可可靠兜底。

### 核心代码
- `novel_forge/prompt_budget.py`：`fit_chapter_prompt()`、`fit_text()`、`read_chapter_input_budget()`
- `novel_forge/prompts.py`：`build_chapter_write_prompt()`

---

## 三、跨 10 章记忆遗忘与回滚脏状态（事件溯源长记忆）

### 现象
1. 写过第 10 章后，模型迅速遗忘第 3 章确立的主角目标、人物情绪/关系转折、期限承诺与未完支线。
2. 历史事实只增不减，已完结支线和早已回收的伏笔持续污染中后期上下文。
3. 更隐蔽的数据一致性问题：如果第 3 章立下承诺（`active`）、第 8 章兑现（`fulfilled`），当回滚重跑第 8 章（`delete_uncommitted_facts(job_id, 8)`）时，若仅按 `sourceChapter < 8` 过滤，第 3 章的承诺仍停留在 `fulfilled`，无法恢复回 `active`。

### 解决方案与设计权衡
- **事件溯源（Event Sourcing）重放**：
  - 每章审校通过后，仅提取本章实际变化的轻量记忆增量（角色状态、硬约束、期限承诺、支线），做 **fail-soft 原文证据校验**（畸形条目单独跳过，绝不拖垮已通过审校的章节提交），作为版本化日志写入 `factHistory[].delta.memory`；
  - 当前状态（`characterStates`、`constraints`、`commitments`、`subplots`）全部由 `replay_long_memory(factHistory)` 按章序确定性重放得出；
  - 章节重跑或回滚时，只需截断/替换目标章及之后的 `factHistory` 并重新重放，第 7 章结束时的所有状态（包括恢复 `active` 的承诺）即可 100% 精准还原。
- **每 10 章幂等卷归档**：
  - 每满 10 章在正文和断点落库后，用辅助模型归档本卷主线摘要与最多 4 个带章号转折点，绑定 `manuscript_fingerprint()` 原稿指纹；
  - 重启幂等：同指纹卷直接复用；归档失败只补卷摘要不重写第 10 章；老任务续跑只补最近 1 个完整卷避免启动雪崩；普通字数微调/润色只使受影响卷摘要失效，不删除已提交的 `factHistory`。
- **生命周期衰减与硬上限**：
  - 活跃角色卡硬上限 **20 张**（主角必留 → 本章相关 → 最近出场，每角色最多 3 条最新关系变化）；
  - 已完结支线、已兑现/过期承诺、已回收超过 20 章的伏笔自动退出写章上下文；
  - **为什么不上 RAG / 向量库？** 小说连续性最核心的是「结构化硬约束 + 近期滚动窗口 + 卷级主线压缩」，在无向量检索的前提下用事件溯源 + 预算槽位即可确定性覆盖，免去了切块失真、共指消解误召回和额外向量库运维成本。

### 核心代码
- `novel_forge/long_memory.py`：`normalize_memory_delta()`、`replay_long_memory()`、`relevant_memory_characters()`、`active_memory_items()`
- `novel_forge/storage.py`：`save_verified_chapter()`、`delete_uncommitted_facts()`、`save_volume_summary()`
- `novel_forge/pipeline.py`：`_ensure_volume_summary()`、`_ensure_latest_completed_volume()`

---

## 四、强思考模型吃光输出预算与思维链泄露

### 现象
实机接入不可关闭思考模式的推理模型（如 Gemini 推理系列）时：
1. 单次思考即可消耗 20,000+ tokens，遇到 16k / 24k / 32k 硬编码输出上限时，JSON 或正文输出到一半直接触发 `MAX_TOKENS` 截断；
2. 截断后如果用同一偏小预算反复重试，会连续白烧数分钟和几十万 tokens；
3. 部分接口在思考耗尽、无可见正文时，若直接拼接全部 `parts`，会把 `thought=True` 的内部思维链泄露成正文。

### 解决方案
- **可见正文强过滤**：Gemini / Vertex 适配层始终过滤掉 `part.thought == True` 的片段，只返回可见正文，同时将 `thoughtsTokenCount` 准确计入用量统计。
- **全链路默认预算拉满 + 渠道拒限自动折半**：业务调用（写作、审校、评估、修订、总编、卷归档、选题）默认统一使用 `DEFAULT_MAX_TOKENS = 65_536`，同时保留 `rejects_output_limit` 自动折半机制兼容低上限渠道。
- **状态化档位调度（防震荡 & 到顶即停）**：`call_llm` 记录本次调用已试过和已被渠道拒绝的预算档位；遇到 `OUTPUT_TRUNCATED` 时只在未试过的更高档位扩容（翻倍至最高 65,536），已到顶无法再扩容时立即抛 `OUTPUT_TRUNCATED` 留断点，不再原地空转 5 次；成书评估截断时只对当前失败分段扩容重跑，不重跑已通过分段。

### 核心代码
- `novel_forge/llm.py`：`call_llm()`、`next_output_budget()`、`output_was_truncated()`、`_extract_visible_text()`
- `novel_forge/evaluator.py`：分段评估单段扩容重试逻辑

---

## 五、100 章 135 条报告「生成全书台账」单次输出 34k+ tokens 断流

### 现象
在 100 章长篇评估后修订中，原稿审计 + 评估报告累计 135 条意见。单次「锁定台账」要求模型一次性输出全书规则、检查项、财务流水、蓝图和 135 条逐条裁决，单次输出飙升至 **34,000+ tokens（500KB+）**，持续流式生成 5～7 分钟后触发上游网关 `502 unexpected EOF` 断流。

### 解决方案
- **阈值分流**（`NOVEL_FORGE_LEDGER_BATCH_THRESHOLD=30`，设为 `0` 可回滚）：
  - ≤ 30 条保持原单次调用路径（短书零额外开销）；
  - > 30 条自动拆为两阶段分批生成：
    1. **阶段 A（核心台账与蓝图）**：只生成全书 `rules`、`checks`、`financialFlows`、`blueprint`，单次输出压回几千 tokens，按输入指纹独立缓存于 `v2-ledger-core`；
    2. **阶段 B（分批裁决）**：携带冻结的核心台账，按每批 **25 条** 意见并发/顺序裁决 `decisions`（提示词强制优先用 `{"sourceId":"..."}` 替代大段抄录原文、`skip` 省略空字段），逐批落盘缓存于 `v2-ledger-decisions`；
    3. **阶段 C（合并终检）**：按原序合并后统一过现有 `validated_plan()` 终检，产出完全同构的 `v2-ledger`，下游改写/终审/总编零改动。
- **缓存与换模型友好**：缓存指纹不绑定模型名，中途断流或切换模型续跑时，原稿审计、核心台账和已通过裁决批次全部命中缓存，只补跑剩余批次。

### 核心代码
- `novel_forge/revision_v2.py`：`make_plan()`、`make_batched_plan()`
- `novel_forge/revision_v2_checks.py`：`validated_ledger_core()`、`validated_decision_batch()`、`validated_plan()`

---

## 六、自动升版作废全书改写 + 过严校验频繁中断（三层分级防线）

### 现象
在 100 章、70+ 批 LLM 调用的长程修订中暴露出两个放大器问题：
1. **整本返工放大**：独立终审发现新跨章联动回到台账阶段时，原逻辑重生成整份台账；只要模型把无关规则微调了两个字，各章 `scope` 指纹全变，导致前面已跑完的所有改写批次全部作废重来。
2. **脆弱校验连坐**：
   - 模型抄替换锚点手滑多打一个字（如原文 `，高高举起右手` 写成 `她高高举起右手`），旧报错只说「替换锚点未唯一命中」，不区分 **0 次命中** 还是 **多次命中**，模型误以为太短而在重试时往后加标点，连错两次直接中断整本；
   - 蓝图 `preserve` 带上不在 `chapters` 中的参照章、或规则 3 条证据中错 1 条、或审校 `outcomes` 漏 1 个 ID 但重试不告诉它漏了哪个 ID，都会把整批辛苦成果作废。

### 解决方案
1. **自动升版改为局部增量合并**（`make_semantic_delta()` + `merge_semantic_delta()`）：
   - 终审触发自动升版时，只针对新增 `concerns` 生成增量规则与裁决并合并进旧台账，原样保留无关旧规则、顺序、已通过改写回执与 `v2-coverage`，只派发真正受影响的章节（如 100 章里仅重跑第 8 章）。
   - 新一轮改写启动时在同一事务内同步重置 `editProgress` 与顶层 `done/total`，消除界面进度打架。
2. **全局三层分级容错防线**（覆盖台账、改写、终审全链路，可通过 `NOVEL_FORGE_REVISION_SOFT_FALLBACK=0` 关闭第三层）：
   - **第 1 层｜零成本确定性自愈**：
     - 锚点精确查找 0 命中时，按“去空白 + 常见全半角标点归一”建立字符位置映射表，**仅在唯一命中时**还原原文精确 `[start, end)` 偏移（保留数字、正负号与小数语义，多命中绝不盲猜）；
     - 规则与裁决证据统一支持 `sourceId` 与有跨度上限的省略号匹配，一条规则只要保留 ≥1 条可核实原文证据即放行；
     - 蓝图 `preserve` 校验章号存在后自动收敛为与 `chapters` 的交集；终审 `outcomes` 自动对齐 `id + chapter → id@chapter` 并对同状态重复项去重。
   - **第 2 层｜带精确诊断与原文提示的重试（上限提至 3 次）**：
     - 锚点失败明确区分 **0 次命中**（从当章原文用核心子串召回最接近的真实片段附在提示中，让模型直接复制原句，不自动替它修改）与 **多次命中（附次数）**；
     - 终审与裁决校验失败明文列出 `缺失 ID`、`多余/未识别 ID`、`状态冲突 ID`，让第 2 次重试照单补齐。
   - **第 3 层｜有界单条降级不连坐（严守安全红线）**：
     - 3 次尝试后，若仅余少量可隔离的非财务、非作者锁定口径问题（如单批 1～2 条锚点失败或一组 2 条补丁重叠、少量普通裁决缺证或终审漏项），安全应用其余有效成果，将失败项如实登记为 `needs_review` / `open` 推进后续批次；
     - **红线绝不放宽**：绝不伪装成 `patched`/`fixed`，财务流水余额不闭合、作者已锁定口径失败、非法章号、正文清空、JSON 损坏、网络与熔断错误仍严格拦截；且未核实的降级项会在最终 `apply()` 写回原稿前被门禁拦住，交由作者或总编确认。

### 核心代码
- `novel_forge/revision.py`：`model_json()`
- `novel_forge/revision_checks.py`：`audited_evidence()`、`validate_ledger()`
- `novel_forge/revision_v2_checks.py`：`unique_at()`、`edit_patches()`、`validated_plan()`、`validated_semantic_outcomes()`
- `novel_forge/revision_v2.py`：`make_semantic_delta()`、`merge_semantic_delta()`、`edit_v2()`、`soft_v2()`

---

## 七、并发熔断器“半开单探测”并发自杀 Bug

### 现象
为解决上游持续故障时单次请求耗时极长（300s × 5 次重试 = 25 分钟）的问题，引入了按「供应商 + 规范化端点」隔离的三态熔断器（连续 3 次 5xx/超时/网络故障打开，冷却 60s 后进入半开单探测）。

但在实机并发运行（并行批次 ≥ 2 或两本书共用同一代理端点）时，发现一个经典并发 Bug：
- 冷却结束后，第 1 个请求抢到唯一半开探测位（`probing=True`）并开始等待强思考模型返回（耗时 1～3 分钟）；
- 1 毫秒后，同批次的第 2、3 个并发请求到达，看到 `probing=True` 立刻在 15ms 内抛出 `CIRCUIT_OPEN`；
- 结果**即便第 1 个探测请求最终成功闭合了熔断器，整个并发批次依然因为第 2、3 个分支已经记录异常而被直接打断**。

### 解决方案
- **冷却期快败 + 半开期异步协作等待**：
  - 冷却期内（`open_until > now`）：所有请求仍立即快速失败抛 `CIRCUIT_OPEN`，不发网络请求；
  - 半开探测中（`probing == True`）：严格保持仅发 **1 个** 上游探测网络请求，其余并发到达的调用不立刻报错，而是异步挂起等待该探测结束（支持调用方独立超时、取消，以及跨事件循环安全唤醒）：
    - 探测**成功** → 熔断器闭合清零，所有等待者立即唤醒并继续发送请求；
    - 探测**失败** → 熔断器重新进入冷却，所有等待者立即唤醒并快败抛 `CIRCUIT_OPEN`，零额外上游请求；
    - 探测被**取消** → 释放探测位并唤醒等待者，由其中 **1 个** 接力成为新的唯一探测；
  - 用递增版本号 `epoch` 隔离历史在途请求的迟到结果，防止旧请求的成功响应误关新一轮熔断；429、4xx 参数错、本地取消和输出截断不计入供应商熔断故障。

### 核心代码
- `novel_forge/llm.py`：`_claim_circuit()`、`_wait_for_probe()`、`_settle_circuit()`
- `tests/test_python_llm.py`：6 项半开并发、取消接力、跨事件循环唤醒与过时响应回归测试

---

## 八、高频轮询淹没日志导致故障无法排查

### 现象
控制台 180KB 日志中 99% 都是前端 2.5s～3s 轮询的 `GET /api/jobs/.../revisions` 和 `GET /api/jobs/.../editor-desk` 访问日志，完全没有应用级阶段进出、模型耗时、重试次数和校验失败原因，排查卡点只能直接查 SQLite 表。

### 解决方案
- 在 `revision.py` 与 `revision_v2.py` 补齐精炼结构化日志（`revision_stage_start/end`、`revision_model_ok/invalid/failed`，记录 `run`、`stage`、`role`、`attempt`、`retries`、`elapsedMs`、`reason`、`ledgerVersion`、`affectedChapters`）；
- 在 `novel_forge/__main__.py` 为 `uvicorn.access` 挂载轻量 `QuietPollingAccessFilter`，仅过滤 `/api/health` 与 `/api/jobs/.../(revisions|editor-desk)` 的 `GET 200/304` 刷屏日志，完整保留写操作、导出及所有 4xx/5xx 异常请求。

### 核心代码
- `novel_forge/__main__.py`：`QuietPollingAccessFilter`、`logging_config()`

---

## 九、长篇断点续跑与单事务原子提交

### 现象
长篇写作动辄运行数小时，若在“正文已落盘、规范事实未提交”或“章节提交后、断点未更新”之间发生进程崩溃/主动暂停，重启后极易出现重复重写同一章、或未通过审校的脏事实污染事实库。

### 解决方案
- **双层持久边界**：LangGraph `AsyncSqliteSaver`（`workflow.sqlite3`）持久化图状态与节点轨迹；业务库（`novel-forge.sqlite3`）以 `BEGIN IMMEDIATE` 在同一 SQLite 事务内原子写入通过审校的正文、章节索引、字数统计与规范事实增量；`config.json` 采用同目录临时文件 + `os.replace()` 原子替换。
- **幂等恢复与优雅暂停**：续跑时以业务库已提交章节为准跳过已完成工作，未通过审校的 `needs_review` 章节计入已保存与导出但绝不提交其 `factsDelta`；暂停时立即取消在途 HTTP 请求与冷却等待，但等待已进入提交临界区的 SQLite 事务完成再退出；单进程数据目录文件锁（`claim_data_dir`）防止多实例误写同一库。

### 核心代码
- `novel_forge/workflow.py`：`build_graph()`、`Worker`
- `novel_forge/storage.py`：`Store`、`save_verified_chapter()`、`claim_data_dir()`
- `novel_forge/pipeline.py`：`PipelineRuntime`

---

## 十、初稿质量四阶段系统性治理（把事后修稿能力前移到写中）

### 现象与数据归因
在复盘 300+ 章真实生成日志、700+ 条成书评估与修订审计记录，以及一次 20 章对照实验后，发现初稿仍集中暴露 6 类结构性短板：
1. **写中审校“近视/失明”**：`build_verify_prompt` / `build_repair_prompt` / `build_fact_extraction_prompt` 调用事实筛选时未传 `context_text`（默认空串盲切最后 28 条），且未注入总纲世界观硬规则和前卷摘要，导致 20 章对照实验写中 `repairs = 0`（全标 P2 放水），完本评估却炸出多项数值与世界观硬冲突。
2. **核心桥段跨章自我撞车**：开局一次性按每批 5 章写死全书 100 章章纲，只看近 5 章摘要、看不见更早章节的已用大招与揭示桥段，导致第 4/10 章、12/16 章、13/18 章连标题和破局机制都原样复演，且同一反派连续多章无新筹码原地赖场。
3. **工具链路自带的隐藏 Bug**：① 热梗样例池每章循环投喂同一组前 2 条样本，逼着模型把同一个比喻全书复读 7 次；② 动态字数目标在前中段写超后把大结局压到 `0.65×base`（2000 字目标被压成 900～1300 字草草收尾）；③ 章纲 JSON 解析失败时静默写入占位空壳章纲继续开写，酿成正文与总纲彻底货不对板；④ `injuries` 按字面完全相等去重且永不衰减，100 章滚出 200～300 条“嘴唇发紫/贴创可贴/美甲磕断”挤爆高优槽位。
4. **AI 味换马甲与说明书台词**：传统套话被拦后，模型转向高频填充词（单章 `瞬间` 6+ 次）、嘴角微表情变体、保姆级旁白翻译与长括号报账、连续同构突转断章，以及反派临死条理清晰背供词/报数值公式。
5. **长篇时序、账目与人物状态漂移**：缺少写中结构化的剧情时钟、核心余额、角色关系档位与已确认认知锁，导致 50～100 章里出现跨章余额算错、季节/昼夜矛盾、角色无铺垫 OOC 顺从化、认知回档以及新配角重名撞车。

### 解决方案（四阶段独立开关、渐进验收）
- **Phase 1｜止血与修盲**：
  - **消耗制热梗**：持久化候选池，每章至多投喂 1 条未用样本，且仅当样本实际留在最终写章提示词并随正文原子提交（`Store._save_chapter` 同事务）才记入 `usedMemeTitles`，预算裁掉或失败均不消耗；
  - **审校补全视野 + 防误杀升格**：三处 prompt builder 统一传入本章正文+章纲构成的 `context_text`、总纲硬规则与前两卷摘要；严格只对带可核实原文引证的三类硬冲突（`hard_value_conflict` / `world_rule_conflict` / `core_scene_repetition`）自动升格阻断，绝不误杀常规 `warning/P2`；
  - **保护终局字数 & 取消空壳章纲**：末尾 1–3 章目标字数硬下限设为 `base`，其余章下限提至 `0.85×base`，完本总字数上限放宽至 `1.18×` 且缩写优先选前中段；章纲补跑仍有缺章即保存已解析部分并抛 `OUTLINE_PARSE_FAILED` 进手动断点；
  - **事实记忆瘦身与回滚**：同角色同部位唯一活跃伤势原地合并，并引入 `injuryReplayBase` 支持按章精确回滚；无 ID 的同类倒计时自动复用原 `commitment` 条目更新 `deadline`。
- **Phase 2｜329 章真文校准的写中风格门禁**：
  - 先在 329 章真实样本上校准阈值再落地 4 条零调用本地规则（均先全章识别引号再切章尾 380 字防跨窗误判、纳入非致命风格集合）：`cliche_filler_density`（叙述 `瞬间 ≥ 6` 或六词 ≥ 12 且密度 ≥ 4.5/千字，排除成语）、`mouth_arc_cliche`（单章 ≥ 2 或 3 章滑窗 ≥ 3）、`explanatory_narration`（高置信翻译句式 + 45 字以上多笔金额括号报账）、`repetitive_ending_hook`（同构突转、连续三章系统警报框、跨 3 章同组实词/拟声词意象，缺章保留空位不误认紧邻）；
  - 写章系统词补齐“禁说明书台词/背供词、禁战后抽象总结、高潮禁出戏玩梗”红线（随 `NOVEL_FORGE_WRITE_AI_GATE` 生效）。
- **Phase 3｜弹性剧情单元（Arc）滚动章纲 + 核心口径锁 + 已演桥段去重**（`NOVEL_FORGE_ROLLING_ARC_OUTLINES`）：
  - **核心口径锁（`canonLocks`）**：总纲阶段锁定力量刻度、时代货币、世界观禁区与核心资产固定属性（总限 420 字、按完整句截断防误砍数值/否定词），作为不可裁切保护锚点同时喂给滚纲、写章与审校；
  - **零孤儿章弹性滚动**：保留用户设定的 `totalChapters` 进度口径，并在 LangGraph 状态图与实时工作流面板中增加 `archive`（单元归档）节点与 `commit → archive → outlines` 滚动回环及单元进度条；剩余 `≤ 6` 章锁为终局收网单元一次规划到底，否则由模型自主决定 `[3, min(6, remaining - 3)]` 章，数学保证全书绝不留下 1–2 章孤儿尾段；单次事务写入 `outlines + arcPlans + checkpoint`，校验规划上下文指纹（`arc_context_version`）拒绝基于已被改写正文生成的过期章纲；
  - **单元归档与 `usedBeats` 闭环**：单元末章提交后归档主线、`usedBeats`、`usedHooks` 与 `villainStatus`（引文单条 fail-soft 核验），累计去重的已演桥段清单同时喂给下一单元滚纲（防自我撞车、防同反派连续两单元无新筹码赖场）与写中审校（最多 12 条供 `core_scene_repetition` 直接抓包）。
- **Phase 4｜随章台账 + 核心人物关系/认知锁 + 零额外调用单元快检**（`NOVEL_FORGE_STORY_LEDGER`）：
  - **独立事件溯源台账**：即使 `NOVEL_FORGE_LONG_MEMORY=0` 也可独立入库与按章回滚；增量维护剧情时钟 `storyClock` 与至多 6 条核心余额 `ledgerBalances`（最近 3 笔变更、同值复述不重复记账，配合 `Decimal` 精确净变动算术核对）；
  - **轻量核心人物锁（限容 12 人、按章出场按需注入）**：扩展 `stanceTier`（5 档关系，跨两档剧变需重大触发防 OOC）与 `knownFacts`（每人至多 3 条已确认认知防回档）；重名拦截（`character_name_collision`）严格要求**全名精确匹配 + 原文引证 + 原文明示为不同人**，绝不误杀同姓亲属、转岗或化名；收窄超 10 章轻中旧伤仅按伤处/伤情匹配，主角旧轻伤不再仅凭姓名永驻上下文；
  - **零额外调用单元快检（`driftChecks`）**：复用 Phase 3 单元归档单次调用，喂入**单元开始前基线 + 单元内按章已核实变化**（严防拿单元末状态倒推早章的时点穿越误判），输出单条 fail-soft 引证的 `driftChecks` 交给下一单元滚纲顺势纠偏；支持可选 `NOVEL_FORGE_ARC_REVIEW_MODEL` 仅在低频归档快检环节切换更强模型。

### 实机 A/B 对照验证（同题材、同大纲、同模型）
在相同题材与 20 章 / 40,000 字目标设定下，固定使用同一轻量写手模型（未配置独立辅助审校模型）与同一强推理评估模型，分别执行治理前基线与 Phase 1–4 全开的盲跑对照，结果如下：

| 评估指标 | 治理前基线 | Phase 1–4 全开 | 变化与归因 |
|---|---:|---:|---|
| 六维均分（1–5 分制） | 3.33 | **3.83** | **+0.50**（4 项上升、2 项持平、0 项回退） |
| 世界设定与因果 / 伏笔与结局 | 2.5 / 3.5 | **3.5 / 4.5** | 各 **+1.0**，终局评价由「偏平」升至「值得」 |
| 核心桥段跨章自我撞车 | 3 组 | **0 组** | 4 段弹性滚纲（1–6 / 7–12 / 13–16 / 17–20）+ `usedBeats` 去重 + 反派按计划退场 |
| 核心货币账本一致性 | 4 个数量级横跳 | **连续 14 章 0 误差** | `canonLocks` 锁汇率 + `ledgerBalances` 逐章精确记账 |
| 终局 3 章字数（18 / 19 / 20） | 1517 / 1440 / **1327** | 2079 / 2399 / **2475** | 终局字数下限护栏彻底消除结尾塌缩 |
| 写中拦截与风格套话 | 0 拦截 / 8 残余 / 72 套话 | **6 章一次修过 / 0 残余 / 49 套话** | Phase 2 本地门禁零额外成本压降套话 32% |
| 总 Token 消耗 | 585.5K | 776.6K | +32.6%（主要来自更足终局字数、6 次返修与 3 轮单元归档） |

**对照实验暴露的关键工程结论（“自写自审”能力瓶颈）**：
- 本次对照中 6 次写中返修全部由 Phase 2 确定性本地规则拦截，而轻量写手模型兼任 `chapter_verify` 与 `arc_archive` 时对自身局部语义穿帮高度宽容（3 轮归档 `driftChecks` 均返回空数组）。事后核查 `factHistory` 证实：`storyClock` 已准确记录某处倒计时跳变、`characterStates` 已准确锁定反派不知情状态、`canonLocks` 也已锁定限知视角禁令——**瓶颈已从“写中漏记事实”转移到“弱模型自写自审不判错”**。
- 因此生产最佳实践为：**写作可用性价比主力模型，审校/事实提取或低频 `NOVEL_FORGE_ARC_REVIEW_MODEL` 优先配置更强推理模型**，形成异构“写作 + 严审”闭环。

### 核心代码
- `novel_forge/arc_planning.py`：`normalize_canon_locks()`、`arc_chapter_span()`、`validate_arc_plan()`、`compact_used_beats()`
- `novel_forge/text_evidence.py`：`normalize_quote()`、`locate_quote()`
- `novel_forge/chapter_style.py`：`_check_new_style()`、`find_chapter_style_issues()`
- `novel_forge/long_memory.py`：`story_ledger_view()`、`story_ledger_events()`、`normalize_arc_summary()`
- `novel_forge/verifier.py`：`_confirmed_name_collisions()`、`_story_conflict()`、`ledger_arithmetic_conflicts()`
- `novel_forge/pipeline.py`：`_plan_arc()`、`_archive_arc()`、`compute_dynamic_chapter_target()`

---

## 十一、评估“去挑刺化”校准与修订返修跨编号误杀复盘

### 现象
1. **成书评估“职业病式挑刺”与合并路由隐患**：对已经多轮精修干净的成稿做二次评估时，模型仍倾向于强行凑满 `aiFlavorIssues` / `whereToSkip` / `rewritePlan`，甚至将主观文风偏好标成 `P0`；若为了降噪把跨多章的同类问题粗暴合并成 1 条，又会导致下游评估后修订与单点精修按 `chapterNumber` 路由时漏修其他章节。
2. **修订 v2 定点返修（`repair`）频繁卡死在“补丁引用了批次以外的裁决项”**：在 100 章实机修订进入 `repair` 阶段（4 批待返修）时，其中 3 批在模型输出完全合法的文本替换补丁后连续触发「补丁引用了批次以外的裁决项」，最后 1 批用尽 3 次重试直接中断。

### 根因
1. **评估缺少“零问题合法”显式许可与合并边界**：原评估提示词未明确允许干净稿返回空数组 `[]`，也未把 `P0` 严格锁定为客观硬事实冲突；而下游修订管线依赖每条问题的 `chapterNumber` 做逐章路由，合并粒度只能控制在“同章同类”。
2. **返修批次存在三套合法 ID，但校验器只认其一且报错不指明错因**：
   - `repair` 批次的输入同时包含 `requiredIssues`（带章号，如 `A11-2@74`）、`ledger.issues[].decisionId`（裸号 `A11-2`）、`feedback[].repair.id`（`RF-...`）与 `repair.basis[].ruleId`（`L-...`）；
   - 编辑模型在落实返修反馈时，自然会把本批的 `RF-...` / `L-...` 或裸号 `A11-2` 填进 `patches[].issueIds`（或 `coverage[].id`）；
   - 原 `edit_patches()` 仅做 `key in set(requiredIssues)` 硬比对，一刀切报模糊错误，既不放行本批已知辅助编号，也不在报错中列出被拒编号与合法范围，导致模型 3 次重试都无法定位问题。

### 解决方案
1. **评估去挑刺与不漏修口径校准**（`novel_forge/evaluator.py`）：
   - 在初审（`_BATCH_SYSTEM` / `_BATCH_PROMPT`）与精读（`_PRO_SYSTEM` / `_PRO_PROMPT`）中写入克制铁律：干净章节/全书可直接返回 `aiFlavorIssues: []`、`whereToSkip: []`、`rewritePlan: []`，达标维度 `fixAdvice` 留空 `""`，无真实 `P0` 且六维达标时如实评 `ready_to_submit`；
   - 明确分层规则：`P0` 仅限客观硬事实冲突且逐处独立绝不合并；`P1/P2` 仅合并**同一章内**的同类文风问题，跨章显著问题保持按章独立，保证后续精修与 Revision v2 路由零漏章。
2. **返修校验第 1 层无损自愈 + 第 2 层精准诊断**（`novel_forge/revision_v2.py`、`novel_forge/revision_v2_checks.py`）：
   - 在 `PATCH` 提示词中明示 `issueIds` / `coverage[].id` 仅填 `requiredIssues`，纯对应 `feedback/repair`（`RF-*`）或 `ledger.rules`（`L-*`）的补丁留空 `[]`；
   - `edit_v2()` 收集当前批次的 `ledger.rules`、`ledger.checks`、`feedback[].repair.id` 与 `basis[].ruleId` 构成 `known_aux_ids` 传入 `edit_patches()`；
   - `edit_patches()` 对 `patches[].issueIds` 与 `coverage` 实施确定性自愈：① 本批合法的 `known_aux_ids` 自动识别放行并从 `issueIds`/`coverage` 剥离；② 无歧义的裸裁决号（如第 74 章补丁写 `A11-2`）自动对齐为 `A11-2@74`；③ 真正越界的未知编号（如 `R999`）仍严格拦截，并在报错中明文列出被拒编号与本批合法 `requiredIssues` 供重试自纠；
   - 因 `units()` 的缓存指纹 `inputHash` 仅哈希批次输入数据、不绑定提示词，修复上线后断点续跑直接命中此前已保存的 3/4 批缓存，仅补跑失败批次即可无缝进入独立复审。

### 核心代码
- `novel_forge/evaluator.py`：`_BATCH_SYSTEM`、`_BATCH_PROMPT`、`_PRO_SYSTEM`、`_PRO_PROMPT`
- `novel_forge/revision_v2.py`：`PATCH`、`edit_v2()`
- `novel_forge/revision_v2_checks.py`：`edit_patches()`
- `tests/test_python_evaluator.py`、`tests/test_revision_v2.py`：空数组干净稿评估验收与 `known_aux_ids` / 裸号对齐 / 越界编号拦截回归测试

---

## 十二、总编室原生 Tool Loop、回合内自动压缩与人工可控断点恢复

### 现象
1. **深查 100 章长篇触发上下文爆窗**：在总编室跨数十章检索线索、连续读章核对人物与时间线时，单回合内多轮工具调用的返回文本迅速累积，容易突破模型窗口或挤占最终输出额度；切换总编模型后，早期对话结论也容易丢失。
2. **后台故障自动抢跑与限流重试放大**：早期 `revisions.on_failure` 会在修订失败后自动触发总编诊断，在遇到上游限流或临时错误时可能静默重试、干扰人工排查，甚至带来擅自变动当前候选版本的风险。

### 解决方案与设计权衡
- **原生 Tool Loop + 异构渠道无缝降级**：
  - 将只读检索与章段读取抽象至 `manuscript_tools.py`（支持关键词单查、最多 40 词批量/正则检索与分页片段返回）；
  - `EditorDesk` 在支持原生 tool calling 的渠道上启用原生多轮工具循环（上限 `MAX_TOOL_ROUNDS = 40`、每轮最多 `MAX_TOOLS_PER_ROUND = 12`），当上游渠道不支持原生工具时自动无缝回退至模拟 JSON 工具循环，保持提案生成、证据引用与落库语义完全一致。
- **双层上下文压缩（跨回合 + 回合内）**：
  - **跨回合压缩**：早期对话通过 `save_conversation_summary`（或 `compactedContext`）按 `throughId` 增量持久化摘要，保留关键章号、作者决定与未完事项，切换模型后完整承接上下文；
  - **回合内自动压缩**：单次问答中估算消息 Token 超过 `INTRA_TURN_COMPACT_TOKENS = 75_000`（支持 `NOVEL_FORGE_EDITOR_COMPACT_TOKENS` 覆写，最多触发 `MAX_INTRA_TURN_COMPACTIONS = 3` 次）时，`_compact_native_messages()` 自动将早期工具往返压缩成结构化摘要，并以确定性 `_fallback_round_digest()` 兜底，长程深查百章不爆窗。
- **人工主导故障诊断与显式令牌恢复**：
  - 移除 `revisions.on_failure` 后台自动抢跑，限流等运行时异常严格保留现场并等待人工诊断，绝不擅自变更候选版本；
  - 新增 `POST /api/jobs/{job_id}/editor-desk/recover`，仅在总编完成排查并经作者显式确认恢复令牌后才触发断点恢复。

### 核心代码
- `novel_forge/editor_desk.py`：`EditorDesk`、`_native_tools()`、`_compact_native_messages()`、`_fallback_round_digest()`、`dispatch_recovery()`
- `novel_forge/manuscript_tools.py`：`search_chapters()`、`_single_search()`、`_batch_search()`
- `novel_forge/app.py`：`editor_recover()`
- `tests/test_editor_desk.py`：原生工具循环自纠与回合内压缩、不支持原生工具无缝回退、跨模型切换记忆保留、人工断点恢复回归测试

---

## 十三、OpenAI Sign in with ChatGPT（SIWC）本地 OAuth、凭证保险库与 Responses 流式适配

### 现象
为了让纯本地工作台无需手工申请和管理 API Key 即可安全使用个人 ChatGPT 计划调用 OpenAI Responses 流式接口，需要同时解决三个本地安全与协议难题：
1. **本地凭证落盘安全**：明文写入 OAuth `access_token` / `refresh_token` / `id_token` 或允许符号链接路径存在泄露与提权风险；
2. **回环 OAuth 防劫持**：本地桌面工作台若对非回环地址开放 OAuth 回调或缺少状态绑定，容易遭遇跨站或局域网劫持；
3. **Responses SSE 协议细节**：多字节 UTF-8 字符可能跨 HTTP chunk 截断，且多轮 tool-calling 需要保真回传上游 `providerState`。

### 解决方案与设计权衡
- **严格回环隔离的本地 OAuth 2.0 + PKCE 流程**（`openai_siwc.py`、`siwc_routes.py`）：
  - 默认仅在 `NOVEL_FORGE_HOST=127.0.0.1` 回环监听时启用（`is_loopback` 严格校验），通过一次性随机 `state`、PKCE 与流程 Cookie 绑定单次授权会话；
  - 精细区分错误类型：终态刷新错误（`invalid_grant`、`refresh_token_reused` 等）立刻提示重新登录不盲目重试，瞬时账户不可用允许重试，配额与限流给出清晰中文引导。
- **系统级凭证加密保险库**（`siwc_vault.py`）：
  - 读写前拒绝任何符号链接路径；Windows 平台直接调用原生 `CryptProtectData` / `CryptUnprotectData`（DPAPI）对三类 token 字段加密存储（解密后立即 `ctypes.memset` 清零非托管内存缓冲）；POSIX 平台强制 `0o700` 目录与 `0o600` 文件权限，全部写入通过同目录临时文件 `flush + fsync + os.replace()` 原子替换。
- **字节缓冲流式解析与多轮状态保真**（`openai_responses.py`）：
  - 按原始字节流缓冲至换行符后再解析 JSON，彻底杜绝多字节中文字符跨 chunk 截断乱码，并设 `16MB` 流体积硬上限（`_STREAM_LIMIT`）；
  - 多轮工具调用中保留 `openai-responses-siwc` 原生 `output` 结构（`providerState`），确保推理链与工具结果在多轮对话中无损衔接。

### 核心代码
- `novel_forge/openai_siwc.py`：`OpenAISiwc`、`SiwcError`、`is_loopback()`
- `novel_forge/siwc_vault.py`：`SiwcVault`、`_dpapi()`
- `novel_forge/siwc_routes.py`：`siwc_router()`
- `novel_forge/openai_responses.py`：`_events()`、`_build_responses_input()`
- `web-gemini/src/pages/ChatGPTConnection.tsx`、`tests/test_openai_siwc.py`

---

## 十四、`web-gemini` 前端动效系统、视口级 Portal 浮层与高性能交互收口

### 现象
随着工作台扩展出监控流图、评估多轮对比、总编室与设置面板，前端逐步暴露四类体验瑕疵：
1. 各页面过渡时长与缓动散落各处，动效语言割裂且缺少统一的 `prefers-reduced-motion` 无障碍退避；
2. 评估页中嵌套在 `<Disclosure title="模型设置">`（`overflow-hidden`）里的 `ModelPicker` 下拉菜单被父容器裁切，无法正常滚动与选择；
3. 拖拽加宽总编室时，右侧主区 KPI 卡片因仅按视口断点而非容器宽度排版发生挤叠；移动端抽屉缺少 `Escape` 关闭且 `<Select>` 出现双箭头。

### 解决方案与实机验收
- **统一动效令牌与无障碍收口**：抽离 `web-gemini/src/lib/motion.ts` 与 `styles/index.css` / `range.css` 统一缓动和时长令牌，覆盖 8 个基础 UI 原语与全站业务页，并接入全站 `prefers-reduced-motion` 静态直切；
- **视口感知 `PortalPopover` 解耦嵌套裁切**：在 `Overlay.tsx` 实现通过 `createPortal(..., document.body)` 挂载的视口感知浮层 `PortalPopover`（`z-[95]`，自动检测视口上下剩余空间翻转定位），接入 `ModelPicker`，并让 `Collapsible` / `Disclosure` 在展开动画完成后切换为 `overflow-visible`，彻底解决任意嵌套容器裁切浮层问题；
- **1:1 跟手拖拽与 CSS 容器查询自适应**：为总编室补齐 `340–640px` 1:1 指针拖拽分栏手柄（偏好持久化至 `prefs.ts`），主区与 Hero 切换为 CSS 容器查询（`@container/main`、`@container/hero`），确保分栏宽度动态变化时卡片布局永不挤叠；同步补齐移动端抽屉 `Escape` 退出与全文 `.txt` 导出工具（`download.ts`）；
- **量化验收**：Chrome CDP 真实采样下各交互维持 **107–142 FPS**、切页 **CLS = 0**、12/12 视口组合 **0 横向溢出**，`tsc -b && vite build` 零错误，全仓库 Python 单元测试 **232 / 232 全部通过**。

### 核心代码
- `web-gemini/src/lib/motion.ts`、`web-gemini/src/styles/index.css`、`web-gemini/src/styles/range.css`
- `web-gemini/src/components/ui/Overlay.tsx`（`PortalPopover`）、`web-gemini/src/components/ui/Collapsible.tsx`、`web-gemini/src/components/model/ModelPicker.tsx`
- `web-gemini/src/layout/Shell.tsx`、`web-gemini/src/pages/EditorDesk.tsx`、`web-gemini/src/pages/EvaluationPage.tsx`、`web-gemini/src/lib/download.ts`

---

## 面试高频追问快答提纲

1. **为什么长篇记忆选“事件溯源 + 单元/卷摘要 + 预算槽位”而不是 RAG / 向量库？**
   - 小说连贯性依赖的是**强时序状态（伤势是否愈合、账目余额、剧情时钟、承诺是否兑现、角色关系档位与已确认认知）**，向量相似度检索容易召回过时状态、漏掉隐式硬约束，且难以支持按章精准回滚。事件溯源重放可以确定性还原任意历史章状态（连带防止旧章重审和单元快检的“时点穿越”误判），配合单元/卷摘要与 Token 预算槽位，在纯本地 SQLite 下即可稳定覆盖 50～100 章，复杂度和故障面远低于向量库。
2. **Agent 输出格式不稳定、长程任务频繁中断怎么治理？**
   - 核心原则是**“分层容错 + 不造假通过”**：第 1 层对纯格式差异（空白/标点/别名/参照章）做确定性无损自愈；第 2 层在重试时把精确缺口（缺失 ID、0 命中近似原文片段）喂回模型，避免盲猜重试；第 3 层对极少数非核心局部失败做有界隔离降级并如实标 `needs_review`/`open` 继续推进，把人工决策集中到交付门禁前，而对财务账目、正文完整性、JSON 损坏死守严格拦截。
3. **长任务并发调用外部 LLM 时怎么避免雪崩和假死？**
   - 三道护栏协同：① **客户端超时 + 分级退避**（区分 429 限流、5xx 故障、不可重试 4xx）；② **按端点隔离的三态熔断器**（连续 3 次传输故障快败避免 25 分钟卡死，半开期异步挂起等待唯一探测，杜绝并发自杀和过期响应误关新熔断）；③ **输出预算截断护栏**（针对推理模型过滤思维链、记录拒限/截断档位防震荡，到顶即停不空转）。
4. **你是怎么系统性提升 AI 长篇初稿质量，而不是只靠写完再返工大修的？**
   - 先做**定量日志归因**（统计 300+ 章、700+ 条评估与修订审计的问题分布，再跑 20 章对照实验区分“已解决”与“真根因”），再把最有效的事后校验能力按四阶段前移：① 修盲与去噪（给写中审校补全上下文、消耗制热梗、保护终局字数、禁空壳章纲）；② 用真文校准本地风格门禁（零额外 token 成本、控误杀率）；③ 用弹性 3–6 章滚动单元 + 不可变核心口径锁 + `usedBeats` 清单消灭预写死百章导致的桥段自我撞车；④ 把台账（时钟/余额/人物关系与认知锁）和单元快检并入既有归档调用，全链路带独立开关与 195 项自动化单测兜底；实机同模型 A/B 对照中六维均分由 `3.33` 升至 `3.83`、桥段撞车清零、核心货币账本连续 14 章零误差。
