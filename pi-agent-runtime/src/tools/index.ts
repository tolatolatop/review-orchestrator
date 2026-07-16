import {
  createGitDiffTool,
  createListFilesTool,
  createReadFileTool,
  createSearchCodeTool,
  createShellTool,
  createWriteFileTool,
} from "./repository.js";
import { ToolRegistry } from "./registry.js";
import {
  createReviewActionTool,
  type ReviewActionToolOptions,
} from "./orchestrator.js";

export function createDefaultToolRegistry(
  reviewActionOptions: ReviewActionToolOptions = {},
): ToolRegistry {
  const registry = new ToolRegistry();
  registry.register("repository.list-files", createListFilesTool);
  registry.register("repository.read-file", createReadFileTool);
  registry.register("repository.search-code", createSearchCodeTool);
  registry.register("repository.git-diff", createGitDiffTool);
  registry.register("workspace.write-file", createWriteFileTool);
  registry.register("workspace.shell", createShellTool);
  registry.register(
    "orchestrator.review-action",
    (context) => createReviewActionTool(context, reviewActionOptions),
  );
  return registry;
}

export { createCompletionTool } from "./completion.js";
export { ToolRegistry } from "./registry.js";
