import { defineTool } from "@earendil-works/pi-coding-agent";

import { validateAgentOutput } from "../agent/registry.js";
import type { AgentDefinition, AgentToolContext, RuntimeTool } from "../agent/types.js";
import { textToolResult } from "./utils.js";

export function createCompletionTool(
  definition: AgentDefinition,
  context: AgentToolContext,
): RuntimeTool {
  const result = definition.result;
  return defineTool({
    name: result.toolName,
    label: result.label,
    description: result.description,
    promptSnippet: result.promptSnippet,
    promptGuidelines: [`Always finish by calling ${result.toolName} exactly once.`],
    executionMode: "sequential" as const,
    parameters: result.schema,
    async execute(_toolCallId, params) {
      const record = context.record;
      if (record.result !== undefined || record.status === "completed") {
        throw new Error(`Agent ${record.agent_id} already submitted its result.`);
      }
      record.status = "validating_result";
      record.stage = "validating_result";
      const output = validateAgentOutput(definition, params);
      const size = Buffer.byteLength(JSON.stringify(output), "utf8");
      if (size > record.execution_limits.maxResultBytes) {
        throw new Error(
          `Agent result is ${size} bytes; limit is ${record.execution_limits.maxResultBytes}.`,
        );
      }
      await context.validateOutput(output);
      record.result = output;
      record.status = "completed";
      record.stage = "completed";
      context.addEvent({
        type: result.eventType,
        stage: record.stage,
        tool: result.toolName,
      });
      return {
        ...textToolResult(result.successMessage, { result_bytes: size }),
        terminate: true,
      };
    },
  });
}
