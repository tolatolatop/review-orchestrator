import { randomUUID } from "node:crypto";
import {
  chmod,
  mkdir,
  readFile,
  readdir,
  realpath,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

import { VERSION as PI_VERSION } from "@earendil-works/pi-coding-agent";

import {
  AgentConfigurationError,
  resolveDomainPreset,
  validateAgentInput,
} from "./agent/registry.js";
import { AgentRunner } from "./agent/runner.js";
import { restoreWorkspaceOwnership } from "./agent/environment.js";
import type {
  AgentDefinition,
  AgentExecutionLimits,
  AgentToolContext,
  DomainPresetOverrides,
  DomainPresetResourceReference,
  JsonObject,
  LegacySessionKind,
  RuntimeEvent,
  RuntimeTool,
  SessionKind,
  SessionRecord,
  ThinkingLevel,
} from "./agent/types.js";
import {
  codeReviewAgent,
  createBuiltinAgentRegistry,
  prAssistantAgent,
} from "./agents/index.js";
import { createCompletionTool, createDefaultToolRegistry } from "./tools/index.js";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MAX_REQUEST_BYTES = 1_000_000;
const MAX_EVENTS = 200;
const AGENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const COMMIT_PATTERN = /^[0-9a-f]{7,64}$/i;
const THINKING_LEVELS = new Set(["minimal", "low", "medium", "high", "xhigh"]);
const PRESET_LIMIT_MAXIMUMS = {
  maxTurns: 1_000,
  maxToolCalls: 10_000,
  maxResultBytes: 10_000_000,
} as const;

interface StartSessionRequest {
  kind: SessionKind;
  agent_id: string;
  task_type: string;
  repository_skills: string[];
  preset_resource?: DomainPresetResourceReference;
  preset_overrides?: DomainPresetOverrides;
  idempotency_key?: string;
  title?: string;
  workspace_path: string;
  input: JsonObject;
}

interface PublicSession {
  id: string;
  kind: SessionKind;
  agent_id: string;
  agent_version: string;
  idempotency_key?: string;
  title: string;
  status: SessionRecord["status"];
  stage: string;
  workspace_path: string;
  provider: string;
  model: string;
  thinking_level: ThinkingLevel;
  skills: string[];
  skill_digests: Record<string, string>;
  profile: string;
  tools: string[];
  execution_limits: SessionRecord["execution_limits"];
  execution_counters: SessionRecord["execution_counters"];
  resolved_preset?: SessionRecord["resolved_preset"];
  execution_environment?: SessionRecord["execution_environment"];
  result?: JsonObject;
  session_archive?: JsonObject;
  error?: string;
  session_file?: string;
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
  environmentRoot: resolve(
    process.env.PI_AGENT_TASK_ENVIRONMENT_ROOT ?? "/var/lib/pi-agent-task",
  ),
  workspaceRoot: resolve(process.env.PI_AGENT_WORKSPACE_ROOT ?? "/workspaces"),
  skillsRoot: resolve(process.env.PI_AGENT_SKILLS_ROOT ?? "/opt/pi-agent/skills"),
  agentDir: resolve(process.env.PI_CODING_AGENT_DIR ?? "/var/lib/pi-agent/config"),
  modelsFile: resolve(process.env.PI_AGENT_MODELS_FILE ?? "/etc/pi-agent/models.json"),
  environmentTemplateRoot: process.env.PI_AGENT_ENVIRONMENT_TEMPLATE_ROOT === undefined
    ? undefined
    : resolve(process.env.PI_AGENT_ENVIRONMENT_TEMPLATE_ROOT),
  taskUidMin: parsePositiveInt(process.env.PI_AGENT_TASK_UID_MIN, 20_000),
  taskUidMax: parsePositiveInt(process.env.PI_AGENT_TASK_UID_MAX, 60_000),
  taskGid: parsePositiveInt(process.env.PI_AGENT_TASK_GID, 65532),
  workspaceOwnerUid: parsePositiveInt(process.env.PI_AGENT_WORKSPACE_OWNER_UID, 1000),
  workspaceOwnerGid: parsePositiveInt(process.env.PI_AGENT_WORKSPACE_OWNER_GID, 1000),
  serviceToken: process.env.PI_AGENT_RUNTIME_TOKEN,
  defaultProvider: process.env.PI_AGENT_PROVIDER ?? "openai",
  defaultModel: process.env.PI_AGENT_MODEL ?? "gpt-5.4",
  defaultThinking: normalizeThinking(process.env.PI_AGENT_THINKING_LEVEL ?? "high"),
  defaultSkill: process.env.PI_AGENT_REVIEW_SKILL ?? "code-review",
  defaultInstructionSkill: process.env.PI_AGENT_COMMAND_SKILL ?? "pr-assistant",
  modelBaseUrl: process.env.PI_AGENT_MODEL_BASE_URL,
};

const agentRegistry = createBuiltinAgentRegistry();
const toolRegistry = createDefaultToolRegistry();
const sessions = new Map<string, SessionRecord>();
const idempotentSessions = new Map<string, string>();
const persistenceQueues = new Map<string, Promise<void>>();
const runner = new AgentRunner({
  stateRoot: config.stateRoot,
  environmentRoot: config.environmentRoot,
  skillsRoot: config.skillsRoot,
  agentDir: config.agentDir,
  modelsFile: config.modelsFile,
  ...(config.environmentTemplateRoot === undefined
    ? {}
    : { environmentTemplateRoot: config.environmentTemplateRoot }),
  taskUidMin: config.taskUidMin,
  taskUidMax: config.taskUidMax,
  taskGid: config.taskGid,
  workspaceOwnerUid: config.workspaceOwnerUid,
  workspaceOwnerGid: config.workspaceOwnerGid,
  toolRegistry,
  addEvent,
  persist: persistRecord,
});

function parsePositiveInt(value: string | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function normalizeThinking(value: string): ThinkingLevel {
  if (!THINKING_LEVELS.has(value)) throw new Error(`Unsupported thinking level: ${value}`);
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
    agent_id: record.agent_id,
    agent_version: record.agent_version,
    title: record.title,
    status: record.status,
    stage: record.stage,
    workspace_path: record.workspace_path,
    provider: record.provider,
    model: record.model,
    thinking_level: record.thinking_level,
    skills: record.skills,
    skill_digests: record.skill_digests,
    profile: record.profile,
    tools: record.tools,
    execution_limits: record.execution_limits,
    execution_counters: record.execution_counters,
    created_at: record.created_at,
    updated_at: record.updated_at,
    events: record.events,
  };
  if (record.idempotency_key !== undefined) response.idempotency_key = record.idempotency_key;
  if (record.execution_environment !== undefined) {
    response.execution_environment = record.execution_environment;
  }
  if (record.resolved_preset !== undefined) {
    response.resolved_preset = record.resolved_preset;
  }
  if (record.result !== undefined) response.result = record.result;
  if (record.session_archive !== undefined) response.session_archive = record.session_archive;
  if (record.error !== undefined) response.error = record.error;
  if (record.session_file !== undefined) response.session_file = record.session_file;
  return response;
}

function persistRecord(record: SessionRecord): Promise<void> {
  const snapshot = `${JSON.stringify({
    ...publicSession(record),
    agent_input: record.agent_input,
    repository_context: record.repository_context,
  }, null, 2)}\n`;
  const previous = persistenceQueues.get(record.id) ?? Promise.resolve();
  const pending = previous.catch(() => undefined).then(async () => {
    await writeRecordSnapshot(record.id, snapshot);
  });
  persistenceQueues.set(record.id, pending);
  void pending.finally(() => {
    if (persistenceQueues.get(record.id) === pending) persistenceQueues.delete(record.id);
  }).catch(() => undefined);
  return pending;
}

async function writeRecordSnapshot(id: string, snapshot: string): Promise<void> {
  const dir = join(config.stateRoot, "runtime-sessions");
  await mkdir(dir, { recursive: true });
  const target = join(dir, `${id}.json`);
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, snapshot, { mode: 0o600 });
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
      const value = JSON.parse(await readFile(join(dir, name), "utf8")) as Partial<SessionRecord>;
      if (value.id === undefined || value.workspace_path === undefined) continue;
      const legacyDefinition = value.kind === "instruction" ? prAssistantAgent : codeReviewAgent;
      const repository = value.repository_context ?? legacyRepositoryFromSnapshot(value);
      const record: SessionRecord = {
        id: value.id,
        kind: value.kind ?? legacyDefinition.legacyKind ?? "agent",
        agent_id: value.agent_id ?? legacyDefinition.id,
        agent_version: value.agent_version ?? legacyDefinition.version,
        agent_input: value.agent_input ?? repository as unknown as JsonObject,
        title: value.title ?? value.id,
        status: TERMINAL_STATUSES.has(String(value.status))
          ? value.status as SessionRecord["status"]
          : "failed",
        stage: value.stage ?? "runtime_restarted",
        workspace_path: value.workspace_path,
        repository_context: repository,
        provider: value.provider ?? config.defaultProvider,
        model: value.model ?? config.defaultModel,
        thinking_level: value.thinking_level ?? config.defaultThinking,
        skills: value.skills ?? [legacyDefinition.defaultSkills.primary],
        skill_digests: value.skill_digests ?? {},
        profile: value.profile ?? legacyDefinition.defaultProfile,
        tools: value.tools ?? [],
        execution_limits: value.execution_limits ?? legacyDefinition.limits,
        execution_counters: value.execution_counters ?? { turns: 0, toolCalls: 0 },
        created_at: value.created_at ?? now(),
        updated_at: value.updated_at ?? now(),
        events: value.events ?? [],
      };
      if (value.idempotency_key !== undefined) record.idempotency_key = value.idempotency_key;
      if (value.execution_environment !== undefined) {
        record.execution_environment = value.execution_environment;
      }
      if (value.resolved_preset !== undefined) {
        record.resolved_preset = value.resolved_preset;
      }
      if (value.result !== undefined) record.result = value.result;
      if (value.session_archive !== undefined) record.session_archive = value.session_archive;
      if (value.error !== undefined) record.error = value.error;
      if (value.session_file !== undefined) record.session_file = value.session_file;
      if (!TERMINAL_STATUSES.has(String(value.status))) {
        record.stage = "runtime_restarted";
        record.error = "The pi-agent runtime restarted while this session was active.";
        record.updated_at = now();
        await restoreWorkspaceOwnership(
          record.workspace_path,
          config.workspaceOwnerUid,
          config.workspaceOwnerGid,
        ).catch(() => undefined);
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

function legacyRepositoryFromSnapshot(value: Partial<SessionRecord>): SessionRecord["repository_context"] {
  const legacy = value as Partial<SessionRecord> & {
    review?: SessionRecord["repository_context"];
  };
  return legacy.review ?? {
    provider: "unknown",
    repo_full_name: "unknown/unknown",
    pr_number: 1,
    base_sha: "0000000",
    head_sha: "0000000",
  };
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

function validateRepositoryContext(value: unknown, name: string): JsonObject {
  assertObject(value, name);
  const context: JsonObject = {
    provider: requireString(value.provider, `${name}.provider`),
    repo_full_name: requireString(value.repo_full_name, `${name}.repo_full_name`),
    pr_number: Number(value.pr_number),
    base_sha: requireString(value.base_sha, `${name}.base_sha`),
    head_sha: requireString(value.head_sha, `${name}.head_sha`),
  };
  if (!Number.isInteger(context.pr_number) || Number(context.pr_number) <= 0) {
    throw new HttpError(422, `${name}.pr_number must be a positive integer.`);
  }
  if (!COMMIT_PATTERN.test(String(context.base_sha)) || !COMMIT_PATTERN.test(String(context.head_sha))) {
    throw new HttpError(422, `${name} base_sha and head_sha must be commit hashes.`);
  }
  return context;
}

function validateLegacyInstruction(value: unknown): JsonObject {
  assertObject(value, "instruction");
  const text = requireString(value.text, "instruction.text");
  if (text.length > 8000) throw new HttpError(422, "instruction.text is too long.");
  const authorLogin = requireString(value.author_login, "instruction.author_login");
  const historyValue = value.history ?? [];
  if (!Array.isArray(historyValue)) throw new HttpError(422, "instruction.history must be an array.");
  if (historyValue.length > 6) {
    throw new HttpError(422, "instruction.history may contain at most 6 turns.");
  }
  const history = historyValue.map((item, index) => {
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
      outcome,
      head_sha: headSha,
    };
  });
  const instruction: JsonObject = { text, author_login: authorLogin, history };
  if (value.source_url !== undefined) {
    instruction.source_url = requireString(value.source_url, "instruction.source_url");
  }
  return instruction;
}

export function validateStartRequest(value: unknown): StartSessionRequest {
  assertObject(value, "request");
  const workspacePath = requireString(value.workspace_path, "workspace_path");
  for (const field of ["agent_version", "model", "profile", "skills"]) {
    if (value[field] !== undefined) {
      throw new HttpError(
        422,
        `${field} is not a Runtime request override; use a domain preset.`,
      );
    }
  }
  let definition: AgentDefinition;
  let input: JsonObject;
  let kind: SessionKind;
  let taskType: string;
  let repositorySkills: string[];
  const presetResource = value.preset_resource === undefined
    ? undefined
    : validatePresetResource(value.preset_resource);
  const presetOverrides = value.preset_overrides === undefined
    ? undefined
    : validatePresetOverrides(value.preset_overrides);

  try {
    if (value.agent_id !== undefined) {
      const agentId = requireString(value.agent_id, "agent_id");
      if (!AGENT_ID_PATTERN.test(agentId)) throw new HttpError(422, `Invalid agent id: ${agentId}`);
      definition = agentRegistry.resolve(agentId);
      taskType = requireString(value.task_type, "task_type");
      if (!AGENT_ID_PATTERN.test(taskType)) {
        throw new HttpError(422, `Invalid task type: ${taskType}`);
      }
      repositorySkills = validateRepositorySkills(value.repository_skills ?? []);
      input = validateAgentInput(definition, value.input);
      if (
        typeof input.workspace_path === "string"
        && input.workspace_path !== workspacePath
      ) {
        throw new HttpError(422, "input.workspace_path must match workspace_path.");
      }
      kind = definition.legacyKind ?? "agent";
    } else {
      const kindValue = value.kind ?? "review";
      if (kindValue !== "review" && kindValue !== "instruction") {
        throw new HttpError(422, "kind must be review or instruction when agent_id is omitted.");
      }
      kind = kindValue;
      definition = agentRegistry.resolveLegacy(kindValue);
      taskType = kind === "review" ? "code-review" : "message-command";
      repositorySkills = [
        kind === "review" ? config.defaultSkill : config.defaultInstructionSkill,
      ];
      if (kind === "review") {
        input = validateRepositoryContext(value.review, "review");
        input.workspace_path = workspacePath;
        if (value.review !== null && typeof value.review === "object" && !Array.isArray(value.review)) {
          const review = value.review as JsonObject;
          if (typeof review.review_mode === "string") input.review_mode = review.review_mode;
          if (
            review.workspace_path !== undefined
            && requireString(review.workspace_path, "review.workspace_path") !== workspacePath
          ) {
            throw new HttpError(422, "review.workspace_path must match workspace_path.");
          }
        }
      } else {
        input = {
          repository_context: validateRepositoryContext(value.repository_context, "repository_context"),
          instruction: validateLegacyInstruction(value.instruction),
        };
      }
      input = validateAgentInput(definition, input);
    }
  } catch (error) {
    if (error instanceof HttpError) throw error;
    if (error instanceof AgentConfigurationError) throw new HttpError(422, error.message);
    throw error;
  }

  const request: StartSessionRequest = {
    kind,
    agent_id: definition.id,
    task_type: taskType,
    repository_skills: repositorySkills,
    ...(presetResource === undefined ? {} : { preset_resource: presetResource }),
    ...(presetOverrides === undefined ? {} : { preset_overrides: presetOverrides }),
    workspace_path: workspacePath,
    input,
  };
  if (value.idempotency_key !== undefined) {
    const key = requireString(value.idempotency_key, "idempotency_key");
    if (key.length > 256) throw new HttpError(422, "idempotency_key is too long.");
    request.idempotency_key = key;
  }
  if (typeof value.title === "string" && value.title.trim() !== "") request.title = value.title.trim();
  return request;
}

function validateRepositorySkills(value: unknown): string[] {
  if (!Array.isArray(value)) {
    throw new HttpError(422, "repository_skills must be an array.");
  }
  return value.map((item, index) => {
    const reference = requireString(item, `repository_skills[${index}]`);
    if (reference.length > 512 || /[\r\n\0]/.test(reference)) {
      throw new HttpError(422, `Invalid Skill reference: ${reference}`);
    }
    return reference;
  });
}

function validatePresetResource(value: unknown): DomainPresetResourceReference {
  assertObject(value, "preset_resource");
  rejectUnknownFields(value, "preset_resource", ["id", "name", "revision"]);
  const revision = Number(value.revision);
  if (!Number.isInteger(revision) || revision <= 0) {
    throw new HttpError(422, "preset_resource.revision must be a positive integer.");
  }
  const id = requireString(value.id, "preset_resource.id");
  const name = requireString(value.name, "preset_resource.name");
  if (id.length > 128 || name.length > 128) {
    throw new HttpError(422, "preset_resource id and name are too long.");
  }
  return {
    id,
    name,
    revision,
  };
}

function validatePresetOverrides(value: unknown): DomainPresetOverrides {
  assertObject(value, "preset_overrides");
  rejectUnknownFields(value, "preset_overrides", ["model", "tools", "limits"]);
  const overrides: DomainPresetOverrides = {};
  if (value.model !== undefined) {
    assertObject(value.model, "preset_overrides.model");
    rejectUnknownFields(value.model, "preset_overrides.model", [
      "provider", "id", "thinking_level",
    ]);
    const model: NonNullable<DomainPresetOverrides["model"]> = {};
    if (value.model.provider !== undefined) {
      model.provider = requireString(value.model.provider, "preset_overrides.model.provider");
    }
    if (value.model.id !== undefined) {
      model.id = requireString(value.model.id, "preset_overrides.model.id");
    }
    if (value.model.thinking_level !== undefined) {
      const thinking = requireString(
        value.model.thinking_level,
        "preset_overrides.model.thinking_level",
      );
      if (!THINKING_LEVELS.has(thinking)) {
        throw new HttpError(422, `Unsupported thinking level: ${thinking}`);
      }
      model.thinking_level = thinking as ThinkingLevel;
    }
    if (Object.keys(model).length === 0) {
      throw new HttpError(422, "preset_overrides.model must not be empty.");
    }
    overrides.model = model;
  }
  if (value.tools !== undefined) {
    if (!Array.isArray(value.tools)) {
      throw new HttpError(422, "preset_overrides.tools must be an array.");
    }
    if (value.tools.length > 64) {
      throw new HttpError(422, "preset_overrides.tools may contain at most 64 items.");
    }
    overrides.tools = value.tools.map((item, index) => {
      const tool = requireString(item, `preset_overrides.tools[${index}]`);
      if (tool.length > 512) {
        throw new HttpError(422, `preset_overrides.tools[${index}] is too long.`);
      }
      return tool;
    });
  }
  if (value.limits !== undefined) {
    assertObject(value.limits, "preset_overrides.limits");
    rejectUnknownFields(value.limits, "preset_overrides.limits", [
      "maxTurns", "maxToolCalls", "maxResultBytes",
    ]);
    const limits: Partial<AgentExecutionLimits> = {};
    for (const field of ["maxTurns", "maxToolCalls", "maxResultBytes"] as const) {
      if (value.limits[field] === undefined) continue;
      const parsed = Number(value.limits[field]);
      if (
        !Number.isInteger(parsed)
        || parsed <= 0
        || parsed > PRESET_LIMIT_MAXIMUMS[field]
      ) {
        throw new HttpError(
          422,
          `preset_overrides.limits.${field} must be an integer between 1 and ${PRESET_LIMIT_MAXIMUMS[field]}.`,
        );
      }
      limits[field] = parsed;
    }
    if (Object.keys(limits).length === 0) {
      throw new HttpError(422, "preset_overrides.limits must not be empty.");
    }
    overrides.limits = limits;
  }
  if (Object.keys(overrides).length === 0) {
    throw new HttpError(422, "preset_overrides must not be empty.");
  }
  return overrides;
}

function rejectUnknownFields(
  value: Record<string, unknown>,
  name: string,
  allowed: string[],
): void {
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).find((field) => !allowedSet.has(field));
  if (unknown !== undefined) {
    throw new HttpError(422, `${name}.${unknown} is not supported.`);
  }
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
  const definition = agentRegistry.resolve(request.agent_id);
  let resolved;
  let resolvedPreset;
  try {
    const resolution = resolveDomainPreset(
      {
        agentId: request.agent_id,
        taskType: request.task_type,
        repositorySkills: request.repository_skills,
        ...(request.preset_resource === undefined
          ? {}
          : { resource: request.preset_resource }),
        ...(request.preset_overrides === undefined
          ? {}
          : { overrides: request.preset_overrides }),
      },
      definition,
      {
        provider: config.defaultProvider,
        model: config.defaultModel,
        thinkingLevel: config.defaultThinking,
        ...(config.modelBaseUrl === undefined ? {} : { modelBaseUrl: config.modelBaseUrl }),
      },
    );
    resolved = resolution.configuration;
    resolvedPreset = resolution.preset;
  } catch (error) {
    if (error instanceof AgentConfigurationError) throw new HttpError(422, error.message);
    throw error;
  }
  const repository = definition.repositoryContext(request.input);
  const timestamp = now();
  const record: SessionRecord = {
    id: randomUUID(),
    kind: request.kind,
    agent_id: definition.id,
    agent_version: definition.version,
    agent_input: request.input,
    title: request.title ?? definition.title(request.input),
    status: "starting",
    stage: "starting",
    workspace_path: workspace,
    repository_context: repository,
    provider: resolved.provider,
    model: resolved.model,
    thinking_level: resolved.thinkingLevel,
    skills: [resolved.skills.primary, ...resolved.skills.supporting],
    skill_digests: {},
    profile: resolved.profileName,
    tools: [],
    execution_limits: resolved.limits,
    execution_counters: { turns: 0, toolCalls: 0 },
    resolved_preset: resolvedPreset,
    created_at: timestamp,
    updated_at: timestamp,
    events: [{ at: timestamp, type: "session_created", stage: "starting" }],
  };
  if (request.idempotency_key !== undefined) record.idempotency_key = request.idempotency_key;
  sessions.set(record.id, record);
  if (record.idempotency_key !== undefined) {
    idempotentSessions.set(record.idempotency_key, record.id);
  }
  await persistRecord(record);
  void runner.run(record, resolved);
  return record;
}

async function cancelSession(record: SessionRecord): Promise<void> {
  if (TERMINAL_STATUSES.has(record.status)) return;
  record.status = "cancelled";
  record.stage = "cancelled";
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
        agent_framework: "1.0.0",
      });
      return;
    }
    authorize(request);
    if (request.method === "POST" && url.pathname === "/v1/sessions") {
      const record = await startSession(validateStartRequest(await readJson(request)));
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
    const status = error instanceof HttpError
      ? error.status
      : error instanceof AgentConfigurationError
        ? 422
        : 500;
    const message = error instanceof Error ? error.message : String(error);
    sendJson(response, status, { error: message });
  }
}

