/**
 * Per-user account panel for Apple Health (HealthKit) push.
 *
 * Mounted under the ``account.extensions`` slot. Headlines:
 *
 * - Failure-mode disclosure (iOS Background App Refresh + lock state)
 *   ABOVE the install button so users know what they're signing up
 *   for.
 * - "Install our Shortcut" button + SHA-256 hash for supply-chain
 *   verification (paranoid users can compare).
 * - Webhook URL display (shown once at rotation; SHA-256 hash at
 *   rest, never returned to the client).
 * - Last-delivery indicator so a silently-broken Shortcut is visible.
 * - Manual fallback instructions for users who can't / won't use
 *   the prebuilt Shortcut.
 */

import { useEffect, useState } from "react";

interface RotateResult {
  status: string;
  raw_token?: string;
  webhook_url?: string;
  message?: string;
}

interface LinkRow {
  backend_name: string;
  enabled: boolean;
  last_delivery_at: string;
  webhook_token_last4: string;
}

// SHA-256 hash of the prebuilt Shortcut bundle. Update on every
// release of the apple-health plugin's Shortcut artifact.
const _SHORTCUT_HASH_DISPLAY =
  "(SHA-256 hash placeholder — populated on release)";

const _SHORTCUT_INSTALL_URL =
  "https://www.icloud.com/shortcuts/PLACEHOLDER";

function _formatRelative(iso: string): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "never";
  const ago = Math.max(0, Date.now() - t);
  const hours = Math.floor(ago / (60 * 60 * 1000));
  if (hours < 1) return "less than an hour ago";
  if (hours < 36) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.floor(hours / 24)} day${Math.floor(hours / 24) === 1 ? "" : "s"} ago`;
}

export function AppleHealthPanel(): JSX.Element {
  const [webhookUrl, setWebhookUrl] = useState<string>("");
  const [link, setLink] = useState<LinkRow | null>(null);
  const [rotating, setRotating] = useState<boolean>(false);

  useEffect(() => {
    fetch("/api/health/me/links", { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        const items = (data?.items ?? []) as LinkRow[];
        const apple = items.find(
          (i) => i.backend_name === "apple-health",
        );
        setLink(apple ?? null);
      });
  }, []);

  const handleRotate = async () => {
    setRotating(true);
    try {
      const resp = await fetch("/api/health/me/rotate-token/apple-health", {
        method: "POST",
        credentials: "include",
      });
      const result: RotateResult = await resp.json();
      if (result.status === "ok" && result.webhook_url) {
        setWebhookUrl(result.webhook_url);
      }
    } finally {
      setRotating(false);
    }
  };

  return (
    <div className="space-y-4 p-4 border rounded">
      <div>
        <h3 className="text-lg font-medium">Apple Health</h3>
        <p className="text-sm text-muted-foreground">
          Push HealthKit data from your iPhone via an iOS Shortcut.
        </p>
      </div>

      {/* Failure-mode disclosure — rendered ABOVE the install button
          so users know what they're signing up for. */}
      <div className="p-3 bg-yellow-50 border border-yellow-200 rounded text-sm">
        <strong>Heads up:</strong> iOS Shortcut Automations only run
        while your phone is unlocked at the scheduled time, and iOS
        sometimes revokes Background App Refresh on Shortcuts after
        major iOS updates. If your daily summary stops updating,
        check that the Automation is still enabled in Settings →
        Shortcuts → Automation. The "last delivery" field below
        shows when we last received data — if it says "more than 36
        hours ago," that's the smoking gun.
      </div>

      <div className="space-y-2">
        <a
          href={_SHORTCUT_INSTALL_URL}
          className="inline-block px-3 py-2 bg-primary text-primary-foreground rounded"
          target="_blank"
          rel="noreferrer"
        >
          Install our Shortcut
        </a>
        <p className="text-xs text-muted-foreground">
          Bundle SHA-256: <code>{_SHORTCUT_HASH_DISPLAY}</code>
          <br />
          Compare this hash before installing if your threat model
          includes "Gilbert plugin repo got hijacked." The Shortcut
          bundle on iCloud is signed; the hash above is what we
          published — if iCloud serves a different one, don't run
          the Shortcut.
        </p>
      </div>

      <button
        onClick={handleRotate}
        disabled={rotating}
        className="px-3 py-2 bg-primary text-primary-foreground rounded"
      >
        {rotating ? "Generating..." : "Generate / rotate webhook URL"}
      </button>

      {webhookUrl && (
        <div className="p-3 bg-muted rounded text-xs space-y-2">
          <div className="font-mono break-all">
            <strong>Webhook URL (shown once):</strong>
            <br />
            {webhookUrl}
          </div>
          <p>
            Copy this URL into the Shortcut's "URL" field now. We
            never show it again — only its SHA-256 hash is stored on
            this Gilbert instance.
          </p>
        </div>
      )}

      {link && (
        <div className="text-sm space-y-1">
          <div>
            <strong>Status:</strong>{" "}
            {link.enabled ? "enabled" : "disabled"}
            {link.webhook_token_last4 && (
              <>
                {" "}— token ends in <code>{link.webhook_token_last4}</code>
              </>
            )}
          </div>
          <div>
            <strong>Last delivery:</strong>{" "}
            {_formatRelative(link.last_delivery_at)}
          </div>
        </div>
      )}

      <details className="text-xs">
        <summary className="cursor-pointer">
          Manual fallback (without our Shortcut)
        </summary>
        <ol className="list-decimal pl-5 mt-2 space-y-1">
          <li>
            iOS Shortcuts → "+" → Add action → "Find Health Samples".
            Configure to find samples for whatever data you want to
            share.
          </li>
          <li>
            Add "Get URL Contents" → URL = (the per-user URL above) →
            Method POST → JSON body in the documented shape.
          </li>
          <li>
            Schedule the Shortcut as an Automation (e.g. daily at
            midnight).
          </li>
        </ol>
      </details>
    </div>
  );
}

