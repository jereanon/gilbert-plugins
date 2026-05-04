/**
 * useBrowserApi — plugin-local WS RPC bindings.
 *
 * Lives inside the browser plugin so core's ``useWsApi`` doesn't need
 * to know about browser-specific RPCs. Components inside the plugin
 * call ``const api = useBrowserApi()`` and get typed bindings for the
 * ``browser.credentials.*`` and ``browser.vnc.*`` frame types.
 */

import { useMemo } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  BrowserCredential,
  BrowserCredentialDraft,
  BrowserVncSession,
} from "./types";

export function useBrowserApi() {
  const { rpc } = useWebSocket();

  return useMemo(
    () => ({
      listCredentials: () =>
        rpc<{ credentials: BrowserCredential[] }>({
          type: "browser.credentials.list",
        }),

      saveCredential: (draft: BrowserCredentialDraft) =>
        rpc<{ ok: boolean; id: string }>({
          type: "browser.credentials.save",
          ...draft,
        }),

      deleteCredential: (credential_id: string) =>
        rpc<{ ok: boolean }>({
          type: "browser.credentials.delete",
          credential_id,
        }),

      startVncSession: (params: {
        credential_id?: string;
        target_url?: string;
      }) =>
        rpc<{ ok: boolean; session: BrowserVncSession }>({
          type: "browser.vnc.start",
          ...params,
        }),

      stopVncSession: (session_id: string) =>
        rpc<{ ok: boolean }>({
          type: "browser.vnc.stop",
          session_id,
        }),

      listVncSessions: () =>
        rpc<{ sessions: BrowserVncSession[] }>({
          type: "browser.vnc.list",
        }),
    }),
    [rpc],
  );
}
