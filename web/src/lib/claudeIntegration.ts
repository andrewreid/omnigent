/**
 * Client for the Claude subscription endpoints (``/v1/connections/claude/*``).
 *
 * The user pastes the long-lived token ``claude setup-token`` prints; the
 * server stores it encrypted and vends it to their managed sandboxes as
 * ``CLAUDE_CODE_OAUTH_TOKEN``. No redirect: status, submit and disconnect are
 * all JSON.
 */

import { authenticatedFetch } from "./identity";

/** Shape of ``GET /v1/connections/claude/status``. */
export interface ClaudeConnectionStatus {
  /** Whether the Claude subscription connection is enabled on the server. */
  enabled: boolean;
  /** Whether the current user has stored a token. */
  connected: boolean;
  /** Non-secret hint, e.g. ``"sk-ant-oat01-…a1b2"``, or null. */
  token_hint: string | null;
  /** Unix epoch seconds the token was first stored, or null. */
  connected_at: number | null;
}

/** Fetch the current user's Claude connection status. */
export async function fetchClaudeStatus(): Promise<ClaudeConnectionStatus> {
  const res = await authenticatedFetch("/v1/connections/claude/status");
  if (!res.ok) {
    throw new Error(`Claude status failed: ${res.status}`);
  }
  return (await res.json()) as ClaudeConnectionStatus;
}

/**
 * Store *token* for the current user. Resolves to ``null`` on success, or the
 * server's user-facing rejection message (e.g. not a setup token).
 */
export async function submitClaudeToken(token: string): Promise<string | null> {
  const res = await authenticatedFetch("/v1/connections/claude/secret", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret: token }),
  });
  if (res.ok) return null;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // fall through to the generic message
  }
  return `Couldn't save the token (${res.status}).`;
}

/** Remove the current user's stored Claude token. */
export async function disconnectClaude(): Promise<void> {
  const res = await authenticatedFetch("/v1/connections/claude/disconnect", {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`Claude disconnect failed: ${res.status}`);
  }
}
