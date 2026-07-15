import { spawn } from "node:child_process";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { mkdir, readFile, readdir, realpath, rename, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

import type { Api, Model } from "@earendil-works/pi-ai";
import {
  AuthStorage,
  createAgentSession,
  DefaultResourceLoader,
  defineTool,
  ModelRegistry,
  SessionManager,
  SettingsManager,
  VERSION as PI_VERSION,
  type AgentSession,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MAX_REQUEST_BYTES = 1_000_000;
const MAX_TOOL_OUTPUT_BYTES = 500_000;
const MAX_EVENTS = 200;
const SKILL_NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const COMMIT_PATTERN = /^[0-9a-f]{7,64}$/i;
const THINKING_LEVELS = new Set(["minimal", "low", "medium", "high", "xhigh"]);

type SessionStatus =
  | "starting"
  | "running"
  | "waiting_for_input"
  | "completed"
  | "failed"
  | "cancelled";

type ThinkingLevel = "minimal" | "low" | "medium" | "high" | "xhigh";
type SessionKind = "review" | "instruction";

interface ReviewInput {
  provider: string;
  repo_full_name: string;
  pr_number: number;
  base_sha: string;
  head_sha: string;
  workspace_path: string;
  review_mode?: string;
}

interface RepositoryContext {
  provider: string;
  repo_full_name: string;
  pr_number: number;
  base_sha: string;
  head_sha: string;
}

interface InstructionHistoryItem {
  author_login: string;
  command: string;
  answer: string;
  outcome: "answered" | "needs_clarification" | "refused";
  head_sha: string;
}

interface InstructionInput {
  text: string;
  author_login: string;
  source_url?: string;
  history: InstructionHistoryItem[];
}

interface ModelSelection {
  provider?: string;
  id?: string;
  thinking_level?: string;
  base_url?: string;
}

interface StartSessionRequest {
  kind: SessionKind;
  idempotency_key?: string;
  title?: string;
  workspace_path: string;
  review?: ReviewInput;
  repository_context?: RepositoryContext;
  instruction?: InstructionInput;
  model?: ModelSelection;
  skills?: string[];
  profile?: string;
}

interface HumanMessageRequest {
  message: string;
  delivery?: "answer" | "steer" | "follow_up";
}

interface RuntimeEvent {
  at: string;
  type: string;
  stage: string;
  tool?: string;
}

interface PendingInput {
  id: string;
  question: string;
  choices?: string[];
  resolve: (answer: string) => void;
  reject: (error: Error) => void;
}

interface SessionRecord {
  id: string;
  kind: SessionKind;
  idempotency_key?: string;
  title: string;
  status: SessionStatus;
  stage: string;
  workspace_path: string;
  review?: ReviewInput;
  repository_context?: RepositoryContext;
  instruction?: InstructionInput;
  provider: string;
  model: string;
  thinking_level: ThinkingLevel;
  skills: string[];
  profile: string;
  result?: Record<string, unknown>;
  error?: string;
  session_file?: string;
  created_at: string;
  updated_at: string;
  events: RuntimeEvent[];
  session?: AgentSession;
  pending?: PendingInput;
}

interface PublicPendingInput {
  id: string;
  question: string;
  choices?: string[];
}

interface PublicSession {
  id: string;
  kind: SessionKind;
  idempotency_key?: string;
  title: string;
  status: SessionStatus;
  stage: string;
  workspace_path: string;
  provider: string;
  model: string;
  thinking_level: ThinkingLevel;
  skills: string[];
  profile: string;
  result?: Record<string, unknown>;
  error?: string;
  session_file?: string;
  pending_input?: PublicPendingInput;
  created_at: string;
  updated_at: string;
  events: RuntimeEvent[];
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const config = {
  host: process.env.PI_AGENT_HOST ?? "0.0.0.0",
  port: parsePositiveInt(process.env.PI_AGENT_PORT, 3210),
  stateRoot: resolve(process.env.PI_AGENT_STATE_ROOT ?? "/var/lib/pi-agent"),
  workspaceRoot: resolve(process.env.PI_AGENT_WORKSPACE_ROOT ?? "/workspaces"),
  skillsRoot: resolve(process.env.PI_AGENT_SKILLS_ROOT ?? "/opt/pi-agent/skills"),
  agentDir: resolve(process.env.PI_CODING_AGENT_DIR ?? "/var/lib/pi-agent/config"),
  modelsFile: resolve(
    process.env.PI_AGENT_MODELS_FILE ?? "/etc/pi-agent/models.json",
  ),
  serviceToken: process.env.PI_AGENT_RUNTIME_TOKEN,
  defaultProvider: process.env.PI_AGENT_PROVIDER ?? "openai",
  defaultModel: process.env.PI_AGENT_MODEL ?? "gpt-5.4",
  defaultThinking: normalizeThinking(process.env.PI_AGENT_THINKING_LEVEL ?? "high"),
  defaultSkill: process.env.PI_AGENT_REVIEW_SKILL ?? "code-review",
  defaultInstructionSkill: process.env.PI_AGENT_COMMAND_SKILL ?? "pr-assistant",
  defaultProfile: process.env.PI_AGENT_REVIEW_PROFILE ?? "default",
  modelBaseUrl: process.env.PI_AGENT_MODEL_BASE_URL,
};

const sessions = new Map<string, SessionRecord>();
const idempotentSessions = new Map<string, string>();
const persistenceQueues = new Map<string, Promise<void>>();

function parsePositiveInt(value: string | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function normalizeThinking(value: string): ThinkingLevel {
  if (!THINKING_LEVELS.has(value)) {
    throw new Error(`Unsupported thinking level: ${value}`);
  }
  return value as ThinkingLevel;
}

function now(): string {
  return new Date().toISOString();
}

function addEvent(record: SessionRecord, event: Omit<RuntimeEvent, "at">): void {
  record.events.push({ at: now(), ...event });
  if (record.events.length > MAX_EVENTS) {
    record.events.splice(0, record.events.length - MAX_EVENTS);
  }
  record.updated_at = now();
  void persistRecord(record).catch((error: unknown) => {
    process.stderr.write(
      `Failed to persist pi-agent session ${record.id}: ${error instanceof Error ? error.message : String(error)}\n`,
    );
  });
}

function publicSession(record: SessionRecord): PublicSession {
  const response: PublicSession = {
    id: record.id,
    kind: record.kind,
    title: record.title,
    status: record.status,
    stage: record.stage,
    workspace_path: record.workspace_path,
    provider: record.provider,
    model: record.model,
    thinking_level: record.thinking_level,
    skills: record.skills,
    profile: record.profile,
    created_at: record.created_at,
    updated_at: record.updated_at,
    events: record.events,
  };
  if (record.idempotency_key !== undefined) {
    response.idempotency_key = record.idempotency_key;
  }
  if (record.result !== undefined) response.result = record.result;
  if (record.error !== undefined) response.error = record.error;
  if (record.session_file !== undefined) response.session_file = record.session_file;
  if (record.pending !== undefined) {
    const pending: PublicPendingInput = {
      id: record.pending.id,
      question: record.pending.question,
    };
    if (record.pending.choices !== undefined) pending.choices = record.pending.choices;
    response.pending_input = pending;
  }
  return response;
}

function persistRecord(record: SessionRecord): Promise<void> {
  const snapshot = `${JSON.stringify(publicSession(record), null, 2)}\n`;
  const previous = persistenceQueues.get(record.id) ?? Promise.resolve();
  const pending = previous.catch(() => undefined).then(async () => {
    await writeRecordSnapshot(record.id, snapshot);
  });
  persistenceQueues.set(record.id, pending);
  void pending.finally(() => {
    if (persistenceQueues.get(record.id) === pending) {
      persistenceQueues.delete(record.id);
    }
  }).catch(() => undefined);
  return pending;
}

async function writeRecordSnapshot(id: string, snapshot: string): Promise<void> {
  const dir = join(config.stateRoot, "runtime-sessions");
  await mkdir(dir, { recursive: true });
  const target = join(dir, `${id}.json`);
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, snapshot, {
    mode: 0o600,
  });
  await rename(temporary, target);
}

export async function flushPersistence(): Promise<void> {
  while (persistenceQueues.size > 0) {
    await Promise.allSettled([...persistenceQueues.values()]);
  }
}

async function loadPersistedSessions(): Promise<void> {
  const dir = join(config.stateRoot, "runtime-sessions");
  await mkdir(dir, { recursive: true });
  for (const name of await readdir(dir)) {
    if (!name.endsWith(".json")) continue;
    try {
      const value = JSON.parse(await readFile(join(dir, name), "utf8")) as PublicSession;
      const status = TERMINAL_STATUSES.has(value.status) ? value.status : "failed";
      const record: SessionRecord = {
        ...value,
        kind: value.kind ?? "review",
        status,
      };
      if (record.kind === "review") {
        record.review = {
          provider: "unknown",
          repo_full_name: "unknown/unknown",
          pr_number: 1,
          base_sha: "unknown",
          head_sha: "unknown",
          workspace_path: value.workspace_path,
        };
      }
      if (!TERMINAL_STATUSES.has(value.status)) {
        record.stage = "runtime_restarted";
        record.error = "The pi-agent runtime restarted while this session was active.";
        record.updated_at = now();
        await persistRecord(record);
      }
      sessions.set(record.id, record);
      if (record.idempotency_key !== undefined) {
        idempotentSessions.set(record.idempotency_key, record.id);
      }
    } catch {
      // A corrupt state snapshot must not prevent other sessions from being served.
    }
  }
}

function assertObject(value: unknown, name: string): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(422, `${name} must be an object.`);
  }
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new HttpError(422, `${name} must be a non-empty string.`);
  }
  return value.trim();
}

