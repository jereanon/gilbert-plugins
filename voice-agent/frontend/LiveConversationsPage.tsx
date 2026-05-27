/**
 * LiveConversationsPage — provider-agnostic live transcript viewer.
 *
 * Watches every active voice conversation Gilbert is in — Mentra
 * smart-glasses sessions, browser voice-agent sessions, future
 * modalities — by subscribing to the standard ``conversation.*``
 * bus events that every voice plugin publishes:
 *
 *   - conversation.session_started → add to the active-sessions list
 *   - conversation.transcript_turn → append turn to that session
 *   - conversation.session_ended   → mark session ended
 *
 * The component is plugin-coupling free: it doesn't care which
 * provider published an event, only that the event arrived. Adding
 * a new voice modality (phone, kiosk, etc.) requires nothing here
 * as long as the new plugin publishes the same three event types.
 */

import { useEffect, useMemo, useRef, useState, type ReactElement } from "react";

import { Card } from "@/components/ui/card";
import { useWebSocket } from "@/hooks/useWebSocket";

type Provider = "mentra" | "voice_agent" | string;

interface TranscriptTurn {
  who: string; // "them" | "us" | "system"
  text: string;
  ts: number; // seconds since session start
  receivedAt: number; // wall-clock ms (Date.now()) at arrival
  key: string;
}

interface LiveSession {
  sessionId: string;
  provider: Provider;
  userId: string;
  displayName: string;
  startedAt: string; // ISO
  endedAt: string | null;
  endedReason: string | null;
  turns: TranscriptTurn[];
}

function providerLabel(p: Provider): string {
  switch (p) {
    case "mentra":
      return "Glasses";
    case "voice_agent":
      return "Browser";
    case "phone_call":
      return "Phone";
    default:
      return p;
  }
}

function providerBadgeClass(p: Provider): string {
  // Token-based so it works in light + dark themes consistently.
  switch (p) {
    case "mentra":
      return "bg-purple-500/15 text-purple-700 dark:text-purple-300";
    case "voice_agent":
      return "bg-blue-500/15 text-blue-700 dark:text-blue-300";
    case "phone_call":
      return "bg-green-500/15 text-green-700 dark:text-green-300";
    default:
      return "bg-muted text-muted-foreground";
  }
}

function whoLabel(who: string): string {
  if (who === "them") return "User";
  if (who === "us") return "Gilbert";
  return "System";
}

function whoTurnClass(who: string): string {
  // Card-style turn rows that work against the design system's
  // surface color. Border-left coloring distinguishes speakers
  // without needing a separate background color.
  const base = "px-3 py-2 rounded-md border-l-4";
  if (who === "them") return `${base} border-foreground/30 bg-muted/30`;
  if (who === "us") return `${base} border-blue-500 bg-blue-500/5`;
  return `${base} border-amber-500 bg-amber-500/5 italic text-sm`;
}

function fmtTime(iso: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString();
  } catch {
    return "—";
  }
}

