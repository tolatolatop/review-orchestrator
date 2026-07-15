import { Value } from "typebox/value";

import type {
  AgentDefinition,
  AgentExecutionLimits,
  AgentInvocation,
  JsonObject,
  ResolvedAgentConfiguration,
  RuntimeDefaults,
  SkillSelection,
  ThinkingLevel,
} from "./types.js";

const AGENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const SKILL_NAME_PATTERN = AGENT_ID_PATTERN;
const VERSION_PATTERN = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;

export class AgentConfigurationError extends Error {}

export class AgentRegistry {
  readonly #definitions = new Map<string, Map<string, AgentDefinition>>();

  register(definition: AgentDefinition): void {
    validateDefinition(definition);
    const versions = this.#definitions.get(definition.id) ?? new Map();
    if (versions.has(definition.version)) {
      throw new AgentConfigurationError(
        `Agent is already registered: ${definition.id}@${definition.version}`,
      );
    }
    versions.set(definition.version, definition);
    this.#definitions.set(definition.id, versions);
  }

  resolve(id: string, version?: string): AgentDefinition {
    const versions = this.#definitions.get(id);
    if (versions === undefined || versions.size === 0) {
      throw new AgentConfigurationError(`Unknown agent: ${id}`);
    }
    if (version !== undefined) {
      const selected = versions.get(version);
      if (selected === undefined) {
        throw new AgentConfigurationError(`Unknown agent version: ${id}@${version}`);
      }
      return selected;
    }
    return [...versions.values()].sort((left, right) =>
      right.version.localeCompare(left.version, undefined, { numeric: true })
    )[0]!;
  }

  resolveLegacy(kind: "review" | "instruction"): AgentDefinition {
    const matches = this.list().filter((definition) => definition.legacyKind === kind);
    if (matches.length === 0) {
      throw new AgentConfigurationError(`No agent is registered for legacy kind: ${kind}`);
    }
    return matches.sort((left, right) =>
      right.version.localeCompare(left.version, undefined, { numeric: true })
    )[0]!;
  }

  list(): AgentDefinition[] {
    return [...this.#definitions.values()].flatMap((versions) => [...versions.values()]);
  }
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
    if (!definition.allowSkillOverride) {
      throw new AgentConfigurationError(`Agent ${definition.id} does not allow skill overrides.`);
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

  const requestModel = invocation.model;
  if (requestModel?.provider !== undefined && !definition.modelPolicy.allowProviderOverride) {
    throw new AgentConfigurationError(`Agent ${definition.id} does not allow provider overrides.`);
  }
  if (requestModel?.id !== undefined && !definition.modelPolicy.allowModelOverride) {
    throw new AgentConfigurationError(`Agent ${definition.id} does not allow model overrides.`);
  }
  if (requestModel?.base_url !== undefined && !definition.modelPolicy.allowBaseUrlOverride) {
    throw new AgentConfigurationError(`Agent ${definition.id} does not allow model base URL overrides.`);
  }

  // Deployment defaults form the base, legal session overrides are applied next,
  // and a named profile has the final say within the agent's fixed allow-list.
  const provider = profile.provider ?? requestModel?.provider ?? defaults.provider;
  const model = profile.model ?? requestModel?.id ?? defaults.model;
  const thinkingLevel = profile.thinkingLevel
    ?? normalizeThinking(requestModel?.thinking_level ?? defaults.thinkingLevel);
  const baseUrl = requestModel?.base_url ?? defaults.modelBaseUrl;
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
    interactionPolicy: { ...definition.interactionPolicy },
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
  if (all.length > 16) throw new AgentConfigurationError("An agent may load at most 16 skills.");
  for (const skill of all) {
    if (!SKILL_NAME_PATTERN.test(skill)) {
      throw new AgentConfigurationError(`Invalid skill name: ${skill}`);
    }
  }
  if (new Set(all).size !== all.length) {
    throw new AgentConfigurationError("Primary and supporting skills must be unique.");
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
