# Subscription broker (ChatGPT / Codex first)

> **Status:** design, not implemented. Fork-only (`andrewreid/omnigent`) until
> proven; upstreaming is a later decision.
> **Basis:** `designs/CODEX_SUBSCRIPTION_BROKER.md` (the handoff brief). This doc
> supersedes the brief where they disagree. Every such point is called out under
> "Corrections to the brief".
> **Verified against:** `main` @ `56c6a7f73` and `codex-cli 0.154.0`, plus an
> empirical spike on 2026-09-27 against a live ChatGPT Pro account (results
> below).

## Goal

Let a managed sandbox (K8s, Modal, Daytona, Islo, E2B, openshell, cwsandbox) run
the `codex` and `codex-native` harnesses on the session owner's personal ChatGPT
subscription **without any sandbox ever holding the refresh token**.

The Omnigent server owns the refresh chain. It stores it encrypted through the
existing `SecretCipher` port (KMS or Vault Transit) and vends short-lived access
tokens to sandboxes on demand.

Codex is the first provider. Claude follows in a simpler shape (Option S,
section 5): a server-held long-lived token, because Claude Code has no refresh
hook open to third-party hosts. The seams (connect-flow shapes, a lease-guarded
refreshing resolver, a runner-side broker route, broker-aware readiness) are
generic so antigravity can follow.

## Decisions

| Topic | Decision |
|---|---|
| Delivery to codex | **Path B primary:** Omnigent's app-server client logs in with `chatgptAuthTokens` and answers `account/chatgptAuthTokens/refresh` from the broker. Nothing is written to disk. **Path A (`auth.json` materializer) is opt-in and off by default.** |
| Runner → broker auth | New **runner-scoped** route `GET /v1/runners/{runner_id}/credentials/{provider}`, gated on the runner binding token. The launch token stays host-only. |
| Refresh serialization | **Lease + CAS** in the connection row's metadata. No DB lock is held across the upstream HTTP call. Works the same on SQLite, Postgres and MySQL. |
| Connect flow | A generic **device-code** step added to `connections_base` alongside the redirect flow. Only the ChatGPT provider ships. |
| Token TTL | Vend the upstream access token as-is (10-day lifetime), refresh server-side when little life remains, and document the leak window honestly. |
| Claude | **Option S:** the user pastes a `claude setup-token` token (1 year, `user:inference`); it is stored encrypted and vended into `CLAUDE_CODE_OAUTH_TOKEN` at spawn. No server-owned refresh chain. See "Claude subscriptions". |

## Spike results (2026-09-27)

Harness: `spike.py` running in a throwaway `CODEX_HOME` with
`cli_auth_credentials_store = "file"`, logged in with `codex login --device-auth`.
`OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` were stripped from every
run. No token values were logged. The chain was revoked (`codex logout`) at the end.

| # | Test | Result |
|---|---|---|
| inspect | Token shape | `auth_mode=chatgpt`; `tokens.{access_token,id_token,refresh_token,account_id}`. **access_token TTL = 864000 s (10 days)**; id_token TTL = 3600 s. The access token is a JWT with `exp`, `client_id`, `scp`, `session_id`, and `https://api.openai.com/auth` (plan type). |
| t1 | `auth.json` with the `refresh_token` **key removed** | ❌ Codex sends **no bearer at all** (`401 Missing bearer`): the token struct fails to deserialize. File not rewritten. |
| t1b | `refresh_token: ""` (or a placeholder string), plain file and symlink | ✅ Turn runs. File unchanged; symlink preserved. |
| t2 | `refresh_token: ""` with `last_refresh` 30 days old | ✅ Turn runs. No refresh attempt, no rewrite. |
| t8 | `refresh_token: ""`, **expired id_token**, valid access token | ✅ Turn runs. Only the access token matters. |
| t3 | App-server `account/login/start {type:"chatgptAuthTokens", accessToken, chatgptAccountId}` on an empty `CODEX_HOME` | ✅ `account/read` reports `chatgpt`/`pro`; turn completes; **no `auth.json` is written**. |
| t5 | Same, with a signature-corrupted access token | ✅ A 401 triggers **exactly one** `account/chatgptAuthTokens/refresh {reason:"unauthorized", previousAccountId}`; the fresh token lets the turn complete. The model-list fetch and one MCP transport fail on the bad token before the refresh. |
| rotate | Force refresh (`account/read {refreshToken:true}`) | ✅ refresh_token rotated. The laptop's own `~/.codex` chain was untouched, and a later laptop request authenticated (it failed on the model name, not auth). **Separate logins are independent chains.** |
| t6 | Pre-rotation access token used after the rotation | ✅ Still valid. **Rotation does not revoke access tokens already vended.** |
| t7 | Pre- and post-rotation access tokens after `codex logout` (revoke) | ❌ Both rejected: `Encountered invalidated oauth token for user`. **Revoke kills every access token vended from the chain.** When the refresh callback returned an error, codex retried it **14 times in ~16 s**. |

