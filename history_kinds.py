"""The catalogue of records that can enter an agent's history.

One agent has one history thread (``messages.json``). A session (scene agents)
or a single wake (cold-path agents) works on a **branch** of that thread: the
same material, held in memory, not yet merged. Closing the branch merges the
durable records into the thread and drops the rest. See
``design/05_history_write_pipeline.md``.

This module is the single source of truth for *what* can be written. It answers
three questions about a record, and nothing else:

* which kind it is (by its leading marker, or by the kind the caller names);
* whether it is durable — merged into the thread at close — or branch-only;
* whether a differing copy of it replaces an older one (``key``) or joins it.

It performs no writing. The pipeline owns that, and reads this table.

Two rules keep the table honest:

1. **A kind does not exist until it is declared here.** A record whose marker is
   unknown is reported as such (``resolve`` returns ``None``) rather than
   silently treated as durable.
2. **Durability is one field.** The dividing rule is: *durable = a reader who
   never saw this branch needs the record to be exactly as it was; branch-only =
   the record exists to make this branch happen.* Changing the set is an edit in
   this file and nowhere else — the placeholder policy below is explicit so an
   unrecognised record cannot be lost by accident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

DURABLE = "durable"      # merged into the thread when the branch closes
BRANCH = "branch"        # exists only while the branch is open

FRAMEWORK = "framework"  # the engine wrote it for the agent
AGENT = "agent"          # the agent produced it through its own tools


#: What to do with a record whose kind is not declared. Keeping it is the safe
#: direction: history that exists today must not disappear because a kind was
#: not yet catalogued. Flip this to ``BRANCH`` to make the catalogue exhaustive.
UNKNOWN_DURABILITY = DURABLE


#: A stored record leads with its world time (``memory_store`` stamps it as it
#: is written). The catalogue reads records, so it strips that prefix first.
_STAMP_RE = re.compile(r"^\[Y\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


@dataclass(frozen=True)
class Kind:
    """One record kind.

    name:    stable identifier used in code.
    match:   compiled pattern matching the record's leading line. ``None`` for a
             record that carries no marker yet — such a record is only reachable
             by naming its kind explicitly, and is listed here so the debt is
             visible rather than forgotten.
    durability: ``DURABLE`` or ``BRANCH``.
    author:  who writes it.
    key:     when set, two records of this kind with the same key are versions of
             one fact; the newer one replaces the older (``replace_stale``).
    repeatable: each occurrence stands on its own. A record of such a kind is
             written even when one that reads exactly the same is already there:
             two sessions each open once, and an opening is not a statement that
             can be 'already said'.
    doc:     what it is, in one sentence.
    """

    name: str
    durability: str
    author: str
    doc: str
    match: Optional[re.Pattern] = None
    key: Optional[str] = None
    replace_stale: bool = False
    repeatable: bool = False
    debris: bool = False
    notes: Tuple[str, ...] = field(default_factory=tuple)


def _p(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

KINDS: Tuple[Kind, ...] = (
    # ── the session's own boundaries ──────────────────────────────────────
    Kind(
        name="session_begin",
        match=_p(r"^\[session_begin\]"),
        durability=DURABLE,
        author=FRAMEWORK,
        repeatable=True,
        doc="The situation at session start: who is where, what was carried over.",
        notes=(
            "The session number and the world time are not in it: the count lives "
            "in the framework's metadata, and the time is the stamp the record "
            "already wears.",
            "Not a versioned kind: each session's opening is a record of that "
            "session and stays, so a later session may not replace it — and does "
            "not have to read differently to stand beside it.",
        ),
    ),
    Kind(
        name="session_end",
        match=_p(r"^\[session_end\]"),
        durability=DURABLE,
        author=FRAMEWORK,
        repeatable=True,
        doc="What the session was: the world time it closed at, and the narrative "
            "of its events.",
        notes=(
            "The narrative is the account a later reader gets. What the session "
            "did is recorded beside it, as 'accepted_intent' and 'exchange' — the "
            "narrative is prose, those are what can be checked.",
        ),
    ),
    Kind(
        name="accepted_intent",
        match=_p(r"^\[accepted\]"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="An attempt a character declared that the session took: as the review "
            "read it, and as it came to pass, with who perceived it.",
        notes=(
            "Entered where each of those stood — when the review read the attempt, "
            "and when the round reached its end — never gathered at a close. The "
            "attempt's text stands in the first of the two; the second names it by "
            "its id and carries its perceivers, so one act is written out once. "
            "Attempts authored for NPCs are not here: they are already recorded as "
            "what the agent declared.",
        ),
    ),
    Kind(
        name="exchange",
        match=_p(r"^\[exchange:"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="A question and its answer between two agents: a character and the "
            "scene manager, or the scene manager and the Game Master.",
        notes=(
            "The only record of what was actually asked; nothing else keeps it.",
            "Entered where the exchange is made, not gathered at the session's "
            "close — the file is written at the close either way, but a record "
            "entered at the close lands after everything that happened later.",
        ),
    ),

    # ── what the world did ────────────────────────────────────────────────
    Kind(
        name="entity_change",
        match=_p(r"^\[entity\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What one entity-changing tool call changed, written as it happened.",
    ),
    Kind(
        name="sheet_block",
        match=_p(r"^\[entity:"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="One block of the agent's own sheet, named with the version of its "
            "content: written when its content changed, and left where it was "
            "written — an earlier version is not withdrawn, it falls out with a "
            "trim.",
    ),
    Kind(
        name="awareness",
        match=_p(r"^\[awareness:"),
        durability=BRANCH,
        author=AGENT,
        doc="Legacy transport form of what the world tells a character.",
        notes=(
            "No longer written: knowledge is state in the session ledger, and "
            "what a character knows is delivered as 'account'. Kept declared "
            "because older memories still carry the marker.",
        ),
    ),
    Kind(
        name="perception",
        match=_p(r"^\[perception"),
        durability=DURABLE,
        author=AGENT,
        doc="What the scene manager perceived, and the one it wrote it for: the "
            "record it authored, addressed, as it stands in its own memory.",
        notes=(
            "The character receives the ledger's copy of the same words — the "
            "text without the address — so this record is what the scene manager "
            "remembers having said, not a second delivery of it.",
        ),
    ),
    Kind(
        name="account",
        match=_p(r"^\[account\]"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="What the world told this agent happened, addressed to it by name, as "
            "it stands in the agent's own memory.",
        notes=(
            "The agent's copy of what it was told: the same words the one who "
            "wrote them keeps, without the address they were written under.",
        ),
    ),

    # ── what the agent committed to ───────────────────────────────────────
    Kind(
        name="commitment",
        match=_p(r"^\[intent\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What the agent declared it would do, as it declared it.",
    ),
    Kind(
        name="cancelled_subscription",
        match=_p(r"^\[cancelled\]"),
        durability=DURABLE,
        author=AGENT,
        doc="Subscriptions the agent cancelled — part of the same decision.",
    ),

    # ── the agent's own deliberation ──────────────────────────────────────
    Kind(
        name="written",
        match=_p(r"^\[written\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What the invocation wrote through its tools, in the order it wrote "
            "it, as each tool reported it.",
        notes=(
            "A cold-path wake is not a conversation: without this record the "
            "memory keeps what the agent thought and loses what it did, and the "
            "next wake cannot know the world it is in is partly its own doing.",
        ),
    ),
    Kind(
        name="reasoning",
        match=_p(r"^\[reasoning\]"),
        durability=DURABLE,
        author=AGENT,
        doc="The agent's own deliberation, kept by the agents that keep it "
            "(the cold-path ones).",
    ),
    Kind(
        name="thoughts",
        match=_p(r"^\[thoughts\]"),
        durability=BRANCH,
        author=AGENT,
        doc="Reasoning kept for inspection only; not part of memory.",
    ),
    Kind(
        name="disposition",
        match=_p(r"^\[disposition\]"),
        durability=DURABLE,
        author=FRAMEWORK,
        repeatable=True,
        doc="The standing psychological baseline, asked at the opening of every "
            "cycle in which the character is called to declare an intent.",
        notes=(
            "Each statement stands on its own: two cycles that read the same are "
            "still two openings, and what he states at one opening is not "
            "answered for by what he stated at another.",
        ),
    ),

    # ── cold-path invocation records ──────────────────────────────────────
    Kind(
        name="invocation_call",
        match=_p(r"^\[call_(gm|wm):"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="The invocation itself. The context that prompted it is rebuilt for "
            "every call and is not a record.",
    ),
    Kind(
        name="change_record",
        match=_p(r"^\[(sm|gm|wm)_changes\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What one invocation or session changed, in outline: one aggregated "
            "record instead of the calls that caused it. Also entered into the "
            "memory of the authority that reads it.",
    ),
    Kind(
        name="invoke_self",
        match=_p(r"^\[invoke_self\]"),
        durability=DURABLE,
        author=AGENT,
        doc="The agent's own reason for asking to be woken again.",
    ),
    Kind(
        name="answer",
        match=_p(r"^\[answer\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What an invocation ended on when it closed by answering: the answer "
            "it gave, as the thing it established rather than the asking of it.",
    ),
    Kind(
        name="closed",
        match=_p(r"^\[closed\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What an invocation named as what it achieved when it closed. Older "
            "histories carry it; the closing call names nothing now, so nothing "
            "writes it.",
    ),
    Kind(
        name="correction",
        match=_p(r"^\[correction\]"),
        durability=DURABLE,
        author=AGENT,
        doc="A correction the agent issued at the end of its work.",
    ),
    Kind(
        name="world_setting",
        match=_p(r"^\[world_setting\]"),
        durability=DURABLE,
        author=AGENT,
        key="world_setting",
        replace_stale=True,
        doc="The world setting, as the record the world agents read back.",
    ),
    Kind(
        name="world_setting_creation",
        match=None,
        durability=DURABLE,
        author=AGENT,
        doc="The note that the world setting was created.",
        notes=("Markerless today ('WORLD_SETTING creation'); to be given one.",),
    ),
    Kind(
        name="relationship_update",
        match=_p(r"^\[relationship_update\]"),
        durability=DURABLE,
        author=AGENT,
        doc="What the agent concluded about someone, in its own memory.",
    ),
    Kind(
        name="legacy_identity",
        match=_p(r"^\[character_identity_v1\]"),
        durability=BRANCH,
        author=FRAMEWORK,
        debris=True,
        doc="A record a previous version of the framework wrote for the character's "
            "description, which is pinned above the history now.",
        notes=(
            "Declared so the catalogue is exhaustive and the pipeline can drop it "
            "where it finds one: debris is a property of the kind, not a "
            "general-purpose removal.",
        ),
    ),

    # ── the branch's own machinery ────────────────────────────────────────
    Kind(
        name="phase_task",
        match=_p(r"^\[(resolve_cycle|session_summary|bootstrap)\]"),
        durability=BRANCH,
        author=FRAMEWORK,
        doc="The request that opens a phase.",
        notes=(
            "An instruction that has been carried out must not remain: a model "
            "reads it as pending and starts acting on it again.",
        ),
    ),
    Kind(
        name="menu",
        match=None,
        durability=BRANCH,
        author=FRAMEWORK,
        doc="Dumps of state handed to the agent for one decision: intents, "
            "characters and locations, absorption and timing.",
        notes=(
            "Emitted inside phase tasks today; it is declared separately because "
            "an unchanged menu must not be emitted twice.",
        ),
    ),
    Kind(
        name="turn_context",
        match=_p(r"^\[(session_start|Reinvoke|Notice|trigger)\]"),
        durability=BRANCH,
        author=FRAMEWORK,
        doc="The situation for one invocation: where, when, what is around, and "
            "why the agent was woken.",
        notes=("Rebuilt per invocation; nothing worth keeping as a record.",),
    ),
    Kind(
        name="tool_traffic",
        match=None,
        durability=BRANCH,
        author=AGENT,
        doc="A call and its result, exchanged with the model during the branch.",
    ),
    Kind(
        name="retry",
        match=_p(r"^\[internal_retry"),
        durability=BRANCH,
        author=FRAMEWORK,
        doc="An instruction to repair a failed or rejected call.",
    ),
    Kind(
        name="pending_question",
        match=_p(r"^\[waiting for an answer\]"),
        durability=BRANCH,
        author=FRAMEWORK,
        doc="Bookkeeping while a character's question is being answered.",
    ),
    Kind(
        name="answer_recorded",
        match=_p(r"^\[answer recorded:"),
        durability=BRANCH,
        author=FRAMEWORK,
        doc="Confirmation that an answer reached the question it was written for.",
        notes=(
            "Live protocol only: it names the question by the id it carried while "
            "the batch was open, and is never a record.",
        ),
    ),
    Kind(
        name="player_answer",
        match=_p(r"^\[player_answer"),
        durability=DURABLE,
        author=FRAMEWORK,
        doc="A question the character asked and the answer he was given, kept in "
            "his own memory.",
        notes=(
            "Both halves are his: the question in his own words, the answer as it "
            "was written for him. Entered when the answer arrives, so a question "
            "he asked is knowledge he already has. The id a question carries while "
            "it is being answered is addressing, not content: no record names it.",
        ),
    ),
)

_BY_NAME: Dict[str, Kind] = {k.name: k for k in KINDS}


# ---------------------------------------------------------------------------
# Reading the table
# ---------------------------------------------------------------------------


def kind(name: str) -> Kind:
    """The declared kind, or raise — a caller naming a kind must not invent one."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"undeclared record kind {name!r}") from None


