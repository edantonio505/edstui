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