function validateRepositoryContext(
  value: unknown,
  name: string,
): RepositoryContext {
  assertObject(value, name);
  const context: RepositoryContext = {
    provider: requireString(value.provider, `${name}.provider`),
    repo_full_name: requireString(value.repo_full_name, `${name}.repo_full_name`),
    pr_number: Number(value.pr_number),
    base_sha: requireString(value.base_sha, `${name}.base_sha`),
    head_sha: requireString(value.head_sha, `${name}.head_sha`),
  };
  if (!Number.isInteger(context.pr_number) || context.pr_number <= 0) {
    throw new HttpError(422, `${name}.pr_number must be a positive integer.`);
  }
  if (!COMMIT_PATTERN.test(context.base_sha) || !COMMIT_PATTERN.test(context.head_sha)) {
    throw new HttpError(422, `${name} base_sha and head_sha must be commit hashes.`);
  }
  return context;
}

function validateInstruction(value: unknown): InstructionInput {
  assertObject(value, "instruction");
  const text = requireString(value.text, "instruction.text");
  if (text.length > 8000) throw new HttpError(422, "instruction.text is too long.");
  const historyValue = value.history ?? [];
  if (!Array.isArray(historyValue)) {
    throw new HttpError(422, "instruction.history must be an array.");
  }
  if (historyValue.length > 6) {
    throw new HttpError(422, "instruction.history may contain at most 6 turns.");
  }
  const history = historyValue.map((item, index): InstructionHistoryItem => {
    assertObject(item, `instruction.history[${index}]`);
    const outcome = requireString(item.outcome, `instruction.history[${index}].outcome`);
    if (!["answered", "needs_clarification", "refused"].includes(outcome)) {
      throw new HttpError(422, `instruction.history[${index}].outcome is invalid.`);
    }
    const headSha = requireString(item.head_sha, `instruction.history[${index}].head_sha`);
    if (!COMMIT_PATTERN.test(headSha)) {
      throw new HttpError(422, `instruction.history[${index}].head_sha must be a commit hash.`);
    }
    return {
      author_login: requireString(item.author_login, `instruction.history[${index}].author_login`),
      command: requireString(item.command, `instruction.history[${index}].command`),
      answer: requireString(item.answer, `instruction.history[${index}].answer`),
      outcome: outcome as InstructionHistoryItem["outcome"],
      head_sha: headSha,
    };
  });
  const instruction: InstructionInput = {
    text,
    author_login: requireString(value.author_login, "instruction.author_login"),
    history,
  };
  if (value.source_url !== undefined) {
    instruction.source_url = requireString(value.source_url, "instruction.source_url");
  }
  return instruction;
}