def payload(text: str) -> str:
    """A record without its world-time prefix — what the record says.

    Comparison between records is comparison of payloads: the stamp changes
    with the clock, so a record that says the same thing twice must compare
    equal.
    """
    return _STAMP_RE.sub("", str(text or "").lstrip(), count=1)


def resolve(text: str) -> Optional[Kind]:
    """The kind a stored record belongs to, from its leading marker.

    Returns ``None`` when the record carries no marker this catalogue knows —
    which is a fact worth reporting, not a failure to hide.
    """
    body = payload(text)
    for k in KINDS:
        if k.match is not None and k.match.search(body):
            return k
    return None


#: A versioned record carries the version of its content at the end of its
#: marker line (``[entity:Elena/state@v:8cd37495]``). The text before it names
#: the fact the record is a version of.
_VERSION_SUFFIX_RE = re.compile(r"@v:[0-9a-fA-F]+(?=\]|$)")


def replacement_key(text: str) -> Optional[str]:
    """What a record is a version of, for kinds that declare replacement.

    Derived from the record itself — its marker line without the version — so a
    kind needs no per-kind parsing: the sheet's ``[entity:Elena/state@v:…]`` and
    a single-valued marker like ``[world_setting]`` both name themselves.
    """
    body = payload(text)
    first = body.splitlines()[0].strip() if body.strip() else ""
    if not first.startswith("["):
        return None
    return _VERSION_SUFFIX_RE.sub("", first)


