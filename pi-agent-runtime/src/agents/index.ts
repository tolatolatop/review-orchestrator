import { AgentRegistry } from "../agent/registry.js";
import { changeSummaryAgent } from "./change-summary.js";
import { codeReviewAgent } from "./code-review.js";
import { prAssistantAgent } from "./pr-assistant.js";

export function createBuiltinAgentRegistry(): AgentRegistry {
  const registry = new AgentRegistry();
  registry.register(codeReviewAgent);
  registry.register(prAssistantAgent);
  registry.register(changeSummaryAgent);
  return registry;
}

export { changeSummaryAgent, codeReviewAgent, prAssistantAgent };
