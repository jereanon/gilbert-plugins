/**
 * Plugin-local API hook for the messaging service.
 *
 * Wraps the three WS RPCs MessagingService exposes (`threads.list`,
 * `thread.get`, `send`) plus subscriptions to the three bus events
 * (`messaging.message_received`, `messaging.message_sent`,
 * `messaging.thread_updated`). Lives in the plugin's own frontend
 * directory so core's ``useWsApi`` stays generic — per the rule §9
 * extension policy.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type {
  MessagingMessage,
  MessagingThreadSummary,
  MessageReceivedEvent,
  MessageSentEvent,
  ThreadUpdatedEvent,
} from "./types";

interface ThreadsListResult {
  threads: MessagingThreadSummary[];
}

interface ThreadGetResult {
  other_number: string;
  our_number: string;
  messages: MessagingMessage[];
}

interface SendResult {
  message_id: string;
  status: string;
}

/**
 * Public surface returned by ``useMessaging``. Components call the
 * three methods directly and watch the four reactive fields.
 */
export interface UseMessagingApi {
  threads: MessagingThreadSummary[];
  threadsLoading: boolean;
  threadsError: string | null;

  /** Refetch the thread list. Idempotent; safe to call repeatedly. */
  reloadThreads: () => Promise<void>;

  /** Pull the full message history for one thread. The SPA caches
   *  results in component state — the hook itself is request-driven. */
  loadThread: (otherNumber: string) => Promise<MessagingMessage[]>;

  /** Send a message. The optimistic-render path lives in the caller
   *  — this hook just resolves once the carrier accepts. */
  send: (toNumber: string, body: string) => Promise<SendResult>;

  /** Subscribe to live message events for the active thread. Returns
   *  the unsubscribe function. */
  subscribeMessages: (
    onMessage: (msg: MessagingMessage) => void,
  ) => () => void;
}

export function useMessaging(): UseMessagingApi {
  const { connected, rpc, subscribe } = useWebSocket();
  const [threads, setThreads] = useState<MessagingThreadSummary[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(false);
  const [threadsError, setThreadsError] = useState<string | null>(null);

  const reloadThreads = useCallback(async (): Promise<void> => {
    if (!connected) return;
    setThreadsLoading(true);
    setThreadsError(null);
    try {
      const res = await rpc<ThreadsListResult>({
        type: "messaging.threads.list",
      });
      setThreads(res.threads || []);
    } catch (err) {
      setThreadsError(
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setThreadsLoading(false);
    }
  }, [connected, rpc]);

  // Auto-load on first connection.
  useEffect(() => {
    if (connected) void reloadThreads();
  }, [connected, reloadThreads]);

  // ``thread_updated`` fires for both inbound + outbound. Just
  // re-list — the thread set is small (one per remote number) so
  // a refetch is cheap and avoids drift if multiple SPAs are open.
  useEffect(() => {
    const unsub = subscribe("messaging.thread_updated", (_evt) => {
      void reloadThreads();
    });
    return unsub;
  }, [subscribe, reloadThreads]);

  const loadThread = useCallback(
    async (otherNumber: string): Promise<MessagingMessage[]> => {
      if (!connected) return [];
      const res = await rpc<ThreadGetResult>({
        type: "messaging.thread.get",
        other_number: otherNumber,
      });
      return res.messages || [];
    },
    [connected, rpc],
  );

  const send = useCallback(
    async (toNumber: string, body: string): Promise<SendResult> => {
      const res = await rpc<SendResult>({
        type: "messaging.send",
        to_number: toNumber,
        body,
      });
      // Refresh thread list so the new message lands in the sidebar.
      void reloadThreads();
      return res;
    },
    [rpc, reloadThreads],
  );

  /**
   * Bus-subscribe to both directions. The caller filters by
   * ``other_number`` against the active thread; we don't gate it
   * here because the same hook instance services every thread the
   * user is looking at sequentially.
   */
  const subscribeMessages = useCallback(
    (onMessage: (msg: MessagingMessage) => void) => {
      // Both events carry the same wire shape (modulo ``error`` on
      // outbound). Map them into a uniform ``MessagingMessage`` so
      // the caller doesn't case-split.
      const toMessage = (
        evt: MessageReceivedEvent | MessageSentEvent,
        direction: "inbound" | "outbound",
      ): MessagingMessage => ({
        message_id: evt.message_id,
        user_id: evt.user_id,
        our_number: evt.our_number,
        other_number: evt.other_number,
        direction,
        body: evt.body,
        status: evt.status,
        created_at: evt.created_at,
        media_urls: evt.media_urls || [],
        error: "error" in evt ? evt.error : "",
        backend: "",
      });
      const unsubInbound = subscribe(
        "messaging.message_received",
        (e) => {
          // Bus payloads are typed as ``Record<string, unknown>`` at
          // the WS layer; the messaging service publishes a
          // ``MessageReceivedEvent`` shape but TypeScript doesn't
          // know that. Cast through ``unknown`` so the structural
          // mismatch is explicit (and the bug-search target if the
          // server-side wire shape ever drifts).
          const data = (e?.data ?? {}) as unknown as MessageReceivedEvent;
          if (!data.message_id) return;
          onMessage(toMessage(data, "inbound"));
        },
      );
      const unsubOutbound = subscribe(
        "messaging.message_sent",
        (e) => {
          const data = (e?.data ?? {}) as unknown as MessageSentEvent;
          if (!data.message_id) return;
          onMessage(toMessage(data, "outbound"));
        },
      );
      return () => {
        unsubInbound();
        unsubOutbound();
      };
    },
    [subscribe],
  );

  return useMemo(
    () => ({
      threads,
      threadsLoading,
      threadsError,
      reloadThreads,
      loadThread,
      send,
      subscribeMessages,
    }),
    [
      threads,
      threadsLoading,
      threadsError,
      reloadThreads,
      loadThread,
      send,
      subscribeMessages,
    ],
  );
}