## Corrections to the brief

1. **Not hourly.** The brief derived an hourly cadence from the id_token. The
   access token lives 10 days. The laptop confirms it: `last_refresh` was 6 days
   old and still valid. So server-side refreshes are rare, and race exposure is small.
2. **Leak window is up to 10 days, not ~1 hour.** This is mitigated by t7:
   disconnect (revoke) cuts off every vended token immediately.
3. **Open Q1 is resolved.** Codex runs without a refresh token, but under Path A
   the key must be present (`""`). Path B needs no file at all.
4. **Open Q2 is resolved.** A server-side device-code login is an independent
   chain, and the operator's laptop is unaffected.
5. **The cipher is not KMS-only.** `build_secret_cipher()` selects KMS or Vault
   Transit (`OMNIGENT_CREDENTIAL_CIPHER`). "KMS-backed" here means any `SecretCipher`.
6. **The runner does not hold the launch token.** `_RUNNER_ENV_ALLOWLIST`
   (`omnigent/host/connect.py:477`) excludes `OMNIGENT_HOST_TOKEN`. So the codex
   executor, which runs in the runner, cannot use the existing host broker route.
   Hence the runner-scoped route.
7. **`SELECT … FOR UPDATE` is a poor fit.** SQLite ignores it, and holding it
   across the upstream call pins a pooled connection. Hence lease + CAS.

## Architecture

```
 Settings UI                 Omnigent server                              Sandbox
 ───────────                 ───────────────                              ───────
 Connect ChatGPT ──POST──▶ /connections/chatgpt/device/start
   show user_code ◀──────  {user_code, verification_uri, interval}
   poll ─────────POST──▶ /connections/chatgpt/device/poll ──▶ auth.openai.com
                           tokens ──▶ SecretCipher ──▶ connections row
                                              │
                                  resolve_chatgpt_credential
                                  (lease + CAS refresh when < margin)
                                              │
               ┌──────────────────────────────┴──────────────────────────┐
   GET /runners/{rid}/credentials/chatgpt              GET /hosts/{hid}/credentials/chatgpt
   (runner binding token)                              (launch token; Path A only)
               │                                                          │
   runner: codex executor / native client               host: configure_host_codex
   account/login/start chatgptAuthTokens                (opt-in) write auth.json,
   on account/chatgptAuthTokens/refresh ─▶ re-fetch     refresh_token "", 0600, atomic
```

### 1. Store: refresh lease (PR 1, built as M2)

**As built.** The primitive lives in `CredentialStore`
(`omnigent/stores/credential_store/sqlalchemy_store.py`), so every provider gets
it, and `ConnectionStore` passes it through. Lease state lives in the row's
plaintext `metadata_json` (`refresh_lease_holder`, `refresh_lease_until`,
`needs_reconnect`), so **no migration**. Every metadata change is one short
write transaction that reads the row `FOR UPDATE` (Postgres, MySQL,
CockroachDB). On SQLite the immediate write transaction serializes writers.
No lock is held across the upstream call. This replaced the earlier
text-compare CAS and `refresh_version` idea: a locked read-modify-write in a
tiny transaction is simpler and portable.

- `acquire_refresh_lease(user_id, provider, *, holder, ttl_s) -> bool`: claims the
  lease unless another holder's lease is still live. An expired lease can be
  taken over, so a crashed holder never wedges the row.
