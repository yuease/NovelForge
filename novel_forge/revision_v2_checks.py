"""Deterministic validation for revision workflow v2."""

from collections import Counter
from hashlib import sha256
import json
import re

from .revision_checks import (
    RevisionContentError, audited_evidence, chapter_number, fingerprint, issue_map,
    evidence_excerpt, indexed_text, nearby_excerpt, normalized, occurrences, quote_parts, required_list, risk,
    string, structural_review, valid_numbers, validate_ledger, validate_reference_chapter,
)

MAX_DEFERRED_IDS = 2
FINANCE = re.compile(r"finance|财务|账目|账款|余额|金额|收入|款项|还款|欠款|贷款|借款|债务|偿还|转账|结清|税后|流水|"
                     r"报酬|奖金|违约金|合同款|[0-9零一二三四五六七八九十百千万亿]+\s*(?:元|块钱|美元)|[¥￥$]", re.I)
ORDINARY_CATEGORIES = {"plot", "voice", "over_explaining", "formulaic_reaction", "shared_voice",
                       "static_situation", "repetitive_hooks"}


def financial_text(issue):
    return any(FINANCE.search(str(issue.get(key) or "")) for key in (
        "track", "category", "problem", "quote", "suggestion", "reason", "impact",
    )) or any(FINANCE.search(string(ref.get("quote"), 320)) for ref in (issue.get("evidence") or ())
              if isinstance(ref, dict))


def ordinary_decision(issue, answers, ledger, affected=(), proposal=None):
    if (issue["id"] in answers or issue.get("priority") not in ("P1", "P2")
            or (issue.get("category") or issue.get("track")) not in ORDINARY_CATEGORIES
            or financial_text(issue) or financial_text(proposal or {})):
        return False
    numbers = {issue.get("chapter"), *affected}
    return not (
        any(rule["track"] == "finance" and (not rule.get("chapters") or numbers.intersection(rule["chapters"]))
            for rule in ledger.get("rules", []))
        or any(financial_text({"quote": check["text"]}) and
               (not check.get("chapters") or numbers.intersection(check["chapters"])) for check in ledger.get("checks", []))
        or any(entry["chapter"] in numbers for flow in ledger.get("financialFlows", []) for entry in flow["entries"])
        or any(numbers.intersection(entry.get("chapters", [])) for entry in ledger.get("authorAmendments", []))
    )


def protected_ledger(*ledgers):
    return {key: [item for ledger in ledgers if ledger for item in ledger.get(key, [])]
            for key in ("rules", "checks", "financialFlows", "authorAmendments")}


def digest(value):
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def exact_passage(item, chapters, allowed):
    chapter = item.get("chapter")
    field = item.get("field", "content")
    quote = string(item.get("quote"), 320)
    if type(chapter) is not int or chapter not in allowed or field not in ("title", "content") or len(quote) < 6:
        return None
    text = chapters[chapter][field]
    at = text.find(quote)
    if at < 0 or text.find(quote, at + 1) >= 0:
        return None
    return {"chapter": chapter, "field": field, "quote": quote}


def verified_repair(item, chapters, editable, visible, ledger, origin):
    priority = item.get("priority")
    if priority not in ("P0", "P1") or item.get("requiresDecision") is not False:
        return None
    target = exact_passage(item, chapters, editable)
    problem, fix = string(item.get("problem"), 650), string(item.get("fix"), 850)
    basis, impact = item.get("basis"), item.get("affectedChapters")
    if not target or not problem or not fix or not isinstance(basis, list) or not 1 <= len(basis) <= 4:
        return None
    if not isinstance(impact, list) or any(type(n) is not int or n not in visible for n in impact):
        return None

    rules = {rule["id"]: rule for rule in ledger["rules"]}
    author = {entry["after"]: entry for entry in ledger.get("authorAmendments", [])}
    author.update({entry["text"]: entry for entry in ledger.get("authorDecisions", [])})
    checked = []
    for ref in basis:
        if not isinstance(ref, dict):
            return None
        chapter = target["chapter"]
        if isinstance(ref.get("ruleId"), str) and (rule := rules.get(ref["ruleId"])):
            if (rule["chapters"] and chapter not in rule["chapters"]) or ref.get("decision", rule["decision"]) != rule["decision"]:
                return None
            checked.append({"ruleId": rule["id"], "decision": rule["decision"]})
        elif isinstance(ref.get("authorQuote"), str) and (answer := author.get(ref["authorQuote"])):
            if chapter not in answer["chapters"]:
                return None
            checked.append({"authorQuote": ref["authorQuote"]})
        elif (passage := exact_passage(ref, chapters, visible)) and passage != target:
            checked.append(passage)
        else:
            return None
    impact = sorted({target["chapter"], *impact, *(ref["chapter"] for ref in checked if "chapter" in ref)})
    identifier = "RF-" + digest([target, checked, fix])[:16]
    return {
        **risk(target["chapter"], problem, target["quote"], priority, origin),
        "repair": {"id": identifier, "target": target, "basis": checked, "affectedChapters": impact,
                   "fix": fix, "beforeHash": digest(chapters[target["chapter"]])},
    }


