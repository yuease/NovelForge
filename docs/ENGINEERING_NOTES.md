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

## 面试高频追问快答提纲

1. **为什么长篇记忆选“事件溯源 + 卷摘要 + 预算槽位”而不是 RAG / 向量库？**
   - 小说连贯性依赖的是**强时序状态（伤势是否愈合、账目余额、承诺是否兑现、谁知道秘密）**，向量相似度检索容易召回过时状态、漏掉隐式硬约束，且难以支持按章精准回滚。事件溯源重放可以确定性还原任意历史章状态，配合 10 章卷摘要和 Token 预算槽位，在纯本地 SQLite 下即可稳定覆盖 50～100 章，复杂度和故障面远低于向量库。
2. **Agent 输出格式不稳定、长程任务频繁中断怎么治理？**
   - 核心原则是**“分层容错 + 不造假通过”**：第 1 层对纯格式差异（空白/标点/别名/参照章）做确定性无损自愈；第 2 层在重试时把精确缺口（缺失 ID、0 命中近似原文片段）喂回模型，避免盲猜重试；第 3 层对极少数非核心局部失败做有界隔离降级并如实标 `needs_review`/`open` 继续推进，把人工决策集中到交付门禁前，而对财务账目、正文完整性、JSON 损坏死守严格拦截。
3. **长任务并发调用外部 LLM 时怎么避免雪崩和假死？**
   - 三道护栏协同：① **客户端超时 + 分级退避**（区分 429 限流、5xx 故障、不可重试 4xx）；② **按端点隔离的三态熔断器**（连续 3 次传输故障快败避免 25 分钟卡死，半开期异步挂起等待唯一探测，杜绝并发自杀和过期响应误关新熔断）；③ **输出预算截断护栏**（针对推理模型过滤思维链、记录拒限/截断档位防震荡，到顶即停不空转）。
