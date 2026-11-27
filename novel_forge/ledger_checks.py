"""Check ledger arithmetic against amounts and account references in the chapter."""

import re
import unicodedata
from copy import deepcopy
from decimal import Decimal

from .long_memory import story_ledger_label_key
from .text_evidence import locate_quote, normalize_quote


_DIGITS = dict(zip("零一二三四五六七八九", range(10))) | {"〇": 0, "两": 2}
_SCALES = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
_NUMBER = r"(?:[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿半]+)"
_SIGNED = rf"[+-]?[ \t]*{_NUMBER}(?:[万亿][零〇一二两三四五六七八九十百千万亿半]*)?"
_UNITS = (
    "人民币", "块钱", "元", "块", "两银", "两", "斤", "石", "文", "贯", "吨", "积分",
    "枚", "银圆", "级", "点", "分", "角", "钱", "匹", "银元", "公斤", "千克", "克", "铜钱",
)
_BASE_UNITS = {"人民币": "元", "块钱": "元", "块": "元"}
_UNIT = r"[a-zA-Z\u3400-\u9fff]{1,12}"
_EQUALITY = re.compile(
    rf"(?P<left>{_SIGNED})[ \t]*(?P<from>{_UNIT})[ \t]*(?:=|等于|折合|兑换)[ \t]*"
    rf"(?P<right>{_SIGNED})[ \t]*(?P<to>{_UNIT})"
)
_DIALOGUE = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"')
_PANEL = re.compile(r"【[^】]*】")
_SENTENCE = re.compile(r"[。！？!?\n；;]+|(?<!\d)\.|\.(?!\d)")
_BALANCE = re.compile(r"(?:余额|只剩|仅剩|现余|账户里剩|账上余|共有)[ \t]*(?:为|是|还有|只剩|仅剩|下|了)?[ \t]*[:=]?[ \t]*")
_ACCOUNT = re.compile(r"账上|账面|账里|账户里|账户余额")
_NET = re.compile(r"(?:本章|本轮|本次)?(?:合计|累计)?净(?:变动|收入|支出|额)[ \t]*[:=]?[ \t]*")
_OUTFLOW = re.compile(r"支出|花费|扣除|支付|买入|赔付|付出|花了|花掉|扣了|缴纳|消耗|给了")
_INFLOW = re.compile(r"收入|进账|卖得|到账|入账|收了|收到|赚得|赚了")
_TRANSACTION = re.compile(r"净变动|净额|入账|进账|卖[^，,。.!！？;\n]{0,12}进账|花|付|扣|买|转|收|入|卖|借|还|赔|损|缴|耗|给了")
_QUALIFIED = re.compile(r"如果|假如|倘若|若是|预计|预估|至少|超过|约莫|大约|原本|当初|此前|期初|起初|先前|曾经|并非|不是")
_TOTAL = re.compile(r"(?:加起来|一共|合计|总计|共)[ \t]*(?:为|是)?[ \t]*[:=]?[ \t]*")
_RATE = re.compile(r"每|单价|×|\*|倍|折|约|余|上下|左右|至少|超过")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _number(text):
    text = text.strip()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-").strip()
    if re.fullmatch(r"[0-9,.]+", text):
        return sign * Decimal(text.replace(",", ""))
    # Split on large scales first so 十二万三千 and 一万亿 keep their sections.
    for scale in ("亿", "万"):
        if scale in text:
            head, tail = text.split(scale, 1)
            if scale in tail:
                return None
            head = _number(head) if head else 1
            tail = _number(tail) if tail else 0
            return None if head is None or tail is None else sign * (head * _SCALES[scale] + tail)
    total, digits = Decimal(0), ""
    for char in text:
        if char in _DIGITS:
            digits += str(_DIGITS[char])
        elif char == "半":
            if digits or not text.endswith("半"):
                return None
            total += Decimal("0.5")
        else:
            total += (int(digits) if digits else 1) * _SCALES[char]
            digits = ""
    return sign * (total + int(digits or "0"))


def _amount_pattern(units):
    names = "|".join(map(re.escape, sorted(set(units), key=lambda unit: (-len(unit), unit))))
    return re.compile(rf"(?<![0-9.])(?P<number>{_SIGNED})[ \t]*(?P<unit>{names})")