def repair_feedback(findings):
    blockers = [item for item in findings if item["priority"] == "P0" and item["chapter"] is not None
                and (item.get("repair") or item["origin"] in ("structure", "ledger", "裁决"))]
    urgent = {item["chapter"] for item in blockers}
    targeted = [item for item in findings if item["priority"] == "P1" and item.get("repair")
                and (not urgent or item["chapter"] in urgent)]
    return blockers + targeted


def recheck_repairs(result, previous, candidate, allowed, ledger):
    if not previous:
        return [], []
    expected = {item["repair"]["id"]: item for item in previous}
    checked, unresolved, reviews = set(), [], []
    for reply in required_list(result, "repairChecks"):
        key, status = reply.get("id"), reply.get("status")
        if not isinstance(key, str) or key not in expected or key in checked or status not in ("fixed", "open"):
            raise RevisionContentError("返修复核结果缺少有效编号或状态。")
        checked.add(key)
        item = expected[key]
        reason = string(reply.get("reason"), 650)
        if not reason:
            raise RevisionContentError("返修复核需要具体依据。")
        repair = item["repair"]
        chapter = item["chapter"]
        if status == "fixed" and digest(candidate[chapter]) != repair["beforeHash"]:
            proof = exact_passage(reply, candidate, {chapter})
            if proof:
                reviews.append({"id": key, "chapter": chapter, "priority": item["priority"], "status": "fixed",
                                "problem": item["problem"], "reason": reason, "proof": proof})
                continue
            raise RevisionContentError("标为已修的问题缺少当前稿件中的核实引文。")
        carry = {**item, "problem": reason if status == "open" else item["problem"]}
        proposal = {**repair["target"], "priority": item["priority"], "problem": carry["problem"],
                    "fix": repair["fix"], "basis": repair["basis"], "affectedChapters": repair["affectedChapters"],
                    "requiresDecision": False}
        if checked_repair := verified_repair(proposal, candidate, {chapter}, allowed, ledger, item["origin"]):
            carry["repair"] = checked_repair["repair"]
        else:
            carry.pop("repair")
        unresolved.append(carry)
        reviews.append({"id": key, "chapter": chapter, "priority": item["priority"], "status": "open",
                        "problem": item["problem"], "reason": carry["problem"]})
    if checked != set(expected):
        raise RevisionContentError("语义审校遗漏已送修问题的复核。")
    return unresolved, reviews


def distinct_findings(findings):
    unique = {}
    for item in findings:
        repair = item.get("repair")
        key = repair["id"] if repair else (item["priority"], item["chapter"], item["problem"], item.get("quote"))
        unique.setdefault(key, item)
    return list(unique.values())


def remaining_findings(result):
    unique = {}
    for item in [*result.get("blocking", []), *result.get("findings", [])]:
        key = item["priority"], item["chapter"], item["problem"], item.get("quote", "")
        if key not in unique or item.get("repair"):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item["priority"], item["chapter"] or 0))


def preflight(rows, volumes):
    by_number = {row["chapterNumber"]: row for row in rows}
    rejoined = [by_number[n] for volume in volumes for n in volume["chapters"]]
    version = fingerprint(rows)
    if fingerprint(rejoined) != version:
        raise RevisionContentError("分卷后无法无损还原原稿。")
    defects, newline_variants = [], []
    for n, row in by_number.items():
        for field in ("title", "content"):
            value = row[field]
            hits = [m.start() for m in re.finditer(r"[\uFFFD\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", value)]
            if hits or not value.strip():
                defects.append({"chapter": n, "field": field, "offsets": hits[:50], "count": len(hits)})
            if "\r" in value:
                newline_variants.append({"chapter": n, "field": field})
    return {"sourceHash": version, "defects": defects, "newlineVariants": newline_variants}


def assessed_issues(report, chapters):
    issues = issue_map(report, chapters)
    for i, plan in enumerate(report.get("rewritePlan") or [], 1):
        if detail := string(plan, 1200):
            issues.append({"id": f"P{i:03d}", "match": "plan", "priority": "P1", "problem": detail, "quote": ""})
    return issues


