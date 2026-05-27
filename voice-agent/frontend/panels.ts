import { registerPanel } from "@/lib/plugin-panels";
import { LiveConversationsPage } from "./LiveConversationsPage";
import { VoiceAgentPage } from "./VoiceAgentPage";

// Panel ids mirror plugin.py ``VoiceAgentPlugin.ui_routes()`` — the
// SPA mounts each component at the route's path.
registerPanel("voice_agent.page", VoiceAgentPage);
registerPanel("voice_agent.live_conversations", LiveConversationsPage);
