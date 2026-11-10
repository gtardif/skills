#!/usr/bin/env python3
"""Run skill evaluations using LLM-as-judge.

This script reads evaluation prompts from skill eval.md files and runs each
prompt through Claude Code or Codex, using an LLM judge to grade the full
transcript against the expected behaviors and must-not criteria.

Eval files are discovered from:
  1. skills/<skill>/evals/<skill>.md (preferred location)
  2. evals/<skill>.md (legacy location)

The LLM judge approach replaces complex CLI step matching - the judge
can evaluate both knowledge-based responses AND whether correct commands
were used, all from the transcript.

Usage:
  python3 scripts/eval_judge.py                           # all evals with claude
  python3 scripts/eval_judge.py --agent codex             # all evals with codex
  python3 scripts/eval_judge.py --skill docker-sandboxes-lifecycle
  python3 scripts/eval_judge.py --skill docker-sandboxes-lifecycle --prompt 1
  python3 scripts/eval_judge.py --list                    # show available evals
  python3 scripts/eval_judge.py --regrade .eval-judge/<run>/...  # re-grade offline
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_ROOT = REPO_ROOT / "evals"
SKILLS_ROOT = REPO_ROOT / "skills"
DEFAULT_OUT = REPO_ROOT / ".eval-judge"

DEFAULT_LIMITS = {"max_turns": 25, "max_budget_usd": 0.50, "timeout_seconds": 300}


class EvalError(Exception):
    pass


# ─── eval parsing ────────────────────────────────────────────────────────────

@dataclasses.dataclass
class EvalPrompt:
    """A single evaluation prompt from a markdown eval file."""
    eval_file: Path
    skill: str
    prompt_num: int
    title: str
    prompt: str
    expected_behaviors: list[str]
    must_not: list[str]

    @property
    def id(self) -> str:
        return f"{self.skill}/prompt-{self.prompt_num}"


def _parse_checkbox_section(section: str, heading: str) -> list[str]:
    """Parse checkbox items from a ### section, handling multi-line criteria."""
    pattern = rf'### {re.escape(heading)}\s*\n((?:- \[[ x]\] .+(?:\n(?:[ \t]+[^\n]+)?)*)+)'
    match = re.search(pattern, section)
    if not match:
        return []
    
    items = []
    current = None
    for line in match.group(1).split('\n'):
        m = re.match(r'- \[[ x]\] (.+)', line)
        if m:
            if current:
                items.append(current.strip())
            current = m.group(1)
        elif current and line.strip():
            current += ' ' + line.strip()
    if current:
        items.append(current.strip())
    return items


def parse_eval_markdown(path: Path) -> list[EvalPrompt]:
    """Parse a markdown eval file and extract all prompts."""
    content = path.read_text(encoding="utf-8")
    
    # Extract skill from "# Eval: skill-name"
    skill_match = re.search(r'^# Eval: (.+)$', content, re.MULTILINE)
    if not skill_match:
        raise EvalError(f"{path}: missing '# Eval: <skill-name>' header")
    skill = skill_match.group(1).strip()
    
    prompts = []
    prompt_pattern = re.compile(r'^## Prompt (\d+): (.+?)$', re.MULTILINE)
    matches = list(prompt_pattern.finditer(content))
    
    for i, match in enumerate(matches):
        num = int(match.group(1))
        title = match.group(2).strip()
        
        # Section content until next prompt
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[start:end]
        
        # Extract quoted prompt text
        prompt_match = re.search(r'\*\*Prompt to agent:\*\*\s*\n((?:>\s*.+\n?)+)', section)
        if not prompt_match:
            continue
        prompt_text = '\n'.join(
            line.lstrip('>').strip() for line in prompt_match.group(1).strip().split('\n')
        ).strip()
        
        expected = _parse_checkbox_section(section, 'Expected behaviors')
        must_not = _parse_checkbox_section(section, 'Must not')
        
        if not expected and not must_not:
            continue
        
        prompts.append(EvalPrompt(path, skill, num, title, prompt_text, expected, must_not))
    
    return prompts