def _converted(number, unit, conversions):
    unit = _BASE_UNITS.get(unit, unit)
    visited = set()
    while unit in conversions and unit not in visited:
        visited.add(unit)
        unit, factor = conversions[unit]
        number *= factor
    return Decimal(number), unit


def _parsed(match, conversions):
    if match and (number := _number(match["number"])) is not None:
        return _converted(number, match["unit"], conversions)
    return None


def parse_amount(text: str, conversions: dict | None = None) -> tuple[Decimal, str] | None:
    if not isinstance(text, str):
        return None
    text = unicodedata.normalize("NFKC", text).strip()
    conversions = conversions or {}
    units = (*_UNITS, *conversions, *(unit for unit, _ in conversions.values()))
    match = _amount_pattern(units).fullmatch(text)
    if match is None:
        if re.fullmatch(_SIGNED, text) and (number := _number(text)) is not None:
            return Decimal(number), ""
        match = re.fullmatch(rf"(?P<number>{_SIGNED})[ \t]*(?P<unit>{_UNIT})", text)
    return _parsed(match, conversions)


def unit_conversions(book_plan) -> dict[str, tuple[str, Decimal]]:
    plan = _dict(book_plan)
    groups = (
        _dict(plan.get("canonLocks")).get("currencyAndEra"),
        _dict(plan.get("worldview")).get("coreRules"),
        _dict(plan.get("styleBible")).get("domainTaboos"),
    )
    result = {}
    for group in groups:
        for rule in group if isinstance(group, list) else [group]:
            if not isinstance(rule, str):
                continue
            for match in _EQUALITY.finditer(unicodedata.normalize("NFKC", rule)):
                left, right = _number(match["left"]), _number(match["right"])
                source, target = (_BASE_UNITS.get(match[key], match[key]) for key in ("from", "to"))
                # Common units can be followed by prose in the same clause.
                if target not in _UNITS:
                    target = next((unit for unit in sorted(_UNITS, key=len, reverse=True)
                                   if target.startswith(unit)), target)
                    target = _BASE_UNITS.get(target, target)
                if left and right and left > 0 and right > 0 and source != _converted(1, target, result)[1]:
                    result.setdefault(source, (target, Decimal(right) / Decimal(left)))
    return result


def ledger_label_aliases(label: str, book_plan=None) -> list[str]:
    if not isinstance(label, str) or not label.strip():
        return []
    key = story_ledger_label_key(label)
    core = re.sub(r"(?:可用余额|账户余额|账户|账目|账款|账本|余额|账簿|账)$", "", key)
    terms = [label, key, core, *re.split(r"[\s·/：:（）()、_-]+", label)]
    aliases = _dict(_dict(book_plan).get("canonLocks")).get("identityAliases")
    for row in _rows(aliases):
        name, alias = row.get("character"), row.get("alias")
        for source, target in ((name, alias), (alias, name)):
            if isinstance(source, str) and len(source) >= 2 and isinstance(target, str) and source in key:
                terms.extend((key.replace(source, target), core.replace(source, target)))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2 or term == label))


def _account_pattern(label, plan):
    aliases = ledger_label_aliases(label, plan)
    return re.compile("|".join(
        r"\s*".join(map(re.escape, normalize_quote(alias)))
        for alias in sorted(aliases, key=len, reverse=True)
    ) or r"(?!)", re.I)


def _passages(text):
    start = 0
    for mark in _SENTENCE.finditer(text):
        if text[start:mark.start()].strip():
            yield start, text[start:mark.start()]
        start = mark.end()
    if text[start:].strip():
        yield start, text[start:]


def _amounts_for(view, conversions):
    prior_units = {amount[1] for row in _rows(view.get("ledgerBalances"))
                   if (amount := parse_amount(row.get("value"))) and amount[1]}
    return _amount_pattern((*_UNITS, *prior_units, *conversions, *(unit for unit, _ in conversions.values())))


def _near(pattern, text, offset, distance):
    start = max(0, offset - distance)
    return bool(pattern.search(text, start, min(len(text), offset + distance)))


