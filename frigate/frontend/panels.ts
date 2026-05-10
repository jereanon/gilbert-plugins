/**
 * Side-effect import: register the frigate plugin's UI panels.
 *
 * Pulled in by ``frontend/src/plugins/index.ts`` (the auto-loader),
 * which is itself imported once from ``main.tsx`` so all registrations
 * land before any page mounts.
 *
 * Panel IDs match the backend's ``FrigatePlugin.ui_panels()`` and
 * ``ui_routes()`` declarations.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { RecentEventsCard } from "./RecentEventsCard";
import { CamerasPage } from "./CamerasPage";

// Dashboard slot — appears at the bottom of the landing page.
registerPanel("frigate.recent_events", RecentEventsCard);

// Full SPA page mounted at /cameras via Plugin.ui_routes().
registerPanel("frigate.cameras_page", CamerasPage);

