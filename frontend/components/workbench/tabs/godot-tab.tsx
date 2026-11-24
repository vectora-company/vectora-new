"use client";

import { useState } from "react";
import { Gamepad2, Loader2, Play, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { m } from "@/lib/paraglide/messages";
import { useWorkspacesStore } from "@/lib/stores/workspaces-store";
import { WorkbenchError, WorkbenchToolbar } from "../primitives";

interface GodotValidationStage {
  stage: string;
  exit_code: number | null;
  timed_out: boolean;
  output: string;
}

interface GodotValidationResult {
  status: "passed" | "error" | "remote_unsupported";
  stage?: string;
  message: string;
  engine_version?: string;
  configured_version?: string | null;
  expected_version?: string;
  stages?: GodotValidationStage[];
}

export function GodotTab() {
  const workspace = useWorkspacesStore((s) => s.getActive());
  const [projectPath, setProjectPath] = useState(".");
  const [expectedVersion, setExpectedVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<GodotValidationResult | null>(null);

  async function validate() {
    if (!workspace) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await fetch(
        `/workspaces/${encodeURIComponent(workspace.id)}/godot/validate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_path: projectPath.trim() || ".",
            expected_version: expectedVersion.trim() || null,
          }),
        },
      );
      const payload = (await response.json()) as
        GodotValidationResult | { detail?: string };
      if (!response.ok) {
        throw new Error(
          "detail" in payload && payload.detail
            ? payload.detail
            : m.godot_workbench_request_failed(),
        );
      }
      setResult(payload as GodotValidationResult);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : m.godot_workbench_request_failed(),
      );
    } finally {
      setBusy(false);
    }
  }

  if (!workspace) {
    return <WorkbenchError>{m.godot_workbench_no_workspace()}</WorkbenchError>;
  }

  const canRun =
    workspace.trusted && (workspace.transport ?? "local") === "local";

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkbenchToolbar>
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <Input
            aria-label={m.godot_workbench_project_path()}
            value={projectPath}
            onChange={(event) => setProjectPath(event.target.value)}
            placeholder="."
            className="h-7 min-w-0 text-xs"
            disabled={busy}
          />
          <Input
            aria-label={m.godot_workbench_expected_version()}
            value={expectedVersion}
            onChange={(event) => setExpectedVersion(event.target.value)}
            placeholder="4.6"
            className="h-7 w-16 shrink-0 text-xs"
            disabled={busy}
          />
        </div>
        <Button
          size="sm"
          className="h-7 shrink-0 gap-1.5 text-xs"
          onClick={() => void validate()}
          disabled={!canRun || busy || !projectPath.trim()}
        >
          {busy ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Play className="size-3" />
          )}
          {busy ? m.godot_workbench_running() : m.godot_workbench_validate()}
        </Button>
      </WorkbenchToolbar>

      <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {!canRun && (
          <p className="text-xs text-muted-foreground" role="status">
            {workspace.trusted
              ? m.godot_workbench_local_only()
              : m.godot_workbench_trust_required()}
          </p>
        )}
        <p className="text-xs leading-relaxed text-muted-foreground">
          {m.godot_workbench_description()}
        </p>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {m.godot_workbench_gameplay_note()}
        </p>

        {error && (
          <div
            role="alert"
            className="rounded-md border border-destructive/30 bg-destructive/5 p-2.5 text-xs text-destructive"
          >
            {error}
          </div>
        )}

        {result && (
          <section
            aria-live="polite"
            className="space-y-2 rounded-md border border-border p-2.5"
          >
            <div className="flex items-start gap-2">
              {result.status === "passed" ? (
                <Gamepad2 className="mt-0.5 size-4 shrink-0 text-emerald-500" />
              ) : (
                <TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
              )}
              <div className="min-w-0 space-y-1">
                <p className="text-xs font-medium">
                  {result.status === "passed"
                    ? m.godot_workbench_passed()
                    : m.godot_workbench_failed()}
                </p>
                <p className="break-words text-xs text-muted-foreground">
                  {result.message}
                </p>
                {(result.engine_version || result.configured_version) && (
                  <p className="text-[11px] text-muted-foreground">
                    {m.godot_workbench_versions({
                      actual: result.engine_version ?? "?",
                      configured: result.configured_version ?? "?",
                    })}
                  </p>
                )}
              </div>
            </div>
            {result.stages?.map((stage) => (
              <details
                key={stage.stage}
                className="rounded border border-border/70"
              >
                <summary className="cursor-pointer px-2 py-1.5 text-xs">
                  {stage.stage} · {stage.exit_code ?? "—"}
                  {stage.timed_out ? ` · ${m.godot_workbench_timed_out()}` : ""}
                </summary>
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words border-t border-border/70 bg-muted/20 p-2 font-mono text-[10px]">
                  {stage.output || m.godot_workbench_no_output()}
                </pre>
              </details>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}