def search_chapters(chapters, terms):
    return [
        n for n, chapter in chapters.items()
        if any(term in chapter[field] for term in terms for field in ("title", "content"))
    ]


def plan_evidence(issue, chapters, related, facts):
    text = issue["problem"]
    ranges = re.findall(r"第\s*([0-9]+(?:\s*[、，,至到~～–—-]\s*第?\s*[0-9]+)*)\s*章", text)
    mentioned = set()
    for group in ranges:
        for first, last in re.findall(r"([0-9]+)(?:\s*[-–—~～至到]\s*第?\s*([0-9]+))?", group):
            start, stop = int(first), int(last or first)
            mentioned.update(n for n in chapters if start <= n <= stop)
    terms = dict.fromkeys(part.strip() for pair in re.findall(r'“([^”]+)”|「([^」]+)」|"([^"]+)"', text)
                          for quote in pair if quote for part in re.split(r"[、/／；;…]+", quote)
                          if (4 if mentioned else 8) <= len(part.strip()) <= 160)
    refs = []
    for n in sorted(mentioned or chapters):
        fields = chapters[n]
        direct = []
        for field in ("title", "content"):
            raw = fields[field]
            for term in terms:
                at = raw.find(term)
                if at >= 0:
                    quote = raw[max(0, at - 16):at + len(term) + 16].strip()
                    if evidence := audited_evidence({"chapter": n, "field": field, "quote": quote}, chapters, {n}):
                        direct.append({**evidence, "field": field})
        related_passages = [ref for ref in related if ref["chapter"] == n] if n in mentioned else []
        seen = set()
        candidates = []
        for ref in [*direct, *related_passages]:
            if (key := (ref["field"], ref["quote"])) not in seen:
                candidates.append(ref)
                seen.add(key)
        if not candidates and n in mentioned:
            for fact in facts:
                if fact.get("chapter") == n and (evidence := audited_evidence(fact, chapters, {n})):
                    candidates.append({**evidence, "field": evidence.get("field", "content"), "context": fact.get("detail", "")})
                    break
        refs.extend(candidates[:2])
        if len(refs) >= 32:
            break
    return [{**ref, "id": f"{issue['id']}-Q{index}"} for index, ref in enumerate(refs[:32], 1)]


def located_evidence(issues, chapters, facts=()):
    """Carry actual manuscript excerpts from report localization into adjudication."""
    result = {}
    for issue in issues:
        if "claimedChapter" not in issue or issue["match"] in ("unmatched", "duplicate"):
            continue
        numbers = [issue["chapter"]] if issue["chapter"] is not None else issue["matches"][:5]
        key, fragments = quote_parts(issue["quote"])
        anchors = sorted({key, *fragments} - {""}, key=lambda item: (-len(item), item))
        refs = []
        for n in numbers:
            for field in ("title", "content"):
                raw = chapters[n][field]
                compact = normalized(raw)
                anchor = next((part for part in anchors if len(part) >= 6 and part in compact), None)
                if anchor is None:
                    continue
                first = raw.find(anchor)
                if first >= 0:
                    last = first + len(anchor)
                else:
                    start = compact.index(anchor)
                    positions = [match.start() for match in re.finditer(r"\S", raw)]
                    first, last = positions[start], positions[start + len(anchor) - 1] + 1
                quote = raw[first:last][:320].rstrip()
                if len(normalized(quote)) >= 6:
                    refs.append({"id": f"{issue['id']}-Q{len(refs) + 1}", "chapter": n, "field": field,
                                 "quote": quote, "context": raw[max(0, first - 120):min(last, first + 320) + 120]})
        if refs:
            result[issue["id"]] = refs
    localized_report = [ref for refs in result.values() for ref in refs]
    for issue in issues:
        if issue["match"] == "plan" and (refs := plan_evidence(issue, chapters, localized_report, facts)):
            result[issue["id"]] = refs
        if issue["match"] == "audit" and (source := audited_evidence(issue, chapters, set(chapters))):
            field = source.get("field", "content")
            quoted = normalized(source["quote"])
            aliases = [
                ref["id"] for ref in localized_report
                if ref["chapter"] == source["chapter"] and ref["field"] == field
                and (normalized(ref["quote"]) in quoted or quoted in normalized(ref["quote"]))
            ]
            result[issue["id"]] = [{**source, "field": field, "id": f"{issue['id']}-Q1", "aliases": aliases}]
    return result


