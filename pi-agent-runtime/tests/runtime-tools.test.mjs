import assert from "node:assert/strict";
import {
  mkdtemp,
  readFile as readHostFile,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
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

const stateRoot = await mkdtemp(join(tmpdir(), "pi-agent-state-"));
const workspaceRoot = await mkdtemp(join(tmpdir(), "pi-agent-workspaces-"));
process.env.PI_AGENT_STATE_ROOT = stateRoot;
process.env.PI_AGENT_TASK_ENVIRONMENT_ROOT = join(stateRoot, "task-environments");
process.env.PI_AGENT_WORKSPACE_ROOT = workspaceRoot;
process.env.PI_AGENT_SKILLS_ROOT = resolve("skills");
process.env.PI_CODING_AGENT_DIR = join(stateRoot, "config");
process.env.PI_AGENT_MODELS_FILE = join(stateRoot, "models.json");
const {
  createInstructionTools,
  createReviewTools,
  flushPersistence,
  startSession,
  validateStartRequest,
} = await import("../dist/server.js");

test.after(async () => {
  await flushPersistence();
  await rm(stateRoot, { recursive: true, force: true });
  await rm(workspaceRoot, { recursive: true, force: true });
});

function record(workspace) {
  return {
    id: "test-session",
    title: "test",
    status: "running",
    stage: "analyzing",
    workspace_path: workspace,
    review: {
      provider: "github",
      repo_full_name: "example/repo",
      pr_number: 1,
      base_sha: "aaaaaaaa",
      head_sha: "bbbbbbbb",
      workspace_path: workspace,
    },
    provider: "openai",
    model: "gpt-5.4",
    thinking_level: "high",
    skills: ["code-review"],
    profile: "default",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    events: [],
  };
}

function instructionRecord(workspace) {
  return {
    id: "instruction-session",
    kind: "instruction",
    idempotency_key: "agent-task:task-1:attempt:1",
    title: "PR instruction",
    status: "running",
    stage: "analyzing",
    workspace_path: workspace,
    repository_context: {
      provider: "github",
      repo_full_name: "example/repo",
      pr_number: 1,
      base_sha: "aaaaaaaa",
      head_sha: "bbbbbbbb",
    },
    instruction: {
      text: "Explain the retry behavior.",
      author_login: "alice",
      history: [],
    },
    provider: "openai",
    model: "gpt-5.4",
    thinking_level: "high",
    skills: ["pr-assistant"],
    profile: "default",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    events: [],
  };
}

function tool(tools, name) {
  const selected = tools.find((item) => item.name === name);
  assert.ok(selected, `missing tool: ${name}`);
  return selected;
}

test("review tools confine reads to the workspace", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-tools-"));
  try {
    await writeFile(join(workspace, "app.py"), "first\nsecond\n", "utf8");
    const tools = createReviewTools(record(workspace));
    const readFile = tool(tools, "read_file");

    const result = await readFile.execute(
      "read-1",
      { path: "app.py" },
      undefined,
      undefined,
      undefined,
    );
    assert.equal(result.content[0].text, "1: first\n2: second\n3: ");
    await assert.rejects(
      readFile.execute(
        "read-2",
        { path: "../etc/passwd" },
        undefined,
        undefined,
        undefined,
      ),
      /repository-relative paths/,
    );
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("workspace tools can write files and execute build commands", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-write-shell-"));
  const outside = await mkdtemp(join(tmpdir(), "pi-agent-write-outside-"));
  try {
    const state = record(workspace);
    const tools = createReviewTools(state);
    const write = await tool(tools, "write_file").execute(
      "write-1",
      { path: "generated/result.txt", content: "ready\n" },
      undefined,
      undefined,
      undefined,
    );
    assert.match(write.content[0].text, /Wrote 6 bytes/);
    assert.equal(await readHostFile(join(workspace, "generated/result.txt"), "utf8"), "ready\n");

    const shell = await tool(tools, "shell").execute(
      "shell-1",
      { command: "wc -c < generated/result.txt" },
      undefined,
      undefined,
      undefined,
    );
    assert.equal(shell.details.exit_code, 0);
    assert.equal(shell.content[0].text.trim(), "6");
    await assert.rejects(
      tool(tools, "write_file").execute(
        "write-escape",
        { path: "../outside", content: "no" },
        undefined,
        undefined,
        undefined,
      ),
      /repository-relative paths/,
    );
    await writeFile(join(outside, "secret.txt"), "do not overwrite\n");
    await symlink(join(outside, "secret.txt"), join(workspace, "escape.txt"));
    await assert.rejects(
      tool(tools, "write_file").execute(
        "write-symlink",
        { path: "escape.txt", content: "no" },
        undefined,
        undefined,
        undefined,
      ),
      /outside the agent workspace/,
    );
  } finally {
    await rm(workspace, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  }
});

test("submit_review records structured output and terminates", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-submit-"));
  try {
    const state = record(workspace);
    const submit = tool(createReviewTools(state), "submit_review");
    const payload = {
      summary: "One issue.",
      findings: [
        {
          file: "app.py",
          line: 2,
          severity: "high",
          message: "Broken check.",
          confidence: 0.9,
        },
      ],
    };

    const result = await submit.execute(
      "submit-1",
      payload,
      undefined,
      undefined,
      undefined,
    );

    assert.equal(state.status, "completed");
    assert.equal(state.stage, "completed");
    assert.deepEqual(state.result, payload);
    assert.equal(result.terminate, true);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("instruction tools expose full workspace capability and submit_task_result", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-instruction-tools-"));
  try {
    await writeFile(join(workspace, "app.py"), "def retry():\n    return True\n", "utf8");
    const state = instructionRecord(workspace);
    const tools = createInstructionTools(state);

    assert.deepEqual(
      tools.map((item) => item.name),
      [
        "list_files",
        "read_file",
        "search_code",
        "git_diff",
        "write_file",
        "shell",
        "submit_task_result",
      ],
    );
    assert.equal(tools.some((item) => item.name === "submit_review"), false);
    assert.equal(tools.some((item) => item.name === "request_human_input"), false);

    const payload = {
      outcome: "answered",
      answer: "The retry returns after the first successful attempt.",
      references: [{ path: "app.py", line_start: 1, line_end: 2 }],
    };
    const result = await tool(tools, "submit_task_result").execute(
      "task-result-1",
      payload,
      undefined,
      undefined,
      undefined,
    );

    assert.equal(state.status, "completed");
    assert.equal(state.stage, "completed");
    assert.deepEqual(state.result, payload);
    assert.equal(result.terminate, true);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("instruction session start is idempotent for one agent task attempt", async () => {
  const workspace = await mkdtemp(join(workspaceRoot, "task-"));
  const request = validateStartRequest({
    kind: "instruction",
    idempotency_key: "agent-task:idempotent-task:attempt:1",
    workspace_path: workspace,
    repository_context: {
      provider: "github",
      repo_full_name: "example/repo",
      pr_number: 1,
      base_sha: "aaaaaaaa",
      head_sha: "bbbbbbbb",
    },
    instruction: {
      text: "Explain the retry.",
      author_login: "alice",
      history: [],
    },
  });

  const first = await startSession(request);
  const duplicate = await startSession(request);

  assert.equal(duplicate.id, first.id);
  assert.equal(duplicate.idempotency_key, "agent-task:idempotent-task:attempt:1");
});

test("generic session start resolves and records a domain preset", async () => {
  const workspace = await mkdtemp(join(workspaceRoot, "review-preset-"));
  const request = validateStartRequest({
    agent_id: "code-review",
    task_type: "code-review",
    repository_skills: ["builtin:code-review"],
    workspace_path: workspace,
    input: {
      provider: "github",
      repo_full_name: "example/repo",
      pr_number: 1,
      base_sha: "aaaaaaaa",
      head_sha: "bbbbbbbb",
    },
  });

  const session = await startSession(request);

  assert.equal(session.kind, "review");
  assert.equal(session.agent_id, "code-review");
  assert.equal(session.agent_version, "1.0.0");
  assert.equal(session.profile, "default");
  assert.equal(session.thinking_level, "high");
  assert.deepEqual(session.skills, ["builtin:code-review"]);
  assert.equal(session.resolved_preset.composition.task_type.id, "code-review");
  assert.deepEqual(session.resolved_preset.composition.repository.skills, [
    "builtin:code-review",
  ]);
  for (let attempt = 0; attempt < 100 && session.status !== "failed"; attempt += 1) {
    await new Promise((resolveTick) => setImmediate(resolveTick));
  }
  assert.equal(session.status, "failed");
  assert.match(session.error, /No API key found|Unknown pi model/);
});

test("pi SDK completes a review through submit_review", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-loop-"));
  try {
    const state = {
      ...record(workspace),
      id: "sdk-loop-session",
      provider: "faux-review",
      model: "faux-1",
      thinking_level: "minimal",
    };
    const expected = { summary: "No issues.", findings: [] };
    const faux = fauxProvider({ provider: "faux-review" });
    const model = faux.getModel();
    faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("submit_review", expected)],
        { stopReason: "toolUse" },
      ),
    ]);

    const authStorage = AuthStorage.inMemory();
    const modelRegistry = ModelRegistry.inMemory(authStorage);
    modelRegistry.registerProvider("faux-review", {
      name: "Faux Review",
      baseUrl: model.baseUrl,
      api: model.api,
      apiKey: "test-only",
      streamSimple: faux.provider.streamSimple,
      models: [
        {
          id: model.id,
          name: model.name,
          api: model.api,
          baseUrl: model.baseUrl,
          reasoning: model.reasoning,
          input: model.input,
          cost: model.cost,
          contextWindow: model.contextWindow,
          maxTokens: model.maxTokens,
        },
      ],
    });
    const tools = createReviewTools(state);
    const { session } = await createAgentSession({
      cwd: workspace,
      agentDir: workspace,
      authStorage,
      modelRegistry,
      model: modelRegistry.find("faux-review", "faux-1"),
      thinkingLevel: "minimal",
      customTools: tools,
      tools: tools.map((item) => item.name),
      sessionManager: SessionManager.inMemory(workspace),
      settingsManager: SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false },
      }),
    });

    await session.prompt("Submit the final review.");

    assert.equal(state.status, "completed");
    assert.deepEqual(state.result, expected);
    assert.equal(faux.state.callCount, 1);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("pi SDK completes an instruction through submit_task_result", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-instruction-loop-"));
  try {
    await writeFile(join(workspace, "app.py"), "def retry():\n    return True\n", "utf8");
    const state = {
      ...instructionRecord(workspace),
      id: "sdk-instruction-session",
      provider: "faux-instruction",
      model: "faux-1",
      thinking_level: "minimal",
    };
    const expected = {
      outcome: "answered",
      answer: "The retry is bounded.",
      references: [{ path: "app.py", line_start: 1, line_end: 2 }],
    };
    const faux = fauxProvider({ provider: "faux-instruction" });
    const model = faux.getModel();
    faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("submit_task_result", expected)],
        { stopReason: "toolUse" },
      ),
    ]);

    const authStorage = AuthStorage.inMemory();
    const modelRegistry = ModelRegistry.inMemory(authStorage);
    modelRegistry.registerProvider("faux-instruction", {
      name: "Faux Instruction",
      baseUrl: model.baseUrl,
      api: model.api,
      apiKey: "test-only",
      streamSimple: faux.provider.streamSimple,
      models: [
        {
          id: model.id,
          name: model.name,
          api: model.api,
          baseUrl: model.baseUrl,
          reasoning: model.reasoning,
          input: model.input,
          cost: model.cost,
          contextWindow: model.contextWindow,
          maxTokens: model.maxTokens,
        },
      ],
    });
    const tools = createInstructionTools(state);
    const { session } = await createAgentSession({
      cwd: workspace,
      agentDir: workspace,
      authStorage,
      modelRegistry,
      model: modelRegistry.find("faux-instruction", "faux-1"),
      thinkingLevel: "minimal",
      customTools: tools,
      tools: tools.map((item) => item.name),
      sessionManager: SessionManager.inMemory(workspace),
      settingsManager: SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false },
      }),
    });

    await session.prompt("Answer the user command.");

    assert.equal(state.status, "completed");
    assert.deepEqual(state.result, expected);
    assert.equal(faux.state.callCount, 1);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});
