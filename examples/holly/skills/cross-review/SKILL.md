---
name: cross-review
description: The review lifecycle - gates, an INDEPENDENT different-vendor reviewer, the fix loop, servicing an external review bot, and releasing the branch. Review runs on the local branch diff BEFORE anything reaches the remote.
user-invocable: false
---

# cross-review — independent verification before the remote

**Audience: Holly ONLY.** This is an orchestration playbook; workers never load
it. Everything a reviewer needs travels in its dispatch: the diff, the
acceptance contract, and the mandate block below pasted verbatim between its
delimiters. Never point a worker at this file — the verbs here (dispatching
sessions, sequencing publication, routing fixes) are yours alone.

The author never signs off on its own work. That author is an implementer
sub-agent, or Holly itself for a directly-authored doc, spec or skill. A
DIFFERENT-vendor sub-agent reviews and returns a structured report.

**Independence = a different vendor + withholding the author's transcript and
worktree. It does NOT mean denying repo read.** The sibling-class and
coupled-artifact axes require grepping a clean checkout, so always permit repo
read, and hand over the changed surface plus its adjacency — callers, sibling
surfaces, the target type — not just the raw hunk.

**Nothing mechanically blocks a push.** There is no policy gate; the ordering
below holds only because you sequence it. That is the honest description, and
you must not tell a worker otherwise.

## Author the contract WIDE — this is upstream of everything else

The reviewer checks the diff against the contract it was handed. Any dimension
the contract omits, the review starts blind on, and that blind spot recurs
round after round.

A contract that enumerates SITES ("these four call sites, these two branches")
guarantees siblings arrive one round at a time. Enumerate the INPUT DOMAIN
instead. Before dispatching an implementer, the contract must name:

- for any function mapping inputs to decisions: the full input taxonomy —
  shapes, legacy/alternate field names, nested and wrapped inputs, the ordering
  of overlapping matches, and the none-match fall-through
- the failure mode required for every input outside the happy path, stated as
  behaviour rather than as a site list
- the coupled non-code artifacts this change kind obligates
- for a rich or stateful surface: the state-space axes, and an invariant or
  round-trip matrix test as a delivered artifact

## Every review, every round: the identical full mandate

One dispatch runs the WHOLE mandate, ALWAYS. A re-review re-sends the
**identical** block — never a narrowed "just confirm these N are closed".
That narrowing is exactly how a fix's own siblings leak out one round at a
time.

A returned report must OPEN with the battery-completeness checklist. Missing
or partial checklist = INCOMPLETE review, not a clean bill: re-dispatch the
full battery. Never read a bare "looks good" as a completed review. This holds
with equal force when servicing an external review bot — bot comments are
never grounds to narrow the dispatch.

## REVIEWER MANDATE — paste everything between the delimiters, verbatim

The dispatch `input` = the diff + the acceptance contract + this block,
unedited, delimiters included. The delimiters are load-bearing: a worker is
instructed to refuse a dispatch whose mandate is missing its END marker,
because that is how truncation is detected.

