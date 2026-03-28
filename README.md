# eds tui

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
```

## Usage

```bash
ask                  # start a fresh conversation (clears history)
ask --continue       # continue the previous conversation
ask --upgrade        # update to the latest version from GitHub
```

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
