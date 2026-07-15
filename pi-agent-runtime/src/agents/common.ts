import { Type } from "typebox";

import type { AgentModelPolicy, RepositoryContext } from "../agent/types.js";

export const repositoryContextSchema = Type.Object({
  provider: Type.String({ minLength: 1, maxLength: 64 }),
  repo_full_name: Type.String({ minLength: 1, maxLength: 512 }),
  pr_number: Type.Integer({ minimum: 1 }),
  base_sha: Type.String({ pattern: "^[0-9a-fA-F]{7,64}$" }),
  head_sha: Type.String({ pattern: "^[0-9a-fA-F]{7,64}$" }),
});

export const permissiveModelPolicy: AgentModelPolicy = {
  allowedThinkingLevels: ["minimal", "low", "medium", "high", "xhigh"],
};

export const repositoryTools = [
  "repository.list-files",
  "repository.read-file",
  "repository.search-code",
  "repository.git-diff",
  "workspace.write-file",
  "workspace.shell",
];

export function asRepositoryContext(value: unknown): RepositoryContext {
  return value as RepositoryContext;
}
