# Testbed — Automated Scenario Testing

## Quick Start (Linux)

```bash
# 1. Clone the repository
git clone <repo-url> llm_world
cd llm_world

# 2. Create Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Set API keys
# Both the game engine and the MCP evaluation agent need DeepSeek access.
# Create .env in the project root:
cat > .env << 'EOF'
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL_SA=deepseek-v4-flash

# SA backend
LLM_WORLD_SA_BACKEND=deepseek
LLM_WORLD_SA_MAX_TEXT_WORDS=0
LLM_WORLD_ASYNC_SUMMARY=1
LLM_WORLD_LOG_API_REQUESTS=1
LLM_WORLD_HISTORY_FRACTION=0.75
EOF

# 4. Verify setup
python testbed/runner.py --help
```

## Running a Scenario

```bash
# Activate environment first
source .venv/bin/activate

# Run the Curse of Strahd scenario (30 turns, 30s per turn timeout)
python testbed/runner.py curse_of_strahd

# Override turn count
python testbed/runner.py curse_of_strahd --max-turns 10

# Custom run ID for tracking
python testbed/runner.py curse_of_strahd --run-id my_test_1
```

## Output Structure

After a run completes, results are in `testbed/report/results/<run_id>/`:

```
results/
  curse_of_strahd_20260604_120000/
    run_meta.json              # Run metadata (turns, wall time)
    game/                      # Final game state (copy)
    backups/
      turn_0001/               # Full snapshot after turn 1
        game/                  #   game state at this point
        logs/                  #   logs at this point
        stream.txt             #   stream log
      turn_0002/
      ...
    overall_game_quality.txt   # Written by MCP evaluation agent
    errors.txt                 # Written by MCP evaluation agent
```

## MCP Evaluation Agent

The evaluation agent reads the results directory and writes `overall_game_quality.txt` and `errors.txt`.

### Using the MCP Agent

Configure the MCP agent with:

- **Read access**: `testbed/report/results/` (read all run data)
- **Write access**: `testbed/report/results/<run_id>/overall_game_quality.txt` and `errors.txt`
- **Base prompt**: `testbed/agent_prompt.md`
- **Scenario-specific metrics**: `testbed/scenarios/<name>/metrics.md`
- **Scenario story prompt**: `testbed/scenarios/<name>/story_prompt.md`
- **Model**: `deepseek-chat` (or the model specified in scenario config)
- **Operation limit**: configurable per scenario (`agent_operation_limit_sec` in config.json)

### API Key for MCP Agent

Set the same `DEEPSEEK_API_KEY` environment variable or pass it explicitly
in the MCP agent configuration. Both the game engine and the evaluator
use the same DeepSeek endpoint.

## Docker (Alternative)

```bash
# Build
docker build -f testbed/Dockerfile -t llm_world_testbed .

# Run with mounted .env
docker run --rm \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/testbed/report:/app/testbed/report \
  llm_world_testbed \
  python testbed/runner.py curse_of_strahd
```

## Adding a New Scenario

1. Create `testbed/scenarios/<name>/` folder
2. Add `config.json` with run parameters
3. Add `metrics.md` with quality criteria
4. Add `setup/init/plot.json` — initial story premise (used during WORLD_SEED)
5. Add `setup/characters/<Name>/description.json` and `metadata.json`
   - Characters are all that's needed — the game world is seeded by the GM at runtime.
   - No pre-built world files — the engine generates everything fresh like a new game.
