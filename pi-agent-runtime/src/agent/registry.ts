import { createHash } from "node:crypto";

import { Value } from "typebox/value";

import type {
  AgentDefinition,
  DomainPresetSelection,
  DomainPresetOverrides,
  AgentExecutionLimits,
  AgentInvocation,
  JsonObject,
  ResolvedAgentConfiguration,
  ResolvedDomainPreset,
  RuntimeDefaults,
  SkillSelection,
  ThinkingLevel,
} from "./types.js";

const AGENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const VERSION_PATTERN = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;

export class AgentConfigurationError extends Error {}

export class AgentRegistry {
  readonly #definitions = new Map<string, AgentDefinition>();

  register(definition: AgentDefinition): void {
    validateDefinition(definition);
    if (this.#definitions.has(definition.id)) {
      throw new AgentConfigurationError(`Agent is already registered: ${definition.id}`);
    }
    this.#definitions.set(definition.id, definition);
  }

  resolve(id: string): AgentDefinition {
    const definition = this.#definitions.get(id);
    if (definition === undefined) throw new AgentConfigurationError(`Unknown agent: ${id}`);
    return definition;
  }

  resolveLegacy(kind: "review" | "instruction"): AgentDefinition {
    const matches = this.list().filter((definition) => definition.legacyKind === kind);
    if (matches.length === 0) {
      throw new AgentConfigurationError(`No agent is registered for legacy kind: ${kind}`);
    }
    return matches[0]!;
  }

