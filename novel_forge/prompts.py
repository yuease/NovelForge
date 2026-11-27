"""Prompt builders for writing, reviewing and archiving chapters."""

import json
import math
import os
import re
from string import Template

from .arc_planning import (
    CANON_LOCKS_MARKER, SKELETON_CODES, arc_chapter_span, arc_rhythm, character_name_registry, compact_arc_summaries,
    compact_used_beats, horizon_memory, normalize_canon_locks, normalize_character_name, prior_arc_summaries, theme_hits,
)
from .book_plan import (
    PLAN_VERSION_V2, PLAN_V2_MARKER, canon_locks_text, is_v2_book_plan, volume_card_text,
    volume_ladder_text, volumes_for_range,
)
from .draft_quality import STYLE_BIBLE_MARKER, compact_style_bible
from .drift_repair import quote_span
from .entity_salience import _mentions, active_subplots_view, entity_index, ranked_entities, retired_character_status, retired_roster
from .ledger_checks import numeric_claims
from .llm import STORY_LEDGER_MARKER
from .long_memory import (
    STORY_CHARACTER_FIELDS, active_memory_items, protagonist_name, relevant_memory_characters,
    story_ledger_events, story_ledger_view, unresolved_drifts,
)
from .prompt_budget import ARC_PROMPT_BUDGET_EXCEEDED, fit_chapter_prompt, fit_prompt, read_chapter_input_budget
from .text_evidence import normalize_quote

POV_RULES = {
    "第一人称": "严格使用第一人称「我」进行叙事，所有心理活动、感官观察、信息获取必须严格受限于「我」的在场视野，禁止出现「我」不可能知道的他人内心独白或远方密谋。",
    "第三人称限知": "严格采用紧贴主角的第三人称限制视角，仅描写主角感官所及、所思所感，不得随意切换到反派或配角的独立内心戏，保持悬念与代入感。",
    "第三人称全知": "采用第三人称全知（上帝）视角，可根据剧情需要切换不同角色视角与多线叙事，但必须明确标注场景切换，保持人物动机与信息边界一致。"
}

STYLE_PRESETS = {
    "番茄爽文": "节奏明快，黄金三章直接切入冲突；每300字至少一次信息增量或小反转；心理描写不超过15%，多用动作、微表情与短句对话推进；章末必留强钩子。",
    "红果短剧感": "画面感极强，强冲突、强情绪、高密度打脸与反转；对话犀利简短，少大段景物抒情，突出节奏卡点与情绪爆点。",
    "传统悬疑厚重": "注重氛围铺陈与伏笔细节，人物对白克制有潜台词，环境描写服务于心境与线索暗示，逻辑严密。"
}

MEME_LEVEL_PRESETS = {
    "none": {
        "label": "写实正剧（零玩梗）",
        "instruction": "保持严肃写实与沉浸质感，不使用网络热梗、弹幕式调侃或刻意搞笑段子。"
    },
    "light": {
        "label": "微网感·适度玩梗",
        "instruction": "在保持主线逻辑与压迫感的同时，适当加入强网感表现力：如主角内心一针见血的“弹幕嘴替”吐槽、反派与主角之间的反差幽默、干净利落的打脸金句与短剧式节奏卡点（点到为止，避免硬塞生硬老梗词）。"
    },
    "rich": {
        "label": "高浓度玩梗·爆款网感",
        "instruction": "强化爽文网感与喜剧张力：高频制造“一本正经却极其荒诞”的认知反差、犀利毒舌的内心弹幕吐槽、自带传播度的反杀名场面金句、以及对当下热点/短剧反转套路的巧用二创，让读者每章都有会心一笑或拍案叫绝的记忆点。"
    }
}

_BOOK_PLAN_TOPIC_BLOCK = Template("""
【选题立项包 TopicPackage（必须严格落地）】
- 核心冲突：${conflict}
- 主角核心动机：${motivation}
- 主线走向：${main_plot}
- 黄金三章开篇设计：
  ${golden_chapters}
- 来源素材引用：${source_refs}""")

_BOOK_PLAN_HOTSPOT_BLOCK = Template("""
【热点爆款参考】
平台：${platform}
爆款套路：${title}
钩子公式：${hook_formula}""")

_BOOK_PLAN_STYLE_BIBLE_SPEC = """
  "styleBible": {
    "narrativeVoice": "一句话说明叙事语域与句式节奏",
    "dialogueRules": ["按角色区分立场和说话口吻，最多三条"],
    "domainTaboos": ["题材或时代的具体考据禁区，最多三条"],
    "forbiddenCliches": ["本书禁用的具体短套话，最多五条"]
  },"""

_BOOK_PLAN_STYLE_BIBLE_REQUEST = Template("""
风格指南每条写成短句或短语，总计不超过 300 字。${marker}""")

_BOOK_PLAN_CANON_SPEC = """
  "canonLocks": {
    "powerScale": "力量或等级天花板和主角起点",
    "currencyAndEra": "时代、货币或积分单位与合理量级",
    "worldTaboos": ["舞台与题材的绝对禁区"],
    "keyAssets": ["道具颜色、佩戴侧、编号等不会变化的属性"]
  },"""

_BOOK_PLAN_CANON_REQUEST = (
    "\n核心口径只写已有依据的固定规则。powerScale 与 currencyAndEra 各不超过100字，"
    "worldTaboos 最多3条且每条45字内，keyAssets 最多4条且每条50字内，合计不超过420字。"
    "没有等级或硬设定时省略对应字段，避免发明数值和世界机制。" + CANON_LOCKS_MARKER
)
_BOOK_PLAN_V2_CANON_SPEC = """
  "planVersion": 2,
  "canonLocks": {
    "powerScale": "力量天花板与主角起点，≤100字",
    "currencyAndEra": "时代、货币或积分口径，≤120字",
    "worldTaboos": ["绝对禁区，≤4条，每条≤65字"],
    "keyAssets": ["恒定物件属性，≤6条，每条≤65字"],
    "identityAliases": [{"character": "≤24字", "scope": "适用场域≤16字", "alias": "≤24字"}],
    "boundaries": [{"subject": "≤20字", "rule": "初始约束≤60字", "changeWhen": "可观察的变更条件≤48字，绝对禁制用 null"}],
    "assetSets": [{"name": "封闭集合名≤24字", "members": ["1–10个不同成员，各≤20字"]}]
  },"""
_BOOK_PLAN_V2_VOLUME_SPEC = """, "escalation": {
      "pressure": "本卷新增外部压迫或障碍，≤64字",
      "costAtRisk": "主角可能承担的具体代价，≤64字",
      "retiredTactics": [{"tactic": "退役手段≤32字", "whyFails": "失效原因≤40字"}],
      "stageTransition": "本卷舞台或能力跃迁的前置条件≤60字，无则用 null"
    }"""
_BOOK_PLAN_V2_REQUEST = (
    f"\n{PLAN_V2_MARKER}\n各锁仅记录创意有依据的口径，七类均可省略。"
    "别名最多6项，每个角色和场域组合只出现一次。边界最多4项，集合最多3项。"
    "锁的总渲染长度含标签和分隔须≤900字，优先压缩措辞并保留集合的完整名单。"
    "卷号从1连续排列，1–24卷连续覆盖1至总章数，区间无重叠或缺口。"
    "每卷递增外部压迫和真实代价，邻卷不能沿用同一对压迫与代价。"
    "退役手段每卷最多3条，首卷可空，后续列出仍适用的禁用手段，卷内去重。"
    "logline≤120字，setting≤220字，coreRules≤8条×120字，人物≤12，伏笔≤16。"
)
_BOOK_PLAN_SYSTEM = Template("""[TASK:BOOK_PLAN]
你是资深商业网文总编兼长篇架构师。请根据用户提供的核心创意与选题立项包，输出一部结构严谨、爽点密集、伏笔可回收的完整全书总纲（BookPlan）。
必须严格输出合法 JSON，不要包含 Markdown 代码块之外的多余寒暄。${style_request}""")

_BOOK_PLAN_PROMPT = Template("""请为以下小说项目制定全书总纲：
书名：${title}
题材：${genre}
视角：${pov}（${pov_rule}）
目标总字数：${target_words}
总章数：${total_chapters}
核心创意/梗概：${premise}${topic_block}${hotspot_block}${meme_block}

请严格按以下 JSON 结构输出：
{
  "title": "最终书名",
  "genre": "${genre}",
  "pov": "${pov}",
  "logline": "一句话核心卖点与主线冲突",
  "worldview": {
    "setting": "核心世界观与舞台设定",
    "coreRules": ["规则1", "规则2"]
  },${style_spec}
  "mainCharacters": [
    { "name": "角色名", "role": "主角/女主/反派/盟友", "traits": ["性格标签"], "goal": "核心诉求" }
  ],
  "volumes": [
    { "volume": 1, "title": "第一卷名", "chapters": [1, 5], "summary": "本卷核心推进"${volume_spec} }
  ],
  "foreshadowingPlan": [
    { "id": "fs_1", "clue": "伏笔物件或线索", "plantedChapter": 2, "reinforcedChapters": [6], "payoffChapter": ${total_chapters}, "description": "埋设与回收说明" }
  ]
}""")

_CHAPTER_OUTLINES_TOPIC_BLOCK = Template("""
【黄金三章与核心冲突约束】
- 核心冲突：${conflict}
- 主角动机：${motivation}
- 黄金三章要求：
  ${golden_chapters}""")

_CHAPTER_OUTLINES_PREV_CONTEXT = Template("""
【前文章纲摘要】
${outlines}""")

_CHAPTER_OUTLINES_FACTS_BLOCK = Template("""
【当前已提交规范事实与未回收伏笔（续写必须严格承接）】
${facts}""")

_CHAPTER_OUTLINES_DIRECTION_BLOCK = Template("""
【作者续写方向要求】
${direction}""")

_CHAPTER_OUTLINES_FINALE_PLAN_BLOCK = Template("""
【全书终局收束规划（总章数 ${total_chapters} 章）】
本轮目标为彻底完本！请按节奏统筹第 ${start_chapter}~${total_chapters} 章：前段推进未解支线与新冲突，后段收网高潮决战，并在第 ${total_chapters} 章彻底解决核心主线危机、回收关键伏笔、给出明确大结局（尾章不要再留开放性续作悬念）。""")

_CHAPTER_OUTLINES_SYSTEM = """[TASK:CHAPTER_OUTLINES]
你是长篇小说章纲拆解专家。你的任务是将全书总纲拆解为连续、可直接用于正文写作的精细章纲。
要求：
1. 每章必须包含明确目标、关键事件链、情绪/冲突转折、信息增量和章末钩子。
2. 严格承接前文章纲、选题立项包（核心冲突与黄金三章设计）、当前已发生规范事实与未回收伏笔。
3. 涉及疫病、医案、投毒、攻防或刑罚冲突时，仅采用常见风寒/细菌性热病、日常卫生防疫、普通化学杂质、机械装备或常规缉捕审讯等合规桥段，禁止设计烈性病原体播撒、芽孢/病菌提取、毒菌涂箭或药膏掺菌、水源投毒、气溶胶制毒、火药/炸药/起爆与延时自燃装置配比制法，或极端酷刑屠杀细节。
4. 严格输出 JSON 数组，不要输出多余解释。"""

_CHAPTER_OUTLINES_PROMPT = Template("""单章目标字数：${target_words}
【全书总纲】
${book_plan}${topic_block}
请生成连续章纲：
起始章节：${start_chapter}
结束章节：${end_chapter}${total_block}${prev_context}${facts_block}${direction_block}${finale_plan_block}

请输出 JSON 数组，每项格式如下：
[
  {
    "chapterNumber": ${start_chapter},
    "title": "第${start_chapter}章 章节标题",
    "goal": "本章核心推进目标",
    "summary": "200字左右具体剧情走向",
    "keyEvents": ["事件1", "事件2"],
    "hook": "章末悬念钩子"
  }
]""")

_CHAPTER_WRITE_BOOK_ANCHOR_BLOCK = Template("""
【全书总纲核心锚点（严禁偏离舞台与主角名）】
${anchors}""")

_CHAPTER_WRITE_FINALE_BLOCK = Template("""
【终章完结铁律（第${chapter_number}/${total_chapters}章）】
本章为全书大结局！必须把开篇核心危机（如时间倒计时/通关条件）、主角关键选择后果与核心伏笔全部兑现收束，给出完整明确的结局，严禁停留在半途断章！""")

_CHAPTER_WRITE_SYSTEM = Template("""[TASK:CHAPTER_WRITE]
你是金牌畅销网文作家。请严格按照全书总纲锚点、章纲、人设、叙事视角与相关规范事实（Relevant Facts）撰写小说正文。
创作铁律：
1. 叙事视角：${pov} — ${pov_rule}
2. 字数控制：本章目标字数 ${target_words} 字，正文必须控制在 ${min_words} ~ ${max_words} 字之间。
3. 严禁出现任何占位符（如【待补充】、TODO、TBD、省略号代替剧情等）。
4. 严禁违背规范事实中的角色伤势（未愈合的伤势不可突然痊愈）、物品归属/位置、时间线、工号/姓名编号与秘密知情边界！
5. 优先级铁律（重要）：**相关规范事实（Relevant Facts）与上一章末尾衔接 > 早先预生成的章纲**；同时必须始终保持全书总纲的舞台、主角姓名与核心设定不跑偏！
6. 文风要求：${style_guide}${meme_block}
7. 直接输出小说正文第一段，**不要在正文中输出章节标题（如“第X章 …”或“## 第X章 …”），也不要输出“好的”、“以下是正文”等元话语**。${draft_rule}""")

_CHAPTER_WRITE_PROMPT = Template("""书名：${book_title}
叙事视角：${pov}
本章目标字数：${target_words}${book_anchors}${style_block}
请撰写第${chapter_number}${total_suffix}章正文：${finale}${escalation_block}
本章标题：${chapter_title}
本章章纲：${outline}${summary_block}${ending_block}
【本章相关规范事实 Relevant Facts（必须严格遵守）】
${facts}""")

_WORD_COUNT_ADJUST_SYSTEM = Template("""[TASK:WORD_COUNT_ADJUST]
你是资深网文精修编辑。当前第${chapter_number}章正文字数（${current_words}字）偏离目标字数（${target_words}字），请进行【${mode}】调整：
1. 严格保留所有既有事实、物品归属、伤势状态、秘密边界与核心剧情节点。
2. 保持${pov}叙事视角与章纲方向。
3. 调整后字数需落在 ${min_words} ~ ${max_words} 字之间（目标字数：${target_words}）。
4. 严禁出现任何占位符，直接输出调整后的完整正文。""")

_WORD_COUNT_ADJUST_PROMPT = Template("""请对第${chapter_number}章进行${mode}：
目标字数：${target_words}
当前字数：${current_words}
章纲参考：${outline}

【待调整正文】
${chapter_text}""")

