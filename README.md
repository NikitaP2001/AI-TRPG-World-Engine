# AI-TRPG-World-Engine

A system for running tabletop RPG campaigns with language model agents. The central
problem it addresses is a simple one to state and surprisingly hard to solve: how do
you get multiple AI agents to share a world over many sessions without that world
becoming incoherent?

---

## What tabletop RPGs are, and why they're an interesting AI problem

A tabletop RPG is a form of collaborative storytelling with rules. One participant
— the Game Master — runs the world: describes environments, plays non-player
characters, judges the consequences of player actions. The other participants each
control one character, making decisions from that character's limited point of view.
The world persists between sessions. Events have consequences. What a character
knows is different from what another character knows, and both differ from what the
GM knows.

This structure creates a problem when you try to run it with language models.

A language model has no persistent state between calls. It has no memory of what
happened last session unless you put that memory in its context. It has no inner
world model — it only has whatever text is in front of it. And crucially, it cannot
distinguish between remembering an established fact and inventing a plausible one.
It will invent confidently, and differently each time.

---

## The first attempt: one model, one context

The simplest approach is one model playing the Game Master, with the entire game
history in its context. Players type what they do; the model narrates what happens.

This works for a single session. It breaks across sessions for several compounding
reasons.

Context fills up. A language model can only hold so much text in its active window.
As the campaign grows, older material has to be compressed or dropped. Standard
compression — summarization — loses detail. Detail that gets lost is detail that
later gets contradicted or reinvented.

The model invents. Ask it about a place that hasn't been described yet, and it will
describe one — plausibly, in detail, and differently the next time you ask. There is
no mechanism to prevent this, because the model has no way to distinguish "I recall
this" from "I am generating this."

The problem with that second point is subtler than it first appears. It isn't just
that wrong facts appear — it's that the world comes into existence reactively, in
response to questions. An experienced player senses this. They notice that every door
is a black box until they open it, that the city on the other side of the mountain
has no texture until they go there, that the NPC's secret past materializes at the
exact moment someone asks about it. The world does not feel like it exists
independently. It feels like it's being generated in response to attention.

This is a harder problem than memory. Memory can be improved with better
summarization or retrieval. The reactive world problem requires something different:
the world has to exist before it's observed, and what's observed has to be a reading
of what exists, not a generation triggered by the observation.

And there is a third failure, distinct from both: everyone is in the same room. A
language model given full game history will leak information across characters —
it "knows" what another player's character is planning, what an NPC is secretly
thinking, what's hidden in the room next door. Asking it to pretend otherwise is an
instruction layered on top of the knowledge. The instruction can be ignored or
degraded under context pressure. This breaks the core mechanic of TRPG: the
interesting tension between characters having genuinely different information.

---

## Separating authority: the agent architecture

The first structural move is to split the single model into multiple agents, each
with its own context, its own tools, and its own scope of authority.

An agent here means more than a model with a prompt. It means a model instance
that runs its own reasoning loop — it reasons, calls tools to read or write
persistent state, checks the results, and continues until it reaches a termination
condition. It has a message history that persists across turns. Different agents have
access to different tools, which means they have access to different information and
different kinds of authority to change world state.

The split that falls out naturally from the structure of a TRPG:

**Character agents** — one per player character. Each sees only what their character
can perceive: where they are, who else is present, what's recently happened in their
line of sight. They have no access to world state tools, no access to other
characters' contexts. Their output is a structured decision: an instinctive drive,
a rationalization of that drive against the situation, and a resulting action. These
three layers are separate fields in a tool call — the model must produce all three
explicitly, making the reasoning legible and giving the character a consistent
psychological texture that persists across sessions rather than being reinvented
fresh each turn. Each character also maintains its own memory — a running
self-reflection updated periodically, and a separate relationship record for each
person they've encountered, written from their own perspective.

One thing worth making explicit: on the architectural level there is no distinction
between a player character and a non-player character. Both are agent instances with
the same structure. The difference is only in who or what drives the decisions —
a human through a UI, or the agent running fully autonomously. This means the system
can run as a traditional multiplayer TRPG, as a single-player game, or as a fully
autonomous simulation where every character is an agent. A character agent running
on its own is simply a very capable NPC — one rich enough in psychology and memory
to be worth running as a full agent rather than as a simple rule set.

**Scene Manager** — runs the active scene. Receives all character decisions, narrates
what happens, handles questions characters ask about what they perceive. It has
direct access to the world record for the current location and can answer most
questions from that. Escalation to the Game Master is a last resort, not a routine
step — only when something requires authority the Scene Manager doesn't have.

