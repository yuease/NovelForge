"""Validate that the progress stream reflects real StateGraph node execution."""

import asyncio
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from novel_forge.workflow import Worker, build_graph
from novel_forge.pipeline import PipelineRuntime, review_action
from novel_forge.storage import Store


class ControlledWorker(Worker):
    def __init__(self, script):
        self.status = "running"
        super().__init__(SimpleNamespace(store=None, run_pipeline=self.stages))
        self.script = deque(script)
        self.verifying = asyncio.Event()
        self.release = asyncio.Event()

    async def read_job(self, _job_id):
        return {"status": self.status}

    async def fail_workflow(self, _job_id, _exc):
        self.status = "interrupted"

    async def stages(self, _job_id):
        expected = "prepare"
        yield {"phase": expected}
        while self.script:
            phase, reply = self.script.popleft()
            if phase != expected:
                raise AssertionError(f"Expected {expected}, got {phase}")
            if phase == "verify":
                self.verifying.set()
                await self.release.wait()
            if isinstance(reply, Exception):
                raise reply
            if reply["phase"] == "review":
                expected = yield reply
                if expected != review_action(reply):
                    raise AssertionError(f"Incorrect review action: {expected}")
                yield {"phase": expected, "chapter": reply.get("chapter")}
            elif reply["phase"] == "done":
                self.status = "completed"
                return
            else:
                expected = reply["phase"]
                yield reply


class TestWorkflowProgress(unittest.IsolatedAsyncioTestCase):
    async def test_resume_waits_for_interrupted_graph_to_release_its_checkpoint(self):
        store = Store(Path(self.enterContext(TemporaryDirectory())))
        job_id = store.create_job({"title": "续跑竞争测试"})["id"]
        pipeline = PipelineRuntime(store)
        interrupted, release = asyncio.Event(), asyncio.Event()
        starts = []

        async def runner(current_id):
            starts.append(current_id)
            if len(starts) == 1:
                await pipeline._update(current_id, {"status": "interrupted"})
                interrupted.set()
                await release.wait()
            else:
                await pipeline._update(current_id, {"status": "completed"})

        pipeline.runner = runner
        self.addAsyncCleanup(pipeline.close)
        await pipeline.start_job(job_id)
        await asyncio.wait_for(interrupted.wait(), 2)
        resume = asyncio.create_task(pipeline.resume_job(job_id))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(resume.done())
        finally:
            release.set()
        self.assertEqual((await asyncio.wait_for(resume, 2))["status"], "running")
        self.assertEqual((await pipeline.wait_for_job(job_id))["status"], "completed")
        self.assertEqual(starts, [job_id, job_id])

    async def test_unverified_commit_uses_the_existing_commit_edge(self):
        worker = ControlledWorker([
            ("prepare", {"phase": "topic"}),
            ("topic", {"phase": "plan"}),
            ("plan", {"phase": "outlines"}),
            ("outlines", {"phase": "chapter", "chapter": 1}),
            ("chapter", {"phase": "verify", "chapter": 1}),
            ("verify", {"phase": "review", "chapter": 1, "passed": False,
                        "canRepair": False, "canCommitForReview": True}),
            ("commit", {"phase": "chapter", "chapter": 2}),
            ("chapter", {"phase": "verify", "chapter": 2}),
            ("verify", {"phase": "review", "chapter": 2, "passed": True, "canRepair": False}),
            ("commit", {"phase": "calibrate"}),
            ("calibrate", {"phase": "audit"}),
            ("audit", {"phase": "finish"}),
            ("finish", {"phase": "done"}),
        ])
        worker.graph = build_graph(worker, InMemorySaver())
        worker.release.set()
        await asyncio.wait_for(worker.run("book", "run"), timeout=3)
        progress = await worker.read_progress("book")
        self.assertEqual(progress["edgeVisits"]["verify:commit"], 2)
        self.assertEqual(progress["edgeVisits"]["commit:chapter"], 1)
        self.assertNotIn("reject", progress["nodeVisits"])
        self.assertFalse(worker.script)

    async def test_live_node_repair_loop_and_completed_checkpoint(self):
        saved = InMemorySaver()
        worker = ControlledWorker([
            ("prepare", {"phase": "topic"}),
            ("topic", {"phase": "plan"}),
            ("plan", {"phase": "outlines"}),
            ("outlines", {"phase": "chapter", "chapter": 1}),
            ("chapter", {"phase": "verify", "chapter": 1}),
            ("verify", {"phase": "review", "chapter": 1, "passed": False, "canRepair": True}),
            ("repair", {"phase": "verify", "chapter": 1}),
            ("verify", {"phase": "review", "chapter": 1, "passed": True, "canRepair": False}),
            ("commit", {"phase": "calibrate"}),
            ("calibrate", {"phase": "audit"}),
            ("audit", {"phase": "finish"}),
            ("finish", {"phase": "done"}),
        ])
        worker.graph = build_graph(worker, saved)

        with worker.progress.subscribe("book") as queue:
            future = asyncio.create_task(worker.run("book", "run"))
            try:
                await asyncio.wait_for(worker.verifying.wait(), timeout=3)
                live = await worker.read_progress("book")
                self.assertEqual(live["activeNode"], "verify")
                self.assertEqual(live["activeChapter"], 1)
                self.assertNotIn("verify", live["nodeVisits"])
                self.assertTrue(live["runActive"])
                worker.release.set()
                await asyncio.wait_for(future, timeout=3)
            finally:
                worker.release.set()
                if not future.done():
                    future.cancel()
                    await asyncio.gather(future, return_exceptions=True)

            frames = []
            while not queue.empty():
                frames.append(queue.get_nowait())
            self.assertEqual([frame["revision"] for frame in frames], sorted(frame["revision"] for frame in frames))
            self.assertEqual([frame["activeNode"] for frame in frames if frame["activeNode"]], [
                "prepare", "topic", "plan", "outlines", "chapter", "verify",
                "repair", "verify", "commit", "calibrate", "audit", "finish",
            ])
            self.assertFalse(frames[-1]["runActive"])
            self.assertIsNone(frames[-1]["activeNode"])
            self.assertEqual(frames[-1]["jobStatus"], "completed")
        self.assertEqual(worker.progress.listeners, {})

        restarted = ControlledWorker([])
        restarted.status = "completed"
        restarted.graph = build_graph(restarted, saved)
        restored = await restarted.read_progress("book")
        self.assertNotEqual(restored["epoch"], live["epoch"])
        self.assertIsNone(restored["activeNode"])
        self.assertEqual(restored["lastNode"], "finish")
        self.assertEqual(restored["nodeVisits"]["verify"], 2)
        self.assertEqual(restored["edgeVisits"]["repair:verify"], 1)

    async def test_failure_stops_highlight_at_the_throwing_node(self):
        worker = ControlledWorker([
            ("prepare", {"phase": "topic"}),
            ("topic", RuntimeError("test failure")),
        ])
        worker.graph = build_graph(worker, InMemorySaver())

        with self.assertLogs("novel_forge.workflow"):
            await worker.run("book", "run")

        latest = await worker.read_progress("book")
        self.assertFalse(latest["runActive"])
        self.assertIsNone(latest["activeNode"])
        self.assertEqual(latest["errorNode"], "topic")
        self.assertEqual(latest["lastNode"], "prepare")
        self.assertEqual(latest["jobStatus"], "interrupted")
