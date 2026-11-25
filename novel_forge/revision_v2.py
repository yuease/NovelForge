"""Active revision workflow and access to historical exports."""

import asyncio
import logging
import os
import re
from time import perf_counter
from typing import NotRequired, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .candidate_rounds import ROUND_KIND
from .llm import DEFAULT_MAX_TOKENS
from .manuscript_tools import readonly_tool_specs, run_readonly_tool
from .openai_siwc import PROVIDER as SIWC, all_stages as siwc_all_stages
from .providers import PROVIDERS
from .revision import (
    AUDIT, LEDGER, VERIFIER, RevisionRuntime, reviewed_findings, revision_fallback_enabled,
    scoped_issues, surroundings, text_sections,
)
from .revision_store import timestamp
from .revision_checks import (
    COMMON_PHRASES, RevisionContentError, audited_evidence, broken_ranges, chapter_number, compact_json, fingerprint, partition,
    phrase_counts, required_list, risk, string, validate_audit, windows,
)
from .revision_v2_checks import (
    MAX_DEFERRED_IDS, aggregate_chapter_checks, assessed_issues, chapter_checks, digest, distinct_findings,
    edit_patches, exact_passage, financial_text,
    hard_review, ledger_entry_id, located_evidence, merge_semantic_delta, metrics, preflight, protected_ledger, recheck_repairs,
    related_chapters, remaining_findings, repair_feedback, scope, validated_decision_batch, validated_ledger_core,
    validated_plan, verified_repair,
)
from .workflow import WorkerError

log = logging.getLogger(__name__)

DECISION_GUIDANCE = """
每个顶层 issues 中的意见（含全局计划）明确裁决，不漏编号。
decisions:[{"id":"R001","decision":"adopt|adapt|skip|author","reason":"采用理由、实际改法或略过依据",
"chapters":[1],"evidence":[{"chapter":1,"quote":"连续原文"}],"searchTerms":[],"wholeBook":false,
"impact":"plot|continuity|finance","options":["关键口径一","关键口径二"]}]。
reason 用一两句说清依据或改法。skip 可省略空的 searchTerms 和 options。
adopt/adapt 需要有效章节和引文。duplicate 必须 skip。引用缺失的意见可查证后改编，明确无依据则略过。
区分个别章的意见与全书统一事项。对全书统一，searchTerms 提供原稿里可辨认的旧写法及实际变体；
工作台检索标题和正文并补齐候选章号。命中仅表示应核查，改不改取决于角色、对象、时间和上下文。
语义上覆盖全书且没有可用词面索引时用 wholeBook=true。chapters 可另外列出需要联动的章。
issueEvidence 给出了各评估意见在原稿中定位到的真实摘录和上下文。有对应片段时优先返回
{"sourceId":"R001-Q1"}，省略重复引文。只有找不到对应编号时才填写正确章号、字段和必要的连续原文。
sourceId 指向本轮原稿中已核实的片段。如果同一段真实原文支持多个裁决，可以在这些裁决中复用编号。
证据与意见存在偏差时，在裁决理由写明实际位置和可修改的部分，必要时补另一章证据。
评估的全局改稿计划（P 编号）是多处改动的描述，不能当作原文引用。逐个子项核对 issueEvidence 中
的候选片段，匹配的章分别引用自己的 {"sourceId":"P001-Q1"} 等编号。不要拼接不同章节的文字，
不要为了填满证据而使用不相关的候选。chapters 包含所有需要联动的章。
quoteMatch=partial 表示评估引文使用了省略或混入其他内容。核实 issueEvidence 的语境后再裁决；
引用原稿摘录或证据编号，评估报告里的省略号和转述不作为引文证据。
只有证据不足且选择关系主线、核心设定或关键账目才用 author；已有作者口径须落实在新规则和裁决中。
所有关键分歧写入对应 decisions 项的 author 选项。
审计补查发现的改稿意见也须裁决。
"""

BLUEPRINT_GUIDANCE = """
台账规则可优先引用 issueEvidence 的 sourceId，也可给原稿章号与连续引文。财务流水填写章号和连续引文。
只有确定某段短语在任何相关上下文都违背已确认口径时，才在 checks 里设 absent；
用空 chapters 执行全书检索，允许例外章。正常称呼、合法的其他角色编号和情境差异交给审校判断。
blueprint:[{"kind":"plot|voice","chapters":[1,2],"preserve":[1],"joins":[3],"change":"区别冲突后果、衔接与保护约束"}]。
节奏压缩先识别要达成的目标、重复动作与重复解释，给各章明确的保留和合并方案。
区分冗余信息与影响人物选择、因果、代价或悬念的精确信息，把必需信息写进蓝图。
不适用的列表返回空数组。旧台账存在时保留无关规则；作者变更优先，移除与其冲突的旧检查。
"""

PLAN = LEDGER + """
另外必须输出 decisions 和 blueprint。unresolved 固定返回空数组，skip 无证据时返回 evidence:[]。
""" + DECISION_GUIDANCE + BLUEPRINT_GUIDANCE

CORE_PLAN = LEDGER + BLUEPRINT_GUIDANCE + """
这一阶段只返回 rules、checks、financialFlows、blueprint 四个字段，均为数组。省略 unresolved 和 decisions，逐条裁决留给下一阶段。
优先记录有实质跨章影响的口径，保留有证据的关键单章规则。同一事实只写一次，规则和流水仅引用能证明口径的最短连续原文。
证据不足且必须确认的关键分歧先不写成确定规则，交给后续裁决。可见回复以几千 tokens 为目标，保留必要流水和证据。
"""

BATCH_PLAN = DECISION_GUIDANCE + """
仅返回 {"decisions":[...]}，按本批 issues 的顺序逐条裁决。按冻结的 coreLedger 执行章间统一口径。
有旧裁决时保留未受输入变化影响的内容。已有作者答案和 requestedChange 优先于冲突的旧裁决。
遇到证据不足且会改变核心口径的分歧，返回对应条目的 author 选项，切勿自行另立全书口径。
skip 可省略为空的 evidence、searchTerms 和 options。无关字段省略。
"""

SEMANTIC_DELTA_PLAN = DECISION_GUIDANCE + """
只为本次新增联动意见生成 {"rules":[],"checks":[],"decisions":[]}。严格限于 affectedChapters。
沿用旧台账口径。只加确实需要的局部规则、核验和本批裁决，无变化的规则与检查不必复述。
rules 项使用 track、decision、chapters、evidence。evidence 可用 issueEvidence 的 sourceId 或原稿连续引文。
checks 项使用 text、mode（present 或 absent）、chapters、allowedChapters，仅用于确定可核验的措辞。
确需替换完全位于本次影响范围的旧规则或检查时，写 replaces 指向 oldScope 中的原条目 id。
不重算旧流水，不重写旧蓝图与裁决。重大歧义按 author 返回，已有作者答案时按答案裁决。
"""

LEDGER_BATCH_SIZE = 25
LEDGER_PLANNING_SCHEMA = 1


def ledger_batch_threshold():
    try:
        value = int(os.getenv("NOVEL_FORGE_LEDGER_BATCH_THRESHOLD", "30"))
    except ValueError:
        return 30
    return value if value >= 0 else 30


PATCH = """
你是分卷编辑。仅更改 editableChapters 中的指定章节。按台账、剧情蓝图和裁决落实，保护人设、伏笔和文风。
所有锚点基于输入里的同一版本。短替换用 mode=exact 及 before；长段落用 mode=range 及 startAnchor、endAnchor。
长区间首尾分别唯一、按先后顺序，替换包含两端，不能跨章或跨字段。补丁不能重叠。
返回 {"patches":[{"chapter":1,"field":"title|content","mode":"exact|range","before":"短原文",
"startAnchor":"唯一开头","endAnchor":"唯一结尾","after":"替换稿","issueIds":["R001"],"reason":"改法"}],
"coverage":[{"id":"R001","outcome":"patched|already_met|needs_review","reason":"依据","chapter":1,"quote":"连续原文"}]}。
每条 requiredIssues 都给出 coverage。patched 需要有对应补丁；already_met 需要可核验原文。
patches[].issueIds 与 coverage[].id 仅填写本批 requiredIssues 中的编号；仅对应 feedback/repair（如 RF-xxx）或 ledger.rules（如 L-xxx）的补丁，issueIds 留空数组 []。
判断无需改正文但无法引用具体原句时，使用 needs_review 并说明原因，交给独立终审核对。
ledger.issues 中带章号的编号仅用于该章。全书统一项命中时先读该章语境，合规用法可以保留并解释。
需要压缩节奏时，优先合并重复的动作、场面或解释。保留读者理解人物选择、后果与风险所需的精确信息。
如需连带改动，由你结合当前章和台账判断如何保留信息。篇幅变短本身不能证明节奏变好。
逐条处理反馈。含 repair 的反馈只改 target 所指章节的相关文字，以 basis 为依据；
affectedChapters 中的其他章供审校复核。有疑义保留原文。没有修改可以返回空 patches，但 coverage 仍必须完整。
"""