**Game Master** — owns the persistent world record for places, NPCs, and events
encountered in play. Creates and updates these through typed tools. It is not called
every turn; it is called when something happens that requires its authority.

**World Manager** — authors world-scale structure and templates (species, factions,
long-horizon interests). Works at a time scale and level of abstraction separate
from the active scene. Never narrates scenes; wakes from subscriptions and
scheduled self-invokes.

This split enforces context isolation structurally: a character agent genuinely does
not have access to another character's reasoning, because that reasoning never enters
its context.

---

## What confabulation costs in a TRPG specifically

In a language model assistant, an invented fact is an annoyance. In a TRPG, it is
a structural failure.

Players make decisions based on facts about the world. If a player decides their
character trusts a certain NPC, that decision is only meaningful if the NPC will
behave consistently with what the player believes about them. If the NPC's
personality changes because the model regenerated it differently this session, the
player's decision was based on fiction that has now been silently revised. Over time,
players stop committing to decisions, because the world doesn't feel like it retains
the consequences of decisions.

This is why tool-based world writing matters. When the GM creates an NPC, it writes
a structured record: name, location, description, current state. The next invocation
reads that record. The NPC is what the record says it is, not what the model would
generate if asked fresh.

---

## The memory problem: three different needs

Long-term coherence requires memory, but memory in this context means three
genuinely different things with different structures and different solutions.

**Narrative memory** needs to span months of play while staying compact enough for
an agent's working context. Flat summarization loses exactly the details that matter.
The solution here is hierarchical and indexed: every ten turns the Scene Manager
produces a paragraph summary; every ten paragraphs, an arc summary. These summaries
contain structured metadata alongside prose — entity names with timestamps, location
references — which function as an index rather than a complete record. An agent
reading a summary knows what to look up. When a specific detail matters, a tool call
retrieves the full turn record. The summary is a table of contents; the tools provide
the chapters.

**Character memory** is separate from the world's story and belongs to each
character individually. This includes a structured self-reflection — beliefs, goals,
emotional state — updated periodically, and a relationship record for each person
encountered, written from that character's perspective. A scheduled review process
re-examines known relationships every ten turns. Two characters who know the same NPC
can have genuinely different assessments of them, because the assessments are stored
separately and updated from separate experiences.

**World / event memory** is different in kind from both. Agents write structured
records (entities, tags, mechanical state) and append causally attributed events
to an event log indexed by place and time. Later queries ask "what happened here
between then and now" and receive records, not regenerated prose. This is not what
summarization is for: summaries index the story; tools retrieve the chapters and
the world facts.

---

## The reactive world problem: why prompting the GM doesn't work

The agent architecture and memory systems address coherence and isolation. But there
is a problem that neither of them solves, which took longer to identify precisely
because it looks like a prompt problem when it isn't.

Early in development, the Game Master was asked, through prompt instructions, to
inject background world events into the narrative — trade disputes in distant cities,
political developments, things happening off-camera. The instructions were specific.
The events appeared. And they consistently felt wrong.

Not factually wrong — wrong in texture. Every injected event felt like it was written
because a player was nearby and paying attention. A rumor about a war reached the
players at exactly the moment it could affect them. An NPC turned out to have exactly
the history relevant to their current quest. An experienced player will notice this.
Not because any individual event is implausible, but because the distribution is
impossible: in a real world, most of what's happening is not relevant to you, and the
world does not arrange itself around your attention.

This is not fixable by writing better prompt instructions. The Game Master's context
is the active scene. Even told explicitly to inject unrelated events, it generates
events that are shaped by what's in its context. It cannot do otherwise.

---

## Agents that act on their own interests, not on requests

The fix is a different model of how agents are invoked — one that inverts the
relationship between agents and events.

In a naive system, agents are responders: something happens in the scene, the scene
asks an agent what to do, the agent answers. Every invocation is a reaction. This
is why the GM keeps generating events that feel tied to player attention — because
the GM is only ever called when player attention is already present.

The alternative is agents that act from subscriptions. Instead of being asked when
something has already happened, an agent declares in advance what it finds
significant, and is invoked when those conditions are met — not to react to a
request, but because the thing it was watching for has occurred.

The World Manager, between sessions, is not waiting to be asked. It has registered
interest in certain classes of events: a faction crossing a threshold, a significant
entity dying, a sealed location being opened. When those conditions fire — via
event queries, corrections, or scheduled self-invokes — the WM acts on its own
initiative: extending the world, creating consequences, setting new conditions in
motion. It is playing its own game, which produces a world that feels alive because
it advances along its own logic, not in response to player observation.

