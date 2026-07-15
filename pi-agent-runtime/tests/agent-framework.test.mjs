import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai/providers/faux";
import {
  AuthStorage,
  createAgentSession,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";

import {
  agentRegistry,
  codeReviewAgent,
  prAssistantAgent,
  validateStartRequest,
} from "../dist/server.js";
import { changeSummaryAgent } from "../dist/agents/change-summary.js";
import {
  AgentConfigurationError,
  AgentRegistry,
  resolveAgentConfiguration,
  resolveDomainPreset,
  validateAgentInput,
} from "../dist/agent/registry.js";
import { TaskOverlayExecutionEnvironmentProvider } from "../dist/agent/environment.js";
import {
  AgentRunner,
  buildSessionArchive,
  finalizeAgentRun,
} from "../dist/agent/runner.js";
import { composeSkillPrompt, loadAgentSkills } from "../dist/agent/skills.js";
import { consumeToolCall, consumeTurn } from "../dist/agent/limits.js";

const repository = {
  provider: "github",
  repo_full_name: "example/repo",
  pr_number: 7,
  base_sha: "aaaaaaaa",
  head_sha: "bbbbbbbb",
};

function configuration(definition, overrides = {}) {
  return resolveAgentConfiguration(
    {
      definition,
      input: overrides.input ?? (
        definition.id === "code-review"
          ? repository
          : { repository_context: repository, instruction: {
            text: "Explain retries.",
            author_login: "alice",
            history: [],
          } }
      ),
      ...(overrides.profile === undefined ? {} : { profile: overrides.profile }),
      ...(overrides.skills === undefined ? {} : { skills: overrides.skills }),
    },
    {
      provider: "openai",
      model: "gpt-5.4",
      thinkingLevel: "high",
    },
  );
}

function sessionRecord(workspace, definition, resolved, input) {
  const timestamp = new Date().toISOString();
  return {
    id: `test-${definition.id}`,
    kind: definition.legacyKind ?? "agent",
    agent_id: definition.id,
    agent_version: definition.version,
    agent_input: input,
    title: definition.title(input),
    status: "running",
    stage: "running",
    workspace_path: workspace,
    repository_context: definition.repositoryContext(input),
    provider: resolved.provider,
    model: resolved.model,
    thinking_level: resolved.thinkingLevel,
    skills: [resolved.skills.primary, ...resolved.skills.supporting],
    skill_digests: {},
    profile: resolved.profileName,
    tools: [],
    execution_limits: resolved.limits,
    execution_counters: { turns: 0, toolCalls: 0 },
    created_at: timestamp,
    updated_at: timestamp,
    events: [],
  };
}

test("builtin registry exposes one installed definition per production agent", () => {
  assert.deepEqual(
    agentRegistry.list().map((agent) => `${agent.id}@${agent.version}`),
    ["code-review@1.0.0", "pr-assistant@1.0.0"],
  );
  assert.equal(agentRegistry.resolveLegacy("review").id, "code-review");
  assert.throws(() => agentRegistry.resolve("change-summary"), /Unknown agent/);
});

test("generic start contract accepts only domain preset selectors", () => {
  const request = validateStartRequest({
    agent_id: "code-review",
    task_type: "code-review",
    repository_skills: ["builtin:code-review"],
    workspace_path: "/workspaces/repo",
    input: repository,
  });
  assert.equal(request.kind, "review");
  assert.equal(request.agent_id, "code-review");
  assert.equal(request.task_type, "code-review");
  assert.deepEqual(request.repository_skills, ["builtin:code-review"]);
  assert.throws(
    () => validateStartRequest({
      agent_id: "code-review",
      agent_version: "1.0.0",
      task_type: "code-review",
      workspace_path: "/workspaces/repo",
      input: repository,
    }),
    /agent_version is not a Runtime request override/,
  );
});

test("domain preset composition applies field-specific ownership", () => {
  const { configuration: resolved, preset } = resolveDomainPreset(
    {
      agentId: "pr-assistant",
      taskType: "message-command",
      repositorySkills: ["npm:@example/repository-skill"],
    },
    prAssistantAgent,
    {
      provider: "openai",
      model: "gpt-5.4",
      thinkingLevel: "high",
    },
  );

  assert.equal(resolved.profileName, "default");
  assert.equal(resolved.skills.primary, "npm:@example/repository-skill");
  assert.deepEqual(resolved.tools, [
    "repository.list-files",
    "repository.read-file",
    "repository.search-code",
    "repository.git-diff",
    "workspace.write-file",
    "workspace.shell",
  ]);
  assert.equal(preset.model.id, "gpt-5.4");
  assert.equal(preset.composition.agent.id, "pr-assistant");
  assert.equal(preset.composition.agent.version, "1.0.0");
  assert.match(preset.composition.agent.digest, /^[0-9a-f]{64}$/);
  assert.deepEqual(preset.composition.repository, {
    skills: ["npm:@example/repository-skill"],
  });
  assert.deepEqual(preset.composition.task_type, {
    id: "message-command",
    profile: "default",
  });
  assert.throws(
    () => resolveDomainPreset(
      {
        agentId: "pr-assistant",
        taskType: "arbitrary-profile",
        repositorySkills: [],
      },
      prAssistantAgent,
      { provider: "openai", model: "gpt-5.4", thinkingLevel: "high" },
    ),
    /does not support task type/,
  );
});

test("database preset resources apply validated bounded overrides", () => {
  const request = validateStartRequest({
    agent_id: "code-review",
    task_type: "code-review",
    repository_skills: ["code-review", "security-analysis"],
    preset_resource: {
      id: "preset-1",
      name: "security-review",
      revision: 3,
    },
    preset_overrides: {
      model: { thinking_level: "medium" },
      tools: ["repository.git-diff", "repository.read-file"],
      limits: { maxTurns: 12, maxToolCalls: 40 },
    },
    workspace_path: "/workspaces/repo",
    input: repository,
  });
  const { configuration: resolved, preset } = resolveDomainPreset(
    {
      agentId: request.agent_id,
      taskType: request.task_type,
      repositorySkills: request.repository_skills,
      resource: request.preset_resource,
      overrides: request.preset_overrides,
    },
    codeReviewAgent,
    { provider: "openai", model: "gpt-5.4", thinkingLevel: "high" },
  );

  assert.equal(resolved.thinkingLevel, "medium");
  assert.deepEqual(resolved.tools, [
    "repository.git-diff",
    "repository.read-file",
  ]);
  assert.equal(resolved.limits.maxTurns, 12);
  assert.equal(resolved.limits.maxToolCalls, 40);
  assert.equal(resolved.limits.maxResultBytes, 250_000);
  assert.deepEqual(preset.resource, {
    id: "preset-1",
    name: "security-review",
    revision: 3,
  });
  assert.deepEqual(preset.skills, ["code-review", "security-analysis"]);
  assert.throws(
    () => resolveDomainPreset(
      {
        agentId: "code-review",
        taskType: "code-review",
        repositorySkills: ["code-review"],
        overrides: { tools: ["untrusted.shell"] },
      },
      codeReviewAgent,
      { provider: "openai", model: "gpt-5.4", thinkingLevel: "high" },
    ),
    /outside the agent code-review allow-list/,
  );
  assert.throws(
    () => validateStartRequest({
      agent_id: "code-review",
      task_type: "code-review",
      repository_skills: ["code-review"],
      preset_overrides: { limits: { maxTurns: 0 } },
      workspace_path: "/workspaces/repo",
      input: repository,
    }),
    /must be an integer between/,
  );
});

test("installed named presets can change tools and execution limits", () => {
  const resolved = configuration(changeSummaryAgent, {
    profile: "deep",
    input: { repository_context: repository },
  });
  assert.equal(resolved.thinkingLevel, "high");
  assert.equal(resolved.limits.maxTurns, 24);
  assert.equal(resolved.limits.maxToolCalls, 80);
  const concise = configuration(changeSummaryAgent, {
    profile: "concise",
    input: { repository_context: repository },
  });
  assert.deepEqual(concise.tools, ["repository.git-diff"]);
  assert.throws(
    () => configuration(codeReviewAgent, { profile: "strict" }),
    /Unknown profile/,
  );
  assert.throws(
    () => configuration({
      ...codeReviewAgent,
      profiles: {
        default: {
          description: "Invalid tool expansion.",
          tools: ["repository.read-file", "untrusted.shell"],
        },
      },
    }),
    /cannot add tools outside the agent allow-list/,
  );
});

test("skill composition is deterministic and records content digests", async () => {
  const skills = await loadAgentSkills(resolve("skills"), {
    primary: "code-review",
    supporting: ["security-analysis"],
  });
  assert.deepEqual(skills.map((skill) => `${skill.role}:${skill.name}`), [
    "primary:code-review",
    "supporting:security-analysis",
  ]);
  assert.ok(skills.every((skill) => /^[0-9a-f]{64}$/.test(skill.digest)));
  const prompt = composeSkillPrompt(skills);
  assert.ok(prompt.indexOf("Primary Agent Skill: code-review") < prompt.indexOf("Supporting Agent Skill: security-analysis"));
  assert.match(prompt, /Apply this as supporting guidance/);
});

test("task overlay clones a prebuilt npm Skill environment", async () => {
  const root = await mkdtemp(join(tmpdir(), "pi-agent-environment-"));
  try {
    const template = join(root, "template");
    const packageRoot = join(template, "node_modules", "example-agent-skill");
    await mkdir(packageRoot, { recursive: true });
    await writeFile(
      join(packageRoot, "package.json"),
      JSON.stringify({ name: "example-agent-skill", piAgentSkill: "SKILL.md" }),
    );
    await writeFile(
      join(packageRoot, "SKILL.md"),
      "---\nname: example-agent-skill\n---\nUse the prebuilt package.\n",
    );
    const resolved = configuration(codeReviewAgent);
    const record = sessionRecord(root, codeReviewAgent, resolved, repository);
    record.id = "overlay-test";
    const provider = new TaskOverlayExecutionEnvironmentProvider({
      stateRoot: join(root, "state"),
      skillsRoot: resolve("skills"),
      templateRoot: template,
    });
    const environment = await provider.prepare(record, {
      primary: "npm:example-agent-skill",
      supporting: ["builtin:security-analysis"],
    });
    const loaded = await loadAgentSkills(
      resolve("skills"),
      {
        primary: "npm:example-agent-skill",
        supporting: ["builtin:security-analysis"],
      },
      environment.skillPaths,
    );

    assert.equal(environment.mode, "task-overlay");
    assert.equal(environment.template, template);
    assert.match(environment.processEnv.PATH, /task-environments\/overlay-test/);
    assert.deepEqual(loaded.map((skill) => `${skill.role}:${skill.name}`), [
      "primary:example-agent-skill",
      "supporting:security-analysis",
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("agent tool selection is allow-listed and completion is schema driven", async () => {
  const root = await mkdtemp(join(tmpdir(), "pi-agent-framework-tools-"));
  try {
    const input = { repository_context: repository, audience: "developer" };
    const resolved = configuration(changeSummaryAgent, { input });
    const record = sessionRecord(root, changeSummaryAgent, resolved, input);
    const runner = new AgentRunner({
      stateRoot: root,
      skillsRoot: resolve("skills"),
      agentDir: root,
      modelsFile: join(root, "models.json"),
      addEvent(state, event) {
        state.events.push({ at: new Date().toISOString(), ...event });
      },
      async persist() {},
    });
    const tools = runner.createTools(record, resolved);
    assert.deepEqual(tools.map((tool) => tool.name), [
      "list_files",
      "read_file",
      "search_code",
      "git_diff",
      "write_file",
      "shell",
      "submit_change_summary",
    ]);
    assert.equal(tools.some((tool) => tool.name === "request_human_input"), false);
    const submit = tools.find((tool) => tool.name === "submit_change_summary");
    await assert.rejects(
      submit.execute("bad-result", { summary: "Missing changes.", changes: [], risks: [] }),
      /Invalid result for agent change-summary/,
    );
    assert.equal(record.status, "validating_result");

    const limitedRecord = sessionRecord(root, changeSummaryAgent, {
      ...resolved,
      limits: { ...resolved.limits, maxResultBytes: 10 },
    }, input);
    const limitedTools = runner.createTools(limitedRecord, {
      ...resolved,
      limits: limitedRecord.execution_limits,
    });
    const limitedSubmit = limitedTools.find((tool) => tool.name === "submit_change_summary");
    await assert.rejects(
      limitedSubmit.execute("large-result", {
        summary: "A valid but larger result.",
        changes: [{ area: "runtime", description: "Changed behavior." }],
        risks: [],
      }),
      /limit is 10/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("prompt builders are stable and keep prior exchanges as delimited context", () => {
  assert.equal(
    codeReviewAgent.buildPrompt({
      input: repository,
      repository,
      profile: "default",
      skills: { primary: "code-review", supporting: [] },
    }),
    [
      "Review example/repo pull request #7.",
      "Provider: github",
      "Base commit: aaaaaaaa",
      "Head commit: bbbbbbbb",
      "Profile: default",
      "Inspect the complete base...head diff and relevant repository context.",
      "When finished, call submit_review with the final structured result.",
    ].join("\n"),
  );

  const input = {
    repository_context: repository,
    instruction: {
      text: "Explain retry behavior.",
      author_login: "alice",
      history: [{
        author_login: "bob",
        command: "Where is retry configured?",
        answer: "In config.py.",
        outcome: "answered",
        head_sha: "aaaaaaaa",
      }],
    },
  };
  validateAgentInput(prAssistantAgent, input);
  assert.equal(
    prAssistantAgent.buildPrompt({
      input,
      repository,
      profile: "default",
      skills: { primary: "pr-assistant", supporting: [] },
    }),
    [
      "Answer a command about example/repo pull request #7.",
      "Provider: github",
      "Base commit: aaaaaaaa",
      "Head commit: bbbbbbbb",
      "Profile: default",
      "Previous orchestrator-owned exchanges:",
      "Previous exchange 1 at aaaaaaaa:\nbob: Where is retry configured?\nassistant (answered): In config.py.",
      "Current user command:",
      "alice: Explain retry behavior.",
      "Answer the current command directly. Inspect repository evidence as needed.",
      "When finished, call submit_task_result with the final structured result.",
    ].join("\n"),
  );

  const summaryInput = {
    repository_context: repository,
    audience: "release-manager",
    focus: "database compatibility",
  };
  assert.equal(
    changeSummaryAgent.buildPrompt({
      input: summaryInput,
      repository,
      profile: "deep",
      skills: { primary: "change-summary", supporting: [] },
    }),
    [
      "Summarize example/repo pull request #7.",
      "Base commit: aaaaaaaa",
      "Head commit: bbbbbbbb",
      "Audience: release-manager",
      "Profile: deep",
      "Requested focus: database compatibility",
      "Inspect the commit-range diff and relevant context.",
      "When finished, call submit_change_summary with the structured result.",
    ].join("\n"),
  );
});

test("execution budgets are enforced independently for turns and tool calls", () => {
  const resolved = configuration(codeReviewAgent);
  const record = sessionRecord("/tmp/workspace", codeReviewAgent, {
    ...resolved,
    limits: { maxTurns: 1, maxToolCalls: 1, maxResultBytes: 1000 },
  }, repository);
  assert.equal(consumeTurn(record), undefined);
  assert.deepEqual(consumeTurn(record), { dimension: "turn", limit: 1 });
  assert.equal(consumeToolCall(record), undefined);
  assert.deepEqual(consumeToolCall(record), { dimension: "tool call", limit: 1 });
});

test("an agent that omits its completion tool fails with a uniform reason", () => {
  const resolved = configuration(codeReviewAgent);
  const record = sessionRecord("/tmp/workspace", codeReviewAgent, resolved, repository);
  const events = [];
  finalizeAgentRun(record, "submit_review", (event) => events.push(event));
  assert.equal(record.status, "failed");
  assert.equal(record.stage, "missing_result");
  assert.equal(
    record.error,
    "Agent code-review ended without calling submit_review.",
  );
  assert.deepEqual(events, [{ type: "session_failed", stage: "missing_result" }]);
});

test("runtime archive contains the full session branch, context, and stats", () => {
  const header = { id: "session-1", version: 3 };
  const entries = [
    { type: "message", message: { role: "user", content: "Review this." } },
    { type: "message", message: { role: "assistant", content: "Done." } },
  ];
  const branch = [entries[0]];
  const context = { messages: entries.map((entry) => entry.message) };
  const stats = { inputTokens: 12, outputTokens: 5, turns: 1 };
  const archive = buildSessionArchive({
    sessionManager: {
      getHeader: () => header,
      getEntries: () => entries,
      getBranch: () => branch,
      buildSessionContext: () => context,
    },
    getSessionStats: () => stats,
  });

  assert.deepEqual(archive, { header, entries, branch, context, stats });
  assert.notEqual(archive.entries, entries);
});

test("a new agent definition registers without changing the registry or runner", () => {
  const registry = new AgentRegistry();
  registry.register({
    ...changeSummaryAgent,
    id: "release-notes",
    version: "2.0.0",
    description: "Test-only release notes agent.",
  });
  assert.equal(registry.resolve("release-notes").result.toolName, "submit_change_summary");
  assert.throws(
    () => registry.register({ ...changeSummaryAgent, id: "release-notes", version: "2.0.0" }),
    AgentConfigurationError,
  );
});

test("pi SDK evaluates the third agent through its generic completion contract", async () => {
  const root = await mkdtemp(join(tmpdir(), "pi-agent-change-summary-eval-"));
  try {
    const input = { repository_context: repository, audience: "reviewer" };
    const resolved = configuration(changeSummaryAgent, { input });
    const record = sessionRecord(root, changeSummaryAgent, resolved, input);
    record.provider = "faux-summary";
    record.model = "faux-1";
    record.thinking_level = "minimal";
    const runtime = new AgentRunner({
      stateRoot: root,
      skillsRoot: resolve("skills"),
      agentDir: root,
      modelsFile: join(root, "models.json"),
      addEvent(state, event) {
        state.events.push({ at: new Date().toISOString(), ...event });
      },
      async persist() {},
    });
    const tools = runtime.createTools(record, resolved);
    const expected = {
      summary: "The change centralizes retry configuration.",
      changes: [{ area: "runtime", description: "Retry policy now comes from one profile." }],
      risks: ["Deployments with custom retry values should verify the new profile."],
    };
    const faux = fauxProvider({ provider: "faux-summary" });
    const model = faux.getModel();
    faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("submit_change_summary", expected)],
        { stopReason: "toolUse" },
      ),
    ]);
    const authStorage = AuthStorage.inMemory();
    const modelRegistry = ModelRegistry.inMemory(authStorage);
    modelRegistry.registerProvider("faux-summary", {
      name: "Faux Summary",
      baseUrl: model.baseUrl,
      api: model.api,
      apiKey: "test-only",
      streamSimple: faux.provider.streamSimple,
      models: [{
        id: model.id,
        name: model.name,
        api: model.api,
        baseUrl: model.baseUrl,
        reasoning: model.reasoning,
        input: model.input,
        cost: model.cost,
        contextWindow: model.contextWindow,
        maxTokens: model.maxTokens,
      }],
    });
    const { session } = await createAgentSession({
      cwd: root,
      agentDir: root,
      authStorage,
      modelRegistry,
      model: modelRegistry.find("faux-summary", "faux-1"),
      thinkingLevel: "minimal",
      customTools: tools,
      tools: tools.map((tool) => tool.name),
      sessionManager: SessionManager.inMemory(root),
      settingsManager: SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false },
      }),
    });
    await session.prompt(changeSummaryAgent.buildPrompt({
      input,
      repository,
      profile: "default",
      skills: resolved.skills,
    }));
    assert.equal(record.status, "completed");
    assert.deepEqual(record.result, expected);
    assert.equal(faux.state.callCount, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
