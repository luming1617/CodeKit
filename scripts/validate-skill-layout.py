#!/usr/bin/env python3
"""Validate CodeKit skill layout: SKILL.md plus a root-level openai.yaml manifest.

Also rejects dangling references in SKILL.md, i.e. scripts and artifacts the skill
does not actually own, which are usually copy-paste leftovers from another skill.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
INTERFACE_FIELDS = ("display_name", "short_description", "default_prompt")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
SCRIPT_REF = re.compile(r"scripts/([A-Za-z0-9_.-]+\.py)")
SHARED_SCRIPT_REF = re.compile(r"_shared/scripts/([A-Za-z0-9_.-]+\.py)")
SHARED_SCRIPT_PREFIX = "_shared/scripts/"
REFERENCE_REF = re.compile(r"(?:SKILL_DIR`?/)?references/([A-Za-z0-9_./-]+\.md)")
SHARED_REF = re.compile(r"_shared/prompts/([A-Za-z0-9_.-]+\.md)")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
EXTERNAL_SCHEME = re.compile(r"^(?:https?|mailto|file|ftp):", re.IGNORECASE)
AGENT_KEYS = ("name", "description", "mode", "tools")
# Artifacts only a single skill writes; any other skill mentioning them is referencing
# a file that never appears in its own run.
OWNED_ARTIFACTS = {"run-manifest.json": "initialize"}
# On Windows `python3` is the Microsoft Store alias stub: `which` finds it, but running
# it prints nothing and exits non-zero, so a skill that hardcodes it reads as "Python is
# missing" and blocks the phase. Skills must use the <PYTHON> placeholder instead.
PYTHON3_COMMAND = re.compile(r"(?:^|\s)(?:\$ )?python3(?=[ \t]+[\"'<$])")
# A role's standing requirements must live in exactly one place: ``agents/<role>.md`` is the
# source the host injects at dispatch, and ``references/*-prompt.md`` only adds that step's
# inputs, constraints and output anchors. Two identical sentences in both files is a copy,
# not a coincidence -- the pair then drifts apart in silence (review-specialist and its
# prompt restated the same six rules with zero identical lines). One shared sentence is
# tolerated as boilerplate, so the limit is per (agent, prompt) pair.
ROLE_LINE_MIN = 20
ROLE_DUP_LIMIT = 2
ROLE_MARKUP = re.compile(r"^[\s#>*\-.0-9、]+")
ROLE_SKIP_PREFIX = ("<", "|", "---", "```")


def frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise ValueError(f"missing frontmatter: {path}")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"unterminated frontmatter: {path}") from exc

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("'\"")
    return fields


def interface_fields(path: Path) -> dict[str, str]:
    """Parse the flat ``interface`` mapping that carries the Codex UI metadata."""
    fields: dict[str, str] = {}
    section = ""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            raise ValueError(f"line {number}: expected 'key: value'")
        indent = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in indent:
            raise ValueError(f"line {number}: indentation must use spaces")
        key, value = raw.split(":", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if indent:
            if section == "interface":
                fields[key] = value
            continue
        if value:
            raise ValueError(f"line {number}: top-level entries must be mappings")
        section = key
    return fields


def check_skill(skill: Path, problems: list[str]) -> None:
    name = skill.name
    try:
        declared = frontmatter(skill / "SKILL.md").get("name", "")
    except (OSError, ValueError) as exc:
        problems.append(str(exc))
        return
    if declared != name:
        problems.append(f"{name}: SKILL.md name {declared!r} must match the directory name")

    if (skill / "agents" / "openai.yaml").exists():
        problems.append(
            f"{name}: openai.yaml must sit next to SKILL.md; "
            "skills/<name>/agents/openai.yaml is generated at install time"
        )

    manifest = skill / "openai.yaml"
    if not manifest.is_file():
        problems.append(f"{name}: missing skills/{name}/openai.yaml")
        return
    try:
        fields = interface_fields(manifest)
    except (OSError, ValueError) as exc:
        problems.append(f"skills/{name}/openai.yaml: {exc}")
        return

    for field in INTERFACE_FIELDS:
        value = fields.get(field, "")
        if not value:
            problems.append(f"skills/{name}/openai.yaml: interface.{field} is required")
            continue
        if not CJK.search(value):
            problems.append(
                f"skills/{name}/openai.yaml: interface.{field} must be written in Chinese"
            )

    prompt = fields.get("default_prompt", "")
    if prompt and f"${name}" not in prompt:
        problems.append(f"skills/{name}/openai.yaml: default_prompt must mention ${name}")


def check_dangling_references(skill: Path, problems: list[str]) -> None:
    """Reject SKILL.md paths the skill cannot resolve at runtime."""
    try:
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"{skill.name}: cannot read SKILL.md: {exc}")
        return

    for script in sorted(set(SCRIPT_REF.findall(text.replace(SHARED_SCRIPT_PREFIX, "#")))):
        if not (skill / "scripts" / script).is_file():
            problems.append(
                f"{skill.name}: SKILL.md calls scripts/{script}, which this skill does not ship"
            )

    for script in sorted(set(SHARED_SCRIPT_REF.findall(text))):
        if not (skill.parent / "_shared" / "scripts" / script).is_file():
            problems.append(
                f"{skill.name}: SKILL.md calls _shared/scripts/{script}, which the "
                "skills tree does not ship"
            )

    for artifact, owner in sorted(OWNED_ARTIFACTS.items()):
        if artifact in text and skill.name != owner:
            problems.append(
                f"{skill.name}: SKILL.md references {artifact}, which only the "
                f"{owner} skill writes; resolve the run context from this skill's own inputs"
            )


def _relative_links(text: str) -> list[str]:
    links = []
    for target in MARKDOWN_LINK.findall(text):
        if EXTERNAL_SCHEME.match(target) or target.startswith("#") or target.startswith("/"):
            continue
        links.append(target.split("#", 1)[0])
    return [link for link in links if link]


def check_assets(skill: Path, problems: list[str]) -> None:
    """Reject resource references that resolve to nothing, and cross-skill escapes."""
    sources = [skill / "SKILL.md", *sorted((skill / "references").rglob("*.md"))]
    for source in sources:
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        where = source.relative_to(skill.parent).as_posix()
        if PYTHON3_COMMAND.search(text):
            problems.append(
                f"{where}: hardcodes the python3 launcher in a command; call scripts via the "
                "<PYTHON> placeholder resolved per host "
                "(see skills/_shared/prompts/path-convention.md)"
            )
        for name in sorted(set(REFERENCE_REF.findall(text))):
            if not (skill / "references" / name).is_file():
                problems.append(f"{where}: references/{name} does not exist")
        for name in sorted(set(SHARED_REF.findall(text))):
            if not (SKILLS_DIR / "_shared" / "prompts" / name).is_file():
                problems.append(f"{where}: _shared/prompts/{name} does not exist")
        for link in sorted(set(_relative_links(text))):
            target = (source.parent / link).resolve()
            try:
                target.relative_to(skill.resolve())
            except ValueError:
                problems.append(
                    f"{where}: link '{link}' escapes the skill directory; hand off "
                    "across skills only via fixed filenames inside <feature-directory>"
                )
                continue
            if not target.exists():
                problems.append(f"{where}: link '{link}' does not resolve to a file")


def check_python_launcher(skill: Path, problems: list[str]) -> None:
    """A skill that ships gate scripts must resolve <PYTHON>, not assume a host."""
    if not any((skill / "scripts").glob("*.py")):
        return
    try:
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return
    if "<PYTHON>" not in text:
        problems.append(
            f"{skill.name}: SKILL.md must call its scripts through the <PYTHON> placeholder"
        )
    if "path-convention.md" not in text:
        problems.append(
            f"{skill.name}: SKILL.md must reference _shared/prompts/path-convention.md, "
            "the single source that resolves <PYTHON> per host"
        )


def check_agents(skills: list[Path], problems: list[str]) -> None:
    """Global agent names must be unique: they install as ``~/<tool>/agents/<name>``."""
    seen: dict[str, Path] = {}
    for skill in skills:
        for agent in sorted((skill / "agents").glob("*.md")):
            try:
                metadata = frontmatter(agent)
            except (OSError, ValueError) as exc:
                problems.append(f"{agent.relative_to(skill.parent)}: {exc}")
                continue
            missing = [key for key in AGENT_KEYS if not metadata.get(key)]
            if missing:
                problems.append(
                    f"{agent.relative_to(skill.parent)}: frontmatter missing {', '.join(missing)}"
                )
            name = metadata.get("name", "")
            if not name:
                continue
            if name in seen:
                problems.append(
                    f"duplicate global agent name '{name}': {seen[name]} and "
                    f"{agent.relative_to(skill.parent)}; one would silently overwrite the other"
                )
            else:
                seen[name] = agent.relative_to(skill.parent)


def role_body_lines(path: Path) -> set[str]:
    """Content sentences of a role file: frontmatter, slots, tables and fences excluded."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines = lines[index + 1 :]
                break
    result: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith(ROLE_SKIP_PREFIX):
            continue
        sentence = ROLE_MARKUP.sub("", stripped).strip()
        if len(sentence) >= ROLE_LINE_MIN:
            result.add(sentence)
    return result


