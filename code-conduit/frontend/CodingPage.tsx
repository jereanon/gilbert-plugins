/**
 * /coding — Code Conduit activity feed + outbound compose form.
 *
 * Two-column layout (stacks on narrow viewports):
 *
 * - Left: a scrollable feed of recent inbound events from the
 *   coding agent. Severity-color-coded; default filter hides
 *   ``info``-grade events (tool calls, progress) so the feed
 *   stays readable. Filter chips let the user toggle severities.
 *
 * - Right: a "send to coder" form. Optional project alias + a
 *   "new session" toggle. Mirrors the ``code_send`` AI tool's
 *   parameter shape so the form is functionally a typed terminal
 *   for the same flow voice / chat uses.
 *
 * Polls every 5s via the underlying hook — no WS subscription
 * yet. Good enough for a notification panel; live tail can come
 * later if the latency becomes annoying.
 */

import { useCallback, useMemo, useState } from "react";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  HelpCircleIcon,
  InfoIcon,
  RefreshCwIcon,
  SendIcon,
  TerminalIcon,
} from "lucide-react";

import { PageHeader } from "@/components/layout/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { cn } from "@/lib/utils";

import { useCodeConduitApi } from "./api";
import type { CodingEvent, CodingEventKind } from "./types";

const KIND_LABELS: Record<CodingEventKind, string> = {
  done: "Done",
  error: "Error",
  attention: "Needs attention",
  info: "Info",
};

const KIND_BADGE_CLASS: Record<CodingEventKind, string> = {
  done: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  error: "bg-rose-500/15 text-rose-600 dark:text-rose-400",
  attention: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  info: "bg-muted text-muted-foreground",
};

const KIND_ICON: Record<
  CodingEventKind,
  typeof CheckCircle2Icon
> = {
  done: CheckCircle2Icon,
  error: AlertTriangleIcon,
  attention: HelpCircleIcon,
  info: InfoIcon,
};

type FeedFilter = CodingEventKind | "notable" | "all";

const FILTER_OPTIONS: { value: FeedFilter; label: string }[] = [
  { value: "notable", label: "Notable" },
  { value: "all", label: "All" },
  { value: "done", label: "Done" },
  { value: "error", label: "Error" },
  { value: "attention", label: "Attention" },
];

function applyClientFilter(
  events: CodingEvent[],
  filter: FeedFilter,
): CodingEvent[] {
  if (filter === "all") return events;
  if (filter === "notable") {
    return events.filter((e) => e.kind !== "info");
  }
  return events.filter((e) => e.kind === filter);
}

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // Same locale-bound rendering Mentra's debug feed uses —
  // matches the user's system clock without dragging in a date
  // library.
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function CodingPage(): React.ReactElement {
  const { events, enabled, eventsLoading, eventsError, reloadEvents } =
    useCodeConduitApi({ limit: 200 });

  const [filter, setFilter] = useState<FeedFilter>("notable");
  const filtered = useMemo(
    () => applyClientFilter(events, filter),
    [events, filter],
  );

  return (
    <div className="container mx-auto max-w-6xl py-8 px-4 space-y-6">
      <PageHeader
        title="Coding"
        description="Live feed of coding-agent activity + a compose form to fire a fresh relay."
        icon={TerminalIcon}
      />

      {!enabled && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
          The Code Conduit service is registered but disabled. Turn
          it on under <span className="font-mono">Settings → Services</span>{" "}
          and configure the backend at{" "}
          <span className="font-mono">Settings → Integrations → Code Conduit</span>{" "}
          before this feed starts populating.
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_22rem] gap-6">
        {/* Left: feed */}
        <div className="space-y-4">
          <FilterBar
            value={filter}
            onChange={setFilter}
            onRefresh={reloadEvents}
            loading={eventsLoading}
          />
          {eventsError && (
            <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-sm text-rose-600 dark:text-rose-400">
              Couldn't load events: {eventsError}
            </div>
          )}
          {filtered.length === 0 && !eventsLoading && (
            <EmptyFeed filter={filter} />
          )}
          <div className="space-y-2">
            {filtered.map((event, idx) => (
              <EventRow
                key={`${event.timestamp}-${event.raw_type}-${idx}`}
                event={event}
              />
            ))}
          </div>
        </div>

        {/* Right: compose */}
        <ComposePanel />
      </div>
    </div>
  );
}

interface FilterBarProps {
  value: FeedFilter;
  onChange: (next: FeedFilter) => void;
  onRefresh: () => void;
  loading: boolean;
}

function FilterBar(props: FilterBarProps): React.ReactElement {
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="flex flex-wrap gap-1">
        {FILTER_OPTIONS.map((opt) => (
          <Button
            key={opt.value}
            type="button"
            variant={props.value === opt.value ? "default" : "outline"}
            size="sm"
            onClick={() => props.onChange(opt.value)}
          >
            {opt.label}
          </Button>
        ))}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => void props.onRefresh()}
        disabled={props.loading}
        aria-label="Refresh"
      >
        <RefreshCwIcon
          className={cn("h-4 w-4", props.loading && "animate-spin")}
        />
      </Button>
    </div>
  );
}

interface EmptyFeedProps {
  filter: FeedFilter;
}

