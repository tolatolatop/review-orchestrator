import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { AgentToolContext, JsonObject, RuntimeTool } from "../agent/types.js";
import { textToolResult } from "./utils.js";

export interface ReviewActionToolOptions {
  baseUrl?: string;
  token?: string;
}

function errorMessage(payload: unknown, status: number): string {
  if (payload !== null && typeof payload === "object") {
    const detail = (payload as JsonObject).detail;
    if (typeof detail === "string") return detail;
    if (detail !== null && typeof detail === "object") {
      const message = (detail as JsonObject).message;
      const code = (detail as JsonObject).code;
      if (typeof message === "string") {
        return typeof code === "string" ? `${code}: ${message}` : message;
      }
    }
  }
  return `Orchestrator rejected the review action with HTTP ${status}.`;
}

export function createReviewActionTool(
  context: AgentToolContext,
  options: ReviewActionToolOptions,
): RuntimeTool {
  return defineTool({
    name: "request_review_action",
    label: "Request review action",
    description: [
      "Request an orchestrator-owned review action for this PR's current revision.",
      "Use retry only for the latest failed review and rerun only for the latest completed or cancelled review.",
      "Call this Tool only when the user explicitly asks to retry or run the review again.",
    ].join(" "),
    promptSnippet: "Request a validated retry or rerun through the Review Orchestrator",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("retry"), Type.Literal("rerun")], {
        description: "retry a failed review, or rerun a completed/cancelled review",
      }),
    }),
    async execute(_toolCallId, params, signal) {
      if (!options.baseUrl || !options.token) {
        throw new Error("Review Orchestrator Tool callback is not configured.");
      }
      const orchestration = context.record.agent_input.orchestration_context;
      if (orchestration === null || typeof orchestration !== "object") {
        throw new Error("This Agent session is missing its orchestration context.");
      }
      const taskId = (orchestration as JsonObject).agent_task_id;
      if (typeof taskId !== "string" || taskId.length === 0) {
        throw new Error("This Agent session is missing its AgentTask identity.");
      }

      const response = await fetch(
        `${options.baseUrl.replace(/\/+$/, "")}/api/v1/internal/agent-tools/review-action`,
        {
          method: "POST",
          headers: {
            authorization: `Bearer ${options.token}`,
            "content-type": "application/json",
          },
          body: JSON.stringify({
            agent_task_id: taskId,
            agent_session_id: context.record.id,
            action: params.action,
          }),
          ...(signal === undefined ? {} : { signal }),
        },
      );
      const payload: unknown = await response.json().catch(() => undefined);
      if (!response.ok) throw new Error(errorMessage(payload, response.status));
      if (payload === null || typeof payload !== "object") {
        throw new Error("Review Orchestrator returned an invalid Tool response.");
      }
      const result = payload as JsonObject;
      context.addEvent({
        type: "review_action_requested",
        stage: `review_action:${params.action}`,
        tool: "request_review_action",
      });
      return textToolResult(
        `Review ${params.action} accepted: attempt ${String(result.attempt)} is waiting for its placeholder comment.`,
        {
          action: params.action,
          source_review_run_id: result.source_review_run_id,
          review_request_event_id: result.review_request_event_id,
          review_run_id: result.review_run_id,
          attempt: result.attempt,
          status: result.status,
          deduplicated: result.deduplicated,
        },
      );
    },
  });
}
