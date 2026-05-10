/**
 * Side-effect import: register the withings plugin's account-page panel.
 */

import { registerPanel } from "@/lib/plugin-panels";
import { WithingsPanel } from "./WithingsPanel";

registerPanel("withings.account", WithingsPanel);

