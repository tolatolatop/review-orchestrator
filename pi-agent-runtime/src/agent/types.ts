import type { AgentSession, ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { TSchema } from "typebox";

export type SessionStatus =
  | "starting"
  | "preparing"
  | "running"
  | "waiting_for_input"
  | "validating_result"
  | "completed"
  | "failed"
  | "cancelled";

export type ThinkingLevel = "minimal" | "low" | "medium" | "high" | "xhigh";
export type LegacySessionKind = "review" | "instruction";
export type SessionKind = LegacySessionKind | "agent";
export type JsonObject = Record<string, unknown>;
export type RuntimeTool = ToolDefinition<any, any, any>;

export interface RepositoryContext {
  provider: string;
  repo_full_name: string;
  pr_number: number;
  base_sha: string;
  head_sha: string;
}

export interface SkillSelection {
  primary: string;
  supporting: string[];
}

export interface LoadedSkill {
  name: string;
  path: string;
  digest: string;
  content: string;
  role: "primary" | "supporting";
}

export interface AgentExecutionLimits {
  maxTurns: number;
  maxToolCalls: number;
  maxResultBytes: number;
}

export interface AgentExecutionCounters {
  turns: number;
  toolCalls: number;
}

export interface AgentModelPolicy {
  allowedThinkingLevels: ThinkingLevel[];
  allowedProviders?: string[];
  allowedModels?: string[];
}

export interface AgentProfile {
  description: string;
  primarySkill?: string;
  supportingSkills?: string[];
  tools?: string[];
  provider?: string;
  model?: string;
  thinkingLevel?: ThinkingLevel;
  limits?: Partial<AgentExecutionLimits>;
}

export interface AgentResultDefinition {
  toolName: string;
  label: string;
  description: string;
  promptSnippet: string;
  successMessage: string;
  eventType: string;
  schema: TSchema;
}

export interface AgentBuildContext {
  input: JsonObject;
  repository: RepositoryContext;
  profile: string;
  skills: SkillSelection;
}

export interface AgentOutputValidationContext {
  workspacePath: string;
  repository: RepositoryContext;
}

export interface AgentDefinition {
  id: string;
  version: string;
  description: string;
  legacyKind?: LegacySessionKind;
  inputSchema: TSchema;
  result: AgentResultDefinition;
  defaultSkills: SkillSelection;
  allowRepositorySkills: boolean;
  tools: string[];
  profiles: Record<string, AgentProfile>;
  taskTypeProfiles: Record<string, string>;
  defaultProfile: string;
  modelPolicy: AgentModelPolicy;
  limits: AgentExecutionLimits;
  systemPrompt: string;
  title(input: JsonObject): string;
  repositoryContext(input: JsonObject): RepositoryContext;
  buildPrompt(context: AgentBuildContext): string;
  validateOutput?(
    output: JsonObject,
    context: AgentOutputValidationContext,
  ): Promise<void>;
}

export interface ResolvedAgentConfiguration {
  definition: AgentDefinition;
  profileName: string;
  skills: SkillSelection;
  tools: string[];
  provider: string;
  model: string;
  thinkingLevel: ThinkingLevel;
  baseUrl?: string;
  limits: AgentExecutionLimits;
}

export interface DomainPresetSelection {
  agentId: string;
  taskType: string;
  repositorySkills: string[];
  resource?: DomainPresetResourceReference;
  overrides?: DomainPresetOverrides;
}

export interface DomainPresetResourceReference {
  id: string;
  name: string;
  revision: number;
}

export interface DomainPresetOverrides {
  model?: {
    provider?: string;
    id?: string;
    thinking_level?: ThinkingLevel;
  };
  tools?: string[];
  limits?: Partial<AgentExecutionLimits>;
}

export interface ResolvedDomainPreset {
  schema_version: "1";
  resource?: DomainPresetResourceReference;
  composition: {
    agent: { id: string; version: string; digest: string };
    repository: { skills: string[] };
    task_type: { id: string; profile: string };
  };
  model: {
    provider: string;
    id: string;
    thinking_level: ThinkingLevel;
  };
  skills: string[];
  skill_digests: Record<string, string>;
  tools: string[];
  limits: AgentExecutionLimits;
  environment: { mode: "task-overlay"; template: string };
}

export interface RuntimeEvent {
  at: string;
  type: string;
  stage: string;
  tool?: string;
}

export interface SessionRecord {
  id: string;
  kind: SessionKind;
  agent_id: string;
  agent_version: string;
  agent_input: JsonObject;
  idempotency_key?: string;
  title: string;
  status: SessionStatus;
  stage: string;
  workspace_path: string;
  repository_context: RepositoryContext;
  provider: string;
  model: string;
  thinking_level: ThinkingLevel;
  skills: string[];
  skill_digests: Record<string, string>;
  profile: string;
  tools: string[];
  execution_limits: AgentExecutionLimits;
  execution_counters: AgentExecutionCounters;
  resolved_preset?: ResolvedDomainPreset;
  execution_environment?: {
    mode: "task-overlay";
    root: string;
    template: string;
    task_uid?: number;
    credential_separation: boolean;
  };
  result?: JsonObject;
  session_archive?: JsonObject;
  error?: string;
  session_file?: string;
  created_at: string;
  updated_at: string;
  events: RuntimeEvent[];
  session?: AgentSession;
}

export interface AgentToolContext {
  record: SessionRecord;
  repository: RepositoryContext;
  processEnv?: Record<string, string>;
  processUid?: number;
  processGid?: number;
  addEvent(event: Omit<RuntimeEvent, "at">): void;
  validateOutput(output: JsonObject): Promise<void>;
}

export type AgentToolFactory = (context: AgentToolContext) => RuntimeTool;

export interface RuntimeDefaults {
  provider: string;
  model: string;
  thinkingLevel: ThinkingLevel;
  modelBaseUrl?: string;
}

export interface AgentInvocation {
  definition: AgentDefinition;
  input: JsonObject;
  profile?: string;
  skills?: string[];
}
