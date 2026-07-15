import { mkdir } from "node:fs/promises";
import { join, resolve } from "node:path";

import type { Api, Model } from "@earendil-works/pi-ai";
import {
  AuthStorage,
  createAgentSession,
  DefaultResourceLoader,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";

import { createCompletionTool, createDefaultToolRegistry, type ToolRegistry } from "../tools/index.js";
import {
  type ExecutionEnvironmentProvider,
  type PreparedExecutionEnvironment,
  TaskOverlayExecutionEnvironmentProvider,
} from "./environment.js";
import { consumeToolCall, consumeTurn } from "./limits.js";
import { composeSkillPrompt, loadAgentSkills } from "./skills.js";
import type {
  AgentDefinition,
  AgentToolContext,
  JsonObject,
  ResolvedAgentConfiguration,
  RuntimeEvent,
  RuntimeTool,
  SessionRecord,
} from "./types.js";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

export interface AgentRunnerOptions {
  stateRoot: string;
  skillsRoot: string;
  agentDir: string;
  modelsFile: string;
  environmentTemplateRoot?: string;
  environmentRoot?: string;
  taskUidMin?: number;
  taskUidMax?: number;
  taskGid?: number;
  workspaceOwnerUid?: number;
  workspaceOwnerGid?: number;
  environmentProvider?: ExecutionEnvironmentProvider;
  toolRegistry?: ToolRegistry;
  addEvent(record: SessionRecord, event: Omit<RuntimeEvent, "at">): void;
  persist(record: SessionRecord): Promise<void>;
}

export class AgentRunner {
  readonly #options: AgentRunnerOptions;
  readonly #tools: ToolRegistry;
  readonly #environmentProvider: ExecutionEnvironmentProvider;

  constructor(options: AgentRunnerOptions) {
    this.#options = options;
    this.#tools = options.toolRegistry ?? createDefaultToolRegistry();
    this.#environmentProvider = options.environmentProvider
      ?? new TaskOverlayExecutionEnvironmentProvider({
        stateRoot: options.environmentRoot ?? options.stateRoot,
        skillsRoot: options.skillsRoot,
        ...(options.environmentTemplateRoot === undefined
          ? {}
          : { templateRoot: options.environmentTemplateRoot }),
        ...(options.taskUidMin === undefined
          ? {}
          : { taskUidMin: options.taskUidMin }),
        ...(options.taskUidMax === undefined
          ? {}
          : { taskUidMax: options.taskUidMax }),
        ...(options.taskGid === undefined ? {} : { taskGid: options.taskGid }),
        ...(options.workspaceOwnerUid === undefined
          ? {}
          : { workspaceOwnerUid: options.workspaceOwnerUid }),
        ...(options.workspaceOwnerGid === undefined
          ? {}
          : { workspaceOwnerGid: options.workspaceOwnerGid }),
      });
  }

  createTools(
    record: SessionRecord,
    configuration: ResolvedAgentConfiguration,
    environment?: PreparedExecutionEnvironment,
  ): RuntimeTool[] {
    const definition = configuration.definition;
    const context: AgentToolContext = {
      record,
      repository: record.repository_context,
      ...(environment === undefined ? {} : { processEnv: environment.processEnv }),
      ...(environment?.processUid === undefined
        ? {}
        : { processUid: environment.processUid }),
      ...(environment?.processGid === undefined
        ? {}
        : { processGid: environment.processGid }),
      addEvent: (event) => this.#options.addEvent(record, event),
      validateOutput: async (output: JsonObject) => {
        await definition.validateOutput?.(output, {
          workspacePath: record.workspace_path,
          repository: record.repository_context,
        });
      },
    };
    const tools = this.#tools.create(configuration.tools, context);
    tools.push(createCompletionTool(definition, context));
    const names = tools.map((tool) => tool.name);
    if (new Set(names).size !== names.length) {
      throw new Error(`Agent ${definition.id} resolves duplicate runtime tool names.`);
    }
    return tools;
  }

  async run(
    record: SessionRecord,
    configuration: ResolvedAgentConfiguration,
  ): Promise<void> {
    let environment: PreparedExecutionEnvironment | undefined;
    try {
      record.status = "preparing";
      record.stage = "preparing";
      this.#options.addEvent(record, { type: "agent_preparing", stage: record.stage });

      const selection = {
        primary: configuration.skills.primary,
        supporting: [...configuration.skills.supporting],
      };
      environment = await this.#environmentProvider.prepare(record, selection);
      record.execution_environment = {
        mode: environment.mode,
        root: environment.root,
        template: environment.template,
        ...(environment.processUid === undefined
          ? {}
          : { task_uid: environment.processUid }),
        credential_separation:
          environment.processUid !== undefined
          && environment.processUid !== process.getuid?.(),
      };
      const loadedSkills = await loadAgentSkills(
        this.#options.skillsRoot,
        selection,
        environment.skillPaths,
      );
      record.skills = loadedSkills.map((skill) => skill.name);
      record.skill_digests = Object.fromEntries(
        loadedSkills.map((skill) => [skill.name, skill.digest]),
      );
      if (record.resolved_preset !== undefined) {
        record.resolved_preset.skill_digests = { ...record.skill_digests };
        record.resolved_preset.environment.template = environment.template;
      }
      const skillPaths = loadedSkills.map((skill) => skill.path);

      const authStorage = createAuthStorage(this.#options.agentDir, record.provider);
      const modelRegistry = ModelRegistry.create(authStorage, this.#options.modelsFile);
      const selectedModel = resolveModel(
        modelRegistry,
        record.provider,
        record.model,
        configuration.baseUrl,
      );
      const settingsManager = SettingsManager.inMemory({
        compaction: { enabled: true },
        retry: { enabled: true, maxRetries: 2 },
      });
      const completionTool = configuration.definition.result.toolName;
      const loader = new DefaultResourceLoader({
        cwd: record.workspace_path,
        agentDir: this.#options.agentDir,
        settingsManager,
        additionalSkillPaths: skillPaths,
        noExtensions: true,
        noPromptTemplates: true,
        noThemes: true,
        noContextFiles: true,
        skillsOverride: (base) => ({
          skills: base.skills.filter((skill) =>
            skillPaths.some((skillPath) => resolve(skill.filePath) === resolve(skillPath))
          ),
          diagnostics: base.diagnostics,
        }),
        appendSystemPrompt: [
          configuration.definition.systemPrompt,
          composeSkillPrompt(loadedSkills),
          `The ${completionTool} tool is the only valid final output channel.`,
          `Execution limits: ${record.execution_limits.maxTurns} turns, ${record.execution_limits.maxToolCalls} tool calls.`,
        ],
      });
      await loader.reload();
      const discovered = new Set(loader.getSkills().skills.map((skill) => skill.name));
      for (const skill of loadedSkills) {
        if (!discovered.has(skill.name)) {
          throw new Error(`pi-agent did not load configured skill: ${skill.name}`);
        }
      }

      const sessionDirectory = join(this.#options.stateRoot, "pi-sessions", record.id);
      await mkdir(sessionDirectory, { recursive: true });
      const sessionManager = SessionManager.create(record.workspace_path, sessionDirectory);
      const customTools = this.createTools(record, configuration, environment);
      const activeTools = customTools.map((tool) => tool.name);
      record.tools = activeTools;
      const { session } = await createAgentSession({
        cwd: record.workspace_path,
        agentDir: this.#options.agentDir,
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
          record.stage = "running";
        } else if (event.type === "turn_start") {
          const exceeded = consumeTurn(record);
          if (exceeded !== undefined) {
            this.#failForLimit(record, exceeded.dimension, exceeded.limit);
            return;
          }
          record.stage = "thinking";
        } else if (event.type === "tool_execution_start") {
          const toolName = typeof raw.toolName === "string" ? raw.toolName : "tool";
          const exceeded = consumeToolCall(record);
          if (exceeded !== undefined) {
            this.#failForLimit(record, exceeded.dimension, exceeded.limit);
            return;
          }
          record.stage = `tool:${toolName}`;
          this.#options.addEvent(record, {
            type: event.type,
            stage: record.stage,
            tool: toolName,
          });
          return;
        } else if (event.type === "tool_execution_end" && record.status !== "waiting_for_input") {
          if (!TERMINAL_STATUSES.has(record.status)) record.stage = "running";
        } else if (event.type === "auto_retry_start") {
          record.stage = "model_retry";
        }
        if (
          [
            "agent_start",
            "turn_start",
            "turn_end",
            "agent_end",
            "auto_retry_start",
            "auto_retry_end",
          ].includes(event.type)
        ) {
          this.#options.addEvent(record, { type: event.type, stage: record.stage });
        }
      });
      record.status = "running";
      record.stage = "running";
      this.#options.addEvent(record, { type: "runtime_ready", stage: record.stage });

      const prompt = configuration.definition.buildPrompt({
        input: record.agent_input,
        repository: record.repository_context,
        profile: record.profile,
        skills: selection,
      });
      await session.prompt(prompt, { source: "rpc" });

      finalizeAgentRun(record, completionTool, (event) => {
        this.#options.addEvent(record, event);
      });
    } catch (error) {
      if (!TERMINAL_STATUSES.has(record.status)) {
        record.status = "failed";
        record.stage = "failed";
        record.error = error instanceof Error ? error.message : String(error);
        this.#options.addEvent(record, { type: "session_failed", stage: record.stage });
      }
    } finally {
      if (record.session !== undefined) {
        record.session_archive = buildSessionArchive(record.session);
      }
      record.updated_at = new Date().toISOString();
      try {
        await this.#options.persist(record);
      } finally {
        if (environment !== undefined) {
          await this.#environmentProvider.dispose(environment).catch(() => undefined);
        }
      }
    }
  }

  #failForLimit(record: SessionRecord, dimension: string, limit: number): void {
    if (TERMINAL_STATUSES.has(record.status)) return;
    record.status = "failed";
    record.stage = "execution_limit_exceeded";
    record.error = `Agent ${dimension} limit exceeded (${limit}).`;
    this.#options.addEvent(record, {
      type: "execution_limit_exceeded",
      stage: record.stage,
    });
    void record.session?.abort();
  }
}

export function buildSessionArchive(session: SessionRecord["session"]): JsonObject {
  if (session === undefined) return {};
  return JSON.parse(JSON.stringify({
    header: session.sessionManager.getHeader(),
    entries: session.sessionManager.getEntries(),
    branch: session.sessionManager.getBranch(),
    context: session.sessionManager.buildSessionContext(),
    stats: session.getSessionStats(),
  })) as JsonObject;
}

export function finalizeAgentRun(
  record: SessionRecord,
  completionTool: string,
  addEvent: (event: Omit<RuntimeEvent, "at">) => void,
): void {
  if (TERMINAL_STATUSES.has(record.status)) return;
  record.status = "failed";
  record.stage = "missing_result";
  record.error = `Agent ${record.agent_id} ended without calling ${completionTool}.`;
  addEvent({ type: "session_failed", stage: record.stage });
}

function createAuthStorage(agentDir: string, provider: string): AuthStorage {
  const authStorage = AuthStorage.create(join(agentDir, "auth.json"));
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
    throw new Error(
      `Unknown pi model: ${provider}/${modelId}. Configure it in models.json if it is custom.`,
    );
  }
  return baseUrl ? { ...selected, baseUrl } : selected;
}
