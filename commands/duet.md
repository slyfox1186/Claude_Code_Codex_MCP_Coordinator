---
description: Run the full Claude->Codex->Claude implement/review/reconcile workflow via agent_duet
argument-hint: [task description, or blank to use the current conversation]
---

# Run an agent_duet workflow

You are driving the `agent_duet` MCP coordinator. It runs a fresh Claude Code process to
implement, a fresh Codex process to review independently, and a fresh Claude Code process
to adjudicate that review and revalidate. You are the operator's interface to it, not the
implementer: do not do the work yourself.

## The task

$ARGUMENTS

## Before you start: collect what is missing

A run costs real time and real model turns, and it cannot be steered once it starts. So
gather everything you need **first**. Never invent a task, and never start on a vague one.

You need four things. Work out which you already have, then ask only for the rest:

| What | Where it usually comes from |
|---|---|
| The task | the argument above, or the conversation so far |
| The repository | the current working directory's repository root |
| Acceptance criteria | inferred from the task, then confirmed |
| Delivery mode | leave it alone unless the user asked for a review branch |

**If the argument above is empty**, do not guess and do not start. Two cases:

- *There is relevant conversation context.* Draft the task and acceptance criteria from
  it, show the draft, and ask the user to confirm or correct it. Confirming a draft is
  much less work for them than answering an open question.
- *There is no useful context.* Ask for the task directly. Prefer `AskUserQuestion` with
  concrete options when you can see plausible candidates in the repository (a failing
  test, an obvious TODO, an unfinished module); fall back to an open question when you
  genuinely cannot tell.

**If the task is present but too thin to act on**, ask targeted questions rather than
starting and hoping. A task is too thin when a competent implementer could satisfy it in
materially different, mutually incompatible ways — "make it faster" without a target,
"add auth" without saying which mechanism, "fix the bug" without saying which one. Ask
about the specific fork in the road, not for a specification.

Do not interrogate the user. One round of questions, then proceed. If they say "you
decide", pick the most defensible option, state the assumption you are making, and start.

Restate the final task and acceptance criteria in one short block before calling
`duet_start`, so what the run received is on the record.

**Do not pass `delivery_mode` unless the user asked for that behaviour.** Omitted, the
run lands its commit on the branch they are already on, which is what asking for a change
normally means. Pass `review_branch` only when they say they want to review it on a
separate branch first, or when the working tree is dirty and they will not clean it —
that mode is the one that creates `agent-duet/<id>`, and a branch nobody asked for reads
as the tool going behind their back.

## What to do

1. **Determine the repository.** Use the current working directory's repository root. It
   must sit below an `allowed_repo_roots` entry in the user's agent-duet config, or
   `duet_start` will refuse. If it refuses for that reason, tell the user which root is
   missing rather than trying to work around it.

2. **Write real acceptance criteria** from the task. Concrete and checkable — "existing
   tests still pass", "the new endpoint rejects an empty payload" — not restatements of
   the task. If you cannot infer any, say so and ask.

3. **Call `duet_start` exactly once.** Keep the returned `run_id`. Never start a second
   run for the same task; if you think you need one, stop and ask.

4. **Poll with `duet_wait`**, passing that `run_id` and `timeout_seconds=90`. Keep
   exactly one `duet_wait` call in flight for the run: wait for its result before calling
   `duet_wait` or `duet_status` again. If the client moves the call to a background task,
   wait for that task's result instead of starting another poll. Continue until the phase
   is `AWAITING_FINALIZE` or terminal (`COMPLETE`, `FAILED`, `CANCELLED`). Between calls,
   report the phase in one short line so the user can see progress. The detached model
   work continues independently and can take hours; the 90-second value limits only one
   status poll, not Claude's or Codex's work.

5. **At `AWAITING_FINALIZE`, stop and report.** Summarize, from the returned evidence
   only:
   - the branch the commit will land on, the base commit, and the exact files the run
     changed. If that branch is not the one the user is on, say so plainly — it means
     the run used `review_branch` and the work will need merging afterwards;
   - each configured validation command and whether it passed;
   - whether the reviewer was verified as non-mutating
     (`codex_readonly_verified`), and any mutations it did make;
   - the proposed commit message;
   - anything the run flagged as a remaining risk.

   Then ask whether to finalize. **Do not call `duet_finalize` until the user answers
   yes in a message.** Your own summary is not approval.

6. **If the user approves**, call `duet_finalize` with the run's exact branch and a commit
   message. If the repository has a remote, pass its exact URL and push normally. If it
   has no remote, pass `push=false`; no remote URL is required, and finalization creates
   a local commit only. Report the local commit SHA and whether anything was pushed. For
   a push, also report the remote SHA and deployment status verbatim. If deployment says
   `NOT_CHECKED`, say `NOT_CHECKED` — never describe it as deployed or healthy.

7. **If `duet_finalize` refuses**, it has committed nothing and the run is still at
   `AWAITING_FINALIZE`. The refusal names a file and a reason, and most of them name a
   line. That is a task for you, not a question for the user: go read what it points at
   in the run's worktree, then act.

   - *Credential-shaped content.* Read the line. If it is a real secret, remove it from
     the file and say what you removed. If it is ordinary code that merely reads like a
     credential, reword it so it no longer does — renaming a variable is usually enough —
     and say what you changed and why it was a false alarm.
   - *A coordination artifact is still present.* Delete it from the worktree; it is
     agent-duet's own scratch file and was never part of the change.
   - *The content changed after validation.* Something edited the worktree after the run
     validated it. Say what differs and stop; re-validating is not yours to fake.

   After a fix you made in the worktree, call `duet_finalize` again with the same
   arguments. Do not ask for approval a second time — you already have it for this
   change. Report the fix and the result together.

## Boundaries

- `duet_start` never commits, pushes, or deploys. `duet_finalize` is the only tool that
  publishes, and it requires explicit approval.
- Report only what the tools returned. Do not infer that something succeeded because a
  phase advanced, and never restate a model's claim as evidence.
- If a *run* fails — a phase errors out, or `duet_status` reports `FAILED` — report the
  phase and the error verbatim, then stop. Do not retry automatically; that usually means
  something about the repository or the environment needs a human decision. A
  `duet_finalize` refusal is not this: it is a guard telling you exactly what to look at,
  and step 7 says to look.
- If the user closes the session, the run keeps going. To pick it back up later, call
  `duet_status` with the `run_id`.
