"""AWS Bedrock AI backend — AI via the Bedrock Converse API.

Bedrock doesn't expose an OpenAI-compatible endpoint; requests go to
``bedrock-runtime.<region>.amazonaws.com`` signed with AWS SigV4. The
`Converse API <https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html>`_
gives us a unified shape across model families (Anthropic Claude, Meta
Llama, Mistral, Amazon Nova, …) with first-class tool-calling and
streaming support.

We use boto3 (sync) wrapped with ``asyncio.to_thread`` for non-streaming
and run the blocking ``converse_stream`` event loop in a thread pool,
funneling events back to the async caller via an ``asyncio.Queue``.

Credentials resolve through boto3's default chain when the explicit
config fields are blank — useful for installations running on EC2,
ECS, or Lambda where IAM roles supply credentials automatically.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

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

_DEFAULT_REGION = "us-east-1"
# The Claude models on Bedrock use cross-region inference profiles —
# "us." / "eu." prefixed IDs automatically route to the right region.
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

_AVAILABLE_MODELS = [
    ModelInfo(
        id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        name="Claude Sonnet 4.5 (Bedrock)",
        description="Anthropic's flagship — strongest tool use and reasoning.",
    ),
    ModelInfo(
        id="us.anthropic.claude-opus-4-1-20250805-v1:0",
        name="Claude Opus 4.1 (Bedrock)",
        description="Anthropic's highest-intelligence tier.",
    ),
    ModelInfo(
        id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        name="Claude Haiku 4.5 (Bedrock)",
        description="Anthropic's fast tier — cheap and quick.",
    ),
    ModelInfo(
        id="us.meta.llama3-3-70b-instruct-v1:0",
        name="Llama 3.3 70B Instruct (Bedrock)",
        description="Meta's open-weight 70B chat model.",
    ),
    ModelInfo(
        id="mistral.mistral-large-2407-v1:0",
        name="Mistral Large 2407 (Bedrock)",
        description="Mistral's flagship reasoning model.",
    ),
    ModelInfo(
        id="us.amazon.nova-pro-v1:0",
        name="Amazon Nova Pro",
        description="Amazon's mid-tier flagship — strong multimodal.",
    ),
    ModelInfo(
        id="us.amazon.nova-lite-v1:0",
        name="Amazon Nova Lite",
        description="Amazon's fast tier — multimodal, cheap.",
    ),
    ModelInfo(
        id="us.amazon.nova-micro-v1:0",
        name="Amazon Nova Micro",
        description="Amazon's smallest Nova — text-only, extremely cheap.",
    ),
]


class BedrockAI(AIBackend):
    """AI backend using AWS Bedrock's Converse API via boto3."""

    backend_name = "bedrock"

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
                key="aws_region",
                type=ToolParameterType.STRING,
                description=(
                    "AWS region for the Bedrock runtime endpoint (e.g. "
                    "``us-east-1``, ``eu-west-1``). Must match where the "
                    "requested model is available; cross-region inference "
                    "profile IDs (``us.`` / ``eu.`` prefixed) route "
                    "automatically within that partition."
                ),
                default=_DEFAULT_REGION,
            ),
            ConfigParam(
                key="aws_access_key_id",
                type=ToolParameterType.STRING,
                description=(
                    "Optional AWS access key ID. Leave blank to use boto3's "
                    "default credential chain (env vars, ~/.aws/credentials, "
                    "EC2/ECS/Lambda IAM role)."
                ),
                default="",
            ),
            ConfigParam(
                key="aws_secret_access_key",
                type=ToolParameterType.STRING,
                description=(
                    "Optional AWS secret access key. Paired with "
                    "``aws_access_key_id``."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="aws_session_token",
                type=ToolParameterType.STRING,
                description=(
                    "Optional AWS session token for temporary credentials "
                    "(STS AssumeRole, SSO)."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description=(
                    "Default Bedrock model ID or inference profile ID. "
                    "Free-text because the available model catalog differs "
                    "per account/region and changes frequently — paste any "
                    "model ID from the Bedrock console."
                ),
                default=_DEFAULT_MODEL,
            ),
            ConfigParam(
                key="enabled_models",
                type=ToolParameterType.ARRAY,
                description=(
                    "Models suggested in the chat UI and AI profile editor. "
                    "The model field above accepts any ID, so this is just "
                    "a shortcut list for common choices."
                ),
                default=all_model_ids,
                choices=tuple(all_model_ids),
            ),
            ConfigParam(
                key="max_tokens",
                type=ToolParameterType.INTEGER,
                description=(
                    "Maximum tokens in a single AI response. Sent as "
                    "``inferenceConfig.maxTokens`` to Converse."
                ),
                default=8192,
            ),
            ConfigParam(
                key="temperature",
                type=ToolParameterType.NUMBER,
                description=(
                    "Sampling temperature (0.0 = deterministic, 1.0 = creative)."
                ),
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
                    "Send a tiny 'hi' message to the configured Bedrock "
                    "model to verify credentials and region."
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
                message="Bedrock backend is not initialized — save settings first.",
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
                message=f"Bedrock error: {exc}",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Connected to Bedrock (model: {response.model}).",
        )

    def __init__(self) -> None:
        self._client: Any | None = None  # boto3 client
        self._model: str = _DEFAULT_MODEL
        self._enabled_models: list[str] = [m.id for m in _AVAILABLE_MODELS]
        self._max_tokens: int = 8192
        self._temperature: float = 0.7

    async def initialize(self, config: dict[str, Any]) -> None:
        region = str(config.get("aws_region") or _DEFAULT_REGION).strip()
        if not region:
            raise ValueError("BedrockAI requires 'aws_region' in config")

        self._model = str(config.get("model", _DEFAULT_MODEL))
        raw_enabled = config.get("enabled_models")
        if isinstance(raw_enabled, list) and raw_enabled:
            self._enabled_models = [str(m) for m in raw_enabled]
        self._max_tokens = int(config.get("max_tokens", 8192))
        self._temperature = float(config.get("temperature", 0.7))

        access_key = str(config.get("aws_access_key_id") or "").strip() or None
        secret_key = str(config.get("aws_secret_access_key") or "").strip() or None
        session_token = str(config.get("aws_session_token") or "").strip() or None

        # boto3 clients are sync — creating one doesn't do network I/O,
        # but credential resolution (e.g. loading ~/.aws/credentials) can
        # touch the filesystem. Run it in a thread to keep init async.
        def _make_client() -> Any:
            return boto3.client(
                "bedrock-runtime",
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                config=BotoConfig(
                    # Bedrock invocations can be slow on cold models; the
                    # default 60s read timeout cuts off legitimate turns.
                    read_timeout=300,
                    connect_timeout=15,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )

        self._client = await asyncio.to_thread(_make_client)
        logger.info(
            "Bedrock AI backend initialized (region=%s, model=%s)",
            region,
            self._model,
        )

    async def close(self) -> None:
        # boto3 clients don't expose an async close; dropping the reference
        # is sufficient — the underlying HTTP pool reaps on GC.
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
            raise RuntimeError("BedrockAI not initialized")

        call_kwargs = self._build_converse_kwargs(request)

        ai_logger.debug(
            "Bedrock converse request: model=%s messages=%d",
            call_kwargs["modelId"],
            len(call_kwargs["messages"]),
        )

        client = self._client
        try:
            response = await asyncio.to_thread(client.converse, **call_kwargs)
        except ClientError as exc:
            raise self._error_from_client_error(exc) from exc
        except BotoCoreError as exc:
            raise AIBackendError(f"Bedrock call failed: {exc}") from exc

        ai_logger.debug(
            "Bedrock converse response: stopReason=%s usage=%s",
            response.get("stopReason"),
            response.get("usage"),
        )

        return self._parse_converse_response(response)

    async def generate_stream(
        self,
        request: AIRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream Bedrock Converse events as provider-neutral ``StreamEvent``s.

        ``converse_stream`` returns a synchronous iterator over event
        dicts, which we drive in a background thread and forward onto an
        ``asyncio.Queue``. The main coroutine consumes the queue and
        maps events to ``StreamEvent``s, accumulating usage and a final
        ``AIResponse`` for the ``MESSAGE_COMPLETE`` frame.

        Event shapes (from the Bedrock SDK):

        - ``messageStart`` — beginning of the assistant message
        - ``contentBlockStart`` — tool call begins; payload has ``toolUse.name`` + ``toolUseId``
        - ``contentBlockDelta`` — ``delta.text`` OR ``delta.toolUse.input`` chunks
        - ``contentBlockStop`` — end of the content block
        - ``messageStop`` — payload has ``stopReason``
        - ``metadata`` — payload has ``usage`` + latency
        """
        if self._client is None:
            raise RuntimeError("BedrockAI not initialized")

        call_kwargs = self._build_converse_kwargs(request)

        ai_logger.debug(
            "Bedrock converse_stream request: model=%s messages=%d",
            call_kwargs["modelId"],
            len(call_kwargs["messages"]),
        )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        _END = object()

        client = self._client

        def _pump() -> None:
            try:
                response = client.converse_stream(**call_kwargs)
                for event in response["stream"]:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:  # noqa: BLE001 - surface to caller
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _END)

        pump_task = asyncio.create_task(asyncio.to_thread(_pump))

        text_parts: list[str] = []
        # Accumulators keyed by contentBlockIndex — Bedrock streams tool
        # calls and text blocks with a stable integer index per block.
        tool_builders: dict[int, dict[str, Any]] = {}
        tool_started: set[int] = set()
        tool_ended: set[int] = set()
        stop_reason_raw = "end_turn"
        usage_input = 0
        usage_output = 0

        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, Exception):
                    if isinstance(item, ClientError):
                        raise self._error_from_client_error(item) from item
                    raise AIBackendError(
                        f"Bedrock stream failed: {item}"
                    ) from item

                event = item
                if not isinstance(event, dict):
                    continue

                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    idx = int(start.get("contentBlockIndex", 0))
                    start_payload = start.get("start") or {}
                    tool_use = start_payload.get("toolUse")
                    if isinstance(tool_use, dict):
                        builder = tool_builders.setdefault(
                            idx,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        builder["id"] = str(tool_use.get("toolUseId") or "")
                        builder["name"] = str(tool_use.get("name") or "")
                        if idx not in tool_started:
                            tool_started.add(idx)
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_id=builder["id"],
                                tool_name=builder["name"],
                            )
                    continue

                if "contentBlockDelta" in event:
                    delta_evt = event["contentBlockDelta"]
                    idx = int(delta_evt.get("contentBlockIndex", 0))
                    delta = delta_evt.get("delta") or {}
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=text,
                        )
                    tool_delta = delta.get("toolUse")
                    if isinstance(tool_delta, dict):
                        input_chunk = tool_delta.get("input")
                        if isinstance(input_chunk, str) and input_chunk:
                            builder = tool_builders.setdefault(
                                idx,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            builder["arguments"] += input_chunk
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call_id=builder.get("id", ""),
                                tool_name=builder.get("name", ""),
                                partial_json=input_chunk,
                            )
                    continue

                if "contentBlockStop" in event:
                    stop = event["contentBlockStop"]
                    idx = int(stop.get("contentBlockIndex", 0))
                    if idx in tool_builders and idx not in tool_ended:
                        tool_ended.add(idx)
                        builder = tool_builders[idx]
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_END,
                            tool_call_id=builder.get("id", ""),
                            tool_name=builder.get("name", ""),
                        )
                    continue

                if "messageStop" in event:
                    stop_reason_raw = str(
                        event["messageStop"].get("stopReason") or "end_turn"
                    )
                    continue

                if "metadata" in event:
                    usage = event["metadata"].get("usage") or {}
                    if isinstance(usage, dict):
                        usage_input = int(usage.get("inputTokens", usage_input) or 0)
                        usage_output = int(
                            usage.get("outputTokens", usage_output) or 0
                        )
                    continue
        finally:
            await pump_task

        stop_reason = self._map_stop_reason(stop_reason_raw)

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
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        final_message = Message(
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            tool_calls=tool_calls,
        )
        final_response = AIResponse(
            message=final_message,
            model=call_kwargs["modelId"],
            stop_reason=stop_reason,
            usage=TokenUsage(input_tokens=usage_input, output_tokens=usage_output),
        )
        ai_logger.debug(
            "Bedrock stream response: stopReason=%s usage=%s",
            stop_reason_raw,
            {"inputTokens": usage_input, "outputTokens": usage_output},
        )
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            response=final_response,
        )

    # --- Request building ---

    def _build_converse_kwargs(self, request: AIRequest) -> dict[str, Any]:
        model_id = request.model or self._model
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": self._build_messages(request.messages),
            "inferenceConfig": {
                "maxTokens": self._max_tokens,
                "temperature": self._temperature,
            },
        }
        if request.system_prompt:
            kwargs["system"] = [{"text": request.system_prompt}]
        if request.tools:
            kwargs["toolConfig"] = {
                "tools": [self._tool_spec(t) for t in request.tools],
            }
        return kwargs

    def _build_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to Bedrock Converse message format.

        Bedrock uses content-block arrays (``[{"text": "..."}, {"toolUse":
        {...}}, ...]``) per message, with a separate top-level ``system``
        field, similar to Anthropic's Messages API. Tool results live in
        a subsequent ``user``-role message with a ``toolResult`` block.
        """
        result: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # Converse only allows system text at the top level;
                # historical system rows are flattened into a user note.
                if msg.content:
                    result.append(
                        {
                            "role": "user",
                            "content": [{"text": f"[system] {msg.content}"}],
                        }
                    )
                continue

            if msg.role == MessageRole.USER:
                result.append(
                    {
                        "role": "user",
                        "content": self._user_content_blocks(msg),
                    }
                )

            elif msg.role == MessageRole.ASSISTANT:
                # Slash-command turns carry both tool_calls and tool_results
                # on one assistant row; split for Bedrock's turn shape
                # (assistant -> tool_use blocks, user -> tool_result blocks).
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": tc.tool_call_id,
                                "name": tc.tool_name,
                                "input": tc.arguments,
                            }
                        }
                    )
                if blocks:
                    result.append({"role": "assistant", "content": blocks})
                if msg.tool_results:
                    result.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "toolResult": {
                                        "toolUseId": tr.tool_call_id,
                                        "content": [{"text": tr.content}],
                                        "status": (
                                            "error" if tr.is_error else "success"
                                        ),
                                    }
                                }
                                for tr in msg.tool_results
                            ],
                        }
                    )

            elif msg.role == MessageRole.TOOL_RESULT:
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": tr.tool_call_id,
                                    "content": [{"text": tr.content}],
                                    "status": (
                                        "error" if tr.is_error else "success"
                                    ),
                                }
                            }
                            for tr in msg.tool_results
                        ],
                    }
                )

        return result

    @staticmethod
    def _user_content_blocks(msg: Message) -> list[dict[str, Any]]:
        """Build Bedrock content-blocks for a user message.

        Bedrock Converse accepts image blocks for vision-capable models
        (Claude, Nova) as ``{"image": {"format": "png", "source":
        {"bytes": b"..."}}}``. Documents and text attachments become
        text stubs pointing the model at the workspace tools.
        """
        import base64

        if not msg.attachments:
            return [{"text": msg.content}] if msg.content else [{"text": ""}]

        blocks: list[dict[str, Any]] = []

        for att in msg.attachments:
            if att.kind == "image" and att.data:
                # Bedrock wants raw bytes, not a base64 string.
                try:
                    raw = base64.b64decode(att.data)
                except Exception:
                    raw = b""
                if raw:
                    fmt = (att.media_type or "image/png").split("/")[-1]
                    # Converse accepts png / jpeg / gif / webp.
                    if fmt not in {"png", "jpeg", "jpg", "gif", "webp"}:
                        fmt = "png"
                    if fmt == "jpg":
                        fmt = "jpeg"
                    blocks.append(
                        {"image": {"format": fmt, "source": {"bytes": raw}}}
                    )
                    continue
            # Every other attachment becomes a text stub.
            kind_label = att.kind if att.kind != "file" else "file"
            blocks.append(
                {
                    "text": (
                        f"[Attached {kind_label}: {att.name or kind_label} "
                        f"({att.media_type}, {att.size} bytes) — use "
                        f"read_workspace_file or run_workspace_script "
                        f"to access]"
                    )
                }
            )
            if att.kind == "text" and att.text:
                blocks[-1] = {"text": f"## {att.name}\n\n{att.text}"}

        if msg.content:
            blocks.append({"text": msg.content})

        return blocks or [{"text": ""}]

    @staticmethod
    def _tool_spec(tool: ToolDefinition) -> dict[str, Any]:
        """Convert a ``ToolDefinition`` to Bedrock Converse's tool shape."""
        return {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": tool.to_json_schema()},
            }
        }

    # --- Response parsing ---

    @staticmethod
    def _map_stop_reason(raw: str) -> StopReason:
        if raw == "tool_use":
            return StopReason.TOOL_USE
        if raw == "max_tokens":
            return StopReason.MAX_TOKENS
        return StopReason.END_TURN

    def _parse_converse_response(self, data: dict[str, Any]) -> AIResponse:
        output = data.get("output") or {}
        message = output.get("message") or {}
        content_blocks = message.get("content") or []

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if "text" in block and isinstance(block["text"], str):
                text_parts.append(block["text"])
            tool_use = block.get("toolUse")
            if isinstance(tool_use, dict):
                args = tool_use.get("input")
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(
                    ToolCall(
                        tool_call_id=str(tool_use.get("toolUseId", "")),
                        tool_name=str(tool_use.get("name", "")),
                        arguments=args,
                    )
                )

        usage_raw = data.get("usage") or {}
        usage = None
        if isinstance(usage_raw, dict) and usage_raw:
            usage = TokenUsage(
                input_tokens=int(usage_raw.get("inputTokens", 0) or 0),
                output_tokens=int(usage_raw.get("outputTokens", 0) or 0),
            )

        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            tool_calls=tool_calls,
        )

        return AIResponse(
            message=assistant_msg,
            model=self._model,
            stop_reason=self._map_stop_reason(str(data.get("stopReason") or "")),
            usage=usage,
        )

    @staticmethod
    def _error_from_client_error(exc: ClientError) -> AIBackendError:
        err_response = exc.response or {}
        meta = err_response.get("ResponseMetadata") or {}
        status = int(meta.get("HTTPStatusCode") or 0) or None
        err_info = err_response.get("Error") or {}
        code = str(err_info.get("Code") or "")
        message = str(err_info.get("Message") or str(exc))
        ai_logger.warning(
            "Bedrock ClientError: status=%s code=%s message=%s",
            status,
            code,
            message,
        )
        prefix = "Bedrock rejected request"
        if status:
            prefix += f" ({status})"
        if code:
            prefix += f" [{code}]"
        return AIBackendError(f"{prefix}: {message}", status=status)