def durability_of(text: str) -> str:
    """Whether a record survives the close of its branch.

    An unrecognised record follows ``UNKNOWN_DURABILITY``: see the note there.
    """
    k = resolve(text)
    return k.durability if k is not None else UNKNOWN_DURABILITY


def is_durable(text: str) -> bool:
    return durability_of(text) == DURABLE


def markerless() -> List[Kind]:
    """Declared kinds that carry no marker yet, so migration work stays visible."""
    return [k for k in KINDS if k.match is None]


def debris() -> List[Kind]:
    """Kinds that are not records any more: found in a thread, they go."""
    return [k for k in KINDS if k.debris]


def is_debris(text: str) -> bool:
    k = resolve(text)
    return bool(k is not None and k.debris)


def by_durability() -> Dict[str, List[str]]:
    """The adjustable list itself: what is kept, and what is dropped."""
    out: Dict[str, List[str]] = {DURABLE: [], BRANCH: []}
    for k in KINDS:
        out.setdefault(k.durability, []).append(k.name)
    return out


def describe() -> str:
    """The catalogue as text — for review, and for anyone changing the list."""
    lines = []
    for k in KINDS:
        marker = k.match.pattern if k.match is not None else "(no marker yet)"
        key = f" key={k.key}" if k.key else ""
        replace = " replaces stale" if k.replace_stale else ""
        lines.append(f"{k.name:22s} {k.durability:8s} {k.author:9s} {marker}{key}{replace}")
        lines.append(f"{'':22s} {k.doc}")
        for n in k.notes:
            lines.append(f"{'':22s} note: {n}")
    return "\n".join(lines)
