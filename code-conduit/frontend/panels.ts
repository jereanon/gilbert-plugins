/**
 * Side-effect module that registers the /coding page component
 * against the panel id declared in
 * ``CodeConduitPlugin.ui_routes()``. Core's Vite glob picks this
 * up automatically — no edits to core SPA needed.
 */

import { registerPanel } from "@/lib/plugin-panels";

import { CodingPage } from "./CodingPage";

registerPanel("code_conduit.page", CodingPage);
