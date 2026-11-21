"""Publish the execution boundaries of the live graph without exposing its inputs."""

import asyncio
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TypedDict
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Trace(TypedDict):
    lastNode: str | None
    lastChapter: int | None
    nodeVisits: dict[str, int]
    edgeVisits: dict[str, int]


class ProgressFrame(Trace):
    jobId: str
    epoch: str
    revision: int
    runId: str | None
    runActive: bool
    activeNode: str | None
    activeChapter: int | None
    activeSince: str | None
    errorNode: str | None
    jobStatus: str | None


def empty_trace() -> Trace:
    return {"lastNode": None, "lastChapter": None, "nodeVisits": {}, "edgeVisits": {}}


def advance_trace(trace: Trace, phase: str, chapter: int | None) -> Trace:
    nodes = dict(trace["nodeVisits"])
    nodes[phase] = nodes.get(phase, 0) + 1
    edges = dict(trace["edgeVisits"])
    if trace["lastNode"]:
        edge = f"{trace['lastNode']}:{phase}"
        edges[edge] = edges.get(edge, 0) + 1
    return {"lastNode": phase, "lastChapter": chapter, "nodeVisits": nodes, "edgeVisits": edges}


class WorkflowProgress:
    """Snapshots and a bounded queue per client, all owned by the asyncio event loop."""

    def __init__(self) -> None:
        self.epoch = uuid4().hex
        self.revision = 0
        self.frames: OrderedDict[str, ProgressFrame] = OrderedDict()
        self.listeners: dict[str, set[asyncio.Queue[ProgressFrame]]] = {}

    def snapshot(self, job_id: str, status: str | None, trace: Trace | None = None) -> ProgressFrame:
        frame = self.frames.get(job_id)
        if frame is not None:
            if frame["runActive"] or status == frame["jobStatus"]:
                return frame
            return {**frame, "jobStatus": status}
        return {
            **(trace or empty_trace()),
            "jobId": job_id, "epoch": self.epoch, "revision": self.revision,
            "runId": None, "runActive": False, "activeNode": None, "activeChapter": None,
            "activeSince": None, "errorNode": None, "jobStatus": status,
        }

    def publish(self, job_id: str, frame: ProgressFrame) -> None:
        self.revision += 1
        frame = {**frame, "revision": self.revision}
        self.frames[job_id] = frame
        self.frames.move_to_end(job_id)
        if len(self.frames) > 256:
            for stale, prior in self.frames.items():
                if not prior["runActive"]:
                    del self.frames[stale]
                    break
        for queue in self.listeners.get(job_id, ()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    def begin(self, job_id: str, run_id: str) -> None:
        self.publish(job_id, {
            **self.snapshot(job_id, "running"), **empty_trace(),
            "runId": run_id, "runActive": True, "jobStatus": "running",
            "activeNode": None, "activeChapter": None, "activeSince": None, "errorNode": None,
        })

    def enter(self, job_id: str, run_id: str, phase: str, chapter: int | None) -> None:
        frame = self.frames.get(job_id)
        if frame is None or frame["runId"] != run_id:
            return
        self.publish(job_id, {
            **frame, "activeNode": phase, "activeChapter": chapter, "activeSince": now(),
        })

    def leave(self, job_id: str, run_id: str, trace: Trace | None = None) -> None:
        frame = self.frames.get(job_id)
        if frame is None or frame["runId"] != run_id:
            return
        self.publish(job_id, {
            **frame, **(trace or {}), "activeNode": None, "activeChapter": None,
            "activeSince": None, "errorNode": frame["activeNode"] if trace is None else None,
        })

    def stop(self, job_id: str, run_id: str, status: str | None) -> None:
        frame = self.frames.get(job_id)
        if frame is None or frame["runId"] != run_id:
            return
        self.publish(job_id, {
            **frame, "runActive": False, "activeNode": None, "activeChapter": None,
            "activeSince": None, "jobStatus": status,
        })

    @contextmanager
    def subscribe(self, job_id: str) -> Iterator[asyncio.Queue[ProgressFrame]]:
        queue: asyncio.Queue[ProgressFrame] = asyncio.Queue(maxsize=64)
        clients = self.listeners.setdefault(job_id, set())
        clients.add(queue)
        try:
            yield queue
        finally:
            clients.discard(queue)
            if not clients:
                self.listeners.pop(job_id, None)