def adjudication_evidence(ref, issue, sources, chapters):
    all_chapters = set(chapters)
    if "sourceId" in ref:
        selected = next((item for item in sources
                         if ref["sourceId"] == item["id"] or ref["sourceId"] in item.get("aliases", ())), None)
        if not selected or ("chapter" in ref and chapter_number(ref["chapter"]) != selected["chapter"]):
            return None
        if "field" in ref and ref["field"] != selected["field"]:
            return None
        return audited_evidence(selected, chapters, all_chapters)
    if evidence := audited_evidence(ref, chapters, all_chapters, punctuation=True):
        return evidence

    # A copied, abbreviated report quote can refer to its already located
    # passage. A different unsupported quote still fails validation.
    if issue.get("quoteMatch") != "partial":
        return None
    proposed, _ = quote_parts(string(ref.get("quote"), 4000))
    reported, _ = quote_parts(issue["quote"])
    comparable = lambda value: re.sub(r"…+|\.{3,}", "…", value)
    if not proposed or comparable(proposed) != comparable(reported):
        return None
    matches = [item for item in sources if item["chapter"] == chapter_number(ref.get("chapter"))
               and item["field"] == ref.get("field", "content")]
    return audited_evidence(matches[0], chapters, all_chapters) if len(matches) == 1 else None


