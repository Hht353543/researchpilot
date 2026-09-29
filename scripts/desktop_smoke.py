"""Exercise a frozen EXE outside the checkout with no developer tools on PATH.

This uses a scripted local model endpoint, not a vendor key or a clean Windows VM.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.openai_stub import run_stub_server


def wait_ready(root: Path, process: subprocess.Popen[Any]) -> tuple[httpx.Client, dict[str, Any]]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Frozen executable exited before becoming ready")
        try:
            instance = json.loads((root / "instance.json").read_text(encoding="utf-8"))
            client = httpx.Client(
                base_url=f"http://127.0.0.1:{instance['port']}",
                timeout=120,
                headers={"X-ResearchPilot": "desktop"},
                trust_env=False,
            )
            health = client.get("/health").json()
            if health.get("instance_id") == instance["instance_id"]:
                return client, instance
            client.close()
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Frozen executable did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=Path("dist/ResearchPilot.exe"))
    parser.add_argument("--json-out", type=Path, default=Path("output/desktop-smoke.json"))
    args = parser.parse_args()
    checks: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="ResearchPilot 成品 空格 ") as directory:
        outside = Path(directory)
        executable = outside / "ResearchPilot.exe"
        shutil.copy2(args.exe.resolve(), executable)
        data = outside / "用户 数据"
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ.get("SYSTEMROOT", "C:\\Windows")) / "System32")
        environment["API_KEY"] = "foreign-value-must-not-be-used"
        environment["RESEARCHPILOT_BIND_HOST"] = "0.0.0.0"
        command = [str(executable), "--data-dir", str(data), "--no-browser", "--no-tray"]
        occupied = socket.socket()
        try:
            occupied.bind(("127.0.0.1", 8000))
            occupied.listen()
        except OSError:
            occupied.close()
        with run_stub_server() as (base_url, state):
            process = subprocess.Popen(command, cwd=outside, env=environment)
            client: httpx.Client | None = None
            try:
                client, instance = wait_ready(data, process)
                health = client.get("/health").json()
                assert health["desktop_mode"] and not health["provider_ready"]
                assert instance["port"] != 8000
                checks["isolated_startup_and_port_fallback"] = True
                for asset in ("/", "/static/app.js", "/static/desktop.js", "/static/icons.svg"):
                    assert client.get(asset).status_code == 200
                assert 'importedNames.join("、")' in client.get("/static/desktop.js").text
                assert client.get("/kb/search", params={"q": "RAG"}).json()["hits"]
                checks["bundled_frontend_and_knowledge"] = True
                blocked = client.post("/research", json={"question": "分析 RAG", "mode": "async"})
                assert blocked.status_code == 503
                update = {
                    "service": "custom",
                    "base_url": base_url,
                    "model": "gpt-4o-mini",
                    "api_key": "test-key",
                    "max_iterations": 1,
                }
                configured = client.put("/desktop/settings", json=update)
                assert configured.status_code == 200, configured.text
                assert "test-key" not in (data / "settings.json").read_text()
                checks["dpapi_and_hot_configuration"] = True
                content = (
                    "# 成品验收资料\nRAG 使用检索增强生成，为企业知识库建立引用与来源追溯，"
                    "帮助团队获得可靠报告。"
                )
                assert (
                    client.post(
                        "/kb/import", json={"filename": "成品资料.md", "content": content}
                    ).status_code
                    == 200
                )
                assert client.post("/kb/reindex").status_code == 200
                assert any(
                    doc["doc_id"].startswith("import_")
                    for doc in client.get("/kb/documents").json()["documents"]
                )
                result_response = client.post(
                    "/research", json={"question": "分析 RAG 知识库如何帮助企业研究"}
                )
                assert result_response.status_code == 200, result_response.text
                result = result_response.json()
                assert result["status"] == "completed" and result["report"]["markdown"]
                assert result["evidence"]["sources"] and state.chat_requests > 1
                task_id = result["task_id"]
                checks["scripted_http_research_and_citations"] = True
                duplicate = subprocess.run(command, cwd=outside, env=environment, timeout=60, check=False)
                assert duplicate.returncode == 0
                assert (
                    json.loads((data / "instance.json").read_text())["instance_id"] == instance["instance_id"]
                )
                checks["single_instance"] = True
                assert client.post("/desktop/exit").status_code == 200
                process.wait(timeout=30)
                assert process.returncode == 0
                client.close()
                client = None
                # Exercise the native Windows tray on restart, without opening a browser.
                tray_command = [part for part in command if part != "--no-tray"]
                process = subprocess.Popen(tray_command, cwd=outside, env=environment)
                client, _ = wait_ready(data, process)
                assert client.get("/health").json()["provider_ready"]
                assert client.get("/research/" + task_id).json()["report"]["markdown"]
                assert client.get("/desktop/settings").json()["max_iterations"] == 1
                checks["restart_preserves_settings_and_history"] = True
                checks["native_tray_startup"] = True
                assert client.post("/desktop/exit").status_code == 200
                process.wait(timeout=30)
                checks["graceful_exit"] = process.returncode == 0 and not (data / "instance.json").exists()
            finally:
                if client:
                    if process.poll() is None:
                        try:
                            client.post("/desktop/exit")
                            process.wait(timeout=20)
                        except (httpx.HTTPError, subprocess.TimeoutExpired):
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
                    client.close()
                elif process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
                occupied.close()
    checks["clean_windows_vm"] = "not_verified"
    checks["live_deepseek_key"] = "not_verified"
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(checks, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
