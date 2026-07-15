import { randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, realpath, rename, stat, writeFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

import { VERSION as PI_VERSION } from "@earendil-works/pi-coding-agent";

import {
  AgentConfigurationError,
  resolveAgentConfiguration,
  validateAgentInput,
} from "./agent/registry.js";
import { AgentRunner } from "./agent/runner.js";
import type {
  AgentDefinition,
  AgentInvocation,
  AgentToolContext,
  JsonObject,
  LegacySessionKind,
  ModelSelection,
  ResolvedAgentConfiguration,
  RuntimeEvent,
  RuntimeTool,
  SessionKind,
  SessionRecord,
  ThinkingLevel,
} from "./agent/types.js";
import {
  changeSummaryAgent,
  codeReviewAgent,
  createBuiltinAgentRegistry,
  prAssistantAgent,
} from "./agents/index.js";
import { createCompletionTool, createDefaultToolRegistry } from "./tools/index.js";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
const MAX_REQUEST_BYTES = 1_000_000;
const MAX_EVENTS = 200;
const AGENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const SKILL_NAME_PATTERN = AGENT_ID_PATTERN;
const COMMIT_PATTERN = /^[0-9a-f]{7,64}$/i;
const THINKING_LEVELS = new Set(["minimal", "low", "medium", "high", "xhigh"]);

interface StartSessionRequest {
  kind: SessionKind;
  agent_id: string;
  agent_version?: string;
  idempotency_key?: string;
  title?: string;
  workspace_path: string;
  input: JsonObject;
  model?: ModelSelection;
  skills?: string[];
  profile?: string;
}

interface HumanMessageRequest {
  message: string;
  delivery?: "answer" | "steer" | "follow_up";
}

interface PublicPendingInput {
  id: string;
  question: string;
  choices?: string[];
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
  interaction_policy: SessionRecord["interaction_policy"];
  result?: JsonObject;
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
  modelsFile: resolve(process.env.PI_AGENT_MODELS_FILE ?? "/etc/pi-agent/models.json"),
  serviceToken: process.env.PI_AGENT_RUNTIME_TOKEN,
  defaultProvider: process.env.PI_AGENT_PROVIDER ?? "openai",
  defaultModel: process.env.PI_AGENT_MODEL ?? "gpt-5.4",
  defaultThinking: normalizeThinking(process.env.PI_AGENT_THINKING_LEVEL ?? "high"),
  defaultSkill: process.env.PI_AGENT_REVIEW_SKILL ?? "code-review",
  defaultInstructionSkill: process.env.PI_AGENT_COMMAND_SKILL ?? "pr-assistant",
  defaultProfile: process.env.PI_AGENT_REVIEW_PROFILE ?? "default",
  modelBaseUrl: process.env.PI_AGENT_MODEL_BASE_URL,
};

const agentRegistry = createBuiltinAgentRegistry();
const toolRegistry = createDefaultToolRegistry();
const sessions = new Map<string, SessionRecord>();
const idempotentSessions = new Map<string, string>();
const persistenceQueues = new Map<string, Promise<void>>();
const runner = new AgentRunner({
  stateRoot: config.stateRoot,
  skillsRoot: config.skillsRoot,
  agentDir: config.agentDir,
  modelsFile: config.modelsFile,
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
    interaction_policy: record.interaction_policy,
    created_at: record.created_at,
    updated_at: record.updated_at,
    events: record.events,
  };
  if (record.idempotency_key !== undefined) response.idempotency_key = record.idempotency_key;
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
        interaction_policy: value.interaction_policy ?? legacyDefinition.interactionPolicy,
        created_at: value.created_at ?? now(),
        updated_at: value.updated_at ?? now(),
        events: value.events ?? [],
      };
      if (value.idempotency_key !== undefined) record.idempotency_key = value.idempotency_key;
      if (value.result !== undefined) record.result = value.result;
      if (value.error !== undefined) record.error = value.error;
      if (value.session_file !== undefined) record.session_file = value.session_file;
      if (!TERMINAL_STATUSES.has(String(value.status))) {
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
  let definition: AgentDefinition;
  let input: JsonObject;
  let kind: SessionKind;

  try {
    if (value.agent_id !== undefined) {
      const agentId = requireString(value.agent_id, "agent_id");
      if (!AGENT_ID_PATTERN.test(agentId)) throw new HttpError(422, `Invalid agent id: ${agentId}`);
      const version = value.agent_version === undefined
        ? undefined
        : requireString(value.agent_version, "agent_version");
      definition = agentRegistry.resolve(agentId, version);
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

  let model: ModelSelection | undefined;
  if (value.model !== undefined) {
    assertObject(value.model, "model");
    model = {};
    if (value.model.provider !== undefined) {
      model.provider = requireString(value.model.provider, "model.provider");
    }
    if (value.model.id !== undefined) model.id = requireString(value.model.id, "model.id");
    if (value.model.thinking_level !== undefined) {
      const thinking = requireString(value.model.thinking_level, "model.thinking_level");
      normalizeThinking(thinking);
      model.thinking_level = thinking;
    }
    if (value.model.base_url !== undefined) {
      model.base_url = requireString(value.model.base_url, "model.base_url");
    }
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

  const request: StartSessionRequest = {
    kind,
    agent_id: definition.id,
    agent_version: definition.version,
    workspace_path: workspacePath,
    input,
  };
  if (value.idempotency_key !== undefined) {
    const key = requireString(value.idempotency_key, "idempotency_key");
    if (key.length > 256) throw new HttpError(422, "idempotency_key is too long.");
    request.idempotency_key = key;
  }
  if (typeof value.title === "string" && value.title.trim() !== "") request.title = value.title.trim();
  if (typeof value.profile === "string" && value.profile.trim() !== "") {
    request.profile = value.profile.trim();
  }
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
  const definition = agentRegistry.resolve(request.agent_id, request.agent_version);
  const defaultSkills = request.kind === "review"
    ? [config.defaultSkill]
    : request.kind === "instruction"
      ? [config.defaultInstructionSkill]
      : undefined;
  const invocation: AgentInvocation = {
    definition,
    input: request.input,
    ...(request.profile === undefined
      ? request.kind === "review" ? { profile: config.defaultProfile } : {}
      : { profile: request.profile }),
    ...(request.skills === undefined
      ? defaultSkills === undefined ? {} : { skills: defaultSkills }
      : { skills: request.skills }),
    ...(request.model === undefined ? {} : { model: request.model }),
  };
  let resolved: ResolvedAgentConfiguration;
  try {
    resolved = resolveAgentConfiguration(invocation, {
      provider: config.defaultProvider,
      model: config.defaultModel,
      thinkingLevel: config.defaultThinking,
      ...(config.modelBaseUrl === undefined ? {} : { modelBaseUrl: config.modelBaseUrl }),
    });
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
    interaction_policy: resolved.interactionPolicy,
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

async function sendHumanMessage(record: SessionRecord, request: HumanMessageRequest): Promise<void> {
  if (TERMINAL_STATUSES.has(record.status)) {
    throw new HttpError(409, `Session is already ${record.status}.`);
  }
  const message = requireString(request.message, "message");
  const delivery = request.delivery ?? (record.pending ? "answer" : "steer");
  if (record.pending !== undefined) {
    if (delivery !== "answer") throw new HttpError(409, "This session is waiting for an answer.");
    const pending = record.pending;
    delete record.pending;
    record.status = "running";
    record.stage = "running";
    pending.resolve(message);
    await persistRecord(record);
    return;
  }
  if (delivery === "answer") throw new HttpError(409, "The session has no pending human-input request.");
  if (delivery === "steer" && !record.interaction_policy.allowSteer) {
    throw new HttpError(409, `Agent ${record.agent_id} does not allow steering.`);
  }
  if (delivery === "follow_up" && !record.interaction_policy.allowFollowUp) {
    throw new HttpError(409, `Agent ${record.agent_id} does not allow follow-up messages.`);
  }
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

function publicAgent(definition: AgentDefinition) {
  return {
    id: definition.id,
    version: definition.version,
    description: definition.description,
    legacy_kind: definition.legacyKind,
    default_profile: definition.defaultProfile,
    profiles: definition.profiles,
    default_skills: definition.defaultSkills,
    tools: definition.tools,
    model_policy: definition.modelPolicy,
    interaction_policy: definition.interactionPolicy,
    limits: definition.limits,
    result_tool: definition.result.toolName,
    input_schema: definition.inputSchema,
    output_schema: definition.result.schema,
  };
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
    if (request.method === "GET" && url.pathname === "/v1/agents") {
      sendJson(response, 200, {
        items: agentRegistry.list().map(publicAgent),
      });
      return;
    }
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
      const humanRequest: HumanMessageRequest = {
        message: requireString(body.message, "message"),
      };
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
    interaction_policy: value.interaction_policy ?? definition.interactionPolicy,
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

export { agentRegistry, changeSummaryAgent, codeReviewAgent, prAssistantAgent, toolRegistry };

const entrypoint = process.argv[1];
if (entrypoint !== undefined && import.meta.url === pathToFileURL(entrypoint).href) {
  await startServer();
}
