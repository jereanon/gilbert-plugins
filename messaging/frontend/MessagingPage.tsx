// MessagingPage — bidirectional SMS thread view.
//
// Layout: two-pane.
//   left  = thread list (one row per remote number, newest first)
//   right = conversation view + compose box for the selected thread
//
// Lives at /messages and /messages/:otherNumber (deep-link variant —
// same component, just preselects a thread). The component reads the
// optional :otherNumber param via useParams() so one component
// services both routes (per the existing pattern in phone's
// PhoneCallsPage).
//
// Live updates: useMessaging subscribes to messaging.message_received
// + messaging.message_sent; we filter to whichever thread is open
// and append in-place so the conversation feels real-time.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { SendIcon } from "lucide-react";

import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { cn } from "@/lib/utils";

import { useMessaging } from "./useMessaging";
import type { MessagingMessage, MessagingThreadSummary } from "./types";

function timeAgo(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

const STATUS_TONE: Record<string, string> = {
  queued: "bg-muted text-muted-foreground",
  sent: "bg-info/15 text-info",
  delivered: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  received: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  failed: "bg-destructive/15 text-destructive",
};

export function MessagingPage() {
  const navigate = useNavigate();
  const { otherNumber: routeOtherNumber } = useParams();

  const {
    threads,
    threadsLoading,
    threadsError,
    reloadThreads,
    loadThread,
    send,
    subscribeMessages,
  } = useMessaging();

  const [activeOther, setActiveOther] = useState<string | null>(
    routeOtherNumber ?? null,
  );
  const [thread, setThread] = useState<MessagingMessage[]>([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [composeBody, setComposeBody] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // Reflect URL into active state on initial mount + deep-link clicks.
  useEffect(() => {
    if (routeOtherNumber && routeOtherNumber !== activeOther) {
      setActiveOther(routeOtherNumber);
    }
  }, [routeOtherNumber, activeOther]);

  // Pull thread history when the active thread changes.
  useEffect(() => {
    let cancelled = false;
    if (!activeOther) {
      setThread([]);
      return;
    }
    setThreadLoading(true);
    void loadThread(activeOther).then((msgs) => {
      if (!cancelled) {
        setThread(msgs);
        setThreadLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeOther, loadThread]);

  // Live-append messages for the active thread.
  useEffect(() => {
    return subscribeMessages((msg) => {
      if (!activeOther) return;
      if (msg.other_number !== activeOther) return;
      setThread((prev) => {
        // Idempotent — if we already saw this id, replace it (status
        // updates can re-fire) instead of duplicating.
        const existing = prev.findIndex(
          (m) => m.message_id === msg.message_id,
        );
        if (existing >= 0) {
          const next = prev.slice();
          next[existing] = msg;
          return next;
        }
        return [...prev, msg];
      });
    });
  }, [activeOther, subscribeMessages]);

  // Auto-scroll the conversation pane on new messages.
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [thread]);

  const handleSelect = useCallback(
    (t: MessagingThreadSummary) => {
      setActiveOther(t.other_number);
      navigate(`/messages/${encodeURIComponent(t.other_number)}`);
    },
    [navigate],
  );

  const handleSend = useCallback(async () => {
    const body = composeBody.trim();
    if (!body || !activeOther || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await send(activeOther, body);
      setComposeBody("");
    } catch (err) {
      setSendError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }, [activeOther, composeBody, send, sending]);

  const activeThreadSummary = useMemo(
    () => threads.find((t) => t.other_number === activeOther) ?? null,
    [threads, activeOther],
  );

  return (
    <div className="flex h-[100svh] flex-col">
      <PageHeader
        eyebrow="Messaging"
        title="Messages"
        description="Two-way text messages — Gilbert can read and send on your behalf."
      />

      <div className="flex flex-1 min-h-0 border-t border-border">
        {/* Thread list */}
        <aside className="w-72 shrink-0 overflow-y-auto border-r border-border">
          {threadsLoading && threads.length === 0 ? (
            <div className="flex items-center justify-center p-8 text-muted-foreground">
              <LoadingSpinner />
            </div>
          ) : threadsError ? (
            <div className="p-4 text-sm text-destructive">
              {threadsError}
            </div>
          ) : threads.length === 0 ? (
            <div className="p-4 text-sm text-muted-foreground">
              No threads yet. Send a message from a chat with{" "}
              <code className="text-xs">/msg send</code> or wait for an
              inbound text to land.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {threads.map((t) => {
                const isActive = t.other_number === activeOther;
                return (
                  <li key={`${t.our_number}::${t.other_number}`}>
                    <button
                      type="button"
                      onClick={() => handleSelect(t)}
                      className={cn(
                        "block w-full px-4 py-3 text-left transition-colors",
                        isActive
                          ? "bg-accent"
                          : "hover:bg-accent/50",
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-sm">
                          {t.other_number || "(unknown)"}
                        </span>
                        <span className="text-[10px] text-muted-foreground">
                          {timeAgo(t.last_message_at)}
                        </span>
                      </div>
                      <p className="mt-1 truncate text-xs text-muted-foreground">
                        {t.last_message_direction === "outbound"
                          ? "You: "
                          : ""}
                        {t.last_message_preview}
                      </p>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* Conversation pane */}
        <section className="flex flex-1 flex-col min-w-0">
          {activeOther ? (
            <>
              {activeThreadSummary && (
                <header className="border-b border-border px-5 py-3">
                  <div className="flex items-baseline gap-3">
                    <h2 className="font-mono text-base">
                      {activeOther}
                    </h2>
                    <span className="text-[11px] text-muted-foreground">
                      via {activeThreadSummary.our_number}
                    </span>
                  </div>
                </header>
              )}
              <div className="flex-1 overflow-y-auto px-5 py-4">
                {threadLoading ? (
                  <div className="flex items-center justify-center py-8 text-muted-foreground">
                    <LoadingSpinner />
                  </div>
                ) : thread.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No messages in this thread yet.
                  </p>
                ) : (
                  <ul className="flex flex-col gap-2.5">
                    {thread.map((m) => (
                      <MessageBubble key={m.message_id} message={m} />
                    ))}
                  </ul>
                )}
                <div ref={conversationEndRef} />
              </div>
              <Composer
                value={composeBody}
                onChange={setComposeBody}
                onSend={handleSend}
                sending={sending}
                error={sendError}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center text-muted-foreground">
              <p className="text-sm">
                Pick a thread from the left, or wait for one to appear.
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: MessagingMessage }) {
  const isUs = message.direction === "outbound";
  return (
    <li
      className={cn(
        "flex flex-col gap-1",
        isUs ? "items-end" : "items-start",
      )}
    >
      <div
        className={cn(
          "max-w-[80%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap break-words",
          isUs
            ? "bg-primary text-primary-foreground rounded-br-md"
            : "bg-muted rounded-bl-md",
        )}
      >
        {message.body}
      </div>
      <div className="flex items-center gap-2 px-1">
        <span className="text-[10px] text-muted-foreground">
          {formatTimestamp(message.created_at)}
        </span>
        {message.status && message.status !== "sent" && message.status !== "received" && (
          <Badge
            variant="outline"
            className={cn(
              "text-[9px] py-0",
              STATUS_TONE[message.status] ?? "",
            )}
          >
            {message.status}
          </Badge>
        )}
        {message.error && (
          <span className="text-[10px] text-destructive" title={message.error}>
            failed
          </span>
        )}
      </div>
    </li>
  );
}

function Composer({
  value,
  onChange,
  onSend,
  sending,
  error,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void | Promise<void>;
  sending: boolean;
  error: string | null;
}) {
  return (
    <div className="border-t border-border px-4 py-3">
      <div className="flex gap-2 items-end">
        <Textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void onSend();
            }
          }}
          placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
          className="min-h-[44px] max-h-32 text-sm"
          disabled={sending}
        />
        <Button
          type="button"
          size="sm"
          onClick={() => void onSend()}
          disabled={!value.trim() || sending}
        >
          <SendIcon className="size-3.5 mr-1" />
          Send
        </Button>
      </div>
      {error && (
        <p className="text-[11px] text-destructive mt-1.5">{error}</p>
      )}
    </div>
  );
}