- `update_secret(..., lease_holder=holder) -> bool`: commits only if *holder*
  still owns the lease, and releases it in the same write. Fresh tokens also
  clear `needs_reconnect`. A write without a lease leaves another holder's lease
  in place.
- `release_refresh_lease(...)` and `mark_needs_reconnect(...)`. A reconnect's
  `upsert` rewrites the metadata, which clears the flag.

`omnigent/connections/refresh.py::refresh_coordinated` is the shared resolver
core (lease TTL 60 s, above the clients' 15 s HTTP timeout):

1. Read the row. If it needs a reconnect, return `None`. If it's fresh, return it.
2. Try to acquire the lease.
   - **Lost:** poll until the holder's refresh lands, then return the fresh row.
     On timeout, return the current row; the caller decides from its expiry.
   - **Won:** re-read under the lease, because another holder may have just
     committed. Call upstream **outside any transaction**, then persist through
     `update_secret(lease_holder=…)`.
3. `RefreshRejected` (GitHub `bad_refresh_token`/`invalid_grant`, Databricks
   `invalid_grant`) marks `needs_reconnect` and returns `None`, with no retries.
   Any other error is transient: return the current row.
4. It returns `Refreshed(connection, minted)`. Callers prefer `minted`, so a
   failed persist never drops a token whose predecessor is already spent.

GitHub and Databricks resolvers are retrofitted onto it. The shared `status`
endpoint reports `needs_reconnect`, and the Settings panels say "reconnect".
Tests: `tests/server/test_refresh_coordination.py`, which includes a 20-process
race asserting exactly one upstream refresh, plus a control run without the lease
showing the race is real.

**Why this is mandatory, not hygiene:** OpenAI's auth server may do refresh-token
*reuse detection* (Auth0-style). There, presenting a rotated-away refresh token
revokes the whole family, not just the loser's copy. We did not test this, to
avoid bricking the spike chain. We assume it. A lost race must never result in a
second upstream refresh.

PR 1 retrofits GitHub and Databricks onto the same resolver core. Acceptance:
a multi-process race test (N ≥ 20 processes, shared SQLite and Postgres) shows
exactly one upstream refresh call, and `invalid_grant` sets `needs_reconnect` and
does not retry.

### 2. ChatGPT provider + device-code seam (PR 2)

> **As built (M3).** The seam is `DeviceCodeHooks` plus
> `create_device_connection_router`. It exposes `POST …/device/start`, which returns
> `{user_code, verification_url, interval, expires_in, handle}`, and
> `POST …/device/poll {handle}`, which returns `pending|complete|expired|error`.
> The handle is an HS256 JWT holding `sub`, `account_generation` and the
> provider's poll state, with a 15-minute expiry. The key is
> `OMNIGENT_CHATGPT_STATE_KEY`, or a per-process fallback, which is fine for one
> replica. Protocol, from codex `rust-v0.154.0`:
> 1. `POST {issuer}/api/accounts/deviceauth/usercode {client_id}`.
> 2. Poll `…/deviceauth/token {device_auth_id,user_code}`; it returns 403/404
>    while pending.
> 3. Form-POST `{issuer}/oauth/token` with `authorization_code`, the issued
>    `code_verifier`, and `redirect_uri={issuer}/deviceauth/callback`.
>
> Refresh is a JSON POST to `/oauth/token` with `grant_type=refresh_token`.
> A 401, a 400 `invalid_grant`, or `refresh_token_expired|reused|invalidated` is
> treated as permanent (`RefreshRejected`, so `needs_reconnect`). Codex's source
> confirms OpenAI does **refresh-token reuse detection** (`refresh_token_reused`),
> so the M2 lease is required. Disconnect calls the hooks' `revoke` (JSON POST to
> `/oauth/revoke` with `token_type_hint=refresh_token`) before deleting the row.
> Requests send `User-Agent: omnigent`, not codex's `originator` header. Enable
> the flow with `OMNIGENT_CHATGPT_SUBSCRIPTION_CONNECT=1`. `OMNIGENT_CHATGPT_ISSUER`
> and `OMNIGENT_CHATGPT_CLIENT_ID` override the defaults.

