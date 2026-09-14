"""Smoke test for the *running* ResearchPilot compose stack (real containers).

``scripts/compose_smoke.py`` reproduces the compose topology with local processes
because this development machine has no container engine. This script is the
opposite half of the pair: it assumes the stack is already up - in CI that is the
``docker compose up --wait`` step of the ``docker`` job - and verifies it through
its real network surface.

It only uses the standard library, so the very same file runs

* on the runner against the published ports (default), and
* inside the API container via ``docker compose exec -T api`` (``--in-container``),

without installing anything first. Every check is a hard gate: the script exits
non-zero with the failing reason, so a broken container cannot produce a green
workflow.

    python scripts/container_smoke.py --api-url http://127.0.0.1:8000 --mcp-url http://127.0.0.1:8765
    docker compose exec -T api python scripts/container_smoke.py --in-container
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Run as `python scripts/container_smoke.py`, so the package must be importable.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchpilot.utils import configure_script_stdio  # noqa: E402

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_MCP_URL = "http://127.0.0.1:8765"
# Inside the compose network the MCP service is reached by its service name.
IN_CONTAINER_MCP_URL = "http://mcp:8765"

EXPECTED_TOOLS = {"search_knowledge", "get_document", "search_web", "get_research_context"}
QUESTION = "如何通过 MCP 网关把知识库复用到多个 Agent？"
CHINESE_QUERY = "工具注册表"


def _fetch(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Return ``(status, body)`` for a real HTTP round trip."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="strict")


def _fetch_json(url: str, **kwargs: Any) -> dict[str, Any]:
    status, body = _fetch(url, **kwargs)
    if status != 200:
        raise RuntimeError(f"{url} returned HTTP {status}")
    return json.loads(body)


def wait_for_health(url: str, *, timeout: float, label: str) -> dict[str, Any]:
    """Poll ``/health`` until the service answers with ``status == ok``."""
    deadline = time.time() + timeout
    last = "not started"
    while time.time() < deadline:
        try:
            payload = _fetch_json(url, timeout=5)
            if payload.get("status") == "ok":
                return payload
            last = f"unexpected payload: {payload}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise RuntimeError(f"{label} never became healthy at {url}: {last}")


def _rpc(mcp_url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _rpc_payload(method, params)
    # ``mcp_url`` is the service base URL (e.g. ``http://mcp:8765``); the
    # streamable-HTTP transport lives on ``POST /mcp``.
    endpoint = f"{mcp_url.rstrip('/')}/mcp"
    status, body = _fetch(endpoint, method="POST", payload=payload, timeout=60.0)
    if status != 200:
        raise RuntimeError(f"{endpoint} returned HTTP {status}")
    response = json.loads(body)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    if response.get("id") != payload["id"]:
        raise RuntimeError(f"{method} answered with the wrong id: {response.get('id')}")
    return dict(response["result"])


def _rpc_payload(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def check(failures: list[str], name: str, condition: bool, detail: str) -> None:
    status = "ok" if condition else "FAIL"
    print(f"[container-smoke] {status:4} {name}: {detail}")
    if not condition:
        failures.append(f"{name}: {detail}")


def verify_stack(
    api_url: str, mcp_url: str, *, timeout: float, in_container: bool
) -> tuple[int, dict[str, Any]]:
    failures: list[str] = []
    summary: dict[str, Any] = {"api_url": api_url, "mcp_url": mcp_url, "in_container": in_container}

    # --- MCP service ------------------------------------------------------- #
    mcp_health = wait_for_health(f"{mcp_url}/health", timeout=timeout, label="mcp")
    tools = sorted(mcp_health.get("tools") or [])
    summary["mcp_tools"] = tools
    check(failures, "mcp health", True, f"server={mcp_health.get('server')} tools={tools}")
    check(failures, "mcp exposes the 4 documented tools", set(tools) == EXPECTED_TOOLS, str(tools))

    # --- MCP JSON-RPC over the real transport ------------------------------ #
    initialize = _rpc(mcp_url, "initialize", {"clientInfo": {"name": "container-smoke"}})
    check(
        failures,
        "mcp initialize",
        bool(initialize.get("serverInfo")),
        json.dumps(initialize.get("serverInfo"), ensure_ascii=False),
    )
    listed = _rpc(mcp_url, "tools/list")
    listed_names = sorted(item["name"] for item in listed.get("tools", []))
    check(failures, "mcp tools/list", set(listed_names) == EXPECTED_TOOLS, str(listed_names))

    # Chinese query over the network hop: the transport must be UTF-8 clean.
    called = _rpc(
        mcp_url,
        "tools/call",
        {"name": "search_knowledge", "arguments": {"query": CHINESE_QUERY, "top_k": 2}},
    )
    echoed = (called.get("structuredContent") or {}).get("query")
    hits = len((called.get("structuredContent") or {}).get("items") or [])
    summary["mcp_chinese_query"] = {"echoed": echoed, "hits": hits}
    check(
        failures,
        "mcp tools/call (UTF-8)",
        echoed == CHINESE_QUERY and hits > 0,
        f"query={echoed} hits={hits}",
    )
    counted = _fetch_json(f"{mcp_url}/health", timeout=30)
    call_count = (counted.get("calls") or {}).get("search_knowledge", 0)
    summary["mcp_search_knowledge_calls"] = call_count
    check(
        failures,
        "mcp counted the call server-side",
        call_count >= 1,
        f"search_knowledge={call_count}",
    )

    # --- API service ------------------------------------------------------- #
    api_health = wait_for_health(f"{api_url}/health", timeout=timeout, label="api")
    summary["api"] = {
        "version": api_health.get("version"),
        "provider": api_health.get("provider"),
        "mcp_transport": api_health.get("mcp_transport"),
        "kb_documents": (api_health.get("knowledge_base") or {}).get("documents"),
    }
    check(
        failures,
        "api /health",
        api_health.get("mcp_transport") == "http",
        f"version={api_health.get('version')} provider={api_health.get('provider')} "
        f"mcp_transport={api_health.get('mcp_transport')}",
    )
    documents = (api_health.get("knowledge_base") or {}).get("documents") or 0
    check(failures, "knowledge base loaded", documents > 0, f"documents={documents}")

    api_tools = _fetch_json(f"{api_url}/mcp/tools", timeout=60)
    api_tool_names = sorted(item["name"] for item in api_tools.get("tools", []))
    check(
        failures,
        "api -> mcp over HTTP",
        api_tools.get("transport") == "http" and set(api_tool_names) == EXPECTED_TOOLS,
        f"transport={api_tools.get('transport')} tools={api_tool_names}",
    )

    frontend_status, frontend_body = _fetch(f"{api_url}/", timeout=60)
    check(
        failures,
        "frontend served",
        frontend_status == 200 and "ResearchPilot" in frontend_body,
        f"HTTP {frontend_status} ({len(frontend_body)} bytes)",
    )

    # --- End-to-end research task ------------------------------------------ #
    started = time.perf_counter()
    result = _fetch_json(
        f"{api_url}/research",
        method="POST",
        payload={"question": QUESTION, "settings": {"max_iterations": 1}},
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    metrics = result.get("metrics") or {}
    evidence = (result.get("evidence") or {}).get("evidence") or []
    sources = (result.get("evidence") or {}).get("sources") or []
    report = (result.get("report") or {}).get("markdown") or ""
    summary["research"] = {
        "status": result.get("status"),
        "mcp_calls": metrics.get("mcp_calls"),
        "evidence": len(evidence),
        "sources": len(sources),
        "latency_s": round(elapsed, 2),
    }
    check(
        failures,
        "research status",
        result.get("status") in {"succeeded", "degraded"},
        f"{result.get('status')}",
    )
    check(
        failures,
        "research called MCP over HTTP",
        (metrics.get("mcp_calls") or 0) >= 1,
        f"{metrics.get('mcp_calls')}",
    )
    check(failures, "research produced evidence", bool(evidence), f"{len(evidence)} items")
    check(failures, "research produced sources", bool(sources), f"{len(sources)} sources")
    check(failures, "research produced a report", bool(report.strip()), f"{len(report)} chars")

    # --- Container-internal assertions ------------------------------------- #
    if in_container:
        kb_path = Path(os.environ.get("RESEARCHPILOT_KB_PATH", "/app/data/knowledge_base"))
        runs_path = Path(os.environ.get("RESEARCHPILOT_RUNS_PATH", "/app/runs"))
        documents_on_disk = sorted(kb_path.glob("**/*")) if kb_path.exists() else []
        check(
            failures,
            "packaged knowledge base present in the image",
            bool(documents_on_disk),
            f"{kb_path} ({len(documents_on_disk)} entries)",
        )
        writable = True
        detail = str(runs_path)
        try:
            runs_path.mkdir(parents=True, exist_ok=True)
            probe = runs_path / "container_smoke_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            writable = False
            detail = f"{runs_path} ({type(exc).__name__}: {exc})"
        check(failures, "runs volume is writable", writable, detail)
        summary["container"] = {
            "kb_path": str(kb_path),
            "runs_path": str(runs_path),
            "python": sys.version.split()[0],
        }

    print()
    summary["result"] = "pass" if not failures else "fail"
    summary["failures"] = failures
    if failures:
        print("[container-smoke] FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1, summary
    print("[container-smoke] OK: running stack satisfies the container contract")
    return 0, summary


def main() -> int:
    configure_script_stdio()
    parser = argparse.ArgumentParser(description="Smoke test the running ResearchPilot stack")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--mcp-url", default=None)
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for health")
    parser.add_argument(
        "--in-container",
        action="store_true",
        help="assume the script runs inside the API container (checks image contents too)",
    )
    parser.add_argument("--json-out", default=None, help="write the JSON summary to this path")
    args = parser.parse_args()

    api_url = args.api_url or DEFAULT_API_URL
    mcp_url = args.mcp_url or (IN_CONTAINER_MCP_URL if args.in_container else DEFAULT_MCP_URL)

    try:
        code, summary = verify_stack(api_url, mcp_url, timeout=args.timeout, in_container=args.in_container)
    except Exception as exc:
        print(f"[container-smoke] FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
