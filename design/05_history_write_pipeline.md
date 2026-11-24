# Item 5: One History, One Write Pipeline

*Decisions fixed now so later implementation cannot drift.*

---

## Purpose

The agent's memory is the only thing that survives a session, and today it is
written from eight different places, each with its own conventions, each deciding
for itself what deserves to be kept. This item fixes the invariants before any of
the awareness/account work is implemented: what the branch is, how anything is
written, what a close does, which records are durable, and how duplicates are
suppressed.

---

## 1. What an agent's history is

There is **one** history thread per agent (`messages.json`). It is the only
durable record.

A **branch** is opened on that thread:

| Agent | Branch opens | Branch closes |
|---|---|---|
| Scene Manager, characters | session begins | session ends |
| Game Master, World Manager | the agent is woken (a trigger fires / an interval elapses) | a **termination tool** of that agent is called (§8) |

A branch therefore spans exactly one session for the scene agents, and exactly one
wake for the cold-path agents: their declared termination tools end the
invocation, so the next wake opens a new branch. That is deliberate — the cold
path is not a conversation, and only durable records carry across wakes.

The branch is **not a second kind of memory**. It is the same material —
the same record kinds, the same order, the same rules — held in RAM and not yet
merged. It is a continuation of the thread that has not been appended to the
thread yet. Nothing about it is special to the reader: a record in a branch and
the same record in the thread are the same record.

Therefore the branch is an **implementation detail**. No caller anywhere asks
whether a branch is open, and no caller cares.

---

## 2. The pipeline — the only way anything is written

One entry point writes to an agent's history. Everything goes through it:

- records the framework makes for the agent (session markers, phase tasks,
  menus, dumps, notices, accounts given to it, change summaries, retirement
  paragraphs);
- records the agent makes through its tools (what it wrote, what it committed,
  what changed).

The pipeline owns, in one place:

- the record's **kind** and role;
- its **timestamp prefix** and its position in the branch;
- the **dedup / actuality test** (below);
- whether the record is **durable or branch-only**;
- the **trim** applied when a durable record lands in the thread;
- the **merge** itself at close.

Rules:

1. **No module writes to `messages.json` directly.** Direct writes are how the
   current mess happened; the pipeline is the only writer.
2. **No module rewrites the thread either.** The single rewriting operation that
   exists today (dropping outdated records) becomes a pipeline operation with a
   declared kind behind it, not a general-purpose hammer.
3. **A new record kind does not exist until it is in the catalogue** (§3). The
   catalogue is the single source of truth for what can appear in a history.

---

## 3. The catalogue of records

Every record that can enter a thread is declared once — its name, what it is, who
authors it, whether it is durable, its dedup key, and which context layer it
appears in. It replaces the present situation, where the vocabulary lives
scattered across the code and the docs disagree about it.

**Durable** — merged into the thread at close. A later reader needs it to be
exactly as it was, without this conversation:

- the session-boundary record, as the situation the agent needs (who is where,
  what is carried over) — **not** its label or its session number: what an agent
  does not need to know, it is not told (§8);
- entity-change records (what a tool call changed);
- the agent's own sheet blocks, as records naming the version of their content;
- **accounts given to the agent** — what the world told it happened;
- the change summary a later agent invocation reads;
- history-retirement paragraphs;
- what the agent committed to (its declared intents / decisions);
- reasoning records, for the agents that keep them.

**Branch-only** — dropped at close:

- phase tasks and instructions that were carried out (a carried-out instruction
  left in a transcript is read as pending and acted on again);
- menus, intent dumps, character lists, scene/user context given per invocation;
- tool traffic (calls and their results), retries, rejections;
- pending-question bookkeeping;
- the agent's transient notifications.

The dividing rule, stated once: **durable = a reader who never saw this branch
needs it to be the same; branch-only = it exists to make this branch happen.**

---

## 4. Dedup and actuality — for every kind

The pipeline compares a candidate record against the thread, and within the branch
against what was already emitted:

- the same kind with the same payload is **not written again**;
- a record is written when it is new **or differs**;
- where a kind declares replacement semantics, the differing record replaces the
  stale one instead of joining it (the world setting is the kind that does: one
  setting stands. A block of a character's own sheet is not one of them — it is
  written when its content changed and never withdrawn).

Two consequences to hold onto:

- Re-invoking an agent with an unchanged situation adds nothing at all.
- A menu that has not changed since the last invocation is not emitted twice.

This test belongs to the pipeline. A caller never implements it.

---

## 5. What a close does

1. Walk the branch in order.
2. For each record: durable → merge-append into the thread; branch-only → dropped.
3. Merge-append adds; it never rewrites what is already in the thread, except
   where a kind declares replacement (§4).
4. The branch is discarded. The agent's next branch starts from the thread as it
   now stands.

