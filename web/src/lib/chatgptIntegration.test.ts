import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { pollChatgptConnect, startChatgptConnect } from "./chatgptIntegration";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

describe("chatgptIntegration", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("polls with the handle and returns the server's status", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "pending" }), { status: 200 }),
    );

    await expect(pollChatgptConnect("h1")).resolves.toEqual({ status: "pending" });

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/v1/connections/chatgpt/device/poll");
    expect(JSON.parse(String(init?.body))).toEqual({ handle: "h1" });
  });

  it("turns a rejected poll into an error status with the server's detail", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "handle belongs to another user" }), { status: 403 }),
    );

    await expect(pollChatgptConnect("h1")).resolves.toEqual({
      status: "error",
      detail: "handle belongs to another user",
    });
  });

  it("surfaces why a sign-in couldn't start", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Enable it in ChatGPT → Settings → Security." }), {
        status: 400,
      }),
    );

    await expect(startChatgptConnect()).rejects.toThrow("Settings → Security");
  });
});
