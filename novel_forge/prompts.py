"""Prompt builders for writing, reviewing and archiving chapters."""

import json
import math
import os
import re
from string import Template

from .draft_quality import STYLE_BIBLE_MARKER, compact_style_bible
from .long_memory import active_memory_items, protagonist_name, relevant_memory_characters
from .prompt_budget import read_chapter_input_budget, fit_chapter_prompt

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
    { "volume": 1, "title": "第一卷名", "chapters": [1, 5], "summary": "本卷核心推进" }
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
3. 严格输出 JSON 数组，不要输出多余解释。"""

_CHAPTER_OUTLINES_PROMPT = Template("""请生成连续章纲：
起始章节：${start_chapter}
结束章节：${end_chapter}${total_block}
单章目标字数：${target_words}
【全书总纲】
${book_plan}${topic_block}${prev_context}${facts_block}${direction_block}${finale_plan_block}

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

_CHAPTER_WRITE_PROMPT = Template("""请撰写第${chapter_number}${total_suffix}章正文：
书名：${book_title}
叙事视角：${pov}
本章目标字数：${target_words}${book_anchors}${finale}
本章标题：${chapter_title}
本章章纲：${outline}${style_block}${summary_block}${ending_block}
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

_FACT_EXTRACTION_SYSTEM = """[TASK:FACT_EXTRACTION]
你是严谨的小说连续性档案员。请从刚生成的章节正文中提取所有关键事实增量（人物状态变化、伤势、时间线、物品位置/归属、秘密知情边界、伏笔埋设/强化/回收）。
必须严格输出合法 JSON。"""

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
    { "character": "角色名", "description": "伤势描述", "severity": "high|medium|low", "active": true }
  ],
  "timeline": [
    { "dayOrTime": "时间点", "event": "发生了什么", "characters": ["相关角色"] }
  ],
  "items": {
    "物品名": { "name": "物品名", "owner": "持有者", "location": "存放位置", "status": "intact" }
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
   ${instruction}""")

_VERIFY_SYSTEM = Template("""[TASK:VERIFY]
你是严谨但懂网文叙事规律的小说连续性与质量审校官兼档案员。请在本次调用中**一次性**完成两件事：
A. 逐项核查本章正文是否存在**真正的硬性逻辑/设定冲突与重度 AI 味**：
1. 伤势一致性（前文未愈合的伤势不能无故正常使用受伤部位）。
2. 物品位置与归属一致性（注意：本章主角合理取用/转移物品导致的变化，将在本章通过后才入库，不得以“事实库尚未同步本章新动作”判 error）。
3. 秘密知情边界：只有当不知情角色（excludedCharacters）在**无任何告知/目击/推理来源**下凭空使用秘密，才判 secret_leak error；角色在本章**主动摊牌揭露**或**目击当前可疑行为（如在配电间徘徊）**属于合理剧情推进，不是 error。
4. 时间线、门牌/编号、金额/扩容规则、空间移动等跨章设定硬冲突（必须顺承上一章末尾衔接；**若规范事实库与旧章纲数值/时间冲突，以规范事实库和上章末尾为准，顺承事实而偏离旧章纲不是错误**）。
5. 叙事视角（${pov}）是否严重越界（如直接透视他人的秘密内心；通过对话转述他人见闻仅作轻微 warning，不判 error）。
6. 是否包含未完成占位符。${ai_instruction}
注：精确字数由系统程序校验，请勿因“后续章节建议”将 word_count 标为 error。${severity_rule}${memory_rule}

B. 同步从本章正文中抽取**候选规范事实增量 (factsDelta)**（仅作候选提取；若未通过审校系统不会入库）：包含 characters、injuries、timeline、items、secrets、foreshadowing。

必须严格输出合法 JSON。""")

_VERIFY_PROMPT = Template("""请一次性完成第${chapter_number}章的审校、AI味问题定位与事实增量抽取：
【章纲】${outline}${ending_block}
【相关规范事实库（含 sourceChapter 溯源）】
${facts}${memory_instructions}

【待审校正文】
${chapter_text}

输出 JSON（严格包含 issues、aiIssues、factsDelta）：
{
  "passed": true,
  "chapterNumber": ${chapter_number},
  "issues": [
    { "type": "injury_contradiction|item_mismatch|secret_leak|pov_violation|placeholder|timeline|inconsistent_details|ai_flavor", "severity": "error|warning", "message": "具体问题说明", "quote": "原文原句证据", "sourceChapter": 1 }
  ],
  "aiIssues": [
    { "category": "over_explaining|formulaic_reaction|shared_voice|static_situation|repetitive_hooks|inconsistent_details", "quote": "原文原句", "impact": "为什么影响阅读", "priority": "P0|P1|P2", "suggestion": "删改建议" }
  ],
  "factsDelta": {
    "characters": { "角色名": { "name": "角色名", "status": "active", "location": "当前地点", "traits": [] } },
    "injuries": [{ "character": "角色名", "description": "伤势描述", "severity": "high|medium|low", "active": true }],
    "timeline": [{ "dayOrTime": "时间点", "event": "关键事件", "characters": ["角色"] }],
    "items": { "物品名": { "name": "物品名", "owner": "持有者", "location": "位置", "status": "intact" } },
    "secrets": [{ "secret": "秘密内容", "holders": ["知情人"], "excludedCharacters": ["不知情人"] }],
    "foreshadowing": [{ "id": "fs_id", "clue": "伏笔线索", "status": "planted|reinforced|paid_off", "details": "进展" }]${memory_spec}
  },
  "summary": "审校总结"
}""")

_REPAIR_ENDING_BLOCK = Template("""
【上一章末尾衔接片段（必须无缝紧接此场景与时间点）】
${ending}""")

_REPAIR_AI_FIX_BLOCK = Template("""
【一并清理的 AI 味/重复问题】
${ai_issues}""")

_REPAIR_SYSTEM = Template("""[TASK:REPAIR]
你是专业小说精修编辑。请根据审校指出的问题列表，对第${chapter_number}章正文进行一次性定向修复（同时修正设定/因果冲突与指定 AI 味问题）。
要求：
1. 彻底解决所有 severity="error" 的一致性、门牌/金额/时间线、占位符或视角问题。${hard_style_rule}
2. ${style_instruction}
3. 遵循优先级：**相关规范事实库（Relevant Facts）与上一章末尾衔接 > 早先章纲**；若章纲数值低于事实库已记录数值，保持与事实库一致。
4. 保留原有的核心剧情推进与爽点节奏，严格遵守${pov}视角，保持目标字数在 ${target_words} 字左右。
5. 直接输出修复后的完整正文，不要输出解释。""")

