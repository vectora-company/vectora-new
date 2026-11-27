import { describe, expect, it } from "vitest";
import type { ToolCall } from "@/lib/types";
import { groupToolActivity } from "../tool-activity";

function tool(overrides: Partial<ToolCall> = {}): ToolCall {
  return {
    id: "tool-1",
    name: "file_read",
    args: {},
    category: "filesystem",
    renderHint: "code_block",
    ...overrides,
  };
}

describe("groupToolActivity", () => {
  it("combina chamadas simples consecutivas e mantém sua ordem", () => {
    const first = tool({ id: "first" });
    const second = tool({ id: "second", name: "list_dir" });

    expect(groupToolActivity([first, second])).toEqual([
      { kind: "run", id: "first", tools: [first, second] },
    ]);
  });

  it("mantém cartões de resultado em sua posição entre os grupos", () => {
    const read = tool({ id: "read" });
    const diff = tool({ id: "diff", name: "file_edit", renderHint: "diff" });
    const terminal = tool({
      id: "terminal",
      name: "terminal",
      category: "general",
      renderHint: "terminal_output",
    });

    expect(groupToolActivity([read, diff, terminal])).toEqual([
      { kind: "run", id: "read", tools: [read] },
      { kind: "card", id: "diff", tool: diff },
      { kind: "run", id: "terminal", tools: [terminal] },
    ]);
  });

  it("agrupa falhas sem apagar seu estado e sinaliza o grupo como falho", () => {
    const success = tool({ id: "ok" });
    const failed = tool({ id: "failed", isError: true });

    expect(groupToolActivity([success, failed])).toEqual([
      {
        kind: "run",
        id: "ok",
        tools: [success, failed],
        failedCount: 1,
      },
    ]);
  });

  it("aceita hint desconhecido como chamada recolhível, sem descartar a tool", () => {
    const unknown = tool({ id: "unknown", renderHint: undefined });

    expect(groupToolActivity([unknown])).toEqual([
      { kind: "run", id: "unknown", tools: [unknown] },
    ]);
  });
});
