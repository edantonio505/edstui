#!/usr/bin/env python3
import os
import sys
import re
import json
import subprocess
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

_model = "qwen3.5:35b"


def make_client():
    host = os.environ.get("EDS_TUI_URL", "http://192.168.0.110:11434").rstrip("/")
    token = os.environ.get("EDS_TUI_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return ollama.Client(host=host, headers=headers, timeout=None)

TOOLS = [
    {
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
]


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
        f"Think step by step before acting. Plan the right command for the task. "
        f"Be direct and concise in your final answer."
    )


def run_command(command: str) -> str:
    label = Text()
    label.append("  $ ", style="bold green")
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
        output_text = Text(output, style="dim")
        console.print(output_text)
        console.print()
        return output
    except subprocess.TimeoutExpired:
        console.print("[red]  Error: command timed out after 30 seconds[/red]\n")
        return "Error: command timed out after 30 seconds"
    except Exception as e:
        # Use Text to avoid Rich markup interpretation in error messages
        error_text = Text()
        error_text.append("  Error: ", style="red")
        error_text.append(str(e), style="red")
        console.print(error_text)
        console.print()
        return f"Error: {e}"


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
    result = subprocess.run(
        ["pipx", "install", "git+https://github.com/edantonio505/edstui.git", "--force"],
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


def main():
    if "--upgrade" in sys.argv:
        self_upgrade()

    is_continue = "--continue" in sys.argv
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

    # Agentic loop
    while True:
        with Live(Spinner("dots2", text=Text("  Thinking...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            response = client.chat(model=_model, messages=messages, tools=TOOLS)

        msg = response.message
        messages.append(msg)

        if msg.tool_calls:
            console.print(Text("  Running tools", style="bold bright_black"))
            console.print()
            for tc in msg.tool_calls:
                command = tc.function.arguments.get("command", "")
                output = run_command(command)
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
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)
