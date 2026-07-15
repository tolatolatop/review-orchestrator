import { cp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";

import { runProcess } from "../tools/utils.js";
import type { SessionRecord, SkillSelection } from "./types.js";

export interface PreparedExecutionEnvironment {
  mode: "task-overlay";
  root: string;
  template: string;
  skillPaths: Map<string, string>;
  processEnv: Record<string, string>;
  processUid?: number;
  processGid?: number;
  workspacePath: string;
  workspaceOwnerUid?: number;
  workspaceOwnerGid?: number;
}

export interface ExecutionEnvironmentProvider {
  prepare(
    record: SessionRecord,
    skills: SkillSelection,
  ): Promise<PreparedExecutionEnvironment>;
  dispose(environment: PreparedExecutionEnvironment): Promise<void>;
}

export interface TaskOverlayEnvironmentOptions {
  stateRoot: string;
  skillsRoot: string;
  templateRoot?: string;
  taskUidMin?: number;
  taskUidMax?: number;
  taskGid?: number;
  workspaceOwnerUid?: number;
  workspaceOwnerGid?: number;
}

/** A clonable base plus a disposable dependency overlay for each Task. */
export class TaskOverlayExecutionEnvironmentProvider
implements ExecutionEnvironmentProvider {
  readonly #options: TaskOverlayEnvironmentOptions;
  readonly #activeUids = new Set<number>();

  constructor(options: TaskOverlayEnvironmentOptions) {
    this.#options = options;
  }

  async prepare(
    record: SessionRecord,
    skills: SkillSelection,
  ): Promise<PreparedExecutionEnvironment> {
    const root = join(this.#options.stateRoot, "task-environments", record.id);
    const marker = join(root, ".pi-agent-environment-ready");
    const ready = await stat(marker).then(() => true).catch(() => false);
    if (!ready) {
      await mkdir(root, { recursive: true });
      if (this.#options.templateRoot !== undefined) {
        await cp(this.#options.templateRoot, root, {
          recursive: true,
          force: false,
          errorOnExist: false,
        });
      }
      const packageJson = join(root, "package.json");
      const hasPackage = await stat(packageJson).then(() => true).catch(() => false);
      if (!hasPackage) {
        await writeFile(packageJson, `${JSON.stringify({ private: true }, null, 2)}\n`);
      }
      await writeFile(marker, `${new Date().toISOString()}\n`);
    }

    const controllerUid = process.getuid?.();
    const isolated = controllerUid === 0;
    const processUid = isolated ? this.#allocateTaskUid(record.id) : controllerUid;
    const processGid = isolated
      ? this.#options.taskGid ?? 65532
      : process.getgid?.();
    if (isolated && processUid !== undefined && processGid !== undefined) {
      try {
        await changeOwnership(root, processUid, processGid);
        await changeOwnership(record.workspace_path, processUid, processGid);
        await setOwnerOnly(root);
        await setOwnerOnly(record.workspace_path);
      } catch (error) {
        this.#activeUids.delete(processUid);
        await restoreWorkspaceOwnership(
          record.workspace_path,
          this.#options.workspaceOwnerUid ?? 1000,
          this.#options.workspaceOwnerGid ?? 1000,
        ).catch(() => undefined);
        await rm(root, { recursive: true, force: true });
        throw error;
      }
    }

    const ordered = [skills.primary, ...skills.supporting];
    const skillPaths = new Map<string, string>();
    try {
      for (const reference of ordered) {
        skillPaths.set(
          reference,
          await this.#prepareSkill(root, reference, processUid, processGid),
        );
      }
    } catch (error) {
      if (isolated && processUid !== undefined) this.#activeUids.delete(processUid);
      await restoreWorkspaceOwnership(
        record.workspace_path,
        this.#options.workspaceOwnerUid ?? 1000,
        this.#options.workspaceOwnerGid ?? 1000,
      ).catch(() => undefined);
      await rm(root, { recursive: true, force: true });
      throw error;
    }
    const inheritedPath = process.env.PATH ?? "/usr/bin:/bin";
    return {
      mode: "task-overlay",
      root,
      template: this.#options.templateRoot ?? "builtin",
      skillPaths,
      processEnv: {
        PATH: `${join(root, "node_modules", ".bin")}:${inheritedPath}`,
        LANG: "C.UTF-8",
        HOME: root,
        npm_config_cache: join(root, ".npm-cache"),
      },
      ...(processUid === undefined ? {} : { processUid }),
      ...(processGid === undefined ? {} : { processGid }),
      workspacePath: record.workspace_path,
      ...(isolated
        ? {
          workspaceOwnerUid: this.#options.workspaceOwnerUid ?? 1000,
          workspaceOwnerGid: this.#options.workspaceOwnerGid ?? 1000,
        }
        : {}),
    };
  }

  async dispose(environment: PreparedExecutionEnvironment): Promise<void> {
    if (
      process.getuid?.() === 0
      && environment.workspaceOwnerUid !== undefined
      && environment.workspaceOwnerGid !== undefined
    ) {
      await restoreWorkspaceOwnership(
        environment.workspacePath,
        environment.workspaceOwnerUid,
        environment.workspaceOwnerGid,
      ).catch(() => undefined);
    }
    await rm(environment.root, { recursive: true, force: true });
    if (environment.processUid !== undefined) {
      this.#activeUids.delete(environment.processUid);
    }
  }

  async #prepareSkill(
    root: string,
    reference: string,
    processUid?: number,
    processGid?: number,
  ): Promise<string> {
    if (reference.startsWith("npm:")) {
      const parsed = parseNpmSkillReference(reference);
      const installedPackage = join(
        root,
        "node_modules",
        ...parsed.packageName.split("/"),
      );
      const exists = await stat(installedPackage).then(() => true).catch(() => false);
      if (!exists) {
        const result = await runProcess(
          "npm",
          ["install", "--no-audit", "--no-fund", "--save-exact", parsed.packageSpec],
          root,
          undefined,
          300_000,
          {
            HOME: root,
            npm_config_cache: join(root, ".npm-cache"),
          },
          processUid,
          processGid,
        );
        if (result.exitCode !== 0) {
          throw new Error(
            `Failed to install Skill ${reference}: ${result.stderr || result.stdout}`,
          );
        }
      }
      return await locatePackageSkill(installedPackage, parsed.skillPath);
    }
    if (reference.startsWith("prebuilt:")) {
      const name = validateSkillName(reference.slice("prebuilt:".length));
      const path = join(root, "skills", name, "SKILL.md");
      await requireFile(path, reference);
      return path;
    }
    const name = validateSkillName(
      reference.startsWith("builtin:")
        ? reference.slice("builtin:".length)
        : reference,
    );
    const path = join(this.#options.skillsRoot, name, "SKILL.md");
    await requireFile(path, reference);
    return path;
  }

  #allocateTaskUid(seed: string): number {
    const minimum = this.#options.taskUidMin ?? 20_000;
    const maximum = this.#options.taskUidMax ?? 60_000;
    if (minimum <= 0 || maximum < minimum) {
      throw new Error("Invalid Task UID allocation range.");
    }
    const span = maximum - minimum + 1;
    let hash = 0;
    for (const character of seed) {
      hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
    }
    for (let offset = 0; offset < span; offset += 1) {
      const candidate = minimum + ((hash + offset) % span);
      if (!this.#activeUids.has(candidate)) {
        this.#activeUids.add(candidate);
        return candidate;
      }
    }
    throw new Error("No Task UID is available.");
  }
}