The Game Master works the same way at a smaller scale. It is not called when players
ask questions — the Scene Manager handles those from the world record. The GM is
called when its own subscriptions fire: a group of NPCs it has been tracking has
reached a dungeon, an NPC the players previously met has appeared in the current
scene, a dragon that exists in the world record has moved through a region where the
players are active. These are things the GM registered interest in, not things the
players asked about. When its subscriptions don't fire, the GM is not invoked at all.

Character agents have the same structure. A character who always responds immediately
when their companion speaks does not re-declare this each turn — they have a standing
subscription that fires when the condition is met. A character who is watching from
a distance and waiting for a specific thing to happen declares that condition once,
and the scene resolution invokes them when it matches, not before.

This model has an important consequence that sounds paradoxical at first: most of the
time, in most places, nothing happens. If no one has subscribed to events in a
particular tavern on a particular night, nothing occurs there. Time can advance
and the log for that location remains empty. When players later
ask the Scene Manager what happened there last night, it checks the log, finds
nothing, and answers: nothing happened. Not "I imagine the tavern was quiet" — but
genuinely nothing, because the world does not generate events that no agent has
reason to generate.

This is what makes the world feel real rather than reactive. A world that generates
events in response to observation is a world that is always, subtly, about the
observers. A world where nothing happens unless something with an interest in that
location was watching — and where that watching was established before the players
arrived, not in response to their arrival — is a world that exists independently.

---

## The World Manager: authoring structure, not scenes

The World Manager's (WM; code: `WorldManager`) relationship to the world is
different in kind from the Game Master's. The GM creates facts encountered in
play: this NPC exists, this location looks like this, this event occurred. The WM
authors longer-horizon structure: race and class templates, factions, world-scale
movement, corrections that bind the GM, and standing interests that wake it when
something matters.

Facts for the active story are written when scenes need them — through typed
tools into a persistent entity store and event log. Later invocations read those
records. The system is narrative-only: there is no separate numeric geology /
ecology / civilization simulation underneath. Simple automation (effects,
effects, subscriptions) handles what does not need an LLM.

Because the WM's tool vocabulary can be large, detailed documentation for each
tool is retrieved on demand. The working context stays focused on what the
current task requires.

The full agent/tool architecture is in [`docs/architecture.md`](docs/architecture.md).

---

## Scene resolution: the problem with sequential turns

Even with agents that have isolated contexts and a persistent world record, the
active scene has its own structural problem.

The current system processes characters sequentially: character A submits a decision,
then character B, then the Scene Manager narrates the combined result. This contains
a hidden unfairness. Character B is effectively submitting after A's action has
already happened in the narrative sequence — even if in the fiction both characters
were acting simultaneously. Whoever goes first sets the terms.

More importantly: a character observing from a distance — watching through a magic
eye, listening from an adjacent room — has no natural moment to intervene in a
sequential model. They either act at their assigned turn (possibly before the event
they want to react to has occurred) or wait until next turn (by which point the
moment has passed). Real observation doesn't work this way.

The redesign treats a scene as a shared timeline. Each character declares an
intention as a time-scoped interval — what they're doing, for how long, and what
condition would change that. The Scene Manager holds all declarations simultaneously,
sets a planning horizon, and resolves what happens from the structure of those
declarations: who was where, whose condition matched whose action, what each
character could observe of the result. Characters with standing conditions don't
re-declare them each turn — the condition fires when it matches, regardless of whose
turn it nominally is.

Most of what happens in a scene resolves without narration determining it. The
narration describes what resolution produced.

---

## What each agent knows and doesn't

The context boundaries are the core of why this works, and they are worth stating
plainly.

Character agents know what their character has perceived, their own memory and
relationship records, and what the Scene Manager tells them about the current
situation — filtered by what their character could observe. They do not know what
other characters are thinking, or what the GM/WM have written off-screen.

The Scene Manager knows everything in the active scene — all character decisions,
the scene location, relevant world data for this place. It does not know distant
world structure the WM is authoring. When something exceeds its authority, it
escalates — rarely, as a last resort, not as a routine step.

The Game Master knows the persistent world record for places and NPCs encountered in
play. It participates when its own subscriptions fire or when the Scene Manager
escalates. It creates facts once; every subsequent reference reads from that creation.

The World Manager knows world-scale structure and templates. It does not participate
in active scenes. It acts when its subscriptions fire, and is otherwise building the
world at its own pace, playing its own game.