export async function startServer(): Promise<void> {
  await Promise.all([
    mkdir(config.stateRoot, { recursive: true, mode: 0o700 }),
    mkdir(config.agentDir, { recursive: true, mode: 0o700 }),
    mkdir(config.environmentRoot, { recursive: true, mode: 0o755 }),
  ]);
  await Promise.all([
    chmod(config.stateRoot, 0o700),
    chmod(config.agentDir, 0o700),
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

function compatibilityRecord(
  value: Record<string, unknown>,
  definition: AgentDefinition,
): SessionRecord {
  const legacyReview = value.review as SessionRecord["repository_context"] | undefined;
  const repository = (value.repository_context as SessionRecord["repository_context"] | undefined)
    ?? legacyReview
    ?? {
      provider: "unknown",
      repo_full_name: "unknown/unknown",
      pr_number: 1,
      base_sha: "0000000",
      head_sha: "0000000",
    };
  Object.assign(value, {
    kind: definition.legacyKind ?? "agent",
    agent_id: definition.id,
    agent_version: definition.version,
    agent_input: definition === prAssistantAgent
      ? {
        repository_context: repository,
        instruction: value.instruction ?? { text: "", author_login: "unknown", history: [] },
      }
      : repository,
    repository_context: repository,
    skill_digests: value.skill_digests ?? {},
    tools: value.tools ?? [],
    execution_limits: value.execution_limits ?? definition.limits,
    execution_counters: value.execution_counters ?? { turns: 0, toolCalls: 0 },
  });
  return value as unknown as SessionRecord;
}

function createCompatibilityTools(
  value: Record<string, unknown>,
  definition: AgentDefinition,
): RuntimeTool[] {
  const record = compatibilityRecord(value, definition);
  const context: AgentToolContext = {
    record,
    repository: record.repository_context,
    addEvent: (event) => {
      record.events.push({ at: now(), ...event });
    },
    validateOutput: async (output) => {
      await definition.validateOutput?.(output, {
        workspacePath: record.workspace_path,
        repository: record.repository_context,
      });
    },
  };
  const tools = toolRegistry.create(definition.tools, context);
  tools.push(createCompletionTool(definition, context));
  return tools;
}

/** @deprecated Use AgentRegistry and AgentRunner; kept for SDK integration compatibility. */
export function createReviewTools(record: Record<string, unknown>): RuntimeTool[] {
  return createCompatibilityTools(record, codeReviewAgent);
}

/** @deprecated Use AgentRegistry and AgentRunner; kept for SDK integration compatibility. */
export function createInstructionTools(record: Record<string, unknown>): RuntimeTool[] {
  return createCompatibilityTools(record, prAssistantAgent);
}

export { agentRegistry, codeReviewAgent, prAssistantAgent, toolRegistry };

const entrypoint = process.argv[1];
if (entrypoint !== undefined && import.meta.url === pathToFileURL(entrypoint).href) {
  await startServer();
}
