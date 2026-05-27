/**
 * Plugin-local TS types for the Code Conduit SPA panel.
 *
 * Mirror the dataclass shapes the service serializes — see
 * ``std-plugins/code-conduit/code_conduit_service.py``
 * (``_event_to_dict``) and the bus event payload in
 * ``_publish_event``. Field names match exactly so the page can
 * render either an in-memory ring-buffer row or a future
 * WS-pushed live event with the same renderer.
 */

export type CodingEventKind = "done" | "error" | "attention" | "info";

export interface CodingEvent {
  kind: CodingEventKind;
  /** Short, voice-friendly one-liner — what the agent did. */
  summary: string;
  /** Longer prose. Often empty for SSE events; richer for webhook deliveries. */
  detail: string;
  session_id: string;
  project_path: string;
  /** ISO-8601 string from the backend; "" when unavailable. */
  timestamp: string;
  /**
   * Backend's native event-type name (OpenCode's ``session.idle``,
   * Claude Code's ``stop_hook``, etc.). Carried through so the
   * feed shows operators what's firing under the hood.
   */
  raw_type: string;
}

export interface EventsListResult {
  type: "code.events.list.result";
  ref?: string;
  events: CodingEvent[];
  /**
   * False when the service is loaded but toggled off in
   * Settings → Services. Page renders a "service disabled"
   * banner so the empty feed isn't confusing.
   */
  enabled: boolean;
}

export interface SendResult {
  type: "code.send.result";
  ref?: string;
  ok: boolean;
  error?: string;
  session_id?: string;
  project_path?: string;
  backend?: string;
}