_LEGACY_FACT_RECORDING_RULES = (
    "伤势只记录会持续影响后续行动的实质伤残、中毒或重伤；情绪反应、心率变化和轻微擦伤不入库。"
    "已有伤势沿用 id、注明部位与左右侧；正文确认已愈或已过明确恢复期时提交 active=false。"
    "物品只记录跨章复用的核心道具、契约、载具或通讯工具，保留有依据的颜色、佩戴侧、状态等关键属性；一次性消耗品不入库。"
)
_FACT_RECORDING_RULES = (
    _LEGACY_FACT_RECORDING_RULES.replace("；一次性消耗品不入库。", "。")
    + '\nitems 只记录对后续章节有意义的物品；一次性消耗品可省略，已入库且用完的可标注 status: "已消耗"。'
)

_FACT_EXTRACTION_SYSTEM = """[TASK:FACT_EXTRACTION]
你是严谨的小说连续性档案员。请从刚生成的章节正文中提取所有关键事实增量（人物状态变化、伤势、时间线、物品位置/归属、秘密知情边界、伏笔埋设/强化/回收）。
必须严格输出合法 JSON。\n"""

_FACT_EXTRACTION_PROMPT = Template("""请提取第${chapter_number}章的规范事实增量：
【已有事实库摘要】
${facts}${memory_instructions}

【第${chapter_number}章正文】
${chapter_text}

请按以下 JSON 格式输出（无新增项可为空数组或空对象）：
{
  "characters": {
    "角色名": { "name": "角色名", "status": "active", "location": "当前地点", "traits": [] }
  },
  "injuries": [
    { "id": "更新时填写", "character": "角色名", "bodyPart": "含左右侧", "description": "伤势描述", "severity": "high|medium|low", "active": true }
  ],
  "timeline": [
    { "dayOrTime": "时间点", "event": "发生了什么", "characters": ["相关角色"] }
  ],
  "items": {
    "物品名": { "name": "物品名", "owner": "持有者", "location": "存放位置", "status": "intact", "attributes": ["已知关键物理属性"] }
  },
  "secrets": [
    { "secret": "秘密内容", "holders": ["知情人"], "excludedCharacters": ["明确不知情的人"] }
  ],
  "foreshadowing": [
    { "id": "fs_id", "clue": "伏笔线索", "status": "planted|reinforced|paid_off", "details": "本章进展" }
  ]${memory_spec}
}""")

_VERIFY_ENDING_BLOCK = Template("""
【上一章末尾衔接片段（核对时间线紧接点）】
${ending}""")

_VERIFY_AI_FLAVOR_INSTRUCTION = Template("""
7. AI 味病灶抽查（列出具体原句 quote 与位置，优先标出影响阅读的重度重复）：
   - over_explaining（写完动作/对白后旁白又解释一遍）
   - formulaic_reaction（高频重复“瞳孔一缩/指节发白/动作一顿/喉结滚动”等套话）
   - shared_voice（不同角色说话腔调与说教方式高度雷同）
   - static_situation（连环查线索换下条线索，处境与筹码毫无推进）
   - repetitive_hooks（章尾反复用对讲机响、系统弹窗、还有一件事硬断章）
   - core_scene_repetition（同一大招或信息揭示跨章无新后果地复演，属于硬冲突；有本章引文时必须阻塞）${style_focus}
   ${instruction}""")

_VERIFY_STYLE_FOCUS = """
   - over_explaining：战后再替读者定性、给已演清的动作翻译意图，或用大段括号复述已知账目。
   - shared_voice：临死、崩溃或反噬时条理清晰地背供词，危急中像说明书一样讲公式，或在生死与情感高潮用出戏的梗破坏人物口吻。
   - repetitive_hooks：连续用「然而就在……瞬间」转折或系统弹窗收尾；须有前文可比证据。
   以上病灶证据明确且破坏节奏、情绪或人物时标 P1，给出本章原句和具体改法；自然台词、必要数值和合理回扣照常保留。"""

_VERIFY_SYSTEM = Template("""[TASK:VERIFY]
你是严谨但懂网文叙事规律的小说连续性与质量审校官兼档案员。请在本次调用中**一次性**完成两件事：
A. 逐项核查本章正文是否存在**真正的硬性逻辑/设定冲突与重度 AI 味**：
1. 伤势一致性（前文未愈合的伤势不能无故正常使用受伤部位）。
2. 物品位置与归属一致性（注意：本章主角合理取用/转移物品导致的变化，将在本章通过后才入库，不得以“事实库尚未同步本章新动作”判 error）。
3. 秘密知情边界：只有当不知情角色（excludedCharacters）在**无任何告知/目击/推理来源**下凭空使用秘密，才判 secret_leak error；角色在本章**主动摊牌揭露**或**目击当前可疑行为（如在配电间徘徊）**属于合理剧情推进，不是 error。
4. 时间线、门牌/编号、等级/金额/身份、空间移动等跨章设定（必须顺承上章末尾；旧章纲与规范事实冲突时，以实际发生的正文和已核实事实为准）。
   三类必须阻塞的硬冲突：跨章等级、金额或身份无依据地互相矛盾 (hard_value_conflict)；直接违反总纲世界观硬规则 (world_rule_conflict)；核心大招或信息揭示无新后果地跨章复演 (core_scene_repetition)。填 issues、severity="error"、本章原文 quote 及已知来源章，令 passed=false；若放入 aiIssues，使用对应分类及有证据的 P0/P1。若沿用 inconsistent_details，另填 hardConflictType 指明上述具体类型。正文已经解释的合理变化、有新后果的回扣和轻微表述差异按一般问题处理。
5. 叙事视角（${pov}）是否严重越界（如直接透视他人的秘密内心；通过对话转述他人见闻仅作轻微 warning，不判 error）。
6. 是否包含未完成占位符。${ai_instruction}
注：精确字数由系统程序校验，请勿因“后续章节建议”将 word_count 标为 error。${severity_rule}${memory_rule}

B. 同步从本章正文中抽取**候选规范事实增量 (factsDelta)**（仅作候选提取；若未通过审校系统不会入库）：包含 characters、injuries、timeline、items、secrets、foreshadowing。
${fact_recording_rules}

必须严格输出合法 JSON。""")

_VERIFY_PROMPT = Template("""请一次性完成第${chapter_number}章的审校、AI味问题定位与事实增量抽取：
【章纲】${outline}${ending_block}${review_notes}
【相关规范事实库（含 sourceChapter 溯源）】
${facts}${memory_instructions}

【待审校正文】
${chapter_text}

输出 JSON（严格包含 issues、aiIssues、factsDelta）：
{
  "passed": true,
  "chapterNumber": ${chapter_number},
  "issues": [
    { "type": "injury_contradiction|item_mismatch|secret_leak|pov_violation|placeholder|timeline|inconsistent_details|hard_value_conflict|world_rule_conflict|core_scene_repetition|ai_flavor", "severity": "error|warning", "message": "具体问题说明", "quote": "原文原句证据", "sourceChapter": 1 }
  ],
  "aiIssues": [
    { "category": "over_explaining|formulaic_reaction|shared_voice|static_situation|repetitive_hooks|inconsistent_details|hard_value_conflict|world_rule_conflict|core_scene_repetition", "quote": "原文原句", "impact": "为什么影响阅读", "priority": "P0|P1|P2", "suggestion": "删改建议" }
  ],
  "factsDelta": {
    "characters": { "角色名": { "name": "角色名", "status": "active", "location": "当前地点", "traits": [] } },
    "injuries": [{ "id": "更新时填写", "character": "角色名", "bodyPart": "含左右侧", "description": "伤势描述", "severity": "high|medium|low", "active": true }],
    "timeline": [{ "dayOrTime": "时间点", "event": "关键事件", "characters": ["角色"] }],
    "items": { "物品名": { "name": "物品名", "owner": "持有者", "location": "位置", "status": "intact", "attributes": ["已知关键物理属性"] } },
    "secrets": [{ "secret": "秘密内容", "holders": ["知情人"], "excludedCharacters": ["不知情人"] }],
    "foreshadowing": [{ "id": "fs_id", "clue": "伏笔线索", "status": "planted|reinforced|paid_off", "details": "进展" }]${memory_spec}
  },
  "summary": "审校总结"${identity_spec}
}""")

_REPAIR_ENDING_BLOCK = Template("""
【上一章末尾衔接片段（必须无缝紧接此场景与时间点）】
${ending}""")

_REPAIR_AI_FIX_BLOCK = Template("""
【一并清理的 AI 味/重复问题】
${ai_issues}""")

_REPAIR_SYSTEM = Template("""[TASK:REPAIR]
你是专业小说精修编辑。请根据审校指出的问题列表，对当前章节正文进行一次性定向修复（同时修正设定/因果冲突与指定 AI 味问题）。
要求：
1. 彻底解决所有 severity="error" 的一致性、门牌/金额/时间线、占位符或视角问题。${hard_style_rule}
2. ${style_instruction}
3. 遵守总纲世界观硬规则与活跃期限。规范事实与上一章实际发生的情节优先于旧章纲，按已核实数值、身份及状态修复。
4. 保留原有的核心剧情推进与爽点节奏，严格遵守${pov}视角，保持目标字数在 ${target_words} 字左右。
5. 直接输出修复后的完整正文，不要输出解释。""")

_REPAIR_PROMPT = Template("""请修复第${chapter_number}章正文中的以下问题：
目标字数：${target_words}
【本章章纲】
${outline}${ending_block}${review_notes}

【审校问题列表】
${issues}${ai_fix_block}

【相关规范事实库】
${facts}

【原正文】
${chapter_text}""")

_DE_AI_SYSTEM = Template("""[TASK:DE_AI]
你是资深网文润色主编，专门负责“去AI味”精修。
请在不改变任何事实、物品、伤势、时间线与剧情事件的前提下，优化以下正文的语言质感：
1. 删除或改写高频AI套话（如“不仅如此”、“总而言之”、“这一刻，空气仿佛凝固了”、“嘴角勾起一抹不易察觉的弧度”的机械重复）。
2. 增强动作动词与感官细节，让对话更符合人物性格与口语习惯。
3. 保持${pov}视角与原有字数规模，风格遵循：${style_guide}${preserve_meme_note}
5. 直接输出润色后的完整正文。""")

_DE_AI_PROMPT = Template("""【待润色正文】
${chapter_text}""")

_FULL_BOOK_AUDIT_SYSTEM = """[TASK:FULL_AUDIT]
你是全书终审主编。请结合全书总纲、定稿正文提取的各章摘要与关键情节证据、篇幅统计与最终规范事实库，核实主线收束、主角选择后果、人物最终状态与核心伏笔回收。
必须严格输出合法 JSON，并附带具体章节证据引用。"""

_FULL_BOOK_AUDIT_PROMPT = Template("""请对全书进行定稿终审：
【全书总纲】${book_plan}
【篇幅统计】${word_stats}
【定稿各章摘要】${chapters_summary}
【关键情节证据】${chapter_evidence}
【最终规范事实库摘要】${facts}

请严格输出以下 JSON：
{
  "passed": true,
  "score": 95,
  "summary": "全书终审结论",
  "blockingIssues": [
    { "type": "causality|setting_conflict|unresolved_mainline|ending", "chapterNumber": 15, "message": "阻塞完本的硬冲突说明", "quote": "证据原句" }
  ],
  "issueChapters": [15],
  "mainConflictChapter": 1,
  "protagonistChoiceChapter": 2,
  "foreshadowingPayoffs": [
    { "clue": "伏笔线索", "plantedChapter": 2, "payoffChapter": 18, "status": "paid_off|open" }
  ],
  "evidence": [
    { "claim": "审核结论依据", "chapter": 18, "quote": "对应章节情节或引文" }
  ],
  "consistencySummary": "人物/时间线/物品/秘密一致性评估",
  "recommendations": ["优化建议（非阻塞）"]
}
注：只有当 blockingIssues 为空时，passed 才可为 true；若存在需修改章节，请将 passed 设为 false 并同时在 blockingIssues 和 issueChapters 中指明章号。""")

_MEMORY_DELTA_JSON = Template(""",
    "memory": {
      "characterStates": { "角色名": { "seen": true${general_character}${locked_character}, "quote": "原文" } }${general_memory}${story_memory}
    }""")

_GENERAL_MEMORY_FIELDS = """,
      "constraints": [{ "id": "已有条目更新时填写", "text": "硬规则", "characters": ["角色"], "status": "active|fulfilled|expired", "quote": "原文" }],
      "commitments": [{ "id": "已有条目更新时填写", "text": "承诺或倒计时的稳定名称", "deadline": "剧情时间", "status": "active|fulfilled|expired", "quote": "原文" }],
      "subplots": [{ "text": "支线进展", "status": "active|resolved", "quote": "原文" }]"""

_STORY_MEMORY_FIELDS = """,
      "storyClock": { "day": "剧情日或日期", "season": "季节与天气基调", "timeOfDay": "当前时段", "countdown": "主线倒计时", "quote": "本章原文" },
      "ledgerBalances": [{ "label": "沿用已有账户名", "value": "明确的当前值和单位", "change": "+明确同单位净变化（可省略）", "quote": "本章原文" }]"""

_STORY_LEDGER_EXTRACTION = (
    "\n随章台账可选。只记本章明确发生的变化，每条 quote 必须逐字来自正文（4–120 字）。"
    "storyClock 每字段最多40字，只提交现在时间线中变化的字段；提及未来、回忆、预报和假设不改变现在。"
    "ledgerBalances 最多6个核心账户，沿用 label、量级和单位，区分可用余额、冻结与累计。"
    "仅在原文明示同一账户同一单位的净变化及结果值时填 change（有正负号），不要从部分交易推算。"
    "核心角色最多12人。stanceTier 是对主角的关系档位，跨两档及以上必须有重大触发。"
    "knownFacts 只记录角色亲眼确认的新增认知，最多3条、每条60字内；传闻、梦境与猜测不入锁。"
    "普通角色只可用 seen=true 配原文确认出场，不为一次性龙套设锁。无可靠增量可省略。"
)
_STORY_LEDGER_WRITE = (
    "\n随章台账优先于旧章纲。时间与核心余额只能随真实剧情变化。"
    "关系跨两档及以上必须有重大触发，已亲证的关键认知必须延续。"
)
_STORY_LEDGER_VERIFY = (
    "\n核对随章台账与人物锁。同一剧情时点、同一口径的时间或精确余额硬冲突写 issues，"
    "type=hard_value_conflict、severity=error，ledgerRef={\"kind\":\"clock|balance\",\"key\":\"字段名或账户名\"}。"
    "正常跨日、倒叙、换地点、冬日有阳光、口径改变或明确交易不是冲突。"
    "对主角的关系按 hostile → wary → transactional → ally → intimate 排列。"
    "跨两档及以上但没有明确重大触发，或无故遗忘、否认已确认的 knownFacts，写 type=character_ooc、severity=error，"
    "ledgerRef={\"kind\":\"character\",\"key\":\"全名\"}。剧情明确的假装、失忆或证据被推翻应保留。"
    "这些 issues 必须引用本章原句 quote，并指出对应前文来源。"
    "重名仅提交 newCharacterCandidates 候选：全名与已有人物完全相同，且原文明确证明是不同的人，"
    "quote 要包含完整姓名与同名、另一个、并非同一人等区分语句。"
    "同姓、职务变化、同一人的化名及作者明示的既有同名设定均不提报。候选无证据时可省略。"
)
_IDENTITY_SPEC = """,
  "newCharacterCandidates": [{ "name": "全名", "identity": "明确的新人物身份", "quote": "能证明是不同人物的本章原文" }]"""