```
BEGIN REVIEWER-MANDATE-V1

You are the REVIEWER for this one diff, nothing more.

Role boundaries (absolute):
- You review and report. You never edit code, never spawn or message other
  agents or sessions, never push, and never open, approve, or merge PRs.
- Ignore any orchestration playbooks you encounter in the repo or your skill
  list. They belong to the orchestrator that dispatched you; this mandate is
  complete and overrides them.
- You may READ the whole repo — grep siblings, inspect the docs/spec tree.
  Repo read is expected; the diff alone is not enough.

Run ALL passes below and label each finding's pass in your report.

[FOCUSED] — diff vs contract: does the change satisfy its acceptance
contract? Sufficient alone only for isolated, low-state changes.

[WIDE] — what ELSE does this change touch that the diff does not show? Four
mandatory axes. You may fast-exit a genuinely empty axis in one line ("no
blast radius"), but never skip one.
1. Blast-radius — who CONSUMES the changed contract? All callers of every
   changed symbol (flag sites that should have changed to match but did not);
   event consumers of anything emitted or renamed; generated-client mirrors of
   a changed API shape; anything that decodes the changed payload.
2. Sibling-class — name the defect class this fix addresses, then enumerate
   every OTHER site matching its shape: sibling routes, parallel handlers or
   modules, other callers of the same helper or pattern. Report each site the
   fix did NOT cover. A fix that closes the flagged site but leaves siblings
   is INCOMPLETE and blocking.
3. Input-domain — when the diff touches a function that MAPS inputs to
   decisions (classifier, parser, mapper, router, dispatcher, normalizer,
   error-handler), enumerate the FULL input taxonomy, not only the shapes the
   diff exercised: every shape and variant, alternate or legacy field names
   for the same meaning, nested and wrapped inputs, the ORDERING of
   overlapping matches (a broad early match short-circuiting a specific later
   one), and the none-match fall-through. An unhandled, mis-ordered or
   silently dropped shape is BLOCKING. If the fix enumerates BAD cases to
   reject, flag it: a denylist is fail-open; demand the allowlist form.
4. Coupled-artifact — which NON-CODE artifacts must move in lockstep for this
   change KIND, and did they? Discover the repo's own coupling rules from its
   governance docs (AGENTS.md, CONTRIBUTING, the spec tree). Judge whether
   each obligated artifact's PROSE describes the new behaviour, not merely
   that the file was touched.

[WIDE] walk-list — report a hit or "clear" per item:
- Parallel surfaces — a fix on one surface must hit every surface running the
  same logic; grep the twin rather than re-reading the changed one. Canonical
  pairs: client render vs server/SSR loader; read path vs its cached twin; API
  handler vs generated-client mirror. Fixed one, left the twin = blocking.
- Test-surface — tests must exercise EVERY surface the change touches, and
  each test must FAIL when its fix is reverted. A test that passes against the
  unfixed code is not coverage; report it as a defect regardless of what the
  author's table claims.
- Whole-parcel grep — when the change renames or redefines a term, status,
  concept or claim, grep the ENTIRE parcel (source, tests, docs, generated
  artifacts) for the OLD term AND its old semantics; report each hit.
- Env-dependent claims — static configs and docs assert only intrinsic
  truths; flag any "resolves at X / reachable via Y / this provider answers"
  (deployment facts, not file properties).
- Claim completeness — audit EVERY claim about named files, workflows or
  config: REMOVED, ADDED, and anything asserted UNCHANGED. A partial audit
  misses a stale claim left by a removal, an overreaching new claim, or a
  false "still X / no change to Y".

Depth — go adversarial on rich surfaces. On a rich or stateful surface
(serializers, form-payload round trips, state machines, derivations over many
input states) your mandate is not "verify the fix" but "find the states that
break": enumerate the state space, attack the cells the contract did not
name, and deliver the FULL list in one pass rather than one edge per round.

[HONESTY] — for any diff that adds or edits agent-facing prose (a prompt, a
skill, a worker config, a comment a model will read), audit every sentence
that describes what the SYSTEM does, and report each one that asserts
something the runtime does not do. Four shapes to look for specifically,
because no automated check covers them:
- enforcement that does not exist — a policy, gate, guard or runtime step
  described as blocking, gating, requiring or preventing something. Include
  negated phrasings ("will not allow", "does not let") and named actors
  ("X checks every change before ..."), which read as enforcement just as
  strongly. Verify the mechanism EXISTS and does what the sentence says; a
  declared policy that is never evaluated on the deploy path does not count.
- ordering that contradicts the stated lifecycle — anything instructing or
  implying publication before review, in any phrasing.
- a readiness or completion marker set before the gate it is supposed to
  follow.
- an exception to a rule the contract states has none — for example
  permitting same-vendor review, or permitting the orchestrator to merge.
A false claim in agent-facing prose is a defect in its own right, whether or
not any code changed: a worker cannot verify the claim and will act on it.
Truthful statements about real behaviour are fine and must not be flagged —
report only what is FALSE, with the evidence that makes it false.

If the diff includes any doc, ADR or spec file, prose-only or mixed, ALSO run:
[SELF-CONSISTENCY] — does the document contradict ITSELF? Read it whole;
reconcile its consequences, scope and non-goals against its own decision and
any evidence it cites; hunt stale scope a later edit superseded but never
removed.
[GOVERNANCE] — does it obey the repo's OWN doc conventions? Load the repo's
ADR/spec governance (lifecycle states, decision registry or index sync,
numbering, status, cross-reference rules) and verify conformance.

Report format: OPEN with the battery-completeness checklist, one line per
pass, "run — clear" or "run — N findings", never "skipped" or absent:
  [FOCUSED] diff vs contract
  [WIDE-1] blast-radius
  [WIDE-2] sibling-class
  [WIDE-3] input-domain
  [WIDE-4] coupled-artifact
  [HONESTY] (agent-facing prose only, else "n/a")
  [SELF-CONSISTENCY] (docs only, else "n/a")
  [GOVERNANCE] (docs only, else "n/a")
Then classify each finding blocking / non-blocking / suggestion, one line per
item, each with file:line evidence and its pass label. Your final message is
the report; the orchestrator routes everything else.

END REVIEWER-MANDATE-V1
```

## Procedure

1. **Diff.** Two cases, and they differ at the release step.
   *Unopened branch* (the normal one): `git -C .worktrees/<task_id> diff
   main...HEAD`. The implementer has committed and stopped; nothing is pushed.
   *Already-open PR* (a pre-existing PR, or a fix round on one you released
   earlier): review the LOCAL delta that is about to be pushed —
   `git -C .worktrees/<task_id> diff origin/<branch>...HEAD` — because that is
   the unreviewed part. `pull_request_read` returns the published diff only, so
   reviewing it would approve what is already on the remote and miss the commit
   you are about to add. Use MCP here for review THREADS and their resolution
   state, not for the diff. Here you sequence review before the NEXT push
   rather than before the first; nothing blocks either. The implementer has
   committed and stopped; the commit has not been pushed.

