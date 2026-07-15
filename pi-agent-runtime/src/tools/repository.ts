import { readFile, readdir, stat } from "node:fs/promises";
import { join, relative, sep } from "node:path";

import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { AgentToolContext, RuntimeTool } from "../agent/types.js";
import { resolveWorkspacePath, runProcess, textToolResult, validateRelativePath } from "./utils.js";

export function createListFilesTool(context: AgentToolContext): RuntimeTool {
  return defineTool({
    name: "list_files",
    label: "List files",
    description: "List repository files below a repository-relative directory.",
    promptSnippet: "List files inside the isolated agent workspace",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Repository-relative directory; defaults to ." })),
      max_depth: Type.Optional(Type.Integer({ minimum: 1, maximum: 8 })),
    }),
    async execute(_toolCallId, params) {
      const workspace = context.record.workspace_path;
      const base = await resolveWorkspacePath(workspace, params.path ?? ".");
      const baseMetadata = await stat(base);
      if (!baseMetadata.isDirectory()) throw new Error("list_files path must be a directory.");
      const maxDepth = params.max_depth ?? 3;
      const files: string[] = [];
      const walk = async (directory: string, depth: number): Promise<void> => {
        if (depth > maxDepth || files.length >= 2000) return;
        const entries = await readdir(directory, { withFileTypes: true });
        entries.sort((left, right) => left.name.localeCompare(right.name));
        for (const entry of entries) {
          if (entry.name === ".git" || entry.name === "node_modules") continue;
          const path = join(directory, entry.name);
          const rel = relative(workspace, path).split(sep).join("/");
          files.push(entry.isDirectory() ? `${rel}/` : rel);
          if (entry.isDirectory()) await walk(path, depth + 1);
          if (files.length >= 2000) break;
        }
      };
      await walk(base, 1);
      return textToolResult(files.join("\n") || "(empty directory)", {
        count: files.length,
        truncated: files.length >= 2000,
      });
    },
  });
}

export function createReadFileTool(context: AgentToolContext): RuntimeTool {
  return defineTool({
    name: "read_file",
    label: "Read file",
    description: "Read a UTF-8 text file from the isolated agent workspace.",
    promptSnippet: "Read a repository file",
    parameters: Type.Object({
      path: Type.String({ description: "Repository-relative file path" }),
      start_line: Type.Optional(Type.Integer({ minimum: 1 })),
      max_lines: Type.Optional(Type.Integer({ minimum: 1, maximum: 2000 })),
    }),
    async execute(_toolCallId, params) {
      const target = await resolveWorkspacePath(context.record.workspace_path, params.path);
      const metadata = await stat(target);
      if (!metadata.isFile()) throw new Error("read_file path must be a file.");
      if (metadata.size > 2_000_000) throw new Error("File is too large to read safely.");
      const content = await readFile(target, "utf8");
      if (content.includes("\0")) throw new Error("Binary files are not supported.");
      const lines = content.split("\n");
      const start = (params.start_line ?? 1) - 1;
      const maxLines = params.max_lines ?? 500;
      const selected = lines.slice(start, start + maxLines);
      const numbered = selected.map((line, index) => `${start + index + 1}: ${line}`).join("\n");
      return textToolResult(numbered, {
        start_line: start + 1,
        returned_lines: selected.length,
        total_lines: lines.length,
        truncated: start + selected.length < lines.length,
      });
    },
  });
}

export function createSearchCodeTool(context: AgentToolContext): RuntimeTool {
  return defineTool({
    name: "search_code",
    label: "Search code",
    description: "Search repository text with ripgrep. The query is passed as a literal string by default.",
    promptSnippet: "Search text in the repository",
    parameters: Type.Object({
      query: Type.String({ minLength: 1, maxLength: 500 }),
      path: Type.Optional(Type.String({ description: "Repository-relative file or directory" })),
      regex: Type.Optional(Type.Boolean()),
      max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
    }),
    async execute(_toolCallId, params, signal) {
      const requestedPath = params.path ?? ".";
      await resolveWorkspacePath(context.record.workspace_path, requestedPath);
      const args = ["--line-number", "--no-heading", "--color", "never"];
      if (!params.regex) args.push("--fixed-strings");
      args.push(
        "--max-count",
        String(params.max_results ?? 200),
        "--",
        params.query,
        validateRelativePath(requestedPath),
      );
      const result = await runProcess("rg", args, context.record.workspace_path, signal);
      if (result.exitCode > 1) throw new Error(result.stderr || `rg exited with ${result.exitCode}`);
      return textToolResult(result.stdout || "No matches.", {
        exit_code: result.exitCode,
        truncated: result.truncated,
      });
    },
  });
}

export function createGitDiffTool(context: AgentToolContext): RuntimeTool {
  return defineTool({
    name: "git_diff",
    label: "Git diff",
    description: "Read the configured commit-range diff.",
    promptSnippet: "Inspect the configured repository commit range",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Optional repository-relative path filter" })),
      context_lines: Type.Optional(Type.Integer({ minimum: 0, maximum: 200 })),
    }),
    async execute(_toolCallId, params, signal) {
      const repository = context.repository;
      const args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        `--unified=${params.context_lines ?? 40}`,
        `${repository.base_sha}...${repository.head_sha}`,
      ];
      if (params.path !== undefined) {
        await resolveWorkspacePath(context.record.workspace_path, params.path);
        args.push("--", validateRelativePath(params.path));
      }
      const result = await runProcess("git", args, context.record.workspace_path, signal, 60_000);
      if (result.exitCode !== 0) {
        throw new Error(result.stderr || `git diff exited with ${result.exitCode}`);
      }
      const suffix = result.truncated
        ? "\n\n[Diff truncated; use the path filter to inspect smaller sections.]"
        : "";
      return textToolResult((result.stdout || "No changes in this range.") + suffix, {
        truncated: result.truncated,
      });
    },
  });
}
