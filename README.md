# eds tui

Natural Language Command Executor
A one-shot terminal AI assistant powered by a local Ollama model.

## Install

```bash
pipx install git+https://github.com/edantonio505/edstui.git
```

## Configuration

Add to your `~/.bashrc` or `~/.bash_aliases`:

```bash
export EDS_TUI_URL="http://your-ollama-host:11434"
export EDS_TUI_TOKEN="your_token_here"   # optional, only if your server requires auth

export EDS_TUI_MODEL="qwen3.6:35b"       # optional, main model
export EDS_TUI_SMALL_MODEL="ornith:35b"  # optional, model for simpler requests
```

Both models must be served from the same Ollama host and support tool calling.

## Usage

```bash
ask                  # start a fresh conversation (clears history)
ask --continue       # continue the previous conversation
ask --fast           # force the small model for this request
ask --smart          # force the main model for this request
ask --test           # self-check: prove routing, delegation and escalation work
ask --upgrade        # update to the latest version from GitHub
```

## Model routing

Every request is triaged before it runs. A short classification call to the small model
(`ornith:35b`) decides whether the request is simple — a lookup, a listing, a status check,
a one-off command — or complex enough to need the main model (`qwen3.6:35b`). The whole
agentic loop then runs on whichever model was picked, and the choice is printed above the
answer.

Use `--fast` or `--smart` to skip triage and force a model.

If the small model takes a request and then stalls — more than 6 tool-call rounds, or a
request error — the conversation is handed to the main model and continues from there
(`↑ escalating to ...`). Triage failures fall back to the main model, so a missing or
misconfigured small model degrades to the previous single-model behavior instead of
breaking.

## Self-location

Commands run in your current working directory, as before. But `ask` also knows where its
own installed source lives (`eds_tui/main.py` inside the pipx venv), and the system prompt
points at it: questions about `ask` itself — its flags, options, behavior — are answered by
reading that file, not by guessing from whatever is in the working directory.

Without this, asking `ask` about its own flags from inside a project containing an unrelated
`ask.py` sent it searching the wrong program until it hit the tool-call limit.

## Delegation

When the main model is driving a request, it also gets a `delegate_task` tool and can spawn
the small model as a sub-agent for mechanical legwork — gathering listings, counting things,
checking status — while it stays on the reasoning.

A sub-agent gets shell access but no delegation tool of its own and no view of the parent
conversation, so each delegated task has to stand alone. It runs its own agentic loop (up to
5 steps) and returns a text report as the parent's tool result. Delegations are shown nested
under the run:

```
  Running tools

  └─ ornith:35b  Count how many *.py files are in the current directory

     $ ls *.py 2>/dev/null | wc -l
     4

     → There are 4 *.py files in the current directory.
```

The small model never delegates — when it owns a request it just runs commands itself.

## Self-check

`ask --test` exercises the whole arrangement against your live server and reports pass/fail,
exiting nonzero if anything broke:

```
  eds tui self-check
  main: qwen3.6:35b    small: ornith:35b

  ✓ server + both models reachable     0.1s   both served
  ✓ triage: simple → small model       0.8s   → ornith:35b
  ✓ triage: complex → main model       0.9s   → qwen3.6:35b
  ✓ --fast forces small model          0.0s   → ornith:35b
  ✓ --smart forces main model          0.0s   → qwen3.6:35b
  ✓ delegation: main spawns small     15.6s   qwen3.6:35b spawned ornith:35b ×2
  ✓ escalation past turn cap           4.2s   turn cap 1 → qwen3.6:35b after 2 turns
  ✓ bad small model falls back         0.0s   → qwen3.6:35b

  8 passed  0 failed              21.7s
```

The delegation and escalation checks run real agentic sessions, so their shell commands and
answers print inline above the result line.

## Conversation history

By default, every time you run `ask` it starts a completely fresh conversation — no memory of previous questions.

If you want to keep the context going across multiple runs, use `--continue`:

```bash
ask                   # ask something, get an answer, exits
ask --continue        # picks up right where you left off
ask --continue        # keeps going...
ask                   # back to a fresh start
```

The conversation history is saved to `~/.eds_tui_history.json` after each response. Running `ask` without `--continue` always clears it.

## How it works

- Type your question and press Enter to submit
- Paste multi-line text — it collapses to `[+N lines]` so you can keep typing
- The model can run shell commands on your machine to answer questions
- Exits after one question and answer
