import { registerPanel } from "@/lib/plugin-panels";
import { PhoneCallsPage } from "./PhoneCallsPage";

// Panel id mirrors phone/plugin.py ``PhonePlugin.ui_routes()`` — the
// SPA mounts <PhoneCallsPage /> at /calls and /calls/:callId. The
// component reads the optional ``callId`` route param via
// ``useParams()`` so one component services both routes.
registerPanel("phone.calls-page", PhoneCallsPage);