export function validateStartRequest(value: unknown): StartSessionRequest {
  assertObject(value, "request");
  const kindValue = value.kind ?? "review";
  if (kindValue !== "review" && kindValue !== "instruction") {
    throw new HttpError(422, "kind must be review or instruction.");
  }
  const kind = kindValue as SessionKind;
  const workspacePath = requireString(value.workspace_path, "workspace_path");

  let review: ReviewInput | undefined;
  let repositoryContext: RepositoryContext | undefined;
  let instruction: InstructionInput | undefined;
  if (kind === "review") {
    const context = validateRepositoryContext(value.review, "review");
    review = { ...context, workspace_path: workspacePath };
    if (value.review !== null && typeof value.review === "object" && !Array.isArray(value.review)) {
      const reviewValue = value.review as Record<string, unknown>;
      if (typeof reviewValue.review_mode === "string") {
        review.review_mode = reviewValue.review_mode;
      }
    }
  } else {
    repositoryContext = validateRepositoryContext(
      value.repository_context,
      "repository_context",
    );
    instruction = validateInstruction(value.instruction);
  }

  let model: ModelSelection | undefined;
  if (value.model !== undefined) {
    assertObject(value.model, "model");
    model = {};
    if (value.model.provider !== undefined) model.provider = requireString(value.model.provider, "model.provider");
    if (value.model.id !== undefined) model.id = requireString(value.model.id, "model.id");
    if (value.model.thinking_level !== undefined) {
      model.thinking_level = requireString(value.model.thinking_level, "model.thinking_level");
      normalizeThinking(model.thinking_level);
    }
    if (value.model.base_url !== undefined) model.base_url = requireString(value.model.base_url, "model.base_url");
  }

  let skills: string[] | undefined;
  if (value.skills !== undefined) {
    if (!Array.isArray(value.skills)) throw new HttpError(422, "skills must be an array.");
    skills = value.skills.map((item, index) => {
      const skill = requireString(item, `skills[${index}]`);
      if (!SKILL_NAME_PATTERN.test(skill)) throw new HttpError(422, `Invalid skill name: ${skill}`);
      return skill;
    });
  }

  const request: StartSessionRequest = { kind, workspace_path: workspacePath };
  if (review !== undefined) request.review = review;
  if (repositoryContext !== undefined) request.repository_context = repositoryContext;
  if (instruction !== undefined) request.instruction = instruction;
  if (value.idempotency_key !== undefined) {
    const key = requireString(value.idempotency_key, "idempotency_key");
    if (key.length > 256) throw new HttpError(422, "idempotency_key is too long.");
    request.idempotency_key = key;
  }
  if (typeof value.title === "string" && value.title.trim() !== "") request.title = value.title.trim();
  if (typeof value.profile === "string" && value.profile.trim() !== "") request.profile = value.profile.trim();
  if (model !== undefined) request.model = model;
  if (skills !== undefined) request.skills = skills;
  return request;
}

