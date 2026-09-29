"""Cross-process persistence invariants for memory and the knowledge index."""

from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from researchpilot.config import Settings
from researchpilot.memory.long_term import LongTermMemory
from researchpilot.persistence import serialized_file_update
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.utils import sha1_of

ROOT = Path(__file__).resolve().parents[2]


def _memory_writer(
    path: str,
    ready: Any,
    start: Any,
    done: Any,
    writes: list[dict[str, Any]],
) -> None:
    memory = LongTermMemory(path)
    ready.set()
    if not start.wait(timeout=20):
        raise RuntimeError("memory writer was never released")
    for write in writes:
        memory.remember(**write)
    done.set()


def _hold_file_lock(path: str, acquired: Any, release: Any) -> None:
    with serialized_file_update(path):
        acquired.set()
        if not release.wait(timeout=20):
            raise RuntimeError("file lock holder was never released")


def _fail_while_holding_file_lock(path: str, acquired: Any) -> None:
    try:
        with serialized_file_update(path):
            acquired.set()
            raise OSError("simulated persistence failure")
    except OSError:
        pass


@pytest.mark.parametrize(
    ("initial", "first", "second", "expected"),
    [
        (
            [],
            [{"content": "record A", "importance": 0.6}],
            [{"content": "record B", "importance": 0.7}],
            {"record A": (0.6, 0), "record B": (0.7, 0)},
        ),
        (
            [{"content": "record A", "importance": 0.2}],
            [{"content": "record A", "importance": 0.9}],
            [{"content": "record B", "importance": 0.6}],
            {"record A": (0.9, 1), "record B": (0.6, 0)},
        ),
        (
            [
                {"content": "record A", "importance": 0.2},
                {"content": "record B", "importance": 0.3},
            ],
            [{"content": "record A", "importance": 0.8}],
            [{"content": "record B", "importance": 0.9}],
            {"record A": (0.8, 1), "record B": (0.9, 1)},
        ),
        (
            [{"content": "same logical record", "importance": 0.2}],
            [{"content": "same logical record", "importance": 0.7}],
            [{"content": "same logical record", "importance": 0.9}],
            {"same logical record": (0.9, 2)},
        ),
    ],
    ids=["two-inserts", "update-and-insert", "two-record-updates", "same-record"],
)
def test_long_term_memory_serializes_stale_process_writers(
    tmp_path: Path,
    initial: list[dict[str, Any]],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    expected: dict[str, tuple[float, int]],
) -> None:
    """Both writers load first; the second commit must merge with the first commit."""
    path = tmp_path / "memory.json"
    seed = LongTermMemory(path)
    for write in initial:
        seed.remember(**write)

    context = multiprocessing.get_context("spawn")
    ready_a, ready_b = context.Event(), context.Event()
    start_a, start_b = context.Event(), context.Event()
    done_a, done_b = context.Event(), context.Event()
    process_a = context.Process(
        target=_memory_writer,
        args=(str(path), ready_a, start_a, done_a, first),
    )
    process_b = context.Process(
        target=_memory_writer,
        args=(str(path), ready_b, start_b, done_b, second),
    )
    process_a.start()
    process_b.start()
    try:
        assert ready_a.wait(20) and ready_b.wait(20)
        start_a.set()
        assert done_a.wait(20)
        start_b.set()
        assert done_b.wait(20)
    finally:
        process_a.join(timeout=20)
        process_b.join(timeout=20)
        if process_a.is_alive():
            process_a.terminate()
        if process_b.is_alive():
            process_b.terminate()

    assert process_a.exitcode == 0
    assert process_b.exitcode == 0
    records = {record.content: record for record in LongTermMemory(path).all()}
    assert set(records) == set(expected)
    for content, (importance, hits) in expected.items():
        assert records[content].importance == importance
        assert records[content].hits == hits


