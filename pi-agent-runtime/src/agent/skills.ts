import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { join } from "node:path";

import type { LoadedSkill, SkillSelection } from "./types.js";

export async function loadAgentSkills(
  skillsRoot: string,
  selection: SkillSelection,
  resolvedPaths: Map<string, string> = new Map(),
): Promise<LoadedSkill[]> {
  const ordered = [selection.primary, ...selection.supporting];
  return await Promise.all(ordered.map(async (reference, index) => {
    const fallbackName = reference.startsWith("builtin:")
      ? reference.slice("builtin:".length)
      : reference;
    const path = resolvedPaths.get(reference) ?? join(skillsRoot, fallbackName, "SKILL.md");
    await stat(path).catch(() => {
      throw new Error(`Configured agent skill does not exist: ${path}`);
    });
    const raw = await readFile(path, "utf8");
    const declaredName = parseFrontmatterName(raw);
    if (declaredName === undefined) {
      throw new Error(`Skill ${path} is missing its frontmatter name.`);
    }
    if (
      !reference.startsWith("npm:")
      && !reference.startsWith("prebuilt:")
      && declaredName !== fallbackName
    ) {
      throw new Error(
        `Skill ${path} declares name ${declaredName}; expected ${fallbackName}.`,
      );
    }
    return {
      name: declaredName,
      path,
      digest: createHash("sha256").update(raw).digest("hex"),
      content: stripFrontmatter(raw).trim(),
      role: index === 0 ? "primary" : "supporting",
    };
  }));
}

export function composeSkillPrompt(skills: LoadedSkill[]): string {
  return skills.map((skill) => [
    `## ${skill.role === "primary" ? "Primary" : "Supporting"} Agent Skill: ${skill.name}`,
    `<agent-skill name="${skill.name}" role="${skill.role}">`,
    skill.content,
    "</agent-skill>",
  ].join("\n")).join("\n\n");
}

function parseFrontmatterName(raw: string): string | undefined {
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (match?.[1] === undefined) return undefined;
  const name = match[1].match(/^name:\s*([^\r\n]+)\s*$/m)?.[1];
  return name?.trim();
}

function stripFrontmatter(raw: string): string {
  return raw.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "");
}
