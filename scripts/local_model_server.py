"""Serve locally cached models through an OpenAI-compatible API (offline, no key).

This is the *development* inference server used to exercise ResearchPilot's real
provider path without a vendor credential or network access. It loads a cached
causal LM for ``/v1/chat/completions`` and a cached sentence-embedding model for
``/v1/embeddings``, and reports real token usage from the tokenizer.

    python scripts/local_model_server.py --port 8766
    RESEARCHPILOT_PROVIDER=openai API_KEY=local \
      BASE_URL=http://127.0.0.1:8766/v1 MODEL=Qwen/Qwen1.5-0.5B-Chat \
      python scripts/run_benchmark.py --provider openai --limit 6

It is intentionally simple (single worker, greedy decoding by default, no batching);
it exists for verification and demos, not for production throughput.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_generator(model_name: str, device: str) -> tuple[Any, Any, str]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    resolved = device
    if device == "auto":
        resolved = "cuda" if torch.cuda.is_available() else "cpu"
    # explicit low_cpu_mem_usage=False: the default meta-device initialisation makes
    # ``.to(device)`` raise "Cannot copy out of meta tensor" on this stack.
    # transformers ships stubs that type both the auto-class and PreTrainedModel as an
    # unusable `_Wrapped`, so the loaded model is annotated `Any` deliberately. The
    # call itself is verified at runtime (server runs on CPU and GPU).
    model: Any = AutoModelForCausalLM.from_pretrained(
        model_name,
        dtype=torch.float32,
        low_cpu_mem_usage=False,
    )
    model = model.to(resolved)
    if resolved.startswith("cuda"):
        model = model.half()
    model.eval()
    return tokenizer, model, resolved


class _LocalEmbedder:
    """text2vec-style BERT embedder with CLS pooling + L2 normalisation.

    Implemented directly on transformers because sentence-transformers 2.6 trips
    over meta-tensor initialisation with newer transformers releases.
    """

    def __init__(self, model_name: str, device: str) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name, dtype=torch.float32, low_cpu_mem_usage=False).to(
            device
        )
        self.model.eval()
        self.device = device

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self.torch.no_grad():
            batch = self.tokenizer(
                texts, padding=True, truncation=True, max_length=512, return_tensors="pt"
            ).to(self.device)
            hidden = self.model(**batch).last_hidden_state[:, 0]  # CLS pooling
            normalized = self.torch.nn.functional.normalize(hidden, p=2, dim=1)
        return [[float(x) for x in row] for row in normalized.cpu()]


def _load_embedder(model_name: str, device: str) -> Any | None:
    try:
        return _LocalEmbedder(model_name, device)
    except Exception as exc:  # pragma: no cover - optional path
        print(f"[local-model] embeddings disabled: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _schema_instruction(schema_name: str, schema: dict[str, Any]) -> str:
    """Small-model friendly structured-output instruction.

    Describing a schema is not enough for a 0.5B model: observed failures were
    echoing the schema, markdown fences, and putting prose into a ``dict[str, float]``
    field. A concrete skeleton with exact key names and value types works far better.
    """
    skeleton = json.dumps(_schema_skeleton(schema), ensure_ascii=False, indent=2)
    allowed = _enum_hints(schema)
    allowed_block = ("\nAllowed values (use one of them verbatim):\n" + "\n".join(allowed)) if allowed else ""
    return (
        f"Return ONE JSON object for {schema_name}. Use EXACTLY these keys and value "
        f"types; replace the placeholder values with your real answer. Output raw JSON "
        f"only - no markdown fences, nothing before or after.\n"
        f"Template:\n{skeleton}{allowed_block}"
    )


def _enum_hints(schema: dict[str, Any], *, root: dict[str, Any] | None = None, prefix: str = "") -> list[str]:
    """List ``key: a|b|c`` for every enum/Literal field so small models comply."""
    root = root if root is not None else schema
    if not isinstance(schema, dict):
        return []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target: Any = root
        for part in ref.lstrip("#/").split("/"):
            if isinstance(target, dict):
                target = target.get(part)
        return _enum_hints(target, root=root, prefix=prefix) if isinstance(target, dict) else []
    hints: list[str] = []
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        values = "|".join(str(v) for v in schema["enum"] if not isinstance(v, (dict, list)))
        if values:
            hints.append(f"  {prefix or '<value>'}: {values}")
    for any_of in schema.get("anyOf") or []:
        hints.extend(_enum_hints(any_of, root=root, prefix=prefix))
    properties = schema.get("properties") or {}
    for key, spec in properties.items():
        hints.extend(_enum_hints(spec or {}, root=root, prefix=f"{prefix}.{key}" if prefix else key))
    items = schema.get("items")
    if isinstance(items, dict):
        hints.extend(_enum_hints(items, root=root, prefix=f"{prefix}[]"))
    return hints


def _schema_skeleton(schema: dict[str, Any], *, root: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """Build a concrete placeholder instance from a JSON Schema."""
    if not isinstance(schema, dict):
        return None
    root = root if root is not None else schema
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target: Any = root
        for part in ref.lstrip("#/").split("/"):
            if isinstance(target, dict):
                target = target.get(part)
        if isinstance(target, dict):
            return _schema_skeleton(target, root=root, depth=depth)
        return None
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"]:
        return _schema_skeleton(schema["anyOf"][0], root=root, depth=depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties")
        if not properties:
            # dict[str, X] style fields -> {"": <placeholder>}
            return (
                {"": _schema_skeleton(additional, root=root, depth=depth + 1)}
                if isinstance(additional, dict)
                else {}
            )
        required = schema.get("required") or list(properties)
        return {
            key: _schema_skeleton(spec or {}, root=root, depth=depth + 1)
            for key, spec in properties.items()
            if key in required or depth <= 2
        }
    if kind == "array" or "items" in schema:
        items = schema.get("items")
        if isinstance(items, dict):
            sample = _schema_skeleton(items, root=root, depth=depth + 1)
            if sample not in (None, [], {}):
                return [sample]
        return []
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "string":
        return ""
    return None


def create_app(
    *,
    model_name: str = "Qwen/Qwen1.5-0.5B-Chat",
    embedding_model: str | None = "shibing624/text2vec-base-chinese",
    device: str = "auto",
    max_new_tokens: int = 1024,
) -> Any:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="ResearchPilot local model server", version="0.1.0")
    state: dict[str, Any] = {
        "tokenizer": None,
        "model": None,
        "device": device,
        "embedder": None,
        "load_error": None,
    }

    def ensure_loaded() -> None:
        state["load_error"] = None
        if state["model"] is None:
            started = time.perf_counter()
            try:
                tokenizer, model, resolved = _load_generator(model_name, device)
            except Exception as exc:
                state["load_error"] = f"chat model load failed: {type(exc).__name__}: {exc}"
                raise
            state.update({"tokenizer": tokenizer, "model": model, "device": resolved})
            print(f"[local-model] loaded {model_name} in {time.perf_counter() - started:.1f}s on {resolved}")
        if embedding_model and state["embedder"] is None:
            started = time.perf_counter()
            try:
                state["embedder"] = _load_embedder(embedding_model, str(state["device"]))
            except Exception as exc:
                state["embedder"] = None
                print(f"[local-model] embeddings disabled: {type(exc).__name__}: {exc}", file=sys.stderr)
            if state["embedder"] is not None:
                print(
                    f"[local-model] loaded embeddings {embedding_model} "
                    f"in {time.perf_counter() - started:.1f}s"
                )

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            ensure_loaded()
        except Exception as exc:
            return {"status": "error", "error": state["load_error"] or str(exc)}
        return {
            "status": "ok",
            "chat_model": model_name,
            "embedding_model": embedding_model,
            "device": state["device"],
            "embeddings_ready": state["embedder"] is not None,
            "load_error": state["load_error"],
        }

    @app.post("/v1/chat/completions")
    def chat(payload: dict[str, Any]) -> Any:
        import torch

        try:
            ensure_loaded()
        except Exception as exc:
            return JSONResponse(
                {"error": {"message": f"model load failed: {type(exc).__name__}: {exc}"}},
                status_code=503,
            )
        tokenizer = state["tokenizer"]
        model = state["model"]
        messages = [dict(m) for m in payload.get("messages", [])]
        response_format = payload.get("response_format") or {}
        schema = (response_format.get("json_schema") or {}) if isinstance(response_format, dict) else {}
        if schema:
            instruction = _schema_instruction(str(schema.get("name", "Response")), schema.get("schema") or {})
            messages.append({"role": "user", "content": instruction})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        temperature = float(payload.get("temperature") or 0.0)
        max_tokens = int(payload.get("max_tokens") or max_new_tokens)
        generate_kwargs: dict[str, Any] = {
            "max_new_tokens": min(max_tokens, max_new_tokens),
            "pad_token_id": tokenizer.eos_token_id,
        }
        if temperature > 0:
            generate_kwargs.update({"do_sample": True, "temperature": max(temperature, 0.01), "top_p": 0.95})
        else:
            generate_kwargs["do_sample"] = False
        started = time.perf_counter()
        with torch.no_grad():
            output = model.generate(**inputs, **generate_kwargs)
        generated = output[0][inputs["input_ids"].shape[1] :]
        text = tokenizer.decode(generated, skip_special_tokens=True).strip()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        prompt_tokens = int(inputs["input_ids"].shape[1])
        completion_tokens = int(generated.shape[0])
        return JSONResponse(
            {
                "id": f"chatcmpl-local-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(payload.get("model") or model_name),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "_meta": {"latency_ms": latency_ms, "device": state["device"]},
            }
        )

    @app.post("/v1/embeddings")
    def embeddings(payload: dict[str, Any]) -> Any:
        try:
            ensure_loaded()
        except Exception as exc:
            return JSONResponse(
                {"error": {"message": f"model load failed: {type(exc).__name__}: {exc}"}},
                status_code=503,
            )
        embedder = state["embedder"]
        if embedder is None:
            return JSONResponse({"error": {"message": "no embedding model loaded"}}, status_code=503)
        inputs = payload.get("input")
        texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
        # _LocalEmbedder already L2-normalises; sentence-transformers would accept
        # normalize_embeddings=True but that kwarg is not part of this interface.
        vectors = embedder.encode(texts)
        return JSONResponse(
            {
                "object": "list",
                "model": str(payload.get("model") or embedding_model),
                "data": [
                    {"object": "embedding", "index": index, "embedding": [float(x) for x in vector]}
                    for index, vector in enumerate(vectors)
                ],
            }
        )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible model server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--model", default=os.environ.get("LOCAL_CHAT_MODEL", "Qwen/Qwen1.5-0.5B-Chat"))
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("LOCAL_EMBEDDING_MODEL", "shibing624/text2vec-base-chinese"),
    )
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "cuda:0"])
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--no-embeddings", action="store_true")
    args = parser.parse_args()

    import uvicorn

    app = create_app(
        model_name=args.model,
        embedding_model=None if args.no_embeddings else args.embedding_model,
        device=args.device,
        max_new_tokens=args.max_new_tokens,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