_REPAIR_PROMPT = Template("""请修复第${chapter_number}章正文中的以下问题：
目标字数：${target_words}
【本章章纲】
${outline}${ending_block}

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

_MEMORY_DELTA_JSON = """,
    "memory": {
      "characterStates": { "角色名": { "seen": true, "currentGoal": "当前目标", "emotion": "情绪", "stance": "立场", "relationships": { "另一角色": "本章关系变化" }, "quote": "原文" } },
      "constraints": [{ "id": "已有条目更新时填写", "text": "硬规则", "characters": ["角色"], "status": "active|fulfilled|expired", "quote": "原文" }],
      "commitments": [{ "text": "承诺或倒计时", "deadline": "剧情时间", "status": "active|fulfilled|expired", "quote": "原文" }],
      "subplots": [{ "text": "支线进展", "status": "active|resolved", "quote": "原文" }]
    }"""

_MEMORY_EXTRACTION_INSTRUCTIONS = Template("""
可选 memory 只记录本章明确确立或改变的角色目标、立场关系、硬规则、期限和支线；仅出现可写 seen。每条必须带本章逐字 quote（4–100 字），只填有变化的字段，无变化可省略 memory。更新、兑现或完结沿用已有 id，新项省略 id。已有活跃条目：${active}
""")

_VOLUME_SUMMARY_PROMPT = Template("""给第${start_chapter}–${end_chapter}章归档。mainline 不超过 360 字。turningPoints 最多 4 条，每条包含 chapter（整数）和 text（不超过 90 字）。
${evidence}""")


_DEFAULT_POV = "第三人称限知"
_DEFAULT_STYLE = STYLE_PRESETS["番茄爽文"]
_SPACE = r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_TERMS = re.compile(rf"[{_SPACE},，、：:；;()（）]+")
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+\Z")
_DRAFT_RULE = "\n8. 动作已表情绪就收笔。少用套话、心理引导词和破折号，变换句首、按节奏分段，让角色的用词贴合各自立场。"
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


def _meme_guidance(level, samples):
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
    return f"\n【网感与玩梗风格要求（{preset['label']}）】\n{preset['instruction']}{examples}"


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


def extract_relevant_facts(canonical_facts=None, *, context_text="", max_items=28, max_secrets=22, max_foreshadowing=22):
    facts = canonical_facts or {}
    injuries = [entry for entry in _records(facts.get("injuries")) if entry.get("active") is not False]
    open_clues = [entry for entry in _records(facts.get("foreshadowing")) if entry.get("status") != "paid_off"]
    foreshadowing = _pick_relevant(
        open_clues, context_text, max_foreshadowing,
        lambda entry: " ".join(str(entry.get(key) or "") for key in ("id", "clue", "details")),
    )
    characters = {}
    for name, entry in _dict(facts.get("characters")).items():
        entry = _dict(entry)
        characters[name] = {
            "name": entry.get("name") or name, "status": entry.get("status") or "active",
            "location": entry.get("location") or "", "role": entry.get("role") or "",
            **({"sourceChapter": entry["sourceChapter"]} if "sourceChapter" in entry else {}),
        }

    items = _dict(facts.get("items"))
    if len(items) > max_items:
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
        "openForeshadowing": foreshadowing, "recentTimeline": _list(facts.get("timeline"))[-5:],
    }


def _plan_for_outlines_and_audit(plan):
    return {key: value for key, value in plan.items() if key != "styleBible"} if isinstance(plan, dict) else plan


def _topic_anchors(topic):
    return {"conflict": topic.get("coreConflict") or "", "motivation": topic.get("protagonistMotivation") or "",
            "golden_chapters": "\n  ".join(_list(topic.get("goldenThreeChapters")))}


def build_book_plan_prompt(*, title="", premise="", genre="都市重生爽文", pov=_DEFAULT_POV, target_words=30000,
                           total_chapters=15, hotspot_context=None, topic_package=None, meme_level="none",
                           meme_samples=None, draft_quality=False):
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
    system = _BOOK_PLAN_SYSTEM.substitute(style_request=request)
    prompt = _BOOK_PLAN_PROMPT.substitute(
        title=title or topic.get("title") or "请根据核心梗拟定一个不超过14字的商业爆款书名",
        premise=premise or topic.get("premise") or "主角重生归来，凭借前世记忆破局逆袭，揭开惊天阴谋。",
        genre=genre, pov=pov, pov_rule=_pov_rule(pov), target_words=target_words, total_chapters=total_chapters,
        topic_block=topic_block, hotspot_block=hotspot_block, meme_block=_meme_guidance(meme_level, meme_samples),
        style_spec=_BOOK_PLAN_STYLE_BIBLE_SPEC if draft_quality else "",
    )
    return {"system": system, "prompt": prompt}


def build_chapter_outlines_prompt(*, book_plan=None, start_chapter=1, end_chapter=10, total_chapters=None,
                                  existing_outlines=None, target_words_per_chapter=2000, topic_package=None,
                                  relevant_facts=None, continuation_direction="", finalize_book=False):
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
    prompt = _CHAPTER_OUTLINES_PROMPT.substitute(
        start_chapter=start_chapter, end_chapter=end_chapter,
        total_block=f"\n全书总章数：{total_chapters}" if total_chapters else "",
        target_words=target_words_per_chapter, book_plan=_json(_plan_for_outlines_and_audit(book_plan), pretty=True),
        topic_block=topic_block, prev_context=prev_context, facts_block=facts, direction_block=direction,
        finale_plan_block=finale,
    )
    return {"system": _CHAPTER_OUTLINES_SYSTEM, "prompt": prompt}


def _chapter_write_facts(canonical, relevant, context):
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
        return sorted(entries, key=lambda item: (item["relevance"], item["sourceChapter"]), reverse=True)

    injuries = [tag("activeInjuries", item, [item.get("character")])
                for item in _records(source("injuries", "activeInjuries")) if item.get("active") is not False]
    secrets = [
        tag("secrets", entry, [entry.get("secret"), *_list(entry.get("holders")), *_list(entry.get("excludedCharacters"))])
        for entry in _records(source("secrets", "secrets"))
    ]
    items = [
        tag("items", entry, [key, *(_dict(entry).get(field) for field in ("name", "owner", "holder", "location"))], key)
        for key, entry in _dict(source("items", "items")).items()
    ]
    timeline = [
        tag("recentTimeline", entry, [entry.get("event"), *_list(entry.get("characters"))], index)
        for index, entry in enumerate(_records(source("timeline", "recentTimeline"))[-5:])
    ]
    characters = rank(tag("characters", entry, [key, _dict(entry).get("name")], key)
                      for key, entry in _dict(relevant.get("characters")).items())
    clues = rank(tag("openForeshadowing", entry, [entry.get("id"), entry.get("clue")])
                 for entry in _records(relevant.get("openForeshadowing")))
    return rank([*injuries, *secrets, *items, *timeline]), characters, [entry["value"] for entry in clues]


def _chapter_anchors(book_plan, lead, include_core_rules):
    anchors = []
    worldview = _dict(book_plan.get("worldview"))
    if book_plan.get("logline"):
        anchors.append(f"主线卖点：{book_plan['logline']}")
    if worldview.get("setting"):
        anchors.append(f"舞台世界观：{worldview['setting']}")
    cast = _records(book_plan.get("mainCharacters"))
    if lead:
        cast = sorted(cast, key=lambda entry: entry.get("name") == lead, reverse=True)
    if cast:
        names = "、".join(f"{entry.get('name') or ''}({entry.get('role') or ''})" for entry in cast[:6])
        anchors.append(f"核心人物：{names}")
    if include_core_rules:
        anchors.extend(f"世界观硬规则：{rule}" for rule in _list(worldview.get("coreRules"))
                       if isinstance(rule, str) and _EDGE_SPACE.sub("", rule))
    return _CHAPTER_WRITE_BOOK_ANCHOR_BLOCK.substitute(anchors="\n".join(anchors)) if anchors else ""


def _memory_slots(canonical, relevant, context, outline, lead, chapter_number, recent_summaries):
    hard_items = [
        {"kind": kind, "value": value}
        for kind in ("constraints", "commitments")
        for value in active_memory_items(canonical, kind, context)
    ]
    current = _number(chapter_number)
    archives = sorted((item for item in _records(canonical.get("volumeSummaries"))
                       if item.get("endChapter") is not None and _number(item["endChapter"]) < current),
                      key=lambda item: _number(item["endChapter"]), reverse=True)[:2]
    last_archive = _number(archives[0]["endChapter"]) if archives else 0
    summaries = [entry for entry in _records(recent_summaries)
                 if last_archive < _number(entry.get("chapterNumber")) < current and entry.get("summary")]
    rolling = "\n".join(
        f"第{entry['chapterNumber']}章{'（待复核）' if entry.get('needsReview') else ''}：{_text_excerpt(entry['summary'], 260)}"
        for entry in summaries[-5:]
    )
    base = canonical.get("characters")
    characters = relevant_memory_characters(
        canonical, relevant.get("characters") if base is None else base, lead, f"{context} {_json(outline)}",
    )
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
    if use_long_memory:
        blocks = {
            "constraints": [item["value"] for item in parts["hardMemory"] if item["kind"] == "constraints"],
            "commitments": [item["value"] for item in parts["hardMemory"] if item["kind"] == "commitments"],
            "volumeSummaries": parts["archives"],
            "subplots": parts["subplots"],
            "recentPayoffs": [entry["value"] for entry in clues if entry["paid"]],
        }
        memory = {key: value for key, value in blocks.items() if value}
        if memory:
            facts["longMemory"] = memory
    return facts


def build_chapter_write_prompt(*, book_plan=None, outline=None, prev_chapter_ending="", recent_summaries=None,
                               prev_chapter_summary="", relevant_facts=None, canonical_facts=None,
                               pov=_DEFAULT_POV, target_words=2000, total_chapters=None, style_guide=_DEFAULT_STYLE,
                               meme_level="none", meme_samples=None, input_budget=None, draft_quality=False,
                               long_memory=False):
    book_plan, outline, canonical = _dict(book_plan), _dict(outline), _dict(canonical_facts)
    chapter_number = outline.get("chapterNumber") or 1
    use_long_memory = long_memory and input_budget is not None
    lead = protagonist_name(book_plan.get("mainCharacters")) if use_long_memory else None
    relevant = extract_relevant_facts(canonical) if relevant_facts is None else relevant_facts
    summaries = _list(recent_summaries)
    summary_text = "\n".join(
        entry if isinstance(entry, str) else f"第{_dict(entry).get('chapterNumber', 'undefined')}章：{_dict(entry).get('summary', 'undefined')}"
        for entry in summaries[-2:]
    )
    anchors = _chapter_anchors(book_plan, lead, input_budget is not None)
    finale = _CHAPTER_WRITE_FINALE_BLOCK.substitute(
        chapter_number=chapter_number, total_chapters=total_chapters,
    ) if total_chapters and _number(chapter_number) >= _number(total_chapters) else ""
    style_lines = compact_style_bible(book_plan.get("styleBible")) if draft_quality else []
    draft_rule = _DRAFT_RULE if draft_quality else ""
    pretty_outline = _json(outline, pretty=True)

    def render(ending, summary, facts, samples, bible):
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
            meme_block=_meme_guidance(meme_level, samples), draft_rule=draft_rule,
        )
        prompt = _CHAPTER_WRITE_PROMPT.substitute(
            chapter_number=chapter_number, total_suffix=f"/{total_chapters}" if total_chapters else "",
            book_title=book_plan.get("title") or "未命名", pov=pov, target_words=target_words,
            book_anchors=anchors, finale=finale, chapter_title=outline.get("title") or f"第{chapter_number}章",
            outline=pretty_outline, style_block=style_block, summary_block=summary_block, ending_block=ending_block,
            facts=_json(facts, pretty=True),
        )
        return {"system": system, "prompt": prompt}

    if input_budget is None:
        snippet = (
            _text_excerpt(prev_chapter_ending, 800, from_end=True) if prev_chapter_ending
            else prev_chapter_summary or "（本章为开篇或紧接总纲）"
        )
        return render(snippet, summary_text, relevant, meme_samples, style_lines)

    ending = _EDGE_SPACE.sub("", str(prev_chapter_ending or prev_chapter_summary or ""))
    context = " ".join(str(value) if value is not None else "" for value in (
        outline.get("title"), outline.get("summary"), *_list(outline.get("keyEvents")), _text_excerpt(ending, 300, from_end=True),
    ))
    continuity, characters, clues = _chapter_write_facts(canonical, relevant, context)
    has_style = bool(style_lines)
    style_slot = [{"key": "styleBible", "entries": style_lines, "share": 8}] if has_style else []
    samples = _list(meme_samples) if meme_level != "none" else []
    if use_long_memory:
        hard_items, archives, rolling, characters, fallback, payoffs = _memory_slots(
            canonical, relevant, context, outline, lead, chapter_number, summaries,
        )
        slots = [
            {"key": "hardMemory", "entries": hard_items, "share": 20},
            {"key": "continuity", "entries": continuity, "share": 30 if has_style else 32},
            {"key": "archives", "entries": archives, "share": 10},
            *style_slot,
            {"key": "ending", "text": ending, "from_end": True, "share": 12 if has_style else 15},
            {"key": "summary", "text": rolling, "share": 6},
            {"key": "characters", "entries": characters, "allocate_first": bool(lead),
             "first_fallback": fallback, "share": 7 if has_style else 10},
            {"key": "subplots", "entries": active_memory_items(canonical, "subplots", context), "share": 3},
            {"key": "foreshadowing", "entries": [{"value": value, "paid": False} for value in clues]
             + [{"value": value, "paid": True} for value in payoffs], "share": 3},
            {"key": "samples", "entries": samples, "share": 1},
        ]
    else:
        slots = [
            {"key": "continuity", "entries": continuity, "share": 50},
            *style_slot,
            {"key": "ending", "text": ending, "from_end": True, "share": 20},
            {"key": "summary", "text": summary_text, "share": 9 if has_style else 12},
            {"key": "characters", "entries": characters, "share": 7 if has_style else 10},
            {"key": "foreshadowing", "entries": clues, "share": 4 if has_style else 6},
            {"key": "samples", "entries": samples, "share": 2},
        ]

    def render_budgeted(parts):
        return render(parts["ending"], parts["summary"], _render_allocated_facts(parts, use_long_memory),
                      parts["samples"], parts.get("styleBible", []))

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


def _memory_extraction(facts, chapter_text):
    active = {
        kind: [{key: entry[key] for key in ("id", "text", "deadline") if key in entry}
               for entry in active_memory_items(facts, kind, chapter_text, 6)]
        for kind in ("constraints", "commitments", "subplots")
    }
    return _MEMORY_EXTRACTION_INSTRUCTIONS.substitute(active=_json(active))


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


def build_fact_extraction_prompt(*, chapter_number, chapter_text, existing_facts=None, canonical_facts=None,
                                 long_memory=False):
    effective_facts = existing_facts if canonical_facts is None else canonical_facts
    prompt = _FACT_EXTRACTION_PROMPT.substitute(
        chapter_number=chapter_number, chapter_text=chapter_text,
        facts=_json(extract_relevant_facts(existing_facts), pretty=True),
        memory_instructions=_memory_extraction(effective_facts, chapter_text) if long_memory else "",
        memory_spec=_MEMORY_DELTA_JSON if long_memory else "",
    )
    return {"system": _FACT_EXTRACTION_SYSTEM, "prompt": prompt}


def build_verify_prompt(*, chapter_number, chapter_text, outline=None, canonical_facts=None, prev_chapter_ending="",
                        pov=_DEFAULT_POV, min_words=500, max_words=5000, check_ai_flavor=True,
                        chapter_ai_gate=False, long_memory=False):
    # As in Node, exact word counts are checked locally and are not sent to the reviewer.
    ai_instruction = ""
    if check_ai_flavor:
        instruction = (
            "只用 aiIssues 报告 AI 味，避免在 issues 重复记录。P0 是严重损害全章可读性的问题，P1 是必须当章解决的明显重复、"
            "叙事停滞或角色声音混同，P2 是局部措辞建议。P0/P1 必须给出可在本章定位的原文 quote 和具体修法，并令 passed=false。P2 不阻断。"
            if chapter_ai_gate else
            "说明：轻微 AI 味放 aiIssues（priority=P2/P1，issues 设为 warning）；仅当本章大段严重注水解释或原地打转影响可读性时，才计入阻塞修复。"
        )
        ai_instruction = _VERIFY_AI_FLAVOR_INSTRUCTION.substitute(instruction=instruction)
    severity = (
        "硬性逻辑或设定冲突在 issues 中标 error；有原文的 P0/P1 放 aiIssues。任一存在则 passed=false，轻微瑕疵或后续建议只记告警。"
        if chapter_ai_gate else
        '只有存在真实不可调和的硬冲突时才设 severity="error" 且 passed=false；轻微瑕疵或后续建议一律设为 severity="warning" 且 passed=true。'
    )
    system = _VERIFY_SYSTEM.substitute(
        pov=pov, ai_instruction=ai_instruction, severity_rule=severity,
        memory_rule="\n若有活跃硬规则与期限，也核对本章是否延续；有原文支撑的正常状态变化应更新记忆。" if long_memory else "",
    )
    ending = _VERIFY_ENDING_BLOCK.substitute(
        ending=_text_excerpt(prev_chapter_ending, 600, from_end=True)
    ) if prev_chapter_ending else ""
    prompt = _VERIFY_PROMPT.substitute(
        chapter_number=chapter_number, chapter_text=chapter_text, outline=_json(outline or {}), ending_block=ending,
        facts=_json(extract_relevant_facts(canonical_facts), pretty=True),
        memory_instructions=_memory_extraction(canonical_facts, chapter_text) if long_memory else "",
        memory_spec=_MEMORY_DELTA_JSON if long_memory else "",
    )
    return {"system": system, "prompt": prompt}


def build_repair_prompt(*, chapter_number, chapter_text, issues=None, ai_issues=None, outline=None,
                        canonical_facts=None, prev_chapter_ending="", pov=_DEFAULT_POV, target_words=2000,
                        chapter_ai_gate=False):
    ending = _REPAIR_ENDING_BLOCK.substitute(
        ending=_text_excerpt(prev_chapter_ending, 700, from_end=True)
    ) if prev_chapter_ending else ""
    ai_fix = _REPAIR_AI_FIX_BLOCK.substitute(ai_issues=_json(ai_issues, pretty=True)) if _list(ai_issues) else ""
    style_instruction = (
        "按对应原句和修改建议逐项修正句式、套语或分段，保留人物行动和信息。"
        if chapter_ai_gate else
        "同步清理列出的 AI 味问题：删掉“写完动作又旁白解释”的多余句子，替换机械重复的身体反应套话，让不同角色对话贴合各自立场。"
    )
    system = _REPAIR_SYSTEM.substitute(
        chapter_number=chapter_number, pov=pov, target_words=target_words, style_instruction=style_instruction,
        hard_style_rule=' type="ai_flavor" 且 severity="error" 的项目同样必须修复。' if chapter_ai_gate else "",
    )
    prompt = _REPAIR_PROMPT.substitute(
        chapter_number=chapter_number, target_words=target_words, chapter_text=chapter_text,
        outline=_json(outline or {}, pretty=True), ending_block=ending, issues=_json(issues or [], pretty=True),
        ai_fix_block=ai_fix, facts=_json(extract_relevant_facts(canonical_facts), pretty=True),
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
                                 word_stats=None, canonical_facts=None):
    facts = canonical_facts or {}
    timeline = _list(facts.get("timeline"))
    compact_facts = {
        "characters": _dict(facts.get("characters")), "injuries": _list(facts.get("injuries")),
        "foreshadowing": _list(facts.get("foreshadowing")), "items": dict(list(_dict(facts.get("items")).items())[-30:]),
        "secrets": _list(facts.get("secrets"))[-25:], "timelineMilestones": [*timeline[:5], *timeline[-10:]],
    }
    prompt = _FULL_BOOK_AUDIT_PROMPT.substitute(
        book_plan=_json(_plan_for_outlines_and_audit(book_plan) or {}, pretty=True),
        word_stats=_json(word_stats or {}, pretty=True), chapters_summary=_json(chapters_summary or [], pretty=True),
        chapter_evidence=_json(chapter_evidence or [], pretty=True), facts=_json(compact_facts, pretty=True),
    )
    return {"system": _FULL_BOOK_AUDIT_SYSTEM, "prompt": prompt}