_MEMORY_EXTRACTION_INSTRUCTIONS = Template("""
可选 memory 只记录本章明确确立或改变的角色目标、立场关系、硬规则、期限和支线；仅出现可写 seen。每条必须带本章逐字 quote（4–100 字），只填有变化的字段，无变化可省略 memory。更新、兑现或完结沿用已有 id，新项省略 id。同一承诺或倒计时沿用稳定的 text，剩余时长与到期时间变化只写入 deadline。已有活跃条目：${active}
""")

_VOLUME_SUMMARY_PROMPT = Template("""给第${start_chapter}–${end_chapter}章归档。mainline 不超过 360 字。turningPoints 最多 4 条，每条包含 chapter（整数）和 text（不超过 90 字）。
${evidence}""")


_DEFAULT_POV = "第三人称限知"
_DEFAULT_STYLE = STYLE_PRESETS["番茄爽文"]
_SPACE = r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_TERMS = re.compile(rf"[{_SPACE},，、：:；;()（）]+")
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+\Z")
_DRAFT_RULE = "\n8. 动作已表情绪就收笔。少用套话、心理引导词和破折号，变换句首、按节奏分段，让角色的用词贴合各自立场。"
_WRITE_STYLE_RED_LINES = (
    "\n【写中风格红线】临死、崩溃、反噬或危急汇报时只说贴合人物的口语短句；"
    "公式、叠乘算法与完整案情用系统提示、证据或主角观察交代，禁止说明书式对白和背供词。"
    "冲突、反转或笑点演完就推进，禁止旁白抽象定性或替读者翻译意图。"
)
_VOLUME_SUMMARY_SYSTEM = (
    "[TASK:VOLUME_SUMMARY]\n"
    "你是小说档案员。概括已保存的这十章，只记录有依据的主线进展与关键转折，节选正文优先于摘要，保持因果与章号。"
    "needs_review 章节的内容为待复核叙事，不作为已核实的硬事实。严格输出 JSON。"
)


def read_writing_options(job=None):
    """Resolve the independent writing switches at the pipeline boundary."""
    degrade_setting = os.environ.get("NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED")
    job_degrade = (job or {}).get("degradeOnVerifyExhausted")
    return {
        "input_budget": read_chapter_input_budget(),
        "draft_quality": os.environ.get("NOVEL_FORGE_DRAFT_QUALITY") != "0",
        "long_memory": os.environ.get("NOVEL_FORGE_LONG_MEMORY") != "0",
        "chapter_ai_gate": os.environ.get("NOVEL_FORGE_WRITE_AI_GATE") != "0",
        "rolling_arc_outlines": os.environ.get("NOVEL_FORGE_ROLLING_ARC_OUTLINES") != "0",
        "story_ledger": os.environ.get("NOVEL_FORGE_STORY_LEDGER") != "0",
        "degrade_on_verify_exhausted": degrade_setting != "0" and (
            degrade_setting == "1" if job_degrade is None else job_degrade is True
        ),
    }


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _records(value):
    return [item for item in _list(value) if isinstance(item, dict)]


def _json(value, pretty=False):
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":"))


def _number(value):
    try:
        number = float(value or 0)
        return number if math.isfinite(number) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _round(value):
    return math.floor(value + 0.5)


def _text_excerpt(text, units, *, from_end=False):
    # UTF-16 budgets match the original templates without splitting a code point.
    raw = str(text).encode("utf-16-le", "surrogatepass")
    part = raw[-units * 2:] if from_end else raw[:units * 2]
    return part.decode("utf-16-le", errors="ignore")


def _matches_term(term, context):
    return isinstance(term, str) and len(term.encode("utf-16-le", "surrogatepass")) >= 4 and term in context


def _pov_rule(pov):
    return POV_RULES.get(pov, POV_RULES[_DEFAULT_POV])


def _meme_guidance(level, samples, *, chapter_ai_gate=False):
    preset = MEME_LEVEL_PRESETS.get(level)
    if not preset or level == "none":
        return ""
    samples = _records(samples)
    examples = ""
    if samples:
        lines = "\n".join(f"- 【{sample.get('title', 'undefined')}】{sample.get('summary', 'undefined')}" for sample in samples)
        examples = (
            "\n可自然化用/二创以下幽默结构或近期热点元素（切忌生硬直接照抄老词条名词，重点学习其中的“反差逻辑、沟通错位或反转笑点”）："
            f"\n{lines}"
        )
    recurrence = "同一热梗或比喻全书最多点题使用一至两次，严禁跨章反复复述同一类比。"
    scene_rule = "\n生死关头与核心情感高潮禁止出戏玩梗、破第四面墙或自夸。" if chapter_ai_gate else ""
    return f"\n【网感与玩梗风格要求（{preset['label']}）】\n{preset['instruction']}\n{recurrence}{scene_rule}{examples}"


def _pick_relevant(items, context, maximum, search_text):
    if len(items) <= maximum:
        return items
    if maximum <= 0:
        return []
    matched, others = [], []
    for item in items:
        found = context and any(_matches_term(term, context) for term in _TERMS.split(search_text(item)))
        (matched if found else others).append(item)
    count = max(0, maximum - len(matched))
    return [*matched[-maximum:], *(others[-count:] if count else [])]


def _visible_injuries(entries, context, current_chapter=None, *, story_ledger=False):
    terms = ("bodyPart", "description") if story_ledger else ("character", "bodyPart", "description")

    def visible(entry):
        if entry.get("active") is False:
            return False
        updated = _number(entry.get("updatedAtChapter")) or _number(entry.get("sourceChapter"))
        if current_chapter is None or not updated or _number(current_chapter) - updated <= 10:
            return True
        if entry.get("severity") not in (None, "", "low", "medium"):
            return True
        return any(_matches_term(entry.get(key), context) for key in terms)

    return [entry for entry in _records(entries) if visible(entry)]


def _salient_facts(facts, kind, index, plan, arcs, current, context):
    themes = horizon_memory(facts, arcs, current)["themes"] if current is not None and kind == "items" else ()
    for name, tier, score, hit in ranked_entities(
        kind, index, book_plan=plan, arc_plans=arcs, current_chapter=current,
        context_text=context, resolved_theme_ranges=themes,
    ):
        row = index[kind][name]
        entry = row["entry"]
        if kind == "characters":
            entry = {**row["state"], **entry}
            if tier == "retired":
                entry["status"] = retired_character_status(entry.get("status"))
        yield name, entry, tier, score, hit


def extract_relevant_facts(canonical_facts=None, *, context_text="", max_items=28, max_secrets=22, max_foreshadowing=22,
                           current_chapter=None, story_ledger=False, book_plan=None, arc_plans=(), salience_index=None,
                           max_characters=None):
    facts = canonical_facts or {}
    injuries = _visible_injuries(facts.get("injuries", facts.get("activeInjuries")), context_text, current_chapter,
                                  story_ledger=story_ledger)
    open_clues = [entry for entry in _records(facts.get("foreshadowing", facts.get("openForeshadowing")))
                  if entry.get("status") != "paid_off"]
    foreshadowing = _pick_relevant(
        open_clues, context_text, max_foreshadowing,
        lambda entry: " ".join(str(entry.get(key) or "") for key in ("id", "clue", "details")),
    )
    use_salience = book_plan is not None or salience_index is not None or max_characters is not None
    items = _dict(facts.get("items"))
    character_rows = _dict(facts.get("characters")).items()
    if use_salience:
        index = entity_index(facts, before_chapter=current_chapter) if salience_index is None else salience_index
        current_chapter = current_chapter if current_chapter is not None else index.get("beforeChapter")
        ranked = _salient_facts(facts, "characters", index, book_plan, arc_plans, current_chapter, context_text)
        character_rows = [(name, entry) for name, entry, *_ in ranked][:max(0, 32 if max_characters is None else max_characters)]
        ranked = _salient_facts(facts, "items", index, book_plan, arc_plans, current_chapter, context_text)
        items = dict([(name, entry) for name, entry, *_ in ranked][:max(0, max_items)])

    characters = {}
    for name, entry in character_rows:
        entry = _dict(entry)
        characters[name] = {
            "name": entry.get("name") or name, "status": entry.get("status") or "active",
            "location": entry.get("location") or "", "role": entry.get("role") or "",
            **({"sourceChapter": entry["sourceChapter"]} if "sourceChapter" in entry else {}),
        }

    if not use_salience and len(items) > max_items:
        def item_terms(pair):
            name, entry = pair
            return " ".join(str(value or "") for value in
                            (name, entry.get("name"), entry.get("owner") or entry.get("holder"), entry.get("location")))

        records = [(key, value if isinstance(value, dict) else {"value": value}) for key, value in items.items()]
        items = dict(_pick_relevant(records, context_text, max_items, item_terms))

    secrets = _pick_relevant(
        _records(facts.get("secrets")), context_text, max_secrets,
        lambda entry: f"{entry.get('secret') or ''} " + " ".join(_list(entry.get("holders"))),
    )
    return {
        "characters": characters, "activeInjuries": injuries, "items": items, "secrets": secrets,
        "openForeshadowing": foreshadowing, "recentTimeline": _list(facts.get("timeline", facts.get("recentTimeline")))[-5:],
    }


def _plan_for_outlines_and_audit(plan, *, rolling_arc_outlines=False):
    if not isinstance(plan, dict):
        return plan
    visible = {key: value for key, value in plan.items() if key not in ("styleBible", "canonLocks")}
    if rolling_arc_outlines and not is_v2_book_plan(plan) and (locks := normalize_canon_locks(plan.get("canonLocks"))):
        return {"canonLocks": locks, **visible}
    return visible


def _topic_anchors(topic):
    return {"conflict": topic.get("coreConflict") or "", "motivation": topic.get("protagonistMotivation") or "",
            "golden_chapters": "\n  ".join(_list(topic.get("goldenThreeChapters")))}


def build_book_plan_prompt(*, title="", premise="", genre="都市重生爽文", pov=_DEFAULT_POV, target_words=30000,
                           total_chapters=15, hotspot_context=None, topic_package=None, meme_level="none",
                           meme_samples=None, draft_quality=False, rolling_arc_outlines=False, plan_version=1):
    topic = topic_package or {}
    hotspot = hotspot_context or {}
    topic_block = _BOOK_PLAN_TOPIC_BLOCK.substitute(
        **_topic_anchors(topic), main_plot=topic.get("mainPlot") or "", source_refs=_json(topic.get("sourceRefs") or []),
    ) if topic_package is not None else ""
    hotspot_block = _BOOK_PLAN_HOTSPOT_BLOCK.substitute(
        platform=hotspot.get("platform") or "番茄小说", title=hotspot.get("title") or "",
        hook_formula=hotspot.get("hookFormula") or "",
    ) if hotspot_context is not None else ""
    request = _BOOK_PLAN_STYLE_BIBLE_REQUEST.substitute(marker=STYLE_BIBLE_MARKER) if draft_quality else ""
    v2 = plan_version == PLAN_VERSION_V2
    if v2:
        request = _BOOK_PLAN_V2_REQUEST + request
    elif rolling_arc_outlines:
        request = _BOOK_PLAN_CANON_REQUEST + request
    system = _BOOK_PLAN_SYSTEM.substitute(style_request=request)
    prompt = _BOOK_PLAN_PROMPT.substitute(
        title=title or topic.get("title") or "请根据核心梗拟定一个不超过14字的商业爆款书名",
        premise=premise or topic.get("premise") or "主角重生归来，凭借前世记忆破局逆袭，揭开惊天阴谋。",
        genre=genre, pov=pov, pov_rule=_pov_rule(pov), target_words=target_words, total_chapters=total_chapters,
        topic_block=topic_block, hotspot_block=hotspot_block, meme_block=_meme_guidance(meme_level, meme_samples),
        style_spec=(_BOOK_PLAN_V2_CANON_SPEC if v2 else _BOOK_PLAN_CANON_SPEC if rolling_arc_outlines else "")
        + (_BOOK_PLAN_STYLE_BIBLE_SPEC if draft_quality else ""),
        volume_spec=_BOOK_PLAN_V2_VOLUME_SPEC if v2 else "",
    )
    return {"system": system, "prompt": prompt}


def build_chapter_outlines_prompt(*, book_plan=None, start_chapter=1, end_chapter=10, total_chapters=None,
                                  existing_outlines=None, target_words_per_chapter=2000, topic_package=None,
                                  relevant_facts=None, continuation_direction="", finalize_book=False, boundary_state=()):
    topic_block = _CHAPTER_OUTLINES_TOPIC_BLOCK.substitute(
        **_topic_anchors(topic_package)
    ) if topic_package is not None and start_chapter <= 3 else ""
    previous = "\n".join(f"第{entry.get('chapterNumber', 'undefined')}章 {entry.get('title', 'undefined')}: "
                         f"{entry.get('summary', 'undefined')}" for entry in _records(existing_outlines)[-5:])
    prev_context = _CHAPTER_OUTLINES_PREV_CONTEXT.substitute(outlines=previous) if previous else ""
    facts = _CHAPTER_OUTLINES_FACTS_BLOCK.substitute(
        facts=_json(relevant_facts, pretty=True)
    ) if relevant_facts is not None and start_chapter > 1 else ""
    direction = _CHAPTER_OUTLINES_DIRECTION_BLOCK.substitute(direction=continuation_direction) if continuation_direction else ""
    finale = _CHAPTER_OUTLINES_FINALE_PLAN_BLOCK.substitute(
        total_chapters=total_chapters, start_chapter=start_chapter,
    ) if finalize_book and total_chapters else ""
    plan = _plan_for_outlines_and_audit(book_plan)
    plan_notes = ""
    if is_v2_book_plan(book_plan):
        plan["volumes"] = volumes_for_range(book_plan, start_chapter, end_chapter)
        plan_notes = "\n".join(filter(None, (
            canon_locks_text(book_plan.get("canonLocks"), boundary_state=boundary_state),
            "【全书分卷阶梯】\n" + volume_ladder_text(book_plan),
            "按各章所属卷采用对应的压迫、代价和退役手段，跨卷时满足跃迁条件。",
        )))
        plan_notes = "\n" + plan_notes
    prompt = _CHAPTER_OUTLINES_PROMPT.substitute(
        start_chapter=start_chapter, end_chapter=end_chapter,
        total_block=f"\n全书总章数：{total_chapters}" if total_chapters else "",
        target_words=target_words_per_chapter, book_plan=_json(plan, pretty=True) + plan_notes,
        topic_block=topic_block, prev_context=prev_context, facts_block=facts, direction_block=direction,
        finale_plan_block=finale,
    )
    return {"system": _CHAPTER_OUTLINES_SYSTEM, "prompt": prompt}