def validated_plan(value, chapters, issues, answers, source_evidence=None, *, allow_deferred=False,
                   finance_context=None, trusted_deferred=()):
    all_sources = {ref["id"]: ref for refs in (source_evidence or {}).values() for ref in refs}
    ledger = validate_ledger(value, chapters, source_refs=all_sources)
    if ledger["unresolved"]:
        raise RevisionContentError("请将关键分歧写入对应裁决的 author 选项，其余意见给出裁决。")
    for rule in ledger["rules"]:
        ref = rule["evidence"][0]
        rule["id"] = "L-" + digest([rule["track"], ref])[:16]
    known = {item["id"]: item for item in issues}
    decisions, pending, evidence_failures = {}, [], {}
    for item in required_list(value, "decisions"):
        key = item.get("id")
        if not isinstance(key, str):
            raise RevisionContentError("裁决编号格式无效。")
        source = known.get(key)
        status = item.get("decision")
        reason = string(item.get("reason"), 900)
        if status == "skip" and not reason:
            reason = "本条略过，模型未提供具体理由。"
        if not source or key in decisions or not reason or status not in ("adopt", "adapt", "skip", "author"):
            raise RevisionContentError("裁决表有未知编号、重复或无效结论。")
        if source["match"] == "duplicate" and status != "skip":
            raise RevisionContentError(f"{key} 是重复意见，请保留略过依据。")
        refs = required_list({"evidence": item.get("evidence", [])}, "evidence")
        sources = (source_evidence or {}).get(key, [])
        evidence, rejected = [], []
        for ref in refs:
            validate_reference_chapter(ref, chapters)
            passage = adjudication_evidence(ref, source, sources, chapters)
            if passage is None and isinstance(ref.get("sourceId"), str) and ref["sourceId"] in all_sources:
                passage = adjudication_evidence(ref, source, [all_sources[ref["sourceId"]]], chapters)
            if passage is not None:
                evidence.append(passage)
            else:
                rejected.append(ref)
        affected = valid_numbers(item.get("chapters", []), chapters)
        if (status in ("adopt", "adapt") and not evidence) or (status == "author" and rejected):
            options = "、".join(ref["id"] for ref in sources)
            hint = f"可引用 {options}，或提交正确章号与连续原文。" if options else "请核对章号、字段和连续原文。"
            chapters_to_check = sorted({ref["chapter"] for ref in sources} |
                                       {n for ref in refs if (n := chapter_number(ref.get("chapter"))) in chapters})
            evidence_failures[key] = (hint, chapters_to_check[:16], [
                {"chapter": chapter_number(ref.get("chapter")), "sourceId": string(ref.get("sourceId"), 80),
                 "quote": string(ref.get("quote"), 320),
                 **({"nearby": excerpt} if (excerpt := evidence_excerpt(ref, chapters)) else {})}
                for ref in rejected[:8]
            ], item)
        coverage = {}
        if status in ("adopt", "adapt"):
            proposed = item.get("searchTerms") or []
            if not isinstance(proposed, list):
                raise RevisionContentError(f"{key} 的检索用语需要以列表返回。")
            terms = list(dict.fromkeys(term.strip() for term in proposed if isinstance(term, str) and term.strip()))
            if terms:
                matches = search_chapters(chapters, terms)
                affected = sorted(set(affected) | set(matches))
                coverage = {"searchTerms": terms, "matchedChapters": matches}
            if item.get("wholeBook") is True and not terms:
                affected = sorted(chapters)
        if status in ("adopt", "adapt") and not affected:
            raise RevisionContentError(f"{key} 需给出实际受影响章节和原文依据。")
        if status == "author":
            options = item.get("options")
            if key in answers or item.get("impact") not in ("plot", "continuity", "finance"):
                raise RevisionContentError(f"{key} 应按原文或作者已有口径直接裁决。")
            if not isinstance(options, list) or len(options) < 2 or not all(string(option, 500) for option in options):
                raise RevisionContentError(f"{key} 缺少可由作者选择的口径。")
            pending.append({"id": key, "problem": reason, "options": [string(option, 500) for option in options[:4]],
                            "chapters": affected, "evidence": evidence})
        decisions[key] = {**source, "decision": status, "reason": reason, "evidence": evidence,
                          "chapters": affected, **coverage}
    blueprint = []
    for index, item in enumerate(required_list(value, "blueprint"), 1):
        kind = item.get("kind")
        raw_target = item.get("chapters")
        target = valid_numbers([] if raw_target is None else raw_target, chapters)
        change = string(item.get("change"), 1400)
        problems = []
        if kind not in ("plot", "voice"):
            problems.append("kind 必须为 plot 或 voice")
        if not target:
            problems.append("chapters 至少需要一个改写章号")
        if not change:
            problems.append("change 缺少具体改法")
        if problems:
            raise RevisionContentError(f"剧情蓝图第 {index} 条无效：{'；'.join(problems)}。", blueprintIndex=index)
        target_numbers = set(target)
        preserve = [n for n in valid_numbers(item.get("preserve", []), chapters) if n in target_numbers]
        joins = valid_numbers(item.get("joins", []), chapters)
        blueprint.append({"kind": kind, "chapters": target, "preserve": preserve, "joins": joins, "change": change})
    for row in trusted_deferred:
        key = row["id"]
        if key not in known or key in decisions:
            raise RevisionContentError(f"待复核裁决 {key} 重复或不存在。")
        decisions[key] = row
    missing = set(known) - set(decisions)
    failures = sorted(set(evidence_failures) | missing)
    context = protected_ledger(ledger, finance_context)

    def can_defer(key):
        decision = decisions.get(key, {})
        failure = evidence_failures.get(key)
        affected = set(decision.get("chapters", []))
        affected.update(ref["chapter"] for ref in (source_evidence or {}).get(key, []))
        if failure:
            affected.update(ref["chapter"] for ref in failure[2] if ref["chapter"] in chapters)
        return decision.get("decision") != "author" and ordinary_decision(
            known[key], answers, context, affected, failure[3] if failure else None,
        )

    if (failures and allow_deferred and len(failures) <= MAX_DEFERRED_IDS and len(known) > len(failures)
            and all(can_defer(key) for key in failures)):
        for key in failures:
            chapter = known[key].get("chapter")
            chapters_to_check = decisions.get(key, {}).get("chapters") or ([chapter] if chapter in chapters else [])
            decisions[key] = {
                **known[key], "decision": "needs_review", "evidence": [], "chapters": chapters_to_check,
                "reason": "台账未找到可核实的原文证据。" if key in evidence_failures else "模型漏报该裁决。",
            }
    elif failures:
        messages = ["裁决表遗漏 " + "、".join(sorted(missing)[:16])] if missing else []
        for key, (hint, numbers, refs, _) in list(evidence_failures.items())[:8]:
            near = next((ref for ref in refs if "nearby" in ref), None)
            near_text = f"第 {near['chapter']} 章附近原文「{near['nearby']['quote'][:100]}」。" if near else ""
            messages.append(f"{key} 的裁决证据无法在原稿核实。{hint}{near_text}")
        first = next(iter(evidence_failures), None)
        raise RevisionContentError(
            " ".join(messages), check="decision_evidence" if first else "decision_batch_ids",
            missingDecisionIds=sorted(missing), unsupportedDecisionIds=sorted(evidence_failures),
            **({"issueId": first, "chapters": evidence_failures[first][1],
                "rejected": evidence_failures[first][2]} if first else {}),
        )
    ledger.update(blueprint=blueprint, decisions=list(decisions.values()), pending=pending)
    return ledger


def validated_ledger_core(value, chapters, source_evidence=None):
    core = {key: required_list(value, key) for key in ("rules", "checks", "financialFlows", "blueprint")}
    validated_plan({**core, "unresolved": value.get("unresolved", []), "decisions": []}, chapters, [], {}, source_evidence)
    return core


