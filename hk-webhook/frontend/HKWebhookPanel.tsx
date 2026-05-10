/**
 * Per-user account panel for the generic health webhook backend.
 *
 * Mounts under the ``account.extensions`` slot. Shows the user's
 * webhook URL after token rotation, plus copy-paste curl + Python
 * snippets so non-iOS users have a clean documented path.
 */

import { useState } from "react";
import { useWebSocket } from "@/hooks/useWebSocket";

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
  last_sync_error: string;
  webhook_token_last4: string;
  supports_webhook: boolean;
}

export function HKWebhookPanel(): JSX.Element {
  const { rpc } = useWebSocket();
  const [webhookUrl, setWebhookUrl] = useState<string>("");
  const [rotating, setRotating] = useState<boolean>(false);

  const handleRotate = async () => {
    setRotating(true);
    try {
      const resp = await fetch("/api/health/me/rotate-token/hk-webhook", {
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
    <div className="space-y-3 p-4 border rounded">
      <h3 className="text-lg font-medium">Generic Health Webhook</h3>
      <p className="text-sm text-muted-foreground">
        Push metrics from any source — iOS Shortcut, Home Assistant
        automation, Garmin Connect IQ widget, custom Python — by
        POSTing JSON to your per-user webhook URL.
      </p>

      <button
        onClick={handleRotate}
        disabled={rotating}
        className="px-3 py-2 bg-primary text-primary-foreground rounded"
      >
        {rotating ? "Generating..." : "Generate / rotate URL"}
      </button>

      {webhookUrl && (
        <div className="p-3 bg-muted rounded space-y-2">
          <div className="text-xs font-mono break-all">
            <strong>Webhook URL (shown once):</strong>
            <br />
            {webhookUrl}
          </div>
          <p className="text-xs text-muted-foreground">
            Copy this URL now — we'll never show it again. The URL
            itself is the auth credential. Rotating issues a new URL
            and revokes the old one.
          </p>
          <details className="text-xs">
            <summary className="cursor-pointer">curl example</summary>
            <pre className="mt-1 p-2 bg-background overflow-auto">{`curl -X POST '${webhookUrl}' \\
  -H 'Content-Type: application/json' \\
  -d '{
    "metrics": [
      {
        "type": "steps",
        "value": 8431,
        "unit": "count",
        "recorded_at": "2026-05-09T07:00:00+00:00"
      }
    ]
  }'`}</pre>
          </details>
          <details className="text-xs">
            <summary className="cursor-pointer">Python snippet</summary>
            <pre className="mt-1 p-2 bg-background overflow-auto">{`import httpx

httpx.post(
    "${webhookUrl}",
    json={
        "metrics": [
            {
                "type": "weight",
                "value": 80.5,
                "unit": "kg",
                "recorded_at": "2026-05-09T07:00:00+00:00",
            }
        ]
    },
)`}</pre>
          </details>
        </div>
      )}
    </div>
  );
}