REPAIR_REVIEW = """
返修资格与问题类型无关。允许自动处理时，给出明确、可局部执行的 fix，
并为该 finding 增加 field（title 或 content）、requiresDecision=false、affectedChapters 和 basis。
finding 自带的 chapter 与 quote 是当前候选稿中唯一匹配的待改片段。basis 可列另一处可核实片段
{chapter,field,quote}、已锁台账 {ruleId} 或作者已确认口径 {authorQuote}。
affectedChapters 仅填本次已给出上下文的实质关联章节。没有证据或改动须作者定口径时，
保留审校意见，requiresDecision=true，不猜测事实，不为文风口味制造硬伤。
若输入有 repairChecks，为每条返回 repairChecks:[{id,status:"fixed|open",reason,chapter,field,quote}]。
标 fixed 须在修订后的目标章节给出能证明修正的准确引文；关系仍矛盾或仅改了无关文字时标 open。
"""

SEMANTIC = VERIFIER + """
专项核对每章标题承诺的事件、金额、道具是否与正文相符，以及提供的相邻章首尾与跨卷接缝。
留意保留桥段、角色惯用语和伏笔。输入的硬性核验提示也请一并判断。
需要改动的确定问题才标 P0，低优先级提示可保留。逐条核验指定意见。
outcomes 必须覆盖输入顶层 issues[].id 的全部编号，每个编号只返回一项。逐字保留 @章号 后缀。
每个编号都有自己的核验章节。fixed 引文须出现在该章的候选稿；
引用标题时返回 field=title。检索项有合法用法时可判 not_applicable，并说明该章语境。
editorCoverage 是编辑留下的待核验理由，请结合原稿、候选稿和台账独立判断。
如事项只涉及总纲文档，本章正文无需修改，可说明理由；尚无法确认时返回 open。
回看压缩前后是否去掉重复桥段并改善推进，同时保留因果、决策条件与后果。大幅压缩属于复核线索。
若发现冻结口径遗漏了可引用原稿证据的跨章联动，另返回 ledgerConcerns 数组。
每项格式为 {"chapter":1,"quote":"未修改原文的连续引文","chapters":[1,2],"problem":"旧口径及拟修正内容"}。
""" + REPAIR_REVIEW

SEAM = """
你是只读审校员。检查给出的卷间接缝的章首章末是否复读、转折是否顺畅、上下卷的时间、道具和人物状态是否连贯。
返回 {"findings":[{"chapter":1,"quote":"候选稿中的连续原文","priority":"P0|P1|P2","problem":"具体问题"}]}。
已明确的正常回顾及无问题的接缝返回空列表。
""" + REPAIR_REVIEW

VERIFIER_TOOL_GUIDE = (
    "\n【跨章只读查证工具】若需确认本批之外其他章节（如前文伏笔、联动章、人物年龄/金额/器物口径）在改后候选稿（candidate）"
    "或改前原稿（original）中的真实内容，可先调用 search_manuscript、read_chapter 或 read_chapters_batch 查证，"
    "切勿凭空猜测或仅因未看到别章而误报问题。查证完毕后仍须严格按原 JSON 格式输出。"
)


def validated_semantic_outcomes(result, known, *, allow_missing=False):
    rows = result.get("outcomes")
    valid_list = isinstance(rows, list)
    accepted, unknown, conflicts, invalid = {}, set(), set(), set()
    for position, row in enumerate(rows if valid_list else [], 1):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            unknown.add(f"第 {position} 项（缺少有效 ID）")
            continue
        key = row["id"]
        if key not in known:
            chapter = chapter_number(row.get("chapter"))
            if chapter is None or "@" in key or f"{key}@{chapter}" not in known:
                unknown.add(key)
                continue
            key = f"{key}@{chapter}"
            row = {**row, "id": key}
        if row.get("status") not in ("fixed", "open", "not_applicable"):
            invalid.add(key)
        if key in accepted and row.get("status") != accepted[key].get("status"):
            conflicts.add(key)
        else:
            accepted.setdefault(key, row)

    missing, unknown, conflicts = sorted(known - accepted.keys()), sorted(unknown), sorted(conflicts)
    if (allow_missing and valid_list and not unknown and not conflicts and not invalid
            and 0 < len(missing) <= MAX_DEFERRED_IDS and accepted
            and all(string(row.get("reason"), 450) for row in accepted.values())):
        return [*accepted.values(), *(
            {"id": key, "status": "open", "reason": "独立终审未返回该项裁决，保留为待复核。"} for key in missing
        )]
    problems = [f"{label}：{'、'.join(ids)}" for label, ids in (
        ("缺失 ID", missing), ("多余/未识别 ID", unknown), ("重复 ID（状态冲突）", conflicts),
        ("结论或依据无效的 ID", sorted(invalid)),
    ) if ids]
    if not valid_list:
        problems.insert(0, "outcomes 必须是对象数组")
    if problems:
        raise RevisionContentError("语义审校遗漏裁决项。" + "；".join(problems) + "。",
                                   missingOutcomeIds=missing, unknownOutcomeIds=unknown, conflictingOutcomeIds=conflicts)
    return list(accepted.values())


def issue_fingerprint(issue):
    return digest([issue.get(key) for key in ("id", "chapter", "claimedChapter", "match", "category", "problem", "quote")])


class State(TypedDict):
    run_id: str
    route: str
    review_scope: NotRequired[str]
    affected_chapters: NotRequired[list[int]]


