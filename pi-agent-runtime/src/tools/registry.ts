import type { AgentToolContext, AgentToolFactory, RuntimeTool } from "../agent/types.js";

export class ToolRegistry {
  readonly #factories = new Map<string, AgentToolFactory>();

  register(id: string, factory: AgentToolFactory): void {
    if (this.#factories.has(id)) throw new Error(`Tool is already registered: ${id}`);
    this.#factories.set(id, factory);
  }

  create(ids: string[], context: AgentToolContext): RuntimeTool[] {
    const tools = ids.map((id) => {
      const factory = this.#factories.get(id);
      if (factory === undefined) throw new Error(`Agent requested an unknown tool: ${id}`);
      return factory(context);
    });
    const toolNames = tools.map((tool) => tool.name);
    if (new Set(toolNames).size !== toolNames.length) {
      throw new Error("Agent tool selection resolves to duplicate runtime tool names.");
    }
    return tools;
  }

  list(): string[] {
    return [...this.#factories.keys()].sort();
  }
}
