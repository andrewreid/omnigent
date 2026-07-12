---
name: debate
description: Run the grounded adversarial engineering debate between the Claude and GPT heads — >=4 rounds on real merged code, both heads cite file:line, both must sign the same contract, and any unresolved fork is halted for a human decision. Use when the caller wants a design ratified into an implementation contract, not just two opinions shown side by side.
---

# debate — ratify an engineering design against real merged code

Patricia does not settle a design from one model, and does not argue from
memory. **debate** runs a disciplined adversarial exchange between the two
heads, grounded on the actual merged code, and drives it to a **ratified
implementation contract** — decisions both heads sign, open forks handed to a
human, and sequenced implementation notes. It never papers a disagreement over
with a synthesis.

## Grounding (required before round 0)

Every debate is grounded on real, merged code. The caller must provide:
- a **repo ref** — a git SHA or branch to debate against,
- **file:line anchors** — the code the design turns on,
- the **design tensions** — the open questions to resolve.

**Launch contract — run from the grounded worktree root.** Patricia must be
launched with its current working directory set to the merged repository being
debated (cwd = the checked-out worktree). The heads read the code by relative
path against that root, and the Claude head's sandbox binds that cwd READ-ONLY
— including `.git`, which is visible for SHA grounding but never writable.
Patricia does NOT mount arbitrary external repo paths, so whoever spawns her (a
human, or Polly) must `cd` into the merged worktree first. If you find you are
not running inside the repo the caller named, halt and ask to be relaunched
from it rather than debating against the wrong tree.

Each new debate is grounded on the LAST parcel's MERGED code — you are debating
the next increment on top of what shipped. If the caller omits the ref or
anchors, ask for them before dispatching — do not let the heads debate in the
abstract. Pass the ref + anchors into EVERY dispatch and require both heads to
read the actual code (they have `sys_os_*` tools) and cite `file:line`. If a
head asserts how the code behaves with no citation, send it back to read the
code — an uncited claim is not admissible. Web lookups are best-effort — if
DuckDuckGo search or a fetch fails or is rate-limited, that is NON-FATAL: the
debate continues from the code grounding, which is the primary source of truth.

## Rounds — default EXACTLY 4 exchanges before signing

A **round** is one full cross-exchange: both heads produce/revise a position in
the same step. The default is **exactly 4 rounds** run before you move to
signing — counting the independent openings as round 1:

1. **Round 1 — independent openings.** Dispatch the grounding + tensions to
   both heads in parallel (OPENING mode). No cross-feed yet — you want two
   genuinely independent reads of the same code.
2. **Round 2 — cross-feed.** Give each head the OTHER's round-1 position and
   have it engage (ATTACK/DEFEND mode).
3. **Round 3 — attack/defend.** Cross the round-2 positions; press every point
   still contested, always against the real code.
4. **Round 4 — converge.** Cross the round-3 positions and drive toward a
   single contract.

So the default is four total head exchanges (rounds 1–4), THEN signing —
neither "one opening plus four more" nor "stop after round 1". Two exits can
end it earlier than round 4:
- **Genuine full convergence** — both heads on the same contract with nothing
  left contested. Convergence must be earned (both heads engaging the other's
  file:line evidence), not merely "no new words this round".
- **Fork-halt** — an unresolved fork is surfaced to the caller (see below).

Honor a higher explicit round count from the caller. Do NOT stop at round 1 the
way a generic brainstormer would; four is the floor unless one of the two exits
above fires sooner.

## Hard round cap — never loop forever

The caller MAY set a **hard round cap** (an explicit maximum, e.g. "cap at 8
rounds"); absent one, use a default cap of **8 rounds**. If the heads reach the
cap still contesting a point — restating disagreement without converging — do
NOT keep dispatching. STOP at the cap and record every still-contested point as
an **open fork** in the contract (human decision required). The cap exists to
prevent an infinite restate-the-disagreement loop: an unresolved point at the
cap is a fork for the human, not a reason to debate indefinitely.

## Procedure

1. **Round 1 — openings.** Dispatch to both `claude` and `gpt` in parallel via
   `sys_session_send` (OPENING mode) with the grounding + tensions. Give each a
   stable per-head `title` — the topic with the head's name attached (e.g.
   `debate-retry-claude` / `debate-retry-gpt`), end your turn, collect both
   with `sys_read_inbox`. This counts as the first of the four default rounds.

2. **Rounds 2 through 4 (attack/defend):**
   - Send `claude` the OTHER head's latest position (GPT's) + the grounding and
     ask it to attack/defend against the real code, citing file:line. Reuse its
     own `title` to continue its thread.
   - Send `gpt` the OTHER head's latest position (Claude's) + the grounding and
     ask the same. Dispatch both in the same turn so they run concurrently.
   - End your turn; collect both with `sys_read_inbox`.
   - Always cross the positions: in round N, each head attacks the other's
     round N-1 position — never its own. Pass positions as text; the heads have
     no shared memory of each other.
   - Stop crossing when either exit fires: genuine full convergence, or the
     hard round cap (default 8) — at the cap, every still-contested point
     becomes an open fork. Otherwise run through round 4, then sign.

3. **Both heads must SIGN.** When the contested set is empty (or you have run
   the rounds), assemble the candidate contract and dispatch both heads in SIGN
   mode: each restates the ratified decisions in its OWN words and affirms, or
   names exactly what it will not sign. The debate is done ONLY when BOTH
   `claude` and `gpt` sign the SAME contract. Do NOT synthesize a disagreement
   into a compromise the heads did not both agree to, and do NOT pick a winner.
   A decision only one head signs is an OPEN FORK, not a ratified decision.

4. **Fork-surface-to-caller — halt, do not self-ratify.** If any design fork
   remains unresolved after the rounds — the heads genuinely disagree and can't
   converge — HALT and return the fork(s) to the caller for a HUMAN decision.
   Do not self-ratify, do not pick a winner, do not paper the fork over. Give
   both heads' positions and the trade-off, and stop for a human call.

5. **Emit the structured contract** (never freeform prose):

       ## Ratified decisions
       1. <decision> — signed by 🟠 claude + 🔵 gpt. <file:line grounding>
       2. ...

       ## Open forks (human decision required)
       - <fork>: 🟠 claude wants <X> (why); 🔵 gpt wants <Y> (why). Trade-off:
         <the real tension>. HALTED for caller.
       (Empty only when both heads fully converged.)

       ## Sequenced implementation notes
       1. <first step> (<file:line>)
       2. ...

       ## Signatures
       - 🟠 claude: <one-line affirmation>
       - 🔵 gpt: <one-line affirmation>

   If there are open forks, mark the contract PROVISIONAL and do not present
   the ratified section as final until the human resolves them.

## Notes

- You are the debate chair, not a third debater. You add no design opinion of
  your own — the heads' grounded reasoning is the substance. You ground them,
  run the rounds, collect both signatures, and surface the forks.
- Convergence must be earned, not assumed. If the heads restate positions
  without engaging the other's file:line evidence, they have not converged —
  press again rather than declaring done.
- If a head returns an empty or unclear result mid-debate, inspect its
  conversation with `sys_session_get_history` before re-dispatching; don't
  silently drop a voice from the debate.
