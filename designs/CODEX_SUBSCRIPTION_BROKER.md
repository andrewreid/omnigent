# ChatGPT / Codex subscription broker for managed sandboxes

> **Status:** proposal, not implemented. Written as a handoff brief.
> **Verified against:** `origin/main` @ `56c6a7f73` (2026-09-26) and
> `codex-cli 0.154.0`. Every claim below marked **[verified]** was checked
> against that tree or that binary; claims marked **[unverified]** are
> assumptions the implementer must settle first.
>
> **Read this first:** do not work from a stale checkout. The working tree this
> brief was written in sat on a fork branch from 2026-07-30 and disagreed with
> `main` on several load-bearing details (the credential store did not exist
> there at all). Start with `git fetch origin && git checkout -b <branch>
> origin/main`.

## Problem

A managed sandbox (Kubernetes runner Pod, Modal sandbox, Daytona, Islo, E2B,
openshell, cwsandbox) cannot use a personal ChatGPT subscription (Plus / Pro) to
drive the `codex-native` harness. Today's documented answers are:

- **Claude subscription:** works. `claude setup-token` yields a long-lived token
  injected as `CLAUDE_CODE_OAUTH_TOKEN` via the harness-credentials Secret.
- **ChatGPT Business / Enterprise:** documented as `CODEX_ACCESS_TOKEN`, but see
  "Separable side fixes" — the variable never reaches the codex subprocess.
- **ChatGPT Plus / Pro:** no path. `deploy/modal/README.md` states it outright:
  codex stores personal-plan auth in `~/.codex/auth.json` with effectively
  single-use refresh tokens, so copies across machines invalidate each other.

The blocker is not transport — it is **ownership of the refresh chain**. Shipping
`auth.json` in a Kubernetes Secret makes every Pod a *writer* of one rotating
OAuth chain. The first Pod to refresh invalidates every other copy, plus the
operator's own laptop.

**Measured constraint [verified]** — decoded from a live personal `auth.json`
(structure and non-secret `id_token` claims only):

```
{auth_mode, OPENAI_API_KEY, tokens: {id_token, access_token, refresh_token, account_id}, last_refresh}
```

`chatgpt_plan_type: "pro"`, `id_token` `exp - iat = 3600`. So: **hourly**
refresh cadence, refresh token rotates on use.

## Design

Invert the writer relationship. **The server becomes the sole owner of the
refresh chain.** Sandboxes are read-only consumers of derived, ~1-hour access
tokens and never hold a refresh token.

This is not new infrastructure. `origin/main` already has every layer except one.

```
  Omnigent UI (Settings)                    Omnigent server                     Pod / sandbox
  ──────────────────────                    ───────────────                      ─────────────
  "Connect ChatGPT"  ──────────────────▶  POST /deviceauth/usercode
                     ◀── user_code + URL
  user approves in browser
  (any device)                             poll /deviceauth/token
                                           tokens ──▶ KMS encrypt ──▶ connections row
                                                                 │
                                                                 │  GET /v1/hosts/{id}/credentials/chatgpt
                                                                 │  (auth: launch token)
                                                                 ▼
                                           refresh-if-near-expiry
                                           persist rotation (row-locked)
                                           vend {access_token, id_token,
                                                 account_id, expires_at}  ──▶  write ~/.codex/auth.json
                                                                               (NO refresh_token)
                                                                               renew on a timer
```

## What already exists (reuse, do not rebuild)