def test_file_update_lock_excludes_an_independent_process(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.json")
    context = multiprocessing.get_context("spawn")
    acquired_a, acquired_b = context.Event(), context.Event()
    release_a, release_b = context.Event(), context.Event()
    process_a = context.Process(target=_hold_file_lock, args=(path, acquired_a, release_a))
    process_b = context.Process(target=_hold_file_lock, args=(path, acquired_b, release_b))
    process_a.start()
    try:
        assert acquired_a.wait(20)
        process_b.start()
        assert not acquired_b.wait(0.5), "second process entered before the first released the lock"
        release_a.set()
        assert acquired_b.wait(20)
        release_b.set()
    finally:
        release_a.set()
        release_b.set()
        process_a.join(timeout=20)
        if process_b.pid is not None:
            process_b.join(timeout=20)
        for process in (process_a, process_b):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(timeout=20)

    assert process_a.exitcode == 0
    assert process_b.exitcode == 0


def test_file_update_failure_releases_lock_for_another_process(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.json")
    context = multiprocessing.get_context("spawn")
    failed_inside_lock = context.Event()
    acquired_after_failure = context.Event()
    release = context.Event()
    process_a = context.Process(target=_fail_while_holding_file_lock, args=(path, failed_inside_lock))
    process_a.start()
    process_a.join(timeout=20)
    assert failed_inside_lock.is_set()
    assert process_a.exitcode == 0

    process_b = context.Process(target=_hold_file_lock, args=(path, acquired_after_failure, release))
    process_b.start()
    try:
        assert acquired_after_failure.wait(20)
        release.set()
        process_b.join(timeout=20)
    finally:
        release.set()
        if process_b.is_alive():
            process_b.terminate()
            process_b.join(timeout=20)

    assert process_b.exitcode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.time() + 30
    while time.time() < deadline and process.poll() is None:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(f"process did not become healthy\nstdout={stdout}\nstderr={stderr}")


def _mcp_call(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["result"]["structuredContent"]


def test_api_mutations_refresh_an_independent_mcp_process(tmp_path: Path) -> None:
    """A long-running MCP process must observe API add, update and delete commits."""
    runs = tmp_path / "runs"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    settings = Settings(
        _env_file=None,
        provider="mock",
        runs_path=str(runs),
        kb_path=str(corpus),
        embedding_dim=64,
    )
    initial = KnowledgeBase(settings)
    initial.ingest_text(
        "Initial unrelated material keeps the persisted index non-empty.",
        title="Initial",
        source="sync://initial",
    )
    initial.save()

    api_port, mcp_port = _free_port(), _free_port()
    common_env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "RESEARCHPILOT_PROVIDER": "mock",
        "RESEARCHPILOT_RUNS_PATH": str(runs),
        "RESEARCHPILOT_KB_PATH": str(corpus),
        "RESEARCHPILOT_EMBEDDING_DIM": "64",
        "RESEARCHPILOT_ACCESS_TOKEN": "",
    }
    mcp_env = {
        **common_env,
        "RESEARCHPILOT_MCP_TRANSPORT": "http",
        "RESEARCHPILOT_MCP_HOST": "127.0.0.1",
        "RESEARCHPILOT_MCP_PORT": str(mcp_port),
    }
    api_env = {**common_env, "RESEARCHPILOT_MCP_TRANSPORT": "inprocess"}
    mcp = subprocess.Popen(
        [sys.executable, "-m", "researchpilot.mcp_server"],
        cwd=ROOT,
        env=mcp_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "researchpilot.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ],
        cwd=ROOT,
        env=api_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        api_base = f"http://127.0.0.1:{api_port}"
        mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
        _wait_for_health(f"{api_base}/health", api)
        _wait_for_health(f"http://127.0.0.1:{mcp_port}/health", mcp)

        title = "Shared consistency document"
        source = "sync://shared"
        doc_id = f"doc_{sha1_of(source + title)}"
        old_marker = "quartzfalcon-old-version"
        new_marker = "quartzfalcon-new-version"

        added = httpx.post(
            f"{api_base}/kb/documents",
            json={
                "title": title,
                "source": source,
                "content": f"{old_marker} is visible after the first committed API write.",
            },
            timeout=20,
        )
        assert added.status_code == 200, added.text
        add_hits = _mcp_call(
            mcp_url,
            "search_knowledge",
            {"query": old_marker, "top_k": 10, "strategy": "keyword"},
        )["items"]
        assert any(item["doc_id"] == doc_id and old_marker in item["content"] for item in add_hits)

        updated = httpx.post(
            f"{api_base}/kb/documents",
            json={
                "title": title,
                "source": source,
                "content": f"{new_marker} replaces all content from the previous document version.",
            },
            timeout=20,
        )
        assert updated.status_code == 200, updated.text
        update_hits = _mcp_call(
            mcp_url,
            "search_knowledge",
            {"query": new_marker, "top_k": 10, "strategy": "keyword"},
        )["items"]
        assert any(item["doc_id"] == doc_id and new_marker in item["content"] for item in update_hits)
        assert all(old_marker not in item["content"] for item in update_hits if item["doc_id"] == doc_id)

        deleted = httpx.delete(f"{api_base}/kb/documents/{doc_id}", timeout=20)
        assert deleted.status_code == 200, deleted.text
        delete_hits = _mcp_call(
            mcp_url,
            "search_knowledge",
            {"query": new_marker, "top_k": 10, "strategy": "keyword"},
        )["items"]
        assert all(item["doc_id"] != doc_id for item in delete_hits)
    finally:
        for process in (api, mcp):
            process.terminate()
        for process in (api, mcp):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_knowledge_base_save_merges_documents_from_stale_instances(tmp_path: Path) -> None:
    """A save applies only local document mutations to the latest disk snapshot."""
    settings = Settings(
        _env_file=None,
        provider="mock",
        runs_path=str(tmp_path / "runs"),
        kb_path=str(tmp_path / "empty-corpus"),
        embedding_dim=64,
    )
    first = KnowledgeBase(settings)
    second = KnowledgeBase(settings)
    first.ingest_text("first process document content", title="First", source="sync://first")
    second.ingest_text("second process document content", title="Second", source="sync://second")

    first.save()
    second.save()

    reloaded = KnowledgeBase.load_or_create(settings)
    assert {document.title for document in reloaded.store.documents()} == {"First", "Second"}
