# AI-TRPG-World-Engine

A multi-agent LLM system that simulates a persistent, causally-consistent fictional world. The world is not a session—it is a continuous shared state that evolves between turns, between sessions, and independently of player interaction. Three autonomous agents operate over a common JSON world state: a **Game Master** that owns narrative and world causality, a **Storage Assistant** that owns structured world memory, and per-character **Character agents** that own player intent. No single agent has full authority; they compose reality through structured exchange.

---

## Features

- Persistent world state
- Multi-agent architecture
- Turn-based scene resolution
- Character intent planning
- GM narrative authority
- Storage agent self-maintenance
- Paragraph / arc summarization
- Time-aware history trimming
- DeepSeek API backend
- Web UI + REST API
- JSON world on filesystem
- Hot-reload between turns

---

## Installation

**Requirements:** Python 3.9, [DeepSeek API key](https://platform.deepseek.com/)

```bash
git clone https://github.com/NikitaP2001/AI-TRPG-World-Engine.git
cd AI-TRPG-World-Engine
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

Copy and fill config:

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY and OPENROUTER_BASE_URL in .env
```

Initialize a world (place your world seed in `init/`):

```bash
python bootstrap.py
```

Run:

```bash
python webui_server.py
# Open http://localhost:8000
```

---

## Architecture

### Agents

| Agent | Role | Persistence |
|---|---|---|
| **Game Master** | Narrates turns, owns world causality and NPC behavior | `game_master_messages.json` |
| **Storage Assistant** | Maintains world JSON: locations, NPCs, character state | `storage_assistant_messages.json` |
| **Character Agent** | Produces character intent per turn | `characters/<name>/messages.json` |
| **Summarizer** | Collapses turn history into paragraph / arc summaries | triggered async |

### Data flow

```
                        ┌──────────────────────────────────────┐
                        │            World state (JSON)         │
                        │  world/facts.json                     │
                        │  world/story.json  (turns → para → arc│
                        │  characters/<name>/description.json   │
                        │  characters/<name>/memory.json        │
                        └───────────┬──────────────────────────┘
                                    │ read / write via tools
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                       ▼
   ┌──────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
   │  Character Agent │  │    Game Master       │  │  Storage Assistant  │
   │  (per character) │  │                      │  │                     │
   │  Produces intent │  │  Resolves turn:      │  │  After each turn:   │
   │  conditional     │  │  narration, duration │  │  - update NPCs      │
   │  first-person    │  │  world_facts delta   │  │  - update locations │
   └────────┬─────────┘  └─────────┬────────────┘  │  - trim stale data  │
            │                      │                └──────────┬──────────┘
            │ intent               │ narration + facts         │
            └──────────────────────▼                           │
                        ┌─────────────────┐                   │
                        │  Scene runtime  │◄──────────────────┘
                        │  (console_app / │
                        │   webui_server) │
                        └────────┬────────┘
                                 │ turn_complete message
                                 ▼
                        ┌─────────────────┐
                        │   Summarizer    │
                        │  (async)        │
                        │  10 turns →     │
                        │  paragraph      │
                        │  10 paras  →    │
                        │  arc            │
                        └─────────────────┘
```

### Key concepts

- **Turn**: one resolved scene tick — all characters submit intent, GM narrates outcome, world state updates.
- **Paragraph**: summary of 10 turns, injected into GM history as a timestamped delta.
- **Arc**: summary of 10 paragraphs, marks a major story phase.
- **World facts delta** (`[world_facts]`): GM-emitted canonical fact block, injected into both GM and SA histories after each turn.
- **History trimming**: active context window is budget-capped; summaries + anchor messages are protected from trim to maintain coherent long-term memory.
- **Tool-based storage**: Storage Assistant modifies world state exclusively through typed JSON-pointer tools — no free-form writes.

### Stack

- **LangChain + LangGraph** — agent loop and tool binding
- **LangChain-OpenAI** — ChatOpenAI client (DeepSeek-compatible)
- **FastAPI + Uvicorn** — web UI backend
- **FAISS + sentence-transformers** — optional RAG for world context retrieval
- **Python 3.9 / JSON filesystem** — world state storage

---

## Development Notice

This project and this README were developed with assistance from AI coding models, including GPT-5+ and Claude Sonnet 4.6+.