Note what is *not* here: no summarising pass, no narrative block, no prose
generated at close. A close is a merge. Anything that needs to be said to a
reader must have been written through the pipeline while the branch was open.

### 5.1 One write, and it is the close

Until the branch closes, the file is **not written to at all** — not by an entity
change, not by a record delivered to a character mid-session. A session's records
reach the file in exactly one merge, in the order they were produced. The file an
interrupted session leaves behind is the file its predecessor closed, never half
of a session nobody finished.

The trade-off, taken deliberately: a crash mid-session loses that session's
records. Nothing else does. Every mechanical change a session makes is written by
the tool that made it, into the world's own store — an agent's memory is not where
the world's state lives, so no world fact waits on this file.

A record can be *read* before the merge: the agent's own branch is part of what it
reads (§6.1), so a record delivered mid-session is in front of its owner in that
session. What mid-session writing would buy, then, is not visibility but
crash-safety for one session's memory, and that is not worth a file that is
written in pieces.

### 5.2 What still runs after the file is final

The merge is followed by one step: covering what the merge dropped (§4, and the
retirement pass each agent runs from its own context). It happens *after* the last
record is in the file, so a watcher who sees `[session_end]` has not seen the end
of the work: the console says so, one line per agent, and the run reports its own
exit. The rule kept here is that nothing runs after the last record unannounced —
the close is a sequence of named steps, not a silence the caller has to interpret.

---

## 6. Invariants that follow, and why they matter here

1. **Nothing can be delivered late.** Because the pipeline writes into the open
   branch first, a record written mid-session is visible to the agent in this
   session. The present failure — a record landing only in the thread and so being
   read a session later, in the middle of old history — becomes impossible, not
   merely fixed. This is the mechanism that makes "the world told this character
   what happened" work at all; it is a consequence of the abstraction, not a
   special case bolted onto a delivery path.
2. **One writer means one place to look.** "Why does this agent's memory contain
   X / not contain Y" becomes answerable in one function instead of eight.
3. **Kinds are documented where they are declared.** A record's meaning stops
   being something a maintainer reconstructs from call sites.
4. **The branch is per-campaign-lifecycle, not per-turn.** Nothing in this
   pipeline knows what a turn is — consistent with the non-discrete time model.

---

## 7. Inventory to be migrated (bounded work)

Direct writers today, all of which become pipeline callers:

| Site | What it writes |
|---|---|
| `scene_manager/core.py` (`_record_history`) | SM session markers, entity-change records, session end record |
| `player/agent.py` (character close) | the character's input context and its committed intents |
| `player/entity_blocks.py` | the character's sheet blocks (+ the one rewrite site) |
| `engine/orchestrator.py` | awareness markers parsed out of the SM branch into a character's file |
| `engine/change_summary.py` | the change summary a later invocation reads |
| `gm/game_master.py` | GM session/call records, GM reasoning |
| `world_manager/core.py` | WM records, world-setting creation, WM reasoning |
| `player/relationship_review.py` | relationship review records |

Record vocabulary currently in use, to be declared in the catalogue rather than
invented at call sites: session began / session ended / session start / session
summary / entity / intent / trigger / notice / reinvoke / reasoning / thoughts /
disposition / call-to-GM / awareness / internal retry / waiting-for-answer /
player answer / resolve cycle / world setting / entity-block-with-version.

---

## 8. Decisions taken

1. **Durability is a declared property of a kind, and the list is expected to
   change.** The catalogue (§3) carries it, so the set can be adjusted freely as
   understanding improves. The present contents are a reasonable starting point,
   not a commitment: the requirement is that changing them is one edit in one
   place, never a search through call sites.
2. **A kind is described by what a reader needs, not by its label.** The
   session-boundary record carries the situation (who is where, what is carried
   over); the session number and the boundary label are bookkeeping the agent does
   not need, and are dropped from what it reads. Anything the framework itself
   needs session boundaries for (summary ranges, diagnostics) is kept in its own
   metadata, outside the agent's thread.
3. **A branch closes on the agent's declared termination tools.** They already
   exist per agent as a declared set:

   | Agent | Termination tools (declared) |
   |---|---|
   | Game Master | `close_invocation`, `answer_lore_question`, `invoke_self_gm` |
   | World Manager | `close_invocation`, `invoke_self` |

   A termination tool ends the invocation whether its effect is "the work is
   done" or "wake me later" — so for both cold-path agents **every wake is one
   branch**. The set is read from the same declaration the ReAct loop already
   uses, not duplicated.
4. **The branch decision and the durability decision are independent.** Closing a
   branch merges what is durable and drops the rest; it never decides what is
   durable. A kind that must survive a cold-path wake must therefore be durable,
   because nothing else crosses a wake.