| Layer | Path on `origin/main` | Notes |
|---|---|---|
| Generic connections table | `omnigent/db/migrations/versions/ga1b2c3d4e5f_add_connections.py` | PK `(workspace_id, user_id, provider, account_id)`. `provider` is a PK **column** — a new provider needs **no migration**. **[verified]** |
| KMS cipher | `omnigent/stores/credential_store/secret_cipher.py`, `vault_cipher.py`, `sqlalchemy_store.py` | `SecretCipher` port; `build_secret_cipher()` reads `OMNIGENT_CREDENTIAL_KMS_KEY_ID`. Ciphertext bound to row identity as KMS encryption context. No non-KMS fallback. |
| Shared OAuth connect flow | `omnigent/server/routes/connections_base.py` | Owns signed user-bound `state` JWT (600s TTL, HS256), CSRF/replay rebinding, `return_to` sanitising, and the four `/connections/{provider}/{connect,callback,status,disconnect}` endpoints. |
| Provider registry | `omnigent/server/connections_registry.py` | One `ConnectionProvider(name, client_factory, router_factory, credential_resolver)` entry. `create_app` iterates it to set `app.state.<name>_{config,store,client}` and mount the router. |
| Refresh-and-persist resolver | `omnigent/server/github_identity.py` | **The template.** `resolve_access_token`: read row → refresh within a 300s margin → persist rotation → return access token. Non-raising; degrades to `{"connected": false}`. |
| Store façade | `omnigent/connections/github/__init__.py` | `GithubConnectionStore(ConnectionStore[GithubConnection])` with `_secret`/`_metadata`/`upsert`/`update_tokens`. Mirror this. |
| Host-facing broker | `omnigent/server/routes/host_credentials.py` | `GET /hosts/{host_id}/credentials/{provider}`, authenticated by the **launch token** (`X-Omnigent-Host-Token`), `Cache-Control: no-store`, re-resolved per request, stops vending at token expiry / host teardown. Landed in #6330. |
| In-sandbox materializer + renewer | `omnigent/git_credential_github.py` | **The other template.** `_in_sandbox()` gated on `IS_SANDBOX=1`; `configure_host_gh()` materializes the brokered token into gh's `hosts.yml` at launch; `start_host_gh_refresh()` re-writes it on a daemon-thread interval (30 min default, env-overridable) ahead of the ~8h expiry. A second instance of the same pattern: `omnigent/host/databricks_credential.py` → `configure_host_databricks`. |
| Host startup hook | `omnigent/host/connect.py` ~L4848–4870 | Where `configure_host_git` / `configure_host_gh` / `start_host_gh_refresh` / `configure_host_databricks` are called. **Provider-agnostic** — so a hook added here reaches *every* sandbox provider, not just Kubernetes. |
| Config vocabulary | `omnigent/onboarding/configure_models.py` → `build_subscription_provider_entry(cli)` | Already mints `{"kind": "subscription", "cli": "claude"|"codex"}`. The word exists; the durable store behind it does not. |
| Codex auth.json plumbing | `omnigent/inner/codex_executor.py` L164 | `_CODEX_HOME_SYMLINK_FILES = ("auth.json", ".credentials.json", "memories_1.sqlite")` — the per-session `CODEX_HOME` **symlinks** the real `auth.json` so refreshes propagate into running sessions. A host-side rewrite therefore reaches live sessions for free. **[verified]** |

## What is new

1. **A ChatGPT connection provider** — entity, store façade, hooks, registry
   entry, resolver. Mechanical; mirror GitHub.
2. **A device-code connect flow** — does *not* fit the existing
   `ConnectionHooks.begin() -> ConnectStart(authorize_url)` Protocol, which
   assumes a browser redirect. See "Why device code" and "Open question 1".
3. **`omnigent/host/codex_credential.py`** — `configure_host_codex(server_url,
   host_id)` + `start_host_codex_refresh(...)`, modelled line-for-line on
   `configure_host_gh` / `start_host_gh_refresh`, writing `~/.codex/auth.json`.
   Hook both into `omnigent/host/connect.py` beside the existing three.
4. **Row-level refresh serialization** in the credential store. See
   "Concurrency".

## Why device code, not a redirect

The GitHub connector works because the operator **registers their own GitHub
App** with their own callback: `omnigent/server/github_app.py` takes
`client_id`, `client_secret`, `redirect_uri`, documented as "OAuth callback URL
registered on the App". You own the client.