function EmptyFeed(props: EmptyFeedProps): React.ReactElement {
  const message =
    props.filter === "notable"
      ? "Nothing notable from the coding agent recently. Switch the filter to 'All' to see info-grade events like tool calls."
      : props.filter === "all"
        ? "No events in the buffer yet. The feed populates as the configured backend emits activity (OpenCode SSE / Claude Code stop hook / direct webhook posts)."
        : `No ${props.filter} events recorded yet.`;
  return (
    <div className="rounded-lg border border-dashed border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
      {message}
    </div>
  );
}

interface EventRowProps {
  event: CodingEvent;
}

function EventRow(props: EventRowProps): React.ReactElement {
  const { event } = props;
  const kind = (event.kind || "info") as CodingEventKind;
  const Icon = KIND_ICON[kind];
  return (
    <div className="rounded-lg border border-border bg-card p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon
            className={cn(
              "h-4 w-4 flex-shrink-0",
              kind === "done" && "text-emerald-600 dark:text-emerald-400",
              kind === "error" && "text-rose-600 dark:text-rose-400",
              kind === "attention" && "text-amber-600 dark:text-amber-400",
              kind === "info" && "text-muted-foreground",
            )}
          />
          <Badge
            variant="outline"
            className={cn("text-xs", KIND_BADGE_CLASS[kind])}
          >
            {KIND_LABELS[kind]}
          </Badge>
          {event.raw_type && (
            <span className="text-xs text-muted-foreground font-mono truncate">
              {event.raw_type}
            </span>
          )}
        </div>
        <span className="text-xs text-muted-foreground flex-shrink-0">
          {formatTimestamp(event.timestamp)}
        </span>
      </div>
      <div className="text-sm">{event.summary || <em>no summary</em>}</div>
      {event.detail && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer hover:text-foreground">
            details
          </summary>
          <pre className="mt-2 whitespace-pre-wrap break-words">
            {event.detail}
          </pre>
        </details>
      )}
      {(event.project_path || event.session_id) && (
        <div className="text-xs text-muted-foreground font-mono space-x-2">
          {event.project_path && <span>📂 {event.project_path}</span>}
          {event.session_id && <span>🔗 {event.session_id}</span>}
        </div>
      )}
    </div>
  );
}

function ComposePanel(): React.ReactElement {
  const { sendMessage, sendInFlight } = useCodeConduitApi();
  const [message, setMessage] = useState("");
  const [project, setProject] = useState("");
  const [newSession, setNewSession] = useState(false);
  const [status, setStatus] = useState<
    | { kind: "ok"; text: string }
    | { kind: "err"; text: string }
    | null
  >(null);

  const onSubmit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
      e.preventDefault();
      const trimmed = message.trim();
      if (!trimmed) return;
      setStatus(null);
      try {
        const res = await sendMessage({
          message: trimmed,
          project: project.trim(),
          new_session: newSession,
        });
        if (res.ok) {
          const dest =
            res.project_path || project.trim() || "the default project";
          setStatus({
            kind: "ok",
            text: `Sent to ${res.backend || "coding agent"} on ${dest}.`,
          });
          setMessage("");
          setNewSession(false);
        } else {
          setStatus({
            kind: "err",
            text: res.error || "Send failed.",
          });
        }
      } catch (err) {
        setStatus({
          kind: "err",
          text: err instanceof Error ? err.message : String(err),
        });
      }
    },
    [message, project, newSession, sendMessage],
  );

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-border bg-card p-4 space-y-3 h-fit sticky top-4"
    >
      <h2 className="font-semibold flex items-center gap-2">
        <SendIcon className="h-4 w-4" />
        Send to coding agent
      </h2>
      <p className="text-xs text-muted-foreground">
        Same path as the <span className="font-mono">code_send</span>{" "}
        AI tool / <span className="font-mono">/code send</span> slash
        command. Fire-and-forget — the agent's response (if any) will
        appear in the feed.
      </p>
      <textarea
        className={cn(
          "w-full min-h-[6rem] rounded-md border border-input bg-background",
          "px-3 py-2 text-sm",
          "focus:outline-none focus:ring-2 focus:ring-ring",
        )}
        placeholder="e.g. add error handling to the auth flow"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        disabled={sendInFlight}
      />
      <Input
        type="text"
        placeholder="project alias (optional)"
        value={project}
        onChange={(e) => setProject(e.target.value)}
        disabled={sendInFlight}
      />
      <label className="flex items-center gap-2 text-sm text-muted-foreground">
        <input
          type="checkbox"
          checked={newSession}
          onChange={(e) => setNewSession(e.target.checked)}
          disabled={sendInFlight}
        />
        New session (forget previous context)
      </label>
      <Button type="submit" disabled={sendInFlight || !message.trim()}>
        {sendInFlight ? (
          <>
            <LoadingSpinner className="mr-2 h-4 w-4" />
            Sending…
          </>
        ) : (
          <>
            <SendIcon className="mr-2 h-4 w-4" />
            Send
          </>
        )}
      </Button>
      {status && (
        <div
          className={cn(
            "text-xs rounded-md p-2",
            status.kind === "ok"
              ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              : "bg-rose-500/10 text-rose-600 dark:text-rose-400",
          )}
        >
          {status.text}
        </div>
      )}
    </form>
  );
}
