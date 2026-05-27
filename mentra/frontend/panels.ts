import { registerPanel } from "@/lib/plugin-panels";

import { MentraPage } from "./MentraPage";

// Panel id mirrors ``MentraPlugin.ui_routes()`` in
// ``std-plugins/mentra/plugin.py`` — the SPA mounts ``<MentraPage />``
// at ``/mentra``. The route declares ``requires_capability="mentra"``
// so the page disappears from the nav when the plugin is toggled
// off in /settings.
registerPanel("mentra.page", MentraPage);