**Generic seam** in `omnigent/server/routes/connections_base.py`: a second,
optional hooks protocol next to `ConnectionHooks.begin/complete`:

```python
class DeviceCodeHooks(Protocol):
    async def device_start(self, user_id: str) -> DeviceStart: ...
    #   DeviceStart(user_code, verification_uri, interval_s, expires_at, handle)
    async def device_poll(self, user_id: str, handle: str) -> DevicePoll: ...
    #   DevicePoll(status: "pending" | "complete" | "expired" | "denied")
```

`create_connection_router` mounts
`POST /connections/{provider}/device/{start,poll}` when the hooks implement it.
`handle` is the signed, user-bound state JWT already used for redirects: it
carries the upstream `device_auth_id` (non-secret), `sub` and
`account_generation`, and has a 600 s TTL. So there is no server-side pending
store and the CSRF/replay binding carries over. `status` and `disconnect` are
unchanged.

**ChatGPT provider** (mirrors GitHub):

- `omnigent/entities/chatgpt_connection.py`
- `omnigent/connections/chatgpt/__init__.py`: `ChatgptConnectionStore`
- `omnigent/server/chatgpt_oauth_client.py`: `device_start` / `device_poll` /
  `exchange` / `refresh` / `revoke` against `https://auth.openai.com`. The issuer
  and client id are config-overridable (mirroring codex's `--experimental_issuer`
  and `--experimental_client-id`). **Take the exact request/response shapes from
  openai/codex `codex-rs/login/src/device_code_auth.rs` at tag `rust-v0.154.0`**
  (open source). The binary confirms the endpoints `/deviceauth/{usercode,token,callback}`,
  `/oauth/{token,revoke}` and the fields `device_auth_id`, `user_code`,
  `authorization_code`, `code_verifier`.
- `omnigent/server/chatgpt_identity.py`: `resolve_chatgpt_credential` on the PR 1
  core. The refresh margin is **2 days**: vended tokens always have at least 2 days
  of life, and refreshes happen about every 8 days.
- `omnigent/server/routes/connections_chatgpt.py`
- `connections_registry.py`: one `ConnectionProvider("chatgpt", …)`.
- Web: `web/src/lib/chatgptIntegration.ts` plus a modal (user code, verification
  link, poll every `interval_s`); entry in the `SettingsPage.tsx` integration
  map. Copy notes that device-code login must be enabled in ChatGPT → Settings →
  Security.

Row: `provider="chatgpt"`, `account_id=""`. Secret (encrypted):
`{access_token, refresh_token, id_token, account_id}`. Metadata (plaintext, no
tokens): `{access_expires_at, plan_type, chatgpt_account_id, email,
last_refresh, needs_reconnect, refresh_lease_holder, refresh_lease_until,
refresh_version}`.

Disconnect deletes the row **and** calls `/oauth/revoke`. Per t7, that
invalidates every vended access token immediately. This is the real kill switch.

### 3. Broker routes (PR 3a)

- Existing `GET /v1/hosts/{host_id}/credentials/chatgpt` (launch token) needs no
  code change: registering the resolver is enough. It is used only by the Path A
  materializer.
- New `GET /v1/runners/{runner_id}/credentials/{provider}` in
  `runner_tunnel.py`, next to `POST /runners/{runner_id}/token`. It uses the same
  gate (`token_bound_runner_id(binding_token) == runner_id`, then
  `resolve_managed_runner_owner`) and the same resolver registry as
  `host_credentials.py`. It sets `Cache-Control: no-store`. Unlike the mint
  route, it does not need an auth provider.

Payload (never includes `refresh_token` or `id_token`):
`{connected, owner, access_token, account_id, plan_type, expires_at}`.

### 4. Delivery in the sandbox (PR 3b)