def _nearest_account(text, offset, accounts):
    candidates = sorted(
        (offset - hit.end() if hit.end() <= offset else 40 + max(0, hit.start() - offset),
         -len(hit[0]), key)
        for key, account in accounts.items()
        for hit in account.finditer(text, max(0, offset - 40), min(len(text), offset + 40))
    )
    if not candidates:
        return None
    distance, length, key = candidates[0]
    return key if all(other == key for gap, size, other in candidates if (gap, size) == (distance, length)) else None


def numeric_claims(chapter_text: str, view: dict, book_plan=None, *, limit=8) -> list[str]:
    balances = _rows(view.get("ledgerBalances"))
    if limit <= 0 or not balances:
        return []
    pattern = _amounts_for(view, unit_conversions(book_plan))
    accounts = [(row, _account_pattern(row.get("label"), book_plan)) for row in balances]
    _, _, narration, _ = _chapter_evidence(chapter_text)
    lines = []
    for offset, sentence in _passages(narration):
        area_start = max(0, offset - 40)
        preceding = unicodedata.normalize("NFKC", narration[area_start:offset])
        body = unicodedata.normalize("NFKC", sentence)
        for row, account in accounts:
            for mention in pattern.finditer(body):
                if not (_near(account, body, mention.start(), 40)
                        or (_BALANCE.search(body) or _TRANSACTION.search(body))
                        and _near(account, preceding + body, len(preceding) + mention.start(), 40)
                        or len(accounts) == 1 and _near(_ACCOUNT, body, mention.start(), 40)):
                    continue
                excerpt = " ".join(sentence[max(0, mention.start() - 65):mention.end() + 55].split())
                line = f"账户「{row['label']}」(章前前值 {row['value']}) ← 本章提及: 「{excerpt}」"
                if line not in lines:
                    lines.append(line)
                if len(lines) >= limit:
                    return lines
                break
    return lines


def _balance_mentions(text, amounts, conversions):
    for marker in _BALANCE.finditer(text):
        prefix = _clause_prefix(text, marker.start())
        amount = amounts.match(text, marker.end())
        if (value := _parsed(amount, conversions)) and not _QUALIFIED.search(prefix):
            if not re.match(r"左右|上下|多|余|以上|以下", text[amount.end():]):
                yield marker.start(), amount.end(), value


def _clause_prefix(text, end):
    return re.split(r"[，,。！？!?；;\n]|(?<!\d)\.|\.(?!\d)", text[max(0, end - 30):end])[-1]


def _transaction_amounts(text, amounts, conversions):
    for marker in _NET.finditer(text):
        amount = amounts.match(text, marker.end())
        if amount and amount["number"].lstrip().startswith(("+", "-")) and not _QUALIFIED.search(_clause_prefix(text, marker.start())):
            if value := _parsed(amount, conversions):
                yield value, True
    for verb in re.finditer(rf"{_OUTFLOW.pattern}|{_INFLOW.pattern}", text):
        prefix = _clause_prefix(text, verb.start())
        if _QUALIFIED.search(prefix) or re.search(r"(?:没有|从未|未曾|尚未|并未|没|未)$", prefix):
            continue
        gap = re.match(r"[ \t]*(?:了|共|为|是)?[ \t]*[:=]?[ \t]*", text[verb.end():])
        amount = amounts.match(text, verb.end() + gap.end())
        if value := _parsed(amount, conversions):
            number, unit = value
            yield (-abs(number) if _OUTFLOW.fullmatch(verb[0]) else abs(number), unit), False


def _dialogue_spans(text):
    panels = list(_PANEL.finditer(text))
    return [(match.start(), match.end()) for match in _DIALOGUE.finditer(text)
            if not any(panel.start() < match.start() and match.end() < panel.end() for panel in panels)]


def _chapter_evidence(text):
    raw_dialogue = _dialogue_spans(text)
    source, normalized_dialogue, narration = [], [], []
    cursor = offset = 0
    for start, end in raw_dialogue:
        prose = text[cursor:start]
        normalized = normalize_quote(prose)
        spoken = normalize_quote(text[start:end])
        offset += len(normalized)
        normalized_dialogue.append((offset, offset + len(spoken)))
        offset += len(spoken)
        source.extend((normalized, spoken))
        narration.extend((prose, " " * (end - start)))
        cursor = end
    source.append(normalize_quote(text[cursor:]))
    narration.append(text[cursor:])
    return "".join(source), normalized_dialogue, "".join(narration), raw_dialogue


