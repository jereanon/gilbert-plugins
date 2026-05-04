"""Anthropic Claude AI backend — AI via the Anthropic Messages API."""

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

_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_API_VERSION = "2023-06-01"


def _format_bytes(n: int) -> str:
    """Render a byte count as a short human-readable label (``1.2 MB``).

    Used only for the opaque-file attachment stub the model sees so it
    has a sense of scale when the user uploads a non-readable file.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"

_AVAILABLE_MODELS = [
    ModelInfo(
        id="claude-opus-4-20250514",
        name="Claude Opus 4",
        description="Most capable model — complex reasoning, nuanced writing, advanced coding.",
    ),
    ModelInfo(
        id="claude-sonnet-4-20250514",
        name="Claude Sonnet 4",
        description="Balanced performance and speed — strong all-around model.",
    ),
    ModelInfo(
        id="claude-haiku-4-5-20251001",
        name="Claude Haiku 4.5",
        description="Fastest and most cost-effective — ideal for simple tasks.",
    ),
]


class AnthropicAI(AIBackend):
    """AI backend using the Anthropic Messages API via httpx."""

    backend_name = "anthropic"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        all_model_ids = [m.id for m in _AVAILABLE_MODELS]
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
                key="api_key",
                type=ToolParameterType.STRING,
                description="Anthropic API key.",
                sensitive=True,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description=(
                    "Default model ID used when no per-request model is specified."
                ),
                default=_DEFAULT_MODEL,
                choices=tuple(all_model_ids),
            ),
            ConfigParam(
                key="enabled_models",
                type=ToolParameterType.ARRAY,
                description=(
                    "Models available for selection in the chat UI and model "
                    "tier mappings. Only enabled models can be assigned to tiers."
                ),
                default=all_model_ids,
                choices=tuple(all_model_ids),
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum tokens in a single AI response. Sonnet/Opus 4.x "
                    "support up to 64k. The AIService recovers from a max_tokens "
                    "cutoff via bounded text continuation, but a tool_use that "
                    "gets truncated mid-JSON is unrecoverable — keep this "
                    "comfortably above the largest tool input you expect."
                ),
                default=16384,
            ),
            ConfigParam(
                key="temperature",
                type=ToolParameterType.NUMBER,
                description="Temperature (0.0 = deterministic, 1.0 = creative).",
                default=0.7,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Send a tiny 'hi' message to the Anthropic API to verify the API key and model."
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
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="Anthropic backend is not initialized — save settings first.",
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
                message=f"Anthropic API error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to Anthropic (model: {response.model}).",
        )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._model: str = _DEFAULT_MODEL
        self._enabled_models: list[str] = [m.id for m in _AVAILABLE_MODELS]
        self._max_tokens: int = 16384
        self._temperature: float = 0.7

    async def initialize(self, config: dict[str, Any]) -> None:
        api_key = config.get("api_key")
        if not api_key:
            raise ValueError("AnthropicAI requires 'api_key' in config")

        self._model = str(config.get("model", _DEFAULT_MODEL))
        raw_enabled = config.get("enabled_models")
        if isinstance(raw_enabled, list) and raw_enabled:
            self._enabled_models = [str(m) for m in raw_enabled]
        self._max_tokens = int(config.get("max_tokens", 16384))
        self._temperature = float(config.get("temperature", 0.7))

        # Long read timeout: non-streaming completions on the largest
        # models (Opus, big system prompts, many tools) can easily hold
        # the connection past the previous 120s default before the
        # final byte arrives. Connect/write stay short so genuine
        # connectivity failures still surface fast.
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "x-api-key": str(api_key),
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=15.0,
                read=600.0,
                write=60.0,
                pool=15.0,
            ),
        )
        logger.info("Anthropic AI backend initialized (model=%s)", self._model)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def available_models(self) -> list[ModelInfo]:
        return [m for m in _AVAILABLE_MODELS if m.id in self._enabled_models]

    def capabilities(self) -> AIBackendCapabilities:
        return AIBackendCapabilities(
            streaming=True,
            attachments_user=True,
            parallel_tool_calls=True,
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if self._client is None:
            raise RuntimeError("AnthropicAI not initialized")

        body = self._build_request_body(request)

        ai_logger.debug(
            "Anthropic request: model=%s messages=%d", body["model"], len(body["messages"])
        )

        resp = await self._client.post("/messages", json=body)
        if resp.is_error:
            # Surface Anthropic's actual error body — raise_for_status() hides it.
            err_body: Any
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            ai_logger.warning(
                "Anthropic API error: status=%d body=%s request=%s",
                resp.status_code,
                err_body,
                json.dumps(body)[:2000],
            )
            # Pull the human-readable reason out of Anthropic's error envelope:
            # {"type": "error", "error": {"type": "...", "message": "..."}}
            reason = ""
            if isinstance(err_body, dict):
                err_obj = err_body.get("error")
                if isinstance(err_obj, dict):
                    reason = str(err_obj.get("message") or "").strip()
                if not reason:
                    reason = str(err_body.get("message") or "").strip()
            if not reason:
                reason = str(err_body)[:500]
            raise AIBackendError(
                f"Anthropic API rejected request ({resp.status_code}): {reason}",
                status=resp.status_code,
            )
        data = resp.json()

        ai_logger.debug(
            "Anthropic response: stop_reason=%s usage=%s",
            data.get("stop_reason"),
            data.get("usage"),
        )

        return self._parse_response(data)

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream Anthropic SSE events as provider-neutral ``StreamEvent``s.

        Hits ``POST /v1/messages`` with ``stream: true`` and parses the
        SSE response into a sequence of events terminating in exactly
        one ``MESSAGE_COMPLETE``. The mapping:

        - ``content_block_start`` (tool_use) → ``TOOL_CALL_START``
        - ``content_block_delta`` (text_delta) → ``TEXT_DELTA``
        - ``content_block_delta`` (input_json_delta) → ``TOOL_CALL_DELTA``
        - ``content_block_stop`` on a tool_use block → ``TOOL_CALL_END``
        - ``message_stop`` → ``MESSAGE_COMPLETE`` with the full ``AIResponse``

        All Anthropic-specific event names and field layouts stay inside
        this method — the core ``AIService`` agentic loop only ever sees
        neutral ``StreamEvent`` types, so a future OpenAI/Gemini/local
        backend can implement the same neutral surface without any
        changes outside its own ``generate_stream``.
        """
        if self._client is None:
            raise RuntimeError("AnthropicAI not initialized")

        body = self._build_request_body(request)
        body["stream"] = True

        ai_logger.debug(
            "Anthropic stream request: model=%s messages=%d",
            self._model,
            len(body["messages"]),
        )

        # Accumulators. Anthropic streams content blocks by index — a
        # single turn can have multiple text blocks interleaved with
        # tool_use blocks, and the block's type is known at start time
        # but not at stop time, so we track types by index.
        text_parts: list[str] = []
        block_types: dict[int, str] = {}
        tool_builders: dict[int, dict[str, Any]] = {}
        stop_reason_raw = "end_turn"
        usage_input = 0
        usage_output = 0
        usage_cache_creation = 0
        usage_cache_read = 0
        model_id = self._model

        async with self._client.stream(
            "POST",
            "/messages",
            json=body,
        ) as resp:
            if resp.is_error:
                err_bytes = await resp.aread()
                try:
                    err_body: Any = json.loads(err_bytes)
                except Exception:
                    err_body = err_bytes.decode("utf-8", errors="replace")
                ai_logger.warning(
                    "Anthropic stream API error: status=%d body=%s",
                    resp.status_code,
                    err_body,
                )
                reason = ""
                if isinstance(err_body, dict):
                    err_obj = err_body.get("error")
                    if isinstance(err_obj, dict):
                        reason = str(err_obj.get("message") or "").strip()
                    if not reason:
                        reason = str(err_body.get("message") or "").strip()
                if not reason:
                    reason = str(err_body)[:500]
                raise AIBackendError(
                    f"Anthropic API rejected streaming request "
                    f"({resp.status_code}): {reason}",
                    status=resp.status_code,
                )

            event_name = ""
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                # SSE: blank line terminates an event.
                if line == "":
                    if event_name and data_lines:
                        async for ev in self._dispatch_sse_event(
                            event_name,
                            "\n".join(data_lines),
                            text_parts,
                            block_types,
                            tool_builders,
                        ):
                            yield ev
                        # State updates driven off the raw payload so we
                        # can emit message_delta usage / stop_reason info
                        # without duplicating the parse logic.
                        try:
                            data = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            data = None
                        if isinstance(data, dict):
                            event_type = str(data.get("type") or event_name)
                            if event_type == "message_start":
                                msg = data.get("message") or {}
                                model_id = str(msg.get("model") or self._model)
                                usage = msg.get("usage") or {}
                                usage_input += int(usage.get("input_tokens", 0) or 0)
                                usage_output += int(usage.get("output_tokens", 0) or 0)
                                usage_cache_creation += int(
                                    usage.get("cache_creation_input_tokens", 0) or 0
                                )
                                usage_cache_read += int(
                                    usage.get("cache_read_input_tokens", 0) or 0
                                )
                                raw = msg.get("stop_reason")
                                if raw:
                                    stop_reason_raw = str(raw)
                            elif event_type == "message_delta":
                                delta = data.get("delta") or {}
                                raw = delta.get("stop_reason")
                                if raw:
                                    stop_reason_raw = str(raw)
                                usage = data.get("usage") or {}
                                usage_output += int(usage.get("output_tokens", 0) or 0)
                                # ``message_delta`` can also carry trailing cache
                                # accounting on some model versions. Add (don't
                                # overwrite) so partial counts from ``message_start``
                                # aren't lost.
                                usage_cache_creation += int(
                                    usage.get("cache_creation_input_tokens", 0) or 0
                                )
                                usage_cache_read += int(
                                    usage.get("cache_read_input_tokens", 0) or 0
                                )
                    event_name = ""
                    data_lines = []
                    continue
                if line.startswith(":"):
                    # SSE comment — ignore.
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())

        # Map the final stop_reason and assemble the full assistant message.
        if stop_reason_raw == "tool_use":
            stop_reason = StopReason.TOOL_USE
        elif stop_reason_raw == "max_tokens":
            stop_reason = StopReason.MAX_TOKENS
        else:
            stop_reason = StopReason.END_TURN

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_builders.keys()):
            builder = tool_builders[idx]
            raw_json = builder.get("json", "")
            try:
                args = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                # Streaming was cut off mid-JSON — leave args empty and
                # let the core loop's max_tokens handler surface the
                # truncation error to the user.
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
        usage = TokenUsage(
            input_tokens=usage_input,
            output_tokens=usage_output,
            cache_creation_tokens=usage_cache_creation,
            cache_read_tokens=usage_cache_read,
        )
        final_response = AIResponse(
            message=final_message,
            model=model_id,
            stop_reason=stop_reason,
            usage=usage,
        )
        ai_logger.debug(
            "Anthropic stream response: stop_reason=%s usage=%s",
            stop_reason_raw,
            {
                "input_tokens": usage_input,
                "output_tokens": usage_output,
                "cache_creation_input_tokens": usage_cache_creation,
                "cache_read_input_tokens": usage_cache_read,
            },
        )
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=final_response,
        )

    async def _dispatch_sse_event(
        self,
        event_name: str,
        data_str: str,
        text_parts: list[str],
        block_types: dict[int, str],
        tool_builders: dict[int, dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """Translate a single Anthropic SSE event into ``StreamEvent``(s).

        Mutates ``text_parts``, ``block_types``, and ``tool_builders`` in
        place so the outer ``generate_stream`` can assemble the final
        ``AIResponse`` after the stream ends.
        """
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return

        event_type = str(data.get("type") or event_name)

        if event_type == "content_block_start":
            idx = int(data.get("index") or 0)
            block = data.get("content_block") or {}
            btype = str(block.get("type") or "")
            block_types[idx] = btype
            if btype == "tool_use":
                tool_id = str(block.get("id") or "")
                tool_name = str(block.get("name") or "")
                tool_builders[idx] = {
                    "id": tool_id,
                    "name": tool_name,
                    "json": "",
                }
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_START,
                    tool_call_id=tool_id,
                    tool_name=tool_name,
                )
            return

        if event_type == "content_block_delta":
            idx = int(data.get("index") or 0)
            delta = data.get("delta") or {}
            dtype = str(delta.get("type") or "")
            if dtype == "text_delta":
                chunk = str(delta.get("text") or "")
                if chunk:
                    text_parts.append(chunk)
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text=chunk,
                    )
            elif dtype == "input_json_delta":
                partial = str(delta.get("partial_json") or "")
                builder = tool_builders.get(idx)
                if builder is not None and partial:
                    builder["json"] += partial
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call_id=str(builder.get("id", "")),
                        tool_name=str(builder.get("name", "")),
                        partial_json=partial,
                    )
            return

        if event_type == "content_block_stop":
            idx = int(data.get("index") or 0)
            if block_types.get(idx) == "tool_use":
                builder = tool_builders.get(idx) or {}
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL_END,
                    tool_call_id=str(builder.get("id", "")),
                    tool_name=str(builder.get("name", "")),
                )
            return

        # ``message_start`` / ``message_delta`` / ``message_stop`` / ``ping``
        # carry metadata only — the outer method reads them off the raw
        # payload for usage/stop_reason accounting. Nothing to yield here.

    # --- Request Building ---

    def _build_request_body(self, request: AIRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "max_tokens": self._max_tokens,
            "messages": self._build_messages(request.messages),
        }

        if request.system_prompt:
            body["system"] = request.system_prompt

        if request.tools:
            body["tools"] = self._build_tools(request.tools)

        body["temperature"] = self._temperature

        return body

    def _build_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to Anthropic content block format."""
        result: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # System messages are handled via the top-level 'system' param
                continue

            if msg.role == MessageRole.USER:
                if msg.attachments:
                    # Multimodal turn. Order per Anthropic's recommendation:
                    # images first, then documents, then text-kind blocks
                    # (which read as prompt context), then any opaque
                    # ``file`` stubs, then the user's own typed message
                    # last.
                    user_content: list[dict[str, Any]] = []
                    image_atts = [a for a in msg.attachments if a.kind == "image"]
                    doc_atts = [a for a in msg.attachments if a.kind == "document"]
                    text_atts = [a for a in msg.attachments if a.kind == "text"]
                    ref_atts = [a for a in msg.attachments if a.kind == "file"]
                    for img in image_atts:
                        if img.data:
                            user_content.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": img.media_type,
                                        "data": img.data,
                                    },
                                }
                            )
                        else:
                            user_content.append(
                                {
                                    "type": "text",
                                    "text": f"[Attached image: {img.name or 'image'} ({img.media_type}, {img.size} bytes) — use read_workspace_file or run_workspace_script to access]",
                                }
                            )
                    for doc in doc_atts:
                        if doc.data:
                            user_content.append(
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "base64",
                                        "media_type": doc.media_type or "application/pdf",
                                        "data": doc.data,
                                    },
                                }
                            )
                        else:
                            user_content.append(
                                {
                                    "type": "text",
                                    "text": f"[Attached document: {doc.name or 'document'} ({doc.media_type}, {doc.size} bytes) — use read_workspace_file or run_workspace_script to access]",
                                }
                            )
                    for txt in text_atts:
                        if txt.text:
                            user_content.append(
                                {
                                    "type": "text",
                                    "text": f"## {txt.name}\n\n{txt.text}",
                                }
                            )
                        else:
                            user_content.append(
                                {
                                    "type": "text",
                                    "text": f"[Attached file: {txt.name or 'file'} ({txt.media_type}, {txt.size} bytes) — use read_workspace_file or run_workspace_script to access]",
                                }
                            )
                    for ref in ref_atts:
                        # Prefer the explicit ``size`` field (filled
                        # in by the upload endpoint and by the parser
                        # for inline files) so we don't re-decode a
                        # potentially huge base64 string on every
                        # turn.
                        if ref.size:
                            size_label = _format_bytes(ref.size)
                        elif ref.data:
                            import base64 as _b64

                            try:
                                size = len(_b64.b64decode(ref.data, validate=False))
                                size_label = _format_bytes(size)
                            except Exception:
                                size_label = "unknown size"
                        else:
                            size_label = "unknown size"
                        mime = ref.media_type or "application/octet-stream"
                        # Show the workspace coordinates when the
                        # file was uploaded via the HTTP endpoint
                        # (reference mode). The AI uses these to
                        # write scripts that read the file — the
                        # ``skill_name`` is the pseudo-skill name and
                        # the ``path`` is how the script addresses
                        # the file from within the workspace (which
                        # is also its ``cwd``).
                        if ref.workspace_skill and ref.workspace_path:
                            location_hint = (
                                f"It lives on disk at workspace "
                                f"skill='{ref.workspace_skill}' "
                                f"path='{ref.workspace_path}'. Use "
                                "``run_workspace_script`` with "
                                f"skill_name='{ref.workspace_skill}' "
                                "to write and execute a Python or "
                                "bash script against it — the "
                                "script runs with the workspace as "
                                "its working directory, so it can "
                                f"open '{ref.workspace_path}' by its "
                                "bare relative path. Do NOT try to "
                                "read the whole file into context; "
                                "write a script that extracts what "
                                "you need (a count, a summary, a "
                                "parsed structure) and return the "
                                "result. If you need a script file "
                                "on disk first, use "
                                "``write_skill_workspace_file`` to "
                                "create it, then run it."
                            )
                        else:
                            # Legacy inline-file fallback — there's
                            # no disk path; the bytes ride in the
                            # frame and there's nothing the AI can
                            # do with them beyond acknowledging.
                            location_hint = (
                                "(Inline legacy upload — no "
                                "workspace path; you can't run "
                                "scripts against this one.)"
                            )
                        user_content.append(
                            {
                                "type": "text",
                                "text": (
                                    f"[Attached file: {ref.name or 'file'} "
                                    f"({size_label}, {mime}). "
                                    f"{location_hint}]"
                                ),
                            }
                        )
                    # ``kind="file"`` is the catch-all for attachments
                    # the model can't read natively (zips, videos,
                    # binaries, docx, step, dwg, …). The bytes live on
                    # disk in the conversation's per-user skill
                    # workspace tree (``users/<u>/conversations/<c>/
                    # chat-uploads/<name>``). We do NOT upload the
                    # contents to the model — they could easily be
                    # gigabytes, and the model doesn't have a parser
                    # for most formats anyway.
                    #
                    # Instead, the stub tells the model exactly how to
                    # reach the file from its Python sandbox: via the
                    # ``run_workspace_script`` tool with
                    # ``skill_name="chat-uploads"``. That runs a
                    # Python/bash script with the workspace as ``cwd``,
                    # so the script can open the file by its bare
                    # name and return a summary (line count, parsed
                    # features, header fields, whatever). The model
                    # sees the summary, not the raw bytes.
                    #
                    # ``chat-uploads`` is implicitly accessible —
                    # SkillService's activation gate bypasses the
                    # normal "is this skill active?" check for this
                    # synthetic skill name because the user uploaded
                    # the file, which is itself an explicit "look at
                    # this" signal.
                    if msg.content:
                        user_content.append({"type": "text", "text": msg.content})
                    if not user_content:
                        # All attachments were of an unknown kind; fall
                        # back to the plain-string form so Anthropic gets
                        # a valid message.
                        result.append({"role": "user", "content": msg.content})
                    else:
                        result.append({"role": "user", "content": user_content})
                else:
                    result.append({"role": "user", "content": msg.content})

            elif msg.role == MessageRole.ASSISTANT:
                # Slash-command turns are persisted as a single assistant row
                # carrying both ``tool_calls`` and ``tool_results`` (see
                # AIService._slash_command_chat). Anthropic requires the
                # ``tool_result`` to appear on a user-role message *immediately
                # after* the ``tool_use``, so we split such rows into the
                # canonical 3-message sequence here. This also heals any
                # historical conversations stored in the pre-fix shape.
                if msg.tool_calls and msg.tool_results:
                    result.append(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": tc.tool_call_id,
                                    "name": tc.tool_name,
                                    "input": tc.arguments,
                                }
                                for tc in msg.tool_calls
                            ],
                        }
                    )
                    tool_result_blocks: list[dict[str, Any]] = []
                    for tr in msg.tool_results:
                        tr_block: dict[str, Any] = {
                            "type": "tool_result",
                            "tool_use_id": tr.tool_call_id,
                            "content": tr.content,
                        }
                        if tr.is_error:
                            tr_block["is_error"] = True
                        tool_result_blocks.append(tr_block)
                    result.append({"role": "user", "content": tool_result_blocks})
                    # Preserve the assistant's narration of the result as a
                    # final assistant text turn so the next user message
                    # alternates correctly. Fall back to a short placeholder
                    # when the tool produced no text output (e.g. UI-block
                    # only) — an empty content array would be rejected.
                    result.append(
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": msg.content or "(done)"},
                            ],
                        }
                    )
                    continue

                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.tool_call_id,
                            "name": tc.tool_name,
                            "input": tc.arguments,
                        }
                    )
                result.append({"role": "assistant", "content": content})

            elif msg.role == MessageRole.TOOL_RESULT:
                content_blocks: list[dict[str, Any]] = []
                for tr in msg.tool_results:
                    block: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr.content,
                    }
                    if tr.is_error:
                        block["is_error"] = True
                    content_blocks.append(block)
                result.append({"role": "user", "content": content_blocks})

        return self._heal_dangling_tool_uses(result)

    @staticmethod
    def _heal_dangling_tool_uses(
        result: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pair every ``tool_use`` block with a matching ``tool_result``.

        Anthropic rejects a request if any ``tool_use`` id in an assistant
        message isn't immediately followed by a user message containing a
        ``tool_result`` block for that id. Historical conversations stored
        before the ``AIService`` max_tokens recovery landed can carry a
        dangling ``tool_use`` — e.g. a tool call whose JSON input was cut
        off mid-stream and whose matching ``tool_result`` was never
        produced. Loading such a conversation and appending a new user
        turn would otherwise blow up with ``messages.N: tool_use ids
        were found without tool_result blocks immediately after``.

        This pass walks the built message list and, for each dangling
        tool_use id, either folds a synthetic error ``tool_result`` into
        the following user message or inserts a whole synthetic user
        turn carrying just the ``tool_result`` blocks. The content of
        the synthetic ``tool_result`` explains that the tool call didn't
        complete, so the model sees the same "this got cut off" signal
        that the new max_tokens recovery code produces for fresh turns.
        """
        healed: list[dict[str, Any]] = []
        pending: list[str] = []

        for msg in result:
            role = msg.get("role")
            content = msg.get("content")

            if pending:
                if role == "user":
                    msg = AnthropicAI._prepend_synthetic_results(msg, pending)
                    pending = []
                elif role == "assistant":
                    # Two assistant messages in a row after a dangling
                    # tool_use — inject a standalone synthetic user turn
                    # between them so the API sees the right alternation.
                    healed.append(
                        {
                            "role": "user",
                            "content": AnthropicAI._synthetic_tool_results(
                                pending,
                            ),
                        }
                    )
                    pending = []

            healed.append(msg)

            # Record any fresh tool_use ids on this assistant message for
            # the next iteration to pair up.
            if role == "assistant" and isinstance(content, list):
                pending = [
                    str(b.get("id", ""))
                    for b in content
                    if isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("id")
                ]

        # Flush trailing dangling ids — caller is about to append a new
        # user message, but we need the synthetic tool_result to come
        # first so the assistant row's tool_use is immediately paired.
        if pending:
            healed.append(
                {
                    "role": "user",
                    "content": AnthropicAI._synthetic_tool_results(pending),
                }
            )

        return healed

    @staticmethod
    def _synthetic_tool_results(
        tool_use_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Build placeholder tool_result blocks for orphan tool_use ids."""
        return [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": (
                    "(Tool call was not completed — the previous turn was "
                    "cut off before this tool returned.)"
                ),
                "is_error": True,
            }
            for tid in tool_use_ids
        ]

    @staticmethod
    def _prepend_synthetic_results(
        user_msg: dict[str, Any],
        missing_ids: list[str],
    ) -> dict[str, Any]:
        """Prepend synthetic tool_result blocks to an existing user message.

        If the user message already has some of the needed tool_result
        blocks, only the missing ones get injected. Handles both the
        string-content and list-content shapes — plain-string messages
        are wrapped in a text block first.
        """
        content = user_msg.get("content")
        already_paired: set[str] = set()
        if isinstance(content, list):
            for b in content:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_result"
                    and b.get("tool_use_id")
                ):
                    already_paired.add(str(b["tool_use_id"]))
        still_missing = [tid for tid in missing_ids if tid not in already_paired]
        if not still_missing:
            return user_msg

        synthetic = AnthropicAI._synthetic_tool_results(still_missing)

        if isinstance(content, list):
            new_content: list[dict[str, Any]] = synthetic + content
        else:
            text = str(content or "")
            if text:
                new_content = synthetic + [{"type": "text", "text": text}]
            else:
                new_content = list(synthetic)

        return {**user_msg, "content": new_content}

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """Convert tool definitions to Anthropic tool schema format."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.to_json_schema(),
            }
            for tool in tools
        ]

    # --- Response Parsing ---

    def _parse_response(self, data: dict[str, Any]) -> AIResponse:
        """Parse Anthropic API response into an AIResponse."""
        content_text = ""
        tool_calls: list[ToolCall] = []

        for block in data.get("content", []):
            if block["type"] == "text":
                content_text += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_call_id=block["id"],
                        tool_name=block["name"],
                        arguments=block.get("input", {}),
                    )
                )

        # Map Anthropic stop_reason to our enum
        raw_stop = data.get("stop_reason", "end_turn")
        if raw_stop == "tool_use":
            stop_reason = StopReason.TOOL_USE
        elif raw_stop == "max_tokens":
            stop_reason = StopReason.MAX_TOKENS
        else:
            stop_reason = StopReason.END_TURN

        # Parse usage
        usage = None
        raw_usage = data.get("usage")
        if raw_usage:
            usage = TokenUsage(
                input_tokens=raw_usage.get("input_tokens", 0),
                output_tokens=raw_usage.get("output_tokens", 0),
                cache_creation_tokens=int(
                    raw_usage.get("cache_creation_input_tokens", 0) or 0
                ),
                cache_read_tokens=int(
                    raw_usage.get("cache_read_input_tokens", 0) or 0
                ),
            )

        message = Message(
            role=MessageRole.ASSISTANT,
            content=content_text,
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=message,
            model=data.get("model", self._model),
            stop_reason=stop_reason,
            usage=usage,
        )
