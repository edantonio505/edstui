#!/usr/bin/env python3
import os
import sys
import re
import json
import subprocess
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

console = Console()

_model = os.environ.get("EDS_TUI_MODEL", "qwen3.6:35b")
_small_model = os.environ.get("EDS_TUI_SMALL_MODEL", "ornith:35b")

SMALL_MAX_TURNS = 6    # tool-call rounds before escalating off the small model
HARD_MAX_TURNS = 14    # absolute ceiling, prevents a runaway loop


def make_client():
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

SHELL_TOOLS = [RUN_COMMAND_TOOL]                    # sub-agent and small model: shell only
MAIN_TOOLS = [RUN_COMMAND_TOOL, DELEGATE_TOOL]      # main model: shell + delegation


def tools_for(model: str):
    """The main model can delegate; the small model just runs commands."""
    return MAIN_TOOLS if model == _model else SHELL_TOOLS


def build_system_prompt() -> str:
    return (
        f"You are a helpful terminal assistant running on Ubuntu Linux. "
        f"The user's current working directory is: {os.getcwd()}. "
        f"All commands run relative to this directory unless a full path is needed. "
        f"You have access to the user's terminal via the run_command tool. "
        f"Search strategy: "
        f"- Use 'find' to locate files or directories by name. "
        f"- Use 'grep -r' to search inside file contents when looking for text, keywords, or strings. "
        f"- Combine both when needed. "
        f"- Suppress permission errors with '2>/dev/null'. "
        f"If a delegate_task tool is available to you, hand it the mechanical legwork — "
        f"gathering listings, counting things, checking status — and spend your own effort "
        f"on the reasoning and the final answer. Each delegated task must stand alone, since "
        f"the helper cannot see this conversation. Run commands yourself when the work is "
        f"trivial or needs your judgement. "
        f"Think step by step before acting. Plan the right command for the task. "
        f"Be direct and concise in your final answer."
    )


TRIAGE_SYSTEM = (
    "You route requests for a terminal assistant. Reply with exactly one word: SIMPLE or COMPLEX.\n"
    "SIMPLE = answerable directly or with one or two straightforward shell commands "
    "(lookups, listing files, checking status, short factual questions, a single command).\n"
    "COMPLEX = needs multi-step reasoning, writing or refactoring code, debugging, planning, "
    "chaining many commands, or careful judgement.\n"
    "Output only that one word."
)


def resolve_model(client, user_input: str, force_fast: bool = False,
                  force_smart: bool = False) -> str:
    """Which model owns this request: forced by flag, otherwise triaged."""
    if force_smart:
        return _model
    if force_fast:
        return _small_model
    return pick_model(client, user_input)


def pick_model(client, user_input: str) -> str:
    """Return the model to run this request on. Falls back to the main model on any doubt."""
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
        verdict = (response.message.content or "").strip().upper()
        return _small_model if verdict.startswith("SIMPLE") else _model
    except Exception:
        # Triage must never block the request; when in doubt use the main model.
        return _model


def indent_lines(text: str, indent: str) -> str:
    return "\n".join(f"{indent}{line}" for line in text.splitlines())


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
        output = output.strip() or "(no output)"
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


SUBAGENT_MAX_TURNS = 5
SUB_INDENT = "     "


