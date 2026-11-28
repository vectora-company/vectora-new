import type { Message } from "@/lib/types";

/** Combine adjacent tool-only assistant records into one compact activity row. */
export function groupConsecutiveToolMessages(messages: Message[]): Message[] {
  const grouped: Message[] = [];

  for (let index = 0; index < messages.length;) {
    const first = messages[index]!;
    if (!isToolOnlyMessage(first)) {
      grouped.push(first);
      index += 1;
      continue;
    }

    const run = [first];
    let next = index + 1;
    while (next < messages.length && isToolOnlyMessage(messages[next]!)) {
      run.push(messages[next]!);
      next += 1;
    }

    if (run.length === 1) {
      grouped.push(first);
    } else {
      const last = run[run.length - 1]!;
      grouped.push({
        ...first,
        isThinking: last.isThinking,
        activeTool: last.activeTool,
        timestamp: last.timestamp,
        toolCalls: run.flatMap((message) => message.toolCalls ?? []),
      });
    }
    index = next;
  }

  return grouped;
}

function isToolOnlyMessage(message: Message): boolean {
  return (
    message.role === "assistant" &&
    message.content.trim().length === 0 &&
    (message.toolCalls?.length ?? 0) > 0 &&
    !message.reasoning &&
    !message.thinkingSteps?.length &&
    !message.editedFiles?.length &&
    !message.subgraphOutputs?.length &&
    !message.plan?.length &&
    !message.verification &&
    !message.hitlPending &&
    !message.structuredQuestionPending &&
    !message.isError
  );
}
