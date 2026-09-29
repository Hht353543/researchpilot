"""Desktop settings deliberately ignore shell variables and working-directory .env files."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from researchpilot.config import Settings
from researchpilot.persistence import atomic_write_text
from researchpilot.utils import project_root

ServiceName = Literal["deepseek", "openai", "custom"]
PRESETS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "custom": {"label": "其他兼容服务", "base_url": "", "model": ""},
}


class DesktopRuntimeSettings(Settings):
    desktop_mode: bool = True
    ai_service: ServiceName = "deepseek"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class DesktopConfig(BaseModel):
    service: ServiceName = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = Field(default="deepseek-flash", min_length=1, max_length=200)
    temperature: float = Field(default=0.2, ge=0, le=2)
    presence_penalty: float = Field(default=0, ge=-2, le=2)
    frequency_penalty: float = Field(default=0, ge=-2, le=2)
    max_tokens: int = Field(default=1200, ge=64, le=16000)
    top_k: int = Field(default=6, ge=1, le=20)
    max_iterations: int = Field(default=2, ge=1, le=4)
    research_task_timeout_s: float = Field(default=300, ge=30, le=3600)
    token_budget_live: int = Field(default=160000, ge=1000, le=1000000)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("请输入完整的 HTTP 或 HTTPS 服务地址，地址中不能包含凭据。")
        if parsed.query or parsed.fragment:
            raise ValueError("服务地址不能包含查询参数或片段。")
        return value.strip().rstrip("/")


class SettingsUpdate(DesktopConfig):
    api_key: str | None = Field(default=None, repr=False, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("请输入有效的 API Key。")
        return value


class SecretProtector(Protocol):
    def protect(self, value: str) -> str: ...

    def unprotect(self, value: str) -> str: ...


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsProtector:
    """Use current-user DPAPI; never fall back to plaintext credentials."""

    def _transform(self, data: bytes, *, encrypt: bool) -> bytes:
        if os.name != "nt":
            raise OSError("保存密钥需要 Windows 用户数据保护功能。")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = _Blob(len(data), buffer)
        target = _Blob()
        crypt = cast(Any, ctypes).WinDLL("crypt32", use_last_error=True)
        kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_Blob),
        ]
        function.restype = ctypes.c_int
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise OSError("无法保存或读取密钥，请在设置中重新输入。")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def protect(self, value: str) -> str:
        return base64.b64encode(self._transform(value.encode("utf-8"), encrypt=True)).decode("ascii")

    def unprotect(self, value: str) -> str:
        return self._transform(base64.b64decode(value, validate=True), encrypt=False).decode("utf-8")


def default_data_root() -> Path:
    location = os.environ.get("LOCALAPPDATA")
    if not location:
        raise OSError("无法找到当前用户的数据目录。")
    return Path(location) / "ResearchPilot"


class DesktopStore:
    def __init__(self, root: Path | None = None, *, protector: SecretProtector | None = None) -> None:
        self.root = (root or default_data_root()).resolve()
        self.protector = protector or WindowsProtector()
        self.config = DesktopConfig()
        self.api_key: str | None = None
        self.notice = ""

    @property
    def config_path(self) -> Path:
        return self.root / "settings.json"

    def prepare(self) -> None:
        for name in ("logs", "runs", "data"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        seed_dir = self.root / "data" / "knowledge_base"
        if not seed_dir.exists():
            shutil.copytree(project_root() / "data" / "knowledge_base", seed_dir)
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.config = DesktopConfig.model_validate(raw["config"])
            encrypted = raw.get("api_key_encrypted")
            self.api_key = self.protector.unprotect(encrypted) if encrypted else None
        except (OSError, ValueError, KeyError, TypeError):
            self.api_key = None
            self.notice = "已保存的连接设置无法读取，请重新输入 API Key。研究记录和资料仍保留。"

    def save(self, config: DesktopConfig, api_key: str | None) -> None:
        encrypted = self.protector.protect(api_key) if api_key else None
        atomic_write_text(
            self.config_path,
            json.dumps(
                {"version": 1, "config": config.model_dump(), "api_key_encrypted": encrypted},
                ensure_ascii=False,
                indent=2,
            ),
        )
        self.config, self.api_key, self.notice = config, api_key, ""

    def runtime_settings(
        self,
        config: DesktopConfig | None = None,
        *,
        api_key: str | None = None,
    ) -> DesktopRuntimeSettings:
        config = config or self.config
        values = config.model_dump(exclude={"service"})
        return DesktopRuntimeSettings(
            **values,
            ai_service=config.service,
            provider="openai",
            api_key=api_key,
            kb_path=str(self.root / "data" / "knowledge_base"),
            runs_path=str(self.root / "runs"),
            web_corpus_path=str(project_root() / "data" / "web_corpus"),
            pricing_file=str(project_root() / "configs" / "pricing.yaml"),
            mcp_transport="inprocess",
            embedding_provider="hash",
            bind_host="127.0.0.1",
        )

    def public_settings(self) -> dict[str, Any]:
        return {
            **self.config.model_dump(),
            "api_key_configured": bool(self.api_key),
            "presets": PRESETS,
            "notice": self.notice,
        }
