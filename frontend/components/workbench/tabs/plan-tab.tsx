"use client";

/**
 * PlanTab — lista unificada de artifacts + checklist ao vivo (write_todos)
 * da sessão, num Accordion multi-item (vários abertos ao mesmo tempo,
 * expandindo inline — sem faixa fixa separada no rodapé).
 *
 * Estado vive no workbench-store (slice `plan`):
 *   - lista de artifacts da sessão → cacheada por threadId
 *   - slugs abertos no Accordion + conteúdo carregado por slug → idem
 * SWR via `useWorkbenchSWR`.
 */

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { MarkdownView } from "@/components/workbench/markdown-view";
import {
  Boxes,
  BookOpen,
  CheckCircle2,
  CheckSquare,
  Circle,
  CircleDot,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Code2,
  FileCode2,
  FileText,
  LayoutList,
  Loader2,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef } from "react";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useWorkbenchSWR } from "@/lib/hooks/workbench/use-swr";
import { useChatInputStore } from "@/lib/stores/chat-input-store";
import {
  approveThreadDAG,
  cancelThreadDAG,
  getThreadActivity,
  getThreadDAG,
  type DAGPlanView,
  type DAGStageView,
} from "@/lib/api/vectora-client";
import {
  WORKBENCH_STALE_MS,
  useWorkbenchStore,
  type PlanItem,
  type TodoItem,
} from "@/lib/stores/workbench-store";
import { m } from "@/lib/paraglide/messages";
import { useSettingsStore } from "@/lib/stores/settings-store";

