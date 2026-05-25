/**
 * Wire shapes for the messaging plugin — mirrors
 * ``src/gilbert/interfaces/messaging.py`` (the `Message` /
 * `ThreadSummary` dataclasses) and the WS RPC envelopes the
 * service exposes (`messaging.threads.list`, `messaging.thread.get`,
 * `messaging.send`).
 */

export interface MessagingMessage {
  message_id: string;
  user_id: string;
  our_number: string;
  other_number: string;
  /** "inbound" | "outbound" */
  direction: string;
  body: string;
  /** "queued" | "sent" | "delivered" | "failed" | "received" */
  status: string;
  /** ISO 8601 UTC. */
  created_at: string;
  media_urls: string[];
  error: string;
  backend: string;
}

export interface MessagingThreadSummary {
  user_id: string;
  our_number: string;
  other_number: string;
  last_message_at: string;
  last_message_preview: string;
  /** "inbound" | "outbound" */
  last_message_direction: string;
  unread_count: number;
  message_count: number;
}

/** Bus event payload — published when an inbound message arrives. */
export interface MessageReceivedEvent {
  message_id: string;
  user_id: string;
  our_number: string;
  other_number: string;
  body: string;
  status: string;
  created_at: string;
  media_urls: string[];
}

/** Bus event payload — published when an outbound message is dispatched. */
export interface MessageSentEvent {
  message_id: string;
  user_id: string;
  our_number: string;
  other_number: string;
  body: string;
  status: string;
  created_at: string;
  media_urls: string[];
  error: string;
}

/** Bus event payload — published on either direction, for thread-list
 *  refresh signaling without re-fetching the full message list. */
export interface ThreadUpdatedEvent {
  user_id: string;
  our_number: string;
  other_number: string;
  last_message_at: string;
  last_message_direction: string;
  last_message_preview: string;
}