def check_role_body_dupes(skill: Path, problems: list[str]) -> None:
    agents = sorted((skill / "agents").glob("*.md"))
    prompts = sorted((skill / "references").glob("*-prompt.md"))
    if not agents or not prompts:
        return
    for agent in agents:
        body = role_body_lines(agent)
        if not body:
            continue
        for prompt in prompts:
            shared = sorted(body & role_body_lines(prompt))
            if len(shared) >= ROLE_DUP_LIMIT:
                preview = "; ".join(line[:32] for line in shared[:2])
                problems.append(
                    f"{skill.name}: {agent.name} and {prompt.name} restate the same role body in "
                    f"two places ({len(shared)} identical sentences, e.g. {preview}); keep the "
                    "requirements in agents/ and leave only inputs, step-specific constraints and "
                    "the output anchors in the prompt template"
                )


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"skills directory not found: {SKILLS_DIR}", file=sys.stderr)
        return 1
    skills = sorted(
        path for path in SKILLS_DIR.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    )
    if not skills:
        print(f"no skills found in {SKILLS_DIR}", file=sys.stderr)
        return 1

    problems: list[str] = []
    for skill in skills:
        check_skill(skill, problems)
        check_dangling_references(skill, problems)
        check_assets(skill, problems)
        check_python_launcher(skill, problems)
        check_role_body_dupes(skill, problems)
    check_agents(skills, problems)
    if problems:
        for problem in problems:
            print(f"skill layout: {problem}", file=sys.stderr)
        return 1

    agents = sum(len(list((skill / "agents").glob("*.md"))) for skill in skills)
    print(f"validated {len(skills)} CodeKit skills ({agents} agents, SKILL.md + openai.yaml + asset links)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
