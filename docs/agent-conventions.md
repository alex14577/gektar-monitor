# Agent Conventions (bd hve)

Conventions for AI agents and sub-agents working in this project. Extracted
from `CLAUDE.md` (bd hve) so the per-session preamble stays light — every
session loads `CLAUDE.md` into context; only project-rule essentials should
live there, while the longer playbook lives here and is read on demand.

> If you're a sub-agent: you are expected to read **this whole file** when
> the orchestrator points you at a task that involves sub-agent dispatch,
> doc-reading discipline, or any of the operating rules below.

---

## Sub-agent orchestration playbook

See `bd memories orchestrator-playbook` for the full checklist. Short
invariants summarised here for offline reference:

1. **Brainstorm phase** before every task (5–10 min, orchestrator, *no*
   sub-agent): write down the micro-decisions that the existing ADRs do
   not already cover.
2. **Pre-write extraction step** in the writer-agent prompt: «read
   `<doc>` §X, then *write out the `<items>` table in your report*
   before any code». This flips the agent from interpretation mode to
   extraction mode.
3. **Reviewer runs BEFORE `bd close`**, never after. Two-phase: writer
   produces code → reviewer reviews the same code → writer fixes
   blocker-level findings → only then commit and close.
4. **Parallelism only when the grep-intersection of target files is
   empty.** Before each wave, for every pair of tasks, intersect the
   touched-file sets; if non-empty, sequence them instead.
5. **Acceptance criteria in bd must be reconciled with canon docs
   BEFORE `bd update --claim`.** If the bd acceptance is narrower than
   §X of the canon doc, run `bd update --acceptance` first.
6. **Fake-impls used in Protocol tests** must have at least one test
   where *every method of the fake is invoked*, not only
   `isinstance(...)`. This catches runtime bugs from invalid API calls
   that pure type-shape tests will miss.
7. **After a sub-agent report**, the orchestrator personally runs
   `pytest` and `git show --stat` on the changeset. Do not trust «tests
   green» / «no changes needed» in a summary.
8. **Reviewer model for critical tasks** = `model: "opus"`, not sonnet.
   Critical = security surfaces, schema migrations, money/auth paths,
   anything that would page someone if wrong.

---

## Sub-agent doc-reading rules

Sub-agents **do not read the vault wholesale**. They read selectively —
only the files and sections that the orchestrator explicitly points
them at. Long documents (>500 lines) get skimmed. They do *not* by
default chase `[[wiki-links]]` transitively.

Consequences for prompt engineering:

1. **Cite canon fragments directly in the prompt** — not «read §X», but
   paste the quoted block with line numbers. More expensive in tokens,
   much cheaper than a reopen cycle.
2. **Point at concrete atomic files**:
   `docs/architecture/07-concurrency.md`,
   `docs/decisions/ADR-019-notification-state-machine.md`,
   `docs/data-model/notifications.md`. The vault is restructured into
   atomic notes (<500 lines each) — name the file as a whole; line
   ranges aren't required.
3. **Pre-flight grep in the prompt**: «before any code, run
   `grep -rn "<key>" docs/` and copy every match into your report». This
   forces a wide scan instead of a narrow point-lookup.
4. **Atomic docs** (short files focused on one topic) are easier for
   agents to consume than monolithic compendia — they get read in full,
   not skimmed. After the 2026-05-13 restructure: ADRs live in
   `docs/decisions/`, architectural sections in `docs/architecture/`,
   domain models in `docs/data-model/`.