export function LiveConversationsPage(): ReactElement {
  const { connected, subscribe } = useWebSocket();
  const [sessions, setSessions] = useState<Map<string, LiveSession>>(
    new Map()
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  // session_started — add (or overwrite a stale ended) entry.
  useEffect(() => {
    const unsub = subscribe(
      "conversation.session_started",
      (event) => {
        const data = event.data ?? {};
        const sessionId = String(data.session_id ?? "");
        if (!sessionId) return;
        const provider = String(data.provider ?? "unknown");
        const userId = String(data.user_id ?? "");
        const displayName = String(data.display_name ?? "") || userId;
        const startedAt = String(data.started_at ?? new Date().toISOString());

        setSessions((prev) => {
          const next = new Map(prev);
          next.set(sessionId, {
            sessionId,
            provider,
            userId,
            displayName,
            startedAt,
            endedAt: null,
            endedReason: null,
            turns: [],
          });
          return next;
        });

        // Auto-select the new session if nothing is selected — saves
        // a click when the user opens the page mid-session.
        setSelectedId((prev) => prev ?? sessionId);
      }
    );
    return unsub;
  }, [subscribe]);

  // transcript_turn — append the turn to the matching session.
  // Synthesize a stub entry if session_started was missed.
  useEffect(() => {
    const unsub = subscribe(
      "conversation.transcript_turn",
      (event) => {
        const data = event.data ?? {};
        const sessionId = String(data.session_id ?? "");
        const who = String(data.who ?? "");
        const text = String(data.text ?? "");
        if (!sessionId || !who || !text) return;

        const ts =
          typeof data.ts === "number" ? data.ts : Number(data.ts ?? 0);
        const provider = String(data.provider ?? "unknown");
        const userId = String(data.user_id ?? "");

        const turn: TranscriptTurn = {
          who,
          text,
          ts,
          receivedAt: Date.now(),
          key: `${sessionId}-${ts}-${who}-${Math.random()
            .toString(36)
            .slice(2, 8)}`,
        };

        setSessions((prev) => {
          const next = new Map(prev);
          let entry = next.get(sessionId);
          if (!entry) {
            // Late-join recovery — page loaded after session start.
            entry = {
              sessionId,
              provider,
              userId,
              displayName: userId,
              startedAt: new Date().toISOString(),
              endedAt: null,
              endedReason: null,
              turns: [],
            };
          }
          next.set(sessionId, {
            ...entry,
            turns: [...entry.turns, turn],
          });
          return next;
        });

        setSelectedId((prev) => prev ?? sessionId);
      }
    );
    return unsub;
  }, [subscribe]);

  // session_ended — mark ended in place; don't remove so the
  // operator can still scroll back through the final transcript.
  useEffect(() => {
    const unsub = subscribe(
      "conversation.session_ended",
      (event) => {
        const data = event.data ?? {};
        const sessionId = String(data.session_id ?? "");
        if (!sessionId) return;
        const reason = String(data.reason ?? "");
        setSessions((prev) => {
          const next = new Map(prev);
          const entry = next.get(sessionId);
          if (entry && !entry.endedAt) {
            next.set(sessionId, {
              ...entry,
              endedAt: new Date().toISOString(),
              endedReason: reason || "closed",
            });
          }
          return next;
        });
      }
    );
    return unsub;
  }, [subscribe]);

  // Newest-first session order. Live sessions sort above ended ones;
  // within each bucket the most recent start floats to the top.
  const orderedSessions = useMemo(() => {
    const arr = Array.from(sessions.values());
    arr.sort((a, b) => {
      const aLive = a.endedAt == null ? 1 : 0;
      const bLive = b.endedAt == null ? 1 : 0;
      if (aLive !== bLive) return bLive - aLive;
      return b.startedAt.localeCompare(a.startedAt);
    });
    return arr;
  }, [sessions]);

  const selected = selectedId ? sessions.get(selectedId) ?? null : null;

  // Auto-scroll the transcript pane as new turns arrive.
  useEffect(() => {
    if (!selected) return;
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [selected, selected?.turns.length]);

  return (
    <div className="container mx-auto max-w-6xl py-8 px-4">
      <h1 className="text-2xl font-semibold mb-2">Live Conversations</h1>
      <p className="text-muted-foreground mb-6">
        Real-time transcripts from every active voice conversation —
        glasses, browser, phone, or any modality that publishes the
        standard conversation events. Read-only.
        {!connected && (
          <span className="ml-2 text-amber-600 dark:text-amber-400">
            (WebSocket disconnected — events not flowing)
          </span>
        )}
      </p>

      <div className="grid grid-cols-1 md:grid-cols-[20rem_1fr] gap-6">
        {/* Sidebar: session list */}
        <div>
          <h2 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wide">
            Sessions
          </h2>
          {orderedSessions.length === 0 ? (
            <Card className="p-4 text-sm text-muted-foreground">
              No active conversations.
              <p className="mt-2">
                Start a session in the Voice page, or put on a paired
                Mentra glasses device — new conversations appear here
                automatically.
              </p>
            </Card>
          ) : (
            <div className="space-y-2">
              {orderedSessions.map((s) => {
                const lastTurn = s.turns[s.turns.length - 1];
                const isSelected = s.sessionId === selectedId;
                const isLive = s.endedAt == null;
                return (
                  <button
                    key={s.sessionId}
                    type="button"
                    onClick={() => setSelectedId(s.sessionId)}
                    className={`w-full text-left rounded-lg border p-3 transition-colors hover:bg-muted/50 ${
                      isSelected
                        ? "border-blue-500 bg-blue-500/10"
                        : "border-border bg-card"
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span
                        className={`inline-block text-xs font-semibold px-2 py-0.5 rounded ${providerBadgeClass(
                          s.provider
                        )}`}
                      >
                        {providerLabel(s.provider)}
                      </span>
                      {isLive ? (
                        <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                          live
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          ended
                        </span>
                      )}
                      <span className="ml-auto text-xs text-muted-foreground">
                        {s.turns.length} turn
                        {s.turns.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    <div className="font-medium text-sm truncate">
                      {s.displayName || s.userId || "(unknown user)"}
                    </div>
                    {lastTurn && (
                      <div className="text-xs text-muted-foreground mt-1 truncate">
                        <span className="font-medium">
                          {whoLabel(lastTurn.who)}:
                        </span>{" "}
                        {lastTurn.text}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Main pane: selected session transcript */}
        <div>
          <h2 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wide">
            Transcript
          </h2>
          {!selected ? (
            <Card className="p-6 text-sm text-muted-foreground text-center">
              Select a conversation from the sidebar to view its
              transcript.
            </Card>
          ) : (
            <Card className="p-4">
              <div className="mb-4 pb-3 border-b">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span
                    className={`inline-block text-xs font-semibold px-2 py-0.5 rounded ${providerBadgeClass(
                      selected.provider
                    )}`}
                  >
                    {providerLabel(selected.provider)}
                  </span>
                  <span className="text-sm font-medium">
                    {selected.displayName ||
                      selected.userId ||
                      "(unknown user)"}
                  </span>
                  {selected.endedAt ? (
                    <span className="text-xs text-muted-foreground">
                      ended ({selected.endedReason || "closed"})
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                      <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                      live
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground font-mono">
                  {selected.sessionId} · started {fmtTime(selected.startedAt)}
                </div>
              </div>

              {selected.turns.length === 0 ? (
                <div className="text-sm text-muted-foreground italic">
                  Waiting for first turn…
                </div>
              ) : (
                <div className="space-y-2 max-h-[60vh] overflow-y-auto">
                  {selected.turns.map((t) => (
                    <div key={t.key} className={whoTurnClass(t.who)}>
                      <div className="text-xs font-semibold text-muted-foreground mb-1">
                        {whoLabel(t.who)}{" "}
                        <span className="opacity-60">
                          · t+{t.ts.toFixed(1)}s
                        </span>
                      </div>
                      <div className="text-sm whitespace-pre-wrap break-words">
                        {t.text}
                      </div>
                    </div>
                  ))}
                  <div ref={transcriptEndRef} />
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
