/**
 * Client for the ChatGPT subscription endpoints (``/v1/connections/chatgpt/*``).
 *
 * Device-code flow: ``startChatgptConnect`` returns a one-time code and a link;
 * the user approves on OpenAI's site from any device while the page polls
 * ``pollChatgptConnect`` with the returned handle. The server keeps the refresh
 * chain; sandboxes only ever receive short-lived access tokens.
 */

import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/chatgpt/status``. */
export interface ChatgptConnectionStatus {
  enabled: boolean;
  connected: boolean;
  connected_at: number | null;
  /** OpenAI rejected the stored refresh token; the user must reconnect. */
  needs_reconnect?: boolean;
  /** Subscription plan, e.g. ``"pro"``. */
  plan_type: string | null;
  email: string | null;
}

/** Shape of ``POST /v1/connections/chatgpt/device/start``. */
export interface ChatgptDeviceStart {
  user_code: string;
  verification_url: string;
  /** Seconds between polls. */
  interval: number;
  /** Seconds until the code expires. */
  expires_in: number;
  /** Opaque, user-bound handle to poll with. */
  handle: string;
}

export type ChatgptPollStatus =
  | { status: "pending" }
  | { status: "complete" }
  | { status: "expired" }
  | { status: "error"; detail?: string };

async function detailOf(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // fall through
  }
  return `${fallback} (${res.status}).`;
}

/** Fetch the current user's ChatGPT connection status. */
export async function fetchChatgptStatus(): Promise<ChatgptConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/chatgpt/status");
  if (!res.ok) {
    throw new Error(`ChatGPT status failed: ${res.status}`);
  }
  return (await res.json()) as ChatgptConnectionStatus;
}

/** Start a device-code login. Throws with a user-facing message on failure. */
export async function startChatgptConnect(): Promise<ChatgptDeviceStart> {
  const res = await authenticatedFetch("/v1/connections/chatgpt/device/start", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(await detailOf(res, "Couldn't start the ChatGPT sign-in"));
  }
  return (await res.json()) as ChatgptDeviceStart;
}

/** Poll a device-code login once. */
export async function pollChatgptConnect(handle: string): Promise<ChatgptPollStatus> {
  const res = await authenticatedFetch("/v1/connections/chatgpt/device/poll", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handle }),
  });
  if (!res.ok) {
    return { status: "error", detail: await detailOf(res, "Sign-in check failed") };
  }
  return (await res.json()) as ChatgptPollStatus;
}

/** Disconnect: the server revokes the chain at OpenAI, then forgets it. */
export async function disconnectChatgpt(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/chatgpt/disconnect", { method: "POST" });
  if (!res.ok) {
    throw new Error(`ChatGPT disconnect failed: ${res.status}`);
  }
}