def validated_decision_batch(value, chapters, issues, answers, source_evidence, *, allow_deferred=False,
                             finance_context=None):
    items = required_list(value, "decisions")
    expected = {item["id"] for item in issues}
    counts = Counter(item["id"] for item in items if isinstance(item.get("id"), str) and item["id"].strip())
    missing = expected - counts.keys()
    errors = [f"{label}：{'、'.join(sorted(ids))}" for label, ids in (
        ("缺失 ID", missing if not allow_deferred else []), ("多余 ID", counts.keys() - expected),
        ("重复 ID", {key for key, count in counts.items() if count > 1}),
    ) if ids]
    if sum(counts.values()) != len(items):
        errors.append("存在无效裁决编号")
    if errors:
        raise RevisionContentError("本批裁决编号有误。" + "；".join(errors) + "。", check="decision_batch_ids")
    by_id = {
        item["id"]: {**item, "evidence": []} if item.get("decision") == "skip" and "evidence" not in item else item
        for item in items
    }
    decisions = [by_id[issue["id"]] for issue in issues if issue["id"] in by_id]
    plan = validated_plan({"rules": [], "checks": [], "financialFlows": [], "blueprint": [], "decisions": decisions},
                          chapters, issues, answers, source_evidence, allow_deferred=allow_deferred,
                          finance_context=finance_context)
    return [row if row["decision"] == "needs_review" else by_id[row["id"]] for row in plan["decisions"]]


def ledger_entry_id(kind, item):
    return item["id"] if kind == "rules" else "C-" + digest(item)[:16]


def merge_semantic_delta(value, old, chapters, issues, answers, source_evidence, targets):
    allowed = set(targets)
    additions = {}
    for kind in ("rules", "checks"):
        additions[kind] = []
        for item in required_list(value, kind):
            numbers = valid_numbers(item.get("chapters", []), chapters) or targets
            if not allowed.issuperset(numbers):
                raise RevisionContentError(f"增量 {kind} 只能作用于本次联动章节 {targets}。")
            additions[kind].append({**item, "chapters": numbers})
    decisions = required_list(value, "decisions")
    delta = validated_plan({**additions, "financialFlows": [], "blueprint": [], "decisions": decisions,
                            "unresolved": value.get("unresolved", [])},
                           chapters, issues, answers, source_evidence)
    old_ids = {item["id"] for item in old["decisions"]}
    for decision in delta["decisions"]:
        if decision["id"] in old_ids or set(decision["chapters"]) - allowed:
            raise RevisionContentError(f"{decision['id']} 是旧裁决或超出联动章节 {targets}，请只返回本次局部新增裁决。")

    ledger = {**old, "decisions": [*old["decisions"], *delta["decisions"]], "pending": delta["pending"]}
    for kind, rows in additions.items():
        original = old[kind]
        ids = [ledger_entry_id(kind, item) for item in original]
        result = list(original)
        replaced = set()
        for raw, item in zip(rows, delta[kind]):
            key = raw.get("replaces")
            if key is not None:
                if not isinstance(key, str) or ids.count(key) != 1 or key in replaced:
                    raise RevisionContentError(f"增量 {kind} 的替换编号无效或重复：{string(key, 80)}。")
                index = ids.index(key)
                if set(original[index]["chapters"] or chapters) - allowed:
                    raise RevisionContentError(f"旧台账条目 {key} 涉及本次联动范围以外的章节，请保留旧条目。")
                result[index] = {**item, "id": key} if kind == "rules" else item
                replaced.add(key)
            elif item not in result:
                if kind == "rules" and any(entry["id"] == item["id"] for entry in result):
                    raise RevisionContentError(f"增量规则 {item['id']} 与旧规则编号相同，请声明 replaces。")
                result.append(item)
        ledger[kind] = result
    return ledger


def scope(ledger, numbers):
    target = set(numbers)

    def related(item):
        chapters = item["chapters"]
        return not chapters or bool(target.intersection(chapters))

    return {
        "rules": [item for item in ledger["rules"] if related(item)],
        "checks": [item for item in ledger["checks"] if related(item)],
        "financialFlows": [item for item in ledger["financialFlows"]
                           if target.intersection(entry["chapter"] for entry in item["entries"])],
        "blueprint": [item for item in ledger["blueprint"] if related(item) or target.intersection(item["joins"])],
        "authorAmendments": [item for item in ledger.get("authorAmendments", []) if related(item)],
        "issues": [item for item in ledger["decisions"] if item["decision"] in ("adopt", "adapt") and related(item)],
    }


def related_chapters(scoped):
    linked = {ref["chapter"] for item in scoped["rules"] for ref in item["evidence"]}
    linked.update(n for item in scoped["rules"] for n in item["chapters"])
    linked.update(row["chapter"] for item in scoped["financialFlows"] for row in item["entries"])
    linked.update(n for item in scoped["blueprint"] for n in [*item["chapters"], *item["joins"]])
    linked.update(n for item in scoped["issues"] if not item.get("searchTerms") for n in item["chapters"])
    return sorted(linked)


