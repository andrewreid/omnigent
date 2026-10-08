import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { submitClaudeToken } from "./claudeIntegration";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

describe("submitClaudeToken", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("posts the token as JSON and resolves null on success", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ connected: true }), { status: 200 }));

    await expect(submitClaudeToken("sk-ant-oat01-abc")).resolves.toBeNull();

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/v1/connections/claude/secret");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ secret: "sk-ant-oat01-abc" });
  });

  it("surfaces the server's rejection message", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "That doesn't look like a Claude setup token." }), {
        status: 400,
      }),
    );

    await expect(submitClaudeToken("nope")).resolves.toBe(
      "That doesn't look like a Claude setup token.",
    );
  });

  it("falls back to a generic message when the body isn't JSON", async () => {
    fetchMock.mockResolvedValue(new Response("upstream down", { status: 502 }));

    await expect(submitClaudeToken("sk-ant-oat01-abc")).resolves.toBe(
      "Couldn't save the token (502).",
    );
  });
});
