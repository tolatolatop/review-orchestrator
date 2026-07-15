import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";

const MAX_TOOL_OUTPUT_BYTES = 500_000;

interface ProcessResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  truncated: boolean;
}

export function validateRelativePath(value: string): string {
  const normalized = value.replaceAll("\\", "/").replace(/^\.\//, "");
  if (isAbsolute(normalized) || normalized.split("/").some((part) => part === "..")) {
    throw new Error("Only repository-relative paths are allowed.");
  }
  return normalized || ".";
}

export async function resolveWorkspacePath(
  workspace: string,
  requested: string,
): Promise<string> {
  const normalized = validateRelativePath(requested);
  const target = await realpath(resolve(workspace, normalized));
  const rel = relative(workspace, target);
  if (rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel))) {
    return target;
  }
  throw new Error("Path resolves outside the agent workspace.");
}

export async function runProcess(
  command: string,
  args: string[],
  cwd: string,
  signal?: AbortSignal,
  timeoutMs = 30_000,
): Promise<ProcessResult> {
  return await new Promise<ProcessResult>((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, {
      cwd,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
      env: { PATH: process.env.PATH ?? "/usr/bin:/bin", LANG: "C.UTF-8" },
    });
    let stdout: Buffer<ArrayBufferLike> = Buffer.alloc(0);
    let stderr: Buffer<ArrayBufferLike> = Buffer.alloc(0);
    let truncated = false;
    const collect = (
      current: Buffer<ArrayBufferLike>,
      chunk: Buffer<ArrayBufferLike>,
    ): Buffer<ArrayBufferLike> => {
      if (current.length >= MAX_TOOL_OUTPUT_BYTES) {
        truncated = true;
        return current;
      }
      const remaining = MAX_TOOL_OUTPUT_BYTES - current.length;
      if (chunk.length > remaining) truncated = true;
      return Buffer.concat([current, chunk.subarray(0, remaining)]);
    };
    child.stdout.on("data", (chunk: Buffer<ArrayBufferLike>) => {
      stdout = collect(stdout, chunk);
    });
    child.stderr.on("data", (chunk: Buffer<ArrayBufferLike>) => {
      stderr = collect(stderr, chunk);
    });
    const timeout = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    const abort = () => child.kill("SIGKILL");
    signal?.addEventListener("abort", abort, { once: true });
    child.on("error", (error) => {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
      rejectPromise(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
      if (signal?.aborted) {
        rejectPromise(new Error("Operation aborted."));
        return;
      }
      resolvePromise({
        stdout: stdout.toString("utf8"),
        stderr: stderr.toString("utf8"),
        exitCode: code ?? -1,
        truncated,
      });
    });
  });
}

export function textToolResult(
  text: string,
  details: Record<string, unknown> = {},
) {
  return { content: [{ type: "text" as const, text }], details };
}
