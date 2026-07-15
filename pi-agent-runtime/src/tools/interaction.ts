import { randomUUID } from "node:crypto";

import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { AgentToolContext, PendingInput, RuntimeTool } from "../agent/types.js";
import { textToolResult } from "./utils.js";

export function createRequestHumanInputTool(context: AgentToolContext): RuntimeTool {
  return defineTool({
    name: "request_human_input",
    label: "Request human input",
    description: "Pause the agent and ask an operator for information that cannot be established from available context.",
    promptSnippet: "Pause for an operator answer when essential context is unavailable",
    executionMode: "sequential" as const,
    parameters: Type.Object({
      question: Type.String({ minLength: 1, maxLength: 2000 }),
      choices: Type.Optional(
        Type.Array(Type.String({ minLength: 1, maxLength: 500 }), { maxItems: 10 }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const record = context.record;
      if (!record.interaction_policy.allowHumanInput) {
        throw new Error(`Agent ${record.agent_id} does not allow human input.`);
      }
      if (record.pending !== undefined) throw new Error("A human-input request is already pending.");
      const answer = await new Promise<string>((resolveAnswer, rejectAnswer) => {
        const pending: PendingInput = {
          id: randomUUID(),
          question: params.question,
          resolve: resolveAnswer,
          reject: rejectAnswer,
        };
        if (params.choices !== undefined) pending.choices = params.choices;
        record.pending = pending;
        record.status = "waiting_for_input";
        record.stage = "waiting_for_human";
        context.addEvent({
          type: "human_input_requested",
          stage: record.stage,
          tool: "request_human_input",
        });
        const abort = () => rejectAnswer(new Error("Human-input request aborted."));
        signal?.addEventListener("abort", abort, { once: true });
      });
      delete record.pending;
      record.status = "running";
      record.stage = "running";
      context.addEvent({
        type: "human_input_received",
        stage: record.stage,
        tool: "request_human_input",
      });
      return textToolResult(`Operator answer: ${answer}`, { answered: true });
    },
  });
}