def chapter_checks(issues, numbers=None):
    """Give a multi-chapter decision a separately reviewable item in each chapter."""
    selected = set(numbers) if numbers is not None else None
    checks = []
    for issue in issues:
        for n in issue["chapters"]:
            if selected is None or n in selected:
                key = f"{issue['id']}@{n}" if len(issue["chapters"]) > 1 else issue["id"]
                checks.append({**issue, "id": key, "decisionId": issue["id"], "chapter": n, "chapters": [n]})
    return checks


def aggregate_chapter_checks(issues, outcomes):
    grouped = {}
    for item in outcomes:
        grouped.setdefault(item.get("decisionId", item["id"]), []).append(item)
    combined = []
    for issue in issues:
        verdicts = grouped[issue["id"]]
        if len(issue["chapters"]) == 1:
            combined.append(verdicts[0])
            continue
        pending = [row["chapter"] for row in verdicts if row["status"] == "open"]
        status = "open" if pending else "fixed" if any(row["status"] == "fixed" for row in verdicts) else "not_applicable"
        reason = ("仍待核验章节：" + "、".join(map(str, pending))) if pending else f"已逐章核验 {len(verdicts)} 章。"
        combined.append({**issue, "status": status, "reason": reason, "chapterOutcomes": [
            {key: row[key] for key in ("chapter", "status", "reason", "candidateEvidence") if key in row}
            for row in verdicts
        ]})
    return combined


def unique_at(text, anchor):
    if not isinstance(anchor, str) or not anchor.strip():
        raise RevisionContentError("替换锚点为空。")
    hits = list(occurrences(text, anchor))
    if len(hits) == 1:
        return hits[0], hits[0] + len(anchor)
    if not hits:
        key, _ = indexed_text(anchor)
        if sum(char.isalnum() for char in key) >= 6:
            compact, offsets = indexed_text(text)
            hits = list(occurrences(compact, key))
            if len(hits) == 1:
                start = hits[0]
                return offsets[start], offsets[start + len(key) - 1] + 1
    hint = nearby_excerpt(text, anchor) if not hits else None
    near = f"。请复制附近原文「{hint['quote']}」作锚点。" if hint else ""
    raise RevisionContentError(f"替换锚点未唯一命中（{len(hits)} 次命中）：「{anchor[:90]}」{near}",
                               check="edit_anchor", anchor=anchor[:160], matches=len(hits), nearby=hint)


