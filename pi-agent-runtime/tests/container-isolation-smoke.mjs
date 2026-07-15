import assert from "node:assert/strict";
import { mkdir, stat, writeFile } from "node:fs/promises";

import { TaskOverlayExecutionEnvironmentProvider } from "/app/dist/agent/environment.js";
import { runProcess } from "/app/dist/tools/utils.js";

const environmentRoot = "/var/lib/pi-agent-task";
const firstWorkspace = "/workspaces/first";
const secondWorkspace = "/workspaces/second";
await Promise.all([
  mkdir(firstWorkspace, { recursive: true }),
  mkdir(secondWorkspace, { recursive: true }),
]);
const provider = new TaskOverlayExecutionEnvironmentProvider({
  stateRoot: environmentRoot,
  skillsRoot: "/opt/pi-agent/skills",
  templateRoot: "/opt/pi-agent/environment-template",
  taskUidMin: 20_000,
  taskUidMax: 60_000,
  taskGid: 65_532,
  workspaceOwnerUid: 1_000,
  workspaceOwnerGid: 1_000,
});
const skills = { primary: "builtin:code-review", supporting: [] };
const first = await provider.prepare(
  { id: "first-task", workspace_path: firstWorkspace },
  skills,
);
const second = await provider.prepare(
  { id: "second-task", workspace_path: secondWorkspace },
  skills,
);
await writeFile(`${secondWorkspace}/unavailable`, "private\n");

assert.notEqual(first.processUid, second.processUid);
assert.equal((await stat(firstWorkspace)).uid, first.processUid);
assert.equal((await stat(firstWorkspace)).mode & 0o777, 0o700);
const parentProbe = await runProcess(
  "head",
  ["-c", "1", `/proc/${process.pid}/environ`],
  firstWorkspace,
  undefined,
  5_000,
  first.processEnv,
  first.processUid,
  first.processGid,
);
assert.notEqual(parentProbe.exitCode, 0);
const siblingProbe = await runProcess(
  "head",
  ["-c", "1", `${secondWorkspace}/unavailable`],
  firstWorkspace,
  undefined,
  5_000,
  first.processEnv,
  first.processUid,
  first.processGid,
);
assert.notEqual(siblingProbe.exitCode, 0);

await Promise.all([provider.dispose(first), provider.dispose(second)]);
assert.equal((await stat(firstWorkspace)).uid, 1_000);
assert.equal((await stat(firstWorkspace)).mode & 0o777, 0o700);
