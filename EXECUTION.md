# Execution and release notes

Release preparation verified on **2026-10-29**, Python **3.12.4**, macOS ARM64.
This is the existing two-source-file implementation; no application code was
recreated for publication. No unrelated project was modified.

## Reproducible commands

Run from the repository root with an isolated environment outside the repository:

```sh
python3 -m venv ../graphgate-venv
source ../graphgate-venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
python -m py_compile workflow.py main.py
python main.py --self-test --report VERIFICATION.md
python main.py --evaluate
python main.py --demo-all --database /tmp/graphgate-demo.sqlite
```

The release checks used the already-installed isolated environment. All validation
commands above were run with its Python interpreter; exit codes were zero.
No global packages were installed. The pinned snapshot is retained unchanged.

| Check | Actual result |
|---|---|
| Dependency consistency | No broken requirements found |
| Syntax compilation | Both modules compiled |
| Mandatory offline suite | 21 passed, 0 failed |
| Deterministic evaluation | 7 passed, 0 failed |
| Offline demonstration harness | 17 offline menu options executed successfully |
| Live option in harness | Explicitly skipped |
| Source inventory | Exactly `main.py` and `workflow.py` |

Principal versions: LangChain 1.4.7, LangGraph 1.2.18, langchain-openai 1.7.3,
SQLite checkpointer 3.1.2, LangSmith 0.14.14, Pydantic 2.14.1.
Python 3.11+ is required; other operating systems/Python versions are unverified.

## What the assertions actually exercise

- Graph construction and execution, invalid/missing/unsupported evidence branches.
- LangChain tool schemas and malformed arguments; deterministic structured analysis.
- Short-term state/message continuity and confirmed cross-thread preference Store
  creation, correction/update, deletion and refusal without confirmation.
- Real dynamic interrupts, stable thread IDs, safe stale resumes, both rejection
  gates, no premature action, preserved draft edits and validated tool-argument edits.
- Invalid human answers, conditional re-prompting and exhaustion at both gates.
- Checkpoint metadata/history; a second interpreter opens the same SQLite database,
  resumes the same paused thread and persists rejection for the first process to read.
- Static interrupt_before, typed stream_events v3 state/interrupt/output projections.
- Actual historical-checkpoint replay and update_state fork, re-triggered review,
  changed fork output and preservation of original checkpoint contents.
- Recoverable read errors, bounded retry/exhaustion, injected provider connection,
  timeout/rate-limit errors, no-credentials operation and disabled LangSmith tracing.
- Seven deterministic evaluation cases run twice and compared for identical results.

Initial implementation verification also exercised six CLI subprocess scenarios:
approval, draft edit, rejection, invalid-answer re-prompt, missing live opt-in and
menu exit. All six passed after correcting an external harness interpreter path.
Local transcripts and raw execution logs are not part of the public release.

## Public release boundaries

The staged release is limited to `.gitignore`, `README.md`, `EXECUTION.md`,
`VERIFICATION.md`, `requirements.txt`, `main.py` and `workflow.py`. Local transcripts,
raw generated output, Python caches, virtual environments and SQLite state are
excluded. The Git commit uses the authenticated account's public GitHub no-reply
address rather than the pre-existing local work email configuration.

Pre-publication review checked the explicit staged file list and staged diff.
An inline local scan of all seven staged blobs checked known token formats,
private-key blocks, credential assignments, private absolute paths and email
addresses: **zero findings**. Ignore checks passed for representative secrets,
virtual environments, databases, logs and transcripts. Neither gitleaks nor
trufflehog was installed; this was a bounded local pattern scan, not a guarantee
that every possible secret format is detected. The final staged whitespace check
passed. Only the seven listed files are included.

## Skipped and limited

Real provider calls, native model structured output, live tokens, LangSmith trace
uploads and cloud evaluation were **not run**. No cloud trace/dataset/experiment
success or LLM judge is claimed. The optional cloud evaluator scores the offline
workflow deterministically; automated model-based judging is not implemented.

Offline input is already structured synthetic job/profile information, not free-text
extraction. Skill comparison uses exact keys. InMemoryStore lasts one process;
SQLite thread checkpoints are durable. Replay/fork creates new checkpoints in its
isolated thread and advances that thread's latest pointer, preserving old snapshots.

See [VERIFICATION.md](VERIFICATION.md) for the official-documentation coverage matrix.
