import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novel_forge.candidate_rounds import CandidateRounds
from novel_forge.revision_checks import fingerprint
from novel_forge.revision_v2 import RevisionWorkflowV2
from novel_forge.workflow import WorkerError


class Worker:
    def __init__(self, rows):
        self.rows = rows
        self.evaluations = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, action, **fields):
        if action == "revision-source":
            return {"version": fingerprint(self.rows), "idle": True, "readiness": "ready"}
        if action == "candidate-evaluation":
            payload = fields["payload"]
            self.evaluations.append(payload)
            fields["on_progress"]({"evaluationCheckpoint": {"batchResults": [{}], "stage": "pro", "totalBatches": 1},
                                   "evaluationProgress": "初审保存"})
            self.started.set()
            await self.release.wait()
            return {"overallSummary": payload["chapters"][0]["content"], "aiFlavorIssues": []}
        raise AssertionError(action)


class CandidateRoundsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.original = [{"chapterNumber": 1, "title": "第一章", "content": "最初写的是三十元。"}]
        self.v1 = [{**self.original[0], "content": "第一版改成二十元。"}]
        self.v2 = [{**self.original[0], "content": "第二版改成十五元。"}]
        self.baseline = {"overallSummary": "原稿评估", "aiFlavorIssues": [{"priority": "P1"}]}
        self.worker = Worker(self.original)
        self.revisions = RevisionWorkflowV2(Path(temp.name), None, self.worker, None)
        self.rounds = CandidateRounds(self.revisions, self.worker)
        self.addAsyncCleanup(self.rounds.close)
        self.revisions.store.create("run", "book", {"title": "小说", "chapters": self.original, "report": self.baseline},
                                    {"workflowVersion": 2, "sourceVersion": fingerprint(self.original), "chapterCount": 1,
                                     "volumeSize": 10, "isMock": False,
                                     "verifier": {"provider": "openai", "model": "fixture"}})
        self.publish(0, self.v1)
        self.revisions.schedule = lambda run: self.revisions.store.update(run, "running")

    def publish(self, turn, rows):
        self.revisions.commit("run", status="ready", hasCandidate=True, candidateTurn=turn, ledgerVersion=1,
                              work={"turn": turn, "attempt": 0, "targets": None}, products=[
            ("v2-result", turn, {"candidate": rows, "candidateHash": fingerprint(rows), "ledgerVersion": 1}),
        ])

    async def test_pause_keeps_checkpoint_and_reports_follow_candidate_versions(self):
        await self.rounds.action("book", "run", 0, {})
        await self.worker.started.wait()
        self.assertEqual(self.rounds.detail("book", "run", 0)["done"], 1)
        state = await self.rounds.action("book", "run", 0, {"action": "pause"})
        self.assertEqual(state["status"], "paused")
        self.worker.release.set()
        await self.rounds.action("book", "run", 0, {})
        await self.rounds.tasks[("run", 0)]
        self.assertEqual(self.worker.evaluations[-1]["checkpoint"]["stage"], "pro")
        self.assertEqual(self.worker.evaluations[-1]["chapters"], self.v1)
        self.assertEqual(self.rounds.detail("book", "run", 0)["report"]["overallSummary"], self.v1[0]["content"])

        await self.revisions.action("book", "run", "continue-round", {"turn": 0})
        snapshot, source = self.revisions.source("run")
        self.assertEqual(snapshot["report"]["overallSummary"], self.v1[0]["content"])
        self.assertEqual(list(source.values()), self.v1)
        self.assertEqual(self.revisions.load("run", "v2-base"), self.v1)
        self.assertEqual(self.revisions.store.get("run")["sourceVersion"], fingerprint(self.original))
        self.publish(1, self.v2)
        with self.assertRaisesRegex(WorkerError, "先完成当前候选稿"):
            await self.revisions.action("book", "run", "continue-round", {"turn": 0})
        await self.rounds.action("book", "run", 1, {})
        await self.rounds.tasks[("run", 1)]
        self.assertEqual(self.worker.evaluations[-1]["chapters"], self.v2)
        history = self.rounds.list("book", "run")
        self.assertEqual([item["turn"] for item in history["rounds"]], [0, 1])
        self.assertEqual(history["baseline"]["issues"]["P1"], 1)
        self.assertEqual(self.revisions.store.snapshot("run")["chapters"], self.original)

    async def test_continue_requires_completed_evaluation_of_current_candidate(self):
        with self.assertRaisesRegex(WorkerError, "先完成当前候选稿"):
            await self.revisions.action("book", "run", "continue-round", {"turn": 0})
        self.assertEqual(self.revisions.store.get("run")["work"]["turn"], 0)
