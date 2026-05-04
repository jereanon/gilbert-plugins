"""Tool definitions for the browser plugin.

The dispatcher lives on ``BrowserService.execute_tool`` — this module
just declares the static ``ToolDefinition`` list so it can be imported
from one place.
"""

from __future__ import annotations

from gilbert.interfaces.tools import ToolDefinition, ToolParameter, ToolParameterType


_NAVIGATE_TOOL = ToolDefinition(
    name="browser_navigate",
    description=(
        "Navigate the browser to a URL. Returns the resolved URL and page "
        "title once the page has loaded. Use this before any extraction "
        "or interaction tool. The browser session is persistent per user "
        "— cookies and logged-in state carry across tool calls."
    ),
    parameters=[
        ToolParameter(
            name="url",
            type=ToolParameterType.STRING,
            description="Absolute URL to navigate to (must include scheme).",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_GET_TEXT_TOOL = ToolDefinition(
    name="browser_get_text",
    description=(
        "Return the visible text of the current page (or a CSS-scoped "
        "subtree). Whitespace is collapsed and the result is truncated to "
        "50_000 chars."
    ),
    parameters=[
        ToolParameter(
            name="selector",
            type=ToolParameterType.STRING,
            description=(
                "Optional CSS selector to scope extraction to. When "
                "omitted, returns the body text."
            ),
            required=False,
            default="",
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_GET_HTML_TOOL = ToolDefinition(
    name="browser_get_html",
    description=(
        "Return the HTML source of the current page (or a CSS-scoped "
        "subtree). Truncated to 200_000 chars."
    ),
    parameters=[
        ToolParameter(
            name="selector",
            type=ToolParameterType.STRING,
            description=(
                "Optional CSS selector to scope extraction to. When "
                "omitted, returns the full document HTML."
            ),
            required=False,
            default="",
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_SCREENSHOT_TOOL = ToolDefinition(
    name="browser_screenshot",
    description=(
        "Capture a PNG screenshot of the current page. The image will "
        "appear inline in your reply to the user. Use this when the page's "
        "visual layout matters (charts, maps, document scans, login "
        "verification screens)."
    ),
    parameters=[
        ToolParameter(
            name="full_page",
            type=ToolParameterType.BOOLEAN,
            description=(
                "When true, captures the entire scrollable page; when "
                "false (default), only the viewport."
            ),
            required=False,
            default=False,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_CLICK_TOOL = ToolDefinition(
    name="browser_click",
    description=(
        "Click the first element matching a CSS selector. Times out after "
        "15 seconds if no matching element appears."
    ),
    parameters=[
        ToolParameter(
            name="selector",
            type=ToolParameterType.STRING,
            description="CSS selector for the element to click.",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_FILL_TOOL = ToolDefinition(
    name="browser_fill",
    description=(
        "Fill a form input matching a CSS selector with the given value. "
        "Replaces the input's existing contents."
    ),
    parameters=[
        ToolParameter(
            name="selector",
            type=ToolParameterType.STRING,
            description="CSS selector for the input element.",
            required=True,
        ),
        ToolParameter(
            name="value",
            type=ToolParameterType.STRING,
            description="The value to fill in.",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_PRESS_TOOL = ToolDefinition(
    name="browser_press",
    description=(
        "Press a key (e.g. 'Enter', 'Tab', 'Escape', 'Control+A'). The "
        "key is dispatched to whatever element currently has focus."
    ),
    parameters=[
        ToolParameter(
            name="key",
            type=ToolParameterType.STRING,
            description="The key combination to press.",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)

_SELECT_TOOL = ToolDefinition(
    name="browser_select",
    description=(
        "Select an option from a <select> element by its value attribute."
    ),
    parameters=[
        ToolParameter(
            name="selector",
            type=ToolParameterType.STRING,
            description="CSS selector for the <select> element.",
            required=True,
        ),
        ToolParameter(
            name="value",
            type=ToolParameterType.STRING,
            description="The value attribute of the <option> to select.",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)


_EXTRACT_TOOL = ToolDefinition(
    name="browser_extract",
    description=(
        "Extract structured JSON from the current page using a small "
        "AI sampling call. Provide a free-form ``instruction`` describing "
        "what to pull out and an optional ``json_schema`` (as a string) "
        "that the result should match. Returns the JSON the model "
        "produced, or an error string if it couldn't be parsed."
    ),
    parameters=[
        ToolParameter(
            name="instruction",
            type=ToolParameterType.STRING,
            description=(
                "Plain-language description of what to extract — e.g. "
                "'list every product card with name, price, and rating'."
            ),
            required=True,
        ),
        ToolParameter(
            name="json_schema",
            type=ToolParameterType.STRING,
            description=(
                "Optional JSON Schema (as a string) the output should "
                "conform to. The model is told to match the schema; "
                "validation is best-effort."
            ),
            required=False,
            default="",
        ),
    ],
    required_role="user",
    parallel_safe=False,
)


_LOGIN_TOOL = ToolDefinition(
    name="browser_login",
    description=(
        "Log into a site using a credential the user has saved. The "
        "credential ID comes from the Settings → Browser → Credentials "
        "panel. Username and password never appear in the tool arguments "
        "— they're resolved server-side from the encrypted store."
    ),
    parameters=[
        ToolParameter(
            name="credential_id",
            type=ToolParameterType.STRING,
            description="The id of a saved browser credential.",
            required=True,
        ),
    ],
    required_role="user",
    parallel_safe=False,
)


# Read-only tools — safe to fan out across users since they only touch
# their own per-user Page. Within a single user they still serialize on
# the page lock, but parallel-safe is about cross-tool-call contention.
READ_ONLY_TOOLS: list[ToolDefinition] = [
    _NAVIGATE_TOOL,
    _GET_TEXT_TOOL,
    _GET_HTML_TOOL,
    _SCREENSHOT_TOOL,
]

# Interaction tools — share the same Page state as the read-only tools,
# so within a single AI turn they must serialize. We mark them
# parallel_safe=False to be conservative.
INTERACTION_TOOLS: list[ToolDefinition] = [
    _CLICK_TOOL,
    _FILL_TOOL,
    _PRESS_TOOL,
    _SELECT_TOOL,
    _LOGIN_TOOL,
]

# Smart tools — use the AI sampling capability under the hood.
SMART_TOOLS: list[ToolDefinition] = [_EXTRACT_TOOL]
