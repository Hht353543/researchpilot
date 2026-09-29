"""Minimal, dependency-free MCP/JSON-RPC protocol definitions."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "researchpilot-mcp"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """Structured JSON-RPC error carrying a code."""

    def __init__(self, code: int, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class McpToolSpec(BaseModel):
    name: str
    description: str
    inputSchema: dict[str, Any] = Field(default_factory=dict)


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def success(request_id: int | str | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: int | str | None, error: JsonRpcError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.as_dict()}


def text_content(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }
