#!/usr/bin/env python3
import os
import sys
import re
import json
import subprocess
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import time
import ollama
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from rich import box
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.keys import Keys

try:
    from . import skills
except ImportError:     # running main.py directly rather than as a package
    import skills

console = Console()

_model = os.environ.get("EDS_TUI_MODEL", "qwen3.8:latest")
_small_model = os.environ.get("EDS_TUI_SMALL_MODEL", "ornith:35b")

SMALL_MAX_TURNS = 6    # tool-call rounds before escalating off the small model
HARD_MAX_TURNS = 14    # absolute ceiling, prevents a runaway loop

# Where this running copy of eds tui actually lives, regardless of the user's cwd.
# Questions about ask itself must be answered from here, not from whatever happens
# to be in the working directory.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ENTRY = os.path.join(APP_DIR, "main.py")



# miniclosedai's dev.sh serves its own API over self-signed TLS by default
# (https://<host>:8095) -- same trust model already used elsewhere for
# same-box/LAN sibling services in that project (e.g. its voicestudio proxy
# passes verify=False for the same reason). Only ever used for a same-box/
# LAN relay call, never for the actual interdata credential.
_INSECURE_SSL_CONTEXT = ssl._create_unverified_context()


def _resolve_via_miniclosedai():
    """Ask a local/LAN miniclosedai instance whether it's connected to the
    interdata relay and, if so, which models are actually available there.

    Returns (host, headers, main_model, small_model) on success. Raises on
    any failure -- miniclosedai unreachable, interdata not connected/enabled,
    or an empty model list -- so callers fall back to the static EDS_TUI_*
    config below, silently and without adding latency to the common case
    (short 2s timeout, no retries).
    """
    base = os.environ.get("EDS_TUI_MINICLOSEDAI_URL", "https://127.0.0.1:8095").rstrip("/")
    mc_token = os.environ.get("EDS_TUI_MINICLOSEDAI_TOKEN", "")
    headers = {"Authorization": f"Bearer {mc_token}"} if mc_token else {}
    req = urllib.request.Request(f"{base}/relay/api/tags", headers=headers)
    with urllib.request.urlopen(req, timeout=2.0, context=_INSECURE_SSL_CONTEXT) as resp:
        data = json.loads(resp.read())
    names = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
    if not names:
        raise RuntimeError("miniclosedai relay reported no models")

    # Prefer whatever this run is already configured to want (env override or
    # the qwen3.8/ornith defaults); fall back to whatever interdata actually
    # has, same preferred-else-first pattern miniclosedai's own doc-gen uses.
    main_model = _model if _model in names else names[0]
    remaining = [n for n in names if n != main_model]
    small_model = _small_model if _small_model in names else (remaining[0] if remaining else main_model)
    return f"{base}/relay/", headers, main_model, small_model


def make_client():
    global _model, _small_model
    try:
        host, headers, main_model, small_model = _resolve_via_miniclosedai()
        _model, _small_model = main_model, small_model
        return ollama.Client(host=host, headers=headers, timeout=None, verify=False)
    except Exception:
        pass  # miniclosedai not reachable, or not connected to interdata -- fall back

    host = os.environ.get("EDS_TUI_URL", "http://192.168.0.110:11434").rstrip("/")
    token = os.environ.get("EDS_TUI_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return ollama.Client(host=host, headers=headers, timeout=None)

RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command in the user's terminal and return its output. "
            "Use this to find files, list directories, check system info, run programs, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute (e.g. 'find / -name myfile 2>/dev/null')"
                }
            },
            "required": ["command"]
        }
    }
}

DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Hand a small, self-contained subtask to a faster assistant that has the same shell "
            "access you do. Use it for mechanical legwork — gathering listings, counting things, "
            "checking status, reading a value out of a file — so you can stay focused on the "
            "harder reasoning. It cannot see your conversation, so give it one complete "
            "instruction. It returns a short text report of what it found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A complete, self-contained instruction, e.g. 'Count the lines in every "
                        "*.py file in the current directory and report the totals'"
                    )
                }
            },
            "required": ["task"]
        }
    }
}

LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load the full instructions for one of the skills listed in your system prompt. "
            "Use it when the request matches a skill's description and you have not already "
            "been given that skill's text. Returns the skill's procedure, which you then follow."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name, exactly as listed in your system prompt"
                }
            },
            "required": ["name"]
        }
    }
}

CREATE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "Save a reusable procedure as a skill, so it is available in later runs without "
            "the user explaining it again. Use it when the user asks you to remember how to "
            "do something, or to write or update a skill. The file is written and then "
            "re-read to confirm it registers, and you are told either way."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Short lowercase identifier, e.g. 'git-cleanup'. Letters, digits, "
                        "dots, dashes and underscores only."
                    )
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One line saying when to reach for this skill. This is the only text "
                        "matched against future requests, so name the specific subject rather "
                        "than describing it in general terms."
                    )
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The procedure itself, in Markdown. Write it for someone who has a "
                        "shell but was not part of this conversation."
                    )
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Which model should run it: 'main' for reasoning-heavy work, 'small' "
                        "for mechanical work, 'any' to let triage decide. Defaults to 'any'."
                    )
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Set true to replace a skill that already exists."
                }
            },
            "required": ["name", "description", "body"]
        }
    }
}

SHELL_TOOLS = [RUN_COMMAND_TOOL]                    # sub-agent and small model: shell only


def tools_for(model: str):
    """
    The main model can delegate, write skills and load them; the small model just
    runs commands. create_skill is never gated on the registry -- it is how the
    first skill gets written.
    """
    if model != _model:
        return SHELL_TOOLS
    tools = [RUN_COMMAND_TOOL, DELEGATE_TOOL, CREATE_SKILL_TOOL]
    if skills.discover():
        tools.append(LOAD_SKILL_TOOL)   # nothing installed, nothing to load
    return tools


def build_system_prompt(model: str = None, skill: dict = None) -> str:
    model = model or _model
    prompt = (
        f"You are 'eds tui', a helpful terminal assistant invoked as 'ask', running on "
        f"Ubuntu Linux. "
        f"The user's current working directory is: {os.getcwd()}. "
        f"All commands run relative to this directory unless a full path is needed. "
        f"Your own source code is the package at {APP_DIR} — main.py is the entry point and "
        f"skills.py is the skill registry — and that is the copy currently running. "
        f"When the user asks about you (your flags, options, features, or behavior), read "
        f"those files and answer from them. Do not infer your own behavior from files in the "
        f"working directory: they may be unrelated programs, or stale copies that are not "
        f"what is running. "
        f"You have access to the user's terminal via the run_command tool. "
        f"Search strategy: "
        f"- Use 'find' to locate files or directories by name. "
        f"- Use 'grep -r' to search inside file contents when looking for text, keywords, or strings. "
        f"- Combine both when needed. "
        f"- Suppress permission errors with '2>/dev/null'. "
        f"- Always exclude vendored trees: --exclude-dir={{node_modules,.git,.venv,dist,build}}. "
        f"  A recursive grep that walks node_modules returns more output than can be read "
        f"  and wastes the round. "
        f"Stopping discipline: you get roughly {HARD_MAX_TURNS} tool-call rounds and are "
        f"cut off when they run out, so spend them deliberately. Put independent commands "
        f"in one round rather than one command per round. Before each new round, check "
        f"whether the answer is already in the output above — if it is, stop and answer. "
        f"If two or three searches in a row have turned up nothing, that absence is itself "
        f"the finding: report it rather than rephrasing the same search again. A partial "
        f"answer that names what you could not confirm is far more useful than being cut "
        f"off mid-search. "
        f"If a delegate_task tool is available to you, hand it the mechanical legwork — "
        f"gathering listings, counting things, checking status — and spend your own effort "
        f"on the reasoning and the final answer. Each delegated task must stand alone, since "
        f"the helper cannot see this conversation. Run commands yourself when the work is "
        f"trivial or needs your judgement. "
        f"If a create_skill tool is available to you, use it when the user asks you to "
        f"remember a procedure, or to write or update a skill. Put the specific subject in "
        f"the description — that one line is all that future requests are matched against — "
        f"and write the body for someone who has a shell but none of this conversation. "
        f"Think step by step before acting. Plan the right command for the task. "
        f"Be direct and concise in your final answer."
    )

    if skill:
        prompt += (
            "\n\nA skill has been selected for this request. Follow it.\n\n"
            + skills.render(skill)
        )

    # The index is names and one-liners only -- that is the whole point of skills,
    # and it is useless to a model that has no load_skill tool to act on it.
    available = dict(skills.discover())
    if skill:
        available.pop(skill["name"], None)
    if available and model == _model:
        prompt += (
            f"\n\nOther skills you can load:\n{skills.index_lines(available)}\n"
            f"Call load_skill with one of those names when the request matches its "
            f"description. If none match, ignore this list."
        )

    return prompt


TRIAGE_SYSTEM = (
    "You route requests for a terminal assistant. Reply with exactly one word: SIMPLE or COMPLEX.\n"
    "SIMPLE = answerable directly or with one or two straightforward shell commands "
    "(lookups, listing files, checking status, short factual questions, a single command).\n"
    "COMPLEX = needs multi-step reasoning, writing or refactoring code, debugging, planning, "
    "chaining many commands, or careful judgement.\n"
    "Output only that one word."
)


SKILL_MATCH_SYSTEM = (
    "You match a user's request to a skill for a terminal assistant.\n"
    "Reply with exactly one word: the name of the skill below whose description covers "
    "the request, or NONE if none of them do.\n"
    "Skills:\n{index}\n"
    "Output only that one word."
)


def classify_complexity(client, user_input: str) -> str:
    """Which model should own this request. Falls back to the main model on any doubt."""
    try:
        response = client.chat(
            model=_small_model,
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM},
                {"role": "user", "content": user_input},
            ],
            think=False,
            options={"temperature": 0, "num_predict": 8},
        )
        verdict = (response.message.content or "").upper()
    except Exception:
        # Triage must never block the request; when in doubt use the main model.
        return _model

    # The small model wraps its verdict in whatever punctuation it feels like, and
    # sometimes echoes the whole instruction back. Both words present means it did
    # not actually decide, so fail closed to the main model.
    if "SIMPLE" in verdict and "COMPLEX" not in verdict:
        return _small_model
    return _model


def match_skill(client, user_input: str):
    """
    Which skill covers this request, if any.

    Deliberately a separate call from classify_complexity. Asking ornith for a verdict
    and a skill name in one reply measured 4/10 against these fixtures -- it answers
    one question and falls back to the first skill in the list for the other. Asking
    each on its own measured 12/12.
    """
    index = skills.index_lines()
    if not index:
        return None

    try:
        response = client.chat(
            model=_small_model,
            messages=[
                {"role": "system", "content": SKILL_MATCH_SYSTEM.format(index=index)},
                {"role": "user", "content": user_input},
            ],
            think=False,
            options={"temperature": 0, "num_predict": 12},
        )
    except Exception:
        return None         # no skill is always a safe answer

    # Only real names match, so a hallucinated skill reads as no skill at all.
    return skills.match(response.message.content)