> **As built (M4), codex-native.**
> - **Spike (codex 0.154.0, 2026-09-28):**
>   - A `--remote` TUI attached to an app-server logged in with
>     `chatgptAuthTokens` skips the sign-in screen, and nothing is written to
>     `auth.json`.
>   - `account/chatgptAuthTokens/refresh` is **broadcast to every connected
>     client**, so it doesn't matter which client logged in, and the real TUI
>     doesn't answer it with an error.
>   - An error answer fails the turn immediately.
> - **Runner route:** `GET /v1/runners/{id}/credentials/{provider}`
>   (`runner_tunnel.py`) uses the binding token plus
>   `resolve_managed_runner_owner`, the same gate as the owner-token mint. It
>   vends through `host_credentials.vend_provider_credential`, shared with the
>   host route.
> - **Runner side:** `omnigent/runner/brokered_credentials.py`.
>   - When the resolved launch has `login_required` in a managed sandbox,
>     `_auto_create_codex_terminal` calls `start_brokered_chatgpt_auth(ws_url)`
>     right after the app-server starts.
>   - That opens an `omnigent-chatgpt-auth` client and logs in. The client
>     stays connected (closed through `CodexNativeAppServer.close_callbacks`)
>     and answers refresh requests from the broker.
>   - "Not connected", or the broker returning the token codex just rejected,
>     is answered with an error and cached for 60 s, so codex's ~14-retry burst
>     costs one broker call.
>   - It then clears `login_required`, so the headless sign-in fail-fast
>     doesn't trigger.
> - **Readiness:** at startup, `configure_host_chatgpt` asks the host route
>   whether ChatGPT is connected, without taking the token, and sets
>   `OMNIGENT_CHATGPT_SUBSCRIPTION_BROKERED=1`. `_codex_auth_unavailable_reason`
>   treats that as available.
> - **Not yet built:**
>   - The in-process `codex` harness (stdio app-server in `codex_executor`).
>   - The opt-in `auth.json` materializer (Path A) for running bare `codex` in
>     the Pod terminal.

**Path B (default)**, inside the runner:

- `codex` harness: `omnigent/inner/codex_executor.py`. After the `initialize`
  handshake (~L2883), when `IS_SANDBOX=1` and the runner is managed, fetch the
  runner-scoped credential. If connected, call `account/login/start
  {type:"chatgptAuthTokens", …}`. Handle the server request
  `account/chatgptAuthTokens/refresh` in the reader loop by re-fetching from the
  broker. **Refresh-storm guard:** cache a `connected:false` or error answer for
  about 60 s and return it immediately, because codex retried 14 times in 16 s.
- `codex-native` harness: the same, through Omnigent's own
  `CodexAppServerClient` (`omnigent/harnesses/codex_native/app_server.py:902`),
  which connects alongside the TUI's `--remote` connection. See open question 2
  for which client receives the refresh request.
- Put the logic in one module (e.g. `omnigent/inner/codex_brokered_auth.py`) that
  both harnesses call. Adapters only transport it.

**Path A (opt-in)** on the host: `omnigent/host/codex_credential.py`, with
`configure_host_codex` and `start_host_codex_refresh`, is modelled on
`git_credential_github.configure_host_gh` / `start_host_gh_refresh`. It
activates only when `IS_SANDBOX=1` **and** `OMNIGENT_CODEX_MATERIALIZE_AUTH=1`.
It writes `$CODEX_HOME/auth.json` as `{auth_mode:"chatgpt", OPENAI_API_KEY:null,
tokens:{access_token, id_token:<see below>, account_id, refresh_token:""},
last_refresh}` with mode 0600, via temp file plus atomic rename (the executor
symlinks this path into live sessions). The renewal interval defaults to 6 h,
because the token lives 10 days and is served with at least 2 days left.
`OMNIGENT_CODEX_REFRESH_INTERVAL_S`, when non-positive, disables renewal. It is
hooked in `omnigent/host/connect.py` next to `configure_host_gh` (~L4848–4870).
*id_token:* t8 shows an expired id_token is fine, but codex may parse its claims
for plan and email. PR 3b decides between vending the stored id_token (adding
it to the host-route payload only) and synthesizing none. Test both.

### 5. Claude subscriptions: Option S (PR 4)

