# Claude and ChatGPT subscription connections

How to turn on per-user **Claude** and **ChatGPT** subscription connections.
Once enabled, a signed-in user connects their own subscription once, under
**Settings → Sandbox Integrations**. Their managed sandboxes then run Claude Code
and Codex on that subscription, with no API key and nothing to paste into each
sandbox.

Both connections are **off by default**. The design, and the decisions behind it,
are in `designs/SUBSCRIPTION_BROKER.md`.

---

## How it works

- **Claude.** The user runs `claude setup-token` on their own machine and pastes
  the token into Settings. The server stores it encrypted. When a managed
  sandbox host starts, it fetches the token with its launch credential and
  exports `CLAUDE_CODE_OAUTH_TOKEN` for Claude Code.
- **ChatGPT.** The user clicks **Connect ChatGPT**, enters the one-time code on
  OpenAI's page, and approves it. The server keeps the refresh chain (encrypted)
  and refreshes it server-side. A managed `codex-native` runner fetches a
  short-lived access token from `GET /v1/runners/{id}/credentials/chatgpt`. It
  signs codex in through the app-server's `chatgptAuthTokens` login, so nothing
  credential-shaped is written to disk, and it answers codex's token-refresh
  requests from the broker.
- **What sandboxes never get:** the ChatGPT refresh token. Only access tokens
  leave the server.

## Requirements

1. **The credential store.** Connections are encrypted with the credential
   store's cipher. Set `OMNIGENT_CREDENTIAL_KMS_KEY_ID` (AWS KMS) or configure
   Vault; see `designs/CREDENTIAL_STORE.md`. With KMS, the server's IAM
   principal needs these on that key:
   - `kms:Encrypt`
   - `kms:Decrypt`
   - `kms:GenerateDataKey`, for secrets over KMS's 4 KB direct-encrypt limit.
2. **Managed sandboxes.** The credentials are delivered to server-managed
   sandbox hosts, configured through the `sandbox:` section of the server
   config. Sessions on a user's own registered machine keep using that
   machine's local logins.
3. **ChatGPT:** device-code sign-in must be enabled for the user's account,
   under ChatGPT → Settings → Security. Only the native Codex harness
   (`codex-native`) uses the brokered login. The in-process `codex` harness
   still needs its own login.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `OMNIGENT_CLAUDE_SUBSCRIPTION_CONNECT` | to enable Claude | `1` shows the Claude panel and enables the paste flow. |
| `OMNIGENT_CHATGPT_SUBSCRIPTION_CONNECT` | to enable ChatGPT | `1` shows the ChatGPT panel and enables device-code sign-in. |
| `OMNIGENT_CHATGPT_STATE_KEY` | strongly recommended | The secret that signs the 15-minute sign-in handle. It must be at least 32 characters. If unset, each server process uses a random key, so a sign-in only works if every poll reaches the process that started it, and a restart cancels sign-ins in progress. **Set it whenever you run more than one worker or replica.** |
| `OMNIGENT_CHATGPT_ISSUER`, `OMNIGENT_CHATGPT_CLIENT_ID` | no | Overrides for the auth service and OAuth client. The defaults match the Codex CLI. |

Generate a state key with:

```bash
openssl rand -base64 32
```

Rotating the state key only cancels sign-ins that are in progress. Existing
connections are unaffected.

**Proxies:** once the user approves, OpenAI can take about 13 seconds to answer.
The poll that completes the sign-in can then run for over 20 seconds, including
a short wait for a racing save. Give the Omnigent route a request timeout of at
least 60 seconds. Envoy's default is 15 seconds. The web UI retries a poll that
a proxy cuts off, and the server answers a completed sign-in idempotently, so a
shorter timeout costs a retry, not the connection.

## Token lifetimes and revocation

| | Claude | ChatGPT |
|---|---|---|
| What a sandbox holds | the setup token (about 1 year) | an access token (about 10 days; refreshed when under 2 days remain) |
| Disconnect | Deletes the stored token, so new sandboxes stop getting it. It doesn't invalidate the token: sandboxes already running keep their copy until it expires. | Revokes the refresh chain at OpenAI, which also invalidates the access tokens already handed out, then deletes the connection. |
| If revoke fails | n/a | The connection stops being vended at once, is kept so it can still be revoked, and the user is asked to disconnect again (`502`). |

## When something goes wrong

- **"No longer accepts the saved sign-in. Disconnect, then connect again."**
  The provider refused the refresh token: the chain was revoked, rotated
  elsewhere, or reused. The same state is set, failing closed, if a refreshed
  token could not be stored, because the old refresh token is then spent.
  Reconnecting fixes it.
- **A sandbox's codex gets a 401 before its token expires.** The runner reports
  the refused token, as a digest, and the server refreshes it once. If the chain
  can't be refreshed, codex is told to reconnect.
- **The sandbox was torn down.** Its runner can no longer fetch credentials,
  even with a leftover binding token: vending requires the session to still be
  on a live, server-managed host.

## Compatibility

The ChatGPT delivery uses the Codex app-server's `chatgptAuthTokens` login and
answers its `account/chatgptAuthTokens/refresh` requests. Codex marks that
interface unstable and internal. It was verified end to end with `codex-cli`
0.158.0. Pin the Codex version in your host image, and re-verify after
upgrading.
