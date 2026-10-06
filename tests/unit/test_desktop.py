"""Desktop configuration isolation, native credentials and launcher primitives."""

from __future__ import annotations

import base64
import os
import socket

import httpx
import pytest

from researchpilot.desktop.api import ConnectionFailure, probe_connection
from researchpilot.desktop.launcher import SingleInstance, bind_local_socket
from researchpilot.desktop.settings import (
    DesktopConfig,
    DesktopRuntimeSettings,
    DesktopStore,
    WindowsProtector,
)
from researchpilot.llm.base import ChatMessage, LLMError
from researchpilot.llm.openai_provider import OpenAICompatibleProvider
from researchpilot.schemas import CritiqueReport, ResearchPlan


class TestProtector:
    __test__ = False

    def protect(self, value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    def unprotect(self, value: str) -> str:
        return base64.b64decode(value).decode()


def test_desktop_ignores_environment_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=dotenv-key\nMODEL=wrong-model\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "foreign-key")
    monkeypatch.setenv("RESEARCHPILOT_PROVIDER", "mock")
    monkeypatch.setenv("RESEARCHPILOT_BIND_HOST", "0.0.0.0")
    store = DesktopStore(tmp_path / "user", protector=TestProtector())
    store.prepare()
    settings = store.runtime_settings()
    assert settings.api_key is None
    assert settings.provider == "openai"
    assert settings.bind_host == "127.0.0.1"
    assert settings.model == "deepseek-flash"


def test_saved_settings_restore_and_never_contain_plaintext_key(tmp_path):
    store = DesktopStore(tmp_path, protector=TestProtector())
    store.prepare()
    config = DesktopConfig(top_k=3, max_iterations=1)
    store.save(config, "my-test-secret")
    assert "my-test-secret" not in store.config_path.read_text()
    restored = DesktopStore(tmp_path, protector=TestProtector())
    restored.prepare()
    assert restored.api_key == "my-test-secret"
    assert restored.config.top_k == 3
    assert "my-test-secret" not in str(restored.public_settings())


def test_unreadable_credentials_allow_reconfiguration_without_removing_data(tmp_path):
    store = DesktopStore(tmp_path, protector=TestProtector())
    store.prepare()
    store.config_path.write_text('{"config":{},"api_key_encrypted":"!bad"}', encoding="utf-8")
    artifact = tmp_path / "runs" / "kept.txt"
    artifact.write_text("keep")
    store.load()
    assert store.api_key is None and store.notice
    assert artifact.read_text() == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_real_dpapi_roundtrip():
    protector = WindowsProtector()
    encrypted = protector.protect("fake-key-used-only-in-test")
    assert "fake-key-used-only-in-test" not in encrypted
    assert protector.unprotect(encrypted) == "fake-key-used-only-in-test"


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_mutex_releases_on_exit(tmp_path):
    first = SingleInstance(tmp_path)
    second = SingleInstance(tmp_path)
    try:
        assert first.primary and not second.primary
    finally:
        second.close()
        first.close()
    third = SingleInstance(tmp_path)
    try:
        assert third.primary
    finally:
        third.close()


def test_occupied_port_is_selected_automatically():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        listener = bind_local_socket(occupied.getsockname()[1])
        try:
            assert listener.getsockname()[1] != occupied.getsockname()[1]
        finally:
            listener.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop launcher")
@pytest.mark.parametrize("ready", [False, True])
def test_explicit_exit_during_startup_is_successful(tmp_path, monkeypatch, ready):
    from researchpilot.desktop import launcher

    controllers = []
    original_controller = launcher.DesktopController

    def capture(*args, **kwargs):
        controller = original_controller(*args, **kwargs)
        controllers.append(controller)
        return controller

    def exit_during_probe(port, identifier):
        assert controllers[0].on_exit is not None
        controllers[0].on_exit()
        return ready

    monkeypatch.setattr(launcher, "DesktopController", capture)
    monkeypatch.setattr(launcher, "instance_is_ready", exit_during_probe)
    assert launcher.launch(tmp_path, open_browser=False, tray_enabled=False) == 0
    assert not (tmp_path / "instance.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop launcher")
def test_unexpected_server_stop_during_startup_remains_an_error(tmp_path, monkeypatch):
    from researchpilot.desktop import launcher

    monkeypatch.setattr(launcher.uvicorn.Server, "run", lambda *args, **kwargs: None)
    with pytest.raises(OSError, match="本机服务未能启动"):
        launcher.launch(tmp_path, open_browser=False, tray_enabled=False)


@pytest.mark.parametrize("schema", [CritiqueReport, ResearchPlan])
def test_deepseek_uses_documented_json_and_nonthinking_payload(schema):
    import json

    payloads = []

    def respond(request):
        import json

        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatibleProvider(DesktopRuntimeSettings(provider="openai", api_key="test-key"))
    provider._client.close()
    provider._client = httpx.Client(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(respond)
    )
    try:
        original = ChatMessage(role="user", content="Return JSON")
        provider.complete([original], response_schema=schema)
        assert payloads[0]["response_format"] == {"type": "json_object"}
        assert payloads[0]["thinking"] == {"type": "disabled"}
        assert "frequency_penalty" not in payloads[0]
        instruction = payloads[0]["messages"][0]
        assert instruction["role"] == "system"
        assert json.loads(instruction["content"].split("\n", 1)[1]) == schema.model_json_schema()
        assert payloads[0]["messages"][-1] == original.as_dict()
        assert original.content == "Return JSON"
    finally:
        provider.close()


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "invalid_api_key"),
        (403, "invalid_api_key"),
        (402, "insufficient_balance"),
        (404, "model_unavailable"),
        (429, "rate_limited"),
        (503, "model_unavailable"),
    ],
)
def test_connection_failures_explain_recovery_without_echoing_vendor_body(monkeypatch, status, kind):
    client_class = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text="secret-test-key vendor detail")
    )
    monkeypatch.setattr(
        "researchpilot.desktop.api.httpx.Client", lambda **kwargs: client_class(transport=transport)
    )
    with pytest.raises(ConnectionFailure) as failure:
        probe_connection(DesktopConfig(), "secret-test-key")
    assert failure.value.kind == kind
    assert "secret-test-key" not in failure.value.message
    assert "环境变量" not in failure.value.message


@pytest.mark.parametrize(
    "error,kind",
    [
        (httpx.ConnectError("secret"), "connection_network"),
        (httpx.ReadTimeout("secret"), "connection_timeout"),
    ],
)
def test_connection_network_and_timeout_have_user_actions(monkeypatch, error, kind):
    client_class = httpx.Client

    def fail(request):
        raise error

    monkeypatch.setattr(
        "researchpilot.desktop.api.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(fail)),
    )
    with pytest.raises(ConnectionFailure) as failure:
        probe_connection(DesktopConfig(), "secret")
    assert failure.value.kind == kind and "secret" not in failure.value.message


def test_provider_errors_are_redacted_before_pipeline_history_and_trace():
    key = "custom-service-private-key"
    provider = OpenAICompatibleProvider(DesktopRuntimeSettings(api_key=key, max_retries=0))
    provider._client.close()
    provider._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text=f"Invalid credential: {key}")),
        base_url="https://api.deepseek.com",
    )
    try:
        with pytest.raises(LLMError) as failure:
            provider.complete([ChatMessage(role="user", content="test")])
        assert key not in str(failure.value) and "[redacted]" in str(failure.value)
    finally:
        provider.close()
