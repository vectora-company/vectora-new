import type { ToolCall } from "@/lib/types";

interface ToolActivityRun {
  kind: "run";
  id: string;
  tools: ToolCall[];
  failedCount?: number;
}

interface ToolActivityCard {
  kind: "card";
  id: string;
  tool: ToolCall;
}

type ToolActivityItem = ToolActivityCard | ToolActivityRun;

// Rich outputs that need to remain visible in the transcript are boundaries
// between compact activity runs. Ordinary results stay available on expand.
const STANDALONE_RENDER_HINTS = new Set([
  "artifact",
  "browser_screenshot",
  "diff",
  "image_preview",
  "queue_badge",
  "queue_progress",
]);

function isStandaloneToolCard(tool: ToolCall): boolean {
  return (
    tool.renderHint !== undefined &&
    STANDALONE_RENDER_HINTS.has(tool.renderHint)
  );
}

/** Split ordered calls into compact runs and standalone result cards. */
export function groupToolActivity(tools: ToolCall[]): ToolActivityItem[] {
  const items: ToolActivityItem[] = [];
  let run: ToolCall[] = [];

  const flushRun = () => {
    if (run.length === 0) return;
    const failedCount = run.filter((tool) => tool.isError).length;
    items.push({
      kind: "run",
      id: run[0]!.id,
      tools: run,
      ...(failedCount > 0 ? { failedCount } : {}),
    });
    run = [];
  };

  for (const tool of tools) {
    if (isStandaloneToolCard(tool)) {
      flushRun();
      items.push({ kind: "card", id: tool.id, tool });
    } else {
      run.push(tool);
    }
  }

  flushRun();
  return items;
}
