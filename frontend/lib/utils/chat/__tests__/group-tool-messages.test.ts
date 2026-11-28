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
      persistedMessageId: 10,
      runId: "turn-1",
      thinkingStartTime: 100,
      nodeDurations: [{ node: "read", label: "Leitura", duration_ms: 20 }],
      usageMetadata: { input_tokens: 5 },
      wasInterrupted: true,
    });
    const last = message({
      id: "tool-2",
      reasoning: "validando a alteração",
      thinkingSteps: ["executar testes"],
      isThinking: true,
      toolCalls: [{ id: "2", name: "run_command", args: {}, output: "b" }],
      timestamp: new Date(2),
      persistedMessageId: 11,
      runId: "turn-1",
      thinkingDuration: 250,
      currentNodeLabel: "Validando",
      nodeDurations: [{ node: "test", label: "Testes", duration_ms: 50 }],
      usageMetadata: { output_tokens: 3 },
    });

    expect(groupConsecutiveToolMessages([first, last])).toEqual([
      expect.objectContaining({
        id: "tool-1",
        persistedMessageId: 10,
        timestamp: first.timestamp,
        isThinking: true,
        thinkingStartTime: 100,
        thinkingDuration: 250,
        currentNodeLabel: "Validando",
        nodeDurations: [
          { node: "read", label: "Leitura", duration_ms: 20 },
          { node: "test", label: "Testes", duration_ms: 50 },
        ],
        reasoning: "checando os arquivos\nvalidando a alteração",
        thinkingSteps: ["ler a configuração", "executar testes"],
        usageMetadata: { output_tokens: 3 },
        wasInterrupted: true,
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

  it("agrupa ações sem mensagem entre elas e mantém aprovações e perguntas próprias", () => {
    const firstTurn = message({
      id: "turn-1-tool",
      runId: "turn-1",
      toolCalls: [{ id: "1", name: "file_read", args: {} }],
    });
    const secondTurn = message({
      id: "turn-2-tool",
      runId: "turn-2",
      toolCalls: [{ id: "2", name: "file_read", args: {} }],
    });
    const approval = message({
      id: "approval",
      hitlPending: {
        toolName: "file_write",
        argsJson: "{}",
        interruptId: "interrupt-1",
      },
      toolCalls: [{ id: "3", name: "file_write", args: {} }],
    });
    const question = message({
      id: "question",
      structuredQuestionPending: {
        questionId: "question-1",
        threadId: "thread-1",
        prompt: "Escolha",
        options: ["A", "B"],
        allowFreeText: false,
      },
      toolCalls: [{ id: "4", name: "ask_user", args: {} }],
    });

    expect(
      groupConsecutiveToolMessages([firstTurn, secondTurn, approval, question]),
    ).toEqual([
      expect.objectContaining({
        id: "turn-1-tool",
        runId: "turn-1",
        toolCalls: [...firstTurn.toolCalls!, ...secondTurn.toolCalls!],
      }),
      approval,
      question,
    ]);
  });
});
