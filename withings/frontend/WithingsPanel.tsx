/**
 * Per-user account panel for Withings (OAuth pull).
 *
 * Mounted under the ``account.extensions`` slot. Surfaces:
 * - The configured OAuth callback URL alongside the Connect button
 *   so the admin can copy it into the Withings developer dashboard.
 * - Connect / Disconnect / Sync-now buttons.
 * - Disable Connect when ``gilbert.public_base_url`` is unset, with
 *   an explainer pointing the admin at /system.
 * - The "Tokens stored unencrypted on this Gilbert instance until v2"
 *   privacy disclosure (per spec §6.4 v1 framing).
 */

import { useEffect, useState } from "react";

interface LinkRow {
  backend_name: string;
  enabled: boolean;
  last_sync_at: string;
  last_sync_error: string;
}

interface ConnectResult {
  status: string;
  message?: string;
  open_url?: string;
}

export function WithingsPanel(): JSX.Element {
  const [link, setLink] = useState<LinkRow | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  useEffect(() => {
    fetch("/api/health/me/links", { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        const rows = (data?.items ?? []) as LinkRow[];
        setLink(rows.find((r) => r.backend_name === "withings") ?? null);
      });
  }, []);

  const handleConnect = async () => {
    setBusy(true);
    try {
      const resp = await fetch(
        "/api/health/me/connect/withings",
        { method: "POST", credentials: "include" },
      );
      const result: ConnectResult = await resp.json();
      if (result.status === "ok" && result.open_url) {
        window.location.href = result.open_url;
        return;
      }
      alert(result.message || "Could not start Withings connect flow.");
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setBusy(true);
    try {
      await fetch(
        "/api/health/me/disconnect/withings",
        { method: "POST", credentials: "include" },
      );
      setLink(null);
    } finally {
      setBusy(false);
    }
  };

  const isConnected = !!link?.enabled;

  return (
    <div className="space-y-3 p-4 border rounded">
      <div>
        <h3 className="text-lg font-medium">Withings</h3>
        <p className="text-sm text-muted-foreground">
          Connect your Withings account via OAuth. We sync sleep,
          weight, blood pressure, and heart rate every 6 hours.
        </p>
      </div>

      <div className="text-xs p-2 bg-yellow-50 border border-yellow-200 rounded">
        <strong>Privacy note:</strong> OAuth access + refresh tokens
        are stored unencrypted on this Gilbert instance until v2.
        For deployments beyond a single trusted host, hold off on
        connecting until token-encryption-at-rest ships.
      </div>

      {isConnected ? (
        <div className="space-y-2">
          <div className="text-sm">
            <strong>Connected.</strong> Last sync:{" "}
            {link?.last_sync_at || "never"}
            {link?.last_sync_error && (
              <span className="text-destructive ml-2">
                — {link.last_sync_error}
              </span>
            )}
          </div>
          <button
            onClick={handleDisconnect}
            disabled={busy}
            className="px-3 py-2 bg-destructive text-destructive-foreground rounded"
          >
            Disconnect
          </button>
          <p className="text-xs text-muted-foreground">
            Disconnecting revokes Gilbert's OAuth grant with Withings
            and removes the local link row. Historical metrics stay
            in place — use /health delete-all to wipe those too.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          <button
            onClick={handleConnect}
            disabled={busy}
            className="px-3 py-2 bg-primary text-primary-foreground rounded"
          >
            {busy ? "Starting..." : "Connect Withings"}
          </button>
          <p className="text-xs text-muted-foreground">
            Admin precondition: ``gilbert.public_base_url`` must be
            set in /system before users can connect, AND the
            callback URL{" "}
            <code>
              &lt;public_base_url&gt;/api/health/me/oauth/withings/callback
            </code>{" "}
            must be registered in the Withings developer dashboard.
          </p>
        </div>
      )}
    </div>
  );
}