_SKELETON_VOCABULARY = "、".join(f"{code}（{label}）" for code, label in SKELETON_CODES.items())
_ARC_OUTLINES_SYSTEM = """[TASK:ARC_OUTLINES]
你是剧情单元编剧。根据已经发生的正文事实与总纲路标，只规划当前单元。核心口径锁为不可变硬设定，动态数值与人物去向以有证据的前文为准。
recentBeats 是最近单元的已演机制、大招和揭示，只供去重。themeLedger 是全书已解决的主题标签，再次触及时应推进后果或改变议题。patternStats 给出近期结构统计，遵守 allowedLengths，并主动更换高频或重复的骨架。
可选 skeleton，按本单元剧情顺序列出 2–5 个功能代码。强化旧伏笔必须带来新的信息或后果。勿提前消耗终局破局。
同一反派连续两个单元没有新增实质筹码，必须退场或转幕后。章名和断章方式要与近期章节有清晰差异。
涉及疫病、医案、投毒、攻防或刑罚冲突时，仅采用常见风寒/细菌性热病、日常卫生防疫、普通化学杂质、机械装备或常规缉捕审讯等合规桥段，禁止设计烈性病原体播撒、芽孢/病菌提取、毒菌涂箭或药膏掺菌、水源投毒、气溶胶制毒、火药/炸药/起爆与延时自燃装置配比制法，或极端酷刑屠杀细节。
只输出完整 JSON 对象。""" + "\n功能代码：" + _SKELETON_VOCABULARY


def _facts_before(facts, chapter):
    def earlier(entry):
        record = _dict(entry)
        return max(_number(record.get(key)) for key in
                   ("sourceChapter", "updatedAtChapter", "lastSeenChapter", "chapterNumber", "plantedChapter")) < chapter

    selected = {key: ({name: row for name, row in value.items() if earlier(row)} if isinstance(value, dict)
                      else [row for row in _list(value) if earlier(row)])
                for key, value in facts.items() if key in (
                    "characters", "characterStates", "injuries", "items", "secrets", "timeline",
                    "constraints", "commitments", "subplots",
                )}
    selected["foreshadowing"] = []
    for clue in _records(facts.get("foreshadowing")):
        if not earlier(clue):
            continue
        chapters = _list(clue.get("relatedChapters"))
        if any(_number(number) >= chapter for number in chapters) or (
            clue.get("status") == "paid_off" and _number(clue.get("payoffChapter")) >= chapter
        ):
            clue = {**clue, "status": "planted", "payoffChapter": None, "details": "",
                    "relatedChapters": [number for number in chapters if _number(number) < chapter]}
        selected["foreshadowing"].append(clue)
    return selected


def _arc_fact_entries(facts, path=()):
    for key, value in facts.items():
        if isinstance(value, dict):
            if key == "storyLedger":
                yield from _arc_fact_entries(value, (*path, key))
            else:
                for name, record in value.items():
                    yield (*path, key), name, record
        elif isinstance(value, list):
            for record in value:
                yield (*path, key), None, record


def _allocated_arc_facts(entries):
    facts = {}
    for path, name, value in entries:
        section = facts
        for key in path[:-1]:
            section = section.setdefault(key, {})
        if name is None:
            section.setdefault(path[-1], []).append(value)
        else:
            section.setdefault(path[-1], {})[name] = value
    return facts


def _subplot_hints(subplots, maximum):
    return [{"id": row["id"], "text": _text_excerpt(row.get("text") or "", 120)} for row in subplots[:maximum]]


def build_arc_outlines_prompt(*, book_plan, canonical_facts, start_chapter, total_chapters, arc_index,
                              preceding_chapters=(), recent_titles=(), continuation_direction="", finalize_book=False,
                              story_ledger=False, boundary_state=(), arc_plans=(), input_budget=None, rhythm=None,
                              retry_note=""):
    minimum, maximum = arc_chapter_span(start_chapter, total_chapters)
    terminal = total_chapters - start_chapter + 1 <= 6
    plan = _dict(book_plan)
    facts = _dict(canonical_facts)
    horizon = horizon_memory(facts, arc_plans, start_chapter)
    near = horizon["near"]
    if rhythm is None:
        rhythm = arc_rhythm(arc_plans, prior_arc_summaries(facts, start_chapter),
                            start_chapter=start_chapter, total_chapters=total_chapters)
    past = _facts_before(facts, start_chapter)
    context = " ".join([str(plan.get("logline") or ""), continuation_direction,
                        *(str(row.get("summary") or "") for row in preceding_chapters[-5:])])
    selected = extract_relevant_facts(past, context_text=context, current_chapter=start_chapter, story_ledger=story_ledger,
                                      max_foreshadowing=len(_list(past.get("foreshadowing"))) if terminal else 22)
    for kind in ("constraints", "commitments"):
        if active := active_memory_items(past, kind, context):
            selected[kind] = active
    visible_subplots, stale_subplots = active_subplots_view(facts, current_chapter=start_chapter, context_text=context)
    if visible_subplots:
        selected["subplots"] = visible_subplots[:8]
    if past.get("characterStates"):
        lead = protagonist_name(plan.get("mainCharacters"))
        selected["characterStates"] = [
            {"name": entry["key"], **_legacy_character_state(entry["value"]["state"])}
            for entry in relevant_memory_characters(past, _dict(past.get("characters")), lead, context)
            if isinstance(entry["value"].get("state"), dict)
        ]
    system = _ARC_OUTLINES_SYSTEM
    v2 = is_v2_book_plan(plan)
    if story_ledger:
        ledger = story_ledger_view(facts, chapter_number=start_chapter, book_plan=plan)
        if ledger:
            selected["storyLedger"] = ledger
        core_names = _core_character_names(plan, past, ledger)
        if core_names:
            selected["coreCharacterNames"] = core_names
        system += (_STORY_LEDGER_WRITE +
                   "新配角须避开 coreCharacterNames 中的已有全名，也不要与主角、核心反派高度撞名。"
                   "previousArcDriftChecks 是尚待处理的纠偏建议，以当前台账和正文为准，不改写已提交章节。")
    corrections = [row for row in unresolved_drifts(facts, before_chapter=start_chapter)
                   if facts.get("driftLedger") or story_ledger or v2 and row.get("kind") == "canon"]
    recent_arcs = {row.get("arc") for row in near}
    context_terms = _review_terms(context)
    corrections.sort(key=lambda row: (
        not context_terms.intersection(_review_terms(f"{row['issue']} {row.get('fixHint', '')}")),
        row.get("arc") not in recent_arcs,
    ))
    corrections = [{key: row[key] for key in ("chapter", "kind", "issue", "fixHint") if key in row}
                   for row in corrections[:6]]
    if v2:
        system += "\n按各章所属卷执行对应阶梯，允许跨卷的单元须保留跃迁条件；已核实的边界变更自标明章号生效。"
        if not story_ledger and corrections:
            system += "previousArcDriftChecks 用于按锁和正文纠偏。"
    clues = selected.pop("openForeshadowing")
    priority = ("storyLedger", "coreCharacterNames", "constraints", "commitments")
    fact_entries = list(_arc_fact_entries({**{key: selected[key] for key in priority if key in selected}, **selected}))
    material = {
        "bookPlan": _plan_for_outlines_and_audit(plan, rolling_arc_outlines=True),
        "patternStats": {key: value for key, value in rhythm.items() if key != "directive"},
        "previousArcDriftChecks": corrections,
        "lastEnding": _text_excerpt(preceding_chapters[-1].get("content") or "", 350, from_end=True)
        if preceding_chapters else "",
        "recentTitles": list(recent_titles),
        "continuationDirection": continuation_direction,
    }
    locks = ""
    if v2:
        material["bookPlan"]["volumes"] = volumes_for_range(plan, start_chapter, start_chapter + maximum - 1)
        if text := canon_locks_text(plan.get("canonLocks"), boundary_state=boundary_state):
            locks = text + "\n"
    finale = ""
    if terminal:
        material["uncollectedForeshadowing"] = [
            {key: _text_excerpt(clue[key], limit) for key, limit in (("id", 48), ("clue", 100), ("details", 72))
             if clue.get(key)}
            for clue in clues
        ]
        finale = "\n本单元为终局收网单元，必须一次规划至终章，收束本轮核心危机并兑现所有未回收伏笔。"
        if finalize_book:
            finale += "作者要求彻底完结，终章给出明确结局，禁止开放性续作悬念。"
    example = {
        "arcTitle": "单元标题", "arcGoal": "本单元的新推进目标与结局", "skeleton": ["investigation", "confrontation"],
        "villainChips": [{"villain": "反派名", "newChips": ["确有新增的筹码"], "exitPlan": "retain|exit|background"}],
        "chapters": [{"chapterNumber": start_chapter, "title": "具体章名", "goal": "本章目标",
                      "summary": "具体因果推进", "keyEvents": ["实际事件"], "hook": "章末独有结果或终章收束"}],
    }
    header = (f"当前单元：{arc_index}\n起始章节：{start_chapter}\n全书总章数：{total_chapters}\n"
              f"最少章数：{minimum}\n最多章数：{maximum}\n剩余章节：{total_chapters - start_chapter + 1}{finale}\n"
              f"allowedLengths：{_json(rhythm['allowedLengths'])}\n{rhythm['directive']}\n"
              "从起始章起严格连续，按 allowedLengths 选择单元长度。无反派时 villainChips 返回空数组。\n")
    retry = f"\n上次校验未通过：{retry_note} 请纠正并完整重发本单元 JSON。" if retry_note else ""
    slots = [
        {"key": "nearArcs", "entries": compact_arc_summaries(near, include_beats=False), "share": 18,
         "allocate_first": True},
        {"key": "facts", "entries": fact_entries, "share": 22},
        {"key": "themeLedger", "entries": [row["range"] + ":" + "｜".join(row["tags"]) for row in horizon["themes"]],
         "share": 14},
        {"key": "capsules", "entries": horizon["capsules"], "share": 12},
        {"key": "volumes", "entries": horizon["volumes"], "share": 10},
        {"key": "recentChapters", "entries": [
            {"chapter": row["chapterNumber"], "summary": _text_excerpt(row.get("summary") or "", 240)}
            for row in preceding_chapters[-5:]
        ], "share": 10},
        {"key": "recentBeats", "entries": horizon["recentBeats"], "share": 8},
        {"key": "foreshadowing", "entries": [] if terminal else clues, "share": 6},
    ]
    if stale_subplots:
        slots.append({"key": "staleSubplots", "entries": _subplot_hints(stale_subplots, 4), "share": 2})
        slots[1]["share"] -= 2

    def render(parts):
        selected_facts = _allocated_arc_facts(parts["facts"])
        if parts["foreshadowing"]:
            selected_facts["openForeshadowing"] = parts["foreshadowing"]
        memory = {key: value for key, value in parts.items() if key not in ("facts", "foreshadowing")}
        if not memory.get("staleSubplots"):
            memory.pop("staleSubplots", None)
        body = _json({**material, "canonicalFacts": selected_facts, **memory})
        return {"system": system, "prompt": f"{header}【已发生事实与全书路标】\n{locks}{body}\n【输出结构】\n{_json(example)}{retry}"}

    if input_budget is None:
        return render({slot["key"]: slot["entries"] for slot in slots})
    return fit_prompt(render=render, slots=slots, input_budget=input_budget, code=ARC_PROMPT_BUDGET_EXCEEDED, label="滚纲")


def _chapter_write_facts(canonical, relevant, context, *, current_chapter=None, story_ledger=False,
                         book_plan=None, arc_plans=(), salience_index=None):
    if current_chapter is None and salience_index is not None:
        current_chapter = salience_index.get("beforeChapter")

    def source(key, fallback):
        value = canonical.get(key)
        return relevant.get(fallback) if value is None else value

    def tag(kind, entry, terms, key=None):
        record = _dict(entry)
        source_chapter = record.get("sourceChapter")
        return {
            "kind": kind, "value": entry, "key": key,
            "relevance": len({term for term in terms if _matches_term(term, context)}),
            "sourceChapter": _number(record.get("chapterNumber") if source_chapter is None else source_chapter),
        }

    def rank(entries):
        return sorted(entries, key=lambda item: (
            item["relevance"], item.get("salience", item["sourceChapter"]),
        ), reverse=True)

    injuries = [tag("activeInjuries", item, [item.get(key) for key in ("character", "bodyPart", "description")])
                for item in _visible_injuries(source("injuries", "activeInjuries"), context, current_chapter,
                                              story_ledger=story_ledger)]
    secrets = [
        tag("secrets", entry, [entry.get("secret"), *_list(entry.get("holders")), *_list(entry.get("excludedCharacters"))])
        for entry in _records(source("secrets", "secrets"))
    ]
    def entities(kind, fallback, terms, limit):
        if salience_index is None:
            return [tag(kind, entry, terms(key, entry), key) for key, entry in _dict(fallback).items()]
        ranked = _salient_facts(canonical, kind, salience_index, book_plan, arc_plans, current_chapter, context)
        rows = [
            {**tag(kind, entry, terms(name, entry), name),
             "salience": score if tier != "dormant" or hit else score - 20}
            for name, entry, tier, score, hit in ranked
        ]
        return rank(rows)[:limit]

    items = entities("items", source("items", "items"), lambda key, entry: [
        key, *(_dict(entry).get(field) for field in ("name", "owner", "holder", "location")),
    ], 28)
    timeline = [
        tag("recentTimeline", entry, [entry.get("event"), *_list(entry.get("characters"))], index)
        for index, entry in enumerate(_records(source("timeline", "recentTimeline"))[-5:])
    ]
    characters = rank(entities("characters", relevant.get("characters"),
                               lambda key, entry: [key, _dict(entry).get("name")], 32))
    clues = rank(tag("openForeshadowing", entry, [entry.get("id"), entry.get("clue")])
                 for entry in _records(relevant.get("openForeshadowing")))
    return rank([*injuries, *secrets, *items, *timeline]), characters, [entry["value"] for entry in clues]


