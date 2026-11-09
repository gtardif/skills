"""Unit tests for eval_judge.py."""

import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import eval_judge


SAMPLE_EVAL_MD = """\
# Eval: docker-sandboxes-lifecycle

Test skill description.

---

## Prompt 1: simple question

**Prompt to agent:**

> What is the difference between create and run?

### Expected behaviors
- [ ] Explains the difference clearly.
- [ ] Recommends the correct command.

### Must not
- [ ] Must NOT give incorrect information.

### Verification commands
```bash
echo "test"
```

---

## Prompt 2: multi-line criteria

**Prompt to agent:**

> How do I clean up sandboxes?

### Expected behaviors
- [ ] Recommends `sbx prune` with `--filter until=DURATION` (current
      pinned-source flag; e.g. `until=168h`) and `--dry-run` first.
- [ ] States that `sbx prune` never removes a running sandbox.

### Must not
- [ ] Must NOT recommend `sbx rm --all` as the routine cleanup.
- [ ] Must NOT describe `sbx prune` as "safe to run habitually"
      without qualification.

### Verification commands
```bash
sbx prune --dry-run
```
"""


class ParseEvalMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        self.tmpfile.write(SAMPLE_EVAL_MD)
        self.tmpfile.flush()
        self.path = Path(self.tmpfile.name)

    def tearDown(self):
        self.tmpfile.close()
        self.path.unlink(missing_ok=True)

    def test_parses_skill_name(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        self.assertTrue(all(p.skill == "docker-sandboxes-lifecycle" for p in prompts))

    def test_parses_two_prompts(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        self.assertEqual(len(prompts), 2)

    def test_parses_prompt_text(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        self.assertEqual(prompts[0].prompt, "What is the difference between create and run?")
        self.assertEqual(prompts[1].prompt, "How do I clean up sandboxes?")

    def test_parses_expected_behaviors(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        self.assertEqual(len(prompts[0].expected_behaviors), 2)
        self.assertEqual(len(prompts[1].expected_behaviors), 2)

    def test_parses_must_not(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        self.assertEqual(len(prompts[0].must_not), 1)
        self.assertEqual(len(prompts[1].must_not), 2)

    def test_handles_multiline_criteria(self):
        prompts = eval_judge.parse_eval_markdown(self.path)
        # Check multi-line criterion is joined
        self.assertIn("until=168h", prompts[1].expected_behaviors[0])
        self.assertIn("dry-run", prompts[1].expected_behaviors[0])


class DiscoverEvalsTests(unittest.TestCase):
    def test_discovers_real_evals(self):
        # Should find evals in skills/ and evals/ directories
        prompts = eval_judge.discover_evals(None, None)
        self.assertTrue(len(prompts) > 0)

    def test_filters_by_skill_name(self):
        prompts = eval_judge.discover_evals("docker-sandboxes-lifecycle", None)
        self.assertTrue(all(p.skill == "docker-sandboxes-lifecycle" for p in prompts))

    def test_filters_by_prompt_num(self):
        prompts = eval_judge.discover_evals("docker-sandboxes-lifecycle", 1)
        self.assertTrue(all(p.prompt_num == 1 for p in prompts))
    
    def test_prefers_skills_dir_over_evals_dir(self):
        # If <skill>.md exists in skills/<skill>/evals/, it should be used over evals/<skill>.md
        # We can test this by checking that docker-sandboxes-lifecycle is found
        # (it exists in both places now)
        prompts = eval_judge.discover_evals("docker-sandboxes-lifecycle", None)
        self.assertTrue(len(prompts) > 0)
        # All prompts should come from the same file
        eval_files = set(p.eval_file for p in prompts)
        self.assertEqual(len(eval_files), 1)
        # Should be from the skills directory
        self.assertIn("skills/docker-sandboxes-lifecycle/evals", str(list(eval_files)[0]))


class TranscriptToTextTests(unittest.TestCase):
    def test_converts_assistant_text(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}\n')
            f.flush()
            text = eval_judge.transcript_to_text(Path(f.name))
        self.assertIn("ASSISTANT: Hello", text)

    def test_converts_tool_calls(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n')
            f.flush()
            text = eval_judge.transcript_to_text(Path(f.name))
        self.assertIn("TOOL CALL [Bash]", text)
        self.assertIn("ls", text)


class CodexTranscriptToTextTests(unittest.TestCase):
    def test_converts_agent_message(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type":"item.completed","item":{"type":"agent_message","text":"Hello from Codex"}}\n')
            f.flush()
            text = eval_judge.codex_transcript_to_text(Path(f.name))
        self.assertIn("ASSISTANT: Hello from Codex", text)

    def test_converts_command_execution(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type":"item.completed","item":{"type":"command_execution","command":"/bin/zsh -lc \'ls -la\'","aggregated_output":"file1 file2"}}\n')
            f.flush()
            text = eval_judge.codex_transcript_to_text(Path(f.name))
        self.assertIn("TOOL CALL [Bash]", text)
        self.assertIn("ls -la", text)
        self.assertIn("TOOL RESULT: file1 file2", text)

    def test_converts_skill_read(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type":"item.completed","item":{"type":"skill_read","skill_path":"/path/to/.agents/skills/docker-sandboxes-lifecycle/SKILL.md"}}\n')
            f.flush()
            text = eval_judge.codex_transcript_to_text(Path(f.name))
        self.assertIn("TOOL CALL [Skill]", text)
        self.assertIn("docker-sandboxes-lifecycle", text)


if __name__ == "__main__":
    unittest.main()
