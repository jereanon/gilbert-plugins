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
 *
 * Layout:
 *   - Sidebar (left): list of sessions, newest first. Provider badge,
 *     user id / display name, status pill (live / ended), preview
 *     of the last turn.
 *   - Main pane (right): full transcript of the selected session.
 *     Auto-scrolls as new turns arrive. Read-only — this is a
 *     monitor, not a chat input.
 */

import { useEffect, useMemo, useRef, useState, type ReactElement } from "react";

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

function providerColor(p: Provider): string {
  // Tailwind hex-ish badge tints. Aim for distinct hues so
  // multi-provider sessions stand out at a glance.
  switch (p) {
    case "mentra":
      return "bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-200";
    case "voice_agent":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200";
    case "phone_call":
      return "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200";
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
  }
}

function whoLabel(who: string): string {
  if (who === "them") return "User";
  if (who === "us") return "Gilbert";
  return "System";
}

function whoStyle(who: string): string {
  if (who === "them") {
    return "bg-gray-50 dark:bg-gray-800 border-l-4 border-gray-400";
  }
  if (who === "us") {
    return "bg-blue-50 dark:bg-blue-900/30 border-l-4 border-blue-500";
  }
  return "bg-yellow-50 dark:bg-yellow-900/30 border-l-4 border-yellow-500 italic text-sm";
}

export function LiveConversationsPage(): ReactElement {
  const { connected, subscribe } = useWebSocket();
  const [sessions, setSessions] = useState<Map<string, LiveSession>>(
    new Map()
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  // session_started — add (or overwrite, in case a stale ended
  // session existed) a new entry.
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

  // transcript_turn — append the turn to the matching session's
  // transcript. If the session_started event was missed (page loaded
  // mid-conversation), synthesize a stub session entry so the
  // transcript still renders rather than getting dropped.
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
            // Late-join recovery: stub a session entry so the turn
            // has a home. The user-visible metadata is sparse but
            // at least the conversation surfaces.
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

        // Auto-select if nothing's selected, so the first user
        // utterance in a freshly-loaded page immediately renders
        // somewhere visible.
        setSelectedId((prev) => prev ?? sessionId);
      }
    );
    return unsub;
  }, [subscribe]);

  // session_ended — mark the session ended in place (don't remove
  // immediately so the operator can still scroll back through the
  // final transcript).
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
  // within each bucket sort by start time descending so the most
  // recent admit floats to the top.
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

  // Auto-scroll the transcript pane to the bottom as new turns
  // arrive on the currently-selected session.
  useEffect(() => {
    if (!selected) return;
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [selected, selected?.turns.length]);

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-gray-200 dark:border-gray-800 px-6 py-4">
        <h1 className="text-xl font-semibold">Live Conversations</h1>
        <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
          Real-time transcripts from every active voice conversation —
          glasses, browser, phone, anything that publishes the standard
          conversation events. Read-only.
          {!connected && (
            <span className="ml-2 text-amber-600">
              (WebSocket disconnected — events not flowing)
            </span>
          )}
        </p>
      </header>

      <div className="flex-1 flex min-h-0">
        {/* Sidebar: session list */}
        <aside className="w-80 border-r border-gray-200 dark:border-gray-800 overflow-y-auto">
          {orderedSessions.length === 0 ? (
            <div className="p-6 text-sm text-gray-500 dark:text-gray-400">
              No active conversations.
              <p className="mt-2">
                Start a session in the Voice page, or put on a paired
                Mentra glasses device. New conversations appear here
                automatically.
              </p>
            </div>
          ) : (
            <ul className="divide-y divide-gray-200 dark:divide-gray-800">
              {orderedSessions.map((s) => {
                const lastTurn = s.turns[s.turns.length - 1];
                const isSelected = s.sessionId === selectedId;
                const isLive = s.endedAt == null;
                return (
                  <li key={s.sessionId}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(s.sessionId)}
                      className={`w-full text-left px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-900 ${
                        isSelected
                          ? "bg-blue-50 dark:bg-blue-900/20 border-l-4 border-blue-500"
                          : ""
                      }`}
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <span
                          className={`inline-block text-xs font-semibold px-2 py-0.5 rounded ${providerColor(s.provider)}`}
                        >
                          {providerLabel(s.provider)}
                        </span>
                        {isLive ? (
                          <span className="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-300">
                            <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                            live
                          </span>
                        ) : (
                          <span className="text-xs text-gray-500 dark:text-gray-400">
                            ended
                          </span>
                        )}
                        <span className="ml-auto text-xs text-gray-500 dark:text-gray-400">
                          {s.turns.length} turn
                          {s.turns.length === 1 ? "" : "s"}
                        </span>
                      </div>
                      <div className="font-medium text-sm truncate">
                        {s.displayName || s.userId || "(unknown user)"}
                      </div>
                      {lastTurn && (
                        <div className="text-xs text-gray-600 dark:text-gray-400 mt-1 truncate">
                          <span className="font-medium">
                            {whoLabel(lastTurn.who)}:
                          </span>{" "}
                          {lastTurn.text}
                        </div>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* Main pane: selected session's transcript */}
        <main className="flex-1 overflow-y-auto p-6">
          {!selected ? (
            <div className="h-full flex items-center justify-center text-gray-500 dark:text-gray-400 text-sm">
              Select a conversation from the sidebar to view its
              transcript.
            </div>
          ) : (
            <div className="max-w-3xl mx-auto">
              <div className="mb-4 pb-4 border-b border-gray-200 dark:border-gray-800">
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className={`inline-block text-xs font-semibold px-2 py-0.5 rounded ${providerColor(selected.provider)}`}
                  >
                    {providerLabel(selected.provider)}
                  </span>
                  <span className="text-sm font-medium">
                    {selected.displayName ||
                      selected.userId ||
                      "(unknown user)"}
                  </span>
                  {selected.endedAt ? (
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      ended ({selected.endedReason || "closed"})
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-300">
                      <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                      live
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  session {selected.sessionId} · started{" "}
                  {selected.startedAt}
                </div>
              </div>

              {selected.turns.length === 0 ? (
                <div className="text-sm text-gray-500 dark:text-gray-400 italic">
                  Waiting for first turn…
                </div>
              ) : (
                <div className="space-y-2">
                  {selected.turns.map((t) => (
                    <div
                      key={t.key}
                      className={`px-3 py-2 rounded ${whoStyle(t.who)}`}
                    >
                      <div className="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-1">
                        {whoLabel(t.who)}{" "}
                        <span className="text-gray-400">
                          · t+{t.ts.toFixed(1)}s
                        </span>
                      </div>
                      <div className="text-sm whitespace-pre-wrap">
                        {t.text}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <div ref={transcriptEndRef} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
