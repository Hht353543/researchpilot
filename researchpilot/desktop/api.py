"""Desktop-only REST surface. Credentials never appear in responses."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException

from researchpilot import __version__
from researchpilot.api.service import ServiceContainer
from researchpilot.desktop.settings import DesktopConfig, DesktopStore, SettingsUpdate
from researchpilot.security import safe_diagnostic

CONNECTION_ERRORS = {
    401: ("invalid_api_key", "API Key 无效，请检查后重新输入。"),
    403: ("invalid_api_key", "此 API Key 没有访问权限，请检查服务商和密钥。"),
    402: ("insufficient_balance", "账户余额不足，请到服务商账户充值后重试。"),
    404: ("model_unavailable", "找不到模型或服务地址，请在高级选项中检查。"),
    429: ("rate_limited", "服务请求过于频繁，请稍后再试。"),
}


class ConnectionFailure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind, self.message = kind, message


def probe_connection(config: DesktopConfig, api_key: str) -> None:
    """One small completion verifies credentials and the model, without retries."""
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 16,
    }
    if config.service == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.post(
                config.base_url + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code >= 400:
            kind, message = CONNECTION_ERRORS.get(
                response.status_code,
                ("model_unavailable", "模型服务暂时不可用，请稍后重试。"),
            )
            raise ConnectionFailure(kind, message)
        data = response.json()
        if not (data.get("choices") or [{}])[0].get("message", {}).get("content"):
            raise ValueError("no completion")
    except httpx.TimeoutException as exc:
        raise ConnectionFailure("connection_timeout", "模型连接超时，请检查网络后重试。") from exc
    except httpx.HTTPError as exc:
        raise ConnectionFailure("connection_network", "无法连接模型服务，请检查网络或服务地址。") from exc
    except (ValueError, AttributeError, IndexError, TypeError) as exc:
        raise ConnectionFailure(
            "model_unavailable", "服务返回了无法使用的响应，请检查服务地址和模型。"
        ) from exc


class DesktopController:
    def __init__(
        self,
        store: DesktopStore,
        *,
        instance_id: str = "",
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self.store, self.instance_id, self.on_exit = store, instance_id, on_exit

    def router(self, container: ServiceContainer) -> APIRouter:
        router = APIRouter(prefix="/desktop", tags=["desktop"])

        def candidate(update: SettingsUpdate) -> tuple[DesktopConfig, str]:
            config = DesktopConfig.model_validate(update.model_dump(exclude={"api_key"}))
            key = update.api_key
            if key is None:
                if (
                    config.service != self.store.config.service
                    or config.base_url != self.store.config.base_url
                ):
                    raise HTTPException(status_code=422, detail="切换服务商后，请输入该服务的 API Key。")
                key = self.store.api_key
            if not key:
                raise HTTPException(status_code=422, detail="请输入 API Key。")
            return config, key

        def check_idle() -> None:
            if container.pipeline.background_task_count:
                raise HTTPException(status_code=409, detail="研究正在进行，请等待完成或取消后修改设置。")

        def test(config: DesktopConfig, key: str) -> None:
            try:
                probe_connection(config, key)
            except ConnectionFailure as exc:
                raise HTTPException(
                    status_code=400, detail={"kind": exc.kind, "message": exc.message}
                ) from exc

        @router.get("/settings")
        def settings() -> dict[str, Any]:
            return self.store.public_settings()

        @router.post("/settings/test")
        def test_settings(update: SettingsUpdate) -> dict[str, Any]:
            config, key = candidate(update)
            test(config, key)
            return {"ok": True, "message": "连接成功。"}

        @router.put("/settings")
        def save_settings(update: SettingsUpdate) -> dict[str, Any]:
            check_idle()
            config, key = candidate(update)
            test(config, key)
            try:
                container.update_model_settings(
                    self.store.runtime_settings(config, api_key=key),
                    lambda: self.store.save(config, key),
                )
            except RuntimeError as exc:
                if str(exc) == "configuration_busy":
                    raise HTTPException(
                        status_code=409, detail="研究正在进行，请完成或取消后再保存。"
                    ) from exc
                raise
            return self.store.public_settings()

        @router.delete("/settings/api-key")
        def clear_key() -> dict[str, Any]:
            check_idle()
            try:
                container.update_model_settings(
                    self.store.runtime_settings(),
                    lambda: self.store.save(self.store.config, None),
                )
            except RuntimeError as exc:
                if str(exc) == "configuration_busy":
                    raise HTTPException(
                        status_code=409, detail="研究正在进行，请完成或取消后再清除。"
                    ) from exc
                raise
            return self.store.public_settings()

        @router.get("/diagnostics")
        def diagnostics() -> dict[str, Any]:
            log_path = self.store.root / "logs" / "researchpilot.log"
            logs = (
                log_path.read_text(encoding="utf-8", errors="replace")[-12000:] if log_path.exists() else ""
            )
            return {
                "version": __version__,
                "service": self.store.config.service,
                "model": self.store.config.model,
                "api_key_configured": bool(self.store.api_key),
                "active_tasks": container.pipeline.background_task_count,
                "knowledge_base": container.knowledge_base.stats(),
                "data_directory": str(self.store.root),
                "logs": safe_diagnostic(logs, secrets=(self.store.api_key,)),
                "notice": self.store.notice,
            }

        @router.post("/exit")
        def exit_program(background_tasks: BackgroundTasks) -> dict[str, Any]:
            background_tasks.add_task(self.on_exit or container.close)
            return {"ok": True, "message": "ResearchPilot 正在退出。下次双击程序即可重新打开。"}

        return router
