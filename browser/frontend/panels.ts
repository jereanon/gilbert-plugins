/**
 * Side-effect import: register the browser plugin's UI panels +
 * agent-action handlers.
 *
 * This file is the only place the SPA needs to know about the
 * browser plugin. It's pulled in by ``frontend/src/plugins/index.ts``,
 * which is itself imported once from ``main.tsx`` so all
 * registrations land before any page mounts.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { registerAgentActionHandler } from "@/lib/agent-actions";
import { BrowserCredentialsPanel } from "./BrowserCredentialsPanel";
import {
  BrowserVncMounter,
  BROWSER_VNC_OPEN_EVENT,
} from "./BrowserVncMounter";

// Panel IDs match the backend's ``BrowserPlugin.ui_panels()``.
registerPanel("browser.credentials", BrowserCredentialsPanel);
// Always-mounted host for the VNC modal — backend declares this
// panel against the ``app.background`` slot in BrowserPlugin.ui_panels().
registerPanel("browser.vnc-mounter", BrowserVncMounter);

// ``browser.vnc`` agent action: payload.url → open a VNC live-login
// session pointed at that URL. The mounter listens for the custom
// event and pops the modal.
registerAgentActionHandler("browser.vnc", (payload) => {
  const url = String(payload?.url || "").trim();
  if (!url) return;
  window.dispatchEvent(
    new CustomEvent(BROWSER_VNC_OPEN_EVENT, { detail: { url } }),
  );
});