def _proof_excerpt(source, quote, dialogue):
    if not isinstance(quote, str) or not (proof := locate_quote(quote, source)):
        return None, False
    # A speaker can quote the same wording that later appears in narration.
    # Prefer the fully unquoted occurrence, if present.
    start = source.find(proof)
    spoken = False
    while start >= 0:
        spoken = any(start < end and begin < start + len(proof) for begin, end in dialogue)
        if not spoken:
            break
        start = source.find(proof, start + 1)
    return proof, spoken


def is_dialogue_evidence(chapter_text, quote):
    source, dialogue, _, _ = _chapter_evidence(chapter_text)
    return _proof_excerpt(source, quote, dialogue)[1]


def _additive_warnings(text, amounts, conversions):
    previous = previous_raw = ""
    for _, raw in _passages(text):
        sentence = unicodedata.normalize("NFKC", raw)
        for marker in _TOTAL.finditer(sentence):
            total = _parsed(amounts.match(sentence, marker.end()), conversions)
            if total is None:
                continue
            prefix = sentence[:marker.start()]
            parts = [_parsed(hit, conversions) for hit in amounts.finditer(prefix)]
            evidence = sentence
            quote = raw
            if len(parts) < 2 and previous:
                evidence = previous + "。" + sentence
                quote = previous_raw + "。" + raw
                parts = [_parsed(hit, conversions) for hit in amounts.finditer(previous)] + parts
            if (len(parts) >= 2 and all(part and part[1] == total[1] for part in parts)
                    and not _RATE.search(evidence) and sum(part[0] for part in parts) != total[0]):
                yield {"type": "ledger_arithmetic", "severity": "warning", "rule": "additive_total",
                       "quote": quote.strip()[:160], "message": "正文列出的分项金额与合计数不一致。"}
                break
        previous, previous_raw = sentence, raw


