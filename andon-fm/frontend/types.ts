/**
 * Shared TS types for the Andon FM tuner panel.
 *
 * Mirrors the Python ``andon_fm_service.AndonFmService._stations_payload``
 * shape — every WS RPC and every ``andon_fm.now_playing.changed`` event
 * carries fields described here.
 */

export interface AndonFmBlock {
  name: string;
  description: string;
  started_at: string;
  duration_minutes: number;
  image_url: string;
}

export interface AndonFmTweet {
  id: string;
  content: string;
  posted_at: string;
}

export interface AndonFmStation {
  id: string;
  name: string;
  host: string;
  twitter: string;
  stream_url: string;
  image_url: string;
  stale: boolean;
  block: AndonFmBlock | null;
  listeners: number;
  fetched_at: number;
  tweets: AndonFmTweet[];
}

export interface AndonFmStationsResponse {
  stations: AndonFmStation[];
  defaults: { speakers: string[]; volume: number };
  last_fetch_ok: number;
  last_fetch_error: string;
}

export interface AndonFmPlayResult {
  ok: boolean;
  error?: string;
  station_id?: string;
  speakers?: string[];
}

export interface AndonFmStopResult {
  ok: boolean;
  error?: string;
  speakers?: string[];
}

export interface AndonFmSpeakerOption {
  /** Stable identifier used in play requests (the speaker's display
   *  name, since play_on_speakers resolves names not ids). */
  id: string;
  /** Display label. */
  name: string;
  /** Model string from the backend (Sonos product code or empty). */
  model: string;
  /** Backend that owns this speaker — ``"sonos"`` / ``"local"`` /
   *  ``"browser"`` / etc. — for the small UPPERCASE chip on the
   *  right of each picker row. */
  backend: string;
  /** Speaker group label when grouped, ``""`` otherwise. */
  group_name: string;
}

export interface AndonFmSpeakersResponse {
  speakers: AndonFmSpeakerOption[];
  defaults: { speakers: string[]; volume: number };
}

/** Event payload published by the backend on block transitions. */
export interface AndonFmNowPlayingChangedEvent {
  station_id: string;
  station_name: string;
  station_image_url: string;
  block: AndonFmBlock;
  listeners: number;
  fetched_at: number;
}