async function validateWorkspace(workspacePath: string): Promise<string> {
  const [root, workspace] = await Promise.all([
    realpath(config.workspaceRoot),
    realpath(workspacePath).catch(() => {
      throw new HttpError(422, `Workspace does not exist: ${workspacePath}`);
    }),
  ]);
  const rel = relative(root, workspace);
  if (rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel))) {
    const metadata = await stat(workspace);
    if (!metadata.isDirectory()) throw new HttpError(422, "workspace_path must be a directory.");
    return workspace;
  }
  throw new HttpError(422, "workspace_path is outside PI_AGENT_WORKSPACE_ROOT.");
}

function validateRelativePath(value: string): string {
  const normalized = value.replaceAll("\\", "/").replace(/^\.\//, "");
  if (isAbsolute(normalized) || normalized.split("/").some((part) => part === "..")) {
    throw new Error("Only repository-relative paths are allowed.");
  }
  return normalized || ".";
}

async function resolveWorkspacePath(workspace: string, requested: string): Promise<string> {
  const normalized = validateRelativePath(requested);
  const target = await realpath(resolve(workspace, normalized));
  const rel = relative(workspace, target);
  if (rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel))) return target;
  throw new Error("Path resolves outside the review workspace.");
}

interface ProcessResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  truncated: boolean;
}

async function runProcess(
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

function textToolResult(text: string, details: Record<string, unknown> = {}) {
  return { content: [{ type: "text" as const, text }], details };
}

function repositoryContext(record: SessionRecord): RepositoryContext {
  if (record.review !== undefined) return record.review;
  if (record.repository_context !== undefined) return record.repository_context;
  throw new Error("Session has no repository context.");
}

export function createReviewTools(record: SessionRecord) {
  const listFiles = defineTool({
    name: "list_files",
    label: "List files",
    description: "List repository files below a repository-relative directory.",
    promptSnippet: "List files inside the isolated review workspace",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Repository-relative directory; defaults to ." })),
      max_depth: Type.Optional(Type.Integer({ minimum: 1, maximum: 8 })),
    }),
    async execute(_toolCallId, params) {
      const base = await resolveWorkspacePath(record.workspace_path, params.path ?? ".");
      const baseMetadata = await stat(base);
      if (!baseMetadata.isDirectory()) throw new Error("list_files path must be a directory.");
      const maxDepth = params.max_depth ?? 3;
      const files: string[] = [];
      const walk = async (directory: string, depth: number): Promise<void> => {
        if (depth > maxDepth || files.length >= 2000) return;
        const entries = await readdir(directory, { withFileTypes: true });
        entries.sort((a, b) => a.name.localeCompare(b.name));
        for (const entry of entries) {
          if (entry.name === ".git" || entry.name === "node_modules") continue;
          const path = join(directory, entry.name);
          const rel = relative(record.workspace_path, path).split(sep).join("/");
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

  const readFileTool = defineTool({
    name: "read_file",
    label: "Read file",
    description: "Read a UTF-8 text file from the isolated review workspace.",
    promptSnippet: "Read a repository file",
    parameters: Type.Object({
      path: Type.String({ description: "Repository-relative file path" }),
      start_line: Type.Optional(Type.Integer({ minimum: 1 })),
      max_lines: Type.Optional(Type.Integer({ minimum: 1, maximum: 2000 })),
    }),
    async execute(_toolCallId, params) {
      const target = await resolveWorkspacePath(record.workspace_path, params.path);
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

  const searchCode = defineTool({
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
      await resolveWorkspacePath(record.workspace_path, requestedPath);
      const args = ["--line-number", "--no-heading", "--color", "never"];
      if (!params.regex) args.push("--fixed-strings");
      args.push("--max-count", String(params.max_results ?? 200), "--", params.query, validateRelativePath(requestedPath));
      const result = await runProcess("rg", args, record.workspace_path, signal);
      if (result.exitCode > 1) throw new Error(result.stderr || `rg exited with ${result.exitCode}`);
      return textToolResult(result.stdout || "No matches.", {
        exit_code: result.exitCode,
        truncated: result.truncated,
      });
    },
  });

  const gitDiff = defineTool({
    name: "git_diff",
    label: "Git diff",
    description: "Read the pull request diff between the configured base and head commits.",
    promptSnippet: "Inspect the configured pull request commit range",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Optional repository-relative path filter" })),
      context_lines: Type.Optional(Type.Integer({ minimum: 0, maximum: 200 })),
    }),
    async execute(_toolCallId, params, signal) {
      const context = repositoryContext(record);
      const args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        `--unified=${params.context_lines ?? 40}`,
        `${context.base_sha}...${context.head_sha}`,
      ];
      if (params.path !== undefined) {
        await resolveWorkspacePath(record.workspace_path, params.path);
        args.push("--", validateRelativePath(params.path));
      }
      const result = await runProcess("git", args, record.workspace_path, signal, 60_000);
      if (result.exitCode !== 0) throw new Error(result.stderr || `git diff exited with ${result.exitCode}`);
      const suffix = result.truncated ? "\n\n[Diff truncated; use the path filter to inspect smaller sections.]" : "";
      return textToolResult((result.stdout || "No changes in this range.") + suffix, {
        truncated: result.truncated,
      });
    },
  });

  const requestHumanInput = defineTool({
    name: "request_human_input",
    label: "Request human input",
    description: "Pause the review and ask an operator for information that cannot be established from the repository.",
    promptSnippet: "Pause for an operator answer when repository context is insufficient",
    executionMode: "sequential" as const,
    parameters: Type.Object({
      question: Type.String({ minLength: 1, maxLength: 2000 }),
      choices: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 500 }), { maxItems: 10 })),
    }),
    async execute(_toolCallId, params, signal) {
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
        addEvent(record, { type: "human_input_requested", stage: record.stage, tool: "request_human_input" });
        const abort = () => rejectAnswer(new Error("Human-input request aborted."));
        signal?.addEventListener("abort", abort, { once: true });
      });
      delete record.pending;
      record.status = "running";
      record.stage = "analyzing";
      addEvent(record, { type: "human_input_received", stage: record.stage, tool: "request_human_input" });
      return textToolResult(`Operator answer: ${answer}`, { answered: true });
    },
  });

  const findingSchema = Type.Object({
    file: Type.String({ minLength: 1, maxLength: 1024 }),
    line: Type.Optional(Type.Integer({ minimum: 1 })),
    line_end: Type.Optional(Type.Integer({ minimum: 1 })),
    severity: Type.Union([
      Type.Literal("critical"),
      Type.Literal("high"),
      Type.Literal("medium"),
      Type.Literal("low"),
      Type.Literal("info"),
    ]),
    category: Type.Optional(Type.String({ maxLength: 64 })),
    message: Type.String({ minLength: 1, maxLength: 1200 }),
    suggestion: Type.Optional(Type.String({ maxLength: 1200 })),
    confidence: Type.Number({ minimum: 0, maximum: 1 }),
  });
  const submitReview = defineTool({
    name: "submit_review",
    label: "Submit review",
    description: "Submit the final structured pull request review and end the agent run.",
    promptSnippet: "Submit the final machine-readable review",
    promptGuidelines: ["Always finish by calling submit_review exactly once."],
    executionMode: "sequential" as const,
    parameters: Type.Object({
      summary: Type.String({ minLength: 1, maxLength: 8000 }),
      findings: Type.Array(findingSchema, { maxItems: 100 }),
    }),
    async execute(_toolCallId, params) {
      record.result = params as unknown as Record<string, unknown>;
      record.status = "completed";
      record.stage = "completed";
      addEvent(record, { type: "review_submitted", stage: record.stage, tool: "submit_review" });
      return {
        ...textToolResult("Structured review accepted.", { finding_count: params.findings.length }),
        terminate: true,
      };
    },
  });

  return [listFiles, readFileTool, searchCode, gitDiff, requestHumanInput, submitReview];
}