def triage(client, user_input: str):
    """
    Route a request: which model owns it, and which skill applies.

    The two questions run concurrently, so installing skills costs an extra round
    trip but almost no extra wall-clock, and the complexity verdict stays exactly
    what it would have been with no skills installed.
    """
    if not skills.discover():
        return classify_complexity(client, user_input), None

    with ThreadPoolExecutor(max_workers=2) as pool:
        model = pool.submit(classify_complexity, client, user_input)
        skill = pool.submit(match_skill, client, user_input)
        return model.result(), skill.result()


def pin_for(skill: dict):
    """The model a skill pins itself to via its 'model:' field, or None if it doesn't care."""
    if not skill:
        return None
    return {"main": _model, "small": _small_model}.get(skill["model"])


def resolve_model(client, user_input: str, force_fast: bool = False,
                  force_smart: bool = False, skill: dict = None):
    """
    Decide which model owns this request and which skill applies.

    Precedence, most explicit first: a flag the user typed just now, then the
    skill's own 'model:' field, then the triage verdict. --fast and --smart skip
    triage entirely, so they also skip skill auto-matching -- name one with
    /skill-name to combine the two.
    """
    if force_fast:
        return _small_model, skill
    if force_smart:
        return _model, skill

    pinned = pin_for(skill)
    if pinned:
        return pinned, skill        # an explicit skill that names its model needs no call

    model, matched = triage(client, user_input)
    if skill is None:
        skill = matched
        model = pin_for(skill) or model

    return model, skill


def indent_lines(text: str, indent: str) -> str:
    return "\n".join(f"{indent}{line}" for line in text.splitlines())


# A single tool result has to fit in a 32k context alongside everything else. An
# unbounded 'grep -r' into node_modules produced a few hundred KB in one round and
# the request failed outright -- the whole run lost to one unfiltered search.
MAX_OUTPUT_CHARS = 8000


def clip_output(output: str) -> str:
    """Keep the head and tail of a huge command output and say what was dropped."""
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    head, tail = output[:6000], output[-1500:]
    dropped = len(output) - len(head) - len(tail)
    return (
        f"{head}\n\n"
        f"... [{dropped:,} characters cut. This command produced {len(output):,} characters, "
        f"far more than can be read. Narrow it before running anything like it again: add a "
        f"filter, use --include / --exclude-dir=node_modules, or pipe through head.] ...\n\n"
        f"{tail}"
    )


def run_command(command: str, indent: str = "  ") -> str:
    label = Text()
    label.append(f"{indent}$ ", style="bold green")
    label.append(command, style="bold white")
    console.print(label)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.getcwd()
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        output = clip_output(output.strip()) or "(no output)"
        # Use Text to avoid Rich markup interpretation
        output_text = Text(indent_lines(output, indent), style="dim")
        console.print(output_text)
        console.print()
        return output
    except subprocess.TimeoutExpired:
        console.print(f"[red]{indent}Error: command timed out after 30 seconds[/red]\n")
        return "Error: command timed out after 30 seconds"
    except Exception as e:
        # Use Text to avoid Rich markup interpretation in error messages
        error_text = Text()
        error_text.append(f"{indent}Error: ", style="red")
        error_text.append(str(e), style="red")
        console.print(error_text)
        console.print()
        return f"Error: {e}"


def run_command_once(command: str, seen: dict) -> str:
    """
    Run a command, or replay it if this exact command already ran in this loop.

    A model that has not found what it is looking for tends to re-run near-identical
    searches, and each repeat costs a round out of a hard budget. Replaying the
    earlier output costs nothing and tells the model, in the result it is about to
    read, that the repeat gave it nothing new.
    """
    command = (command or "").strip()
    if command in seen:
        label = Text()
        label.append("  $ ", style="bold green")
        label.append(command, style="bold white")
        console.print(label)
        console.print(Text("  (already run this session — reusing the output)",
                           style="dim yellow"))
        console.print()
        return (
            "This exact command already ran earlier in this session and returned:\n"
            f"{seen[command]}\n\n"
            "It was not run again. Re-running it cannot tell you anything new — try a "
            "materially different command, or answer from what you already have."
        )

    output = run_command(command)
    seen[command] = output
    return output


SUBAGENT_MAX_TURNS = 5
SUB_INDENT = "     "


def build_subagent_prompt() -> str:
    return (
        f"You are a focused helper for 'eds tui', a terminal assistant invoked as 'ask', "
        f"running on Ubuntu Linux. "
        f"The current working directory is: {os.getcwd()}. "
        f"The assistant's own source code is at {APP_ENTRY} — read it if the subtask is "
        f"about how 'ask' itself behaves, rather than guessing from the working directory. "
        f"You have been handed one specific subtask. Use the run_command tool to complete it, "
        f"then report what you found plainly and concisely. "
        f"You are working autonomously — nobody can answer questions, so do not ask any. "
        f"Suppress permission errors with '2>/dev/null'."
    )


