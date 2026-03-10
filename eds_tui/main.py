#!/usr/bin/env python3
import os
import sys
import json
import re
import subprocess
from openai import OpenAI, APIStatusError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from rich import box

console = Console()

_base_url = os.environ.get("EDS_TUI_URL", "http://192.168.0.110:11434") + "/v1"

client = OpenAI(
    api_key="ollama",
    base_url=_base_url
)

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


def build_system_prompt(fallback: bool = False) -> str:
    base = (
        f"You are a helpful terminal assistant running on Ubuntu Linux. "
        f"The user's current working directory is: {os.getcwd()}. "
        f"All commands run relative to this directory unless a full path is needed. "
        f"Search strategy: "
        f"- Use 'find' to locate files or directories by name. "
        f"- Use 'grep -r' to search inside file contents when looking for text, keywords, or strings. "
        f"- Combine both when needed. "
        f"- Suppress permission errors with '2>/dev/null'. "
        f"Think step by step before acting. Plan the right command for the task. "
        f"Be direct and concise in your final answer."
    )
    if fallback:
        base += (
            f" When you need to run a shell command, output it in a bash code block like:\n"
            f"```bash\nyour command here\n```\n"
            f"After each command block, wait for the output before continuing. "
            f"When you have the final answer, output it normally without a code block."
        )
    else:
        base += f" You have access to the user's terminal via the run_command tool."
    return base


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


def call_model(messages, use_tools: bool):
    kwargs = dict(model="qwen3.5:35b", messages=messages, max_tokens=8192)
    if use_tools:
        kwargs["tools"] = TOOLS
    return client.chat.completions.create(**kwargs)


def run_with_tools(messages):
    """Agentic loop using the tools API."""
    while True:
        with Live(Spinner("dots2", text=Text("  Thinking...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            response = call_model(messages, use_tools=True)

        message = response.choices[0].message

        if message.tool_calls:
            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ]
            })
            console.print(Text("  Running tools", style="bold bright_black"))
            console.print()
            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments)
                output = run_command(args["command"])
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
        else:
            console.print(Panel(Markdown(message.content), border_style="cyan",
                                padding=(1, 2), box=box.ROUNDED))
            console.print()
            break


def run_fallback(messages):
    """Fallback: parse ```bash blocks from text and execute them manually."""
    while True:
        with Live(Spinner("dots2", text=Text("  Thinking...", style="dim italic")),
                  console=console, refresh_per_second=12, transient=True):
            response = call_model(messages, use_tools=False)

        content = response.choices[0].message.content

        # Extract all ```bash ... ``` blocks
        commands = re.findall(r"```bash\s*\n(.*?)```", content, re.DOTALL)

        if commands:
            console.print(Text("  Running tools", style="bold bright_black"))
            console.print()
            messages.append({"role": "assistant", "content": content})
            results = []
            for cmd in commands:
                cmd = cmd.strip()
                output = run_command(cmd)
                results.append(f"$ {cmd}\n{output}")
            messages.append({"role": "user", "content": "Command output:\n" + "\n\n".join(results)})
        else:
            console.print(Panel(Markdown(content), border_style="cyan",
                                padding=(1, 2), box=box.ROUNDED))
            console.print()
            break


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

    console.print(f"[dim]  Connecting to: {_base_url}[/dim]\n")

    # Try tools API first, fall back to text parsing if not supported
    try:
        messages = [
            {"role": "system", "content": build_system_prompt(fallback=False)},
            {"role": "user", "content": user_input}
        ]
        run_with_tools(messages)
    except APIStatusError as e:
        if e.status_code == 405:
            console.print(f"[dim]  Tools API not supported (405), switching to fallback mode...[/dim]")
            console.print(f"[dim]  Response body: {e.response.text}[/dim]\n")
            messages = [
                {"role": "system", "content": build_system_prompt(fallback=True)},
                {"role": "user", "content": user_input}
            ]
            run_fallback(messages)
        else:
            raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(0)
    except APIStatusError as e:
        console.print(f"\n[red]API error {e.status_code}[/red]")
        console.print(f"[dim]URL: {_base_url}[/dim]")
        console.print(f"[dim]Body: {e.response.text}[/dim]")
        sys.exit(1)