export async function fetchArtifacts(threadId: string): Promise<PlanItem[]> {
  const qs = new URLSearchParams({ session_id: threadId });
  const res = await fetch(`/artifacts/?${qs}`, {
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`Artifact request failed (${res.status})`);
  const data = await res.json();
  return data.artifacts ?? [];
}

async function fetchArtifactContent(
  threadId: string,
  slug: string,
): Promise<string | null> {
  const qs = new URLSearchParams({ session_id: threadId });
  const res = await fetch(`/artifacts/${encodeURIComponent(slug)}?${qs}`, {
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`Artifact request failed (${res.status})`);
  const data = await res.json();
  return data.content ?? null;
}

function fileSlug(path: string): string {
  const last = path.split(/[/\\]/).pop() ?? "";
  return last.replace(/\.md$/i, "");
}

export function shouldShowPlanLoadSpinner(
  fetchedAt: number,
  loading: boolean,
  hasContent: boolean,
): boolean {
  return loading && (fetchedAt === 0 || !hasContent);
}

// Ícone + cor por artifact_type — mesmo padrão de `components/icons/`.
// Tipo desconhecido/ausente (artifact legado sem sidecar) cai no fallback.
const TYPE_ICON: Record<string, { icon: LucideIcon; className: string }> = {
  plan: { icon: ClipboardList, className: "text-blue-400" },
  spec: { icon: FileCode2, className: "text-purple-400" },
  task_list: { icon: CheckSquare, className: "text-amber-400" },
  overview: { icon: LayoutList, className: "text-sky-400" },
  guide: { icon: BookOpen, className: "text-green-400" },
  architecture: { icon: Boxes, className: "text-orange-400" },
  implementation: { icon: Code2, className: "text-pink-400" },
  skill_learned: { icon: Sparkles, className: "text-fuchsia-400" },
  fact_learned: { icon: Sparkles, className: "text-fuchsia-400" },
  remember_proposal: { icon: Sparkles, className: "text-fuchsia-300" },
};
const DEFAULT_TYPE_ICON = { icon: FileText, className: "text-primary" };

function artifactIcon(artifactType: string | undefined) {
  return (artifactType && TYPE_ICON[artifactType]) || DEFAULT_TYPE_ICON;
}

const TODOS_SLUG = "__todos__";

interface PlanTabProps {
  threadId: string;
  onOpenPlanDocument?: (
    item: PlanItem,
    content: string | null,
    error?: string,
    phase?: "open" | "update",
  ) => void;
}

function FilesTouchedSection({ threadId }: { threadId: string }) {
  const [open, setOpen] = useState(false);
  const { data } = useQuery({
    queryKey: ["thread-activity", threadId],
    queryFn: () => getThreadActivity(threadId),
    staleTime: 60_000,
  });
  const files = data?.files_touched ?? [];
  if (files.length === 0) return null;
  return (
    <div className="border-t border-border/40 shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? (
          <ChevronDown className="w-3 h-3 shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 shrink-0" />
        )}
        <span className="font-medium flex-1 text-left">
          {m.workbench_plan_files_touched()} ({files.length})
        </span>
      </button>
      {open && (
        <div className="px-2 pb-2 space-y-0.5">
          {files.map((f) => (
            <p
              key={f}
              className="text-[10px] font-mono text-muted-foreground truncate pl-4"
            >
              {f}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function TodoStatusIcon({ status }: { status: TodoItem["status"] }) {
  switch (status) {
    case "completed":
      return (
        <CheckCircle2 className="w-3 h-3 shrink-0 text-primary" aria-hidden />
      );
    case "in_progress":
      return (
        <CircleDot className="w-3 h-3 shrink-0 text-amber-500" aria-hidden />
      );
    default:
      return (
        <Circle
          className="w-3 h-3 shrink-0 text-muted-foreground/50"
          aria-hidden
        />
      );
  }
}

function dagStatusLabel(plan: DAGPlanView): string {
  if (plan.execution?.status === "running") return m.workbench_dag_running();
  if (plan.execution?.status === "cancelling")
    return m.workbench_dag_cancelling();
  if (plan.execution?.status === "passed") return m.workbench_dag_passed();
  if (plan.execution?.status === "failed") return m.workbench_dag_failed();
  if (plan.execution?.status === "cancelled")
    return m.workbench_dag_cancelled();
  return plan.status === "draft"
    ? m.workbench_dag_draft()
    : m.workbench_dag_approved();
}

function dagStageStatusLabel(status: string): string {
  if (status === "running") return m.workbench_dag_running();
  if (status === "passed") return m.workbench_dag_passed();
  if (status === "failed") return m.workbench_dag_failed();
  if (status === "cancelled") return m.workbench_dag_cancelled();
  return m.workbench_dag_pending();
}

export function DAGPlanCard({
  plan,
  busy,
  error,
  onApprove,
  onCancel,
}: {
  plan: DAGPlanView;
  busy: boolean;
  error: string | null;
  onApprove: () => void;
  onCancel: () => void;
}) {
  const stages: DAGStageView[] = plan.execution?.stages ?? plan.stages;
  return (
    <section
      className="border-b border-border/50 p-2 space-y-2"
      data-testid="dag-plan-card"
      aria-label={m.workbench_dag_title()}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium">{m.workbench_dag_title()}</span>
        <span className="text-[11px] text-muted-foreground">
          {dagStatusLabel(plan)}
        </span>
      </div>
      <ol className="space-y-1">
        {stages.map((stage) => (
          <li
            key={stage.stage_id}
            className="text-[11px] flex items-start gap-1.5 text-muted-foreground"
          >
            <Circle className="w-3 h-3 mt-0.5 shrink-0" />
            <span className="min-w-0">
              {stage.name}
              {plan.execution && ` · ${dagStageStatusLabel(stage.status)}`}
              {stage.error && (
                <span className="block text-destructive">{stage.error}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-1.5">
        {plan.status === "draft" && (
          <Button
            size="sm"
            className="h-7 text-xs"
            disabled={busy}
            onClick={onApprove}
          >
            {busy ? m.workbench_dag_approving() : m.workbench_dag_approve()}
          </Button>
        )}
        {plan.execution?.status === "running" && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={busy}
            onClick={onCancel}
          >
            {m.workbench_dag_cancel()}
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-[11px] text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}

export function PlanTab({ threadId, onOpenPlanDocument }: PlanTabProps) {
  const queryClient = useQueryClient();
  const [dagBusy, setDagBusy] = useState(false);
  const [dagError, setDagError] = useState<string | null>(null);
  const [planLoadAttempt, setPlanLoadAttempt] = useState(0);
  const [planLoadState, setPlanLoadState] = useState<{
    threadId: string;
    error: string | null;
    loading: boolean;
  }>({ threadId, error: null, loading: true });
  // A chave inclui a thread e o contador nunca é resetado. Reiniciar o mapa
  // ao trocar de thread permitia que uma resposta antiga e uma nova usassem
  // o mesmo epoch para o mesmo slug.
  const contentRequestEpoch = useRef(new Map<string, number>());
  const autoExpandedThreadsRef = useRef(new Set<string>());
  const activeThreadRef = useRef(threadId);
  useEffect(() => {
    activeThreadRef.current = threadId;
  }, [threadId]);
  const items = useWorkbenchStore((s) => s.getPlan(threadId).items);
  const todos = useWorkbenchStore((s) => s.getTodos(threadId));
  const fetchedAt = useWorkbenchStore((s) => s.getPlan(threadId).fetchedAt);
  const planLoadError =
    planLoadState.threadId === threadId ? planLoadState.error : null;
  const planLoadPending =
    planLoadState.threadId === threadId ? planLoadState.loading : true;
  const openSlugs = useWorkbenchStore((s) => s.getPlan(threadId).openSlugs);
  const contentsBySlug = useWorkbenchStore(
    (s) => s.getPlan(threadId).contentsBySlug,
  );
  const planAutoExpand = useSettingsStore((s) => s.planAutoExpand);
  const planSort = useSettingsStore((s) => s.planSort);
  const dagQuery = useQuery({
    queryKey: ["thread-dag", threadId],
    queryFn: () => getThreadDAG(threadId),
    refetchInterval: (query) => {
      const plan = query.state.data;
      return plan?.status === "draft" ||
        plan?.execution?.status === "running" ||
        plan?.execution?.status === "cancelling"
        ? 3000
        : false;
    },
  });
  const dag = dagQuery.data;
  const refreshDag = dagQuery.refetch;
  useEffect(() => {
    if (todos.length > 0) void refreshDag();
  }, [refreshDag, todos]);
  const updateDAG = useCallback(
    async (action: "approve" | "cancel") => {
      if (!dag) return;
      setDagBusy(true);
      setDagError(null);
      try {
        const updated =
          action === "approve"
            ? await approveThreadDAG(threadId, dag.plan_id, dag.plan_hash)
            : await cancelThreadDAG(threadId, dag.plan_id);
        queryClient.setQueryData(["thread-dag", threadId], updated);
      } catch {
        setDagError(
          action === "approve"
            ? m.workbench_dag_approval_failed()
            : m.workbench_dag_cancel_failed(),
        );
        await queryClient.invalidateQueries({
          queryKey: ["thread-dag", threadId],
        });
      } finally {
        setDagBusy(false);
      }
    },
    [dag, queryClient, threadId],
  );

  const setPlanItems = useWorkbenchStore((s) => s.setPlanItems);
  const togglePlanOpenSlug = useWorkbenchStore((s) => s.togglePlanOpenSlug);
  const setPlanContent = useWorkbenchStore((s) => s.setPlanContent);

  const isPlanStale = useCallback(
    () => Date.now() - fetchedAt > WORKBENCH_STALE_MS,
    [fetchedAt],
  );

  useWorkbenchSWR({
    key: `plan:${threadId}:${planLoadAttempt}`,
    hasCache: fetchedAt > 0,
    isStale: isPlanStale,
    revalidate: async () => {
      setPlanLoadState({ threadId, error: null, loading: true });
      try {
        const list = await fetchArtifacts(threadId);
        setPlanItems(threadId, list);
        setPlanLoadState({ threadId, error: null, loading: false });
      } catch {
        // Marca a tentativa como concluída sem apagar artifacts já cacheados.
        // Assim o SWR não fica repetindo a mesma falha por fetchedAt===0.
        setPlanItems(threadId, items);
        setPlanLoadState({
          threadId,
          error: m.workbench_plan_load_failed(),
          loading: false,
        });
      }
    },
  });

  // write_todos (TodoListMiddleware) vira UMA entrada sintética no
  // Accordion (não um item por tarefa) — sempre a mais recente, já que é o
  // progresso ao vivo do turno atual, não um documento datado como os
  // artifacts.
  const entries = useMemo(() => {
    const artifactEntries = items.map((item) => ({
      slug: fileSlug(item.path),
      timestamp: new Date(item.created_at).getTime() || 0,
      item,
    }));
    // O checklist ao vivo é sempre o progresso do turno atual — ordena
    // primeiro sem depender do relógio (Infinity vence qualquer created_at).
    const todosEntry =
      todos.length > 0
        ? [
            {
              slug: TODOS_SLUG,
              timestamp: Number.POSITIVE_INFINITY,
              item: null,
            },
          ]
        : [];
    return [...todosEntry, ...artifactEntries].toSorted((a, b) => {
      if (a.item && b.item && planSort === "title") {
        return a.item.title.localeCompare(b.item.title);
      }
      return b.timestamp - a.timestamp;
    });
  }, [items, todos, planSort]);

  const handleAccordionChange = useCallback(
    (next: string[]) => {
      const added = next.filter((v) => !openSlugs.includes(v));
      const removed = openSlugs.filter((v) => !next.includes(v));
      for (const slug of [...added, ...removed]) {
        togglePlanOpenSlug(threadId, slug);
      }
      for (const slug of added) {
        if (slug === TODOS_SLUG) continue;
        const item = items.find(
          (candidate) => fileSlug(candidate.path) === slug,
        );
        if (contentsBySlug[slug] !== undefined) {
          if (item)
            onOpenPlanDocument?.(item, contentsBySlug[slug], undefined, "open");
          continue;
        }
        const requestKey = `${threadId}:${slug}`;
        const requestEpoch =
          (contentRequestEpoch.current.get(requestKey) ?? 0) + 1;
        contentRequestEpoch.current.set(requestKey, requestEpoch);
        const requestThreadId = threadId;
        void fetchArtifactContent(threadId, slug)
          .then((content) => {
            if (
              requestThreadId !== activeThreadRef.current ||
              requestEpoch !== contentRequestEpoch.current.get(requestKey)
            ) {
              if (item)
                onOpenPlanDocument?.(
                  item,
                  null,
                  m.workbench_git_operation_failed(),
                  "update",
                );
              return;
            }
            if (content !== null) setPlanContent(threadId, slug, content);
            if (item) {
              onOpenPlanDocument?.(
                item,
                content,
                content === null
                  ? m.workbench_git_operation_failed()
                  : undefined,
                "update",
              );
            }
          })
          .catch(() => {
            if (
              requestThreadId !== activeThreadRef.current ||
              requestEpoch !== contentRequestEpoch.current.get(requestKey)
            ) {
              if (item)
                onOpenPlanDocument?.(
                  item,
                  null,
                  m.workbench_git_operation_failed(),
                  "update",
                );
              return;
            }
            if (item)
              onOpenPlanDocument?.(
                item,
                null,
                m.workbench_git_operation_failed(),
                "update",
              );
          });
        if (item)
          onOpenPlanDocument?.(
            item,
            contentsBySlug[slug] ?? null,
            undefined,
            "open",
          );
      }
    },
    [
      openSlugs,
      threadId,
      togglePlanOpenSlug,
      contentsBySlug,
      setPlanContent,
      items,
      onOpenPlanDocument,
    ],
  );

  useEffect(() => {
    if (!planAutoExpand || entries.length === 0) return;
    const first = entries[0]?.slug;
    if (!first || autoExpandedThreadsRef.current.has(threadId)) return;
    autoExpandedThreadsRef.current.add(threadId);
    if (!openSlugs.includes(first)) {
      handleAccordionChange([...openSlugs, first]);
    }
  }, [entries, handleAccordionChange, openSlugs, planAutoExpand, threadId]);

  // Estado de loading inicial: ainda não fetchamos uma única vez.
  const hasPlanContent = items.length > 0 || todos.length > 0 || Boolean(dag);
  const initialLoading = shouldShowPlanLoadSpinner(
    fetchedAt,
    planLoadPending,
    hasPlanContent,
  );

  if (initialLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // "Sem planos" só quando NÃO há artifact salvo NEM checklist do write_todos.
  // Plan Mode gera o plano via write_todos (todos), sem create_artifact — olhar
  // só `items` (artifacts) escondia o plano inteiro atrás do empty-state.
  if (items.length === 0 && todos.length === 0 && !dag) {
    if (planLoadError) {
      return (
        <div className="h-full flex flex-col items-center justify-center gap-3 p-4 text-center">
          <p role="alert" className="text-xs text-destructive">
            {planLoadError}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => setPlanLoadAttempt((attempt) => attempt + 1)}
          >
            {m.workbench_plan_load_retry()}
          </Button>
        </div>
      );
    }
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 p-4 text-center">
        <FileText className="w-6 h-6 text-muted-foreground/40" />
        <p className="text-xs text-muted-foreground">
          {m.workbench_plan_empty()}
        </p>
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs gap-1.5"
          onClick={() =>
            useChatInputStore
              .getState()
              .pushDraft(m.workbench_plan_ask_prompt())
          }
        >
          <Sparkles className="w-3 h-3" />
          {m.workbench_plan_ask_cta()}
        </Button>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {planLoadError && (
        <p role="alert" className="px-2 py-1 text-[11px] text-destructive">
          {planLoadError}
          <button
            type="button"
            className="ml-2 underline underline-offset-2"
            onClick={() => setPlanLoadAttempt((attempt) => attempt + 1)}
          >
            {m.workbench_plan_load_retry()}
          </button>
        </p>
      )}
      {dag && (
        <DAGPlanCard
          plan={dag}
          busy={dagBusy}
          error={dagError}
          onApprove={() => void updateDAG("approve")}
          onCancel={() => void updateDAG("cancel")}
        />
      )}
      <div className="flex-1 overflow-y-auto min-h-0">
        <Accordion
          type="multiple"
          value={openSlugs}
          onValueChange={handleAccordionChange}
          className="px-1"
        >
          {entries.map(({ slug, item }) =>
            slug === TODOS_SLUG ? (
              <AccordionItem key={slug} value={slug}>
                <AccordionTrigger data-testid="plan-todos-trigger">
                  <span className="flex items-center gap-2 min-w-0">
                    <CheckSquare className="w-3.5 h-3.5 shrink-0 text-amber-400" />
                    <span className="truncate">
                      {m.workbench_plan_tasks_section()} ({todos.length})
                    </span>
                  </span>
                </AccordionTrigger>
                <AccordionContent>
                  <div
                    className="divide-y divide-border/30"
                    data-testid="plan-todos-list"
                  >
                    {todos.map((t, idx) => (
                      <div
                        key={idx}
                        data-testid="plan-todo-item"
                        className="py-1.5 text-[11px] flex items-center gap-2"
                      >
                        <TodoStatusIcon status={t.status} />
                        <span
                          className={
                            t.status === "completed"
                              ? "line-through text-foreground/40"
                              : "text-foreground/70"
                          }
                        >
                          {t.content}
                        </span>
                      </div>
                    ))}
                  </div>
                </AccordionContent>
              </AccordionItem>
            ) : (
              item && (
                <AccordionItem key={slug} value={slug}>
                  <AccordionTrigger>
                    {(() => {
                      const { icon: Icon, className } = artifactIcon(
                        item.artifact_type,
                      );
                      return (
                        <span className="flex items-center gap-2 min-w-0">
                          <Icon
                            className={`w-3.5 h-3.5 shrink-0 mt-0.5 self-start ${className}`}
                          />
                          <span className="whitespace-normal break-words">
                            {item.title}
                          </span>
                        </span>
                      );
                    })()}
                  </AccordionTrigger>
                  <AccordionContent>
                    {contentsBySlug[slug] === undefined ? (
                      <Loader2 className="w-4 h-4 animate-spin text-muted-foreground m-2" />
                    ) : (
                      <MarkdownView content={contentsBySlug[slug]} />
                    )}
                  </AccordionContent>
                </AccordionItem>
              )
            ),
          )}
        </Accordion>
      </div>

      <FilesTouchedSection threadId={threadId} />
    </div>
  );
}