  list(): AgentDefinition[] {
    return [...this.#definitions.values()];
  }
}

export function resolveDomainPreset(
  selection: DomainPresetSelection,
  definition: AgentDefinition,
  defaults: RuntimeDefaults,
): { configuration: ResolvedAgentConfiguration; preset: ResolvedDomainPreset } {
  if (selection.agentId !== definition.id) {
    throw new AgentConfigurationError(
      `Preset agent ${selection.agentId} does not match ${definition.id}.`,
    );
  }
  const profile = definition.taskTypeProfiles[selection.taskType];
  if (profile === undefined) {
    throw new AgentConfigurationError(
      `Agent ${definition.id} does not support task type ${selection.taskType}.`,
    );
  }
  let configuration = resolveAgentConfiguration(
    {
      definition,
      input: {},
      profile,
      ...(selection.repositorySkills.length === 0
        ? {}
        : { skills: selection.repositorySkills }),
    },
    defaults,
  );
  configuration = applyDomainPresetOverrides(
    configuration,
    selection.overrides,
  );
  const skillReferences = [
    configuration.skills.primary,
    ...configuration.skills.supporting,
  ];
  return {
    configuration,
    preset: {
      schema_version: "1",
      ...(selection.resource === undefined
        ? {}
        : { resource: { ...selection.resource } }),
      composition: {
        agent: {
          id: definition.id,
          version: definition.version,
          digest: agentDefinitionDigest(definition),
        },
        repository: { skills: [...selection.repositorySkills] },
        task_type: { id: selection.taskType, profile },
      },
      model: {
        provider: configuration.provider,
        id: configuration.model,
        thinking_level: configuration.thinkingLevel,
      },
      skills: skillReferences,
      skill_digests: {},
      tools: [...configuration.tools, definition.result.toolName],
      limits: { ...configuration.limits },
      environment: { mode: "task-overlay", template: "runtime-default" },
    },
  };
}

function applyDomainPresetOverrides(
  configuration: ResolvedAgentConfiguration,
  overrides: DomainPresetOverrides | undefined,
): ResolvedAgentConfiguration {
  if (overrides === undefined) return configuration;
  const definition = configuration.definition;
  const provider = overrides.model?.provider ?? configuration.provider;
  const model = overrides.model?.id ?? configuration.model;
  const thinkingLevel = overrides.model?.thinking_level
    ?? configuration.thinkingLevel;
  validateModelSelection(definition, provider, model, thinkingLevel);

  const tools = overrides.tools === undefined
    ? [...configuration.tools]
    : [...overrides.tools];
  if (tools.some((tool) => !definition.tools.includes(tool))) {
    throw new AgentConfigurationError(
      `Preset cannot add tools outside the agent ${definition.id} allow-list.`,
    );
  }
  if (new Set(tools).size !== tools.length) {
    throw new AgentConfigurationError("Preset contains duplicate tools.");
  }

  return {
    ...configuration,
    provider,
    model,
    thinkingLevel,
    tools,
    limits: mergeLimits(configuration.limits, overrides.limits),
  };
}

function agentDefinitionDigest(definition: AgentDefinition): string {
  return createHash("sha256").update(JSON.stringify({
    id: definition.id,
    version: definition.version,
    inputSchema: definition.inputSchema,
    result: definition.result,
    defaultSkills: definition.defaultSkills,
    allowRepositorySkills: definition.allowRepositorySkills,
    tools: definition.tools,
    profiles: definition.profiles,
    taskTypeProfiles: definition.taskTypeProfiles,
    modelPolicy: definition.modelPolicy,
    limits: definition.limits,
    systemPrompt: definition.systemPrompt,
  })).digest("hex");
}

export function validateAgentInput(definition: AgentDefinition, value: unknown): JsonObject {
  if (!Value.Check(definition.inputSchema, value)) {
    const issue = Value.Errors(definition.inputSchema, value)[0];
    const suffix = issue === undefined ? "" : ` at ${issue.instancePath || "/"}: ${issue.message}`;
    throw new AgentConfigurationError(`Invalid input for agent ${definition.id}${suffix}`);
  }
  return value as JsonObject;
}

export function validateAgentOutput(definition: AgentDefinition, value: unknown): JsonObject {
  if (!Value.Check(definition.result.schema, value)) {
    const issue = Value.Errors(definition.result.schema, value)[0];
    const suffix = issue === undefined ? "" : ` at ${issue.instancePath || "/"}: ${issue.message}`;
    throw new AgentConfigurationError(`Invalid result for agent ${definition.id}${suffix}`);
  }
  return value as JsonObject;
}

export function resolveAgentConfiguration(
  invocation: AgentInvocation,
  defaults: RuntimeDefaults,
): ResolvedAgentConfiguration {
  const definition = invocation.definition;
  const profileName = invocation.profile ?? definition.defaultProfile;
  const profile = definition.profiles[profileName];
  if (profile === undefined) {
    throw new AgentConfigurationError(
      `Unknown profile for agent ${definition.id}: ${profileName}`,
    );
  }

  let skills: SkillSelection = {
    primary: definition.defaultSkills.primary,
    supporting: [...definition.defaultSkills.supporting],
  };
  if (invocation.skills !== undefined) {
    if (!definition.allowRepositorySkills) {
      throw new AgentConfigurationError(
        `Agent ${definition.id} does not accept Repository Skills.`,
      );
    }
    if (invocation.skills.length === 0) {
      throw new AgentConfigurationError("At least one skill is required when overriding skills.");
    }
    skills = {
      primary: invocation.skills[0]!,
      supporting: invocation.skills.slice(1),
    };
  }
  if (profile.primarySkill !== undefined) skills.primary = profile.primarySkill;
  if (profile.supportingSkills !== undefined) skills.supporting = [...profile.supportingSkills];
  validateSkillSelection(skills);

  // Deployment forms the Agent base; the Task Type's named preset has final
  // ownership of the fields it explicitly declares.
  const provider = profile.provider ?? defaults.provider;
  const model = profile.model ?? defaults.model;
  const thinkingLevel = profile.thinkingLevel
    ?? normalizeThinking(defaults.thinkingLevel);
  const baseUrl = defaults.modelBaseUrl;
  validateModelSelection(definition, provider, model, thinkingLevel);

  const limits = mergeLimits(definition.limits, profile.limits);
  const tools = profile.tools === undefined ? [...definition.tools] : [...profile.tools];
  if (tools.some((tool) => !definition.tools.includes(tool))) {
    throw new AgentConfigurationError(
      `Agent ${definition.id} profile ${profileName} cannot add tools outside the agent allow-list.`,
    );
  }
  if (new Set(tools).size !== tools.length) {
    throw new AgentConfigurationError(`Agent ${definition.id} profile ${profileName} contains duplicate tools.`);
  }

  return {
    definition,
    profileName,
    skills,
    tools,
    provider,
    model,
    thinkingLevel,
    ...(baseUrl === undefined ? {} : { baseUrl }),
    limits,
  };
}

function validateDefinition(definition: AgentDefinition): void {
  if (!AGENT_ID_PATTERN.test(definition.id)) {
    throw new AgentConfigurationError(`Invalid agent id: ${definition.id}`);
  }
  if (!VERSION_PATTERN.test(definition.version)) {
    throw new AgentConfigurationError(`Invalid semantic agent version: ${definition.version}`);
  }
  validateSkillSelection(definition.defaultSkills);
  if (definition.profiles[definition.defaultProfile] === undefined) {
    throw new AgentConfigurationError(
      `Agent ${definition.id} is missing default profile ${definition.defaultProfile}.`,
    );
  }
  for (const [taskType, profile] of Object.entries(definition.taskTypeProfiles)) {
    if (!AGENT_ID_PATTERN.test(taskType) || definition.profiles[profile] === undefined) {
      throw new AgentConfigurationError(
        `Agent ${definition.id} has invalid task preset ${taskType}:${profile}.`,
      );
    }
  }
  if (definition.tools.length === 0) {
    throw new AgentConfigurationError(`Agent ${definition.id} must declare at least one tool.`);
  }
  if (new Set(definition.tools).size !== definition.tools.length) {
    throw new AgentConfigurationError(`Agent ${definition.id} contains duplicate tools.`);
  }
  mergeLimits(definition.limits);
}

function validateSkillSelection(skills: SkillSelection): void {
  const all = [skills.primary, ...skills.supporting];
  for (const skill of all) validateSkillReference(skill);
  if (new Set(all).size !== all.length) {
    throw new AgentConfigurationError("Primary and supporting skills must be unique.");
  }
}

function validateSkillReference(reference: string): void {
  if (reference.length === 0 || reference.length > 512 || /[\r\n\0]/.test(reference)) {
    throw new AgentConfigurationError(`Invalid Skill reference: ${reference}`);
  }
  if (reference.startsWith("npm:")) {
    if (reference.slice(4).trim() === "" || /\s/.test(reference.slice(4))) {
      throw new AgentConfigurationError(`Invalid npm Skill reference: ${reference}`);
    }
    return;
  }
  const name = reference.startsWith("builtin:")
    ? reference.slice("builtin:".length)
    : reference.startsWith("prebuilt:")
      ? reference.slice("prebuilt:".length)
      : reference;
  if (!AGENT_ID_PATTERN.test(name)) {
    throw new AgentConfigurationError(`Invalid Skill reference: ${reference}`);
  }
}

function validateModelSelection(
  definition: AgentDefinition,
  provider: string,
  model: string,
  thinking: ThinkingLevel,
): void {
  const policy = definition.modelPolicy;
  if (policy.allowedProviders !== undefined && !policy.allowedProviders.includes(provider)) {
    throw new AgentConfigurationError(`Provider ${provider} is not allowed by agent ${definition.id}.`);
  }
  if (policy.allowedModels !== undefined && !policy.allowedModels.includes(model)) {
    throw new AgentConfigurationError(`Model ${model} is not allowed by agent ${definition.id}.`);
  }
  if (!policy.allowedThinkingLevels.includes(thinking)) {
    throw new AgentConfigurationError(
      `Thinking level ${thinking} is not allowed by agent ${definition.id}.`,
    );
  }
}

function normalizeThinking(value: string): ThinkingLevel {
  if (!["minimal", "low", "medium", "high", "xhigh"].includes(value)) {
    throw new AgentConfigurationError(`Unsupported thinking level: ${value}`);
  }
  return value as ThinkingLevel;
}

function mergeLimits(
  base: AgentExecutionLimits,
  override: Partial<AgentExecutionLimits> | undefined = undefined,
): AgentExecutionLimits {
  const merged = { ...base, ...override };
  for (const [name, value] of Object.entries(merged)) {
    if (!Number.isInteger(value) || value <= 0) {
      throw new AgentConfigurationError(`Agent execution limit ${name} must be a positive integer.`);
    }
  }
  return merged;
}
