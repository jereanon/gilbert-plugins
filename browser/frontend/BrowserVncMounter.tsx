/**
 * BrowserVncMounter — invisible always-mounted component that hosts
 * the BrowserVncSessionDialog in response to agent-action triggers.
 *
 * The browser plugin's ``browser.vnc`` agent-action handler can't
 * directly mount a React component (it's a plain function called
 * from a button click handler), so it dispatches a CustomEvent
 * (``gilbert.browser.openVnc``) carrying the target URL. This
 * component listens for that event globally and opens the modal —
 * mounted into the AppShell's ``app.background`` slot so it's alive
 * everywhere in the SPA.
 */

import { useEffect, useState } from "react";
import { BrowserVncSessionDialog } from "./BrowserVncSessionDialog";

export const BROWSER_VNC_OPEN_EVENT = "gilbert.browser.openVnc";

interface OpenVncDetail {
  url: string;
}

export function BrowserVncMounter() {
  const [targetUrl, setTargetUrl] = useState<string | null>(null);

  useEffect(() => {
    const handler = (ev: Event) => {
      const detail = (ev as CustomEvent<OpenVncDetail>).detail;
      const url = detail?.url?.trim();
      if (!url) return;
      // Auto-add scheme if the agent passed a bare hostname.
      const withScheme = /^https?:\/\//i.test(url) ? url : `https://${url}`;
      setTargetUrl(withScheme);
    };
    window.addEventListener(BROWSER_VNC_OPEN_EVENT, handler);
    return () => window.removeEventListener(BROWSER_VNC_OPEN_EVENT, handler);
  }, []);

  if (!targetUrl) return null;
  return (
    <BrowserVncSessionDialog
      targetUrl={targetUrl}
      onClose={() => setTargetUrl(null)}
    />
  );
}