def edit_patches(value, original, allowed, required, *, allow_deferred=False, protected_chapters=(), protected_ids=()):
    expected = set(required)
    patches, failures, edits = [], {}, {}
    for index, patch in enumerate(required_list(value, "patches"), 1):
        n, field, mode = patch.get("chapter"), patch.get("field"), patch.get("mode")
        if type(n) is not int or n not in allowed or field not in ("title", "content") or mode not in ("exact", "range"):
            raise RevisionContentError(f"第 {index} 条补丁的章节、字段或锚定方式无效。")
        after, reason, ids = patch.get("after"), string(patch.get("reason"), 450), patch.get("issueIds", [])
        if not isinstance(after, str) or not reason or "\\ufffd" in after:
            raise RevisionContentError(f"第 {n} 章替换内容或说明无效。")
        if not isinstance(ids, list) or any(not isinstance(key, str) or key not in expected for key in ids):
            raise RevisionContentError("补丁引用了批次以外的裁决项。")
        if mode == "range" and field != "content":
            raise RevisionContentError("起止锚点只适用于本章正文。")
        anchors = [patch.get("before")] if mode == "exact" else [patch.get("startAnchor"), patch.get("endAnchor")]
        if any(not isinstance(anchor, str) or not anchor.strip() for anchor in anchors):
            raise RevisionContentError(f"第 {index} 条补丁的替换锚点为空。")
        patches.append((index, n, field, anchors, after, reason, ids))

    disposition = {}
    for item in required_list(value, "coverage"):
        validate_reference_chapter(item, original)
        key, outcome = item.get("id"), item.get("outcome")
        reason = string(item.get("reason"), 500)
        if (not isinstance(key, str) or key not in expected or key in disposition
                or outcome not in ("patched", "already_met", "needs_review") or not reason):
            raise RevisionContentError("批次裁决覆盖记录无效。")
        if outcome == "already_met" and not audited_evidence(item, original, allowed):
            disposition[key] = {"id": key, "outcome": "needs_review", "reason": reason,
                                "reviewNote": "编辑认为无需修改，缺少可核验引文，请独立复核。"}
        else:
            disposition[key] = {**item, "reason": reason}
    if expected != set(disposition):
        raise RevisionContentError("批次漏报裁决覆盖：" + "、".join(sorted(expected - set(disposition))))
    proposed_ids = {key for *_, ids in patches for key in ids}
    for key, row in disposition.items():
        if row["outcome"] == "patched" and key not in proposed_ids:
            raise RevisionContentError(f"{key} 被标成已改，但缺少对应补丁。")

    errors = []
    for index, n, field, anchors, after, reason, ids in patches:
        text = original[n][field]
        try:
            begin, first_end = unique_at(text, anchors[0])
            end = first_end
            if len(anchors) == 2:
                finish, end = unique_at(text, anchors[1])
                if first_end > finish:
                    raise RevisionContentError(f"第 {n} 章的起止锚点顺序错误或重叠。",
                                               check="edit_anchor", anchor=" … ".join(anchors)[:160])
        except RevisionContentError as exc:
            failures[index] = str(exc)
            errors.append(RevisionContentError(f"第 {n} 章第 {index} 条补丁。{exc}", **exc.details))
            continue
        if text[begin:end] == after:
            raise RevisionContentError(f"第 {n} 章替换前后相同。")
        if not (text[:begin] + after + text[end:]).strip():
            raise RevisionContentError(f"第 {n} 章的{field}将被清空。")
        edits.setdefault((n, field), []).append((begin, end, index, after, reason))

    overlap = set()
    for (n, _), spans in edits.items():
        ordered = sorted(spans)
        for i, (begin, end, index, *_) in enumerate(ordered):
            for next_begin, _, other, *_ in ordered[i + 1:]:
                if next_begin >= end:
                    break
                overlap.update((index, other))
                message = f"第 {n} 章第 {index}、{other} 条补丁的替换区间重叠。"
                failures.setdefault(index, message)
                failures.setdefault(other, message)
                errors.append(RevisionContentError(message, check="edit_overlap"))

    deferred = []
    if failures:
        small = (len(failures) == 2 and overlap == failures.keys()) if overlap else len(failures) <= 2
        if not allow_deferred or not small:
            raise errors[0]
        for index, n, field, anchors, after, reason, ids in patches:
            if index not in failures:
                continue
            if (n in protected_chapters or set(ids).intersection(protected_ids)
                    or financial_text({"quote": " ".join(anchors), "problem": after, "reason": reason})):
                raise errors[0]
            failure = failures[index][:450]
            excerpt = " … ".join(anchors)[:160]
            deferred.append({"chapter": n, "field": field, "patchIndex": index, "issueIds": ids,
                             "anchor": excerpt, "reason": failure})
            for key in ids:
                disposition[key] = {"id": key, "outcome": "needs_review", "reason": failure,
                                    "reviewNote": f"未应用锚点「{excerpt}」"}

    changed, logs = {}, []
    for (n, field), changes in edits.items():
        text = original[n][field]
        for begin, end, index, after, reason in sorted(changes, reverse=True):
            if index in failures:
                continue
            logs.append((index, {"chapter": n, "field": field, "reason": reason, "before": text[begin:end], "after": after}))
            text = text[:begin] + after + text[end:]
        if not text.strip():
            raise RevisionContentError(f"第 {n} 章的{field}将被清空。")
        if text != original[n][field]:
            changed.setdefault(n, dict(original[n]))[field] = text
    return {"changed": list(changed.values()), "log": [row for _, row in sorted(logs)], "coverage": disposition,
            **({"deferred": deferred} if deferred else {})}


def hard_review(original, candidate, ledger):
    findings = structural_review(original, candidate, ledger)
    for n, chapter in candidate.items():
        if any(re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", chapter[field]) for field in ("title", "content")):
            findings.append(risk(n, "仍有异常控制字符。"))
        if n - 1 in candidate:
            end = normalized(candidate[n - 1]["content"][-400:])
            start = normalized(chapter["content"][:400])
            if any(len(end) >= size and end[-size:] == start[:size] for size in range(50, min(len(end), len(start)) + 1)):
                findings.append(risk(n, "章首与上一章结尾有长段重复，需确认是否为有意回顾。", priority="P1"))
    return findings


def metrics(original, candidate, ledger, common):
    phrases = set(common) | {check["text"] for check in ledger["checks"]}
    result = {}
    for label, chapters in (("original", original), ("candidate", candidate)):
        counts = Counter()
        for chapter in chapters.values():
            text = chapter["title"] + "\n" + chapter["content"]
            for phrase in phrases:
                counts[phrase] += text.count(phrase)
        result[label] = {"chars": sum(len(c["content"]) for c in chapters.values()), "phrases": dict(sorted(counts.items()))}
    return result