def build_subagent_prompt() -> str:
    return (
        f"You are a focused helper for a terminal assistant, running on Ubuntu Linux. "
        f"The current working directory is: {os.getcwd()}. "
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
                output = run_command(tc.function.arguments.get("command", ""), indent=SUB_INDENT)
                messages.append({"role": "tool", "content": output})
        else:
            result = (msg.content or "").strip() or "(no result)"
            lines = result.splitlines() or [""]
            rendered = "\n".join([f"{SUB_INDENT}→ {lines[0]}"]
                                 + [f"{SUB_INDENT}  {l}" for l in lines[1:]])
            console.print(Text(rendered, style="dim magenta"))
            console.print()
            return result

    return (
        f"The subtask hit its {SUBAGENT_MAX_TURNS}-step limit without a conclusive answer. "
        f"Handle it yourself if you still need it."
    )


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
        'turns', 'delegations', 'commands', 'escalated' and the final 'model'.
        Used by --test; ignored in normal use.

    Returns
    -------
    str or None
        The final assistant message content, or None if the loop was terminated early.
    """
    if stats is None:
        stats = {}
    stats.update({"turns": 0, "delegations": 0, "commands": 0,
                  "escalated": False, "model": active_model})

    turns = 0
    while True:
        turns += 1
        stats["turns"] = turns
        if turns > HARD_MAX_TURNS:
            console.print("[red]  Stopped: too many tool-call rounds.[/red]\n")
            save_history(messages)
            return None

        if active_model == _small_model and turns > SMALL_MAX_TURNS:
            console.print(Text(f"  ↑ escalating to {_model}", style="dim yellow"))
            console.print()
            active_model = _model
            stats["escalated"] = True

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
                else:
                    stats["commands"] += 1
                    output = run_command(args.get("command", ""))
                messages.append({"role": "tool", "content": output})
        else:
            console.print(Panel(
                Markdown(msg.content),
                border_style="cyan",
                padding=(1, 2),
                box=box.ROUNDED,
            ))
            console.print()
            save_history(messages)
            return msg.content


SIMPLE_PROBE = "how many .py files are in this directory"
COMPLEX_PROBE = ("refactor the agentic loop into its own module and explain the "
                 "tradeoffs of each approach")
DELEGATION_PROBE = ("Delegate two subtasks: first, count how many *.py files are in the "
                    "current directory; second, report the name of the current git branch. "
                    "Then give me both answers.")
ESCALATION_PROBE = "Run 'pwd', then separately run 'whoami', then report both results."


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
        picked = pick_model(client, SIMPLE_PROBE)
        return picked == _small_model, f"→ {picked}"

    def triage_complex():
        picked = pick_model(client, COMPLEX_PROBE)
        return picked == _model, f"→ {picked}"

    def fast_forces_small():
        picked = resolve_model(client, COMPLEX_PROBE, force_fast=True)
        return picked == _small_model, f"→ {picked}"

    def smart_forces_main():
        picked = resolve_model(client, SIMPLE_PROBE, force_smart=True)
        return picked == _model, f"→ {picked}"

    def delegation_works():
        console.print()
        messages = [{"role": "system", "content": build_system_prompt()},
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
            messages = [{"role": "system", "content": build_system_prompt()},
                        {"role": "user", "content": ESCALATION_PROBE}]
            stats = {}
            agentic_loop(client, messages, _small_model, stats=stats)
            return stats["escalated"], f"turn cap 1 → {stats['model']} after {stats['turns']} turns"
        finally:
            globals()["SMALL_MAX_TURNS"] = original

    def bad_small_model_falls_back():
        original = _small_model
        globals()["_small_model"] = "does-not-exist:1b"
        try:
            picked = pick_model(client, SIMPLE_PROBE)
            return picked == _model, f"→ {picked}"
        finally:
            globals()["_small_model"] = original

    check("server + both models reachable", models_present)
    check("triage: simple → small model", triage_simple)
    check("triage: complex → main model", triage_complex)
    check("--fast forces small model", fast_forces_small)
    check("--smart forces main model", smart_forces_main)
    check("delegation: main spawns small", delegation_works)
    check("escalation past turn cap", escalation_fires)
    check("bad small model falls back", bad_small_model_falls_back)

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

    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += prior
    messages.append({"role": "user", "content": user_input})

    # Pick the model: forced by flag, otherwise triaged by the small model
    if force_fast or force_smart:
        active = resolve_model(client, user_input, force_fast, force_smart)
    else:
        with Live(Spinner("dots2", text=Text("  Routing...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            active = resolve_model(client, user_input)

    console.print(Text(f"  {active}", style="dim"))
    console.print()

    # Delegated agentic loop
    agentic_loop(client, messages, active)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)