def assess_ledger_delta(delta, chapter_text, view, *, book_plan=None) -> dict:
    cleaned = deepcopy(delta)
    issues, warnings = [], []
    prior = {key: row for row in _rows(view.get("ledgerBalances"))
             if (key := story_ledger_label_key(row.get("label")))}
    accounts = {key: _account_pattern(row["label"], book_plan) for key, row in prior.items()}
    alias_keys = {}
    for key, before in prior.items():
        for alias in ledger_label_aliases(before["label"], book_plan):
            alias_keys.setdefault(story_ledger_label_key(alias), set()).add(key)
    conversions = unit_conversions(book_plan)
    amounts = _amounts_for(view, conversions)
    source, dialogue, narration, raw_dialogue = _chapter_evidence(chapter_text)
    prose = [(offset, unicodedata.normalize("NFKC", line)) for offset, line in _passages(narration)]
    memory = _dict(_dict(cleaned).get("memory"))
    changes = set()
    seen = set()

    def report(quote, message, *, before=None, rule, hard=False):
        if before:
            label = before["label"]
            ref = {"ledgerRef": {"kind": "balance", "key": label}, "sourceChapter": before.get("updatedAtChapter")}
        else:
            label, ref = "", {}
        key = rule, label, hard
        if key not in seen:
            seen.add(key)
            (issues if hard else warnings).append({
                "type": "hard_value_conflict" if hard else "ledger_arithmetic",
                "severity": "error" if hard else "warning", "rule": rule, "quote": quote,
                "message": message, **ref,
            })

    if "ledgerBalances" in memory:
        retained = []
        for row in _rows(memory["ledgerBalances"]):
            label, quote = row.get("label"), row.get("quote")
            key = story_ledger_label_key(label)
            if key not in prior and len(matching := alias_keys.get(key, ())) == 1:
                key = next(iter(matching))
                label = row["label"] = prior[key]["label"]
            before = prior.get(key)
            account = accounts.get(key) or _account_pattern(label, book_plan)
            quote_accounts = accounts if before else {**accounts, key: account}
            proof, spoken = _proof_excerpt(source, quote, dialogue)
            value = parse_amount(row.get("value"), conversions)
            change = parse_amount(row.get("change"), conversions)
            opening = parse_amount(before.get("value"), conversions) if before else None
            closings = {amount for start, _, amount in _balance_mentions(proof or "", amounts, conversions)
                        if _nearest_account(proof, start, quote_accounts) == key or len(quote_accounts) == 1}
            tied = proof and (account.search(proof) or before and len(prior) == 1 and _ACCOUNT.search(proof))
            movements = list(_transaction_amounts(proof or "", amounts, conversions))
            net = [amount for amount, explicit in movements if explicit]
            # A single stated transaction plus a result can ground a net only when no other
            # movement is described for this account, including unquantified partial movements.
            related = [normalize_quote(line) for _, line in prose if account.search(line)
                       or len(prior) == 1 and _ACCOUNT.search(line)]
            movement_text = "。".join(dict.fromkeys([proof or "", *(line for line in related if line not in (proof or "")
                                                                  and (proof or "") not in line)]))
            clauses = re.split(r"[，,。！？!?；;\n]|(?<!\d)\.|\.(?!\d)|随后|接着|继而|然后|转手|又|再", movement_text)
            sole = len(movements) == 1 and sum(bool(_TRANSACTION.search(clause)) for clause in clauses) == 1
            grounded = net if net else [movements[0][0]] if sole else []
            comparable = opening and value and change and opening[1] == value[1] == change[1]
            sign_wrong = bool(change and proof and (
                change[0] > 0 and _OUTFLOW.search(proof) and not _INFLOW.search(proof)
                or change[0] < 0 and _INFLOW.search(proof) and not _OUTFLOW.search(proof)
            ))
            contradicted = tied and value in closings and any(
                opening and opening[1] == value[1] == amount[1] and opening[0] + amount[0] != value[0]
                for amount in grounded
            )
            if contradicted or sign_wrong or comparable and opening[0] + change[0] != value[0]:
                rule = "balance_arithmetic" if contradicted or not sign_wrong else "change_direction"
                report(quote if proof else "", f"「{label}」的余额与章前余额或本章变动不一致。",
                       before=before, rule=rule, hard=bool(contradicted and not spoken))
            elif not tied or spoken or value is None or value not in closings or row.get("change") and change not in grounded:
                report(quote if proof else "", f"「{label}」的台账增量缺少可核实的余额或净变动证据。",
                       before=before, rule="ungrounded_delta")
            else:
                retained.append(row)
                if change:
                    changes.add(key)
        memory["ledgerBalances"] = retained

    for key, before in prior.items():
        opening = parse_amount(before.get("value"), conversions)
        if not opening or key in changes:
            continue
        account = accounts[key]
        account_has_transaction = any(account.search(line) and _TRANSACTION.search(account.sub("", line)) for _, line in prose)
        for offset, raw in _passages(chapter_text):
            sentence = unicodedata.normalize("NFKC", raw)
            for start, end, value in _balance_mentions(sentence, amounts, conversions):
                if opening[1] != value[1] or opening == value:
                    continue
                area_start = max(0, offset + start - 40)
                area = unicodedata.normalize("NFKC", chapter_text[area_start:offset + start + 40])
                account_key = _nearest_account(area, offset + start - area_start, accounts)
                if account_key != key and not (len(prior) == 1 and _near(_ACCOUNT, sentence, start, 40)):
                    continue
                spoken = any(offset + start < right and left < offset + end for left, right in raw_dialogue)
                context = narration[max(0, offset + start - 80):offset + end + 80]
                if account_has_transaction or _TRANSACTION.search(account.sub("", context)):
                    continue
                report(raw.strip()[:160], f"「{before['label']}」章前余额为 {before['value']}，正文余额变化缺少交代。",
                       before=before, rule="balance_continuity", hard=not spoken)
    warnings.extend(_additive_warnings(narration, amounts, conversions))
    return {"issues": issues, "warnings": warnings, "cleanedDelta": cleaned}
