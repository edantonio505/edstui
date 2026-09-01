# PROMPT — feed this to another eds-tui instance (paste the text starting at "You are" below into a fresh `ask` session)

Setup done on the source machine for reference: Claude Code v2.1.252 at `~/.local/bin/claude`;
five skills registered at `~/.eds_tui/skills/`.

---

You are an eds-tui assistant on a fresh Ubuntu machine. Your job in this session: set up Claude Code (claude CLI) and register five reusable skills so this instance behaves like the reference machine. Do all the work below, verify each step, and report what you did.

## Step 0 — Verify Claude Code is installed

```bash
which claude && claude --version
```

- If missing, install it (pick whichever applies): `npm install -g @anthropic-ai/claude-code` **or** `curl -fsSL https://claude.ai/install.sh | bash`, then re-check `which claude`.
- Note the actual install path — you will use it in the skill body in Step 1 (on the reference machine it is `/home/<user>/.local/bin/claude`).
- If the CLI asks for authentication on first real use, tell the user to run `claude` once interactively and log in; do not block on it.

## Step 1 — Register the `hard-task-claude` skill (the core skill for "use claude")

Call your `create_skill` tool with EXACTLY:
- name: `hard-task-claude`
- description: `When a task is too hard to solve with direct shell work alone — complex debugging, large multi-file refactors, algorithm design, optimization, stuck loops — delegate it to the Claude Code CLI (claude -p) to solve autonomously in the current directory.`
- model: `main`
- body: the full text between the BEGIN/END BODY markers below (replace CLAUDE_PATH with the path you found in Step 0):

BEGIN BODY
# Delegation to Claude Code (claude CLI)

`claude` (Claude Code) is installed at CLAUDE_PATH. It has its own shell access to the current working directory and can read, edit, and run things autonomously. Use it as a heavier-weight solver for tasks you cannot solve directly.

## When to use
- Complex debugging where your own attempts are stuck or the root cause is unclear.
- Large multi-file refactors, feature implementation, or architecture work.
- Algorithm design, performance optimization, or "hard reasoning" tasks.
- Anything where iterating against a running program is expensive to do by hand.

Do NOT use it for trivial lookups, quick greps, or one-liners — just do those yourself.

## Basic invocation

Print mode (`-p`) is non-interactive: it runs the task to completion and prints the result. Run from the project directory:

```bash
timeout 1800 claude -p "<full task prompt>" --dangerously-skip-permissions
```

- `--dangerously-skip-permissions` lets it run commands without prompting (the user wants it to act autonomously). This is the user's own desktop project.
- `timeout 1800` prevents a hung session from blocking forever (30 min).
- For the hardest tasks: add `--model opus`.
- Optional soft cost cap: `--max-budget-usd 5`.
- `--output-format json` gives structured JSON output (result, cost, session_id) if you need to parse it.

## Writing the prompt

The CLI does not see this conversation — the prompt must be fully self-contained:

1. State the goal concretely and the definition of done (e.g. "tests in tests/ pass").
2. Give context: relevant files, current error output, what has already been tried.
3. Set constraints: which files not to touch, style rules, keep changes minimal, etc.
4. Instruction on decisions: "Do not ask questions. Where a choice is needed, pick the most reasonable option consistent with the existing codebase and state what you chose and why."

## Answering questions / continuing

`claude -p` cannot pause for input. If its output ends with an unresolved question or an incomplete step:

1. **Default: answer it yourself.** Pick the most reasonable option and continue. Re-run with the answer baked in, or continue the same session in the directory:
   ```bash
   timeout 1800 claude -p --continue "<your answer + next step>" --dangerously-skip-permissions
   ```
2. **Only wait for the user** when the decision is genuinely theirs to make: destructive/irreversible actions (deleting data, force-pushing, dropping schemas), security-sensitive changes, cost/account decisions, or a preference you cannot reasonably infer. In that case, present the options succinctly and stop.

## Verification and reporting

