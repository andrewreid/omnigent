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
  | { status: "error"; detail?: string }
  /** No usable answer (network failure, proxy error, timeout): poll again. The
   * server answers a completed login from storage, so retrying is safe. */
  | { status: "unreachable" };

/** A poll that hangs longer than this is abandoned and retried. */
const POLL_TIMEOUT_MS = 30_000;

/** The server's error message: FastAPI ``{detail}`` or the app ``{error: {message}}``. */
async function jsonDetail(res: Response): Promise<string | undefined> {
  try {
    const body = (await res.json()) as { detail?: unknown; error?: { message?: unknown } };
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.error?.message === "string") return body.error.message;
  } catch {
    // fall through
  }
  return undefined;
}

async function detailOf(res: Response, fallback: string): Promise<string> {
  return (await jsonDetail(res)) ?? `${fallback} (${res.status}).`;
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

/** Poll a device-code login once. Never throws. */
export async function pollChatgptConnect(handle: string): Promise<ChatgptPollStatus> {
  try {
    const res = await authenticatedFetch("/v1/connections/chatgpt/device/poll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ handle }),
      signal: AbortSignal.timeout(POLL_TIMEOUT_MS),
    });
    if (res.ok) return (await res.json()) as ChatgptPollStatus;
    const detail = await jsonDetail(res);
    // Server errors carry a message; a bare 5xx came from a proxy in between.
    if (detail === undefined && res.status >= 500) return { status: "unreachable" };
    return { status: "error", detail: detail ?? `Sign-in check failed (${res.status}).` };
  } catch {
    // Network failure, timeout, or a response body that broke mid-read.
    return { status: "unreachable" };
  }
}

/** Disconnect: the server revokes the chain at OpenAI, then forgets it. */
/** Disconnect. Resolves ``false`` when the stored sign-in was unreadable and so
 * couldn't be revoked at OpenAI (it is disconnected either way). */
export async function disconnectChatgpt(): Promise<boolean> {
  const res = await authenticatedFetch("/v1/connections/chatgpt/disconnect", { method: "POST" });
  if (!res.ok) {
    throw new Error(await detailOf(res, "Couldn't disconnect ChatGPT"));
  }
  const body = (await res.json().catch(() => ({}))) as { revoked?: unknown };
  return body.revoked !== false;
}
