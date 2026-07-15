import type { SessionRecord } from "./types.js";

export type ExecutionBudgetDimension = "turn" | "tool call";

export interface ExecutionBudgetExceeded {
  dimension: ExecutionBudgetDimension;
  limit: number;
}

export function consumeTurn(record: SessionRecord): ExecutionBudgetExceeded | undefined {
  record.execution_counters.turns += 1;
  if (record.execution_counters.turns > record.execution_limits.maxTurns) {
    return { dimension: "turn", limit: record.execution_limits.maxTurns };
  }
  return undefined;
}

export function consumeToolCall(record: SessionRecord): ExecutionBudgetExceeded | undefined {
  record.execution_counters.toolCalls += 1;
  if (record.execution_counters.toolCalls > record.execution_limits.maxToolCalls) {
    return { dimension: "tool call", limit: record.execution_limits.maxToolCalls };
  }
  return undefined;
}
