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
  toolRegistry?: ToolRegistry;
  addEvent(record: SessionRecord, event: Omit<RuntimeEvent, "at">): void;
  persist(record: SessionRecord): Promise<void>;
}

export class AgentRunner {
  readonly #options: AgentRunnerOptions;
  readonly #tools: ToolRegistry;

  constructor(options: AgentRunnerOptions) {
    this.#options = options;
    this.#tools = options.toolRegistry ?? createDefaultToolRegistry();
  }

  createTools(
    record: SessionRecord,
    configuration: ResolvedAgentConfiguration,
  ): RuntimeTool[] {
    const definition = configuration.definition;
    const context: AgentToolContext = {
      record,
      repository: record.repository_context,
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
    try {
      record.status = "preparing";
      record.stage = "preparing";
      this.#options.addEvent(record, { type: "agent_preparing", stage: record.stage });

      const selection = {
        primary: configuration.skills.primary,
        supporting: [...configuration.skills.supporting],
      };
      const loadedSkills = await loadAgentSkills(this.#options.skillsRoot, selection);
      record.skill_digests = Object.fromEntries(
        loadedSkills.map((skill) => [skill.name, skill.digest]),
      );
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
      const customTools = this.createTools(record, configuration);
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
      record.updated_at = new Date().toISOString();
      await this.#options.persist(record);
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
