# Claude subscription connections

How to turn on per-user **Claude** subscription connections. Once enabled, a
signed-in user connects their own subscription once, under **Settings → Sandbox
Integrations**. Their managed sandboxes then run Claude Code on that
subscription, with no API key and nothing to paste into each sandbox.

The connection is **off by default**.

---

## How it works

The user runs `claude setup-token` on their own machine and pastes the token
into Settings. The server stores it encrypted. When a managed sandbox host
starts, it fetches the token with its launch credential and exports
`CLAUDE_CODE_OAUTH_TOKEN` for Claude Code.

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

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `OMNIGENT_CLAUDE_SUBSCRIPTION_CONNECT` | to enable Claude | `1` shows the Claude panel and enables the paste flow. |

## Token lifetime and revocation

A sandbox holds the setup token, which lasts about a year. Disconnecting deletes
the stored token, so new sandboxes stop getting it. It doesn't invalidate the
token: sandboxes already running keep their copy until it expires.

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
