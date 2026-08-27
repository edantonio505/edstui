"""
Skill registry for eds tui.

A skill is a directory under ~/.eds_tui/skills holding a SKILL.md: a flat
'key: value' frontmatter block between --- fences, then a Markdown body. The
description is all the model sees until the skill is actually loaded; the body
is the procedure it follows once it is.

Nothing in here may raise. A missing directory, an unreadable file or a
malformed SKILL.md degrades to a smaller registry, never a traceback -- ask has
to keep working for a user with no skills, or with one half-written one.

Skills are read from the user's home directory only, never from the working
directory. A skill is instructions that an agent with unconfirmed shell access
will follow, so picking them up from whatever repo happens to be cd'd into
would be a prompt-injection path straight into run_command.
"""
import os
import re

SKILLS_DIR = os.path.expanduser("~/.eds_tui/skills")
VALID_MODELS = ("main", "small", "any")

# A skill name becomes a directory name, and may arrive from a language model, so
# it is validated before it is ever joined onto a path -- '../../.bashrc' must
# never be a write target.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TEMPLATE = """---
name: {name}
description: One line saying when ask should reach for this. This is all the model sees until the skill loads, so make it specific.
model: any
---

Write the procedure here, as if briefing someone who has your shell but none of
your context.

1. First step.
2. Second step.
"""

_cache = None
_problems = []


def parse_frontmatter(text):
    """Split flat 'key: value' frontmatter from the body. Nested YAML is not supported."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip()

    return {}, text     # unterminated fence: no usable frontmatter


def _load_one(directory):
    with open(os.path.join(directory, "SKILL.md")) as f:
        meta, body = parse_frontmatter(f.read())

    description = meta.get("description", "")
    if not description:
        raise ValueError("no 'description' in the frontmatter")
    if not body.strip():
        raise ValueError("nothing below the frontmatter")

    model = meta.get("model", "any").lower()
    if model not in VALID_MODELS:
        raise ValueError(f"model must be one of {', '.join(VALID_MODELS)}, got '{model}'")

    return {
        "name": meta.get("name") or os.path.basename(directory),
        "description": description,
        "model": model,
        "body": body,
        "dir": directory,
    }


def discover():
    """Every well-formed skill, keyed by name. Cached -- ask is a one-shot process."""
    global _cache
    if _cache is not None:
        return _cache

    _cache = {}
    del _problems[:]

    try:
        entries = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return _cache       # no skills directory at all is the normal case, not an error

    for entry in entries:
        directory = os.path.join(SKILLS_DIR, entry)
        if not os.path.isfile(os.path.join(directory, "SKILL.md")):
            continue
        try:
            skill = _load_one(directory)
        except Exception as e:
            _problems.append((entry, str(e)))
            continue
        _cache[skill["name"]] = skill

    return _cache


def problems():
    """(directory, reason) for every SKILL.md that failed to parse. Surfaced by --skills."""
    discover()
    return list(_problems)


def get(name):
    """Look up a skill by name, case-insensitively. None if there is no such skill."""
    found = discover()
    if name in found:
        return found[name]
    lowered = (name or "").lower()
    for key, skill in found.items():
        if key.lower() == lowered:
            return skill
    return None


def index_lines(subset=None):
    """The name+description index -- everything the model knows until a skill loads."""
    found = discover() if subset is None else subset
    return "\n".join(f"- {s['name']}: {s['description']}" for s in found.values())


def match(text, subset=None):
    """
    The first known skill name appearing in text. Used to read a skill out of the
    triage model's reply, which is often loosely formatted. Longest name first so
    'deploy' cannot shadow 'deploy-flow'.
    """
    found = discover() if subset is None else subset
    lowered = (text or "").lower()
    for name in sorted(found, key=len, reverse=True):
        if name.lower() in lowered:
            return found[name]
    return None


def render(skill):
    """A skill as the model receives it, with its directory named so bundled files resolve."""
    return (
        f"--- skill: {skill['name']} ---\n"
        f"{skill['description']}\n"
        f"Files bundled with this skill live in {skill['dir']} — reference them by full path.\n\n"
        f"{skill['body']}\n"
        f"--- end skill: {skill['name']} ---"
    )


def write(name, description, body, model="any", overwrite=False):
    """
    Create or replace a skill on disk, then re-read it to prove it registers.

    Every argument can come from a model, so all of them are validated here rather
    than trusted. Raises with a message written to be handed straight back to the
    model as a tool result, so it can correct itself and retry.
    """
    name = (name or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "name must start with a letter or digit and contain only lowercase "
            f"letters, digits, dots, dashes or underscores — got '{name}'"
        )

    # Frontmatter is one key per line, so a multi-line description would truncate
    # the skill's only searchable field and take the rest of it into the void.
    description = " ".join((description or "").split())
    if not description:
        raise ValueError("description is required — it is the text skill matching searches")

    model = (model or "any").strip().lower()
    if model not in VALID_MODELS:
        raise ValueError(f"model must be one of {', '.join(VALID_MODELS)} — got '{model}'")

    body = (body or "").strip()
    if not body:
        raise ValueError("body is required — it is the procedure the model follows")

    directory = os.path.join(SKILLS_DIR, name)
    path = os.path.join(directory, "SKILL.md")
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(
            f"a skill named '{name}' already exists — pass overwrite to replace it"
        )

    os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        f.write(f"---\nname: {name}\ndescription: {description}\n"
                f"model: {model}\n---\n\n{body}\n")

    # Round-trip through the real parser: writing a file that does not register
    # is a silent failure, and the model needs to hear about it now.
    reset_cache()
    written = get(name)
    if not written:
        raise ValueError(f"wrote {path} but it did not parse back — inspect it by hand")

    return written


def reset_cache():
    """Drop the cache. Only --test needs this, when it repoints SKILLS_DIR at a fixture."""
    global _cache
    _cache = None