class RevisionWorkflowV2(RevisionRuntime):
    def __init__(self, directory, manuscripts, worker, checkpointer):
        super().__init__(directory, manuscripts, worker)
        graph = StateGraph(State)
        for name in ("prepare_v2", "audit_v2", "ledger_v2", "edit_v2", "hard_v2", "soft_v2", "repair_v2", "publish_v2"):
            graph.add_node(name, self.logged_stage(name))
        graph.add_edge(START, "prepare_v2")
        graph.add_edge("prepare_v2", "audit_v2")
        graph.add_edge("audit_v2", "ledger_v2")
        graph.add_conditional_edges("ledger_v2", lambda state: state["route"], {"edit": "edit_v2", "stop": END})
        graph.add_edge("edit_v2", "hard_v2")
        graph.add_conditional_edges("hard_v2", lambda state: state["route"],
                                    {"soft": "soft_v2", "repair": "repair_v2", "finish": "publish_v2"})
        graph.add_conditional_edges("soft_v2", lambda state: state["route"],
                                    {"repair": "repair_v2", "finish": "publish_v2", "ledger": "ledger_v2"})
        graph.add_conditional_edges("repair_v2", lambda state: state["route"], {"edit": "edit_v2", "finish": "publish_v2"})
        graph.add_edge("publish_v2", END)
        self.graph_v2 = graph.compile(checkpointer=checkpointer)

    def logged_stage(self, name):
        stage = getattr(self, name)

        async def execute(state: State):
            run = state["run_id"]
            record = self.store.get(run)
            started = perf_counter()
            log.info("revision_stage_start run=%s stage=%s ledgerVersion=%s affectedChapters=%d autoAmendments=%d",
                     run, name, record.get("ledgerVersion", 0), len(record.get("affectedChapters") or []),
                     record.get("autoAmendments", 0))
            result = None
            status = "ok"
            try:
                result = await stage(state)
                return result
            except asyncio.CancelledError:
                status = "paused"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                record = self.store.get(run)
                log.info("revision_stage_end run=%s stage=%s status=%s route=%s elapsedMs=%.0f "
                         "ledgerVersion=%s affectedChapters=%d autoAmendments=%d",
                         run, name, status, (result or {}).get("route"), (perf_counter() - started) * 1000,
                         record.get("ledgerVersion", 0), len(record.get("affectedChapters") or []),
                         record.get("autoAmendments", 0))

        return execute

    async def invoke(self, run):
        # Batch receipts in SQLite are authoritative. A new graph invocation starts
        # with them and cannot replay old graph edges after an author decision.
        await self.graph_v2.ainvoke(
            {"run_id": run, "route": ""},
            {"configurable": {"thread_id": f"revision-v2:{run}:{uuid4().hex}"}, "recursion_limit": 36},
            durability="sync",
        )

    def commit(self, run, *, record=None, products=(), status=None, **patch):
        expected = {key: record.get(key) for key in ("ledgerVersion", "work", "sourceEpoch")} if record else None
        if not self.store.commit_revision(run, expected=expected, products=products, status=status, **patch):
            raise RevisionContentError("原文、台账或返修批次版本已变化，请继续当前断点。")

    def source(self, run):
        snapshot = self.store.snapshot(run)
        turn = self.store.get(run).get("cycleBaseTurn")
        if turn is not None:
            candidate = self.load(run, "v2-result", turn)
            assessment = self.load(run, ROUND_KIND, turn)
            snapshot = {**snapshot, "chapters": candidate["candidate"], "report": assessment["report"]}
        restored = self.load(run, "v2-restored", "all" if turn is None else f"cycle-{turn}") or {}
        rows = [{**row, **{field: entry["text"] for field, entry in restored.get(str(row["chapterNumber"]), {}).items()}}
                for row in snapshot["chapters"]]
        return snapshot, {row["chapterNumber"]: row for row in rows}

    def v2_ledger(self, run, record):
        return self.load(run, "v2-ledger", record["ledgerVersion"])

    async def prepare_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        saved = self.load(run, "v2-map")
        if saved is not None and saved.get("cycleBaseTurn") == record.get("cycleBaseTurn"):
            return {}
        self.phase(run, "prepare", 1)
        snapshot, original = self.source(run)
        rows = list(original.values())
        volume_list = partition(rows, record["volumeSize"])
        check = preflight(rows, volume_list)
        defects = {entry["chapter"] for entry in check["defects"]}
        # Isolate missing-text chapters so their neighbors can continue.
        slices = []
        for volume in volume_list:
            group = []
            for n in volume["chapters"]:
                if n in defects:
                    if group:
                        slices.append({**volume, "chapters": group})
                        group = []
                    slices.append({**volume, "chapters": [n]})
                else:
                    group.append(n)
            if group:
                slices.append({**volume, "chapters": group})
        mapping = {
            "cycleBaseTurn": record.get("cycleBaseTurn"),
            "volumes": volume_list, "audit": windows(slices, original, max_chars=36_000, max_chapters=7),
            "edit": windows(slices, original, max_chars=15_000, max_chapters=3),
            "blind": broken_ranges(snapshot["report"], original), "preflight": check,
        }
        self.commit(run, products=[("v2-map", "all", mapping)], done=1, volumes=[
            {"number": volume["number"], "start": volume["chapters"][0], "end": volume["chapters"][-1]} for volume in volume_list
        ], auditBlindChapters=mapping["blind"])
        return {}

    async def units(self, run, stage, bucket, entries, execute, *, progress_offset=0, blocked_edit_batches=0):
        record = self.store.get(run)
        keys = {slot: digest(value) for slot, value in entries}
        done, pending = {}, []
        for slot, value in entries:
            existing = self.load(run, bucket, slot)
            if (existing and existing["inputHash"] == keys[slot]
                    and (not existing.get("validationFallback") or revision_fallback_enabled())):
                done[slot] = existing["result"]
            else:
                pending.append((slot, value))
        count_pending = len(pending)
        is_edit = bucket.startswith("v2-edit-")
        total = progress_offset + len(entries) + blocked_edit_batches

        def progress(completed):
            counts = {"done": progress_offset + completed, "total": total}
            return {**counts, "editProgress": counts} if is_edit else counts

        self.store.update(run, stage=stage, **progress(len(done)),
                          reprocessBatches=count_pending if record.get("ledgerVersion", 0) > 1 and stage == "edit" else None)
        queue = iter(pending)
        failures = []

        async def consume():
            while not failures and run not in self.pausing:
                entry = next(queue, None)
                if entry is None:
                    return
                slot, value = entry
                try:
                    result = await execute(slot, value)
                    receipt = {"inputHash": keys[slot], "ledgerVersion": record.get("ledgerVersion"), "result": result}
                    deferred = (result.get("deferred", []) if isinstance(result, dict) else
                                [row for row in result if row.get("decision") == "needs_review"])
                    if deferred:
                        receipt["validationFallback"] = True
                        log.warning("revision_batch_deferred run=%s stage=%s batch=%s issues=%s", run, stage, slot,
                                    sorted({key for row in deferred if isinstance(row, dict)
                                            for key in row.get("issueIds", [row.get("id")]) if key}))
                    self.commit(run, record=record, products=[(bucket, slot, receipt)], **progress(len(done) + 1))
                    done[slot] = result
                except Exception as exc:
                    if isinstance(exc, RevisionContentError):
                        numbers = value.get("editableChapters") or value.get("numbers")
                        label = "、".join(map(str, numbers)) + " 章" if numbers else slot
                        exc = RevisionContentError(f"{label}：{exc}", **{**exc.details,
                                                   "batch": slot, "chapters": numbers or []})
                    failures.append(exc)

        await asyncio.gather(*(consume() for _ in range(min(len(pending), record["concurrency"]))))
        self.check_pause(run)
        if failures:
            raise failures[0]
        return [done[slot] for slot, _ in entries]

    async def audit_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        if record.get("targeted") and self.load(run, "v2-audit-summary") is not None:
            return {}
        snapshot, original = self.source(run)
        mapping = self.load(run, "v2-map")
        defects = preflight(list(original.values()), mapping["volumes"])["defects"]
        blocked = {item["chapter"] for item in defects}
        issues = assessed_issues(snapshot["report"], original)
        entries = [(unit["id"], {
            "book": snapshot["title"], "original": text_sections(unit["chapters"], original),
            "blindChapters": sorted(set(unit["chapters"]).intersection(mapping["blind"])),
            "issues": scoped_issues(issues, unit["chapters"]), "boundaries": surroundings(unit["chapters"], original),
        }) for unit in mapping["audit"] if not blocked.intersection(unit["chapters"])]
        by_id = {unit["id"]: unit for unit in mapping["audit"]}

        async def inspect(slot, value):
            return await self.model_json(run, "audit", AUDIT, value,
                                         lambda result: validate_audit(result, original, by_id[slot]), DEFAULT_MAX_TOKENS)

        results = await self.units(run, "audit", "v2-audit", entries, inspect)
        self.commit(run, products=[("v2-audit-summary", "all", {
            "issues": issues + [issue for item in results for issue in item["issues"]],
            "facts": [fact for item in results for fact in item["facts"]],
            "defects": defects, "unsupported": sum(len(item["unsupported"]) for item in results),
        })], sourceDefects=defects, reportIssueCount=len(issues))
        return {}

    async def make_plan(self, run, record, source, audit, amendment=None):
        snapshot, original = source
        answers = record.get("authorAnswers", {})
        previous = self.v2_ledger(run, record) if record.get("ledgerVersion") else None
        source_evidence = located_evidence(audit["issues"], original, audit["facts"])
        plan_input = {
            "book": snapshot["title"], "facts": audit["facts"], "issues": audit["issues"], "issueEvidence": source_evidence,
            "allChapters": list(original), "style": snapshot.get("styleGuide"), "authorAnswers": answers,
            "oldLedger": previous, "requestedChange": amendment,
            "phraseBaseline": dict(phrase_counts(original, COMMON_PHRASES)),
        }
        threshold = ledger_batch_threshold()
        if threshold and len(audit["issues"]) > threshold:
            return await self.make_batched_plan(run, record, original, plan_input)

        self.phase(run, "ledger", 1)
        protected = protected_ledger(previous, {"authorAmendments": [amendment]} if amendment and amendment.get("after") else {})

        def validate(value, *, allow_deferred=False):
            if record["isMock"]:
                value.update(blueprint=[], decisions=[
                    {"id": item["id"], "decision": "skip", "reason": "模拟运行。", "evidence": [], "chapters": []}
                    for item in audit["issues"]
                ])
            return validated_plan(value, original, audit["issues"], answers, source_evidence,
                                  allow_deferred=allow_deferred, finance_context=protected)

        return await self.model_json(run, "ledger", PLAN, plan_input, validate, DEFAULT_MAX_TOKENS,
                                     fallback_on_final=lambda value, _: validate(value, allow_deferred=True))

    async def make_batched_plan(self, run, record, original, plan_input):
        issues = plan_input["issues"]
        source_evidence = plan_input["issueEvidence"]
        answers = plan_input["authorAnswers"]
        batches = [issues[start:start + LEDGER_BATCH_SIZE] for start in range(0, len(issues), LEDGER_BATCH_SIZE)]
        core_input = {
            **plan_input,
            "ledgerRequest": {"schema": LEDGER_PLANNING_SCHEMA, "phase": "core"},
            "sourceHash": fingerprint(list(original.values())),
        }
        # Includes the full evidence map, old ledger and author's instructions.
        context_hash = digest(core_input)
        receipt = self.load(run, "v2-ledger-core")
        if receipt and receipt["inputHash"] == context_hash:
            core = receipt["result"]
        else:
            self.phase(run, "ledger", 1 + len(batches))
            core = await self.model_json(run, "ledger", CORE_PLAN, core_input,
                                         lambda value: validated_ledger_core(value, original, source_evidence), DEFAULT_MAX_TOKENS)
            receipt = {"inputHash": context_hash, "ledgerVersion": record.get("ledgerVersion"), "result": core}
            self.commit(run, record=record, products=[("v2-ledger-core", "all", receipt)], done=1)

        old = plan_input["oldLedger"] or {}
        amendment = plan_input["requestedChange"] or {}
        protected = protected_ledger(core, old, {"authorAmendments": [amendment]} if amendment.get("after") else {})
        old_decisions = {item["id"]: item for item in old.get("decisions", [])}
        entries = []
        for index, batch in enumerate(batches, 1):
            ids = [item["id"] for item in batch]
            entries.append((f"decisions:{index}", {
                "book": plan_input["book"], "contextHash": context_hash,
                "allChapters": plan_input["allChapters"], "style": plan_input["style"],
                "authorAnswers": answers, "requestedChange": plan_input["requestedChange"],
                "authorAmendments": old.get("authorAmendments", []), "coreLedger": core,
                "ledgerRequest": {"schema": LEDGER_PLANNING_SCHEMA, "phase": "decisions", "batch": index},
                "issues": batch, "issueEvidence": {key: source_evidence[key] for key in ids if key in source_evidence},
                "oldDecisions": [old_decisions[key] for key in ids if key in old_decisions],
            }))

        async def adjudicate(_, value):
            batch = value["issues"]

            def validate(result, *, allow_deferred=False):
                if record["isMock"]:
                    result = {"decisions": [
                        {"id": item["id"], "decision": "skip", "reason": "模拟运行。"} for item in batch
                    ]}
                return validated_decision_batch(result, original, batch, answers, source_evidence,
                                                allow_deferred=allow_deferred, finance_context=protected)

            return await self.model_json(run, "ledger", BATCH_PLAN, value, validate, DEFAULT_MAX_TOKENS,
                                         fallback_on_final=lambda result, _: validate(result, allow_deferred=True))

        decisions = await self.units(run, "ledger", "v2-ledger-decisions", entries, adjudicate, progress_offset=1)
        rows = [item for batch in decisions for item in batch]
        return validated_plan({**core, "decisions": [item for item in rows if item["decision"] != "needs_review"]},
                              original, issues, answers, source_evidence,
                              trusted_deferred=[item for item in rows if item["decision"] == "needs_review"])

    async def make_semantic_delta(self, run, record, source, audit, old, intent):
        snapshot, original = source
        issues, targets = intent["issues"], intent["chapters"]
        answers = record.get("authorAnswers", {})
        evidence = located_evidence(issues, original, audit["facts"])
        old_scope = scope(old, targets)
        old_scope["checks"] = [{**item, "id": ledger_entry_id("checks", item)} for item in old_scope["checks"]]
        self.phase(run, "ledger", 1)
        return await self.model_json(run, "ledger", SEMANTIC_DELTA_PLAN, {
            "ledgerRequest": {"schema": 1, "phase": "semantic_delta"},
            "book": snapshot["title"], "sourceHash": fingerprint(list(original.values())),
            "ledgerVersion": record["ledgerVersion"], "affectedChapters": targets,
            "issues": issues, "issueEvidence": evidence, "oldScope": old_scope, "authorAnswers": answers,
            "facts": [item for item in audit["facts"] if item["chapter"] in targets],
        }, lambda result: merge_semantic_delta(result, old, original, issues, answers, evidence, targets), DEFAULT_MAX_TOKENS)

    async def ledger_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        intent = record.get("ledgerIntent")
        if record.get("ledgerVersion") and not intent:
            return {"route": "edit"}
        source = self.source(run)
        audit = self.load(run, "v2-audit-summary")
        if record.get("lateIssues"):
            audit = {**audit, "issues": [*audit["issues"], *record["lateIssues"]]}
        old = self.v2_ledger(run, record) if record.get("ledgerVersion") else None
        semantic_amendment = bool(old and intent and intent.get("issues") and (
            intent.get("kind") == "semantic_delta" or intent.get("reason") == "审校发现跨章联动"
        ))
        if semantic_amendment:
            ledger = await self.make_semantic_delta(run, record, source, audit, old, intent)
        else:
            ledger = await self.make_plan(run, record, source, audit, intent)
        if ledger["pending"]:
            self.commit(run, record=record, status="waiting_author", pendingDecisions=ledger["pending"])
            return {"route": "stop"}

        author_amendments = list((old or {}).get("authorAmendments", []))
        if intent and intent.get("after"):
            author_amendments = [item for item in author_amendments
                                 if item["after"] != intent["before"] or not set(item["chapters"]).intersection(intent["chapters"])]
            author_amendments.append(intent)
        ledger["authorAmendments"] = author_amendments
        version = record.get("ledgerVersion", 0) + 1
        products = [("v2-ledger", version, ledger)]
        patch = {"hasLedger": True, "ledgerVersion": version, "pendingDecisions": [], "ledgerIntent": None,
                 "done": self.store.get(run)["total"]}
        if old:
            _, original = source
            work = record["work"]
            impacted = {n for n in original if digest(scope(old, [n])) != digest(scope(ledger, [n]))}
            impacted.update(intent.get("chapters", []) if intent else [])
            if work["targets"] is not None:
                work = {**work, "targets": sorted(set(work["targets"]) | impacted)}
            if semantic_amendment:
                work = {**work, "ledgerDeltaVersion": version}
            patch["work"] = work
            patch["affectedChapters"] = sorted(impacted)
        self.commit(run, record=record, products=products, **patch)
        return {"route": "edit"}

    def blocked_chapters(self, record, ledger, original):
        blocked = {row["chapter"] for row in record.get("sourceDefects", [])}
        previous_size = -1
        while previous_size != len(blocked):
            previous_size = len(blocked)
            for rule in [*ledger["rules"], *ledger["decisions"]]:
                if rule.get("decision") != "skip" and any(ref["chapter"] in blocked for ref in rule["evidence"]):
                    blocked.update(rule["chapters"] or original)
            for flow in ledger["financialFlows"]:
                chapters = {item["chapter"] for item in flow["entries"]}
                if blocked.intersection(chapters):
                    blocked.update(chapters)
            for item in ledger["blueprint"]:
                if blocked.intersection([*item["chapters"], *item["joins"]]):
                    blocked.update(item["chapters"])
        return blocked

    async def edit_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        work = record.get("work") or {"turn": 0, "attempt": 0, "targets": None, "feedback": []}
        if not record.get("work"):
            self.commit(run, record=record, work=work)
            record = self.store.get(run)
        snapshot, original = self.source(run)
        saved_base = self.load(run, "v2-base")
        base = {row["chapterNumber"]: row for row in saved_base} if saved_base else original
        ledger = self.v2_ledger(run, record)
        blocked = self.blocked_chapters(record, ledger, original)
        target = (set(work["targets"]) if work["targets"] is not None else set(base)) - blocked
        units = self.load(run, "v2-map")["edit"]
        entries = []
        for unit in units:
            allowed = sorted(target.intersection(unit["chapters"]))
            if not allowed:
                continue
            selected = scope(ledger, allowed)
            selected["issues"] = chapter_checks(selected["issues"], allowed)
            slot = unit["id"]
            if delta_version := work.get("ledgerDeltaVersion"):
                slot = f"{slot}:ledger-{delta_version}"
            entries.append((slot, {
                "book": snapshot["title"], "style": snapshot.get("styleGuide"), "ledger": selected,
                "editableChapters": allowed, "chapters": text_sections(allowed, base),
                "boundaries": surroundings(unit["chapters"], base),
                "requiredIssues": [item["id"] for item in selected["issues"]],
                "feedback": [item for item in work["feedback"] if item.get("chapter") is None or item["chapter"] in allowed],
            }))

        async def edit(slot, value):
            allowed = set(value["editableChapters"])
            protected = {
                n for item in (*ledger["rules"], *ledger.get("authorAmendments", []))
                if item.get("track") == "finance" or "after" in item for n in item["chapters"] or allowed
            }
            protected.update(row["chapter"] for flow in ledger["financialFlows"] for row in flow["entries"])
            protected.update(n for check in ledger["checks"] if financial_text({"quote": check["text"]})
                             for n in check["chapters"] or allowed)
            protected.update(n for item in ledger["decisions"] if item["id"] in record.get("authorAnswers", {})
                             or (item["decision"] in ("adopt", "adapt") and financial_text(item))
                             for n in item["chapters"] or allowed)
            protected_ids = {
                issue["id"] for issue in value["ledger"]["issues"]
                if issue["decisionId"] in record.get("authorAnswers", {}) or financial_text(issue)
            }
            aux_ids = {
                row["id"] for row in (*value["ledger"]["rules"], *value["ledger"]["checks"])
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
            for fb in value["feedback"]:
                repair = fb.get("repair") if isinstance(fb, dict) else None
                if isinstance(repair, dict):
                    if isinstance(repair.get("id"), str):
                        aux_ids.add(repair["id"])
                    for basis in repair.get("basis") or ():
                        if isinstance(basis, dict) and isinstance(basis.get("ruleId"), str):
                            aux_ids.add(basis["ruleId"])

            def validate(result, *, allow_deferred=False):
                accepted = edit_patches(
                    {"patches": [], "coverage": []} if record["isMock"] else result, base, allowed,
                    value["requiredIssues"], allow_deferred=allow_deferred,
                    protected_chapters=protected, protected_ids=protected_ids, known_aux_ids=aux_ids,
                )
                if accepted.get("deferred"):
                    issue_keys = {item["id"]: issue_fingerprint(item) for item in value["ledger"]["issues"]}
                    for item in accepted["deferred"]:
                        item["issueKeys"] = {key: issue_keys[key] for key in item["issueIds"]}
                return accepted

            return await self.model_json(run, "editor", PATCH, value, validate, DEFAULT_MAX_TOKENS,
                                         fallback_on_final=lambda result, _: validate(result, allow_deferred=True))

        hotfix = work["turn"] and work.get("roundType") != "evaluation"
        stage = "repair" if work["attempt"] else "hotfix" if hotfix else "edit"
        bucket = f"v2-edit-{work['turn']}-{work['attempt']}"
        blocked_batches = sum(any(n in blocked for n in unit["chapters"]) for unit in units)
        results = await self.units(run, stage, bucket, entries, edit, blocked_edit_batches=blocked_batches)
        candidate = dict(base)
        for item in results:
            candidate.update((row["chapterNumber"], row) for row in item["changed"])
        changes = [item for result in results for item in result["log"]]
        coverage = {key: value for result in results for key, value in result["coverage"].items()}
        deferred = [item for result in results for item in result.get("deferred", [])]
        if work.get("ledgerDeltaVersion"):
            previous_changes = self.load(run, "v2-changes", bucket) or []
            changes = list({digest(item): item for item in [*previous_changes, *changes]}.values())
            coverage = {**(self.load(run, "v2-coverage", bucket) or {}), **coverage}
            deferred = [*(self.load(run, "v2-validation-edit", bucket) or []), *deferred]
        self.commit(run, record=record, products=[
            ("v2-draft", "all", list(candidate.values())),
            ("v2-changes", bucket, changes),
            ("v2-coverage", bucket, coverage),
            ("v2-validation-edit", bucket, deferred),
        ], blockedChapters=sorted(blocked))
        affected = work["targets"]
        review_chapters = sorted({*(affected or []), *work.get("reviewChapters", [])})
        return {"review_scope": "full" if affected is None else "incremental", "affected_chapters": review_chapters}

    def draft(self, run):
        return {row["chapterNumber"]: row for row in self.load(run, "v2-draft")}

    async def hard_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        self.phase(run, "hard", 1)
        _, original = self.source(run)
        candidate = self.draft(run)
        ledger = self.v2_ledger(run, record)
        findings = hard_review(original, candidate, ledger)
        blockers = [item for item in findings if item["priority"] == "P0"]
        self.commit(run, record=record, products=[("v2-hard", "all", findings), ("v2-findings", "all", findings)], done=1)
        if record["blockedChapters"]:
            return {"route": "finish"}
        return {"route": "repair" if blockers and not record["isMock"] else "soft"}

    async def soft_v2(self, state):
        run = state["run_id"]
        record = self.store.get(run)
        candidate = self.draft(run)
        snapshot, original = self.source(run)
        candidate_hashes = {n: digest(chapter) for n, chapter in candidate.items()}
        ledger = self.v2_ledger(run, record)
        mapping = self.load(run, "v2-map")
        hard = self.load(run, "v2-hard")
        work = record["work"]
        prior_repairs = work.get("repairChecks", [])
        author_answers = record.get("authorAnswers", {})
        editor_coverage = {}
        for attempt in range(work["attempt"] + 1):
            editor_coverage.update(self.load(run, "v2-coverage", f"v2-edit-{work['turn']}-{attempt}") or {})

        def review_links(numbers):
            return [{
                "question": item["problem"], "target": item["repair"]["target"],
                "basis": item["repair"]["basis"], "targetVersion": candidate_hashes[item["chapter"]],
            } for item in prior_repairs
                if item["chapter"] not in numbers and set(numbers).intersection(item["repair"]["affectedChapters"])]

        def review_ledger(numbers):
            selected = scope(ledger, numbers)
            selected["authorDecisions"] = [
                {"text": author_answers[issue["id"]], "chapters": issue["chapters"]}
                for issue in selected["issues"] if issue["id"] in author_answers
            ]
            return selected

        targeted_mode = bool(record.get("targeted"))
        targeted_directive = self.load(run, "v2-hotfix", work["turn"]) if targeted_mode else None
        review_scope_set = (
            set(state.get("affected_chapters") or work.get("targets") or [])
            | set(work.get("reviewChapters") or [])
            | {item["chapter"] for item in prior_repairs}
        ) if targeted_mode else None

        entries = []
        for unit in mapping["edit"]:
            numbers = unit["chapters"]
            if review_scope_set is not None and not review_scope_set.intersection(numbers):
                continue
            selected = review_ledger(numbers)
            issues = chapter_checks(selected["issues"], numbers)
            deferred = []
            for issue in issues:
                item = editor_coverage.get(issue["id"], {})
                if item.get("outcome") == "needs_review":
                    deferred.append({"id": issue["id"], "reason": item["reason"],
                                     **({"note": item["reviewNote"]} if "reviewNote" in item else {})})
            neighbors = [n for n in (numbers[0] - 1, numbers[-1] + 1) if n in candidate]
            scoped_repairs = [item for item in prior_repairs if item["chapter"] in numbers]
            linked = sorted({n for item in scoped_repairs for n in item["repair"]["affectedChapters"]} - set(numbers))
            entries.append((f"batch:{unit['id']}", {
                "book": snapshot["title"], "style": snapshot.get("styleGuide"),
                **({"targetedDirective": targeted_directive} if targeted_directive else {}),
                "ledger": selected,
                "numbers": numbers, "candidate": text_sections(numbers, candidate),
                "before": text_sections(numbers, original), "boundaries": surroundings(numbers, candidate),
                "lengthChanges": [{"chapter": n, "beforeChars": len(original[n]["content"]),
                                   "afterChars": len(candidate[n]["content"])} for n in numbers
                                  if len(original[n]["content"]) != len(candidate[n]["content"])],
                "relatedVersions": {n: candidate_hashes[n] for n in related_chapters(selected)
                                    if n not in numbers},
                "neighborVersions": {n: candidate_hashes[n] for n in neighbors},
                "repairChecks": scoped_repairs,
                "repairLinks": review_links(numbers),
                "repairContext": {n: {
                    "title": candidate[n]["title"], "opening": candidate[n]["content"][:1200],
                    "ending": candidate[n]["content"][-1200:], "version": candidate_hashes[n],
                } for n in linked},
                "issues": issues,
                **({"editorCoverage": deferred} if deferred else {}),
                "hints": [item for item in hard if item["chapter"] in numbers],
            }))
        for volume in mapping["volumes"][:-1]:
            n = volume["chapters"][-1]
            if review_scope_set is not None and not review_scope_set.intersection((n, n + 1)):
                continue
            entries.append((f"join:{n}", {
                "book": snapshot["title"], "ledger": review_ledger([n, n + 1]),
                "repairLinks": review_links([n, n + 1]),
                "numbers": [n, n + 1],
                "titles": [candidate[number]["title"] for number in (n, n + 1)],
                "ending": candidate[n]["content"][-1500:], "opening": candidate[n + 1]["content"][:1500],
            }))

        # A changed chapter, neighbor excerpt or rule changes the corresponding
        # request digest. Compatible semantic receipts are reused across rounds.
        self.commit(run, record=record, reviewScope=state["review_scope"], affectedChapters=state["affected_chapters"])

        async def verify(slot, value):
            allowed = set(value["numbers"])
            join = slot.startswith("join:")
            visible = allowed | set(value.get("neighborVersions", {})) | set(value.get("repairContext", {}))
            visible.update(int(n) for n in value.get("relatedVersions", {}))

            def validate(result, *, allow_missing=False):
                origin = "卷间接缝" if join else "语义审校"
                findings = []
                for item in required_list(result, "findings"):
                    verified = verified_repair(item, candidate, allowed, visible, value["ledger"], origin)
                    if verified:
                        findings.append(verified)
                    elif (evidence := exact_passage(item, candidate, allowed)) and string(item.get("problem"), 650):
                        priority = item.get("priority")
                        findings.append(risk(evidence["chapter"], string(item["problem"], 650), evidence["quote"],
                                             priority if priority in ("P0", "P1", "P2") else "P1", origin))
                    else:
                        findings.extend(reviewed_findings({"findings": [item]}, candidate, allowed, origin))
                unresolved, reviews = recheck_repairs(result, value.get("repairChecks", []), candidate, visible, value["ledger"])
                concerns = []
                for item in required_list({"items": result.get("ledgerConcerns", [])}, "items"):
                    evidence = audited_evidence(item, original, allowed)
                    problem = string(item.get("problem"), 900)
                    chapters = item.get("chapters")
                    if (evidence and problem and isinstance(chapters, list)
                            and all(type(n) is int and n in original for n in chapters)):
                        identifier = "X-" + digest([evidence, sorted(set(chapters))])[:16]
                        concerns.append({**evidence, "id": identifier, "match": "audit", "priority": "P0",
                                         "problem": problem, "chapters": sorted({evidence["chapter"], *chapters})})
                outcomes, deferred = [], []
                if not join:
                    known = {issue["id"]: issue for issue in value["issues"]}
                    if record["isMock"]:
                        outcomes = required_list(result, "outcomes")
                    else:
                        for row in required_list(result, "outcomes"):
                            if "chapter" in row and chapter_number(row["chapter"]) not in candidate:
                                raise RevisionContentError(f"语义审校引用了本书不存在的章节 {row['chapter']}。")
                        try:
                            outcomes = validated_semantic_outcomes(result, set(known))
                        except RevisionContentError as exc:
                            if not allow_missing:
                                raise
                            outcomes = validated_semantic_outcomes(result, set(known), allow_missing=True)
                            if any(row["status"] == "fixed" and not audited_evidence(
                                row, candidate, {known[row["id"]]["chapter"]}
                            ) for row in outcomes):
                                raise RevisionContentError("标为已修的终审结论缺少候选稿证据。") from exc
                            deferred = [
                                {"id": key, "chapter": known[key]["chapter"],
                                 "issueKeys": {key: issue_fingerprint(known[key])},
                                 "reason": "独立终审未返回该项裁决，保留为待复核。"}
                                for key in exc.details["missingOutcomeIds"]
                            ]
                return {"findings": [*findings, *unresolved], "outcomes": outcomes, "concerns": concerns, "reviews": reviews,
                        **({"deferred": deferred} if deferred else {})}

            use_tools = bool(
                not record["isMock"]
                and os.getenv("NOVEL_FORGE_VERIFIER_TOOLS", "1") != "0"
                and getattr(self.worker, "supports_native_tools", False)
                and len(candidate) > len(allowed)
            )
            books = {"candidate": candidate, "original": original}

            async def run_verifier_tool(name, args):
                payload, touched = await asyncio.to_thread(run_readonly_tool, books, "candidate", name, args)
                if payload.get("version") == "candidate":
                    visible.update(touched)
                return payload

            base_instructions = SEAM if join else SEMANTIC
            return await self.model_json(
                run, "final" if join else "verifier",
                base_instructions + (VERIFIER_TOOL_GUIDE if use_tools else ""),
                value, validate, DEFAULT_MAX_TOKENS,
                fallback_on_final=None if join else lambda result, _: validate(result, allow_missing=True),
                tools=readonly_tool_specs(("candidate", "original")) if use_tools else None,
                tool_runner=run_verifier_tool if use_tools else None,
            )

        hotfix = work["turn"] and work.get("roundType") != "evaluation"
        stage = "hotcheck" if hotfix else "recheck" if work["attempt"] else "review"
        results = await self.units(run, stage, "v2-soft", entries, verify)
        issues = [item for item in ledger["decisions"] if item["decision"] in ("adopt", "adapt")]
        reviewed = self.outcome_map(
            chapter_checks(issues), [item for result in results for item in result["outcomes"]], original, candidate,
        )
        outcomes = aggregate_chapter_checks(issues, reviewed)
        findings = distinct_findings(hard + [item for result in results for item in result["findings"]])
        deferred = [item for result in results for item in result.get("deferred", [])]
        missing_ids = {item["id"] for item in deferred}
        findings.extend(risk(item["chapter"], item["id"] + " 尚未解决：" + item["reason"],
                             item["quote"], item["priority"], "裁决") for item in reviewed
                        if item["status"] == "open" and item["id"] not in missing_ids)
        known_concerns = {item["id"] for item in record.get("lateIssues", [])}
        concerns = {item["id"]: item for result in results for item in result.get("concerns", []) if item["id"] not in known_concerns}
        if concerns and record.get("autoAmendments", 0) >= 2:
            findings.extend(risk(None, item["problem"], item["quote"], origin="台账待核实") for item in concerns.values())
        elif concerns:
            targets = sorted({n for item in concerns.values() for n in item["chapters"]})
            self.commit(run, record=record, products=[("v2-base", "all", list(candidate.values()))],
                        lateIssues=[*record.get("lateIssues", []), *concerns.values()],
                        autoAmendments=record.get("autoAmendments", 0) + 1,
                        ledgerIntent={"kind": "semantic_delta", "chapters": targets,
                                      "reason": "审校发现跨章联动", "issues": list(concerns.values())},
                        work={**work, "targets": targets, "feedback": [{**item, "chapter": None} for item in concerns.values()]})
            return {"route": "ledger"}
        reviews = [item for result in results for item in result.get("reviews", [])]
        self.commit(run, record=record, products=[
            ("v2-findings", "all", findings),
            ("v2-repair-review", f"{work['turn']}-{work['attempt']}", reviews),
            ("v2-semantic-status", work["turn"], {
                "attempt": work["attempt"], "ledgerVersion": record["ledgerVersion"],
                "findings": findings, "outcomes": outcomes,
                **({"deferred": deferred} if deferred else {}),
                "verifiedEdits": [item["id"] for item in reviewed if item["status"] == "fixed"],
                "reviewedIds": [item["id"] for item in reviewed if item["id"] not in missing_ids
                                and item["status"] in ("fixed", "not_applicable")],
            }),
        ])
        return {"route": "repair" if not record["isMock"] and repair_feedback(findings) else "finish"}

    async def repair_v2(self, state):
        run = state["run_id"]
        self.check_pause(run)
        record = self.store.get(run)
        work = record["work"]
        feedback = repair_feedback(self.load(run, "v2-findings"))
        target = sorted({item["chapter"] for item in feedback if item["chapter"] is not None})
        if len(target) == 0 or work["attempt"] >= 2:
            return {"route": "finish"}
        reviewed = self.load(run, "v2-repair-review", f"{work['turn']}-{work['attempt']}") or []
        covered = {item["id"] for item in reviewed}
        pending = [item for item in work.get("repairChecks", []) if item["repair"]["id"] not in covered]
        checks = distinct_findings([*pending, *(item for item in feedback if item.get("repair"))])
        affected = sorted({*target, *(n for item in checks for n in item["repair"]["affectedChapters"])})
        work = {key: value for key, value in work.items() if key != "ledgerDeltaVersion"}
        self.commit(run, record=record, products=[("v2-base", "all", list(self.draft(run).values()))],
                    work={**work, "attempt": work["attempt"] + 1, "targets": target, "feedback": feedback,
                          "repairChecks": checks, "reviewChapters": affected},
                    reviewScope="incremental", affectedChapters=affected)
        return {"route": "edit"}

    async def publish_v2(self, state):
        run = state["run_id"]
        self.check_pause(run)
        record = self.store.get(run)
        candidate = self.draft(run)
        original = {row["chapterNumber"]: row for row in self.store.snapshot(run)["chapters"]}
        ledger = self.v2_ledger(run, record)
        findings = self.load(run, "v2-findings")
        blocking = [item for item in findings if item["priority"] == "P0"]
        blocking.extend(risk(n, "原文缺损或依赖的文本待恢复。") for n in record["blockedChapters"])
        work = record["work"]
        semantic = self.load(run, "v2-semantic-status", work["turn"])
        reviewed = semantic and (semantic["attempt"], semantic["ledgerVersion"]) == (work["attempt"], record["ledgerVersion"])
        outcomes = semantic["outcomes"] if reviewed else []
        previous_turn = record.get("candidateTurn")
        previous = self.load(run, "v2-result", previous_turn) if previous_turn is not None else None
        decisions = {row["id"]: row for row in ledger["decisions"]}
        validation_pending = [
            {"stage": "ledger", "id": row["id"], "chapters": row["chapters"], "reason": row["reason"],
             "issueKeys": {row["id"]: issue_fingerprint(row)}}
            for row in decisions.values() if row["decision"] == "needs_review"
        ]
        carried = (previous or {}).get("validationPending", [])
        failed_edits = [
            {"stage": "edit", **item}
            for attempt in range(work["attempt"] + 1)
            for item in (self.load(run, "v2-validation-edit", f"v2-edit-{work['turn']}-{attempt}") or [])
        ]
        soft_pending = [{"stage": "review", **item} for item in semantic.get("deferred", [])] if reviewed else []
        verified_ids = set(semantic.get("verifiedEdits", [])) if reviewed else set()
        reviewed_ids = set(semantic.get("reviewedIds", [])) if reviewed else set()
        issue_keys = {row["id"]: issue_fingerprint(row) for row in chapter_checks(
            [row for row in decisions.values() if row["decision"] in ("adopt", "adapt")]
        )}
        accepted_decisions = {row["id"] for row in outcomes if row["status"] in ("fixed", "not_applicable")}
        review_pending = {}
        for row in [*carried, *failed_edits, *soft_pending, *validation_pending]:
            ids = row.get("issueIds", [])
            keys = row.get("issueKeys", {})
            if row["stage"] == "edit" and ids and all(key in verified_ids and keys.get(key) == issue_keys.get(key) for key in ids):
                continue
            if row["stage"] == "review" and row["id"] in reviewed_ids and keys.get(row["id"]) == issue_keys.get(row["id"]):
                continue
            if row["stage"] == "ledger" and (decision := decisions.get(row["id"])):
                if keys.get(row["id"]) == issue_fingerprint(decision) and (
                    row["id"] in accepted_decisions or (decision["decision"] == "skip" and decision.get("evidence"))
                ):
                    continue
            key = (row["stage"], row.get("id"), row.get("chapter"), row.get("field"), row.get("anchor"), tuple(keys.items()))
            review_pending[key] = row
        validation_pending = list(review_pending.values())
        if semantic and not reviewed:
            # A hard failure kept this candidate from reaching semantic review.
            # Keep the previous issues visible and mark their evidence as old.
            known = {(item["priority"], item["chapter"], item["problem"], item["quote"]) for item in findings}
            prior = [*semantic["findings"], *work.get("repairChecks", [])]
            carried = [
                {**item, "pendingVerification": True} for item in prior
                if (item["priority"], item["chapter"], item["problem"], item["quote"]) not in known
            ]
            findings = remaining_findings({"findings": [*findings, *carried]})
        output = {
            "candidate": list(candidate.values()), "candidateHash": fingerprint(list(candidate.values())),
            "sourceVersion": record["sourceVersion"], "ledgerVersion": record["ledgerVersion"],
            "cycleBaseTurn": record.get("cycleBaseTurn"),
            "findings": findings, "blocking": blocking, "outcomes": outcomes,
            **({"validationPending": validation_pending} if validation_pending else {}),
            "metrics": metrics(original, candidate, ledger, COMMON_PHRASES),
            "changes": [
                item for attempt in range(work["attempt"] + 1)
                for item in (self.load(run, "v2-changes", f"v2-edit-{work['turn']}-{attempt}") or [])
            ],
            "repairReviews": [
                item for attempt in range(1, work["attempt"] + 1)
                for item in (self.load(run, "v2-repair-review", f"{work['turn']}-{attempt}") or [])
            ],
            "authorAnswers": record.get("authorAnswers", {}),
        }
        remaining = remaining_findings(output)
        counts = {priority: sum(item["priority"] == priority for item in remaining) for priority in ("P0", "P1", "P2")}
        pending_blockers = [risk(item.get("chapter"), item["reason"], origin="容错待复核") for item in validation_pending]
        blockers = blocking + pending_blockers
        status = "waiting_source" if record["sourceDefects"] else "needs_review" if blockers else "ready"
        self.commit(run, record=record, products=[("v2-result", work["turn"], output)],
                    status=status, stage="edit" if status == "waiting_source" else "finish",
                    done=record["editProgress"]["done"] if status == "waiting_source" else 1,
                    total=record["editProgress"]["total"] if status == "waiting_source" else 1,
                    hasCandidate=True, blockingCount=len(blockers), blockers=blockers[:8],
                    remainingIssueCounts=counts,
                    candidateHistory=sorted({*record.get("candidateHistory", []), work["turn"]}),
                    changedCount=sum(candidate[n] != original[n] for n in original), candidateTurn=work["turn"])

    def _resolve_targeted_models(self, previous, job, preferred_model):
        def usable(raw):
            if not isinstance(raw, dict):
                return None
            provider = string(raw.get("provider"), 64).strip()
            model = string(raw.get("model"), 256).strip()
            if not provider or not model or provider == "mock":
                return None
            if provider == SIWC and not siwc_all_stages():
                return None
            result = {"provider": provider, "model": model, "useSavedConnection": True}
            if provider == SIWC and isinstance(raw.get("siwcProfileId"), str) and raw["siwcProfileId"]:
                result["siwcProfileId"] = raw["siwcProfileId"]
            return result

        if previous and previous.get("workflowVersion") == 2 and not previous.get("isMock"):
            ed, ver = usable(previous.get("editor")), usable(previous.get("verifier"))
            if ed and ver:
                return ed, ver
        if found := usable(job.get("llmConfig")):
            return found, found
        if found := usable(preferred_model):
            return found, found
        catalog_fn = getattr(self.manuscripts, "get_model_catalog_configs", None)
        if callable(catalog_fn):
            snap = catalog_fn()
            configs = snap.get("configs") or {}
            ordered = [snap.get("activeProvider"), *(k for k in configs if k != snap.get("activeProvider"))]
            for key in ordered:
                cfg = configs.get(key)
                defn = PROVIDERS.get(key or "")
                if not isinstance(cfg, dict) or not defn or key in ("mock", SIWC):
                    continue
                needs_key = defn.get("requiresKey") is not False
                has_conn = bool(cfg.get("apiKey")) if needs_key else bool(cfg.get("baseUrl"))
                model = cfg.get("model") or (cfg.get("discoveredModels") or cfg.get("manualModels") or defn.get("models") or [""])[0]
                if has_conn and model:
                    pick = {"provider": key, "model": model, "useSavedConnection": True}
                    return pick, pick
        raise WorkerError("请先在模型管理配置一个可用于修订的模型（ChatGPT 订阅模型默认仅对总编开放）。", 400, "REVISION_SETTINGS")

    async def start_targeted(self, job_id, payload, *, expected_source_version=None, preferred_model=None):
        async with self.lock:
            previous = self.store.latest(job_id)
            if previous and previous.get("workflowVersion") == 2 and (
                previous["id"] in self.tasks
                or previous["status"] in ("running", "paused", "error", "waiting_author", "waiting_source")
            ):
                raise WorkerError("此作品已有进行中的修订任务，请先处理当前修订。", 409, "REVISION_IN_PROGRESS")
            current = await asyncio.to_thread(self.manuscripts.job_detail, job_id)
            if not current:
                raise WorkerError("找不到作品。", 404, "JOB_NOT_FOUND")
            job, rows = current["job"], current["chapters"]
            if not rows or [row.get("chapterNumber") for row in rows] != list(range(1, len(rows) + 1)):
                raise WorkerError("章节编号需要完整连续。", 409, "REVISION_CHAPTERS")
            chapters = [{key: row[key] for key in ("chapterNumber", "title", "content")} for row in rows]
            version = fingerprint(chapters)
            source = await self.worker.request("revision-source", jobId=job_id)
            self.require_revision_source_ready(source)
            if source["version"] != version or (expected_source_version and expected_source_version != version):
                raise WorkerError("正文已更新，请总编重新确认后再发起定向修改。", 409, "EDITOR_PLAN")
            note = string(payload.get("note"), 2400).strip()
            targets = payload.get("chapters")
            review_chapters = payload.get("reviewChapters", [])
            if (not note or not isinstance(targets, list) or not 1 <= len(targets) <= len(chapters)
                    or not isinstance(review_chapters, list)
                    or any(type(n) is not int or not 1 <= n <= len(chapters) for n in [*targets, *review_chapters])):
                raise WorkerError("请填写要定向修改的章节和修改说明。", 400, "REVISION_SETTINGS")
            targets = sorted(set(targets))
            review_chapters = sorted(set(review_chapters) - set(targets))
            intent = None
            if amendment := payload.get("amendment"):
                if not isinstance(amendment, dict) or any(not string(amendment.get(key), 1200)
                                                         for key in ("before", "after", "evidence")):
                    raise WorkerError("同步全书口径需填写旧口径、新口径和依据。", 400, "REVISION_SETTINGS")
                intent = {key: string(amendment[key], 1200) for key in ("before", "after", "evidence")}
                intent["chapters"] = targets
            editor, verifier = self._resolve_targeted_models(previous, job, preferred_model)
            report = job.get("evaluationReport") or {}
            run = uuid4().hex
            ledger = {
                "rules": [], "checks": [], "financialFlows": [], "blueprint": [],
                "decisions": [], "pending": [],
                "authorAmendments": [intent] if intent else [],
            }
            self.store.create(run, job_id, {
                "title": job["title"], "genre": job.get("genre"), "pov": job.get("pov"),
                "styleGuide": job.get("styleGuide"), "chapters": chapters, "report": report,
            }, {
                "stage": "prepare", "total": 1, "done": 0, "sourceVersion": version,
                "workflowVersion": 2, "targeted": True, "sourceDefects": [],
                "evaluationAt": report.get("evaluatedAt") or "", "volumeSize": 20, "concurrency": 3,
                "chapterCount": len(chapters), "volumes": [], "changedCount": 0, "blockingCount": 0,
                "editor": editor, "verifier": verifier, "createdAt": timestamp(), "isMock": False,
                "hasLedger": True, "ledgerVersion": 1, "ledgerIntent": None,
                "work": {
                    "turn": 1, "attempt": 0, "roundType": "hotfix",
                    "targets": targets, "reviewChapters": review_chapters,
                    "feedback": [{"chapter": n, "problem": note} for n in targets],
                },
                "reviewScope": "incremental", "affectedChapters": sorted(set(targets + review_chapters)),
            })
            self.commit(run, products=[
                ("v2-audit-summary", "all", {"issues": [], "facts": [], "defects": [], "unsupported": 0}),
                ("v2-ledger", 1, ledger),
                ("v2-hotfix", 1, {"note": note, "amendment": intent}),
            ])
            self.schedule(run)
            return self.store.get(run)

    async def special_action(self, job_id, run, action, payload, record):
        status = record["status"]
        if action == "archive" and status in ("waiting_author", "waiting_source") and run not in self.tasks:
            self.store.update(run, "archived")
            return True
        if action not in ("decide", "restore", "repair", "continue-round"):
            return False
        if run in self.tasks:
            raise WorkerError("请等当前阶段保存后再操作。", 409, "REVISION_STATE")
        await self.assert_current(job_id, record)
        if action == "decide" and status == "waiting_author":
            decisions = payload.get("decisions")
            expected = {item["id"] for item in record["pendingDecisions"]}
            if not isinstance(decisions, dict) or set(decisions) != expected or not all(string(v, 1200) for v in decisions.values()):
                raise WorkerError("请为全部关键歧义选定口径。", 400, "REVISION_SETTINGS")
            self.schedule(run, authorAnswers={**record.get("authorAnswers", {}),
                                              **{key: string(value, 1200) for key, value in decisions.items()}})
        elif action == "restore" and status == "waiting_source":
            n, field = payload.get("chapter"), payload.get("field")
            body, evidence = payload.get("text"), string(payload.get("evidence"), 600)
            if (type(n) is not int or not any(row["chapter"] == n and row["field"] == field for row in record["sourceDefects"])
                    or not isinstance(body, str) or not body.strip() or not evidence or "\ufffd" in body
                    or len(body) > 100_000):
                raise WorkerError("请提交待恢复字段的完整可靠原文及来源。", 400, "REVISION_SETTINGS")
            cycle = record.get("cycleBaseTurn")
            restore_slot = "all" if cycle is None else f"cycle-{cycle}"
            restored = self.load(run, "v2-restored", restore_slot) or {}
            restored.setdefault(str(n), {})[field] = {"text": body, "evidence": evidence}
            # Keep the original edit base and its receipts. The edited field and
            # old neighboring context change their digests automatically.
            turn = record["work"]["turn"]
            self.commit(run, record=record, products=[("v2-restored", restore_slot, restored)],
                        sourceEpoch=record.get("sourceEpoch", 0) + 1, ledgerIntent={"chapters": [n], "reason": "可靠原文补齐"},
                        work={"turn": turn, "attempt": 0, "targets": None, "feedback": [],
                              "roundType": record["work"].get("roundType")})
            self.schedule(run)
        elif action == "repair" and status in ("ready", "needs_review") and not record["isMock"]:
            note, chapters = string(payload.get("note"), 2400), payload.get("chapters")
            review_chapters = payload.get("reviewChapters", [])
            if (not note or not isinstance(chapters, list) or not 1 <= len(chapters) <= record["chapterCount"]
                    or not isinstance(review_chapters, list)
                    or any(type(n) is not int or not 1 <= n <= record["chapterCount"] for n in [*chapters, *review_chapters])):
                raise WorkerError("请填写返修章节和反馈。", 400, "REVISION_SETTINGS")
            intent = None
            if amendment := payload.get("amendment"):
                if not isinstance(amendment, dict) or any(not string(amendment.get(key), 1200)
                                                         for key in ("before", "after", "evidence")):
                    raise WorkerError("同步台账需填写旧口径、新口径和依据。", 400, "REVISION_SETTINGS")
                intent = {key: string(amendment[key], 1200) for key in ("before", "after", "evidence")}
                intent["chapters"] = sorted(set(chapters))
            turn = record["work"]["turn"] + 1
            result = self.load(run, "v2-result", record["candidateTurn"])
            self.commit(run, record=record, products=[("v2-base", "all", result["candidate"]),
                                                      ("v2-hotfix", turn, {"note": note, "amendment": intent})],
                        work={"turn": turn, "attempt": 0, "targets": sorted(set(chapters)),
                              "reviewChapters": sorted(set(review_chapters)),
                              "feedback": [{"chapter": n, "problem": note} for n in sorted(set(chapters))]},
                        ledgerIntent=intent, reviewScope="incremental", affectedChapters=sorted(set(chapters + review_chapters)))
            self.schedule(run)
        elif action == "continue-round" and status in ("ready", "needs_review") and not record["isMock"]:
            parent = record.get("candidateTurn")
            assessment = self.load(run, ROUND_KIND, parent) if parent is not None else None
            result = self.load(run, "v2-result", parent) if parent is not None else None
            latest = self.store.latest(job_id)
            if (type(payload.get("turn")) is not int or payload["turn"] != parent or not latest or latest["id"] != run
                    or not result or not assessment or assessment["status"] != "completed" or assessment.get("isMock")
                    or not assessment.get("report") or result["candidateHash"] != assessment["candidateHash"]):
                raise WorkerError("请先完成当前候选稿的真实评估，再按这份新报告继续修。", 409, "ROUND_REPORT")
            turn = record["work"]["turn"] + 1
            self.commit(run, record=record, products=[("v2-base", "all", result["candidate"])],
                        cycleBaseTurn=parent, work={"turn": turn, "attempt": 0, "targets": None, "feedback": [],
                                                    "roundType": "evaluation"},
                        ledgerIntent={"reason": "按新一轮评估重新建立修订计划", "chapters": []},
                        autoAmendments=0, lateIssues=[], pendingDecisions=[], authorAnswers={},
                        reviewScope="full", affectedChapters=[])
            self.schedule(run)
        else:
            raise WorkerError("当前状态无法执行此操作。", 409, "REVISION_STATE")
        return True

    async def apply(self, job_id, run, record, *, force=False):
        if record["status"] == "applied":
            return
        turn = record.get("candidateTurn")
        result = self.load(run, "v2-result", turn) if turn is not None else None
        if not result:
            raise WorkerError("候选稿不存在，无法写回正文。", 409, "REVISION_UNVERIFIED")
        candidate = result["candidate"]
        blocked_unforced = (not force) and bool(result["blocking"] or result.get("validationPending"))
        if (blocked_unforced or record["isMock"] or not record["changedCount"]
                or result["sourceVersion"] != record["sourceVersion"]
                or result["ledgerVersion"] != record["ledgerVersion"]
                or result["candidateHash"] != fingerprint(candidate)
                or result["candidateHash"] != fingerprint(list(self.draft(run).values()))):
            raise WorkerError("候选稿或验收版本需要重新核对。", 409, "REVISION_UNVERIFIED")
        original = {row["chapterNumber"]: row for row in self.store.snapshot(run)["chapters"]}
        await self.worker.request("revision-apply", jobId=job_id, payload={
            "revisionId": run, "sourceVersion": record["sourceVersion"],
            "changes": [row for row in candidate if row != original[row["chapterNumber"]]],
        })
        self.store.update(run, "applied")

    def export(self, job_id, run, kind):
        record = self.require(job_id, run)
        if record.get("workflowVersion", 1) == 1 or kind in ("original", "evaluation"):
            return self.export_archive(run, kind, record)
        snapshot = self.store.snapshot(run)
        title = snapshot["title"]
        name = self.export_name(title)
        if kind in ("ledger", "decisions") and record.get("ledgerVersion"):
            ledger = self.v2_ledger(run, record)
            value = ledger["decisions"] if kind == "decisions" else ledger
            return f"{name}-{kind}.json", compact_json({"version": record["ledgerVersion"], "data": value}), "application/json"
        versioned = re.fullmatch(r"(candidate|report)-(\d+)", kind)
        turn = int(versioned[2]) if versioned else record.get("candidateTurn")
        kind = versioned[1] if versioned else kind
        result = self.load(run, "v2-result", turn) if record.get("hasCandidate") else None
        if not result or kind not in ("candidate", "report"):
            raise WorkerError("该交付物暂不可下载。", 409, "REVISION_STATE")
        if kind == "candidate":
            label = "待核实候选稿" if result["blocking"] or result.get("validationPending") else "候选稿"
            chapters = {row["chapterNumber"]: row for row in result["candidate"]}
            return f"{name}-{label}.md", self.manuscript(title, chapters), "text/markdown"
        ledger = self.load(run, "v2-ledger", result["ledgerVersion"])
        return f"{name}-改稿记录.md", self.v2_report(title, result, ledger), "text/markdown"

    @staticmethod
    def export_name(title):
        return re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", title).strip(" ._")[:80] or "作品"

    @staticmethod
    def v2_report(title, result, ledger):
        original, candidate = result["metrics"]["original"], result["metrics"]["candidate"]
        labels = {"adopt": "采纳", "adapt": "改编", "skip": "略过", "needs_review": "待复核"}
        outcomes = {item["id"]: item for item in result["outcomes"]}
        lines = [
            f"# 《{title}》修订记录", "", "## 全局计划与量化",
            f"- 台账 v{result['ledgerVersion']}，正文 {original['chars']} → {candidate['chars']} 字符。",
            *[f"- {rule['id']} · {rule['decision']}" for rule in ledger["rules"]],
            *[f"- 「{phrase}」{count} → {candidate['phrases'][phrase]} 次。" for phrase, count in original["phrases"].items()
              if count or candidate["phrases"][phrase]],
            "", "## 逐条裁决与验收",
        ]
        for item in ledger["decisions"]:
            outcome = outcomes.get(item["id"])
            where = "、".join(str(n) for n in item["chapters"])
            lines.append(f"- {item['id']} · {labels.get(item['decision'], item['decision'])}"
                         f" · {where or '无指定章'}。{item['reason']}")
            if item.get("searchTerms"):
                matched = "、".join(map(str, item["matchedChapters"])) or "无"
                lines.append(f"  - 检索用语 {'、'.join(item['searchTerms'])}。原稿命中章：{matched}。")
            if item.get("quoteMatch") == "partial":
                lines.append("  - 原评估引文按片段定位，裁决依据以查证后的原文为准。")
            if outcome:
                lines.append(f"  - {outcome['status']} · {outcome['reason']}")
                for check in outcome.get("chapterOutcomes", []):
                    lines.append(f"    - 第 {check['chapter']} 章 · {check['status']} · {check['reason']}")
                    if evidence := check.get("candidateEvidence"):
                        lines.append(f"      - 引文「{evidence['quote']}」")
        lines += ["", "## 偏差、略过与待核实"]
        for item in result.get("validationPending", []):
            numbers = item.get("chapters") or ([item["chapter"]] if item.get("chapter") is not None else [])
            scope_label = "第 " + "、".join(map(str, numbers)) + " 章" if numbers else "范围待定"
            ids = item.get("issueIds") or ([item["id"]] if item.get("id") else [])
            anchor = f" 锚点「{item['anchor']}」。" if item.get("anchor") else ""
            lines.append(f"- 容错待复核 · {scope_label} · {'、'.join(ids) or '独立补丁'}。{item['reason']}{anchor}")
        lines.extend(f"- {item['id']} · {item['match']} · {item['reason']}" for item in ledger["decisions"]
                     if item["match"] != "matched" or item["decision"] == "skip")
        lines.extend(f"- 作者确认 {key} · {value}" for key, value in result["authorAnswers"].items())
        lines.extend(f"- 第 {item['chapter']} 章 · 修改{'章名' if item['field'] == 'title' else '正文'} · {item['reason']}"
                     for item in result["changes"])
        if reviews := result.get("repairReviews"):
            lines += ["", "### 自动返修复核"]
            latest = {item["id"]: item for item in reviews}
            for item in latest.values():
                status = "已修" if item["status"] == "fixed" else "待核实"
                lines.append(f"- {item['priority']} · 第 {item['chapter']} 章 · {status} · {item['problem']}。{item['reason']}")
                if proof := item.get("proof"):
                    lines.append(f"  - 修后引文「{proof['quote']}」")
        lines += ["", "### 余留问题"]
        remaining = remaining_findings(result)
        for priority in ("P0", "P1", "P2"):
            items = [item for item in remaining if item["priority"] == priority]
            lines.append(f"- {priority} 共 {len(items)} 项")
            for item in items:
                location = f"第 {item['chapter']} 章" if item["chapter"] is not None else "全书"
                lines.append(f"  - {location} · {item['origin']} · {item['problem']}")
                if item.get("pendingVerification"):
                    lines.append("    - 来自上一轮，当前候选稿尚待语义复核。")
                if item.get("quote"):
                    lines.append(f"    - 引文「{item['quote']}」")
        return "\n".join(lines) + "\n"