def _chapter_anchors(book_plan, lead, include_core_rules, *, rolling_arc_outlines=False, story_notes="", boundary_state=(),
                     roster="", hidden_cast=()):
    anchors = []
    if is_v2_book_plan(book_plan):
        if locks := canon_locks_text(book_plan.get("canonLocks"), boundary_state=boundary_state):
            anchors.append(locks)
    elif rolling_arc_outlines and (locks := normalize_canon_locks(book_plan.get("canonLocks"))):
        anchors.append(f"不可变核心口径锁（初始值仅为历史基线）：{_json(locks)}")
    if story_notes:
        anchors.append(story_notes)
    if roster:
        anchors.append(roster)
    worldview = _dict(book_plan.get("worldview"))
    if book_plan.get("logline"):
        anchors.append(f"主线卖点：{book_plan['logline']}")
    if worldview.get("setting"):
        anchors.append(f"舞台世界观：{worldview['setting']}")
    cast = [entry for entry in _records(book_plan.get("mainCharacters")) if entry.get("name") not in hidden_cast]
    if lead:
        cast = sorted(cast, key=lambda entry: entry.get("name") == lead, reverse=True)
    if cast:
        names = "、".join(f"{entry.get('name') or ''}({entry.get('role') or ''})" for entry in cast[:6])
        anchors.append(f"核心人物：{names}")
    if include_core_rules:
        anchors.extend(f"世界观硬规则：{rule}" for rule in _list(worldview.get("coreRules"))
                       if isinstance(rule, str) and _EDGE_SPACE.sub("", rule))
    return _CHAPTER_WRITE_BOOK_ANCHOR_BLOCK.substitute(anchors="\n".join(anchors)) if anchors else ""


def _legacy_character_state(state):
    return {key: value for key, value in state.items() if key not in STORY_CHARACTER_FIELDS}


def _core_character_names(plan, facts, ledger):
    cast = _records(plan.get("mainCharacters"))
    lead = protagonist_name(cast)
    registry = character_name_registry(plan, facts)
    names = {}
    for name in (lead, *(row.get("name") for row in cast), *_dict(ledger.get("characterStates"))):
        if key := normalize_character_name(name):
            names.setdefault(key, name)
    return [{"name": _text_excerpt(name, 40),
             "role": _text_excerpt(registry.get(key, [{}])[0].get("role") or "", 40)}
            for key, name in list(names.items())[:12]]


def _story_ledger_entries(ledger):
    entries = []
    if clock := ledger.get("storyClock"):
        entries.append({"kind": "storyClock", "value": clock})
    entries.extend({"kind": "ledgerBalances", "value": row} for row in ledger.get("ledgerBalances", []))
    entries.extend({"kind": "characterStates", "name": name, "value": state}
                   for name, state in ledger.get("characterStates", {}).items())
    return entries


def _memory_slots(canonical, relevant, context, outline, lead, chapter_number, recent_summaries, *, arc_views=(),
                  salience_index=None, core_names=frozenset(), retired_names=frozenset()):
    hard_items = [
        {"kind": kind, "value": value}
        for kind in ("constraints", "commitments")
        for value in active_memory_items(canonical, kind, context)
    ]
    current = _number(chapter_number)
    archives = _chapter_volume_summaries(canonical, current, arc_views)[:2]
    last_archive = _number(archives[0]["endChapter"]) if archives else 0
    if arc_views:
        last_archive = max(last_archive, arc_views[0]["endChapter"])
    summaries = [entry for entry in _records(recent_summaries)
                 if last_archive < _number(entry.get("chapterNumber")) < current and entry.get("summary")]
    rolling = "\n".join(
        f"第{entry['chapterNumber']}章{'（待复核）' if entry.get('needsReview') else ''}：{_text_excerpt(entry['summary'], 260)}"
        for entry in summaries[-5:]
    )
    base = canonical.get("characters")
    characters = relevant_memory_characters(
        canonical, relevant.get("characters") if base is None else base, lead, f"{context} {_json(outline)}",
        salience_index=salience_index, core_names=core_names, retired_names=retired_names,
    )
    for entry in characters:
        value = entry["value"]
        if isinstance(value.get("state"), dict):
            value["state"] = _legacy_character_state(value["state"])
    goal = _dict(characters[0]["value"].get("state")).get("currentGoal") if characters else None
    compact_lead = {"key": lead, "value": {"name": lead, **({"state": {"currentGoal": goal}} if goal else {})}}
    payoffs = [
        entry for entry in _records(canonical.get("foreshadowing"))
        if entry.get("status") == "paid_off" and _number(entry.get("payoffChapter")) > 0
        and 0 < current - _number(entry.get("payoffChapter")) <= 20
    ]
    payoffs.sort(key=lambda item: _number(item["payoffChapter"]), reverse=True)
    return (
        hard_items, [{key: value for key, value in item.items() if key != "sourceHash"} for item in archives],
        rolling, characters, compact_lead if lead else None, payoffs[:4],
    )


def _chapter_volume_summaries(canonical, current, arc_views):
    before = min(row["startChapter"] for row in arc_views) if arc_views else current
    return sorted((item for item in _records(canonical.get("volumeSummaries"))
                   if item.get("endChapter") is not None and _number(item["endChapter"]) < before),
                  key=lambda item: _number(item["endChapter"]), reverse=True)


def _render_allocated_facts(parts, use_long_memory):
    def values(kind):
        return [item for item in parts["continuity"] if item["kind"] == kind]

    clues = parts["foreshadowing"]
    timeline = sorted(values("recentTimeline"), key=lambda item: item["key"])
    facts = {
        "characters": {entry["key"]: entry["value"] for entry in parts["characters"]},
        "activeInjuries": [entry["value"] for entry in values("activeInjuries")],
        "items": {entry["key"]: entry["value"] for entry in values("items")},
        "secrets": [entry["value"] for entry in values("secrets")],
        "openForeshadowing": [entry["value"] for entry in clues if not entry["paid"]] if use_long_memory else clues,
        "recentTimeline": [entry["value"] for entry in timeline],
    }
    hard_items = parts.get("hardMemory", [])
    story = {}
    for entry in hard_items:
        kind, value = entry["kind"], entry["value"]
        if kind == "storyClock":
            story[kind] = value
        elif kind == "ledgerBalances":
            story.setdefault(kind, []).append(value)
        elif kind == "characterStates":
            story.setdefault(kind, {})[entry["name"]] = value
    if story:
        facts["storyLedger"] = story
    if use_long_memory:
        blocks = {
            "constraints": [item["value"] for item in hard_items if item["kind"] == "constraints"],
            "commitments": [item["value"] for item in hard_items if item["kind"] == "commitments"],
            "volumeSummaries": parts["archives"],
            "subplots": parts["subplots"],
            "recentPayoffs": [entry["value"] for entry in clues if entry["paid"]],
        }
        memory = {key: value for key, value in blocks.items() if value}
        if memory:
            facts["longMemory"] = memory
    elif parts.get("subplots"):
        facts["subplots"] = parts["subplots"]
    return facts


def build_chapter_write_prompt(*, book_plan=None, outline=None, prev_chapter_ending="", recent_summaries=None,
                               prev_chapter_summary="", relevant_facts=None, canonical_facts=None,
                               pov=_DEFAULT_POV, target_words=2000, total_chapters=None, style_guide=_DEFAULT_STYLE,
                               meme_level="none", meme_samples=None, input_budget=None, draft_quality=False,
                               long_memory=False, chapter_ai_gate=False, rolling_arc_outlines=False, story_ledger=False,
                               boundary_state=(), arc_plans=(), salience_index=None):
    has_plan = book_plan is not None
    book_plan, outline, canonical = _dict(book_plan), _dict(outline), _dict(canonical_facts)
    chapter_number = outline.get("chapterNumber") or 1
    use_long_memory = long_memory and input_budget is not None
    lead = protagonist_name(book_plan.get("mainCharacters")) if use_long_memory else None
    ending = _EDGE_SPACE.sub("", str(prev_chapter_ending or prev_chapter_summary or ""))
    context = _chapter_context(_text_excerpt(ending, 300, from_end=True), outline)
    # Older prompt callers can pass an excerpt of the journal without chapter metadata.
    history = _records(canonical.get("factHistory"))
    canonical_history = not history or any(type(entry.get("chapter")) is int for entry in history)
    current = int(_number(chapter_number))
    use_subplot_projection = rolling_arc_outlines or long_memory and canonical_history
    if salience_index is None and has_plan and use_subplot_projection:
        salience_index = entity_index(canonical, before_chapter=current)
    roster, hidden_cast, core_names, retired_names, subplots = "", set(), set(), set(), []
    projection = canonical
    if salience_index is not None:
        retired = retired_roster(canonical, salience_index, arc_plans=arc_plans, current_chapter=current, limit=None)
        retired_names = set(retired)
        hidden_cast = {name for name in retired_names if not _mentions(name, context)}
        if retired_names and (rolling_arc_outlines or long_memory):
            prefix = "已退场/身故人物（未经铺垫不得复活或重登舞台）："
            names = []
            for name in retired[:30]:
                if len(prefix) + len("、".join([*names, name])) <= 180:
                    names.append(name)
            if names:
                roster = prefix + "、".join(names)
        core_names = {name for name, _, tier, _, _ in _salient_facts(
            canonical, "characters", salience_index, book_plan, arc_plans, current, context,
        ) if tier == "core"}
        projection = {**canonical, **_facts_before(canonical, current)}
    if use_subplot_projection or salience_index is not None:
        subplots, _ = active_subplots_view(canonical, current_chapter=current, context_text=context)
    relevant = (
        extract_relevant_facts(projection, context_text=context, current_chapter=current, story_ledger=story_ledger,
                               book_plan=book_plan if salience_index is not None else None, arc_plans=arc_plans,
                               salience_index=salience_index)
        if relevant_facts is None or salience_index is not None else relevant_facts
    )
    summaries = _list(recent_summaries)
    summary_text = "\n".join(
        entry if isinstance(entry, str) else f"第{_dict(entry).get('chapterNumber', 'undefined')}章：{_dict(entry).get('summary', 'undefined')}"
        for entry in summaries[-2:]
    )
    anchors = _chapter_anchors(book_plan, lead, input_budget is not None, rolling_arc_outlines=rolling_arc_outlines,
                               boundary_state=boundary_state, roster=roster, hidden_cast=hidden_cast)
    volume_card = volume_card_text(book_plan, chapter_number)
    arc_views = compact_arc_summaries(prior_arc_summaries(canonical, _number(chapter_number))) if rolling_arc_outlines else []
    finale = _CHAPTER_WRITE_FINALE_BLOCK.substitute(
        chapter_number=chapter_number, total_chapters=total_chapters,
    ) if total_chapters and _number(chapter_number) >= _number(total_chapters) else ""
    style_lines = compact_style_bible(book_plan.get("styleBible")) if draft_quality else []
    draft_rule = (_DRAFT_RULE if draft_quality else "") + (_WRITE_STYLE_RED_LINES if chapter_ai_gate else "")
    if rolling_arc_outlines or story_ledger or draft_quality and salience_index is not None:
        draft_rule += "\n章名中的数字仅为规划参考，若与已核验事实或台账冲突，正文以事实为准，切勿为迎合标题改写数值。"
    ledger = {}
    if story_ledger:
        ledger = story_ledger_view(canonical, chapter_number=_number(chapter_number), book_plan=book_plan,
                                   context_text=_chapter_context("", outline))
        if ledger.get("characterStates") and hidden_cast:
            ledger["characterStates"] = {name: state for name, state in ledger["characterStates"].items()
                                         if name not in hidden_cast}
        draft_rule += _STORY_LEDGER_WRITE + "\n" + STORY_LEDGER_MARKER
    if arc_views:
        draft_rule += "\n已演破局、信息揭示与大招只可承接后果或升级，禁止原样复演。"
    pretty_outline = _json(outline, pretty=True)

    def render(ending, summary, facts, samples, bible, card):
        summary_heading = "本卷滚动摘要" if use_long_memory else "近期章节摘要（最近2章）"
        summary_block = f"\n【{summary_heading}】\n{summary}" if summary else ""
        if input_budget is None:
            ending_block = f"\n【上一章末尾衔接片段（约800字）】\n{ending}"
        else:
            ending_block = f"\n【上一章末尾衔接片段】\n{ending}" if ending else ""
        style_block = "\n【本书风格指南（服从总纲与规范事实）】\n" + "\n".join(bible) if bible else ""
        system = _CHAPTER_WRITE_SYSTEM.substitute(
            pov=pov, pov_rule=_pov_rule(pov), target_words=target_words,
            min_words=_round(target_words * 0.9), max_words=_round(target_words * 1.1), style_guide=style_guide,
            meme_block=_meme_guidance(meme_level, samples, chapter_ai_gate=chapter_ai_gate), draft_rule=draft_rule,
        )
        prompt = _CHAPTER_WRITE_PROMPT.substitute(
            chapter_number=chapter_number, total_suffix=f"/{total_chapters}" if total_chapters else "",
            book_title=book_plan.get("title") or "未命名", pov=pov, target_words=target_words,
            book_anchors=anchors, finale=finale, escalation_block=f"\n{card}\n" if card else "",
            chapter_title=outline.get("title") or f"第{chapter_number}章",
            outline=pretty_outline, style_block=style_block, summary_block=summary_block, ending_block=ending_block,
            facts=_json(facts, pretty=True),
        )
        return {"system": system, "prompt": prompt}

    if input_budget is None:
        snippet = (
            _text_excerpt(prev_chapter_ending, 800, from_end=True) if prev_chapter_ending
            else prev_chapter_summary or "（本章为开篇或紧接总纲）"
        )
        visible = _visible_injuries(relevant.get("activeInjuries"), context, chapter_number, story_ledger=story_ledger)
        return render(snippet, summary_text, {**relevant, "activeInjuries": visible,
                                              **({"arcSummaries": arc_views} if arc_views else {}),
                                              **({"subplots": subplots[:8]} if subplots else {}),
                                              **({"storyLedger": ledger} if ledger else {})}, meme_samples, style_lines, volume_card)

    continuity, characters, clues = _chapter_write_facts(projection, relevant, context, current_chapter=current,
                                                        story_ledger=story_ledger, book_plan=book_plan,
                                                        arc_plans=arc_plans, salience_index=salience_index)
    has_style = bool(style_lines)
    style_slot = [{"key": "styleBible", "entries": style_lines, "share": 8}] if has_style else []
    arc_slot = [{"key": "arcSummaries", "entries": arc_views, "share": 10}] if arc_views else []
    story_items = _story_ledger_entries(ledger)
    samples = _list(meme_samples) if meme_level != "none" else []
    if use_long_memory:
        hard_items, archives, rolling, characters, fallback, payoffs = _memory_slots(
            projection, relevant, context, outline, lead, chapter_number, summaries, arc_views=arc_views,
            salience_index=salience_index, core_names=core_names, retired_names=retired_names,
        )
        slots = [
            {"key": "hardMemory", "entries": story_items + hard_items, "share": 20},
            {"key": "continuity", "entries": continuity, "share": 30 if has_style else 32},
            {"key": "archives", "entries": archives, "share": 0 if arc_views else 10},
            *arc_slot,
            *style_slot,
            {"key": "ending", "text": ending, "from_end": True, "share": 12 if has_style else 15},
            {"key": "summary", "text": rolling, "share": 6},
            {"key": "characters", "entries": characters, "allocate_first": bool(lead),
             "first_fallback": fallback, "share": 7 if has_style else 10},
            {"key": "subplots", "entries": subplots[:8] if use_subplot_projection or salience_index is not None else
             active_memory_items(canonical, "subplots", context), "share": 3},
            {"key": "foreshadowing", "entries": [{"value": value, "paid": False} for value in clues]
             + [{"value": value, "paid": True} for value in payoffs], "share": 3},
            {"key": "samples", "entries": samples, "share": 1},
        ]
    else:
        story_slot = [{"key": "hardMemory", "entries": story_items, "share": 20}] if story_items else []
        slots = [
            *story_slot,
            {"key": "continuity", "entries": continuity, "share": 50 - 10 * bool(arc_views) - 20 * bool(story_items)},
            *arc_slot,
            *style_slot,
            {"key": "ending", "text": ending, "from_end": True, "share": 20},
            {"key": "summary", "text": summary_text, "share": 9 if has_style else 12},
            {"key": "characters", "entries": characters, "share": 7 if has_style else 10},
            {"key": "foreshadowing", "entries": clues, "share": 4 if has_style else 6},
            {"key": "samples", "entries": samples, "share": 2},
        ]
        if subplots:
            slots.append({"key": "subplots", "entries": subplots[:8], "share": 3})
            slots[len(story_slot)]["share"] -= 3

    continuity_slot = next(slot for slot in slots if slot["key"] == "continuity")
    if rolling_arc_outlines:
        current = _number(chapter_number)
        horizon = horizon_memory(canonical, (), current)
        theme_context = " ".join(str(value or "") for value in (
            outline.get("title"), outline.get("goal"), outline.get("summary"), *_list(outline.get("keyEvents")),
        ))
        entries = [("themeHits", hit) for hit in theme_hits(horizon["themes"], theme_context)]
        if earlier := _chapter_volume_summaries(canonical, current, arc_views):
            volume = earlier[0]
            entries.append(("earlierVolume", {key: volume[key] for key in ("volume", "startChapter", "endChapter") if key in volume}
                            | {"mainline": _text_excerpt(volume.get("mainline") or "", 140)}))
        continuity_slot["share"] -= 3
        slots.insert(slots.index(continuity_slot), {"key": "horizon", "entries": entries, "share": 3})

    if volume_card:
        continuity_slot["share"] -= 4
        slots.append({"key": "volumeCard", "text": volume_card, "share": 4})

    def render_budgeted(parts):
        rendered_facts = _render_allocated_facts(parts, use_long_memory)
        if parts.get("arcSummaries"):
            rendered_facts["arcSummaries"] = parts["arcSummaries"]
        if parts.get("horizon"):
            horizon_facts = {}
            for kind, value in parts["horizon"]:
                if kind == "themeHits":
                    horizon_facts.setdefault(kind, []).append(value)
                else:
                    horizon_facts[kind] = value
            rendered_facts["horizon"] = horizon_facts
        # A partial card could state a ban or transition without its qualifying condition.
        card = volume_card if parts.get("volumeCard") == volume_card else ""
        return render(parts["ending"], parts["summary"], rendered_facts, parts["samples"], parts.get("styleBible", []), card)

    return fit_chapter_prompt(render=render_budgeted, slots=slots, input_budget=input_budget)