export function createInstructionTools(record: SessionRecord) {
  const commonTools = createReviewTools(record).filter((tool) =>
    ["list_files", "read_file", "search_code", "git_diff"].includes(tool.name),
  );
  const referenceSchema = Type.Object({
    path: Type.String({ minLength: 1, maxLength: 1024 }),
    line_start: Type.Optional(Type.Integer({ minimum: 1 })),
    line_end: Type.Optional(Type.Integer({ minimum: 1 })),
  });
  const submitTaskResult = defineTool({
    name: "submit_task_result",
    label: "Submit task result",
    description: "Submit the final answer to the user's pull-request command and end the agent run.",
    promptSnippet: "Submit the final validated command answer",
    promptGuidelines: ["Always finish by calling submit_task_result exactly once."],
    executionMode: "sequential" as const,
    parameters: Type.Object({
      outcome: Type.Union([
        Type.Literal("answered"),
        Type.Literal("needs_clarification"),
        Type.Literal("refused"),
      ]),
      answer: Type.String({ minLength: 1, maxLength: 30000 }),
      references: Type.Optional(Type.Array(referenceSchema, { maxItems: 50 })),
    }),
    async execute(_toolCallId, params) {
      for (const reference of params.references ?? []) {
        const target = await resolveWorkspacePath(record.workspace_path, reference.path);
        const metadata = await stat(target);
        if (!metadata.isFile()) {
          throw new Error(`Reference path is not a file: ${reference.path}`);
        }
        if (
          reference.line_start !== undefined
          && reference.line_end !== undefined
          && reference.line_end < reference.line_start
        ) {
          throw new Error("Reference line_end must be greater than or equal to line_start.");
        }
      }
      record.result = params as unknown as Record<string, unknown>;
      record.status = "completed";
      record.stage = "completed";
      addEvent(record, {
        type: "task_result_submitted",
        stage: record.stage,
        tool: "submit_task_result",
      });
      return {
        ...textToolResult("Structured task result accepted.", {
          outcome: params.outcome,
          reference_count: params.references?.length ?? 0,
        }),
        terminate: true,
      };
    },
  });
  return [...commonTools, submitTaskResult];
}

