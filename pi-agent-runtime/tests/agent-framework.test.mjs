import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
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
  changeSummaryAgent,
  codeReviewAgent,
  prAssistantAgent,
  validateStartRequest,
} from "../dist/server.js";
import {
  AgentConfigurationError,
  AgentRegistry,
  resolveAgentConfiguration,
  validateAgentInput,
} from "../dist/agent/registry.js";
import { AgentRunner, finalizeAgentRun } from "../dist/agent/runner.js";
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
      ...(overrides.model === undefined ? {} : { model: overrides.model }),
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
    interaction_policy: resolved.interactionPolicy,
    created_at: timestamp,
    updated_at: timestamp,
    events: [],
  };
}

test("builtin registry exposes versioned agents including a non-legacy third agent", () => {
  assert.deepEqual(
    agentRegistry.list().map((agent) => `${agent.id}@${agent.version}`),
    ["code-review@1.0.0", "pr-assistant@1.0.0", "change-summary@1.0.0"],
  );
  assert.equal(agentRegistry.resolveLegacy("review").id, "code-review");
  assert.equal(agentRegistry.resolve("change-summary").legacyKind, undefined);
});

test("generic start contract validates agent input without a kind branch", () => {
  const request = validateStartRequest({
    agent_id: "change-summary",
    agent_version: "1.0.0",
    workspace_path: "/workspaces/repo",
    profile: "deep",
    input: {
      repository_context: repository,
      audience: "release-manager",
      focus: "database compatibility",
    },
  });
  assert.equal(request.kind, "agent");
  assert.equal(request.agent_id, "change-summary");
  assert.equal(request.profile, "deep");
  assert.throws(
    () => validateStartRequest({
      agent_id: "change-summary",
      workspace_path: "/workspaces/repo",
      input: { repository_context: { ...repository, pr_number: 0 } },
    }),
    /Invalid input for agent change-summary/,
  );
});

test("profiles change effective model, skills, and execution limits", () => {
  const resolved = configuration(codeReviewAgent, {
    profile: "strict",
    model: { thinking_level: "minimal" },
  });
  assert.equal(resolved.thinkingLevel, "xhigh");
  assert.deepEqual(resolved.skills, {
    primary: "code-review",
    supporting: ["security-analysis"],
  });
  assert.equal(resolved.limits.maxTurns, 40);
  assert.equal(resolved.limits.maxToolCalls, 160);
  const concise = configuration(changeSummaryAgent, {
    profile: "concise",
    input: { repository_context: repository },
  });
  assert.deepEqual(concise.tools, ["repository.git-diff"]);
  assert.throws(
    () => configuration(codeReviewAgent, { profile: "does-not-exist" }),
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
      profile: "fast",
      skills: { primary: "code-review", supporting: [] },
    }),
    [
      "Review example/repo pull request #7.",
      "Provider: github",
      "Base commit: aaaaaaaa",
      "Head commit: bbbbbbbb",
      "Profile: fast",
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
