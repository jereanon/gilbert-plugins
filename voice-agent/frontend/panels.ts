import { registerPanel } from "@/lib/plugin-panels";
import { VoiceAgentPage } from "./VoiceAgentPage";

// Panel id mirrors plugin.py ``VoiceAgentPlugin.ui_routes()`` — the
// SPA mounts <VoiceAgentPage /> at the route's path (/voice).
registerPanel("voice_agent.page", VoiceAgentPage);
