import { createRequestHumanInputTool } from "./interaction.js";
import {
  createGitDiffTool,
  createListFilesTool,
  createReadFileTool,
  createSearchCodeTool,
} from "./repository.js";
import { ToolRegistry } from "./registry.js";

export function createDefaultToolRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register("repository.list-files", createListFilesTool);
  registry.register("repository.read-file", createReadFileTool);
  registry.register("repository.search-code", createSearchCodeTool);
  registry.register("repository.git-diff", createGitDiffTool);
  registry.register("interaction.request-human-input", createRequestHumanInputTool);
  return registry;
}

export { createCompletionTool } from "./completion.js";
export { ToolRegistry } from "./registry.js";
