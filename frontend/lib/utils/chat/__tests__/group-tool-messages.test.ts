import { describe, expect, it } from "vitest";
import type { Message } from "@/lib/types";
import { groupConsecutiveToolMessages } from "../group-tool-messages";

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: "assistant",
    role: "assistant",
    content: "",
    timestamp: new Date(0),
    ...overrides,
  };
}

describe("groupConsecutiveToolMessages", () => {
  it("combina chamadas adjacentes em um único item e preserva sua ordem", () => {
    const first = message({
      id: "tool-1",
      toolCalls: [{ id: "1", name: "file_read", args: {}, output: "a" }],
    });
    const second = message({
      id: "tool-2",
      toolCalls: [{ id: "2", name: "git_status", args: {}, output: "b" }],
    });

    expect(groupConsecutiveToolMessages([first, second])).toEqual([
      expect.objectContaining({
        id: "tool-1",
        toolCalls: [first.toolCalls![0], second.toolCalls![0]],
      }),
    ]);
  });

  it("não agrupa através de mensagens de usuário ou respostas com texto", () => {
    const tool = message({
      toolCalls: [{ id: "1", name: "file_read", args: {} }],
    });
    const user = message({ id: "user", role: "user", content: "oi" });
    const answer = message({ id: "answer", content: "Concluído." });

    expect(
      groupConsecutiveToolMessages([tool, user, tool, answer, tool]),
    ).toEqual([tool, user, tool, answer, tool]);
  });

  it("mantém mensagens com erro fora do grupo para preservar o aviso", () => {
    const normal = message({
      id: "normal",
      toolCalls: [{ id: "1", name: "file_read", args: {} }],
    });
    const failed = message({
      id: "failed",
      isError: true,
      toolCalls: [{ id: "2", name: "run_command", args: {}, isError: true }],
    });

    expect(groupConsecutiveToolMessages([normal, failed])).toEqual([
      normal,
      failed,
    ]);
  });

  it("agrupa mensagens com progresso de raciocínio sem descartar os dados", () => {
    const first = message({
      id: "tool-1",
      reasoning: "checando os arquivos",
      thinkingSteps: ["ler a configuração"],
      toolCalls: [{ id: "1", name: "file_read", args: {}, output: "a" }],
      timestamp: new Date(1),
    });
    const last = message({
      id: "tool-2",
      reasoning: "validando a alteração",
      thinkingSteps: ["executar testes"],
      isThinking: true,
      toolCalls: [{ id: "2", name: "run_command", args: {}, output: "b" }],
      timestamp: new Date(2),
    });

    expect(groupConsecutiveToolMessages([first, last])).toEqual([
      expect.objectContaining({
        id: "tool-1",
        timestamp: first.timestamp,
        isThinking: true,
        reasoning: "checando os arquivos\nvalidando a alteração",
        thinkingSteps: ["ler a configuração", "executar testes"],
        toolCalls: [first.toolCalls![0], last.toolCalls![0]],
      }),
    ]);
  });

  it("não agrupa mensagens que precisam permanecer próprias na conversa", () => {
    const tool = message({
      toolCalls: [{ id: "1", name: "file_read", args: {} }],
    });
    const withPlan = message({
      id: "plan",
      plan: [{ content: "Fazer a alteração", status: "in_progress" }],
      toolCalls: [{ id: "2", name: "run_command", args: {} }],
    });
    const failedMessage = message({
      id: "failure",
      isError: true,
      toolCalls: [{ id: "3", name: "run_command", args: {}, isError: true }],
    });

    expect(
      groupConsecutiveToolMessages([tool, withPlan, failedMessage]),
    ).toEqual([tool, withPlan, failedMessage]);
  });
});
