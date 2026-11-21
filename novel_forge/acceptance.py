"""Synthetic checks used by the workbench acceptance page."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .memes import MemeStore
from .pipeline import _write


WRITING_SUITES = {
    "foreshadowing": ("长距离跨章伏笔测试 (Ch2->6->11->18 青铜钥匙)", 20, 900, "foreshadowing",
                     "主角重生后寻找玄天七号青铜钥匙，揭开钟楼密室真相"),
    "fault-injection": ("故障注入与源章节溯源自动修复测试", 4, 900, "fault-injection",
                        "主角第1章左臂骨裂，第2章验证伤势矛盾与占位符自动修复"),
    "timeout-resume": ("LLM 超时中断与断点续跑恢复测试", 5, 900, "timeout-once",
                       "主角追查旧案，模拟第3章超时中断后恢复"),
    "empty-premise-auto-topic": ("空梗概自动3选1立项与版本锁定测试", 4, 1000, "normal", ""),
    "audit-gate": ("终审复审与四重完本门禁测试", 4, 1000, "audit-fail-once",
                   "主角揭开钟楼秘密，验证完本终审复审"),
}


async def _writing_result(pipeline, suite):
    name, total, words, mode, premise = WRITING_SUITES[suite]
    store = pipeline.store
    job = await _write(store.create_job, {
        "title": "验收测试-" + name if premise else "", "premise": premise, "genre": "都市重生爽文",
        "pov": "第三人称限知", "targetWords": total * words, "wordsPerChapter": words, "totalChapters": total,
        "llmConfig": {"provider": "mock", "model": "mock-novel-v1"},
        "testScenario": {"mode": mode, "runId": uuid4().hex},
    })
    job_id = job["id"]
    await pipeline.start_job(job_id)
    final = await pipeline.wait_for_job(job_id)
    chapters = await asyncio.to_thread(store.get_chapters, job_id)
    passed = final["status"] == "completed"
    details = {"status": final["status"]}
    if suite == "foreshadowing":
        bodies = {chapter["chapterNumber"]: chapter["content"] for chapter in chapters}
        facts = final["canonicalFacts"]
        clue = next((item for item in facts["foreshadowing"] if item["id"] == "fs_bronze_key"), {})
        passed = (passed and len(chapters) == total and clue.get("status") == "paid_off"
                  and all(word in bodies.get(number, "") for number, word in ((2, "玄天七号"), (6, "玄天七号"),
                                                                           (11, "钟楼"), (18, "玄天七号"))))
        details.update(completedChapters=final["completedChapters"], totalWords=final["totalWords"],
                       foreshadowingStatus=clue.get("status"))
    elif suite == "fault-injection":
        repairs = final["repairLogs"]
        second = next((chapter for chapter in chapters if chapter["chapterNumber"] == 2), {})
        count = second.get("repairCount") or 0
        passed = (passed and count >= 1 and "【待补充" not in second["content"]
                  and any(row.get("chapterNumber") == 2 and row.get("resolved") for row in repairs))
        details.update(repairLogsCount=len(repairs), ch2RepairCount=count)
    elif suite == "timeout-resume":
        await pipeline.resume_job(job_id)
        resumed = await pipeline.wait_for_job(job_id)
        saved = await asyncio.to_thread(store.get_chapters, job_id)
        passed = final["status"] == "interrupted" and len(chapters) == 2 and resumed["status"] == "completed" and len(saved) == total
        details = {"interruptedStatus": final["status"], "chaptersBeforeResume": len(chapters),
                   "resumedStatus": resumed["status"], "chaptersAfterResume": len(saved)}
    elif suite == "empty-premise-auto-topic":
        topic = final.get("topicPackage") or {}
        count = len(topic.get("candidates") or [])
        passed = passed and count == 3 and bool(topic.get("lockedAt") and topic.get("selectionReason") and final["premise"])
        details.update(inspirationMode=final.get("inspirationMode"), candidatesCount=count, lockedAt=topic.get("lockedAt"))
    else:
        gate = final.get("auditGate") or {}
        passed = passed and gate.get("passed") is True and (final.get("auditReport") or {}).get("passed") is True
        details["auditGate"] = final.get("auditGate")
    return {"id": suite, "name": name, "passed": passed, "jobId": job_id, "details": details}


async def _meme_result():
    day_zero = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    fresh = (day_zero - timedelta(days=2)).isoformat()
    old = (day_zero - timedelta(days=20)).isoformat()
    records = [
        {"slug": "old-meme-revived", "title": "老梗新编", "earliestSpreadAt": old,
         "hasHotListRecord": True, "heatEvidenceUrls": ["https://example.com/old-hot"]},
        {"slug": "single-creator-meme", "title": "单人自创梗", "earliestSpreadAt": fresh,
         "spreadRecords": [{"creator": "userA", "publishedAt": fresh, "url": "https://example.com/a"}]},
        {"slug": "fresh-hot-meme", "title": "真爆款新梗", "earliestSpreadAt": fresh,
         "hasHotListRecord": True, "heatEvidenceUrls": ["https://example.com/hot"],
         "spreadRecords": [{"creator": f"creator{n}", "publishedAt": fresh, "url": f"https://example.com/{n}"}
                           for n in (1, 2)]},
    ]
    with TemporaryDirectory(prefix="nf-acc-memes-") as directory:
        store = MemeStore(Path(directory))
        initial = await store.sync_memes(now=day_zero.isoformat(), verify_details=False, custom_items=[
            {**item, "updated_at": day_zero.isoformat()} for item in records
        ])
        first = {item["slug"]: item["status"] for item in initial["items"]}
        day_eight = (day_zero + timedelta(days=8)).isoformat()
        failed = await store.sync_memes(now=day_eight, simulate_failure=True)
        last = {item["slug"]: item["status"] for item in (await store.get_memes(now=day_eight))["items"]}
    details = {
        "day0Statuses": {"oldMeme": first.get("old-meme-revived"), "singleCreator": first.get("single-creator-meme"),
                         "freshHot": first.get("fresh-hot-meme")},
        "day8FailSyncUsingCache": failed.get("usingCache"), "day8FreshHotStatus": last.get("fresh-hot-meme"),
    }
    passed = (list(details["day0Statuses"].values()) == ["rejected_old", "pending_verification", "active"]
              and details["day8FailSyncUsingCache"] is True and details["day8FreshHotStatus"] == "expired")
    return {"id": "memes-admission", "name": "梗Wiki新梗双条件准入与7天自动过期测试", "passed": passed, "details": details}


async def run_acceptance(pipeline, suite):
    results = [await _writing_result(pipeline, name) for name in WRITING_SUITES if suite in (name, "all")]
    if suite in ("memes-admission", "all"):
        results.append(await _meme_result())
    return {"passed": bool(results) and all(item["passed"] for item in results), "results": results}