There is no equivalent for ChatGPT subscription entitlement. OpenAI's platform
OAuth grants API billing, not subscription access. The client that carries
subscription entitlement is codex's own first-party client
(`app_EMoamEEZ73f0CkXaXp7hrann` in the 0.154.0 binary **[verified]**), pinned to
a **loopback** redirect (`/auth/callback` on 127.0.0.1). A server callback at
`https://<your-host>/v1/connections/chatgpt/callback` is not a registered
redirect URI for that client, so the authorize leg would be rejected.

Device code sidesteps redirect registration entirely, and codex already ships
it **[verified]** — strings in the 0.154.0 binary:

- `login/src/device_code_auth.rs`
- flag `--device-auth` (`USE_DEVICE_CODE`), plus `--experimental_issuer`
  (`ISSUER_BASE_URL`) and `--experimental_client-id` (`CLIENT_ID`) overrides
- endpoints `/deviceauth/usercode`, `/deviceauth/callback`, `/deviceauth/token`,
  `/codex/device` under `https://auth.openai.com`
- payload fields `device_auth_id`, `user_code`, struct `UserCodeResp`
- auth-mode variants `ApiKey`, `Chatgpt`, `ChatgptDeviceCode`,
  `ChatgptAuthTokens`, `AmazonBedrock`

**Critical property:** a *fresh* login mints an *independent* refresh chain, so a
server-side device-code login leaves the operator's laptop chain untouched.
Importing an existing `auth.json` instead hands over the single chain and logs
the laptop out on the next rotation. **Do not build the import path as the
primary flow.** **[unverified — confirm empirically that two concurrent codex
logins on one account coexist without invalidating each other.]**

Device-code login must first be enabled in ChatGPT → Settings → Security.

## Data model

No migration. One row in `connections`, `provider = "chatgpt"`, `account_id = ""`.

`secret_enc` (KMS ciphertext of a JSON object):

```json
{"access_token": "...", "refresh_token": "...", "id_token": "...", "account_id": "..."}
```

`metadata_json` (non-secret, all available from `id_token` claims **[verified]**):

```json
{"expires_at": 1790000000, "plan_type": "pro", "chatgpt_account_id": "...",
 "chatgpt_user_id": "...", "email": "...", "auth_mode": "chatgpt",
 "last_refresh": "2026-09-27T...", "refresh_version": 7}
```

`refresh_version` is the compare-and-swap counter (see Concurrency). Keep every
token out of `metadata_json` — it is stored plaintext.

## Broker payload

`GET /v1/hosts/{host_id}/credentials/chatgpt` returns:

```json
{"connected": true, "owner": "andrew@example.com", "access_token": "...",
 "id_token": "...", "account_id": "...", "expires_at": 1790000000,
 "auth_mode": "chatgpt"}
```

**Never** include `refresh_token`. That single omission is the whole security
delta over today's Secret-injection approach.

## Concurrency (do not skip this)

Two Pods hitting the broker within the refresh margin will both refresh, and the
loser's rotated token is dead. Prior art: **PR #5395** (open, XL) fights exactly
this for Databricks — but with a *machine-local* lock, which is useless across
server replicas.

Required, at the store layer:

1. `SELECT … FOR UPDATE` on the connection row for the read-modify-write, **or**
   an optimistic `refresh_version` compare-and-swap with one retry.
2. A **coalescing short-circuit**: if `metadata_json.last_refresh` is within the
   last N seconds (suggest 60), re-read and return the stored access token
   instead of refreshing again.
3. On a `400 invalid_grant` from the token endpoint, mark the connection
   `needs_reconnect` in `metadata_json` and return `{"connected": false}`. Do not
   retry-storm; surface "reconnect ChatGPT" in the UI. Mirror the design note in
   `designs/CREDENTIAL_STORE.md`: a rotated-away credential degrades to
   "reconnect", never a 500.

Write a real multi-process race test. #5395 set the bar with a 20-process race
proving a single authentication call.

## Implementation plan

Land as **three PRs**, in order. PR 1 has standalone value and de-risks the rest.

### PR 1 — Row-level refresh serialization in the credential store