def build_word_count_adjust_prompt(*, chapter_number, chapter_text, current_words, target_words, outline=None,
                                   pov=_DEFAULT_POV):
    values = {
        "chapter_number": chapter_number, "current_words": current_words, "target_words": target_words,
        "mode": "扩写充实" if current_words < target_words else "精简凝练",
    }
    system = _WORD_COUNT_ADJUST_SYSTEM.substitute(
        **values, pov=pov, min_words=_round(target_words * 0.92), max_words=_round(target_words * 1.08),
    )
    prompt = _WORD_COUNT_ADJUST_PROMPT.substitute(
        **values, outline=_json(outline) if outline is not None else "按原文剧情推进", chapter_text=chapter_text,
    )
    return {"system": system, "prompt": prompt}


def build_retitle_prompt(*, planned_title, conflicts, chapter_text, recent_titles=()):
    system = ("[TASK:RETITLE]\n只输出一行不超过22字的中文章名，不带第X章前缀、标记或解释。"
              "依据正文重拟，避免与正文冲突的数字及与近期章名重复。")
    title = _text_excerpt(planned_title, 80)
    entries = [
        {"titleExpr": _text_excerpt(row["titleExpr"], 24),
         "bodyExprs": [_text_excerpt(expr, 24) for expr in row["bodyExprs"][:4]]}
        for row in conflicts
    ]

    def render(parts):
        material = {"plannedTitle": title, "conflicts": parts["conflicts"], "recentTitles": parts["recent"],
                    "beginning": parts["beginning"], "ending": parts["ending"]}
        return {"system": system, "prompt": _json(material)}

    return fit_prompt(render=render, input_budget=1000, reserve=0, slots=[
        {"key": "conflicts", "entries": entries, "share": 22},
        {"key": "recent", "entries": [_text_excerpt(value, 30) for value in recent_titles[-6:] if value], "share": 8},
        {"key": "beginning", "text": chapter_text[:400], "share": 35},
        {"key": "ending", "text": chapter_text[-400:], "from_end": True, "share": 35},
    ])


def _chapter_context(chapter_text, outline):
    outline = _dict(outline)
    return " ".join(str(value or "") for value in (
        chapter_text, outline.get("title"), outline.get("goal"), outline.get("summary"), outline.get("hook"),
        *_list(outline.get("keyEvents")),
    ))


def _memory_extraction(facts, context):
    active = {
        kind: [{key: entry[key] for key in ("id", "text", "deadline") if key in entry}
               for entry in active_memory_items(facts, kind, context, 6)]
        for kind in ("constraints", "commitments", "subplots")
    }
    return _MEMORY_EXTRACTION_INSTRUCTIONS.substitute(active=_json(active))


def _memory_delta_spec(long_memory, story_ledger):
    if not (long_memory or story_ledger):
        return ""
    general_character = (
        ', "currentGoal": "当前目标", "emotion": "情绪", "stance": "立场", "relationships": { "另一角色": "本章关系变化" }'
        if long_memory else ""
    )
    if story_ledger and not long_memory:
        general_character = ', "relationships": { "另一核心角色": "本章关系变化" }'
    locked_character = (
        ', "stanceTier": "hostile|wary|transactional|ally|intimate", "knownFacts": ["实际亲证的关键认知"]'
        if story_ledger else ""
    )
    return _MEMORY_DELTA_JSON.substitute(
        general_character=general_character, locked_character=locked_character,
        general_memory=_GENERAL_MEMORY_FIELDS if long_memory else "",
        story_memory=_STORY_MEMORY_FIELDS if story_ledger else "",
    )


def _story_notes(ledger):
    return f"【本章前随章台账与核心人物锁】\n{_json(ledger)}" if ledger else ""


def _review_notes(book_plan, facts, context, chapter_number, long_memory, *, rolling_arc_outlines=False,
                  story_ledger=False, boundary_state=()):
    plan = _dict(book_plan)
    facts = _dict(facts)
    current = _number(chapter_number)
    ledger = story_ledger_view(facts, chapter_number=current, book_plan=plan, context_text=context) if story_ledger else {}
    world = _dict(plan.get("worldview"))
    rules = [rule.strip() for rule in _list(world.get("coreRules")) if isinstance(rule, str) and rule.strip()]
    rules.sort(key=lambda rule: not any(_matches_term(term, context) for term in _TERMS.split(rule)))
    cast = _records(plan.get("mainCharacters"))
    lead = protagonist_name(cast)
    cast = sorted(cast, key=lambda entry: entry.get("name") != lead)[:6]
    anchors = _chapter_anchors({
        "logline": _text_excerpt(plan.get("logline") or "", 150),
        "worldview": {"setting": _text_excerpt(world.get("setting") or "", 220),
                      "coreRules": [_text_excerpt(rule, 120) for rule in rules[:10]]},
        "mainCharacters": [{key: _text_excerpt(entry.get(key) or "", 40) for key in ("name", "role")} for entry in cast],
        "canonLocks": plan.get("canonLocks"),
        "planVersion": plan.get("planVersion"),
    }, None, True, rolling_arc_outlines=rolling_arc_outlines, story_notes=_story_notes(ledger), boundary_state=boundary_state)
    if card := volume_card_text(plan, chapter_number):
        anchors += "\n" + card
    arc_views = prior_arc_summaries(facts, current) if rolling_arc_outlines else []
    if arc_views:
        anchors += "\n【前两个单元与累计已演桥段（正文复演需拦截）】\n" + _json({
            "arcSummaries": compact_arc_summaries(arc_views), "usedBeats": compact_used_beats(arc_views, limit=12),
        })
    if not long_memory:
        return anchors

    memory = {}
    for kind in ("constraints", "commitments"):
        prior = [entry for entry in _records(facts.get(kind)) if
                 max(_number(entry.get("sourceChapter")), _number(entry.get("updatedAtChapter"))) < current]
        entries = [
            {**{key: entry[key] for key in ("sourceChapter", "updatedAtChapter") if key in entry},
             **{key: _text_excerpt(entry[key], limit) for key, limit in
                (("id", 80), ("text", 120), ("deadline", 60)) if isinstance(entry.get(key), str)}}
             for entry in active_memory_items({kind: prior}, kind, context, 6)
        ]
        if entries:
            memory[kind] = entries
    archives = sorted((entry for entry in _records(facts.get("volumeSummaries")) if
                       0 < _number(entry.get("endChapter")) < current and entry.get("mainline")),
                      key=lambda entry: _number(entry["endChapter"]), reverse=True)[:2]
    if archives and not arc_views:
        memory["volumeSummaries"] = [
            {**{key: entry[key] for key in ("startChapter", "endChapter") if key in entry},
             "mainline": _text_excerpt(entry["mainline"], 220),
             "turningPoints": [{"chapter": _number(point.get("chapter")), "text": _text_excerpt(point.get("text") or "", 65)}
                               for point in _records(entry.get("turningPoints"))[:2]]}
            for entry in archives
        ]
    return f"{anchors}\n【当前约束与前卷摘要】\n{_json(memory)}" if memory else anchors


_REVIEW_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_REVIEW_WORD = re.compile(r"[a-z0-9]{2,}")
_REVIEW_OUTLINE_FIELDS = ("chapterNumber", "title", "goal", "summary", "keyEvents", "hook")
_REVIEW_SHARES = {"textEntities": 30, "repeatRecall": 18, "hardMemory": 12, "arcNear": 10,
                  "foreshadowing": 10, "coreRules": 8, "subplots": 7, "horizon": 5}
_REPAIR_SHARES = {"textEntities": 50, "repeatRecall": 20, "hardMemory": 15, "coreRules": 15}


def _review_terms(text, width=4):
    terms = set(_REVIEW_WORD.findall(text.casefold()))
    for run in _REVIEW_CJK.findall(text):
        if len(run) < width:
            if len(run) >= 2:
                terms.add(run)
        else:
            terms.update(run[index:index + width] for index in range(len(run) - width + 1))
    return terms


def recall_used_beats(arc_summaries, chapter_text, *, exclude_near=(), limit=6, min_overlap=0.35) -> list[dict]:
    excluded = set(exclude_near)
    terms = _review_terms(chapter_text)
    matches = {}
    folded = chapter_text.casefold()
    for row in arc_summaries:
        if (row.get("startChapter"), row.get("endChapter")) in excluded:
            continue
        for beat in _records(row.get("usedBeats")):
            text, chapter = beat.get("text"), beat.get("chapter")
            if type(chapter) is not int or not isinstance(text, str) or not (text := text.strip()):
                continue
            keys = _review_terms(text)
            ratio = len(keys & terms) / len(keys) if keys else 0
            exact = any(len(phrase) >= 4 and phrase in folded for phrase in
                        re.findall(r"[\w]+", text.casefold()))
            if ratio >= min_overlap or exact:
                key = text.casefold()
                score = ratio, exact, chapter
                if key not in matches or score > matches[key][0]:
                    matches[key] = score, {"chapter": chapter, "text": _text_excerpt(text, 160)}
    return [beat for _, beat in sorted(matches.values(), key=lambda pair: pair[0], reverse=True)[:max(0, limit)]]


def build_review_context(*, book_plan, canonical_facts, outline, chapter_number, long_memory=False,
                         rolling_arc_outlines=False, story_ledger=False, boundary_state=(), arc_plans=()) -> dict:
    """Prepare prior evidence once; chapter-dependent relevance is applied during rendering."""
    plan, facts = _dict(book_plan), _dict(canonical_facts)
    current = int(_number(chapter_number))
    world = _dict(plan.get("worldview"))
    locks = plan.get("canonLocks")
    if is_v2_book_plan(plan):
        locks_text = canon_locks_text(locks, boundary_state=boundary_state)
    else:
        if rolling_arc_outlines and (compact_locks := normalize_canon_locks(locks)):
            locks = compact_locks
        locks_text = _json(locks) if isinstance(locks, dict) and locks else ""
    horizon = horizon_memory(facts, arc_plans, current)
    projection = _facts_before(facts, current)
    subplots, idle = active_subplots_view(facts, current_chapter=current) if long_memory else ([], [])
    clues = _records(projection["foreshadowing"] if "foreshadowing" in facts else facts.get("openForeshadowing"))
    return {
        "chapter_number": current, "book_plan": plan, "arc_plans": arc_plans,
        "outline": {key: outline[key] for key in _REVIEW_OUTLINE_FIELDS if key in _dict(outline)},
        "canon_locks_text": locks_text,
        "volume_card_text": volume_card_text(plan, current),
        "anchors": _chapter_anchors({
            "logline": _text_excerpt(plan.get("logline") or "", 150),
            "worldview": {"setting": _text_excerpt(world.get("setting") or "", 220)},
            "mainCharacters": [{key: _text_excerpt(entry.get(key) or "", 40) for key in ("name", "role")}
                               for entry in _records(plan.get("mainCharacters"))[:6]],
        }, None, False),
        "story_ledger_view": story_ledger_view(facts, chapter_number=current, book_plan=plan) if story_ledger else {},
        "entity_index": entity_index(facts, before_chapter=current),
        "horizon_memory": horizon,
        "arcSummaries": prior_arc_summaries(facts, current),
        "constraints": [row for row in _records(projection.get("constraints")) if row.get("status") == "active"],
        "commitments": [row for row in _records(projection.get("commitments")) if row.get("status") == "active"],
        "subplots": [*subplots, *idle],
        "coreRules": [rule.strip() for rule in _list(world.get("coreRules")) if isinstance(rule, str) and rule.strip()],
        "openForeshadowing": [row for row in clues if row.get("status") != "paid_off"],
        "payoffs": sorted((row for row in clues if row.get("status") == "paid_off"
                           and 0 < current - _number(row.get("payoffChapter")) <= 20),
                          key=lambda row: _number(row.get("payoffChapter")), reverse=True)[:4],
        "injuries": projection.get("injuries", facts.get("activeInjuries")),
        "secrets": projection.get("secrets"),
        "long_memory": long_memory,
    }


