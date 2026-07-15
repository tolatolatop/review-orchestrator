import { Type } from "typebox";

import type { AgentDefinition, JsonObject } from "../agent/types.js";
import { asRepositoryContext, permissiveModelPolicy, repositoryContextSchema, repositoryTools } from "./common.js";

const inputSchema = Type.Object({
  repository_context: repositoryContextSchema,
  audience: Type.Optional(Type.Union([
    Type.Literal("developer"),
    Type.Literal("reviewer"),
    Type.Literal("release-manager"),
  ])),
  focus: Type.Optional(Type.String({ minLength: 1, maxLength: 1000 })),
});

export const changeSummaryAgent: AgentDefinition = {
  id: "change-summary",
  version: "1.0.0",
  description: "Summarize a commit range for a selected audience without performing a defect review.",
  inputSchema,
  result: {
    toolName: "submit_change_summary",
    label: "Submit change summary",
    description: "Submit a structured explanation of the change and end the agent run.",
    promptSnippet: "Submit the final structured change summary",
    successMessage: "Structured change summary accepted.",
    eventType: "change_summary_submitted",
    schema: Type.Object({
      summary: Type.String({ minLength: 1, maxLength: 8000 }),
      changes: Type.Array(Type.Object({
        area: Type.String({ minLength: 1, maxLength: 200 }),
        description: Type.String({ minLength: 1, maxLength: 2000 }),
      }), { minItems: 1, maxItems: 50 }),
      risks: Type.Array(Type.String({ minLength: 1, maxLength: 1000 }), { maxItems: 20 }),
    }),
  },
  defaultSkills: { primary: "change-summary", supporting: [] },
  allowSkillOverride: true,
  tools: [...repositoryTools],
  profiles: {
    default: {
      description: "Balanced technical summary.",
    },
    concise: {
      description: "Release-note-sized summary.",
      thinkingLevel: "medium",
      tools: ["repository.git-diff"],
      limits: { maxTurns: 8, maxToolCalls: 20 },
    },
    deep: {
      description: "Detailed architecture-oriented summary.",
      thinkingLevel: "high",
      limits: { maxTurns: 24, maxToolCalls: 80 },
    },
  },
  defaultProfile: "default",
  modelPolicy: permissiveModelPolicy,
  interactionPolicy: {
    allowHumanInput: false,
    allowSteer: true,
    allowFollowUp: true,
  },
  limits: {
    maxTurns: 14,
    maxToolCalls: 50,
    maxResultBytes: 100_000,
  },
  systemPrompt: [
    "You explain a repository commit range accurately using read-only evidence.",
    "Describe what changed and its likely impact; do not turn the task into a defect review.",
    "Use only the tools selected by this agent definition and never modify files.",
  ].join(" "),
  title(input: JsonObject): string {
    const repository = input.repository_context as JsonObject;
    return `Summarize PR #${String(repository.pr_number)}: ${String(repository.repo_full_name)}`;
  },
  repositoryContext(input: JsonObject) {
    return asRepositoryContext(input.repository_context);
  },
  buildPrompt({ input, repository, profile }): string {
    return [
      `Summarize ${repository.repo_full_name} pull request #${repository.pr_number}.`,
      `Base commit: ${repository.base_sha}`,
      `Head commit: ${repository.head_sha}`,
      `Audience: ${String(input.audience ?? "developer")}`,
      `Profile: ${profile}`,
      ...(input.focus === undefined ? [] : [`Requested focus: ${String(input.focus)}`]),
      "Inspect the commit-range diff and relevant context.",
      "When finished, call submit_change_summary with the structured result.",
    ].join("\n");
  },
};