def discover_evals(skill_name: str | None, prompt_num: int | None) -> list[EvalPrompt]:
    """Find all eval prompts, optionally filtered by skill name.
    
    Looks for eval files in two places:
      1. skills/<skill>/evals/<skill>.md (preferred)
      2. evals/<skill>.md (legacy)
    """
    prompts = []
    seen_skills = set()
    
    # First, check skills/<skill>/evals/<skill>.md (preferred location)
    for skill_dir in sorted(SKILLS_ROOT.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        if skill_name and skill_dir.name != skill_name:
            continue
        
        eval_file = skill_dir / "evals" / f"{skill_dir.name}.md"
        if eval_file.exists():
            try:
                prompts.extend(parse_eval_markdown(eval_file))
                seen_skills.add(skill_dir.name)
            except EvalError as e:
                print(f"Warning: {e}", file=sys.stderr)
    
    # Then, check evals/<skill>.md (legacy location)
    for md in sorted(EVALS_ROOT.glob("*.md")):
        if md.name == "README.md":
            continue
        # Skip if we already found this skill in the skills directory
        if md.stem in seen_skills:
            continue
        if skill_name and md.stem != skill_name:
            continue
        try:
            prompts.extend(parse_eval_markdown(md))
        except EvalError as e:
            print(f"Warning: {e}", file=sys.stderr)
    
    if prompt_num is not None:
        prompts = [p for p in prompts if p.prompt_num == prompt_num]
    return prompts


# ─── LLM judge ───────────────────────────────────────────────────────────────

@dataclasses.dataclass
class JudgeResult:
    criterion: str
    passed: bool
    reasoning: str
    is_must_not: bool = False


def call_judge(prompt: str, model: str) -> dict[str, Any]:
    """Call LLM judge via Anthropic API."""
    import urllib.request
    import urllib.error
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API error {e.code}: {e.read().decode() if e.fp else ''}") from e
    
    text = result.get("content", [{}])[0].get("text", "")
    # Extract JSON from response
    json_match = re.search(r'```json\s*\n?(.+?)\n?```', text, re.DOTALL)
    json_str = json_match.group(1) if json_match else text
    
    # Try to parse JSON, with fallback for common issues
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # Try to fix common issues: unescaped quotes in strings
        # This is a basic attempt; complex cases may still fail
        import re as re_inner
        # Try to extract just the evaluations array if the outer object is malformed
        eval_match = re_inner.search(r'"evaluations"\s*:\s*(\[.+\])', json_str, re.DOTALL)
        if eval_match:
            try:
                evals = json.loads(eval_match.group(1))
                return {"evaluations": evals}
            except json.JSONDecodeError:
                pass
        # Re-raise original error
        raise


def judge_transcript(
    transcript: str,
    expected: list[str],
    must_not: list[str],
    model: str,
    workspace_files: dict[str, str] | None = None,
) -> list[JudgeResult]:
    """Have LLM judge evaluate the full transcript against criteria."""
    
    expected_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(expected))
    must_not_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(must_not))
    
    # Build workspace files section if we have any
    files_section = ""
    if workspace_files:
        files_parts = []
        for path, content in sorted(workspace_files.items()):
            files_parts.append(f"### {path}\n```\n{content}\n```")
        files_section = f"""\n\n## Files Created in Workspace\n\nThe following files were found in the workspace after the agent finished:\n\n{chr(10).join(files_parts)}"""
    
    prompt = f"""You are evaluating an AI agent's response about Docker Sandboxes (sbx CLI).
The transcript below shows everything the agent said and did (including any tool calls).

## Full Transcript

{transcript}{files_section}

## Criteria

EXPECTED BEHAVIORS (must satisfy all):
{expected_text}

MUST NOT (must not violate any):
{must_not_text}

## Task

For each criterion, output a JSON evaluation.

For EXPECTED behaviors: set "passed": true if the agent satisfied the criterion.
For MUST NOT items: set "passed": true if the agent AVOIDED the violation (did NOT do the forbidden thing).

Consider the FULL transcript AND any files shown above - the agent may have created files
without showing them in the transcript. If a criterion asks about file contents (e.g.,
"Dockerfile uses multi-stage build"), check the actual file content in the "Files Created"
section, not just what the transcript shows.

Return JSON:
{{
  "evaluations": [
    {{"criterion_number": 1, "criterion_type": "expected", "passed": true/false, "reasoning": "brief why"}},
    ...
  ]
}}

Be strict but fair. Order: all expected behaviors first, then all must_not items.

IMPORTANT: For must_not items, "passed": true means the agent correctly AVOIDED the bad behavior."""

    try:
        result = call_judge(prompt, model)
    except Exception as e:
        # Return all failed if judge errors
        return [JudgeResult(c, False, f"Judge error: {e}") for c in expected] + \
               [JudgeResult(c, False, f"Judge error: {e}", True) for c in must_not]
    
    results = []
    for ev in result.get("evaluations", []):
        is_must_not = ev.get("criterion_type") == "must_not"
        idx = ev.get("criterion_number", 1) - 1
        criteria_list = must_not if is_must_not else expected
        if 0 <= idx < len(criteria_list):
            results.append(JudgeResult(
                criteria_list[idx],
                ev.get("passed", False),
                ev.get("reasoning", ""),
                is_must_not,
            ))
    return results