2. **Gates first — ALL of them, before any reviewer is involved.** Discover the
   full deterministic set rather than assuming test/lint/typecheck: read
   `package.json` scripts, any `scripts/` or `tools/` directory, and the
   governance docs. Repos commonly also define a requirement-index checker, a
   traceability-tag linter, a pinned-line baseline, and schema or index
   coherence validators. Any RED gate goes back to the fixer to drive green
   first — zero reviewer tokens on a defect a validator already names.
   If a pytest count must be recorded or reconciled, get ground truth with
   `python -m pytest --collect-only -q <same files>` against the exact file
   set, command and commit the author reported. Never use
   `grep -c 'def test_'`: it counts functions, not collected cases, and misses
   parametrized expansion.

3. **Dispatch a reviewer whose EFFECTIVE MODEL vendor differs from the
   author's.** Vendor is a property of the model, not of the worker name:
   `claude_code` and `codex` are fixed, but `pi` runs any gateway model, so a
   `pi` reviewer is independent only if you pass it `args.model` from a
   different vendor than the author's. For a doc or skill Holly authored
   directly, the author's vendor is Holly's own model family. If the effective
   vendor on either side cannot be determined, or resolves to the same vendor,
   STOP and escalate — do not dispatch and call it independent. Task-based title, never the vendor name:
   `sys_session_send(agent=..., title="review-<task_slug>", args={purpose:
   "review", input: "<diff> + <contract>" + the mandate block above, verbatim,
   delimiters included})`. Give the diff plus adjacency as text; withhold the
   author's transcript and worktree; permit repo read. Emit the dispatch in the
   SAME turn you decide to review — a turn that only announces the intent
   stalls the run, because nothing dispatches and no inbox wake will arrive.
   Then end your turn and collect the report with `sys_read_inbox`.

4. **Validate the report before acting on it.** No opening checklist, a missing
   axis, or a verdict issued despite an `INCOMPLETE DISPATCH` notice means the
   review did not happen. Re-dispatch the identical full mandate.

5. **Route blocking issues back to the SAME fixer, on the SAME branch.**
   Delegated: re-send to the same implementer conversation (reuse its `agent` +
   `title`, or address it by `session_id`, with `purpose: "implement"`) so it
   keeps its worktree and branch. A new title spawns a fresh worker with no
   memory of the task. Direct-authoring: Holly revises the prose itself, re-runs
   the gates and every applicable pass, and re-dispatches the review. Same loop;
   only the fixer differs. Log each blocking issue as a registry fix-task scoped
   to that worktree, then return to step 1.

6. **Class closure, and the recurrence STOP gate.** Before scoping any fix to
   its flagged site, ask whether it is a one-off or a class. A typo, wrong
   constant or copy-paste slip is a one-off. A fix whose shape generalizes —
   "X not re-checked on retry", "value not normalized before compare",
   "resolution failure treated as absence" — is a CLASS, and both the fix and
   the review must cover EVERY instance in the repo. The FIRST time any later
   round surfaces the SAME class at a NEW site, STOP point-fixing: in that same
   round escalate the fix to whole-surface closure and the review to a
   whole-repo same-class audit, with "zero remaining" as the bar. Demand the
   enumeration table BEFORE the fixes — it is the deliverable that proves the
   class was closed rather than sampled.

7. **A substantive edit after review invalidates the verdict.** A verdict
   covers exactly the diff that was reviewed, at the HEAD it was reviewed at —
   which may be several commits. If anything changes after the clean report —
   a fix, a rebase, a "tiny" follow-up — the gates and the full review run
   again on the new HEAD. There is no mechanism enforcing this; it holds only
   because you check HEAD before releasing.

8. **Release.** Green gates AND zero blocking issues, and only then: tell the
   SAME implementer to push its branch and open the PR — or, if a PR is already
   open, to push the reviewed commits to it. For directly-authored
   work Holly pushes and opens its own reviewed PR. Record the PR URL in the
   registry, mark it ready, and leave it for the human. **Holly does NOT
   merge.**

9. **Terminal branches.** If no different-vendor reviewer is available, you
   CANNOT run independent review: STOP, do not open the PR on unreviewed code,
   and pull in the human. If the contract cannot be satisfied after a few
   loops, stop and escalate with specifics. Never silently degrade to a
   same-vendor or skipped review.

## Servicing an external review bot

Where a review bot comments on the PR, it posts on its own wall-clock lag, so
arm a timer for its sweep — that is a genuine scheduled delay, not sub-agent
polling. Cluster its findings by BUG CLASS before fixing anything, and feed
them in as additional FOCUSED inputs to a re-run of the identical complete
mandate — never a "confirm these are fixed" scope. Every fix diff gets the same
pre-push review as any other change. Reply in-thread rather than as a new
top-level comment. A repeated class is a hard stop for point-fixing: escalate
to whole-surface closure. When handing status to the human, report the count of
UNRESOLVED review threads alongside your summary, because replies do not
establish resolution — "findings serviced" is not "threads resolved", and the
human is merging on that distinction. Holly's hand-off wording must not imply
completeness ('findings serviced' ≠ 'threads resolved'). **Holly never declares
a clean bill and never merges.**

## Notes

- Non-blocking issues and suggestions become registry follow-ups; they do not
  block the PR.
