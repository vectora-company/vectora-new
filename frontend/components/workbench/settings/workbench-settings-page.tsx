"use client";

import type {
  WorkbenchSettingsContext,
  WorkbenchSettingsDescriptor,
  WorkbenchId,
} from "@/lib/types/workbench-settings";
import { m } from "@/lib/paraglide/messages";
import { WorkbenchSettingsContent } from "@/components/workbench/settings/workbench-settings-content";
import { SettingsSection } from "@/components/settings/primitives";
import { WORKBENCH_TABS } from "@/lib/stores/workbench-store";

interface WorkbenchSettingsPageProps {
  descriptors: readonly WorkbenchSettingsDescriptor[];
  context: Omit<WorkbenchSettingsContext, "presentation">;
}

const WORKBENCH_LABELS: Record<WorkbenchId, () => string> = {
  context_graph: () => m.workbench_tab_context_graph(),
  storage: () => m.workbench_tab_storage(),
  tasks: () => m.workbench_tab_tasks(),
  browser: () => m.workbench_tab_browser(),
  git: () => m.workbench_tab_git(),
  terminal: () => m.workbench_tab_terminal(),
  files: () => m.workbench_tab_files(),
  plan: () => m.workbench_tab_plan(),
  library: () => m.workbench_tab_library(),
};

/** Página global que agrupa os mesmos descriptors por workbench. */
export function WorkbenchSettingsPage({
  descriptors,
  context,
}: WorkbenchSettingsPageProps) {
  if (descriptors.length === 0) {
    return (
      <div className="flex min-h-32 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
        {m.workbench_settings_empty()}
      </div>
    );
  }

  const groups = descriptors.reduce<
    Map<WorkbenchId, WorkbenchSettingsDescriptor[]>
  >((result, descriptor) => {
    const group = result.get(descriptor.workbench) ?? [];
    group.push(descriptor);
    result.set(descriptor.workbench, group);
    return result;
  }, new Map());
  const orderedWorkbenches = WORKBENCH_TABS.filter(
    (workbench): workbench is WorkbenchId =>
      workbench !== "godot" && groups.has(workbench),
  );

  return (
    <div className="flex min-w-0 w-full flex-col gap-6 overflow-y-auto p-4">
      <nav aria-label={m.workbench_settings_page_aria()} className="min-w-0">
        <p className="mb-2 text-xs font-medium text-muted-foreground">
          {m.workbench_settings_page_index()}
        </p>
        <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-1 text-sm">
          {orderedWorkbenches.map((workbench) => (
            <a
              key={workbench}
              href={`#workbench-settings-${workbench}`}
              className="break-words text-primary hover:underline"
            >
              {WORKBENCH_LABELS[workbench]()}
            </a>
          ))}
        </div>
      </nav>
      {orderedWorkbenches.flatMap((workbench) =>
        (groups.get(workbench) ?? []).map((descriptor, index) => (
          <SettingsSection
            key={descriptor.id}
            id={index === 0 ? `workbench-settings-${workbench}` : undefined}
            className="scroll-mt-4"
            title={descriptor.title()}
            description={descriptor.description?.()}
          >
            <WorkbenchSettingsContent
              descriptor={descriptor}
              context={{ ...context, presentation: "settings" }}
            />
          </SettingsSection>
        )),
      )}
    </div>
  );
}
