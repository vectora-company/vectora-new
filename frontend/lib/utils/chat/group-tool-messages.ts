import type { Message } from "@/lib/types";
import { groupToolActivity } from "./tool-activity";

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
      const reasoning = run
        .map((message) => message.reasoning?.trim())
        .filter((value): value is string => Boolean(value))
        .join("\n");
      const thinkingDuration = run.reduce(
        (total, message) => total + (message.thinkingDuration ?? 0),
        0,
      );
      const nodeDurations = run.flatMap(
        (message) => message.nodeDurations ?? [],
      );
      const usageMetadata = run
        .toReversed()
        .find((message) => message.usageMetadata)?.usageMetadata;
      grouped.push({
        ...first,
        ...last,
        id: first.id,
        persistedMessageId: first.persistedMessageId,
        runId: first.runId,
        timestamp: first.timestamp,
        thinkingStartTime: first.thinkingStartTime ?? last.thinkingStartTime,
        thinkingDuration: thinkingDuration || undefined,
        isThinking: last.isThinking,
        currentNodeLabel: last.currentNodeLabel,
        nodeDurations,
        activeTool: last.activeTool,
        reasoning: reasoning || undefined,
        thinkingSteps: run.flatMap((message) => message.thinkingSteps ?? []),
        ...(usageMetadata ? { usageMetadata } : {}),
        ...(run.some((message) => message.wasInterrupted)
          ? { wasInterrupted: true }
          : {}),
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
    !groupToolActivity(message.toolCalls ?? []).some(
      (item) => item.kind === "card",
    ) &&
    !message.editedFiles?.length &&
    !message.subgraphOutputs?.length &&
    !message.plan?.length &&
    !message.verification &&
    !message.hitlPending &&
    !message.structuredQuestionPending &&
    !message.isError &&
    !message.checkpointId &&
    message.feedback == null &&
    !message.feedbackId &&
    !message.feedbackComment &&
    !message.images?.length &&
    !message.ragCitations?.length &&
    !message.steers?.length
  );
}
