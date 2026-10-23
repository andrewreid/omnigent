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
a proxy cuts off, and the server answers a completed sign-in idempotently. A
retry that arrives while the cut-off poll is still saving waits up to 10 seconds
for that save; if the save takes longer, the sign-in fails and the user starts
again.

## Token lifetimes and revocation

| | Claude | ChatGPT |
|---|---|---|
| What a sandbox holds | the setup token (about 1 year) | an access token (about 10 days; refreshed when under 2 days remain) |
| Disconnect | Deletes the stored token, so new sandboxes stop getting it. It doesn't invalidate the token: sandboxes already running keep their copy until it expires. | Stops vending, waits up to 60 seconds for a refresh already in progress to save its result, revokes the newest refresh chain at OpenAI (which also invalidated the access tokens handed out from it when we tested), then deletes the connection. A reconnect made while this runs is left alone. |
| If revoke fails | n/a | The connection stays flagged (nothing more is vended) and is kept so it can still be revoked; the user is asked to disconnect again (`502`). |
| If the stored sign-in is unreadable | n/a | It is disconnected, but nothing could be revoked at OpenAI; the response says `"revoked": false` and the panel says so. |

## When something goes wrong

- **"No longer accepts the saved sign-in. Disconnect, then connect again."**
  The provider refused the refresh token (the chain was revoked, rotated
  elsewhere, or reused), or a refresh ended without a known outcome (its
  answer was lost, its result couldn't be stored, or the server died
  mid-refresh). Rotating providers spend the old refresh token on use, so the
  broker fails closed rather than risk sending it twice. Reconnecting fixes it.
  This applies to the GitHub and Databricks connections too.
- **A sandbox's codex gets a 401 before its token expires.** The runner reports
  the refused token, as a digest, and the server refreshes it once. If the chain
  can't be refreshed, codex is told to reconnect.
- **The sandbox was torn down.** Its runner can no longer fetch credentials,
  even with a leftover binding token: vending requires the session to still be
  on a live, server-managed host.

## Upgrading and rolling back

Upgrade every server replica together; don't run old and new servers side by
side.

- Older servers don't follow the in-flight refresh marker or the connection
  generation, so a mixed fleet can refresh one chain twice.
- Older servers can't read secrets sealed with the KMS envelope (the
  `kmsenv1:` prefix, used for secrets over 4 KB). After rolling back, any such
  connection reads as disconnected until the user reconnects. Smaller secrets
  keep the format older servers read.
- No schema migration is involved; the new state lives in the existing
  connection metadata, which older servers ignore.

## Compatibility

The ChatGPT delivery uses the Codex app-server's `chatgptAuthTokens` login and
answers its `account/chatgptAuthTokens/refresh` requests. Codex marks that
interface unstable and internal. It was verified end to end with `codex-cli`
0.158.0. A runner whose codex is older than 0.154.0 keeps codex's own login, and
the brokered login gives up after 30 seconds if the app-server doesn't answer.
Pin the Codex version in your host image, and re-verify after upgrading.
