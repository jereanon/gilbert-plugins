/**
 * Side-effect import: register the apple-health plugin's account-page
 * panel.
 *
 * The ``apple-health.account`` panel ID matches the backend's
 * ``AppleHealthPlugin.ui_panels()`` declaration.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { AppleHealthPanel } from "./AppleHealthPanel";

registerPanel("apple-health.account", AppleHealthPanel);

