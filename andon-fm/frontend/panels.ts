import { registerPanel } from "@/lib/plugin-panels";
import { AndonFmPage } from "./AndonFmPage";

// Panel id mirrors plugin.py ``AndonFmPlugin.ui_routes()`` — the SPA
// mounts <AndonFmPage /> at the route's path (/media/andon-fm).
registerPanel("andon_fm.page", AndonFmPage);