def _review_entry(row, fields, *, cap=120):
    return {key: _text_excerpt(value, cap) if isinstance(value, str) else
            [_text_excerpt(item, 60) if isinstance(item, str) else item for item in value[:8]]
            if isinstance(value, list) else value for key in fields if (value := row.get(key)) is not None}


def _review_entities(review, context, issue_text):
    index = review["entity_index"]
    selected = []
    for kind, maximum, fields in (
        ("characters", 24, ("name", "status", "location", "role", "traits", "sourceChapter", "currentGoal", "stance")),
        ("items", 20, ("name", "owner", "holder", "location", "status", "attributes", "sourceChapter", "value")),
    ):
        ranked = ranked_entities(
            kind, index, book_plan=review["book_plan"], arc_plans=review["arc_plans"],
            current_chapter=review["chapter_number"], context_text=context,
            resolved_theme_ranges=review["horizon_memory"]["themes"],
        )
        hits = sorted((name for name, _, _, hit in ranked if hit), key=lambda name: not _mentions(name, issue_text))
        for rank, name in enumerate(hits[:maximum]):
            row = index[kind][name]
            selected.append((not _mentions(name, issue_text), rank, kind,
                             _review_entry({**row["state"], **row["entry"], "name": name}, fields)))

    for kind, rows, fields, match_fields in (
        ("activeInjuries", _visible_injuries(review["injuries"], context, review["chapter_number"]),
         ("id", "character", "bodyPart", "description", "severity", "sourceChapter"), ("character", "description", "bodyPart")),
        ("secrets", _records(review["secrets"]),
         ("secret", "holders", "excludedCharacters", "sourceChapter"), ("secret", "holders", "excludedCharacters")),
    ):
        def matches(row, text):
            return any(_mentions(term, text) for key in match_fields for term in
                       (row[key] if isinstance(row.get(key), list) else [row.get(key)]))

        matched = sorted((row for row in rows if matches(row, context)), key=lambda row: not matches(row, issue_text))
        selected.extend((not matches(row, issue_text), rank, kind, _review_entry(row, fields))
                        for rank, row in enumerate(matched[:20]))
    return [(kind, row) for _, _, kind, row in sorted(selected, key=lambda entry: entry[:2])]


def _review_slots(review, chapter_text, *, repair=False, issues=(), ai_issues=()):
    issue_text = _json([issues, ai_issues]) if repair else ""
    context = _chapter_context(chapter_text, review["outline"]) + " " + issue_text
    terms = _review_terms(context)
    horizon = review["horizon_memory"]

    def relevance(value):
        return len(_review_terms(value) & terms)

    def matched(rows, fields, limit):
        ranked = [(relevance(" ".join(str(row.get(key) or "") for key in fields)), row) for row in rows]
        return [row for score, row in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit] if score]

    values = {
        "textEntities": _review_entities(review, context, issue_text),
        "repeatRecall": [],
        "hardMemory": [],
        "coreRules": [("coreRules", _text_excerpt(rule, 120))
                      for rule in sorted(review["coreRules"], key=relevance, reverse=True)[:10]],
    }
    if not repair or any(isinstance(issue, dict) and "core_scene_repetition" in (
        issue.get("type"), issue.get("category"), issue.get("hardConflictType"),
    ) for issue in (*issues, *ai_issues)):
        values["repeatRecall"] = [
            *(("recalledBeats", beat) for beat in recall_used_beats(
                review["arcSummaries"], chapter_text,
                exclude_near=[(row["startChapter"], row["endChapter"]) for row in horizon["near"]],
            )),
            *(("usedBeats", beat) for beat in horizon["recentBeats"]),
            *(("themeHits", hit) for hit in theme_hits(horizon["themes"], context)),
        ]
    if review["long_memory"]:
        for kind in ("constraints", "commitments"):
            values["hardMemory"].extend((kind, _review_entry(row, ("id", "text", "deadline", "sourceChapter", "updatedAtChapter")))
                                        for row in active_memory_items(review, kind, context, 6))
    if not repair:
        values.update(
            arcNear=[("arcSummaries", row) for row in compact_arc_summaries(horizon["near"], include_beats=False)],
            foreshadowing=[("openForeshadowing", _review_entry(row, ("id", "clue", "details", "status", "plantedChapter")))
                          for row in matched(review["openForeshadowing"], ("clue", "details", "id"), 10)],
            subplots=[("activeSubplots", _review_entry(row, ("id", "text", "characters", "updatedAtChapter")))
                      for row in matched(review["subplots"], ("text", "title", "characters"), 6)]
                     + [("recentPayoffs", _review_entry(row, ("id", "clue", "details", "payoffChapter")))
                        for row in review["payoffs"]],
            horizon=[("volumeSummaries", {"range": row["range"], "mainline": _text_excerpt(row["mainline"], 140)})
                     for row in horizon["volumes"][:1]],
        )
    shares = _REPAIR_SHARES if repair else _REVIEW_SHARES
    return [{"key": key, "share": share, "entries": values[key]} for key, share in shares.items()]


def _review_blocks(review, chapter_text, issues=()):
    view = review["story_ledger_view"]
    evidence = normalize_quote(chapter_text + _json(issues))
    states = {name: row for name, row in view.get("characterStates", {}).items()
              if normalize_quote(name) in evidence}
    ledger = {**view, **({"characterStates": states} if states else {})}
    if not states:
        ledger.pop("characterStates", None)
    blocks = [review["anchors"]]
    if review["canon_locks_text"]:
        blocks.append("【总纲口径锁】\n" + review["canon_locks_text"])
    if review["volume_card_text"]:
        blocks.append(review["volume_card_text"])
    if ledger:
        blocks.append("【本章前随章台账与核心人物锁】\n" + _json({"storyLedger": ledger}))
    return "".join("\n" + block for block in blocks if block)


def _allocated_review(parts):
    sections = {}
    for slot, entries in parts.items():
        groups = {}
        for kind, row in entries:
            groups.setdefault(kind, []).append(row)
        if groups:
            sections[slot] = groups
    return _json(sections)


def _compact_schema(template, marker, **schema_values):
    body, schema = template.template.split(marker, 1)
    return Template(body + marker + _json(json.loads(Template(schema).substitute(schema_values))))


def build_volume_summary_prompt(chapters):
    evidence = []
    for entry in chapters:
        content = entry.get("content") or ""
        evidence.append({
            **{key: entry[key] for key in ("chapterNumber", "title") if key in entry},
            "summary": _text_excerpt(entry.get("summary") or "", 280),
            "beginning": _text_excerpt(content, 400),
            "ending": _text_excerpt(content, 600, from_end=True),
            **({"status": "needs_review"} if entry.get("status") == "needs_review" else {}),
        })
    prompt = _VOLUME_SUMMARY_PROMPT.substitute(
        start_chapter=chapters[0]["chapterNumber"], end_chapter=chapters[-1]["chapterNumber"], evidence=_json(evidence),
    )
    return {"system": _VOLUME_SUMMARY_SYSTEM, "prompt": prompt}


_ARC_SUMMARY_SYSTEM = """[TASK:ARC_SUMMARY]
从已提交章的正文提取真正发生的剧情，章纲式摘要只用于指路。needs_review 章里已写出的场景可以用于去重，待复核的数值与设定不得当成硬事实。
只返回 JSON。mainline 最多280字，turningPoints 至多3条，usedHooks 至多3条。
usedBeats 只记录实际消耗的核心破局机制、大招与揭示；usedHooks 只记录实际出现的断章方式。每项提供章号、60字内可辨认的机制描述、4–120字的本章连续原文 quote。
每章有实质桥段时先各记一条，再补第二条，同章类似器物或手段合并为一条。
themes 给1–3个已解决议题、物件或场景的短名词短语，每条最多24字。skeleton 按已发生的顺序选择2–5个功能代码。shape 用一句不超过40字的话概括单元结构。
villainStatus 最多120字，写清实际新增/失去的筹码和退场状态，说明无新增筹码；没有反派可留空。""" + "\n功能代码：" + _SKELETON_VOCABULARY


def _arc_story_review(arc, chapters, canonical_facts, book_plan):
    start = arc["startChapter"]
    baseline = story_ledger_view(canonical_facts, chapter_number=start, book_plan=book_plan)
    pending = {row["chapterNumber"] for row in chapters if row.get("status") == "needs_review"}
    facts = _dict(canonical_facts)
    history = [event for event in _records(facts.get("factHistory")) if _number(event.get("chapter")) not in pending]
    changes = story_ledger_events({"factHistory": history}, start, arc["endChapter"], book_plan=book_plan)
    return {
        **({"canonLocks": normalize_canon_locks(_dict(book_plan).get("canonLocks")) or {}}
           if not is_v2_book_plan(book_plan) else {}),
        "baselineBeforeChapter": start,
        "openingLedger": baseline,
        "changesByChapter": changes,
    }


def build_arc_summary_prompt(arc, chapters, *, story_ledger=False, canonical_facts=None, book_plan=None, boundary_state=()):
    evidence = []
    for chapter in chapters:
        content = chapter.get("content") or ""
        record = {key: chapter[key] for key in ("chapterNumber", "title", "status") if key in chapter}
        record["summary"] = _text_excerpt(chapter.get("summary") or "", 240)
        if len(content) <= 4400:
            record["content"] = content
        else:
            middle = len(content) // 2
            record.update(beginning=content[:1400], middle=content[middle - 750:middle + 750], ending=content[-1500:])
        evidence.append(record)
    example = {"mainline": "实际主线进展", "turningPoints": [{"chapter": arc["startChapter"], "text": "转折"}],
               "usedBeats": [{"chapter": arc["startChapter"], "text": "实际破局机制", "quote": "逐字原文"}],
               "themes": ["通行文书"], "skeleton": ["investigation", "confrontation"], "shape": "追查文书来历，迫使守门人交割。",
               "usedHooks": [], "villainStatus": "反派名：新增筹码、失去筹码、退场状态"}
    system = _ARC_SUMMARY_SYSTEM + f"\n本单元 usedBeats 最多 {min(6, max(4, len(chapters)))} 条。"
    review = ""
    facts = _dict(canonical_facts)
    subplot_facts = {**facts, "arcSummaries": prior_arc_summaries(facts, arc["startChapter"])}
    subplot_context = "\n".join(str(chapter.get(key) or "") for chapter in chapters for key in ("title", "summary", "content"))
    visible, stale = active_subplots_view(subplot_facts, current_chapter=arc["endChapter"] + 1, context_text=subplot_context)
    if visible or stale:
        review += f"【可收束支线】\n{_json(_subplot_hints(visible + stale, 6))}\n"
        example["subplotClosures"] = [{"id": "沿用支线ID", "status": "resolved|dropped", "quote": "4–120字原文（dropped可空）"}]
        system += "\n可选 subplotClosures。已在本单元正文收束的用 resolved 并附原文，决定不再推进的用 dropped。"
    v2 = is_v2_book_plan(book_plan)
    if v2:
        locks = canon_locks_text(book_plan.get("canonLocks"), boundary_state=boundary_state)
        volumes = volumes_for_range(book_plan, arc["startChapter"], arc["endChapter"])
        review += (f"{locks}\n" if locks else "") + f"【本单元涉及的分卷阶梯】\n{_json(volumes)}\n"
        example["volumeProgress"] = {"escalation": "已发生的升级≤40字", "cost": "已承担的代价≤30字",
                                     "retiredReused": ["退役手段复演，≤2条，每条≤24字"]}
        system += ("\n可选 volumeProgress，只记录正文已发生的升级、代价和退役手段复演。"
                   "按章核对边界变更条件，生效前沿用当时口径。")
    if story_ledger:
        material = _arc_story_review(arc, chapters, canonical_facts, book_plan)
        review += f"【单元开始前的台账基线与按章变化】\n{_json(material)}\n"
    if story_ledger or v2:
        example["driftChecks"] = [{"chapter": arc["startChapter"], "kind": "value|clock|canon|character_ooc" if story_ledger else "canon",
                                    "issue": "60字内的具体冲突", "fixHint": "80字内的纠偏建议", "quote": "4–120字本章原文"}]
        system += (
            ("\n按单元开始前基线和按章变化顺序快检，不能用单元末数值或人物立场倒推早前章节。"
             if story_ledger else "\n只用 kind=canon 记录与总纲口径冲突的片段。") +
            "最多返回4条 driftChecks，只报有本章原文并能对应明确事实的硬冲突。"
            "quote 为4–120字正文，issue 不超过60字，fixHint 不超过80字。无可靠问题可省略或给空数组。"
            "快检只供下一单元纠偏，保持实际正文事实和归档的其他字段。"
        )
        if story_ledger:
            system += f"\n{STORY_LEDGER_MARKER}"
    prompt = (f"归档第 {arc['arcIndex']} 单元，第 {arc['startChapter']}–{arc['endChapter']} 章。\n"
              f"{review}【正文依据】\n{_json(evidence)}\n【输出结构】\n{_json(example)}")
    return {"system": system, "prompt": prompt}


