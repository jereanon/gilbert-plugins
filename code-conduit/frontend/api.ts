/**
 * Plugin-local API hook for the Code Conduit SPA panel.
 *
 * Two RPCs (mirror the service's ``get_ws_handlers``):
 * - ``code.events.list`` — pulls the ring-buffer feed.
 * - ``code.send`` — fires an outbound relay (same path as the
 *   ``code_send`` AI tool / ``/code send`` slash command).
 *
 * Polls events on a fixed interval. We don't subscribe to live
 * bus events here in v1 — keeps the implementation tiny and
 * matches how the Mentra debug webview surfaces its event ring
 * buffer (poll every few seconds, render whatever's there).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type {
  CodingEvent,
  CodingEventKind,
  EventsListResult,
  SendResult,
} from "./types";

const POLL_INTERVAL_MS = 5_000;

export interface UseCodeConduitApi {
  events: CodingEvent[];
  enabled: boolean;
  eventsLoading: boolean;
  eventsError: string | null;
  reloadEvents: () => Promise<void>;

  sendMessage: (input: SendMessageInput) => Promise<SendResult>;
  sendInFlight: boolean;
}

export interface SendMessageInput {
  message: string;
  project?: string;
  new_session?: boolean;
}

export interface UseCodeConduitOptions {
  /** Filter the feed to a specific severity bucket. */
  kind?: CodingEventKind | "";
  /** Cap the number of events returned. Default 100. */
  limit?: number;
}

export function useCodeConduitApi(
  options: UseCodeConduitOptions = {},
): UseCodeConduitApi {
  const { connected, rpc } = useWebSocket();
  const { kind = "", limit = 100 } = options;

  const [events, setEvents] = useState<CodingEvent[]>([]);
  const [enabled, setEnabled] = useState<boolean>(true);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [sendInFlight, setSendInFlight] = useState(false);

  const reloadEvents = useCallback(async (): Promise<void> => {
    if (!connected) return;
    setEventsLoading(true);
    setEventsError(null);
    try {
      const res = await rpc<EventsListResult>({
        type: "code.events.list",
        payload: { kind, limit },
      });
      setEvents(res.events || []);
      setEnabled(res.enabled !== false);
    } catch (err) {
      setEventsError(err instanceof Error ? err.message : String(err));
    } finally {
      setEventsLoading(false);
    }
  }, [connected, rpc, kind, limit]);

  // Initial load + recurring poll. We don't tail a live stream —
  // the rolling 5s refresh is plenty for a notification panel and
  // avoids the complexity of per-page WS subscriptions.
  useEffect(() => {
    if (!connected) return;
    void reloadEvents();
    const handle = window.setInterval(() => {
      void reloadEvents();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, [connected, reloadEvents]);

  const sendMessage = useCallback(
    async (input: SendMessageInput): Promise<SendResult> => {
      setSendInFlight(true);
      try {
        const res = await rpc<SendResult>({
          type: "code.send",
          payload: {
            message: input.message,
            project: input.project ?? "",
            new_session: input.new_session ?? false,
          },
        });
        // Refresh the feed so the inbound echo (the agent's
        // response, when it lands) shows up alongside other
        // recent events without waiting for the next poll tick.
        void reloadEvents();
        return res;
      } finally {
        setSendInFlight(false);
      }
    },
    [rpc, reloadEvents],
  );

  return useMemo(
    () => ({
      events,
      enabled,
      eventsLoading,
      eventsError,
      reloadEvents,
      sendMessage,
      sendInFlight,
    }),
    [
      events,
      enabled,
      eventsLoading,
      eventsError,
      reloadEvents,
      sendMessage,
      sendInFlight,
    ],
  );
}
