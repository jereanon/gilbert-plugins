"""Vendor-neutral OpenAI Chat Completions AI backend.

Complements the provider-specific AI plugins (``groq``, ``ollama``,
``openrouter``, ``xai``, …) by covering the long tail of installations
that point at something *those* plugins don't ship: a self-hosted vLLM
server, an LM Studio instance, a corporate gateway that proxies OpenAI
behind custom headers, a managed provider that hasn't gotten a
dedicated Gilbert plugin yet. Wherever the wire format is OpenAI Chat
Completions and the model catalog is your problem, this is the plugin.

What sets it apart from the other OpenAI-compat plugins:

- **Free-form model string.** No hardcoded catalog — each target
  endpoint has its own model IDs (``llama-3.1-70b-versatile``,
  ``mixtral-8x7b``, ``qwen2.5-coder``, …). The user types what their
  endpoint supports; the UI doesn't try to validate against a list.
  Use ``refresh_models`` (below) to populate a dropdown from the
  endpoint if you'd rather click than type.
- **No default ``base_url``.** Fails initialize with a clear message
  when unset — the whole point of the plugin is that the user picks
  the endpoint.
- **Optional API key.** Local proxies (vLLM, LM Studio, llama.cpp)
  often don't need one; when ``api_key`` is blank, no ``Authorization``
  header is sent.
- **Custom request headers.** Some proxies want bespoke auth
  (``x-api-key``, workspace headers, non-standard bearer prefixes).
  The ``request_headers`` multiline param lets the user supply
  arbitrary ``key: value`` pairs that get merged into every request.
- **Runtime model discovery.** Many endpoints implement ``GET /models``.
  The ``refresh_models`` action hits it and populates the in-memory
  model list; the UI picks it up via ``available_models()``.
- **Endpoint-support toggles.** ``supports_tools`` / ``supports_streaming``
  booleans — flip off for endpoints that 4xx when ``tools`` or
  ``stream: true`` is set (vanilla llama.cpp, some older proxies).

Everything else — request/response building, streaming SSE parsing,
attachment handling, tool-call delta aggregation — is the same
OpenAI Chat Completions shape every other OpenAI-compat plugin uses.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.ai import (
    AIBackend,
    AIBackendCapabilities,
    AIBackendError,
    AIRequest,
    AIResponse,
    Message,
    MessageRole,
    ModelInfo,
    StopReason,
    StreamEvent,
    StreamEventType,
    TokenUsage,
)
from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import (
    ToolCall,
    ToolDefinition,
    ToolParameterType,
)

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("gilbert.ai")


class OpenAICompatibleAI(AIBackend):
    """AI backend talking the OpenAI Chat Completions wire format to
    any OpenAI-compatible endpoint."""

    backend_name = "openai_compatible"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="enabled",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Initialize this backend at startup. Uncheck to hide its "
                    "settings and skip initialization."
                ),
                default=True,
            ),
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description=(
                    "Base URL of the OpenAI-compatible endpoint — e.g. "
                    "``http://vllm.internal/v1``, ``http://localhost:1234/v1`` "
                    "(LM Studio), or a corporate gateway that proxies OpenAI. "
                    "For providers with dedicated Gilbert plugins (Groq, "
                    "OpenRouter, Ollama, xAI, …) prefer those — they ship "
                    "curated model catalogs. No default — must be filled in."
                ),
                default="",
            ),
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description=(
                    "API key for the endpoint. Leave blank for local "
                    "proxies that don't require authentication."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description=(
                    "Default model ID. Free-form — type whatever the "
                    "target endpoint supports. Use the 'Refresh models' "
                    "action to pull the list from ``/models``."
                ),
                default="",
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum tokens in a single AI response. Local models "
                    "often have smaller context windows than OpenAI's, so "
                    "the default is deliberately conservative."
                ),
                default=4096,
            ),
            ConfigParam(
                key="temperature",
                type=ToolParameterType.NUMBER,
                description=("Sampling temperature (0.0 = deterministic, 1.0 = creative)."),
                default=0.7,
            ),
            ConfigParam(
                key="request_headers",
                type=ToolParameterType.STRING,
                description=(
                    "Extra headers to send on every request, one per line as "
                    "``key: value``. Useful for proxies with bespoke auth "
                    "(``x-api-key``, workspace headers, etc.). Lines starting "
                    "with ``#`` and blank lines are ignored."
                ),
                default="",
                multiline=True,
            ),
            ConfigParam(
                key="supports_tools",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Whether the endpoint implements tool/function calling. "
                    "Turn off for endpoints (vanilla llama.cpp, some local "
                    "proxies) that 4xx when ``tools`` is set — the backend "
                    "will strip tools from requests and refuse tool-requiring "
                    "calls with a clear error."
                ),
                default=True,
            ),
            ConfigParam(
                key="supports_streaming",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "Whether the endpoint implements SSE streaming. Turn off "
                    "for endpoints that choke on ``stream: true`` — the "
                    "backend will fall back to a single non-streaming request "
                    "per round."
                ),
                default=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=("Send a tiny 'hi' message to verify the endpoint and credentials."),
            ),
            ConfigAction(
                key="refresh_models",
                label="Refresh models",
                description=(
                    "List available models via ``GET /models`` and update the model dropdown."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        if key == "refresh_models":
            return await self._action_refresh_models()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message=("OpenAI-compatible backend is not initialized — save settings first."),
            )
        try:
            request = AIRequest(
                messages=[Message(role=MessageRole.USER, content="hi")],
                system_prompt="Reply with a single word.",
                tools=[],
            )
            response = await self.generate(request)
        except AIBackendError as exc:
            return ConfigActionResult(
                status="error",
                message=f"Endpoint error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected (model: {response.model}).",
        )

    async def _action_refresh_models(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message=("OpenAI-compatible backend is not initialized — save settings first."),
            )
        try:
            resp = await self._client.get("/models")
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Request failed: {exc}",
            )
        if resp.status_code == 404:
            return ConfigActionResult(
                status="error",
                message=(
                    "Endpoint doesn't implement ``/models``. Type the model "
                    "ID manually in the ``model`` field."
                ),
            )
        if resp.is_error:
            try:
                err_body: Any = resp.json()
            except Exception:
                err_body = resp.text
            reason = self._extract_error_reason(err_body)
            return ConfigActionResult(
                status="error",
                message=f"List models failed ({resp.status_code}): {reason}",
            )
        try:
            data = resp.json()
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"List models returned non-JSON: {exc}",
            )
        ids: list[str] = []
        for entry in data.get("data") or []:
            if isinstance(entry, dict):
                entry_id = entry.get("id")
                if isinstance(entry_id, str) and entry_id:
                    ids.append(entry_id)
        if not ids:
            return ConfigActionResult(
                status="error",
                message=(
                    "``/models`` returned no entries. Type the model ID "
                    "manually in the ``model`` field."
                ),
            )
        self._discovered_models = [
            ModelInfo(id=mid, name=mid, description="") for mid in sorted(set(ids))
        ]
        return ConfigActionResult(
            status="ok",
            message=f"Discovered {len(self._discovered_models)} model(s).",
            data={"model_ids": [m.id for m in self._discovered_models]},
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._model: str = ""
        self._max_tokens: int = 4096
        self._temperature: float = 0.7
        self._supports_tools: bool = True
        self._supports_streaming: bool = True
        # Populated by ``refresh_models``; in-memory only. Survives
        # config edits that don't tear down the backend, but not a full
        # restart — the user can re-run the action any time.
        self._discovered_models: list[ModelInfo] = []

    async def initialize(self, config: dict[str, Any]) -> None:
        base_url = str(config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError(
                "OpenAICompatibleAI requires 'base_url' in config "
                "(e.g. https://api.groq.com/openai/v1)"
            )

        self._model = str(config.get("model") or "")
        self._max_tokens = int(config.get("max_tokens", 4096))
        self._temperature = float(config.get("temperature", 0.7))
        self._supports_tools = bool(config.get("supports_tools", True))
        self._supports_streaming = bool(config.get("supports_streaming", True))

        headers: dict[str, str] = {
            "content-type": "application/json",
        }
        api_key = str(config.get("api_key") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        for hname, hval in _parse_header_lines(str(config.get("request_headers") or "")).items():
            headers[hname] = hval

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=120.0,
        )
        logger.info(
            "OpenAI-compatible AI backend initialized (base=%s model=%s)",
            base_url,
            self._model or "<none>",
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def available_models(self) -> list[ModelInfo]:
        """Return the user's discovered models if they've run refresh.

        Default is an empty list — there is no catalog to ship because
        the endpoint is whatever the user points at. The chat UI falls
        back to the free-form ``model`` field when this is empty.
        """
        return list(self._discovered_models)

    def capabilities(self) -> AIBackendCapabilities:
        return AIBackendCapabilities(
            streaming=self._supports_streaming,
            attachments_user=True,
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if self._client is None:
            raise RuntimeError("OpenAICompatibleAI not initialized")

        body = self._build_request_body(request)

        ai_logger.debug(
            "OpenAI-compat request: model=%s messages=%d",
            body["model"],
            len(body["messages"]),
        )

        resp = await self._client.post("/chat/completions", json=body)
        if resp.is_error:
            raise self._error_from_response(resp.status_code, resp, body)
        data = resp.json()

        ai_logger.debug(
            "OpenAI-compat response: finish_reason=%s usage=%s",
            self._first_finish_reason(data),
            data.get("usage"),
        )

        return self._parse_response(data)

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream OpenAI-compatible SSE chunks as provider-neutral events.

        Identical shape to the first-party OpenAI plugin. Falls back to
        ``generate()`` + a single ``MESSAGE_COMPLETE`` when the user has
        disabled streaming on an endpoint that doesn't support it.
        """
        if self._client is None:
            raise RuntimeError("OpenAICompatibleAI not initialized")

        if not self._supports_streaming:
            response = await self.generate(request)
            yield StreamEvent(
                type=StreamEventType.MESSAGE_COMPLETE,
                response=response,
            )
            return

        body = self._build_request_body(request)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        ai_logger.debug(
            "OpenAI-compat stream request: model=%s messages=%d",
            body["model"],
            len(body["messages"]),
        )

        text_parts: list[str] = []
        tool_builders: dict[int, dict[str, Any]] = {}
        tool_started: set[int] = set()
        tool_ended: set[int] = set()
        finish_reason_raw = "stop"
        usage_input = 0
        usage_output = 0
        model_id = body["model"]

        async with self._client.stream(
            "POST",
            "/chat/completions",
            json=body,
        ) as resp:
            if resp.is_error:
                err_bytes = await resp.aread()
                raise self._error_from_stream_body(
                    resp.status_code,
                    err_bytes,
                )

            async for line in resp.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                model_id = str(data.get("model") or model_id)
                usage = data.get("usage")
                if isinstance(usage, dict):
                    usage_input = int(usage.get("prompt_tokens", usage_input) or 0)
                    usage_output = int(usage.get("completion_tokens", usage_output) or 0)

                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue

                delta = choice.get("delta") or {}
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=content,
                        )
                    delta_tool_calls = delta.get("tool_calls")
                    if isinstance(delta_tool_calls, list):
                        for tc_delta in delta_tool_calls:
                            if not isinstance(tc_delta, dict):
                                continue
                            async for ev in self._ingest_tool_call_delta(
                                tc_delta,
                                tool_builders,
                                tool_started,
                            ):
                                yield ev

                raw_finish = choice.get("finish_reason")
                if raw_finish:
                    finish_reason_raw = str(raw_finish)
                    if finish_reason_raw == "tool_calls":
                        for idx in sorted(tool_builders.keys()):
                            if idx in tool_ended:
                                continue
                            builder = tool_builders[idx]
                            tool_ended.add(idx)
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_END,
                                tool_call_id=str(builder.get("id", "")),
                                tool_name=str(builder.get("name", "")),
                            )

        stop_reason = self._map_finish_reason(finish_reason_raw)

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_builders.keys()):
            builder = tool_builders[idx]
            raw_json = builder.get("arguments", "")
            try:
                args = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    tool_call_id=str(builder.get("id", "")),
                    tool_name=str(builder.get("name", "")),
                    arguments=args,
                )
            )

        final_message = Message(
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            tool_calls=tool_calls,
        )
        usage_obj = TokenUsage(
            input_tokens=usage_input,
            output_tokens=usage_output,
        )
        final_response = AIResponse(
            message=final_message,
            model=model_id,
            stop_reason=stop_reason,
            usage=usage_obj,
        )
        ai_logger.debug(
            "OpenAI-compat stream response: finish_reason=%s usage=%s",
            finish_reason_raw,
            {"prompt_tokens": usage_input, "completion_tokens": usage_output},
        )
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=final_response,
        )

    async def _ingest_tool_call_delta(
        self,
        tc_delta: dict[str, Any],
        tool_builders: dict[int, dict[str, Any]],
        tool_started: set[int],
    ) -> AsyncIterator[StreamEvent]:
        idx = int(tc_delta.get("index") or 0)
        builder = tool_builders.setdefault(
            idx,
            {"id": "", "name": "", "arguments": ""},
        )

        tc_id = tc_delta.get("id")
        if isinstance(tc_id, str) and tc_id:
            builder["id"] = tc_id

        fn = tc_delta.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name:
                builder["name"] = name
            args_chunk = fn.get("arguments")
            if isinstance(args_chunk, str) and args_chunk:
                builder["arguments"] += args_chunk
                if idx in tool_started:
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(builder.get("id", "")),
                        tool_name=str(builder.get("name", "")),
                        partial_json=args_chunk,
                    )

        if idx not in tool_started and builder.get("id") and builder.get("name"):
            tool_started.add(idx)
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                tool_call_id=str(builder["id"]),
                tool_name=str(builder["name"]),
            )
            buffered = str(builder.get("arguments", ""))
            if buffered:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_DELTA,
                    tool_call_id=str(builder["id"]),
                    tool_name=str(builder["name"]),
                    partial_json=buffered,
                )

    # --- Request Building ---

    def _build_request_body(self, request: AIRequest) -> dict[str, Any]:
        model = request.model or self._model
        if not model:
            raise AIBackendError(
                "No model configured — set the ``model`` field on the "
                "openai-compatible backend or pass ``model=`` per request.",
            )
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": self._build_messages(
                request.messages,
                request.system_prompt,
            ),
        }

        if request.tools:
            if not self._supports_tools:
                raise AIBackendError(
                    "Endpoint is configured with ``supports_tools=false`` "
                    "but this request carries tools. Disable tool-using "
                    "profiles for this backend, or enable tools in settings.",
                )
            body["tools"] = self._build_tools(request.tools)

        return body

    def _build_messages(
        self,
        messages: list[Message],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI Chat Completions format.

        Identical shape to the first-party ``openai`` plugin — system
        prompt as the first ``role=system`` row, tool calls as
        ``assistant.tool_calls[i]`` with JSON-string arguments, tool
        results as ``role=tool`` rows.
        """
        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                if msg.content:
                    result.append({"role": "system", "content": msg.content})
                continue

            if msg.role == MessageRole.USER:
                result.append(self._build_user_message(msg))

            elif msg.role == MessageRole.ASSISTANT:
                if msg.tool_calls and msg.tool_results:
                    result.append(
                        {
                            "role": "assistant",
                            "content": msg.content or None,
                            "tool_calls": [self._encode_tool_call(tc) for tc in msg.tool_calls],
                        }
                    )
                    for tr in msg.tool_results:
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr.tool_call_id,
                                "content": tr.content,
                            }
                        )
                    continue

                assistant_row: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or None,
                }
                if msg.tool_calls:
                    assistant_row["tool_calls"] = [
                        self._encode_tool_call(tc) for tc in msg.tool_calls
                    ]
                result.append(assistant_row)

            elif msg.role == MessageRole.TOOL_RESULT:
                for tr in msg.tool_results:
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.tool_call_id,
                            "content": tr.content,
                        }
                    )

        return result

    def _build_user_message(self, msg: Message) -> dict[str, Any]:
        """Build a user-role message, inlining any attachments.

        Same shape as the first-party OpenAI plugin: images as
        ``image_url`` parts, documents as text stubs (Chat Completions
        doesn't accept PDFs natively — if the target endpoint does
        support them, the user can still upload them and the model will
        see the stub, which is no worse than today's OpenAI behavior),
        text attachments inlined.
        """
        if not msg.attachments:
            return {"role": "user", "content": msg.content}

        image_atts = [a for a in msg.attachments if a.kind == "image"]
        doc_atts = [a for a in msg.attachments if a.kind == "document"]
        text_atts = [a for a in msg.attachments if a.kind == "text"]
        ref_atts = [a for a in msg.attachments if a.kind == "file"]

        parts: list[dict[str, Any]] = []

        for img in image_atts:
            if img.data:
                data_url = f"data:{img.media_type};base64,{img.data}"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"[Attached image: {img.name or 'image'} "
                            f"({img.media_type}, {img.size} bytes) — use "
                            f"read_workspace_file or run_workspace_script "
                            f"to access]"
                        ),
                    }
                )

        for doc in doc_atts:
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached document: {doc.name or 'document'} "
                        f"({doc.media_type}, {doc.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script "
                        f"to access]"
                    ),
                }
            )

        for txt in text_atts:
            if txt.text:
                parts.append(
                    {
                        "type": "text",
                        "text": f"## {txt.name}\n\n{txt.text}",
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"[Attached file: {txt.name or 'file'} "
                            f"({txt.media_type}, {txt.size} bytes) — use "
                            f"read_workspace_file or run_workspace_script "
                            f"to access]"
                        ),
                    }
                )

        for ref in ref_atts:
            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached file: {ref.name or 'file'} "
                        f"({ref.media_type}, {ref.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script "
                        f"to access]"
                    ),
                }
            )

        if msg.content:
            parts.append({"type": "text", "text": msg.content})

        if not parts:
            return {"role": "user", "content": msg.content}

        return {"role": "user", "content": parts}

    @staticmethod
    def _encode_tool_call(tc: ToolCall) -> dict[str, Any]:
        return {
            "id": tc.tool_call_id,
            "type": "function",
            "function": {
                "name": tc.tool_name,
                "arguments": json.dumps(tc.arguments),
            },
        }

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.to_json_schema(),
                },
            }
            for tool in tools
        ]

    # --- Error & Response Parsing ---

    def _error_from_response(
        self,
        status: int,
        resp: httpx.Response,
        body: dict[str, Any],
    ) -> AIBackendError:
        err_body: Any
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        ai_logger.warning(
            "OpenAI-compat API error: status=%d body=%s request=%s",
            status,
            err_body,
            json.dumps(body)[:2000],
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"Endpoint rejected request ({status}): {reason}",
            status=status,
        )

    def _error_from_stream_body(
        self,
        status: int,
        err_bytes: bytes,
    ) -> AIBackendError:
        try:
            err_body: Any = json.loads(err_bytes)
        except Exception:
            err_body = err_bytes.decode("utf-8", errors="replace")
        ai_logger.warning(
            "OpenAI-compat stream API error: status=%d body=%s",
            status,
            err_body,
        )
        reason = self._extract_error_reason(err_body)
        return AIBackendError(
            f"Endpoint rejected streaming request ({status}): {reason}",
            status=status,
        )

    @staticmethod
    def _extract_error_reason(err_body: Any) -> str:
        reason = ""
        if isinstance(err_body, dict):
            err_obj = err_body.get("error")
            if isinstance(err_obj, dict):
                reason = str(err_obj.get("message") or "").strip()
            elif isinstance(err_obj, str):
                reason = err_obj.strip()
            if not reason:
                reason = str(err_body.get("message") or "").strip()
        if not reason:
            reason = str(err_body)[:500]
        return reason

    @staticmethod
    def _first_finish_reason(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            return str(choices[0].get("finish_reason") or "")
        return ""

    @staticmethod
    def _map_finish_reason(raw: str) -> StopReason:
        if raw == "tool_calls" or raw == "function_call":
            return StopReason.TOOL_USE
        if raw == "length":
            return StopReason.MAX_TOKENS
        return StopReason.END_TURN

    def _parse_response(self, data: dict[str, Any]) -> AIResponse:
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        raw_content = message.get("content")
        content_text = raw_content if isinstance(raw_content, str) else ""

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    tool_call_id=str(tc.get("id", "")),
                    tool_name=str(fn.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        stop_reason = self._map_finish_reason(str(choice.get("finish_reason") or ""))

        usage = None
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = TokenUsage(
                input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
            )

        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content=content_text,
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=assistant_msg,
            model=str(data.get("model") or self._model),
            stop_reason=stop_reason,
            usage=usage,
        )


def _parse_header_lines(raw: str) -> dict[str, str]:
    """Parse a multi-line ``key: value`` blob into a header dict.

    Tolerates blank lines and ``#`` comments. Whitespace around the
    colon is stripped. Silently drops lines that don't contain a colon,
    rather than raising — the user shouldn't have to reboot the backend
    over a typo caught at save time.
    """
    out: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key:
            out[key] = value
    return out
