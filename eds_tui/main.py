#!/usr/bin/env python3
import os
import sys
import subprocess
import ollama
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from rich import box

console = Console()

_model = "qwen3.5:35b"


def make_client():
    host = os.environ.get("EDS_TUI_URL", "http://192.168.0.110:11434").rstrip("/")
    token = os.environ.get("EDS_TUI_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return ollama.Client(host=host, headers=headers)

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
        console.print(f"[dim]{output}[/dim]\n")
        return output
    except subprocess.TimeoutExpired:
        console.print("[red]  Error: command timed out after 30 seconds[/red]\n")
        return "Error: command timed out after 30 seconds"
    except Exception as e:
        console.print(f"[red]  Error: {e}[/red]\n")
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


def main():
    client = make_client()

    print_header()

    cwd_short = os.path.basename(os.getcwd()) or os.getcwd()
    prompt_text = Text()
    prompt_text.append(f" {cwd_short}", style="bold cyan")
    prompt_text.append(" ❯ ", style="bold bright_white")
    console.print(prompt_text, end="")

    try:
        user_input = input().strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)

    if not user_input:
        console.print("[dim]No input. Exiting.[/dim]")
        sys.exit(0)

    console.print()

    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": user_input}
    ]

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
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)