function createAuthStorage(provider: string): AuthStorage {
  const authStorage = AuthStorage.create(join(config.agentDir, "auth.json"));
  const genericKey = process.env.PI_AGENT_LLM_API_KEY;
  if (genericKey) authStorage.setRuntimeApiKey(provider, genericKey);
  return authStorage;
}

function resolveModel(
  registry: ModelRegistry,
  provider: string,
  modelId: string,
  baseUrl: string | undefined,
): Model<Api> {
  const selected = registry.find(provider, modelId);
  if (selected === undefined) {
    throw new Error(`Unknown pi model: ${provider}/${modelId}. Configure it in models.json if it is custom.`);
  }
  return baseUrl ? { ...selected, baseUrl } : selected;
}

export async function startSession(request: StartSessionRequest): Promise<SessionRecord> {
  if (request.idempotency_key !== undefined) {
    const existingId = idempotentSessions.get(request.idempotency_key);
    if (existingId !== undefined) {
      const existing = sessions.get(existingId);
      if (existing !== undefined) return existing;
      idempotentSessions.delete(request.idempotency_key);
    }
  }
  const workspace = await validateWorkspace(request.workspace_path);
  if (
    request.kind === "review"
    && request.review?.workspace_path !== request.workspace_path
  ) {
    throw new HttpError(422, "review.workspace_path must match workspace_path.");
  }
  const context = request.review ?? request.repository_context;
  if (context === undefined) throw new HttpError(422, "Repository context is required.");
  const provider = request.model?.provider ?? config.defaultProvider;
  const model = request.model?.id ?? config.defaultModel;
  const thinking = normalizeThinking(request.model?.thinking_level ?? config.defaultThinking);
  const defaultSkill = request.kind === "instruction"
    ? config.defaultInstructionSkill
    : config.defaultSkill;
  const skills = request.skills?.length ? request.skills : [defaultSkill];
  const timestamp = now();
  const record: SessionRecord = {
    id: randomUUID(),
    kind: request.kind,
    title: request.title ?? (
      request.kind === "review"
        ? `Review PR #${context.pr_number}: ${context.repo_full_name}`
        : `PR #${context.pr_number} command: ${context.repo_full_name}`
    ),
    status: "starting",
    stage: "starting",
    workspace_path: workspace,
    provider,
    model,
    thinking_level: thinking,
    skills,
    profile: request.profile ?? config.defaultProfile,
    created_at: timestamp,
    updated_at: timestamp,
    events: [{ at: timestamp, type: "session_created", stage: "starting" }],
  };
  if (request.idempotency_key !== undefined) {
    record.idempotency_key = request.idempotency_key;
  }
  if (request.review !== undefined) {
    record.review = { ...request.review, workspace_path: workspace };
  }
  if (request.repository_context !== undefined) {
    record.repository_context = request.repository_context;
  }
  if (request.instruction !== undefined) record.instruction = request.instruction;
  sessions.set(record.id, record);
  if (record.idempotency_key !== undefined) {
    idempotentSessions.set(record.idempotency_key, record.id);
  }
  await persistRecord(record);
  void runAgent(record, request.model?.base_url ?? config.modelBaseUrl);
  return record;
}

function instructionPrompt(
  record: SessionRecord,
  context: RepositoryContext,
): string {
  if (record.instruction === undefined) {
    throw new Error("Instruction session has no instruction payload.");
  }
  const history = record.instruction.history.length === 0
    ? "(no previous bot exchanges)"
    : record.instruction.history.map((item, index) => [
      `Previous exchange ${index + 1} at ${item.head_sha}:`,
      `${item.author_login}: ${item.command}`,
      `assistant (${item.outcome}): ${item.answer}`,
    ].join("\n")).join("\n\n");
  return [
    `Answer a command about ${context.repo_full_name} pull request #${context.pr_number}.`,
    `Provider: ${context.provider}`,
    `Base commit: ${context.base_sha}`,
    `Head commit: ${context.head_sha}`,
    `Profile: ${record.profile}`,
    "Previous orchestrator-owned exchanges:",
    history,
    "Current user command:",
    `${record.instruction.author_login}: ${record.instruction.text}`,
    "Answer the current command directly. Inspect repository evidence as needed.",
    "When finished, call submit_task_result with the final structured result.",
  ].join("\n");
}

