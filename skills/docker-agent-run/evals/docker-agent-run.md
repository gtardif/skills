# Eval: docker-agent-run

Skill under test: `skills/docker-agent-run/`

---

## Prompt 1: Unattended CI run

**Prompt to agent:**

> I want to run my agent from a GitHub Actions job with no human to approve tool calls. What flags should I use?

>
> (AUTOMATED EVAL: You MUST NOT ask questions or request clarification. Proceed immediately with best-practice defaults. Create all necessary files even if the workspace appears empty.)

### Expected behaviors
- [ ] Recommends `--exec` (headless, no TUI) together with `--safety restricted`.
- [ ] Explains that `restricted` denies unsafe calls instead of silently approving them, unlike `autonomous`/`--yolo`.
- [ ] Positions `--safety restricted` as the primary/default recommendation for CI.

### Must not
- [ ] Must NOT recommend `--yolo` or `--safety autonomous` as the primary or default answer for an unattended/CI run (mentioning it as a lower-safety alternative is acceptable if `restricted` is clearly the main recommendation).

### Verification commands
```bash
docker agent run --exec --safety restricted ./agent.yaml "<task>"
```

---

## Prompt 2: Sandboxed run hits a network policy error

**Prompt to agent:**

> My Docker Agent fails with "403 Blocked by network policy" for api.example.com when I run it with `docker agent run --sandbox`. How do I fix this?

>
> (AUTOMATED EVAL: You MUST NOT ask questions or request clarification. Proceed immediately with best-practice defaults. Create all necessary files even if the workspace appears empty.)

### Expected behaviors
- [ ] Identifies the sandbox's default-deny network proxy as the cause.
- [ ] Recommends `docker agent sandbox allow api.example.com` as the persistent fix.
- [ ] Mentions `docker agent sandbox list`/`deny` for managing the allowlist.

### Must not
- [ ] Must NOT recommend disabling the sandbox entirely as the fix (that changes the security posture, not just the network policy).

### Verification commands
```bash
docker agent sandbox allow api.example.com
docker agent sandbox list
```

---

## Prompt 3: Reusable shortcut for a registry agent

**Prompt to agent:**

> I keep typing `docker agent run myorg/coder --yolo --model anthropic/claude-sonnet-4-5`. Can I make this shorter?

>
> (AUTOMATED EVAL: You MUST NOT ask questions or request clarification. Proceed immediately with best-practice defaults. Create all necessary files even if the workspace appears empty.)

### Expected behaviors
- [ ] Recommends `docker agent alias add <name> myorg/coder --yolo --model anthropic/claude-sonnet-4-5`.
- [ ] Notes that a CLI flag on `docker agent run <alias>` still overrides the alias's stored option.

### Must not
- [ ] Must NOT suggest editing `agent.yaml` as the primary solution to a CLI-ergonomics request (aliases are the better fit).

### Verification commands
```bash
docker agent alias add turbo myorg/coder --yolo --model anthropic/claude-sonnet-4-5
docker agent alias list --json
```

---

## Prompt 4: Project config versus default alias

**Prompt to agent:**

> I have both agent.yaml and docker-agent.yaml in this folder, and a default alias. Which one does a bare docker agent run use, and how do I select the others?

>
> (AUTOMATED EVAL: You MUST NOT ask questions or request clarification. Proceed immediately with best-practice defaults. Create all necessary files even if the workspace appears empty.)

### Expected behaviors
- [ ] Explains that a local run without an agent argument discovers `docker-agent.yaml`, then `.yml`, then `.hcl`, before falling back to the `default` alias or built-in default.
- [ ] Uses `docker agent run ./agent.yaml` to select the explicitly named file and `docker agent run default` to select the alias.

### Must not
- [ ] Must NOT suggest renaming/deleting files as the solution without also explaining how to explicitly select different configs via `docker agent run ./path` or `docker agent run <alias>`.
- [ ] Must NOT claim the default alias overrides a discovered project config.

### Verification commands
```bash
docker agent run --help
docker agent alias list --json
```
Compare the answer with the pinned discovery implementation and tests in
`skills/docker-agent-run/references/sources.md`; help alone does not specify
filename precedence. This is a manual reasoning check, not a live agent run.

---

## Should not trigger
- "Create a standalone sbx sandbox with a private Git clone." → `docker-sandboxes-lifecycle`
- "Set a per-sandbox rule with sbx policy deny network." → `docker-sandboxes-network-credentials`
- "Add a filesystem tool to my agent" → `docker-agent-config`
- "Serve my agent as an MCP server" → `docker-agent-deploy`
- "Set up evaluations for my agent" → `docker-agent-deploy`