**Why not the codex shape.** Claude Code 2.1.269 does have SDK-host refresh
hooks. The stream-json control requests are `oauth_token_refresh` (gated on
`CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH`) and `host_auth_token_refresh` (gated on
`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`). Both are enabled only for first-party
`CLAUDE_CODE_ENTRYPOINT` values (`claude-desktop`, `local-agent`,
`claude-vscode`). Using them means impersonating an Anthropic client, so we
don't. The other mid-session channels are also internal: a re-read token file
(`CCR_OAUTH_TOKEN_FILE`, "injected by the CCR host") and
`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`. `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`
bootstraps a login *inside* the sandbox, which makes the sandbox the chain owner.
That is the opposite of the goal. `apiKeyHelper` is probably API-key mode only,
which subscription OAuth tokens can't use (unverified). So there is no
sanctioned way to hand a running Claude process a fresh short-lived token.

**Shape.** The long-lived token from `claude setup-token` is minted by the real
CLI on the user's machine, so the server never acts as Anthropic's OAuth client.

- **Connect:** a third flow shape in `connections_base`, next to redirect and
  device code: `PasteSecretHooks.accept(user_id, secret) -> None`, mounted as
  `POST /connections/{provider}/paste`. Validate the format (`sk-ant-oat01-`
  prefix) and optionally make a probe call. The Settings modal shows the
  `claude setup-token` instruction and a password field.
- **Provider:** `omnigent/connections/claude/` with `ClaudeConnectionStore`.
  Secret: `{oauth_token}`. Metadata: `{connected_at, token_prefix_fp,
  expires_hint}`, where `expires_hint` is roughly connect time plus 1 year,
  shown in the UI. The resolver decrypts and returns the token and needs no
  lease. Registry entry `ConnectionProvider("claude", …)`.
- **Delivery (as built):** at host startup, `configure_host_claude`
  (`omnigent/host/claude_credential.py`, hooked next to `configure_host_gh`)
  fetches `GET /v1/hosts/{hid}/credentials/claude` with the launch token and
  sets `CLAUDE_CODE_OAUTH_TOKEN` in the host env. `_build_runner_env` reads
  the live host env at each runner spawn and already forwards that name
  (`HARNESS_CREDENTIAL_ENV_VARS`), so claude-sdk and claude-native get it
  through the same path a Secret-provided token takes. Readiness already
  counts the env var (`_claude_token_env_configured`). So the Claude slice
  needs **neither** the runner-scoped route nor the readiness change. Both
  move to the codex milestone, which needs them. One fetch per host launch is
  enough, because the token is long-lived.
- **Opt-in:** `OMNIGENT_CLAUDE_SUBSCRIPTION_CONNECT=1` plus a configured
  cipher. `build_claude_connection()` is the single helper both server
  entrypoints call, so they can't drift apart.
- **Precedence:** a connected owner's brokered token beats a fleet-wide
  `CLAUDE_CODE_OAUTH_TOKEN` from the harness-credentials Secret
  (`_BASE_HARNESS_CREDENTIAL_ENV_VARS`). The per-user connection is the more
  specific grant. Log which source won; never log the value.
- **Disconnect:** delete the row. Upstream revocation of a setup-token is
  unknown; the UI should tell the user to revoke it on claude.ai if that is
  possible (to verify).

**Gain over today's Secret injection:** per-user instead of one fleet-wide token;
encrypted at rest; absent from the Pod spec and etcd; disconnect stops future
vends. **Not gained:** a leaked token lives up to a year, and the refresh chain
question doesn't arise because there is no refresh token.

**Option R (deferred):** a server-owned Claude refresh chain via the paste-code
flow (`platform.claude.com/oauth/authorize` with
`/oauth/code/callback`). It needs (a) a spike on access-token lifetime and on
whether a `.credentials.json` without a `refreshToken` is re-read after
expiry or a 401, and (b) a policy answer, because the server would act as Claude
Code's OAuth client.

### 6. Harness readiness (PR 3b)

`omnigent/onboarding/harness_readiness.py` reports `needs-auth` when no *local*
credential is visible. For codex that check is
`_codex_auth_json_has_available_credential`. Under Path B nothing is local, so
a brokered sandbox would read "isn't configured" while working fine. In a
sandbox (`IS_SANDBOX=1` plus a launch token), readiness must also accept
`connected: true` from the host broker route. The probe is cheap because the
route is per-request and resolves from the DB. This covers every brokered
provider, not just codex.

