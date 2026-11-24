// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mockWorkspace = {
  id: "ws-1",
  trusted: true,
  transport: "local",
  cwd: "/tmp/project",
};

vi.mock("@/lib/paraglide/messages", () => ({
  m: new Proxy({}, { get: (_target, key) => () => String(key) }),
}));

vi.mock("@/lib/stores/workspaces-store", () => ({
  useWorkspacesStore: (
    selector: (state: { getActive: () => unknown }) => unknown,
  ) => selector({ getActive: () => mockWorkspace }),
}));

import { GodotTab } from "../godot-tab";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("GodotTab", () => {
  it("sends the nested project path and presents a successful validation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "passed",
          message: "Importação e inicialização concluídas.",
          engine_version: "4.6.3",
          configured_version: "4.6",
          stages: [
            {
              stage: "import",
              exit_code: 0,
              timed_out: false,
              output: "Import successful",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<GodotTab />);
    fireEvent.change(screen.getByLabelText("godot_workbench_project_path"), {
      target: { value: "games/platformer" },
    });
    fireEvent.change(
      screen.getByLabelText("godot_workbench_expected_version"),
      {
        target: { value: "4.6" },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "godot_workbench_validate" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe("/workspaces/ws-1/godot/validate");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      project_path: "games/platformer",
      expected_version: "4.6",
    });
    expect(
      await screen.findByText("godot_workbench_passed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("godot_workbench_gameplay_note"),
    ).toBeInTheDocument();
    expect(screen.getByText("Import successful")).toBeInTheDocument();
  });

  it("blocks execution for an untrusted workspace", () => {
    mockWorkspace.trusted = false;
    render(<GodotTab />);

    expect(
      screen.getByRole("button", { name: "godot_workbench_validate" }),
    ).toBeDisabled();
    expect(
      screen.getByText("godot_workbench_trust_required"),
    ).toBeInTheDocument();
    mockWorkspace.trusted = true;
  });

  it("shows endpoint errors instead of a success state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Godot não instalado." }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    render(<GodotTab />);
    fireEvent.click(
      screen.getByRole("button", { name: "godot_workbench_validate" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Godot não instalado.",
    );
    expect(
      screen.queryByText("godot_workbench_passed"),
    ).not.toBeInTheDocument();
  });
});