Never trust the report blindly:
- Inspect what changed (`git status`, `git diff` if it's a repo).
- Run the project's tests or the relevant command yourself.
- Fix small remaining gaps directly; only send it back via `--continue` if a real chunk is left.
- Report to the user: what was done, key decisions made, and verification evidence.
END BODY

## Step 2 — Register the four helper skills (skip any the user doesn't need)

Same mechanism: call `create_skill` with the name/description/model given, and use the body verbatim.

### 2a. `cooltime`
- description: `When the user asks for the time (or says "time", "what time is it", "clock") — show the time with the animated ASCII clock + figlet banner in the terminal.`
- model: `small`
- body: the procedure in section A of the appendix below, including the full `~/.local/bin/cooltime` script in its heredoc.

### 2b. `weather`
- description: `When the user asks about the weather / temperature outside ("what's the temp", "how's the weather", "is it raining", "weather outside") — show the current weather plus an ASCII temperature graph and 3-day outlook from wttr.in.`
- model: `small`
- body: the procedure in section B of the appendix below, including the full `/tmp/wx.py` script.

### 2c. `latest-news`
- description: `When the user asks for the "latest news", "newest news", "what's in the news", or similar — fetch and summarize the latest US and world news from public RSS feeds.`
- model: `any`
- body: the procedure in section C of the appendix below (the four curl RSS fetches + answer composition rules).

### 2d. `desktop-ports`
- description: `When the user asks which ports are in use / available on the Desktop, what ports projects use, or wants a port for a new project`
- model: `any`
- body: NOTE — the port table in section D of the appendix is specific to the reference machine's ~/Desktop. If this machine has different projects, run the "Re-scan command" from section D first and build the skill from the results, keeping the same structure (in-use table / free ranges / re-scan command).

## Step 3 — Verify

1. Confirm the skills are registered (they should now appear in your system prompt's skill list / `ls ~/.eds_tui/skills`).
2. Smoke-test each: ask yourself to demonstrate one skill, e.g. run `python3 /tmp/wx.py` pattern, and do one real `claude -p` smoke test:
   ```bash
   timeout 300 claude -p "Print the output of 'uname -a' and stop." --dangerously-skip-permissions
   ```
3. Report: claude version + path, list of registered skills, and any failures.

---
# APPENDIX (skill bodies, verbatim)

## A. cooltime body

# Cool time display

Show the current time with an animated terminal graphic. The script lives at `~/.local/bin/cooltime` (a Python 3 script, uses only stdlib + optional `figlet` for the finale banner).

## Run it

```sh
python3 ~/.local/bin/cooltime
```

What it does (~8s total):
1. Draws an ASCII analog clock face and animates the hour/minute hands ticking for ~6 seconds (0.7s per frame).
2. Prints the digital time under the face each frame.
3. Finishes with a `figlet` banner of `HH:MM` and the full date line.

Requires: `python3` (always present). `figlet` is optional — if missing, the finale just shows the date line.

## If the script is missing (new machine)

Recreate it with:

```sh
mkdir -p ~/.local/bin && cat > ~/.local/bin/cooltime <<'SCRIPT'
import os, math, sys, time, datetime, shutil, subprocess

def render(now):
    W, H = 45, 27
    grid = [[' ']*W for _ in range(H)]
    cx, cy = W/2, H/2
    R = min(W, H)/2 - 2
    def put(x, y, ch):
        xi, yi = int(round(x)), int(round(y))
        if 0 <= yi < H and 0 <= xi < W: grid[yi][xi] = ch
    for y in range(H):
        for x in range(W):
            d = math.hypot(x-cx, y-cy)
            if abs(d - R) < 0.6: put(x, y, 'O' if d < R else 'o')
    for i in range(12):
        a = math.radians(i*30 - 90)
        put(cx + math.cos(a)*(R-2), cy + math.sin(a)*(R-2), '|' if i%3==0 else ':')
    def hand(frac, chars):
        a = math.radians(frac*360 - 90)
        for s, ch in enumerate(chars):
            put(cx + math.cos(a)*s*0.9, cy + math.sin(a)*s*0.9, ch)
    h = now.hour % 12 + now.minute/60
    m = now.minute + now.second/60
    hand(h/12, ['@']*6)
    hand(m/60, ['o','q']*6 + ['@'])
    put(cx, cy, '+')
    return '\n'.join(''.join(r).rstrip() for r in grid)

def clear():
    sys.stdout.write('\x1b[H\x1b[2J')
    sys.stdout.flush()

def main():
    clear()
    t0 = time.time()
    while time.time() - t0 < 6.0:
        now = datetime.datetime.now()
        clear()
        print(render(now))
        print()
        print('  ====  %s  ====' % now.strftime('%H:%M:%S'))
        sys.stdout.flush()
        time.sleep(0.7)
    now = datetime.datetime.now()
    clear()
    if shutil.which('figlet'):
        subprocess.run(['figlet', '-c', now.strftime('%H:%M')])
    print(now.strftime('   %A, %B %d — %H:%M:%S'))
    print('   time is now.')

main()
SCRIPT
chmod +x ~/.local/bin/cooltime
```

## Notes

- Screen clearing is done by writing ANSI escapes directly to stdout (`\x1b[H\x1b[2J`) — do NOT route them through `os.system()`, that produces `sh: [H[2J: not found` errors.
- The clock hands use `@`/`o`/`q` characters so the hand stays legible at any angle.
- If the user wants a different duration, edit the `6.0` in the while loop condition.

## B. weather body

# Weather with ASCII graph

Shows current conditions at the user's location (IP-based via wttr.in), an ASCII bar graph of today's hourly temperatures, and a 3-day outlook. Optional argument: a place name (e.g. "Lisbon", "NYC").

## Steps

1. Write the script to `/tmp/wx.py` with this exact heredoc:

```bash
cat << 'EOF' > /tmp/wx.py
import json, sys, urllib.parse, urllib.request

loc = sys.argv[1] if len(sys.argv) > 1 else ''
url = ('https://wttr.in/' + urllib.parse.quote(loc) + '/?format=j1') if loc else 'https://wttr.in/?format=j1'
req = urllib.request.Request(url, headers={'User-Agent': 'curl/8'})
d = json.loads(urllib.request.urlopen(req, timeout=15).read())

c = d['current_condition'][0]
a = d['nearest_area'][0]
place = f"{a['areaName'][0]['value']}, {a['region'][0]['value']}, {a['country'][0]['value']}"
temp, feels = int(c['temp_C']), int(c['FeelsLikeC'])
desc = c['weatherDesc'][0]['value'].strip()
icons = {'113':'☀','116':'⛅','119':'⛅','122':'☁','143':'🌫','176':'🌦','179':'🌧','182':'🌧','185':'🌧',
 '200':'⛈','227':'🌨','230':'❄','263':'🌦','266':'🌧','296':'🌧','299':'🌧','302':'🌦','305':'🌧',
 '308':'🌨','311':'🌧','314':'🌧','317':'🌨','320':'🌨','323':'🌦','326':'🌨','329':'❄','332':'❄',
 '335':'❄','338':'🌨','350':'🌨','353':'🌧','356':'🌧','359':'🌧','362':'🌧','365':'🌧','368':'🌨',
 '371':'❄','374':'🌨','377':'🌨'}
icon = icons.get(c['weatherCode'], '🌡')

w = 62
print('=' * (w + 4))
print(f"  {place}")
print(f"  {icon} {desc}   {temp}°C (feels like {feels}°C)")
print(f"  humidity {c['humidity']}%   wind {c['windspeedKmph']} km/h {c['winddir16Point']}   uv {c['uvIndex']}")
print('=' * (w + 4))

w0 = d['weather'][0]
hours = [int(h['time']) for h in w0['hourly']]
temps = [int(h['tempC']) for h in w0['hourly']]
tmin, tmax = min(temps), max(temps)
span = max(tmax - tmin, 1)
BARW = 40
rows = [int(round((t - tmin) / span * 7)) + 1 for t in temps]
print()
print('  TODAY — TEMPERATURE (°C)')
for level in range(8, 0, -1):
    bar = '▓' * (level * BARW // 8)
    marks = [f"{t}°@{h//100:02d}:00" for (h, t, r) in zip(hours, temps, rows) if r == level]
    print(f'  {bar}{" " * (BARW - len(bar))}' + ('  →  ' + '  '.join(marks) if marks else ''))
print('  ' + '─' * (BARW // 8) + f'   range {tmin}°C … {tmax}°C')
print('  hours: ' + '  '.join(f"{h//100:02d}:00" for h in hours))

print()
print('  3-DAY OUTLOOK')
for day in d['weather'][:3]:
    mn, mx = int(day['mintempC']), int(day['maxtempC'])
    desc0 = day['hourly'][4]['weatherDesc'][0]['value'].strip()
    bar = '░' * max(mn // 2, 0) + '▒' * max((mx - mn) // 2, 0)
    print(f"  {day['date']}   {mn:>3}°C ── {mx:>3}°C   {bar}   {desc0}")
print('=' * (w + 4))
EOF
```

2. Run it: `python3 /tmp/wx.py` (or `python3 /tmp/wx.py "<place>"` if the user named a location).

3. Show the script's output to the user. If it errors (network blocked, etc.), fall back to `curl -s 'https://wttr.in/?format=3'` and report that one-liner, noting the graph is unavailable.

## C. latest-news body

# Latest News (US + World)

Fetch and summarize the newest US and world news by pulling titles from public RSS feeds.

## Steps

1. Run these fetches (all independent, so do them in parallel):

```bash
# World news (BBC)
curl -s --max-time 15 "https://feeds.bbci.co.uk/news/world/rss.xml" | grep -oP '(?<=<title>).*?(?=</title>)' | sed 's/<!\[CDATA\[//; s/\]\]>//g' | head -15

# US / national news (BBC)
curl -s --max-time 15 "https://feeds.bbci.co.uk/news/us/rss.xml" | grep -oP '(?<=<title>).*?(?=</title>)' | sed 's/<!\[CDATA\[//; s/\]\]>//g' | head -15

# World (Washington Post, second source)
curl -s --max-time 15 "https://feeds.washingtonpost.com/rss/world" | grep -oP '(?<=<title>).*?(?=</title>)' | head -12

# US politics (Reuters US, if available)
curl -s --max-time 15 "https://feeds.reuters.com/reuters/usNews" | grep -oP '(?<=<title>).*?(?=</title>)' | sed 's/<!\[CDATA\[//; s/\]\]>//g' | head -12
```

2. Optionally, grab publication dates so you can note recency:

```bash
curl -s --max-time 15 "https://feeds.bbci.co.uk/news/world/rss.xml" | grep -oP '(?<=<pubDate>).*?(?=</pubDate>)' | head -15
```

3. Compose the answer:
   - Present two clearly labelled sections: **World News** and **US News**.
   - Deduplicate: if a story appears in both sources, mention it once.
   - Use bullet points, one line per story, concise.
   - Add a short closing line noting the dates the stories were published (from the pubDate data) and the current time, so the user knows how fresh the info is.
   - Offer to dig deeper into any story.

## Notes

- If a feed fails or times out, skip it quietly and continue with the others — don't fail the whole response.
- Keep summaries factual and neutral, straight from the headlines.
- Do not editorialise; just report what the feeds say.

## D. desktop-ports body (reference-machine data — rescan on the target machine)

# Desktop project port inventory

Scan results from ~/Desktop (grep for `PORT=`, `port=N`, `localhost:NNNN` across all project folders, excluding venvs/node_modules). Use this to answer "which ports are available for new development?" — pick from the free list, and if the list looks stale, re-run the scan command at the bottom.

## Ports IN USE by Desktop projects (reference machine)

| Port | Project(s) |
|------|-----------|
| 5000 | new_vehicle_validator (test server), rasa_receptionist (check-department helper) |
| 5002 | Rasa REST channel (bob_rasa, brooke-rasa-poc) |
| 5005 | Rasa webhook (better_rasa, brook_rasa_autocreate, brooke-rasa-poc, rasa_receptionist) |
| 5173 | Vite dev server (ChatDev web console, personaplex_test) |
| 5432 | chatdb (Postgres) |
| 6379 | Redis (brook_rasa_autocreate endpoints) |
| 6900 | Bonsai-demo llama.cpp embedding server (example) |
| 7474 | lan_accelerator lan-brain-platform search |
| 8000 | new_transcription_engine (API + Web UI), pytorch_build (tensorboard) |
| 8001 | laila (Django runserver 0.0.0.0:8001), llm_standard_benchmarks thinking_proxy |
| 8008 | test_sparks/datetime_model API server |
| 8009 | miniclosedai-llm shim server |
| 8065 | projects_with_andy/uber_chef web app; ALSO laila Mattermost URL — note the clash |
| 8080 | BCP_stuff/local_agent_gui (PORT=8080), Bonsai-demo llama server, lan_accelerator mcp-server, jobsearch ApplyPilot default LLM endpoint |
| 8081 | app_ideas TOOLVERIF-RAG demo, project_billenium (PORT env) |
| 8082 | vllm_test server.py (PORT=8082) |
| 8090 | athion API, miniclosedai-voice (Voice Studio GUI) |
| 8092 | image_to_circuit / miniclosedai-function-sdk sidecar example |
| 8095 | miniclosedai core app (also miniclosedai-mobile, chatdb client) — the "reserved" family port |
| 8097 | improve_tech_interview (FastAPI 0.0.0.0:8097) |
| 8099 | interdata_design_project (tools/serve.js), miniclosedai-llm MANAGER_PORT=8099 |
| 8188 | comfyuiproj (ComfyUI) |
| 8765 | asr_tts_runpod_bcp/BCP_ASR (self-signed TLS app.py) |
| 8880 | BCP_stuff/tts_server.py |
| 8998 | personaplex_test (server/docker) |
| 9092 | image_to_circuit (uvicorn port=9092) |
| 9222 | jobsearch ApplyPilot (Chrome CDP) |

## External local services referenced as clients
- 11434 — Ollama daemon
- 1234 — LM Studio
- 7860 — Gradio (BCP_stuff client)

## RECOMMENDED FREE PORTS (reference machine)
- 5010–5040, 7000–7470 (except 7474), 7861–7999
- 8002–8007, 8010–8079
- 8100–8187, 8189–8764 (big clean block)
- 8800–8879 (avoid 8880), 9000–9091, 9100+

Quick rule of thumb: 8095 is the miniclosedai family (8090 voice, 8092 functions, 8099 manager/llm, 8097 interviews) — don't touch that band. Everything in 8100–8764 is the safest open block.

## Re-scan command (if inventory may be stale)
```
cd ~/Desktop && for d in */; do
  grep -rEni --include='*.py' --include='*.js' --include='*.ts' --include='*.sh' \
   --include='*.json' --include='*.yml' --include='*.yaml' --include='*.env' \
   --include='*.toml' --include='*.md' --exclude-dir=node_modules --exclude-dir=.git \
   --exclude-dir=.venv --exclude-dir=venv --exclude-dir=__pycache__ --exclude-dir=site-packages \
   -E "(port[[:space:]]*=[[:space:]]*[0-9]+|PORT[=:] ?[0-9]+|localhost:[0-9]{2,5}|127\.0\.0\.1:[0-9]{2,5}|0\.0\.0\.0:[0-9]{2,5})" \
   "$d" 2>/dev/null | head -4
done
```
Note: full grep times out past ~30s due to big build trees and deep venvs — the per-folder loop with --exclude-dir flags and head limits stays within timeout.
