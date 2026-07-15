import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
process.env.PI_AGENT_WORKSPACE_ROOT = workspaceRoot;
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

test("request_human_input pauses and resumes the same session", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-human-"));
  try {
    const state = record(workspace);
    const requestHuman = tool(createReviewTools(state), "request_human_input");
    const execution = requestHuman.execute(
      "human-1",
      { question: "Is this public?", choices: ["yes", "no"] },
      undefined,
      undefined,
      undefined,
    );
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(state.status, "waiting_for_input");
    assert.equal(state.stage, "waiting_for_human");
    assert.equal(state.pending.question, "Is this public?");
    state.pending.resolve("yes");

    const result = await execution;
    assert.equal(state.status, "running");
    assert.equal(state.stage, "analyzing");
    assert.equal(result.content[0].text, "Operator answer: yes");
  } finally {
    await rm(workspace, { recursive: true, force: true });
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

test("instruction tools expose only read operations and submit_task_result", async () => {
  const workspace = await mkdtemp(join(tmpdir(), "pi-agent-instruction-tools-"));
  try {
    await writeFile(join(workspace, "app.py"), "def retry():\n    return True\n", "utf8");
    const state = instructionRecord(workspace);
    const tools = createInstructionTools(state);

    assert.deepEqual(
      tools.map((item) => item.name),
      ["list_files", "read_file", "search_code", "git_diff", "submit_task_result"],
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
    skills: ["pr-assistant"],
  });

  const first = await startSession(request);
  const duplicate = await startSession(request);

  assert.equal(duplicate.id, first.id);
  assert.equal(duplicate.idempotency_key, "agent-task:idempotent-task:attempt:1");
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