## Threat model

Trust boundary: the sandbox, as today. Any in-sandbox process that can reach the
runner's binding token (Path B) or the launch token (Path A) can obtain the
current access token.

- **Refresh token never leaves the server.** A sandbox cannot mint successors.
- **Leak window: up to 10 days** (the upstream access-token lifetime). We cannot
  shorten it.
- **Kill switch: disconnect.** Revoke upstream and delete the row. Per t7, every
  vended token dies at once. Offer this in the UI and document it as the
  incident response.
- **Path B default: no token at rest in the sandbox.** The token exists in the
  runner's and app-server's memory only. It is readable by a same-uid process with
  ptrace or `/proc` access, so this is a posture improvement, not isolation.
  Path A trades that away for bare-`codex` convenience, which is why it is opt-in.
- **At rest on the server:** `SecretCipher`-encrypted and bound to the row identity
  via encryption context.
- **Out of scope:** the credential proxy (`designs/SANDBOX_CREDENTIAL_PROXY.md`,
  #4889, #7436) for true swap-on-access.

## PR plan (fork)

1. **PR 1: lease + CAS refresh core** in the credential store; retrofit GitHub
   and Databricks; `needs_reconnect`; multi-process race test.
2. **PR 2: device-code seam + ChatGPT provider + Settings UI.**
3. **PR 3a: runner-scoped broker route.** Small; can land with PR 2.
4. **PR 3b: Path B delivery** in `codex` and `codex-native`, plus the opt-in
   Path A materializer.
5. **PR 4: Claude Option S** (built first, as M1). Paste-secret flow shape,
   `claude` provider, host-side export of `CLAUDE_CODE_OAUTH_TOKEN`,
   precedence rule. Independent of every other PR.
6. **Side fix, independent:** add `CODEX_ACCESS_TOKEN` (and `CODEX_API_KEY`) to
   `_clean_codex_env`'s `allow_exact` (`omnigent/inner/codex_executor.py:656`).

End-to-end acceptance: on a `host_type: managed` session whose owner has
connected ChatGPT, a `codex-native` turn runs with no `OPENAI_API_KEY`, no
`auth.json` in any Secret and (Path B) no `auth.json` in the Pod. Disconnecting
mid-session makes the next turn fail with a clear "reconnect ChatGPT" status
rather than a retry storm.

## Open questions

1. **`chatgptAuthTokens` is marked `[UNSTABLE] FOR OPENAI INTERNAL USE ONLY - DO NOT USE`**
   in the 0.154.0 app-server schema. It works today (t3, t5), but it may change
   without notice. Mitigation: gate on the codex version, fall back to Path A
   automatically when `account/login/start` rejects the type, and cover it with an
   e2e test that runs on codex upgrades. It is also a signal for question 5.
2. **Multi-client refresh routing (codex-native).** With the TUI and Omnigent's
   client both attached, which connection receives
   `account/chatgptAuthTokens/refresh`? If it is the TUI, which can't answer it,
   Path B stalls on 401. Spike this first in PR 3b. The fallback is to log in and
   answer from Omnigent's client only, if codex routes to the logging-in
   connection.
3. **Refresh-token reuse detection:** assumed, untested. The PR 1 design is safe
   either way.
4. **Business/Enterprise workspaces:** does device-code login work, and does
   `chatgpt_account_id` select the workspace? Not tested (Pro only).
5. **Terms of service** (brief Q4) and **#7134 tension** (brief Q5): unchanged.
   Still to be raised before any upstream PR.
6. **Per-agent subscription** (#6318): out of scope. One ChatGPT connection per
   owner.
7. **#6253 / #8144** (probe-home symlink refresh) matter only to Path A now.
8. **Claude policy.** To our knowledge, Anthropic restricts third-party
   products from offering claude.ai login (unverified here). Option S, where the
   user's own CLI mints the token and Omnigent runs the real CLI, is the most
   defensible shape. Confirm before upstreaming.
9. **Claude setup-token revocation.** Can a setup-token be revoked upstream? If
   not, disconnect only stops future vends.