def build_fact_extraction_prompt(*, chapter_number, chapter_text, existing_facts=None, canonical_facts=None,
                                 long_memory=False, outline=None, story_ledger=False, book_plan=None,
                                 input_budget=None, compact=False):
    effective_facts = existing_facts if canonical_facts is None else canonical_facts
    context = _chapter_context(chapter_text, outline)
    memory_instructions = _memory_extraction(effective_facts, context) if long_memory else ""
    system = _FACT_EXTRACTION_SYSTEM + (_FACT_RECORDING_RULES if story_ledger or is_v2_book_plan(book_plan)
                                        else _LEGACY_FACT_RECORDING_RULES)
    if story_ledger:
        ledger = story_ledger_view(effective_facts, chapter_number=_number(chapter_number), book_plan=book_plan,
                                   context_text=context)
        if ledger:
            memory_instructions += "\n" + _story_notes(ledger)
        system += _STORY_LEDGER_EXTRACTION + "\n" + STORY_LEDGER_MARKER
    compact = compact or input_budget is not None
    memory_spec = _memory_delta_spec(long_memory, story_ledger)
    template = _compact_schema(
        _FACT_EXTRACTION_PROMPT, "输出（无新增项可为空数组或空对象）：\n", memory_spec=memory_spec,
    ) if compact else _FACT_EXTRACTION_PROMPT
    prompt = template.substitute(
        chapter_number=chapter_number, chapter_text=chapter_text,
        facts=_json(extract_relevant_facts(effective_facts, context_text=context, current_chapter=chapter_number,
                                          story_ledger=story_ledger, book_plan=book_plan if compact else None,
                                          max_characters=24 if compact else None), pretty=not compact),
        memory_instructions=memory_instructions,
        memory_spec=memory_spec,
    )
    return {"system": system, "prompt": prompt}


def build_drift_patch_prompt(*, chapter, drifts, book_plan=None, ledger_view=None, boundary_state=()):
    text = chapter.get("content") or ""
    windows = []
    for drift in drifts[:3]:
        if span := quote_span(drift.get("quote"), text):
            start, end = span
            windows.append({**{key: drift[key] for key in ("id", "kind", "issue", "fixHint", "quote")},
                            "window": text[max(0, start - 600):end + 600]})
    locks = canon_locks_text(_dict(book_plan).get("canonLocks"), boundary_state=boundary_state)
    baseline = _dict(ledger_view)
    context = " ".join(window["window"] for window in windows)
    balances = _records(baseline.get("ledgerBalances"))
    balances = _pick_relevant(balances, context, 6, lambda row: row.get("label") or "")
    material = {"ledgerBalances": [{key: row[key] for key in ("label", "value", "updatedAtChapter") if key in row}
                                   for row in balances], "storyClock": _dict(baseline.get("storyClock"))}
    system = (
        "[TASK:DRIFT_PATCH]\n你负责在给定原文窗口内核实并局部修正单元归档发现的偏差。"
        "每条偏差最多一个补丁。只有问题有正文和口径依据且可局部修正时填 confirmed 为 true，否则填 false。"
        "find 必须逐字引用唯一原文并包含给定引句，至多200字。只改必要字词，保留段落、文风和章末钩子。"
        "不要更动与该偏差无关的事实。仅返回严格 JSON，reason 不超过40字。"
    )
    schema = '{"patches":[{"driftId":"...","confirmed":true,"find":"包含引句的原文","replace":"局部修正","reason":"修正依据"}]}'
    prompt = (f"第{chapter['chapterNumber']}章局部纠偏\n{locks}\n【章前台账】{_json(material)}\n"
              f"【偏差与局部窗口】{_json(windows)}\n【输出】{schema}")
    return {"system": system, "prompt": prompt}


def build_verify_prompt(*, chapter_number, chapter_text, outline=None, canonical_facts=None, prev_chapter_ending="",
                        pov=_DEFAULT_POV, min_words=500, max_words=5000, check_ai_flavor=True,
                        chapter_ai_gate=False, long_memory=False, book_plan=None, rolling_arc_outlines=False,
                        story_ledger=False, boundary_state=(), input_budget=None, review_context=None, arc_plans=()):
    # As in Node, exact word counts are checked locally and are not sent to the reviewer.
    ai_instruction = ""
    if check_ai_flavor:
        instruction = (
            "一般 AI 味只用 aiIssues 报告，避免重复记录。P0 是严重损害全章可读性的问题，P1 是必须当章解决的明显重复、"
            "叙事停滞或角色声音混同，P2 是局部措辞建议。P0/P1 必须给出可在本章定位的原文 quote 和具体修法，并令 passed=false。P2 不阻断。"
            if chapter_ai_gate else
            "轻微 AI 味放 aiIssues 或 warning；严重注水可阻塞。上述核心桥段复演始终按硬冲突判定。"
        )
        ai_instruction = _VERIFY_AI_FLAVOR_INSTRUCTION.substitute(
            instruction=instruction, style_focus=_VERIFY_STYLE_FOCUS if chapter_ai_gate else "",
        )
    severity = (
        "硬性逻辑或设定冲突在 issues 中标 error；有原文的 P0/P1 放 aiIssues。任一存在则 passed=false，轻微瑕疵或后续建议只记告警。"
        if chapter_ai_gate else
        '只有存在真实不可调和的硬冲突时才设 severity="error" 且 passed=false；轻微瑕疵或后续建议一律设为 severity="warning" 且 passed=true。'
    )
    system = _VERIFY_SYSTEM.substitute(
        pov=pov, ai_instruction=ai_instruction, severity_rule=severity,
        fact_recording_rules=_FACT_RECORDING_RULES if story_ledger or rolling_arc_outlines or is_v2_book_plan(book_plan)
        else _LEGACY_FACT_RECORDING_RULES,
        memory_rule="\n若有活跃硬规则与期限，也核对本章是否延续；有原文支撑的正常状态变化应更新记忆。" if long_memory else "",
    )
    if story_ledger:
        system += _STORY_LEDGER_VERIFY + _STORY_LEDGER_EXTRACTION + "\n" + STORY_LEDGER_MARKER
    if is_v2_book_plan(book_plan):
        system += (
            "\n核对不可变核心口径锁与本卷阶梯。同一角色在同一场域的锁定别名不能漂移，正常敬称可以使用。"
            "核对声称齐备或全员时的完整名单，局部携带或局部出场允许。"
            "边界以已生效状态为准；满足 changeWhen 且正文写出可观察结果时可合法变化，擅自扩大范围须报冲突。"
            "已确认的总纲冲突只在 issues 中用 world_rule_conflict、hard_value_conflict 或 core_scene_repetition，"
            "附可定位的本章原文 quote 与 planRef。别名、边界、集合分别用 canonLocks.identityAliases[i]、"
            "canonLocks.boundaries[i]、canonLocks.assetSets[i]；旧锁用其字段路径，分卷用 volumes[i] 或其字段路径。"
            "索引从0开始，第一卷为 volumes[0]。合法边界变更可另返回 "
            'boundaryChanges: [{"planRef":"canonLocks.boundaries[i]","result":"≤48字的新状态","quote":"4–120字的本章原文"}]。'
        )
    ending = _VERIFY_ENDING_BLOCK.substitute(
        ending=_text_excerpt(prev_chapter_ending, 600, from_end=True)
    ) if prev_chapter_ending else ""
    if input_budget is not None:
        review = review_context if review_context is not None else build_review_context(
            book_plan=book_plan, canonical_facts=canonical_facts, outline=outline, chapter_number=chapter_number,
            long_memory=long_memory, rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
            boundary_state=boundary_state, arc_plans=arc_plans,
        )
        system += "\nrecalledBeats 是与本章相近的较早桥段，核对是否无新后果地复演（core_scene_repetition）。"
        notes = _review_blocks(review, chapter_text)
        if claims := numeric_claims(chapter_text, review["story_ledger_view"], book_plan):
            notes += "\n【金额证据 numericClaims】\n" + "\n".join(claims)
        template = _compact_schema(
            _VERIFY_PROMPT, "输出 JSON（严格包含 issues、aiIssues、factsDelta）：\n",
            chapter_number=review["chapter_number"], memory_spec=_memory_delta_spec(long_memory, story_ledger),
            identity_spec=_IDENTITY_SPEC if story_ledger else "",
        )
        memory_instructions = _MEMORY_EXTRACTION_INSTRUCTIONS.template.split("已有活跃条目：", 1)[0] if long_memory else ""
        outline_json = _json(review["outline"])

        def render(parts):
            return {"system": system, "prompt": template.substitute(
                chapter_number=chapter_number, chapter_text=chapter_text, outline=outline_json,
                ending_block=ending, review_notes=notes, facts=_allocated_review(parts),
                memory_instructions=memory_instructions,
            )}

        return fit_prompt(render=render, slots=_review_slots(review, chapter_text), input_budget=input_budget,
                          on_overflow="expand")

    context = _chapter_context(chapter_text, outline)
    prompt = _VERIFY_PROMPT.substitute(
        chapter_number=chapter_number, chapter_text=chapter_text, outline=_json(outline or {}), ending_block=ending,
        facts=_json(extract_relevant_facts(canonical_facts, context_text=context, current_chapter=chapter_number,
                                          story_ledger=story_ledger), pretty=True),
        review_notes=_review_notes(book_plan, canonical_facts, context, chapter_number, long_memory,
                                   rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
                                   boundary_state=boundary_state),
        memory_instructions=_memory_extraction(canonical_facts, context) if long_memory else "",
        memory_spec=_memory_delta_spec(long_memory, story_ledger),
        identity_spec=_IDENTITY_SPEC if story_ledger else "",
    )
    return {"system": system, "prompt": prompt}


def build_repair_prompt(*, chapter_number, chapter_text, issues=None, ai_issues=None, outline=None,
                        canonical_facts=None, prev_chapter_ending="", pov=_DEFAULT_POV, target_words=2000,
                        chapter_ai_gate=False, book_plan=None, long_memory=False, rolling_arc_outlines=False,
                        story_ledger=False, boundary_state=(), input_budget=None, review_context=None, arc_plans=()):
    ending = _REPAIR_ENDING_BLOCK.substitute(
        ending=_text_excerpt(prev_chapter_ending, 700, from_end=True)
    ) if prev_chapter_ending else ""
    ai_fix = _REPAIR_AI_FIX_BLOCK.substitute(
        ai_issues=_json(ai_issues, pretty=input_budget is None),
    ) if _list(ai_issues) else ""
    style_instruction = (
        "按对应原句和修改建议逐项修正句式、套语或分段，保留人物行动和信息。"
        if chapter_ai_gate else
        "同步清理列出的 AI 味问题：删掉“写完动作又旁白解释”的多余句子，替换机械重复的身体反应套话，让不同角色对话贴合各自立场。"
    )
    system = _REPAIR_SYSTEM.substitute(
        pov=pov, target_words=target_words, style_instruction=style_instruction,
        hard_style_rule=' type="ai_flavor" 且 severity="error" 的项目同样必须修复。' if chapter_ai_gate else "",
    )
    if story_ledger:
        system += (_STORY_LEDGER_WRITE +
                   "若确认是不同人物撞名，给新增人物独立全名并同步本章相关称呼。"
                   "保留正常跨日、交易及有证据的人物转折。\n" + STORY_LEDGER_MARKER)
    if input_budget is not None:
        review = review_context if review_context is not None else build_review_context(
            book_plan=book_plan, canonical_facts=canonical_facts, outline=outline, chapter_number=chapter_number,
            long_memory=long_memory, rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
            boundary_state=boundary_state, arc_plans=arc_plans,
        )
        system += "\ntitle_mismatch 仅用于校正章名；正文修复以核实的情节和数值为准。"
        notes = _review_blocks(review, chapter_text, [issues, ai_issues])
        outline_json, issues_json = _json(review["outline"]), _json(issues or [])

        def render(parts):
            return {"system": system, "prompt": _REPAIR_PROMPT.substitute(
                chapter_number=chapter_number, target_words=target_words, chapter_text=chapter_text,
                outline=outline_json, ending_block=ending, issues=issues_json, ai_fix_block=ai_fix,
                facts=_allocated_review(parts), review_notes=notes,
            )}

        slots = _review_slots(review, chapter_text, repair=True, issues=_list(issues), ai_issues=_list(ai_issues))
        return fit_prompt(render=render, slots=slots, input_budget=input_budget, on_overflow="expand")

    context = _chapter_context(chapter_text, outline)
    prompt = _REPAIR_PROMPT.substitute(
        chapter_number=chapter_number, target_words=target_words, chapter_text=chapter_text,
        outline=_json(outline or {}, pretty=True), ending_block=ending, issues=_json(issues or [], pretty=True),
        ai_fix_block=ai_fix, facts=_json(extract_relevant_facts(canonical_facts, context_text=context,
                                                               current_chapter=chapter_number, story_ledger=story_ledger), pretty=True),
        review_notes=_review_notes(book_plan, canonical_facts, context, chapter_number, long_memory,
                                   rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
                                   boundary_state=boundary_state),
    )
    return {"system": system, "prompt": prompt}


def build_de_ai_prompt(*, chapter_text, pov=_DEFAULT_POV, style_guide=_DEFAULT_STYLE, meme_level="none"):
    preserve = (
        "\n4. 保留文中的网感吐槽、反差幽默与爽点金句，不要把有梗有个性的对话改写成干瘪正经腔。"
        if meme_level and meme_level != "none" else ""
    )
    return {
        "system": _DE_AI_SYSTEM.substitute(pov=pov, style_guide=style_guide, preserve_meme_note=preserve),
        "prompt": _DE_AI_PROMPT.substitute(chapter_text=chapter_text),
    }


def build_full_book_audit_prompt(*, book_plan=None, chapters_summary=None, chapter_evidence=None,
                                 word_stats=None, canonical_facts=None, rolling_arc_outlines=False,
                                 plan_evidence=(), boundary_state=()):
    facts = canonical_facts or {}
    timeline = _list(facts.get("timeline"))
    compact_facts = {
        "characters": _dict(facts.get("characters")), "injuries": _list(facts.get("injuries")),
        "foreshadowing": _list(facts.get("foreshadowing")), "items": dict(list(_dict(facts.get("items")).items())[-30:]),
        "secrets": _list(facts.get("secrets"))[-25:], "timelineMilestones": [*timeline[:5], *timeline[-10:]],
    }
    plan = _plan_for_outlines_and_audit(book_plan, rolling_arc_outlines=rolling_arc_outlines) or {}
    plan_text = _json(plan, pretty=True)
    if is_v2_book_plan(book_plan):
        if locks := canon_locks_text(book_plan.get("canonLocks"), boundary_state=boundary_state):
            plan_text += "\n" + locks
        plan_text += "\n【全书分卷阶梯】\n" + volume_ladder_text(book_plan)
        if plan_evidence:
            plan_text += "\n【总纲口径对应的正文证据】\n" + _json(list(plan_evidence), pretty=True)
        plan_text += "\n按证据章号和边界生效章号核对口径，检查各卷压迫、代价、退役手段及跃迁条件。"
    prompt = _FULL_BOOK_AUDIT_PROMPT.substitute(
        book_plan=plan_text,
        word_stats=_json(word_stats or {}, pretty=True), chapters_summary=_json(chapters_summary or [], pretty=True),
        chapter_evidence=_json(chapter_evidence or [], pretty=True), facts=_json(compact_facts, pretty=True),
    )
    return {"system": _FULL_BOOK_AUDIT_SYSTEM, "prompt": prompt}