async function runAgent(record: SessionRecord, baseUrl: string | undefined): Promise<void> {
  try {
    const skillPaths = record.skills.map((skill) => join(config.skillsRoot, skill, "SKILL.md"));
    for (const skillPath of skillPaths) {
      await stat(skillPath).catch(() => {
        throw new Error(`Configured agent skill does not exist: ${skillPath}`);
      });
    }
    const authStorage = createAuthStorage(record.provider);
    const modelRegistry = ModelRegistry.create(authStorage, config.modelsFile);
    const selectedModel = resolveModel(modelRegistry, record.provider, record.model, baseUrl);
    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: true },
      retry: { enabled: true, maxRetries: 2 },
    });
    const loader = new DefaultResourceLoader({
      cwd: record.workspace_path,
      agentDir: config.agentDir,
      settingsManager,
      additionalSkillPaths: skillPaths,
      noExtensions: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: true,
      skillsOverride: (base) => ({
        skills: base.skills.filter((skill) =>
          skillPaths.some((skillPath) => resolve(skill.filePath) === resolve(skillPath)),
        ),
        diagnostics: base.diagnostics,
      }),
      appendSystemPrompt: [
        record.kind === "review"
          ? "You are an automated pull request review agent running in a read-only, isolated workspace."
          : "You are a pull request assistant answering a user's explicit command in a read-only, isolated workspace.",
        "Use only the supplied tools. Never attempt to modify files or publish provider comments.",
        record.kind === "review"
          ? "The submit_review tool is the only valid final output channel."
          : "The submit_task_result tool is the only valid final output channel.",
      ],
    });
    await loader.reload();
    const loadedSkills = new Set(loader.getSkills().skills.map((skill) => skill.name));
    for (const skill of record.skills) {
      if (!loadedSkills.has(skill)) throw new Error(`pi-agent did not load configured skill: ${skill}`);
    }

    const sessionDirectory = join(config.stateRoot, "pi-sessions", record.id);
    await mkdir(sessionDirectory, { recursive: true });
    const sessionManager = SessionManager.create(record.workspace_path, sessionDirectory);
    const customTools = record.kind === "review"
      ? createReviewTools(record)
      : createInstructionTools(record);
    const activeTools = customTools.map((tool) => tool.name);
    const { session } = await createAgentSession({
      cwd: record.workspace_path,
      agentDir: config.agentDir,
      authStorage,
      modelRegistry,
      model: selectedModel,
      thinkingLevel: record.thinking_level,
      customTools,
      tools: activeTools,
      resourceLoader: loader,
      sessionManager,
      settingsManager,
    });
    record.session = session;
    if (session.sessionFile !== undefined) record.session_file = session.sessionFile;
    session.setSessionName(record.title);
    session.subscribe((event) => {
      const raw = event as unknown as Record<string, unknown>;
      if (event.type === "agent_start") {
        record.status = "running";
        record.stage = "analyzing";
      } else if (event.type === "turn_start") {
        record.stage = "thinking";
      } else if (event.type === "tool_execution_start") {
        const toolName = typeof raw.toolName === "string" ? raw.toolName : "tool";
        record.stage = `tool:${toolName}`;
        addEvent(record, { type: event.type, stage: record.stage, tool: toolName });
        return;
      } else if (event.type === "tool_execution_end" && record.status !== "waiting_for_input") {
        record.stage = "analyzing";
      } else if (event.type === "auto_retry_start") {
        record.stage = "model_retry";
      }
      if (["agent_start", "turn_start", "turn_end", "agent_end", "auto_retry_start", "auto_retry_end"].includes(event.type)) {
        addEvent(record, { type: event.type, stage: record.stage });
      }
    });
    record.status = "running";
    record.stage = "analyzing";
    addEvent(record, { type: "runtime_ready", stage: record.stage });

    const context = repositoryContext(record);
    const prompt = record.kind === "review"
      ? [
        `Review ${context.repo_full_name} pull request #${context.pr_number}.`,
        `Provider: ${context.provider}`,
        `Base commit: ${context.base_sha}`,
        `Head commit: ${context.head_sha}`,
        `Profile: ${record.profile}`,
        "Inspect the complete base...head diff and relevant repository context.",
        "When finished, call submit_review with the final structured result.",
      ].join("\n")
      : instructionPrompt(record, context);
    await session.prompt(`/skill:${record.skills[0]} ${prompt}`, { source: "rpc" });

    if (!TERMINAL_STATUSES.has(record.status)) {
      record.status = "failed";
      record.stage = "missing_result";
      record.error = record.kind === "review"
        ? "pi-agent ended without calling submit_review."
        : "pi-agent ended without calling submit_task_result.";
      addEvent(record, { type: "session_failed", stage: record.stage });
    }
  } catch (error) {
    if (record.status !== "cancelled") {
      record.status = "failed";
      record.stage = "failed";
      record.error = error instanceof Error ? error.message : String(error);
      addEvent(record, { type: "session_failed", stage: record.stage });
    }
  } finally {
    record.updated_at = now();
    await persistRecord(record);
  }
}