Touches `omnigent/stores/credential_store/*` and `omnigent/connections/*`. Adds
the row lock / CAS, the coalescing window, and the `needs_reconnect` state to the
*existing* GitHub and Databricks resolvers. No new provider.

Acceptance: a multi-process race test shows exactly one refresh call; an
`invalid_grant` marks `needs_reconnect` and does not retry.

### PR 2 — ChatGPT connection provider + device-code flow

New files:

- `omnigent/entities/chatgpt_connection.py`
- `omnigent/connections/chatgpt/__init__.py` (`ChatgptConnectionStore`)
- `omnigent/server/chatgpt_oauth_client.py` (device-code start / poll / refresh
  against `https://auth.openai.com/deviceauth/*`; issuer + client id both
  config-overridable, mirroring codex's `--experimental_*` flags)
- `omnigent/server/chatgpt_identity.py` (`resolve_chatgpt_credential`, modelled
  on `github_identity.py`)
- `omnigent/server/routes/connections_chatgpt.py`

Edits:

- `omnigent/server/connections_registry.py` — one `ConnectionProvider` entry
- `web/src/lib/chatgptIntegration.ts` — new; **not** a copy of
  `githubIntegration.ts`, because there is no full-page redirect. Needs a modal
  that displays `user_code` + verification URL and polls a status endpoint.
- `web/src/pages/SettingsPage.tsx` — add to the integration-control map
  (~L1112, alongside `github:`)

Acceptance: a user can connect from Settings; the row lands KMS-encrypted;
`GET /v1/connections/chatgpt/status` reports `connected` with plan type and no
token material; disconnect deletes the row and revokes upstream via
`https://auth.openai.com/oauth/revoke`.

### PR 3 — In-sandbox materializer

New: `omnigent/host/codex_credential.py` with `configure_host_codex` and
`start_host_codex_refresh`. Both **no-ops unless `IS_SANDBOX=1`** — a local
`omnigent host` shares the developer's real `~/.codex`, so an auto-apply there
would clobber their own login. Copy that guard verbatim from
`git_credential_github._in_sandbox`.

Write `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) with `auth_mode`,
`tokens.{access_token, id_token, account_id}`, `last_refresh`, and **no**
`refresh_token`. Mode `0600`. Write via temp file + atomic rename — the executor
symlinks this path into every live session's private `CODEX_HOME`, so a partial
write is visible to a running agent.

Renewal interval must sit well under the 3600s expiry; suggest 1500s, env
override `OMNIGENT_CODEX_REFRESH_INTERVAL_S`, non-positive disables.

Hook both into `omnigent/host/connect.py` beside `configure_host_gh` /
`start_host_gh_refresh` (~L4848–4870).

Acceptance: a `host_type: managed` session on a connected owner runs a
`codex-native` turn with no `OPENAI_API_KEY` and no `auth.json` in any Secret;
the session survives past 60 minutes (proves renewal); `kubectl exec … -- env`
shows no refresh token anywhere in the Pod.

## Threat model

Unchanged in shape from the existing broker, and `host_credentials.py` already
documents it: **the trust boundary is the sandbox, not the endpoint.** Any
in-sandbox process that can read the launch token can call the broker and obtain
the vended credential for its TTL. Teardown stops future vends; it cannot revoke
an already-vended token.

What this design does buy over Secret injection:

- The **refresh token never enters the sandbox**, so a leak is capped at ~1 hour
  and cannot be used to mint successors.
- Revocation is a real operation: disconnect deletes the row and revokes
  upstream; every Pod loses access at its next renewal.
- The secret at rest is KMS-encrypted and bound to the row identity, with
  CloudTrail on every decrypt.

What it does **not** fix: the 1-hour access token is readable from
agent-controlled shell inside the Pod. That is the open complaint in **#4889**.
The real fix is the credential proxy
(`designs/SANDBOX_CREDENTIAL_PROXY.md`, swap-on-access — nothing
credential-shaped in the sandbox), but codex-native runs `sandbox: none` and the
proxy requires a network-isolating backend (`linux_bwrap` / `darwin_seatbelt`).
Out of scope here; note it in the PR and link #4889 and #7436.

## Open questions — settle before writing code

1. **Does codex accept a refresh-token-free `auth.json`, and under which
   `auth_mode`?** **[unverified, and this gates everything.]** Attempts to seed
   synthetic tokens via `codex login --with-access-token` were rejected at JWT
   validation ("agent identity JWT payload is not valid base64url" / "not valid
   JSON"), and that flag looks specific to the enterprise agent token rather
   than a personal access token. Test with a real token in a throwaway
   `CODEX_HOME` before anything else. If codex demands a refresh token, or
   rewrites `auth.json` and clobbers the host's copy, this design collapses and
   the answer becomes the credential proxy instead.
2. **Does the device-code flow yield a chain independent of other logins?**
   **[unverified.]** See "Why device code".
3. **Client id.** The flow runs under codex's first-party client unless OpenAI
   issues one. `--experimental_client-id` shows codex contemplates a custom
   client; whether OpenAI grants one to a third-party server is a question to
   ask them, not to assume.
4. **Terms of service.** Upstream documents personal-plan injection as
   non-injectable *"by design"* (see #2127), which reads like a policy call, not
   only a technical one. One human's own subscription refreshed server-side and
   fanned out to N concurrent Pods may still read as seat circumvention. **Ask
   maintainers before writing an XL PR.**
5. **Design tension with #7134** (fleet config control plane), which is built on
   the explicit principle that *secrets stay host-side and are never serialized
   to the server* — the opposite of a broker. Reconcile or the proposal reads as
   contradicting an accepted direction.

## Generalization (the reason this is worth doing)

Because the materializer hooks into `omnigent/host/connect.py`, which is
provider-agnostic, **every** managed-sandbox provider inherits it: Kubernetes,
Modal, Daytona, Islo, E2B, openshell, cwsandbox.

The same three-layer shape (connection provider → refreshing resolver →
in-sandbox materializer) then covers:

- **Claude subscription**, strictly better than a long-lived
  `CLAUDE_CODE_OAUTH_TOKEN` sitting in a Kubernetes Secret.
- **antigravity** `oauth_creds.json` + installation id — **closes #2127**, whose
  author proposes the same materializer shape and flags the same concurrency
  unknown.

Say this explicitly in the issue: the ask is a *generic subscription broker*,
with codex as its first provider, not a codex special case.

## Separable side fixes (land independently of all the above)

1. **`CODEX_ACCESS_TOKEN` never reaches the codex subprocess.** **[verified on
   `origin/main`]** `_clean_codex_env` (`omnigent/inner/codex_executor.py` L656)
   calls `clean_agent_env(allow_prefixes=("OPENAI_", "REQUESTS_", "CODEX_HOME"),
   allow_exact=(…))`. `CODEX_ACCESS_TOKEN` matches no prefix
   (`"CODEX_ACCESS_TOKEN".startswith("CODEX_HOME")` is `False`), is not in
   `allow_exact`, and is not in `agent_env.BASE_ALLOW_EXACT`. Meanwhile
   `omnigent/host/connect.py` forwards it host→runner in
   `_BASE_HARNESS_CREDENTIAL_ENV_VARS`, and `deploy/modal/README.md` plus
   `deploy/kubernetes/overlays/sandbox-runners/README.md` both document it as
   the ChatGPT Business/Enterprise sandbox credential. codex-cli 0.154.0 *does*
   read it (7 occurrences in the binary; `CODEX_API_KEY` likewise, also
   dropped). Net: the documented Business/Enterprise managed-sandbox path cannot
   work. Fix is one entry in `allow_exact`. No upstream issue exists — searched
   issues and PRs for `CODEX_ACCESS_TOKEN`, `store_secret`, `subscription`,
   `credential broker`, `auth.json`.
   - Workaround available today with no patch: declare it in the agent spec's
     `os_env.sandbox.env_passthrough`, which `agent_env` documents as the escape
     hatch and which reaches `_clean_codex_env` as `extra_allow`.
2. **Stale auth guidance in the sandbox-runners overlay README.** Its "Server
   auth (managed hosts)" section says the built-in `accounts` provider cannot
   support the managed runner dial-back and directs operators to header/OIDC
   *proxy* auth. `origin/main` disagrees: `UnifiedAuthProvider.mint_runner_token`
   (`omnigent/server/auth.py` L581; L395 is the abstract default) signs an HS256 owner JWT for **both** `oidc`
   and `accounts`, and `omnigent/runner/_entry.py` L738
   (`_make_managed_mint_factory`) mints it from `POST /v1/runners/{id}/token`
   using the binding token alone, then re-mints before expiry. Landed via #360
   and #1869. **[verified]**

## Upstream references

| Ref | Relevance |
|---|---|
| #4889 (open) | Subscription auth without exposing reusable credentials. No maintainer reply. This design is a partial answer — cross-link, do not duplicate. |
| #2127 (open) | antigravity credential seeding for managed sandboxes. **Subsumed** by a generic broker. Proposes the same materializer shape; states the "codex personal-plan non-injectable by design" framing. |
| #5395 (open, XL) | Serialize Databricks credential refreshes. The concurrency problem, machine-local lock only. |
| #6330 (merged) | Executor-agnostic, provider-generic credential broker. The foundation. |
| #3088 (merged) | `POST /v1/hosts/{id}/harnesses/{harness}/credential` + `host.store_secret` frame. `kind: key|gateway|adopt`, server is a non-persisting pass-through. A `subscription` kind here is an alternative surface — but it is a one-shot write to a long-lived host, so it cannot own a refresh chain. |
| #583 (open) | Per-session Claude account via `CLAUDE_CONFIG_DIR` profiles. Multi-subscription, but local host, no store, no refresh ownership. |
| #7436 (open) | AWS SigV4 credential proxy for secretless sandboxes. The phase-2 shape. |
| #1421 (open, XL) | Non-HTTP credential broker for sandboxed tools / terminals / MCP. |
| #8302 (open) | Azure Key Vault / Managed HSM as a third `SecretCipher`. Confirms the cipher port is the extension seam. |
| #6253, #8144 (open) | Competing fixes to codex probe-home credential symlink refresh. Both touch the `auth.json` symlink machinery PR 3 depends on — conflict risk. |
| #7134 (open) | Fleet config control plane. Opposing principle (secrets stay host-side). |
| #6318 (open) | K8s provider has no per-agent seam; `secret_name` is fleet-wide. Relevant if one agent should use a different subscription. Also notes `main` accepts `secret_mounts`, `pod_ready_timeout_s`, `runtime_class`. |
| #3299 (open) | Documents `ghcr.io/omnigent-ai/omnigent-host:latest` lagging `main` (0.6.0, built 2026-07-21, at filing) and the misleading 504 it causes. Relevant to testing PR 3: pin a self-built host image. |

## Verification commands used

Re-run these to confirm the **[verified]** claims rather than trusting this doc.

```sh
git fetch origin && git log -1 --format='%h %ci' origin/main

# codex env filter still drops CODEX_ACCESS_TOKEN
git show origin/main:omnigent/inner/codex_executor.py | sed -n '656,700p'
git show origin/main:omnigent/inner/agent_env.py | grep -n 'BASE_ALLOW_EXACT' -A22

# auth.json is symlinked into each session's private CODEX_HOME
git show origin/main:omnigent/inner/codex_executor.py | sed -n '164p'

# the materializer + renewer template, and its host hook
git show origin/main:omnigent/git_credential_github.py | grep -n '^def '
git show origin/main:omnigent/host/connect.py | sed -n '4845,4872p'

# the refresh-and-persist resolver template
git show origin/main:omnigent/server/github_identity.py

# codex device-code flow support (adjust the cask path to your install)
B=$(readlink -f "$(command -v codex)")
strings -a "$B" | grep -oE '/deviceauth/[a-z]+' | sort -u
strings -a "$B" | grep -c CODEX_ACCESS_TOKEN
```
