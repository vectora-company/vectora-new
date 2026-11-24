// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchArtifacts } from "../plan-tab";

afterEach(() => vi.unstubAllGlobals());

describe("fetchArtifacts", () => {
  it("surfaces a failed response so the Plan workbench can show recovery UI", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })),
    );

    await expect(fetchArtifacts("thread-1")).rejects.toThrow(
      "Artifact request failed (503)",
    );
  });

  it("returns the artifacts for the requested thread", async () => {
    const artifacts = [
      {
        title: "Implementation plan",
        path: "artifacts/plan.md",
        created_at: "2026-11-24T00:00:00Z",
      },
    ];
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ artifacts })));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchArtifacts("thread-1")).resolves.toEqual(artifacts);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      "session_id=thread-1",
    );
  });
});
