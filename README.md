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

export EDS_TUI_MODEL="qwen3.8:latest"    # optional, main model
export EDS_TUI_SMALL_MODEL="ornith:35b"  # optional, model for simpler requests
```

Both models must be served from the same Ollama host and support tool calling.

## Usage

```bash
ask                  # start a fresh conversation (clears history)
ask --continue       # continue the previous conversation
ask --fast           # force the small model for this request
ask --smart          # force the main model for this request
ask --skills         # list installed skills
ask --skill-new NAME # scaffold a new skill
ask --test           # self-check: prove routing, skills, delegation and escalation work
ask --upgrade        # update to the latest version from GitHub
```

## Model routing

Every request is triaged before it runs. A short classification call to the small model
(`ornith:35b`) decides whether the request is simple — a lookup, a listing, a status check,
a one-off command — or complex enough to need the main model (`qwen3.8:latest`). The whole
agentic loop then runs on whichever model was picked, and the choice is printed above the
answer.

Use `--fast` or `--smart` to skip triage and force a model. When you have skills installed,
a second concurrent call asks which of them applies — see [Skills](#skills).

If the small model takes a request and then stalls — more than 6 tool-call rounds, or a
request error — the conversation is handed to the main model and continues from there
(`↑ escalating to ...`). Triage failures fall back to the main model, so a missing or
misconfigured small model degrades to the previous single-model behavior instead of
breaking.

## Skills

A skill is a reusable procedure you write once and `ask` loads when it is relevant — how *you*
ship a release, how *you* restore a dev database, the three commands that actually diagnose a
bad deploy on *your* machine. Without them, any such procedure has to be retyped as part of the
question every single run.

They live in `~/.eds_tui/skills/`, one directory each:

```
~/.eds_tui/skills/
  deploy-flow/
    SKILL.md
  triage-logs/
    SKILL.md
    check.sh          # optional; reference it from the body by full path
```

`SKILL.md` is frontmatter plus a Markdown body:

```markdown
---
name: deploy-flow
description: Ship a release from this repo — branch checks, tag, push, verify.
model: main
---

1. Confirm the working tree is clean and we are not on `main`.
2. ...
```

- `description` is **required**. It is the only thing the model sees until the skill loads, so
  make it specific — that is what selection matches against.
- `name` defaults to the directory name.
- `model` is `main`, `small` or `any` (default). Use it to keep a reasoning-heavy skill off the
  small model instead of letting it burn turns until escalation.

Frontmatter is flat `key: value` only. Nested YAML is not supported.

`ask --skill-new deploy-flow` writes a starter file. `ask --skills` lists what is installed,
including anything that failed to parse and why.

### How a skill gets selected

Three ways, most explicit first:

1. **You name it** — type `/deploy-flow ship this release` at the prompt.
2. **Triage matches it** — alongside the routing call, the small model is asked which skill (if
   any) covers your request. Both questions run concurrently, so this costs a round trip but
   almost no wall-clock, and it costs zero tool-call rounds. The chosen skill is shown on the
   status line: `qwen3.8:latest  ·  skill: deploy-flow`.
3. **The model loads it mid-run** — the main model always sees the list of skill names and
   descriptions and can call `load_skill` to pull one in when it decides it needs it.

The two triage questions are asked in **separate calls** on purpose. Asking the small model for
a complexity verdict and a skill name in one reply measured 4/10 on a fixture set — it answers
one question and falls back to the first skill in the list for the other. Asked separately it
measured 12/12.

`--fast` and `--smart` skip triage entirely, so they also skip automatic skill matching; combine
them with `/skill-name` if you want both. A flag you typed always beats a skill's `model:` field,
which in turn beats the triage verdict.

Sub-agents get no skills — no list, no `load_skill`. A delegated task has to stand alone, so the
main model inlines whatever the helper needs into the task itself.

### Why there are no project-local skills

`ask` reads skills from your home directory only, never from the working directory. A skill is
instructions that an agent with unconfirmed shell access will follow, so picking them up out of
whatever repo you happen to `cd` into would turn cloning a repository into a code-execution path.

## Self-location

Commands run in your current working directory, as before. But `ask` also knows where its
own installed source lives (the `eds_tui` package inside the pipx venv), and the system prompt
points at it: questions about `ask` itself — its flags, options, behavior — are answered by
reading those files, not by guessing from whatever is in the working directory.

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
exiting nonzero if anything broke. The skill checks build a throwaway fixture in a temp
directory — they never touch your real `~/.eds_tui/skills`:

```
  eds tui self-check
  main: qwen3.8:latest    small: ornith:35b

  ✓ server + both models reachable     0.0s   both served
  ✓ triage: simple → small model       1.1s   → ornith:35b
  ✓ triage: complex → main model       0.9s   → qwen3.8:latest
  ✓ --fast forces small model          0.0s   → ornith:35b
  ✓ --smart forces main model          0.0s   → qwen3.8:latest
  ✓ delegation: main spawns small     23.7s   qwen3.8:latest spawned ornith:35b ×2
  ✓ escalation past turn cap           7.9s   turn cap 1 → qwen3.8:latest after 3 turns
  ✓ bad small model falls back         0.0s   → qwen3.8:latest
  ✓ skills: discovered and parsed      0.0s   name, description, model and body round-tripped
  ✓ skills: triage matches a skill     1.3s   → selfcheck-widget
  ✓ skills: load_skill returns body   18.7s   qwen3.8:latest called load_skill ×1
  ✓ skills: /name pin vs flag          0.0s   pin → ornith:35b, --smart overrides → qwen3.8:latest

  12 passed  0 failed              49.9s
```

The delegation, escalation and skill-loading checks run real agentic sessions, so their shell commands and
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
