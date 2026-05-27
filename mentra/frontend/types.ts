/**
 * Wire shapes for the Mentra admin panel — mirrors the
 * ``MentraService.get_ws_handlers()`` registry in
 * ``std-plugins/mentra/mentra_service.py`` plus the
 * ``GlassesCapabilities`` dataclass from
 * ``src/gilbert/interfaces/mentra.py``.
 */

/**
 * One row in the ``mentra_user_mappings`` storage collection. Maps a
 * Mentra cloud ``userId`` (the user's Mentra-side email) to a
 * Gilbert ``user_id`` plus the persona we'll apply when dispatching
 * the session's voice input into the AI service. Without a row,
 * ``MentraService`` refuses to open the session.
 */
export interface MentraUserMapping {
  /** Storage primary key; minted server-side as ``map_<uuid16>``. */
  id: string;
  /** Mentra-side identity (an email per the Mentra cloud's convention). */
  mentra_user_id: string;
  /** Gilbert-side ``user_id`` — what the AI service sees as the caller. */
  gilbert_user_id: string;
  /** Human-readable label for the row in the admin SPA. Defaults to
   *  ``mentra_user_id`` when the operator leaves it blank. */
  display_name: string;
  /** Role set the synthesized ``UserContext`` carries — generally
   *  just ``["user"]``; admin glasses sessions are rare. */
  roles: string[];
  /** ISO 8601 UTC, server-stamped on create. */
  created_at: string;
}

/**
 * Capability bundle the Mentra cloud advertises in the connection
 * ack — translated from the ``Capabilities`` upstream interface
 * into snake_case for Python and camelCase here. Empty object when
 * the session hasn't completed its handshake yet.
 */
export interface MentraGlassesCapabilities {
  modelName?: string;
  hasCamera?: boolean;
  hasDisplay?: boolean;
  hasMicrophone?: boolean;
  hasSpeaker?: boolean;
  hasImu?: boolean;
  hasButton?: boolean;
  hasLight?: boolean;
  hasWifi?: boolean;
}

/** One live glasses session — server-side ephemeral state, not
 *  persisted. The admin SPA polls ``mentra.sessions.list`` to
 *  refresh this table. */
export interface MentraSessionSummary {
  session_id: string;
  mentra_user_id: string;
  gilbert_user_id: string;
  /** ISO 8601 UTC; when the session was admitted by the webhook. */
  connected_at: string;
  capabilities: MentraGlassesCapabilities;
}

// ── RPC envelope shapes ─────────────────────────────────────────────

export interface MappingsListResult {
  mappings: MentraUserMapping[];
}

export interface MappingResult {
  mapping: MentraUserMapping;
}

export interface DeleteResult {
  status: string;
}

export interface SessionsListResult {
  sessions: MentraSessionSummary[];
}

/** Patch body for ``mentra.mappings.create`` — every field required
 *  except ``display_name`` (server defaults it to ``mentra_user_id``). */
export interface CreateMappingInput {
  mentra_user_id: string;
  gilbert_user_id: string;
  display_name?: string;
  roles?: string[];
}

/** Patch body for ``mentra.mappings.update`` — every field beside
 *  ``mapping_id`` is optional. */
export interface UpdateMappingInput {
  mapping_id: string;
  mentra_user_id?: string;
  gilbert_user_id?: string;
  display_name?: string;
  roles?: string[];
}
