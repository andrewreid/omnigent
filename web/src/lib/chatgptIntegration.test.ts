import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { disconnectChatgpt, pollChatgptConnect, startChatgptConnect } from "./chatgptIntegration";

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

  it("reports a bare 5xx from a proxy as unreachable, not a failed sign-in", async () => {
    fetchMock.mockResolvedValue(new Response("upstream request timeout", { status: 503 }));

    await expect(pollChatgptConnect("h1")).resolves.toEqual({ status: "unreachable" });
  });

  it("reports a network failure as unreachable", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(pollChatgptConnect("h1")).resolves.toEqual({ status: "unreachable" });
  });

  it("keeps a server 5xx that carries a detail as an error", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "ChatGPT sign-in is unavailable." }), { status: 503 }),
    );

    await expect(pollChatgptConnect("h1")).resolves.toEqual({
      status: "error",
      detail: "ChatGPT sign-in is unavailable.",
    });
  });

  it("reports a response body that breaks mid-read as unreachable", async () => {
    fetchMock.mockResolvedValue(new Response("{not json", { status: 200 }));

    await expect(pollChatgptConnect("h1")).resolves.toEqual({ status: "unreachable" });
  });

  it("keeps the app's error envelope as an error, not a proxy failure", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { code: "internal_error", message: "An internal error occurred." },
        }),
        { status: 500 },
      ),
    );

    await expect(pollChatgptConnect("h1")).resolves.toEqual({
      status: "error",
      detail: "An internal error occurred.",
    });
  });

  it("bounds each poll with a timeout so a hung request is retried", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "pending" }), { status: 200 }),
    );

    await pollChatgptConnect("h1");

    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });

  it("surfaces why a disconnect didn't finish", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "disconnect again to finish revoking it." }), {
        status: 502,
      }),
    );

    await expect(disconnectChatgpt()).rejects.toThrow("disconnect again");
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