# ─── agent runner ────────────────────────────────────────────────────────────

def prepare_workspace(eval_prompt: EvalPrompt, agent: str) -> Path:
    """Create workspace with skills installed."""
    root = Path(tempfile.mkdtemp(prefix=f"eval-{eval_prompt.prompt_num}-"))
    workspace = root / "workspace"
    workspace.mkdir()
    
    # Create a minimal README to indicate this is a project workspace
    # This helps agents understand they should create files here
    readme = workspace / "README.md"
    readme.write_text("# Project Workspace\n\nThis is an empty project workspace. Create the requested files here.\n")
    
    # Claude uses .claude/skills/, Codex uses .agents/skills/
    if agent == "codex":
        skills_dir = workspace / ".agents" / "skills"
    else:
        skills_dir = workspace / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    for skill in SKILLS_ROOT.iterdir():
        if skill.is_dir() and not skill.name.startswith("."):
            shutil.copytree(skill, skills_dir / skill.name)
    
    return root


# File extensions to collect from workspace for judge context
WORKSPACE_FILE_PATTERNS = [
    "Dockerfile", "Dockerfile.*", "*.dockerfile",
    "compose.yaml", "compose.yml", "compose.*.yaml", "compose.*.yml",
    "compose.override.yaml", "compose.override.yml",
    "docker-compose.yaml", "docker-compose.yml",
    ".dockerignore",
    "agent.yaml", "docker-agent.yaml", "agents.yaml",
    "sbxenv.yaml", ".sbxenv.yaml",
    "*.md",  # READMEs
    ".env.example", ".env.sample",
    "requirements.txt", "package.json",  # Dependency files for context
]


def collect_workspace_files(workspace: Path, max_files: int = 20, max_size: int = 50000) -> dict[str, str]:
    """Collect relevant files from workspace for judge context.
    
    Returns a dict of {relative_path: content} for files matching patterns.
    Excludes the .claude/.agents skills directories.
    """
    import fnmatch
    
    files = {}
    total_size = 0
    
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        
        # Skip skills directories
        rel = path.relative_to(workspace)
        rel_str = str(rel)
        if rel_str.startswith(".claude/") or rel_str.startswith(".agents/"):
            continue
        
        # Check if file matches any pattern
        name = path.name
        if not any(fnmatch.fnmatch(name, pat) for pat in WORKSPACE_FILE_PATTERNS):
            continue
        
        # Read file content
        try:
            content = path.read_text(errors="replace")
        except Exception:
            continue
        
        # Size limits
        if len(content) > 10000:
            content = content[:10000] + "\n... (truncated)"
        
        total_size += len(content)
        if total_size > max_size or len(files) >= max_files:
            break
        
        files[rel_str] = content
    
    return files