async function changeOwnership(path: string, uid: number, gid: number): Promise<void> {
  const result = await runProcess(
    "chown",
    ["-R", `${uid}:${gid}`, path],
    "/",
    undefined,
    120_000,
  );
  if (result.exitCode !== 0) {
    throw new Error(`Failed to set Task environment ownership: ${result.stderr}`);
  }
}

async function setOwnerOnly(path: string): Promise<void> {
  const result = await runProcess(
    "chmod",
    ["700", path],
    "/",
    undefined,
    30_000,
  );
  if (result.exitCode !== 0) {
    throw new Error(`Failed to isolate Task path: ${result.stderr}`);
  }
}

export async function restoreWorkspaceOwnership(
  workspacePath: string,
  uid: number,
  gid: number,
): Promise<void> {
  if (process.getuid?.() !== 0) return;
  await changeOwnership(workspacePath, uid, gid);
  await setOwnerOnly(workspacePath);
  await runProcess(
    "chmod",
    ["-R", "u+rwX", workspacePath],
    "/",
    undefined,
    120_000,
  );
}

interface NpmSkillReference {
  packageSpec: string;
  packageName: string;
  skillPath?: string;
}

function parseNpmSkillReference(reference: string): NpmSkillReference {
  const raw = reference.slice("npm:".length);
  const [packageSpec, skillPath] = raw.split("::", 2);
  if (!packageSpec || /[\s\\]/.test(packageSpec)) {
    throw new Error(`Invalid npm Skill reference: ${reference}`);
  }
  const withoutVersion = packageSpec.startsWith("@")
    ? packageSpec.split("@").slice(0, 2).join("@")
    : packageSpec.split("@")[0]!;
  if (!withoutVersion || withoutVersion.includes(":")) {
    throw new Error(`npm Skill must use a registry package name: ${reference}`);
  }
  return {
    packageSpec,
    packageName: withoutVersion,
    ...(skillPath === undefined ? {} : { skillPath }),
  };
}

async function locatePackageSkill(
  packageRoot: string,
  requestedPath?: string,
): Promise<string> {
  const packageJson = JSON.parse(
    await readFile(join(packageRoot, "package.json"), "utf8"),
  ) as { piAgentSkill?: string };
  const relativePath = requestedPath ?? packageJson.piAgentSkill ?? "SKILL.md";
  if (relativePath.startsWith("/") || relativePath.split(/[\\/]/).includes("..")) {
    throw new Error(`Invalid Skill path in npm package ${basename(packageRoot)}.`);
  }
  const path = resolve(packageRoot, relativePath);
  await requireFile(path, packageRoot);
  return path;
}

function validateSkillName(name: string): string {
  if (!/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(name)) {
    throw new Error(`Invalid Skill name: ${name}`);
  }
  return name;
}

async function requireFile(path: string, reference: string): Promise<void> {
  const metadata = await stat(path).catch(() => undefined);
  if (metadata === undefined || !metadata.isFile()) {
    throw new Error(`Configured Skill ${reference} does not exist at ${path}.`);
  }
}