def delegate_task(client, task: str) -> str:
    """
    Run a subtask on the small model in its own agentic loop.

    The sub-agent gets shell access but no delegation tool of its own, and no view
    of the parent conversation — the task string is its entire brief. Its final
    answer is returned as the parent's tool result.
    """
    label = Text()
    label.append("  └─ ", style="bold magenta")
    label.append(_small_model, style="bold magenta")
    label.append(f"  {task}", style="dim")
    console.print(label)
    console.print()

    messages = [
        {"role": "system", "content": build_subagent_prompt()},
        {"role": "user", "content": task},
    ]

    seen = {}
    for _ in range(SUBAGENT_MAX_TURNS):
        try:
            with Live(Spinner("dots2", text=Text(f"{SUB_INDENT}{_small_model} working...",
                                                 style="dim italic")),
                      console=console, refresh_per_second=12, transient=True):
                response = client.chat(model=_small_model, messages=messages, tools=SHELL_TOOLS)
        except Exception as e:
            console.print(Text(f"{SUB_INDENT}delegation failed: {e}", style="red"))
            console.print()
            return f"Delegation failed: {e}. Handle this subtask yourself."

        msg = response.message
        messages.append(msg)

        if msg.tool_calls:
            for tc in msg.tool_calls:
                command = (tc.function.arguments.get("command", "") or "").strip()
                if command in seen:
                    output = (f"Already run in this subtask, output was:\n{seen[command]}\n\n"
                              "Try something different or report what you have.")
                else:
                    output = run_command(command, indent=SUB_INDENT)
                    seen[command] = output
                messages.append({"role": "tool", "content": output})
        else:
            result = (msg.content or "").strip() or "(no result)"
            lines = result.splitlines() or [""]
            rendered = "\n".join([f"{SUB_INDENT}→ {lines[0]}"]
                                 + [f"{SUB_INDENT}  {l}" for l in lines[1:]])
            console.print(Text(rendered, style="dim magenta"))
            console.print()
            return result

    # Same fix as the main loop: rather than handing the parent an apology and
    # making it redo the work, ask for a conclusion with tools removed.
    try:
        with Live(Spinner("dots2", text=Text(f"{SUB_INDENT}{_small_model} wrapping up...",
                                             style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            response = client.chat(
                model=_small_model,
                messages=messages + [{"role": "user", "content": FINAL_ANSWER_NUDGE}],
            )
        result = (response.message.content or "").strip()
    except Exception:
        result = ""

    if not result:
        return (f"The subtask hit its {SUBAGENT_MAX_TURNS}-step limit without a conclusive "
                f"answer. Handle it yourself if you still need it.")

    console.print(Text(f"{SUB_INDENT}→ {result.splitlines()[0]}", style="dim magenta"))
    console.print()
    return (f"(subtask hit its {SUBAGENT_MAX_TURNS}-step limit; this is its best "
            f"conclusion from what it saw)\n{result}")


def load_skill(name: str) -> str:
    """
    Hand a skill's full text back to the model. An unknown name returns the list of
    real ones rather than an error, so a wrong guess costs a turn instead of the run.
    """
    skill = skills.get(name)

    label = Text()
    label.append("  ◆ ", style="bold blue")
    label.append("skill ", style="bold blue")
    label.append(name or "(unnamed)", style="bold white" if skill else "red")
    console.print(label)
    console.print()

    if skill:
        return skills.render(skill)

    index = skills.index_lines()
    if not index:
        return f"There is no skill named '{name}', and no skills are installed."
    return f"There is no skill named '{name}'. The skills that exist are:\n{index}"


def as_bool(value):
    """Tool arguments arrive as whatever the model felt like emitting, strings included."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def create_skill(args: dict) -> str:
    """
    Write a skill the model composed, and tell it plainly whether the file registered.

    Failures come back as an ordinary tool result rather than an exception, so a bad
    name or a missing description costs the model one turn to correct instead of
    ending the run.
    """
    name = args.get("name", "")

    label = Text()
    label.append("  ◆ ", style="bold blue")
    label.append("create skill ", style="bold blue")
    label.append(name or "(unnamed)", style="bold white")
    console.print(label)

    try:
        skill = skills.write(
            name=name,
            description=args.get("description", ""),
            body=args.get("body", ""),
            model=args.get("model", "any"),
            overwrite=as_bool(args.get("overwrite", False)),
        )
    except Exception as e:
        console.print(Text(f"     ✗ {e}", style="red"))
        console.print()
        return f"create_skill failed: {e}"

    path = os.path.join(skill["dir"], "SKILL.md")
    console.print(Text(f"     → wrote {path}", style="dim"))
    console.print(Text(f"     → re-parsed: registers as '{skill['name']}', "
                       f"model {skill['model']}", style="dim"))
    console.print()

    return (f"Saved and verified: {path} parses back and registers as '{skill['name']}' "
            f"(model {skill['model']}). It is available from the next ask run onward.")


def print_header():
    title = Text()
    title.append("eds", style="bold bright_white")
    title.append(" tui", style="bold cyan")
    console.print()
    console.print(Panel(
        title,
        subtitle=f"[dim]{os.getcwd()}[/dim]",
        border_style="bright_black",
        padding=(0, 2),
        box=box.ROUNDED,
    ))
    console.print()


HISTORY_FILE = os.path.expanduser("~/.eds_tui_history.json")


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(messages):
    # Save only user/assistant/tool messages, not system prompt
    saveable = [m for m in messages if (m if isinstance(m, dict) else vars(m)).get("role") != "system"]
    # Normalize ollama message objects to dicts
    normalized = []
    for m in saveable:
        if isinstance(m, dict):
            normalized.append(m)
        else:
            d = {"role": m.role, "content": m.content or ""}
            if getattr(m, "tool_calls", None):
                d["tool_calls"] = [
                    {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in m.tool_calls
                ]
            normalized.append(d)
    with open(HISTORY_FILE, "w") as f:
        json.dump(normalized, f)


def clear_history():
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)


def self_upgrade():
    console.print("\n[dim]  Upgrading eds-tui from GitHub...[/dim]\n")
    subprocess.run(["pipx", "uninstall", "eds-tui"], text=True, capture_output=True)
    result = subprocess.run(
        ["pipx", "install", "git+https://github.com/edantonio505/edstui.git"],
        text=True
    )
    if result.returncode == 0:
        console.print("[green]  Done. Restart ask to use the new version.[/green]\n")
    else:
        console.print("[red]  pipx failed, trying pip...[/red]\n")
        subprocess.run([
            sys.executable, "-m", "pip", "install",
            "git+https://github.com/edantonio505/edstui.git",
            "--upgrade", "--break-system-packages"
        ])
    sys.exit(0)


WRAP_UP_NUDGE = (
    "SYSTEM NOTE: you have {left} tool-call round(s) left before you are cut off. "
    "Stop widening the search. Run something only if it would change your conclusion; "
    "otherwise answer now from what you already have, and say which parts you could "
    "not confirm."
)

FINAL_ANSWER_NUDGE = (
    "You have used the entire tool-call budget for this request. No further commands "
    "will run. Answer now using only what the commands above already showed you. "
    "State the best conclusion the evidence supports, and say plainly which parts you "
    "could not confirm and what you would have checked next. Do not ask to run anything."
)


def render_answer(content: str):
    """Print a final assistant answer in the standard panel."""
    console.print(Panel(
        Markdown(content or ""),
        border_style="cyan",
        padding=(1, 2),
        box=box.ROUNDED,
    ))
    console.print()


def final_answer(client, messages, active_model: str):
    """
    Make one last call with no tools attached, so the model must answer from the
    evidence already on the transcript.

    A run that hit the cap used to print 'Stopped: too many tool-call rounds' and
    return nothing — every command paid for, no answer, and the user left to redo
    the work by hand. The information needed is almost always already in the
    transcript by then; what the model failed at was stopping, not finding.
    The nudge itself is not saved to history, only the answer it produced.
    """
    try:
        with Live(Spinner("dots2", text=Text("  Wrapping up...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            response = client.chat(
                model=active_model,
                messages=messages + [{"role": "user", "content": FINAL_ANSWER_NUDGE}],
            )
    except Exception as e:
        console.print(Text(f"  Could not produce a final answer: {e}", style="red"))
        console.print()
        save_history(messages)
        return None

    content = (response.message.content or "").strip()
    if not content:
        console.print(Text("  Stopped: too many tool-call rounds, and no answer "
                           "could be salvaged.", style="red"))
        console.print()
        save_history(messages)
        return None

    messages.append(response.message)
    render_answer(content)
    save_history(messages)
    return content


def agentic_loop(client, messages, active_model: str, stats: dict = None):
    """
    Execute the agentic loop: iteratively call the LLM, process tool calls,
    escalate models when needed, and stop when the assistant gives a final answer.

    Parameters
    ----------
    client : ollama.Client
        The Ollama client to use for API calls.
    messages : list[dict]
        Chat history to pass to the model (will be mutated).
    active_model : str
        Currently selected model name. Escalation within the loop may change this.
    stats : dict, optional
        If given, is populated with observability counters for this run:
        'turns', 'delegations', 'commands', 'skills_loaded', 'skills_created',
        'escalated' and the final 'model'.
        Used by --test; ignored in normal use.

    Returns
    -------
    str or None
        The final assistant message content, or None if the loop was terminated early.
    """
    if stats is None:
        stats = {}
    stats.update({"turns": 0, "delegations": 0, "commands": 0, "skills_loaded": 0,
                  "skills_created": 0, "escalated": False, "capped": False,
                  "model": active_model})

    # Output of every command run this turn-loop, so an identical re-run can be
    # answered from here instead of spending a round to learn nothing.
    seen = {}

    # 'turns' is the current model's own budget and resets on escalation; 'total'
    # is the whole run, for the stats line. The run is still bounded -- escalation
    # happens at most once, so the ceiling is SMALL_MAX_TURNS + HARD_MAX_TURNS.
    turns = 0
    total = 0
    while True:
        turns += 1
        total += 1
        stats["turns"] = total
        if turns > HARD_MAX_TURNS:
            stats["capped"] = True
            console.print(Text("  Tool-call budget spent — answering from what "
                               "was found.", style="dim yellow"))
            console.print()
            return final_answer(client, messages, active_model)

        if active_model == _small_model and turns > SMALL_MAX_TURNS:
            console.print(Text(f"  ↑ escalating to {_model}", style="dim yellow"))
            console.print()
            active_model = _model
            stats["escalated"] = True
            # The main model inherits the transcript, not the spent budget. Charging
            # it for the small model's rounds left it with 8 of 14 and cut it off
            # mid-investigation.
            turns = 1

        stats["model"] = active_model

        try:
            with Live(Spinner("dots2", text=Text("  Thinking...", style="dim italic")),
                      console=console, refresh_per_second=12, transient=True):
                response = client.chat(model=active_model, messages=messages,
                                       tools=tools_for(active_model))
        except Exception as e:
            if active_model == _small_model:
                console.print(Text(f"  ↑ {_small_model} failed ({e}), escalating to {_model}",
                                   style="dim yellow"))
                console.print()
                active_model = _model
                stats["escalated"] = True
                continue
            # The main model has nowhere to escalate to, but a request that fails
            # mid-run used to raise a traceback and discard a transcript full of
            # gathered evidence. Try to answer from it first; only if there is
            # nothing to answer from does the error reach the user.
            console.print(Text(f"  {_model} request failed ({e})", style="red"))
            console.print()
            if any(msg.get("role") == "tool" for msg in messages
                   if isinstance(msg, dict)):
                stats["capped"] = True
                return final_answer(client, messages, active_model)
            raise

        msg = response.message
        messages.append(msg)

        if msg.tool_calls:
            console.print(Text("  Running tools", style="bold bright_black"))
            console.print()
            for tc in msg.tool_calls:
                args = tc.function.arguments
                if tc.function.name == "delegate_task":
                    stats["delegations"] += 1
                    output = delegate_task(client, args.get("task", ""))
                elif tc.function.name == "load_skill":
                    stats["skills_loaded"] += 1
                    output = load_skill(args.get("name", ""))
                elif tc.function.name == "create_skill":
                    stats["skills_created"] += 1
                    output = create_skill(args)
                else:
                    stats["commands"] += 1
                    output = run_command_once(args.get("command", ""), seen)
                messages.append({"role": "tool", "content": output})

            # Warn before the cap rather than at it. A model that knows it has two
            # rounds left will usually conclude; one that is cut off without notice
            # never gets the chance. Ridden in on the last tool result because the
            # model is certain to read that, and it creates no fake user turn.
            left = HARD_MAX_TURNS - turns
            if 0 < left <= 2 and messages[-1].get("role") == "tool":
                messages[-1]["content"] += "\n\n" + WRAP_UP_NUDGE.format(left=left)
        else:
            render_answer(msg.content)
            save_history(messages)
            return msg.content


def print_skills():
    """List installed skills, and anything that failed to parse. Used by --skills."""
    found = skills.discover()
    console.print()

    if not found:
        console.print(Text("  No skills installed.", style="dim"))
        console.print(Text(f"  They live in {skills.SKILLS_DIR}", style="dim"))
        console.print(Text("  Create one with:  ask --skill-new <name>", style="dim"))
    else:
        console.print(Text(f"  {len(found)} skill(s) in {skills.SKILLS_DIR}",
                           style="bold bright_white"))
        console.print()
        for skill in found.values():
            line = Text("  ")
            line.append(skill["name"], style="bold cyan")
            if skill["model"] != "any":
                line.append(f"  [{skill['model']} model]", style="dim yellow")
            console.print(line)
            console.print(Text(f"      {skill['description']}", style="dim"))

    broken = skills.problems()
    if broken:
        console.print()
        console.print(Text("  Skipped, could not parse:", style="bold red"))
        for entry, reason in broken:
            console.print(Text(f"    {entry}: {reason}", style="red"))

    console.print()


def scaffold_skill(name: str):
    """Write a starter SKILL.md so the format is discoverable without reading the source."""
    directory = os.path.join(skills.SKILLS_DIR, name)
    path = os.path.join(directory, "SKILL.md")

    if os.path.exists(path):
        console.print(Text(f"\n  {path} already exists.\n", style="red"))
        sys.exit(1)

    os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        f.write(skills.TEMPLATE.format(name=name))

    console.print(Text(f"\n  Created {path}", style="green"))
    console.print(Text("  Edit the description first — it is all the model sees "
                       "until the skill loads.\n", style="dim"))
    sys.exit(0)


def extract_skill(user_input: str):
    """
    Pull a leading /skill-name off the request. Returns (request, skill_or_None).
    An unknown name exits with the list rather than sending '/typo' to the model.
    """
    if not user_input.startswith("/"):
        return user_input, None

    name, _, rest = user_input[1:].partition(" ")
    skill = skills.get(name)
    if not skill:
        console.print(Text(f"\n  No skill named '{name}'.", style="red"))
        print_skills()
        sys.exit(1)

    return rest.strip() or f"Run the {skill['name']} skill.", skill


def looks_like_a_shell_flag(text: str) -> bool:
    """Flags belong on the shell command line, not typed at the ❯ prompt."""
    stripped = text.strip()
    return stripped.startswith("--") or stripped.startswith("ask -")


def shell_flag_hint(text: str):
    stripped = text.strip()
    command = stripped if stripped.startswith("ask ") else f"ask {stripped}"
    console.print(Text("  That is a command-line flag, not a question.", style="yellow"))
    console.print(Text(f"  Run it from your shell instead:  {command}", style="dim"))
    console.print()
    sys.exit(0)


SIMPLE_PROBE = "how many .py files are in this directory"
COMPLEX_PROBE = ("refactor the agentic loop into its own module and explain the "
                 "tradeoffs of each approach")
DELEGATION_PROBE = ("Delegate two subtasks: first, count how many *.py files are in the "
                    "current directory; second, report the name of the current git branch. "
                    "Then give me both answers.")
ESCALATION_PROBE = "Run 'pwd', then separately run 'whoami', then report both results."

# A skill nothing else on the machine could satisfy, so a match proves the skill
# reached the model rather than the model already knowing the answer.
FIXTURE_SKILL = """---
name: selfcheck-widget
description: Report the status of the widget subsystem. Only this skill knows how.
model: small
---

When asked about the widget subsystem, reply with exactly: WIDGET-OK
"""
SKILL_PROBE = "what is the status of the widget subsystem"


def self_check():
    """
    Run the --test self-check: exercise routing, delegation and escalation against
    the live server and report pass/fail. Exits nonzero if anything failed.
    """
    global _small_model, SMALL_MAX_TURNS

    client = make_client()
    started_all = time.time()
    results = []

    console.print()
    console.print(Text("  eds tui self-check", style="bold bright_white"))
    console.print(Text(f"  main: {_model}    small: {_small_model}", style="dim"))
    console.print()

    def check(name, fn):
        started = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ok)
        line = Text()
        line.append("  ✓ " if ok else "  ✗ ", style="bold green" if ok else "bold red")
        line.append(f"{name:<34}", style="white" if ok else "red")
        line.append(f"{time.time() - started:6.1f}s", style="dim")
        if detail:
            line.append(f"   {detail}", style="dim" if ok else "red")
        console.print(line)

    def models_present():
        names = {m.model for m in client.list().models}
        missing = [m for m in (_model, _small_model) if m not in names]
        return not missing, "both served" if not missing else f"missing: {', '.join(missing)}"

    def triage_simple():
        picked, _ = triage(client, SIMPLE_PROBE)
        return picked == _small_model, f"→ {picked}"

    def triage_complex():
        picked, _ = triage(client, COMPLEX_PROBE)
        return picked == _model, f"→ {picked}"

    def fast_forces_small():
        picked, _ = resolve_model(client, COMPLEX_PROBE, force_fast=True)
        return picked == _small_model, f"→ {picked}"

    def smart_forces_main():
        picked, _ = resolve_model(client, SIMPLE_PROBE, force_smart=True)
        return picked == _model, f"→ {picked}"

    def delegation_works():
        console.print()
        messages = [{"role": "system", "content": build_system_prompt(_model)},
                    {"role": "user", "content": DELEGATION_PROBE}]
        stats = {}
        agentic_loop(client, messages, _model, stats=stats)
        n = stats["delegations"]
        return n >= 1, f"{_model} spawned {_small_model} ×{n}"

    def escalation_fires():
        console.print()
        original = SMALL_MAX_TURNS
        globals()["SMALL_MAX_TURNS"] = 1
        try:
            messages = [{"role": "system", "content": build_system_prompt(_small_model)},
                        {"role": "user", "content": ESCALATION_PROBE}]
            stats = {}
            agentic_loop(client, messages, _small_model, stats=stats)
            return stats["escalated"], f"turn cap 1 → {stats['model']} after {stats['turns']} turns"
        finally:
            globals()["SMALL_MAX_TURNS"] = original

    def cap_still_answers():
        """
        The cap must produce an answer from what was gathered, never silence. This is
        the check that would have caught a capped run returning None after spending
        every round.
        """
        console.print()
        original = HARD_MAX_TURNS
        globals()["HARD_MAX_TURNS"] = 1
        try:
            messages = [{"role": "system", "content": build_system_prompt(_model)},
                        {"role": "user", "content":
                         "List the files in the current directory and tell me what you see."}]
            stats = {}
            answer = agentic_loop(client, messages, _model, stats=stats)
            ok = bool(answer) and stats["capped"]
            return ok, (f"capped at 1 round → {len(answer or '')}-char answer"
                        if ok else "capped run returned no answer")
        finally:
            globals()["HARD_MAX_TURNS"] = original

    def repeat_command_is_cached():
        """A re-run of an identical command must not spend a round on the shell again."""
        seen = {}
        first = run_command_once("echo cache-probe", seen)
        second = run_command_once("echo cache-probe", seen)
        ok = ("cache-probe" in first
              and "already ran earlier" in second
              and len(seen) == 1)
        return ok, "second identical command replayed, not re-executed"

    def huge_output_is_clipped():
        """One unfiltered grep must not be able to blow the context and kill the run."""
        big = "x" * 500_000
        clipped = clip_output(big)
        ok = len(clipped) < MAX_OUTPUT_CHARS + 500 and "characters cut" in clipped
        return ok, f"500,000 chars → {len(clipped):,} with a note to narrow the search"

    def bad_small_model_falls_back():
        original = _small_model
        globals()["_small_model"] = "does-not-exist:1b"
        try:
            picked, _ = triage(client, SIMPLE_PROBE)
            return picked == _model, f"→ {picked}"
        finally:
            globals()["_small_model"] = original

    def with_fixture_skills(fn):
        """
        Run fn against a throwaway fixture skill. --test must never touch the
        user's real ~/.eds_tui/skills, so SKILLS_DIR is repointed and restored.
        """
        tmp = tempfile.mkdtemp(prefix="eds-tui-selfcheck-")
        original = skills.SKILLS_DIR
        try:
            directory = os.path.join(tmp, "selfcheck-widget")
            os.makedirs(directory)
            with open(os.path.join(directory, "SKILL.md"), "w") as f:
                f.write(FIXTURE_SKILL)
            skills.SKILLS_DIR = tmp
            skills.reset_cache()
            return fn()
        finally:
            skills.SKILLS_DIR = original
            skills.reset_cache()
            shutil.rmtree(tmp, ignore_errors=True)

    def skills_parse():
        def body():
            found = skills.discover()
            skill = found.get("selfcheck-widget")
            if not skill:
                return False, f"fixture not discovered ({list(found)})"
            ok = skill["model"] == "small" and "WIDGET-OK" in skill["body"]
            return ok, "name, description, model and body round-tripped"
        return with_fixture_skills(body)

    def skills_triage_matches():
        def body():
            _, skill = triage(client, SKILL_PROBE)
            return bool(skill and skill["name"] == "selfcheck-widget"), \
                f"→ {skill['name'] if skill else 'no match'}"
        return with_fixture_skills(body)

    def skills_load_tool():
        def body():
            console.print()
            messages = [
                {"role": "system", "content": build_system_prompt(_model)},
                {"role": "user", "content":
                    "Load the selfcheck-widget skill, follow it, and report the "
                    "widget subsystem status."},
            ]
            stats = {}
            agentic_loop(client, messages, _model, stats=stats)
            n = stats["skills_loaded"]
            return n >= 1, f"{_model} called load_skill ×{n}"
        return with_fixture_skills(body)

    def skills_create_tool():
        def body():
            console.print()
            messages = [
                {"role": "system", "content": build_system_prompt(_model)},
                {"role": "user", "content":
                    "Create a skill named selfcheck-made whose description is about "
                    "reporting the fixture subsystem status, and whose body says to reply "
                    "with the exact token FIXTURE-OK."},
            ]
            stats = {}
            agentic_loop(client, messages, _model, stats=stats)
            made = skills.get("selfcheck-made")
            if not made:
                return False, f"called create_skill ×{stats['skills_created']}, nothing registered"
            return True, f"wrote and re-parsed '{made['name']}'"
        return with_fixture_skills(body)

    def skills_write_validates():
        def body():
            cases = [
                ("../../.bashrc", "escape", "traversal name"),
                ("Bad Name", "spaces", "malformed name"),
                ("ok-name", "", "empty description"),
            ]
            for name, description, label in cases:
                try:
                    skills.write(name=name, description=description, body="x")
                except (ValueError, FileExistsError):
                    continue
                return False, f"{label} was accepted"

            # A multi-line description would truncate the frontmatter; it must be folded.
            written = skills.write(name="ok-name", description="line one\nline two",
                                   body="do the thing")
            if "\n" in written["description"]:
                return False, "multi-line description survived into the frontmatter"
            return True, "3 bad inputs rejected, newline folded"
        return with_fixture_skills(body)

    def skills_precedence():
        def body():
            skill = skills.get("selfcheck-widget")          # pins model: small
            pinned, _ = resolve_model(client, COMPLEX_PROBE, skill=skill)
            forced, _ = resolve_model(client, COMPLEX_PROBE, force_smart=True, skill=skill)
            return pinned == _small_model and forced == _model, \
                f"pin → {pinned}, --smart overrides → {forced}"
        return with_fixture_skills(body)

    check("server + both models reachable", models_present)
    check("triage: simple → small model", triage_simple)
    check("triage: complex → main model", triage_complex)
    check("--fast forces small model", fast_forces_small)
    check("--smart forces main model", smart_forces_main)
    check("delegation: main spawns small", delegation_works)
    check("escalation past turn cap", escalation_fires)
    check("turn cap still answers", cap_still_answers)
    check("repeated command is cached", repeat_command_is_cached)
    check("huge output is clipped", huge_output_is_clipped)
    check("bad small model falls back", bad_small_model_falls_back)
    check("skills: discovered and parsed", skills_parse)
    check("skills: triage matches a skill", skills_triage_matches)
    check("skills: load_skill returns body", skills_load_tool)
    check("skills: create_skill writes+parses", skills_create_tool)
    check("skills: write rejects bad input", skills_write_validates)
    check("skills: /name pin vs flag", skills_precedence)

    passed = sum(1 for r in results if r)
    failed = len(results) - passed
    console.print()
    summary = Text("  ")
    summary.append(f"{passed} passed", style="bold green")
    summary.append("  ")
    summary.append(f"{failed} failed", style="bold red" if failed else "dim")
    summary.append(f"{time.time() - started_all:>18.1f}s", style="dim")
    console.print(summary)
    console.print()
    sys.exit(1 if failed else 0)


def main():
    if "--upgrade" in sys.argv:
        self_upgrade()

    if "--skills" in sys.argv:
        print_skills()
        sys.exit(0)

    if "--skill-new" in sys.argv:
        i = sys.argv.index("--skill-new")
        if i + 1 >= len(sys.argv):
            console.print(Text("\n  Usage: ask --skill-new <name>\n", style="red"))
            sys.exit(1)
        scaffold_skill(sys.argv[i + 1])

    if "--test" in sys.argv:
        self_check()

    is_continue = "--continue" in sys.argv
    force_fast = "--fast" in sys.argv
    force_smart = "--smart" in sys.argv
    client = make_client()

    print_header()

    cwd_short = os.path.basename(os.getcwd()) or os.getcwd()

    # Load or start history
    if is_continue:
        prior = load_history()
        if prior:
            console.print(f"[dim]  Continuing conversation ({len([m for m in prior if m.get('role') == 'user'])} previous messages)[/dim]\n")
        else:
            console.print("[dim]  No previous conversation found, starting fresh.[/dim]\n")
    else:
        clear_history()
        prior = []

    pasted_blocks = []
    kb = KeyBindings()

    @kb.add(Keys.BracketedPaste)
    def handle_paste(event):
        text = event.data
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) > 1:
            pasted_blocks.append(text)
            event.current_buffer.insert_text(f"[+{len(lines)} lines]")
        else:
            event.current_buffer.insert_text(text.strip())

    session = PromptSession(key_bindings=kb)
    prompt = ANSI(f"\033[1;36m {cwd_short}\033[0m\033[1;97m ❯ \033[0m")

    try:
        raw = session.prompt(prompt)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)

    paste_iter = iter(pasted_blocks)
    user_input = re.sub(r"\[\+\d+ lines\]", lambda _: next(paste_iter), raw).strip()

    if not user_input:
        console.print("[dim]No input. Exiting.[/dim]")
        sys.exit(0)

    console.print()

    if looks_like_a_shell_flag(user_input):
        shell_flag_hint(user_input)

    user_input, skill = extract_skill(user_input)

    # Pick the model and the skill: forced by flag or /name, otherwise triaged.
    # A forced model needs no call, and neither does a skill that pins one.
    if force_fast or force_smart or pin_for(skill):
        active, skill = resolve_model(client, user_input, force_fast, force_smart, skill)
    else:
        with Live(Spinner("dots2", text=Text("  Routing...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            active, skill = resolve_model(client, user_input, skill=skill)

    status = Text(f"  {active}", style="dim")
    if skill:
        status.append(f"  ·  skill: {skill['name']}", style="dim cyan")
    console.print(status)
    console.print()

    messages = [{"role": "system", "content": build_system_prompt(active, skill)}]
    messages += prior
    messages.append({"role": "user", "content": user_input})

    # Delegated agentic loop
    agentic_loop(client, messages, active)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)
