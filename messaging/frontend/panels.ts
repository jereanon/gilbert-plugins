import { registerPanel } from "@/lib/plugin-panels";

import { MessagingPage } from "./MessagingPage";

// Panel id mirrors messaging/plugin.py ``MessagingPlugin.ui_routes()``
// — the SPA mounts <MessagingPage /> at /messages and
// /messages/:otherNumber. The component reads the optional
// ``otherNumber`` route param via ``useParams()`` so one component
// services both routes (same pattern phone uses).
registerPanel("messaging.page", MessagingPage);