async function sendHumanMessage(record: SessionRecord, request: HumanMessageRequest): Promise<void> {
  if (TERMINAL_STATUSES.has(record.status)) throw new HttpError(409, `Session is already ${record.status}.`);
  const message = requireString(request.message, "message");
  const delivery = request.delivery ?? (record.pending ? "answer" : "steer");
  if (record.pending !== undefined) {
    if (delivery !== "answer") throw new HttpError(409, "This session is waiting for an answer.");
    const pending = record.pending;
    delete record.pending;
    record.status = "running";
    record.stage = "analyzing";
    pending.resolve(message);
    await persistRecord(record);
    return;
  }
  if (delivery === "answer") throw new HttpError(409, "The session has no pending human-input request.");
  if (record.session === undefined) throw new HttpError(409, "The pi-agent session is still starting.");
  if (delivery === "follow_up") await record.session.followUp(message);
  else await record.session.steer(message);
  addEvent(record, { type: `human_${delivery}`, stage: record.stage });
}

async function cancelSession(record: SessionRecord): Promise<void> {
  if (TERMINAL_STATUSES.has(record.status)) return;
  record.status = "cancelled";
  record.stage = "cancelled";
  record.pending?.reject(new Error("Session cancelled."));
  delete record.pending;
  await record.session?.abort();
  addEvent(record, { type: "session_cancelled", stage: record.stage });
  await persistRecord(record);
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_REQUEST_BYTES) throw new HttpError(413, "Request body is too large.");
    chunks.push(buffer);
  }
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new HttpError(400, "Request body must be valid JSON.");
  }
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  const data = Buffer.from(`${JSON.stringify(body)}\n`);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": data.length,
    "cache-control": "no-store",
  });
  response.end(data);
}

function authorize(request: IncomingMessage): void {
  if (!config.serviceToken) return;
  if (request.headers.authorization !== `Bearer ${config.serviceToken}`) {
    throw new HttpError(401, "Invalid pi-agent runtime token.");
  }
}

function sessionIdFromPath(pathname: string, suffix = ""): string | undefined {
  const match = pathname.match(new RegExp(`^/v1/sessions/([^/]+)${suffix}$`));
  return match?.[1] ? decodeURIComponent(match[1]) : undefined;
}

async function handleRequest(request: IncomingMessage, response: ServerResponse): Promise<void> {
  try {
    const url = new URL(request.url ?? "/", "http://runtime.local");
    if (request.method === "GET" && url.pathname === "/health") {
      sendJson(response, 200, {
        status: "ok",
        runtime: "pi-agent",
        version: PI_VERSION,
      });
      return;
    }
    authorize(request);
    if (request.method === "POST" && url.pathname === "/v1/sessions") {
      const record = await startSession(validateStartRequest(await readJson(request)));
      sendJson(response, 202, publicSession(record));
      return;
    }
    const messageSessionId = sessionIdFromPath(url.pathname, "/messages");
    if (request.method === "POST" && messageSessionId !== undefined) {
      const record = sessions.get(messageSessionId);
      if (!record) throw new HttpError(404, "Session not found.");
      const body = await readJson(request);
      assertObject(body, "request");
      const delivery = body.delivery;
      if (delivery !== undefined && !["answer", "steer", "follow_up"].includes(String(delivery))) {
        throw new HttpError(422, "delivery must be answer, steer, or follow_up.");
      }
      const humanRequest: HumanMessageRequest = { message: requireString(body.message, "message") };
      if (delivery !== undefined) {
        humanRequest.delivery = delivery as Exclude<HumanMessageRequest["delivery"], undefined>;
      }
      await sendHumanMessage(record, humanRequest);
      sendJson(response, 202, publicSession(record));
      return;
    }
    const id = sessionIdFromPath(url.pathname);
    if (id !== undefined && request.method === "GET") {
      const record = sessions.get(id);
      if (!record) throw new HttpError(404, "Session not found.");
      sendJson(response, 200, publicSession(record));
      return;
    }
    if (id !== undefined && request.method === "DELETE") {
      const record = sessions.get(id);
      if (!record) throw new HttpError(404, "Session not found.");
      await cancelSession(record);
      sendJson(response, 200, publicSession(record));
      return;
    }
    throw new HttpError(404, "Route not found.");
  } catch (error) {
    const status = error instanceof HttpError ? error.status : 500;
    const message = error instanceof Error ? error.message : String(error);
    sendJson(response, status, { error: message });
  }
}

export async function startServer(): Promise<void> {
  await Promise.all([
    mkdir(config.stateRoot, { recursive: true }),
    mkdir(config.agentDir, { recursive: true }),
  ]);
  await loadPersistedSessions();

  const server = createServer((request, response) => {
    void handleRequest(request, response);
  });
  server.listen(config.port, config.host, () => {
    process.stdout.write(`pi-agent runtime listening on ${config.host}:${config.port}\n`);
  });

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      server.close(() => {
        void flushPersistence().finally(() => process.exit(0));
      });
      setTimeout(() => process.exit(1), 10_000).unref();
    });
  }
}

const entrypoint = process.argv[1];
if (entrypoint !== undefined && import.meta.url === pathToFileURL(entrypoint).href) {
  await startServer();
}
