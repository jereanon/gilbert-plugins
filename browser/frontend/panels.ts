/**
 * Side-effect import: register the browser plugin's UI panels.
 *
 * This file is the only place the SPA needs to know about the
 * browser plugin. It's pulled in by ``frontend/src/plugins/index.ts``,
 * which is itself imported once from ``main.tsx`` so all plugin
 * panels register at startup before any page mounts a
 * ``<PluginPanelSlot>``.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { BrowserCredentialsPanel } from "./BrowserCredentialsPanel";

// Panel ID matches the backend's ``BrowserPlugin.ui_panels()``.
registerPanel("browser.credentials", BrowserCredentialsPanel);
