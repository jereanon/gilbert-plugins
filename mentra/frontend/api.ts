/**
 * Plugin-local API hook for the Mentra admin panel.
 *
 * Wraps the four mapping CRUD RPCs (`mentra.mappings.{list,create,
 * update,delete}`) plus the read-only `mentra.sessions.list`. Lives
 * in the plugin's own frontend directory so core's ``useWsApi``
 * stays generic — per the rule §9 extension policy. Mirrors the
 * shape messaging's ``useMessaging`` exports.
 *
 * Note: the WS frame uses ``mapping_id`` (not ``id``) for the
 * entity-id field on update/delete frames. The envelope-level
 * ``id`` is reserved by ``useWebSocket.rpc()`` for correlation —
 * see the comment in ``mentra_service._ws_mappings_update``.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useWebSocket } from "@/hooks/useWebSocket";

import type {
  CreateMappingInput,
  DeleteResult,
  MappingResult,
  MappingsListResult,
  MentraSessionSummary,
  MentraUserMapping,
  SessionsListResult,
  UpdateMappingInput,
} from "./types";

/**
 * Public surface returned by ``useMentraApi``. Mapping mutations
 * (create/update/delete) return the freshly-persisted row (or the
 * status envelope for delete) so callers can stitch the result back
 * into local state without a follow-up list call. ``reloadMappings``
 * + ``reloadSessions`` are explicit so the caller controls when the
 * round-trip happens (e.g. on a polling timer for sessions).
 */
export interface UseMentraApi {
  mappings: MentraUserMapping[];
  mappingsLoading: boolean;
  mappingsError: string | null;
  reloadMappings: () => Promise<void>;
  createMapping: (input: CreateMappingInput) => Promise<MentraUserMapping>;
  updateMapping: (input: UpdateMappingInput) => Promise<MentraUserMapping>;
  deleteMapping: (mappingId: string) => Promise<void>;

  sessions: MentraSessionSummary[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  reloadSessions: () => Promise<void>;
}

export function useMentraApi(): UseMentraApi {
  const { connected, rpc } = useWebSocket();

  const [mappings, setMappings] = useState<MentraUserMapping[]>([]);
  const [mappingsLoading, setMappingsLoading] = useState(false);
  const [mappingsError, setMappingsError] = useState<string | null>(null);

  const [sessions, setSessions] = useState<MentraSessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);

  const reloadMappings = useCallback(async (): Promise<void> => {
    if (!connected) return;
    setMappingsLoading(true);
    setMappingsError(null);
    try {
      const res = await rpc<MappingsListResult>({
        type: "mentra.mappings.list",
      });
      setMappings(res.mappings || []);
    } catch (err) {
      setMappingsError(err instanceof Error ? err.message : String(err));
    } finally {
      setMappingsLoading(false);
    }
  }, [connected, rpc]);

  const reloadSessions = useCallback(async (): Promise<void> => {
    if (!connected) return;
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const res = await rpc<SessionsListResult>({
        type: "mentra.sessions.list",
      });
      setSessions(res.sessions || []);
    } catch (err) {
      setSessionsError(err instanceof Error ? err.message : String(err));
    } finally {
      setSessionsLoading(false);
    }
  }, [connected, rpc]);

  // Auto-load both tables on first connect.
  useEffect(() => {
    if (!connected) return;
    void reloadMappings();
    void reloadSessions();
  }, [connected, reloadMappings, reloadSessions]);

  const createMapping = useCallback(
    async (input: CreateMappingInput): Promise<MentraUserMapping> => {
      const res = await rpc<MappingResult>({
        type: "mentra.mappings.create",
        mentra_user_id: input.mentra_user_id,
        gilbert_user_id: input.gilbert_user_id,
        display_name: input.display_name ?? "",
        roles: input.roles ?? ["user"],
      });
      // Append locally so the table reflects the new row before
      // the next reload — keeps the form-submit UX snappy.
      setMappings((prev) => [...prev, res.mapping]);
      return res.mapping;
    },
    [rpc],
  );

  const updateMapping = useCallback(
    async (input: UpdateMappingInput): Promise<MentraUserMapping> => {
      const frame: Record<string, unknown> = {
        type: "mentra.mappings.update",
        mapping_id: input.mapping_id,
      };
      if (input.mentra_user_id !== undefined)
        frame.mentra_user_id = input.mentra_user_id;
      if (input.gilbert_user_id !== undefined)
        frame.gilbert_user_id = input.gilbert_user_id;
      if (input.display_name !== undefined)
        frame.display_name = input.display_name;
      if (input.roles !== undefined) frame.roles = input.roles;
      const res = await rpc<MappingResult>(frame);
      setMappings((prev) =>
        prev.map((m) => (m.id === res.mapping.id ? res.mapping : m)),
      );
      return res.mapping;
    },
    [rpc],
  );

  const deleteMapping = useCallback(
    async (mappingId: string): Promise<void> => {
      await rpc<DeleteResult>({
        type: "mentra.mappings.delete",
        mapping_id: mappingId,
      });
      setMappings((prev) => prev.filter((m) => m.id !== mappingId));
    },
    [rpc],
  );

  return useMemo(
    () => ({
      mappings,
      mappingsLoading,
      mappingsError,
      reloadMappings,
      createMapping,
      updateMapping,
      deleteMapping,
      sessions,
      sessionsLoading,
      sessionsError,
      reloadSessions,
    }),
    [
      mappings,
      mappingsLoading,
      mappingsError,
      reloadMappings,
      createMapping,
      updateMapping,
      deleteMapping,
      sessions,
      sessionsLoading,
      sessionsError,
      reloadSessions,
    ],
  );
}
