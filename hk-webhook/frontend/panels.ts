/**
 * Side-effect import: register the hk-webhook plugin's account-page
 * panel.
 *
 * Pulled in by ``frontend/src/plugins/index.ts`` (the auto-loader),
 * which is itself imported once from ``main.tsx`` so all
 * registrations land before any page mounts.
 *
 * The ``hk-webhook.account`` panel ID matches the backend's
 * ``HKWebhookPlugin.ui_panels()`` declaration.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { HKWebhookPanel } from "./HKWebhookPanel";

registerPanel("hk-webhook.account", HKWebhookPanel);