@dataclasses.dataclass
class AgentRunResult:
    """Result of running an agent."""
    transcript_text: str
    workspace_files: dict[str, str]
    workspace_path: Path  # For cleanup


def run_agent(eval_prompt: EvalPrompt, args: argparse.Namespace, run_dir: Path) -> AgentRunResult:
    """Run the agent and return transcript + workspace files."""
    run_dir.mkdir(parents=True, exist_ok=True)
    root = prepare_workspace(eval_prompt, args.agent)
    workspace = root / "workspace"
    
    # Build command based on agent type
    if args.agent == "codex":
        cmd = [
            args.agent_bin, "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            eval_prompt.prompt,
        ]
        if args.model:
            cmd += ["--model", args.model]
    else:  # claude
        cmd = [
            args.agent_bin, "-p", eval_prompt.prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "dontAsk",
            "--allowedTools", "Skill", "Read", "Glob", "Grep", "Bash", "Write", "Edit",
            "--disallowedTools", "WebFetch", "WebSearch", "Task",
            "--max-turns", str(DEFAULT_LIMITS["max_turns"]),
            "--max-budget-usd", str(DEFAULT_LIMITS["max_budget_usd"]),
        ]
        if args.model:
            cmd += ["--model", args.model]
    
    # Save metadata
    meta = {
        "eval_file": str(eval_prompt.eval_file.relative_to(REPO_ROOT)),
        "skill": eval_prompt.skill,
        "prompt_num": eval_prompt.prompt_num,
        "title": eval_prompt.title,
        "prompt": eval_prompt.prompt,
        "expected_behaviors": eval_prompt.expected_behaviors,
        "must_not": eval_prompt.must_not,
        "agent": args.agent,
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
    
    transcript_path = run_dir / "transcript.jsonl"
    env = {k: v for k, v in os.environ.items() if v}
    
    try:
        with open(transcript_path, "w") as out, open(run_dir / "stderr.txt", "w") as err:
            subprocess.run(cmd, cwd=workspace, env=env,
                          stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                          timeout=DEFAULT_LIMITS["timeout_seconds"], check=False)
    except subprocess.TimeoutExpired:
        pass
    
    # Collect workspace files BEFORE cleanup
    workspace_files = collect_workspace_files(workspace)
    
    # Convert transcript to readable text for the judge
    if args.agent == "codex":
        transcript_text = codex_transcript_to_text(transcript_path)
    else:
        transcript_text = transcript_to_text(transcript_path)
    
    return AgentRunResult(
        transcript_text=transcript_text,
        workspace_files=workspace_files,
        workspace_path=root,
    )


def transcript_to_text(path: Path) -> str:
    """Convert stream-json transcript to readable text for judging."""
    lines = []
    for raw in path.read_text().splitlines():
        if not raw.strip().startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        
        kind = ev.get("type")
        if kind == "assistant":
            content = (ev.get("message") or {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text":
                    lines.append(f"ASSISTANT: {block.get('text', '')}")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    inp = json.dumps(block.get("input", {}), indent=2)
                    lines.append(f"TOOL CALL [{name}]: {inp}")
        elif kind == "user":
            content = (ev.get("message") or {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, list):
                        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
                    lines.append(f"TOOL RESULT: {text[:1000]}{'...' if len(str(text)) > 1000 else ''}")
    
    return "\n\n".join(lines)


def codex_transcript_to_text(path: Path) -> str:
    """Convert Codex JSON transcript to readable text for judging."""
    lines = []
    for raw in path.read_text().splitlines():
        if not raw.strip().startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            item_type = item.get("type")
            
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    lines.append(f"ASSISTANT: {text}")
            elif item_type == "command_execution":
                command = item.get("command", "")
                output = item.get("aggregated_output", "")
                # Extract actual command from shell wrapper
                actual_command = command
                shell_match = re.match(r'^/bin/(?:zsh|bash|sh)\s+-\w*c\s+[\'"]?(.+?)[\'"]?$', command, re.DOTALL)
                if shell_match:
                    actual_command = shell_match.group(1).strip()
                    if actual_command.endswith("'") or actual_command.endswith('"'):
                        actual_command = actual_command[:-1]
                lines.append(f'TOOL CALL [Bash]: {{\n  "command": "{actual_command}"\n}}')
                lines.append(f"TOOL RESULT: {output[:1000]}{'...' if len(str(output)) > 1000 else ''}")
            elif item_type == "skill_read":
                skill_path = item.get("skill_path", "")
                # Extract skill name from path
                parts = skill_path.split("/")
                skill_name = ""
                if "skills" in parts:
                    idx = parts.index("skills")
                    if idx + 1 < len(parts):
                        skill_name = parts[idx + 1]
                if skill_name:
                    lines.append(f'TOOL CALL [Skill]: {{\n  "skill": "{skill_name}"\n}}')
                    lines.append(f"TOOL RESULT: Launching skill: {skill_name}")
    
    return "\n\n".join(lines)


# ─── grading ─────────────────────────────────────────────────────────────────

def grade(eval_prompt: EvalPrompt, transcript: str, args: argparse.Namespace,
          workspace_files: dict[str, str] | None = None) -> list[JudgeResult]:
    """Grade transcript using LLM judge."""
    return judge_transcript(
        transcript,
        eval_prompt.expected_behaviors,
        eval_prompt.must_not,
        args.judge_model,
        workspace_files,
    )


def run_and_grade(eval_prompt: EvalPrompt, args: argparse.Namespace, run_dir: Path) -> list[JudgeResult]:
    """Run agent and grade the result."""
    result = run_agent(eval_prompt, args, run_dir)
    (run_dir / "transcript.txt").write_text(result.transcript_text)
    
    # Save workspace files for debugging
    if result.workspace_files:
        files_dir = run_dir / "workspace_files"
        files_dir.mkdir(exist_ok=True)
        for rel_path, content in result.workspace_files.items():
            file_path = files_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
    
    # Grade with workspace files
    results = grade(eval_prompt, result.transcript_text, args, result.workspace_files)
    
    # Cleanup workspace now that we're done
    if not args.keep:
        shutil.rmtree(result.workspace_path, ignore_errors=True)
    
    # Save report
    meta = json.loads((run_dir / "run.json").read_text())
    report = {
        **meta,
        "passed": all(r.passed for r in results),
        "workspace_files": list(result.workspace_files.keys()) if result.workspace_files else [],
        "results": [dataclasses.asdict(r) for r in results],
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    
    return results


def regrade(run_dir: Path, args: argparse.Namespace) -> list[JudgeResult]:
    """Re-grade a saved run offline."""
    meta = json.loads((run_dir / "run.json").read_text())
    transcript = (run_dir / "transcript.txt").read_text()
    
    eval_prompt = EvalPrompt(
        REPO_ROOT / meta["eval_file"], meta["skill"], meta["prompt_num"],
        meta["title"], meta["prompt"], meta["expected_behaviors"], meta["must_not"],
    )
    
    results = grade(eval_prompt, transcript, args)
    
    report = {**meta, "passed": all(r.passed for r in results),
              "results": [dataclasses.asdict(r) for r in results]}
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────

def print_results(label: str, results: list[JudgeResult], run_dir: Path, duration_secs: float | None = None) -> None:
    passed = all(r.passed for r in results)
    time_str = f" ({duration_secs:.1f}s)" if duration_secs is not None else ""
    print(f"\n{'PASS' if passed else 'FAIL'}: {label}{time_str}")
    for r in results:
        mark = "✓" if r.passed else "✗"
        prefix = "must-not" if r.is_must_not else "expected"
        short = r.criterion[:70] + ("..." if len(r.criterion) > 70 else "")
        print(f"  {mark} [{prefix}] {short}")
        if not r.passed and r.reasoning:
            print(f"      → {r.reasoning}")
    print(f"  artifacts: {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skill", help="Run only this skill's eval")
    parser.add_argument("--prompt", type=int, help="Run only this prompt number")
    parser.add_argument("--agent", choices=["claude", "codex"], default="claude", help="Agent to test")
    parser.add_argument("--model", help="Model for agent (default varies by agent)")
    parser.add_argument("--judge-model", default="claude-sonnet-4-5-20250929", help="Model for judge")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per prompt")
    parser.add_argument("--pass-threshold", type=float, default=1.0,
                        help="Minimum pass rate (0.0-1.0) to exit successfully (default: 1.0 = all must pass)")
    parser.add_argument("--agent-bin", help="Agent CLI binary (default: 'claude' or 'codex')")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--keep", action="store_true", help="Keep workspaces")
    parser.add_argument("--regrade", type=Path, help="Re-grade saved run")
    parser.add_argument("--list", action="store_true", help="List evals")
    args = parser.parse_args(argv)
    
    # Set defaults based on agent
    if args.agent_bin is None:
        args.agent_bin = "codex" if args.agent == "codex" else "claude"
    if args.model is None:
        args.model = "" if args.agent == "codex" else "sonnet"
    
    if args.list:
        for p in discover_evals(args.skill, args.prompt):
            print(f"{p.id}: {p.title} ({len(p.expected_behaviors)} expected, {len(p.must_not)} must-not)")
        return 0
    
    if args.regrade:
        results = regrade(args.regrade, args)
        print_results(str(args.regrade), results, args.regrade)
        return 0 if all(r.passed for r in results) else 1
    
    prompts = discover_evals(args.skill, args.prompt)
    if not prompts:
        print("No evals found", file=sys.stderr)
        return 2
    
    if not shutil.which(args.agent_bin):
        print(f"{args.agent_bin} not found", file=sys.stderr)
        return 2
    
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2
    
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    all_results = []  # (label, results, duration_secs)
    skill_times: dict[str, list[float]] = {}  # skill -> list of durations
    
    for ep in prompts:
        for attempt in range(1, args.repeat + 1):
            label = f"{ep.id}" + (f" #{attempt}" if args.repeat > 1 else "")
            run_dir = args.out / stamp / ep.skill / f"prompt-{ep.prompt_num}" / str(attempt)
            print(f"Running {label}...", flush=True)
            
            start_time = time.time()
            results = run_and_grade(ep, args, run_dir)
            duration = time.time() - start_time
            
            print_results(label, results, run_dir, duration)
            all_results.append((label, results, duration))
            
            # Track per-skill timing
            if ep.skill not in skill_times:
                skill_times[ep.skill] = []
            skill_times[ep.skill].append(duration)
    
    # Summary
    passed = sum(1 for _, r, _ in all_results if all(x.passed for x in r))
    total_time = sum(d for _, _, d in all_results)
    pass_rate = passed / len(all_results) if all_results else 0
    
    print(f"\n{'='*60}")
    print(f"Total: {passed}/{len(all_results)} passed ({pass_rate*100:.0f}%) in {total_time:.1f}s")
    if args.pass_threshold < 1.0:
        print(f"Pass threshold: {args.pass_threshold*100:.0f}%")
    
    # Per-skill summary table
    if len(skill_times) > 1 or args.skill is None:
        print(f"\n{'Skill':<45} {'Passed':<10} {'Time':<10}")
        print("-" * 65)
        for skill in sorted(skill_times.keys()):
            skill_results = [(l, r, d) for l, r, d in all_results if l.startswith(skill + "/")]
            skill_passed = sum(1 for _, r, _ in skill_results if all(x.passed for x in r))
            skill_total = len(skill_results)
            skill_time = sum(skill_times[skill])
            rate = f"{skill_passed}/{skill_total}"
            print(f"{skill:<45} {rate:<10} {skill_time:.1f}s")
    
    return 0 if pass_rate >= args.pass_threshold else 1


if __name__ == "__main__":
    sys.exit(main())
