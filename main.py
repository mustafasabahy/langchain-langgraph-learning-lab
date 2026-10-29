"""Terminal teaching demos, deterministic evaluation and embedded self-tests.

Python 3.11+. Run --self-test first. All fixtures and receipts are synthetic.
Only --live / menu 2 can call the provider; only --trace enables tracing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import ValidationError

from workflow import (ActionArgs, RecoverableReadError, Report, bounded_read, config,
                      configured_model, make_tools, model_read, preference_change,
                      resume, runtime, sample, tracing)

BASE = Path(__file__).resolve().parent
MENU = [
    "Run complete offline workflow", "Run live LLM workflow", "Inspect and call tools",
    "Conditional routing", "Short-term memory", "Long-term Store across threads",
    "Approval and rejection", "Review and edit", "Tool-call approval and argument editing",
    "Human input validation", "Checkpoint inspection and interrupt/resume",
    "Static debugging breakpoint", "Streaming", "Time travel/replay",
    "Bounded retries and failures", "Offline self-tests", "Local evaluation dataset",
    "Enable LangSmith tracing and evaluation", "Exit",
]

# This registry is both the documentation checklist and evidence-backed coverage report.
# A test status becomes PASS only after the named function actually returns successfully.
DOC = "https://docs.langchain.com/"
COVERAGE = [
    ("Typed StateGraph, START/END, sequential edges", "oss/python/langgraph/overview", "graph", "1", ""),
    ("Conditional invalid/missing/approval/rejection routes", "oss/python/langgraph/overview", "routing", "4,7", ""),
    ("Validated tools, descriptions, three evidence tools", "oss/python/langchain/tools", "tools", "3", "Synthetic data only"),
    ("Live model integration and genuine tool choice", "oss/python/integrations/chat/openai", "credentials", "2", "Provider execution unverified"),
    ("Structured extraction and analysis report", "oss/python/langchain/structured-output", "analysis", "1,2", "Offline extraction validates supplied structured facts; no free-text NLP"),
    ("Malformed arguments, missing/conflicting evidence", "oss/python/langchain/tools", "tools", "3,4", ""),
    ("Provider errors, rate limits/timeouts, bounded retry", "oss/python/integrations/chat/openai", "model_retry", "2,15", "Injected errors tested; real provider skipped"),
    ("Short-term messages and reducers", "oss/python/langgraph/add-memory", "continuity", "5", "Messages untrimmed in this small demo"),
    ("SQLite step checkpoints/current state/metadata", "oss/python/langgraph/checkpointers", "checkpoint", "11", "Local single-user database"),
    ("Durable recovery in a new process", "oss/python/langgraph/checkpointers", "reopen", "11", "Preference Store is separate and ephemeral"),
    ("Stable thread ID and safe completed resume", "oss/python/langgraph/interrupts", "resume", "11", "New thread ID creates independent state"),
    ("Store confirmation and cross-thread retrieval", "oss/python/langgraph/add-memory", "store", "6", "InMemoryStore lasts one process"),
    ("Store update, correction and deletion", "oss/python/langgraph/add-memory", "store", "6", "No durable preference storage"),
    ("Dynamic interrupt and Command resume", "oss/python/langgraph/interrupts", "resume", "1,11", ""),
    ("Human approval/rejection; no premature action", "oss/python/langgraph/interrupts", "approval", "7", "Local pure simulation receipt"),
    ("Review/edit preserves edited content", "oss/python/langgraph/interrupts", "edit", "8", ""),
    ("Tool approval and argument editing/validation", "oss/python/langgraph/interrupts", "tool_approval", "9", "Tool never sends or writes externally"),
    ("Invalid human input, conditional re-prompt, limit", "oss/python/langgraph/interrupts", "human_validation", "10", "Maximum three attempts per gate"),
    ("Interrupt replay safety and serializable payloads", "oss/python/langgraph/interrupts", "approval", "7,11", "One interrupt per node, no side effects before it"),
    ("Static interrupt_before debugging", "oss/python/langgraph/interrupts", "static", "12", "Use dynamic interrupts for HITL"),
    ("Typed stream_events v3 state/interrupt/output", "oss/python/langgraph/event-streaming", "streaming", "13", "Requires LangGraph >=1.2"),
    ("Model message/token projection", "oss/python/langgraph/event-streaming", "streaming", "13", "No model tokens offline; live tokens unverified"),
    ("History inspection and real checkpoint replay", "oss/python/langgraph/use-time-travel", "time_travel", "14", "Replay executes downstream nodes and re-interrupts"),
    ("Fork with update_state; original checkpoint intact", "oss/python/langgraph/use-time-travel", "time_travel", "14", "Branch checkpoint in same thread; latest pointer advances"),
    ("Recoverable tool failure/retry/exhaustion", "oss/python/langchain/tools", "retry", "15", "Only read operations retried"),
    ("No consequential action automatic retry", "oss/python/langgraph/interrupts", "tool_approval", "9,15", "Real side effects need reconciliation/idempotency"),
    ("Optional LangSmith tracing/config/metadata", "langsmith/trace-with-langchain", "tracing_off", "18", "Project-based workspace; cloud upload unverified"),
    ("Synthetic dataset and deterministic evaluation", "langsmith/evaluation", "evaluation", "17", "Local checks; no LLM judge"),
    ("Optional LangSmith SDK dataset/evaluators", "langsmith/evaluate-llm-application", "evaluation", "18", "Cloud evaluate path implemented, unverified"),
    ("Offline operation, missing credentials, mode labels", "oss/python/langchain/overview", "credentials", "1,2", "No implicit fallback from live to offline"),
    ("Embedded suite and CLI menu", "oss/python/langgraph/overview", "graph", "16", "Live/cloud tests skipped by default"),
]


def show(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def fresh(label: str = "demo") -> dict:
    return config(f"{label}-{uuid.uuid4().hex[:10]}")


def paused(graph, cfg: dict) -> bool:
    return any(task.interrupts for task in graph.get_state(cfg).tasks)


def drive(graph, cfg: dict, *, scripted: bool = False, review: Any = None,
          tool: Any = None) -> dict:
    """Interactive runs always ask a human. Scripted approvals are explicit test fixtures."""
    for _ in range(8):
        snapshot = graph.get_state(cfg)
        payloads = [i.value for task in snapshot.tasks for i in task.interrupts]
        if not payloads:
            return snapshot.values
        payload = payloads[0]
        if scripted:
            answer = ((review if review is not None else {"decision": "approve"})
                      if payload["kind"] == "review" else
                      (tool if tool is not None else {"decision": "approve"}))
        else:
            print(f"PAUSED in thread {cfg['configurable']['thread_id']}. No simulated action yet.")
            show(payload)
            raw = input("Resume with JSON (or 'pause' to leave it saved): ")
            if raw.strip().lower() == "pause":
                return snapshot.values
            try:
                answer = json.loads(raw)
            except json.JSONDecodeError:
                print("Invalid JSON; submitting a string so graph validation demonstrates re-prompting.")
                answer = raw
        resume(graph, cfg, answer)
    raise RuntimeError("Driver budget exhausted")


def execute(graph, data: dict | None = None, *, scripted: bool = True,
            review: Any = None, tool: Any = None, cfg: dict | None = None) -> tuple[dict, dict]:
    cfg = cfg or fresh()
    graph.invoke(data if data is not None else sample(), cfg)
    return drive(graph, cfg, scripted=scripted, review=review, tool=tool), cfg


def time_travel(graph) -> dict:
    """Use an isolated demo thread. Preserve the immutable original checkpoint config."""
    original, cfg = execute(graph)
    history = list(graph.get_state_history(cfg))
    before = next(s for s in history if s.next == ("human_review",))
    original_end = graph.get_state(cfg).config
    replay = graph.invoke(None, before.config)
    assert "__interrupt__" in replay  # downstream human node really re-executed
    assert paused(graph, cfg)
    replayed = drive(graph, cfg, scripted=True, review={"decision": "reject"})
    fork_cfg = graph.update_state(before.config, {"draft": "Forked synthetic draft"}, as_node="draft")
    graph.invoke(None, fork_cfg)
    assert paused(graph, fork_cfg)
    forked = drive(graph, cfg, scripted=True)
    historical = graph.get_state(original_end).values
    assert historical["receipt"] == original["receipt"]
    assert forked["receipt"]["content"] == "Forked synthetic draft"
    assert replayed["status"] == "rejected" and not replayed["receipt"]
    return {"checkpoints_before": len(history), "earlier_draft": before.values["draft"],
            "original": original["status"], "replay": replayed["status"],
            "fork": forked["receipt"], "original_checkpoint_unchanged": True,
            "thread_id": cfg["configurable"]["thread_id"],
            "note": "Replay and fork create new checkpoints in this isolated thread; old checkpoints remain."}


def streaming(graph, *, scripted: bool = False, live: bool = False) -> dict:
    cfg = fresh("stream")
    data = sample()
    data["mode"] = "live" if live else "offline"
    print("LIVE model tokens" if live else "OFFLINE simulation of model selection; REAL graph state events, no model tokens")
    stream = graph.stream_events(data, cfg, version="v3")
    values = 0
    for name, item in stream.interleave("values", "messages"):
        if name == "values":
            values += 1
            print("state:", item.get("status", "starting"))
        else:
            for chunk in item.text:
                print(chunk, end="", flush=True)
    interrupted = stream.interrupted
    print("Interrupt detected:", interrupted)
    show(stream.interrupts)
    if interrupted:
        drive(graph, cfg, scripted=scripted)
    final = graph.get_state(cfg).values
    show({"final_status": final.get("status"), "receipt": final.get("receipt", {})})
    return {"values": values, "interrupted": interrupted, "initial_output": stream.output,
            "final": final}


CASES = [
    {"name": "strong approval", "kind": "strong", "expected": "completed"},
    {"name": "missing evidence", "kind": "missing", "expected": "completed", "missing": ["Python", "SQL"]},
    {"name": "unsupported claim", "kind": "conflict", "expected": "completed", "unsupported": ["Kubernetes"]},
    {"name": "human rejection", "kind": "strong", "expected": "rejected", "review": {"decision": "reject"}},
    {"name": "invalid input", "kind": "strong", "expected": "completed", "invalid": True},
    {"name": "tool recovery", "kind": "strong", "expected": "completed", "failures": 2},
    {"name": "tool exhaustion", "kind": "strong", "expected": "failed", "failures": 3},
]


def evaluate_case(case: dict) -> dict:
    with runtime(":memory:") as graph:
        data = sample(case["kind"])
        data["failures"] = case.get("failures", 0)
        cfg = fresh("eval")
        graph.invoke(data, cfg)
        if case.get("invalid"):
            resume(graph, cfg, {"decision": "maybe", "tone": "loud"})
            assert paused(graph, cfg)
            assert "review-invalid" in graph.get_state(cfg).values["events"]
        result = drive(graph, cfg, scripted=True, review=case.get("review"))
        checks = [result["status"] == case["expected"]]
        for key in ("missing", "unsupported"):
            if key in case:
                checks.append(result["report"][key] == case[key])
        if case["expected"] != "completed":
            checks.append(not result["receipt"])
        if case.get("failures"):
            checks.append(result["attempts"] == 3)
        return {"name": case["name"], "status": result["status"], "passed": all(checks)}


def evaluation() -> list[dict]:
    print("LOCAL deterministic evaluation; no model judge or LangSmith cloud experiment")
    results = [evaluate_case(case) for case in CASES]
    for result in results:
        show(result)
    count = sum(r["passed"] for r in results)
    print(f"Evaluation: {count}/{len(results)} passed")
    return results


def cloud_evaluation() -> None:
    """Explicit optional remote experiment over the same deterministic synthetic cases.

    This is cloud evaluation of an offline workflow, NOT model-based evaluation.
    SDK uploads these inline examples/results to the configured project.
    """
    from langsmith import Client
    if not os.getenv("LANGSMITH_API_KEY") or not os.getenv("LANGSMITH_PROJECT"):
        raise ValueError("Cloud evaluation requires LANGSMITH_API_KEY and LANGSMITH_PROJECT")

    def target(inputs: dict) -> dict:
        return evaluate_case(inputs["case"])

    def correct(outputs: dict, reference_outputs: dict) -> dict:
        return {"key": "deterministic_correctness", "score":
                float(outputs["passed"] and outputs["status"] == reference_outputs["status"])}

    client = Client(timeout_ms=10000)
    results = client.evaluate(target, data=[{"inputs": {"case": c},
                                           "outputs": {"status": c["expected"]}} for c in CASES],
                              evaluators=[correct], experiment_prefix="synthetic-fit-demo",
                              max_concurrency=1, upload_results=True)
    print("SDK cloud evaluation returned; inspect the experiment in your configured LangSmith project.")
    print(results)


def raises(kind, call) -> None:
    try:
        call()
    except kind:
        return
    raise AssertionError(f"Expected {kind.__name__}")


def self_tests() -> dict[str, bool]:
    """Mandatory assertions run with tracing explicitly disabled, no paid provider."""
    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="langgraph-learning-") as directory:
        db = str(Path(directory) / "checkpoints.sqlite")
        store = InMemoryStore()
        with tracing(False), runtime(db, store) as graph:
            def graph_test():
                value, _ = execute(graph)
                assert value["status"] == "completed" and value["receipt"]["simulation"] is True
                assert all(n in value["events"] for n in ["validate", "analyze", "draft", "approval-decision", "complete"])

            def routing_test():
                bad = sample(); bad["request"] = ""
                result, _ = execute(graph, bad)
                assert result["status"] == "failed" and not result["receipt"]
                result, _ = execute(graph, sample("missing"))
                assert "missing-evidence-branch" in result["events"]
                assert result["report"]["missing"] == ["Python", "SQL"]

            def tools_test():
                data = sample("conflict")
                tools = make_tools(data["profile"], data["job"])
                assert all(t.description and t.args_schema for t in tools)
                raises(ValidationError, lambda: tools[0].invoke({"query": 42}))
                raises(ValidationError, lambda: tools[0].invoke({"query": ""}))
                assert tools[0].invoke({"query": "absent"}) == {}
                assert tools[1].invoke({})["requirements"] == ["Python", "SQL"]
                assert tools[2].invoke({"requirements": ["Python", "Rust"]}) == {
                    "matched": ["Python"], "missing": ["Rust"], "unsupported": ["Kubernetes"]}

            def analysis_test():
                result, _ = execute(graph, sample("conflict"))
                report = Report.model_validate(result["report"])
                assert report.matched == ["Python", "SQL"] and report.unsupported == ["Kubernetes"]
                assert result["facts"]["profile"]["name"] == "Synthetic Alex"
                assert "offline:simulated-selection:read-attempts=1" in result["events"]

            def continuity_test():
                initial, cfg = execute(graph)
                graph.invoke({"request": "Follow-up: reuse the synthetic job and profile."}, cfg)
                current = graph.get_state(cfg).values
                assert current["profile"] == initial["profile"] and current["job"] == initial["job"]
                assert len(current["messages"]) > len(initial["messages"])
                assert current["messages"][-1].content.startswith("Follow-up")
                assert len(current["events"]) > len(initial["events"])

            def store_test():
                raises(ValueError, lambda: preference_change(store, {"tone": "formal"}, False))
                preference_change(store, {"tone": "formal"}, True)
                _, a = execute(graph); _, b = execute(graph)
                assert a != b
                assert graph.get_state(a).values["preference"]["tone"] == "formal"
                assert "Tone: formal" in graph.get_state(b).values["draft"]
                preference_change(store, {"tone": "concise"}, True)
                result, _ = execute(graph)
                assert result["preference"] == {"tone": "concise"}
                preference_change(store, None, True)
                result, _ = execute(graph)
                assert result["preference"] == {}

            def approval_test():
                cfg = fresh("gates")
                result = graph.invoke(sample(), cfg)
                assert "__interrupt__" in result and not result["receipt"]
                assert "simulated-action" not in result["events"]
                # Serializability is required because checkpoints and UI must carry payloads.
                json.dumps(result["__interrupt__"][0].value)
                resume(graph, cfg, {"decision": "approve"})
                mid = graph.get_state(cfg).values
                assert paused(graph, cfg) and not mid["receipt"]
                rejected = resume(graph, cfg, {"decision": "reject"})
                assert rejected["status"] == "rejected" and "simulated-action" not in rejected["events"]
                rejected, _ = execute(graph, review={"decision": "reject"})
                assert rejected["status"] == "rejected" and not rejected["receipt"]
                approved, _ = execute(graph)
                assert approved["status"] == "completed" and approved["events"].count("simulated-action") == 1

            def edit_test():
                value, _ = execute(graph, review={"decision": "edit", "draft": "Human revised synthetic draft"})
                assert value["draft"] == "Human revised synthetic draft"
                assert value["receipt"]["content"] == value["draft"]

            def tool_test():
                value, _ = execute(graph, tool={"decision": "edit", "arguments": {
                    "destination": "local-archive", "content": "Edited tool arguments"}})
                assert value["receipt"]["destination"] == "local-archive"
                assert value["receipt"]["content"] == "Edited tool arguments"
                raises(ValidationError, lambda: ActionArgs(destination="external", content="synthetic"))
                assert value["events"].index("tool-valid") < value["events"].index("simulated-action")

            def human_test():
                cfg = fresh()
                graph.invoke(sample(), cfg)
                resume(graph, cfg, "not a mapping")
                assert paused(graph, cfg) and graph.get_state(cfg).values["review_attempts"] == 1
                resume(graph, cfg, {"decision": "edit", "draft": ""})
                assert paused(graph, cfg)
                resume(graph, cfg, {"decision": "approve", "tone": "formal"})
                resume(graph, cfg, {"decision": "edit", "arguments": {
                    "destination": "external", "content": "invalid"}})
                assert paused(graph, cfg) and not graph.get_state(cfg).values["receipt"]
                value = resume(graph, cfg, {"decision": "approve"})
                assert value["status"] == "completed" and "review-invalid" in value["events"]
                value, _ = execute(graph, review={"decision": "invalid"})
                assert value["status"] == "failed" and value["review_attempts"] == 3 and not value["receipt"]
                value, _ = execute(graph, tool={"decision": "invalid"})
                assert value["status"] == "failed" and value["tool_attempts"] == 3 and not value["receipt"]

            def checkpoint_test():
                cfg = fresh("checkpoint")
                graph.invoke(sample(), cfg)
                snapshot = graph.get_state(cfg)
                history = list(graph.get_state_history(cfg))
                assert len(history) >= 5 and snapshot.metadata["step"] >= 1
                assert snapshot.config["configurable"]["thread_id"] == cfg["configurable"]["thread_id"]
                assert snapshot.next == ("human_review",)

            def resume_test():
                cfg = fresh("resume")
                graph.invoke(sample(), cfg)
                other = fresh("other")
                raises(ValueError, lambda: resume(graph, other, {"decision": "approve"}))
                assert paused(graph, cfg) and not graph.get_state(other).values
                result = drive(graph, cfg, scripted=True)
                assert result["status"] == "completed"
                raises(ValueError, lambda: resume(graph, cfg, {"decision": "approve"}))

            def reopen_test():
                cfg = config("durable-process-test")
                graph.invoke(sample(), cfg)
                # A separate interpreter opens the persisted SQLite thread and resumes it.
                command = ("from workflow import runtime,config,resume,tracing; "
                           f"db={db!r}; "
                           "ctx=tracing(False); ctx.__enter__(); "
                           "rt=runtime(db); g=rt.__enter__(); "
                           "r=resume(g,config('durable-process-test'),{'decision':'reject'}); "
                           "assert r['status']=='rejected' and not r['receipt']; "
                           "rt.__exit__(None,None,None); ctx.__exit__(None,None,None)")
                done = subprocess.run([sys.executable, "-c", command], cwd=BASE,
                                      capture_output=True, text=True, timeout=30)
                assert done.returncode == 0, done.stderr
                assert graph.get_state(cfg).values["status"] == "rejected"

            def static_test():
                with runtime(":memory:", debug=True) as debug:
                    cfg = fresh("debug")
                    debug.invoke(sample(), cfg)
                    snapshot = debug.get_state(cfg)
                    assert snapshot.next == ("analyze",) and not paused(debug, cfg)
                    debug.invoke(None, cfg)
                    assert paused(debug, cfg)

            def stream_test():
                value = streaming(graph, scripted=True)
                assert value["values"] >= 5 and value["interrupted"]
                assert value["initial_output"]["draft"]
                assert value["final"]["status"] == "completed"

            def travel_test():
                value = time_travel(graph)
                assert value["original_checkpoint_unchanged"] and value["replay"] == "rejected"

            def retry_test():
                data = sample(); data["failures"] = 2
                result, _ = execute(graph, data)
                assert result["attempts"] == 3 and result["status"] == "completed"
                data["failures"] = 3
                result, _ = execute(graph, data)
                assert result["status"] == "failed" and not result["receipt"]
                tools = make_tools(data["profile"], data["job"])
                raises(RecoverableReadError, lambda: bounded_read(tools, 5))

            def model_retry_test():
                from openai import APIConnectionError, APITimeoutError, RateLimitError
                import httpx
                calls = []
                def intermittent():
                    calls.append(1)
                    if len(calls) == 1:
                        raise APIConnectionError(request=httpx.Request("POST", "https://synthetic.invalid"))
                    return "recovered"
                assert model_read(intermittent) == "recovered" and len(calls) == 2
                calls.clear()
                def unavailable():
                    calls.append(1)
                    raise APIConnectionError(request=httpx.Request("POST", "https://synthetic.invalid"))
                raises(APIConnectionError, lambda: model_read(unavailable))
                assert len(calls) == 2
                request = httpx.Request("POST", "https://synthetic.invalid")
                for error in (APITimeoutError(request=request), RateLimitError(
                        "Synthetic rate limit", response=httpx.Response(429, request=request), body={})):
                    calls.clear()
                    def throttled():
                        calls.append(1)
                        raise error
                    raises(type(error), lambda: model_read(throttled))
                    assert len(calls) == 2

            def credentials_test():
                from unittest.mock import patch
                with patch.dict(os.environ, {"DEMO_LIVE": "0", "OPENAI_API_KEY": "", "OPENAI_MODEL": ""}):
                    raises(ValueError, configured_model)
                    value, _ = execute(graph)
                    assert value["status"] == "completed"
                with patch.dict(os.environ, {"DEMO_LIVE": "1", "OPENAI_API_KEY": "", "OPENAI_MODEL": ""}):
                    raises(ValueError, configured_model)

            def tracing_test():
                from langsmith import get_tracing_context
                with tracing(False):
                    assert get_tracing_context()["enabled"] is False
                    value, _ = execute(graph)
                    assert value["status"] == "completed"

            def eval_test():
                first = evaluation()
                second = [evaluate_case(c) for c in CASES]
                assert len(first) == 7 and all(r["passed"] for r in first) and first == second

            tests = {"graph": graph_test, "routing": routing_test, "tools": tools_test,
                     "analysis": analysis_test, "continuity": continuity_test, "store": store_test,
                     "approval": approval_test, "edit": edit_test, "tool_approval": tool_test,
                     "human_validation": human_test, "checkpoint": checkpoint_test,
                     "resume": resume_test, "reopen": reopen_test, "static": static_test,
                     "streaming": stream_test, "time_travel": travel_test, "retry": retry_test,
                     "model_retry": model_retry_test, "credentials": credentials_test,
                     "tracing_off": tracing_test, "evaluation": eval_test}
            for name, test in tests.items():
                try:
                    test()
                except Exception as exc:
                    results[name] = False
                    print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                else:
                    results[name] = True
                    print(f"PASS {name}")
    print(f"Self-tests: {sum(results.values())}/{len(results)} passed")
    print("SKIPPED live provider, native model structured output, live tokens, LangSmith upload/cloud evaluation/LLM judge")
    print(coverage_matrix(results))
    return results


def coverage_matrix(results: dict[str, bool] | None = None) -> str:
    rows = ["| Concept | Official docs checked | Implemented | Offline test passed | Live/cloud test passed | Demo menu option | Limitation |",
            "|---|---|---|---|---|---|---|"]
    for concept, doc, test, menu, limitation in COVERAGE:
        status = "Not run" if results is None else ("PASS" if results.get(test) else "FAIL")
        if concept.startswith("Live model"):
            status += " (configuration only)"
        if concept.startswith("Model message"):
            status += " (state stream only)"
        if concept.startswith("Optional LangSmith SDK"):
            status += " (local cases/evaluator only)"
        if concept.startswith("Optional LangSmith tracing"):
            status += " (disabled operation only)"
        if concept.startswith("Provider errors"):
            status += " (injected connection/rate-limit/timeout failures)"
        rows.append(f"| {concept} | [Docs]({DOC}{doc}) | Yes | {status} | Skipped | {menu} | {limitation or '—'} |")
    return "\n".join(rows)


def explain_langsmith() -> None:
    print("""Tracing is OFF unless --trace is supplied. Project-based LangSmith configuration:
  export LANGSMITH_TRACING=true
  export LANGSMITH_API_KEY='<set privately>'
  export LANGSMITH_PROJECT='synthetic-learning-demo'
  # If required: LANGSMITH_ENDPOINT and LANGSMITH_WORKSPACE_ID
  python main.py --demo 1 --trace
Graph nodes, tools, model calls and thread metadata become trace candidates.
Only synthetic inputs belong here. CLI events are local logs, never proof of upload.
SDK delivery failures do not make local workflow output invalid; inspect LangSmith to confirm delivery.
Agent-based workspaces instead use LANGSMITH_AGENT_ID / LANGSMITH_AGENT_ENVIRONMENT;
this demo explicitly targets project-based workspaces and does not provision agents.
Local evaluation: python main.py --evaluate (deterministic assertions, no LLM judge).
Cloud evaluation: python main.py --cloud-eval (explicit synthetic upload via Client.evaluate).
Cloud evaluation uses the same seven inline examples/evaluator; it is not model-based evaluation.
Live provider: DEMO_LIVE=1 OPENAI_PROVIDER=openai OPENAI_MODEL=<supported-model> and
OPENAI_API_KEY set privately, then python main.py --live. No cloud/live test has run by default.""")


def demo(option: int, graph, store, *, scripted: bool = False) -> None:
    print(f"\n{option}. {MENU[option - 1]}")
    if scripted:
        print("SCRIPTED OFFLINE DEMONSTRATION: test fixtures supply human decisions explicitly.")
    if option in (1, 2, 7, 8, 9, 10):
        if option == 2:
            configured_model()  # fail clearly before invoking a graph without explicit config
        explanations = {
            1: "Offline uses deterministic tool selection, not a model call.",
            2: "LIVE provider with real model-chosen read tools and structured report.",
            7: "Try reject at either gate: cancellation has no simulated action receipt.",
            8: 'At review try {"decision":"edit","draft":"Edited synthetic draft"}.',
            9: 'At tool approval try {"decision":"edit","arguments":{"destination":"local-archive","content":"Edited"}}.',
            10: 'Try {"decision":"maybe"}, then a valid decision. Validation routes back; three failures stop.',
        }
        print(explanations[option])
        data = sample(); data["mode"] = "live" if option == 2 else "offline"
        review = {"decision": "reject"} if scripted and option == 7 else None
        if scripted and option == 8:
            review = {"decision": "edit", "draft": "Scripted edited synthetic draft"}
        tool = {"decision": "edit", "arguments": {"destination": "local-archive", "content": "Scripted edited args"}} if scripted and option == 9 else None
        cfg = fresh()
        graph.invoke(data, cfg)
        if scripted and option == 10:
            resume(graph, cfg, {"decision": "maybe"})
            print("Invalid input produced another real interrupt:", paused(graph, cfg))
        result = drive(graph, cfg, scripted=scripted, review=review, tool=tool)
        show({"thread_id": cfg["configurable"]["thread_id"], "status": result.get("status"),
              "report": result.get("report"), "draft": result.get("draft"), "receipt": result.get("receipt"),
              "error": result.get("error")})
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error", "Workflow failed"))
    elif option == 3:
        data = sample("conflict"); tools = make_tools(data["profile"], data["job"])
        for t, args in zip(tools, [{"query": "Python"}, {}, {"requirements": ["Python", "Rust"]}]):
            print(t.name, t.description); show(t.args_schema.model_json_schema()); show(t.invoke(args))
        try:
            tools[0].invoke({"query": 123})
        except ValidationError:
            print("Malformed argument rejected by Pydantic (not retried).")
    elif option == 4:
        print("A sequential workflow always follows the same chain. This graph branches on input, evidence and decisions.")
        for kind in ("strong", "missing", "conflict", "invalid"):
            data = sample(kind)
            if kind == "invalid": data["request"] = ""
            cfg = fresh(); graph.invoke(data, cfg)
            result = graph.get_state(cfg).values
            show({"case": kind, "status": result["status"], "events": result["events"], "report": result.get("report")})
    elif option == 5:
        print("Same thread preserves messages, job and profile. Only the follow-up request is supplied.")
        value, cfg = execute(graph, scripted=scripted)
        if paused(graph, cfg): return
        graph.invoke({"request": "Follow-up: reuse earlier synthetic facts."}, cfg)
        current = graph.get_state(cfg).values
        show({"thread": cfg, "before_messages": len(value["messages"]), "after_messages": len(current["messages"]),
              "retained_profile": current["profile"]})
        drive(graph, cfg, scripted=scripted)
    elif option == 6:
        print("Preferences are in InMemoryStore, outside checkpoints; they last this menu session only.")
        confirmed = scripted or input("Confirm storing synthetic tone 'formal'? [yes/no] ").strip().lower() == "yes"
        if not confirmed:
            print("No memory saved."); return
        preference_change(store, {"tone": "formal"}, True)
        for label in ("thread-A", "thread-B"):
            cfg = fresh(label); graph.invoke(sample(), cfg)
            show({"thread": cfg, "retrieved": graph.get_state(cfg).values["preference"]})
        if scripted or input("Confirm correction to concise? [yes/no] ").strip().lower() == "yes":
            preference_change(store, {"tone": "concise"}, True)
            show(store.get(("synthetic-user", "preferences"), "writing").value)
        if scripted or input("Confirm deleting this memory? [yes/no] ").strip().lower() == "yes":
            preference_change(store, None, True)
            print("Deleted:", store.get(("synthetic-user", "preferences"), "writing") is None)
    elif option == 11:
        cfg = fresh("persistent"); graph.invoke(sample(), cfg)
        snapshot = graph.get_state(cfg)
        show({"resume_command": f"python main.py --thread {cfg['configurable']['thread_id']} --resume",
              "checkpoint": snapshot.config, "metadata": snapshot.metadata, "next": snapshot.next,
              "history_count": len(list(graph.get_state_history(cfg)))})
        if scripted: drive(graph, cfg, scripted=True)
        else: print("Saved. Use the printed command in a new process with this database to review/resume.")
    elif option == 12:
        print("Static interrupt_before is a debugger breakpoint; resume it with None, not a human answer.")
        with runtime(":memory:", store, debug=True) as debug:
            cfg = fresh(); debug.invoke(sample(), cfg)
            print("Stopped before:", debug.get_state(cfg).next)
            if not scripted: input("Enter to step past the static breakpoint: ")
            debug.invoke(None, cfg)
            print("Now at a dynamic human-review interrupt.")
            drive(debug, cfg, scripted=scripted)
    elif option == 13:
        streaming(graph, scripted=scripted)
    elif option == 14:
        print("Isolated replay experiment: explicit scripted decisions compare original, reject replay and edited fork.")
        show(time_travel(graph))
    elif option == 15:
        print("Read-only tools retry up to three times. Consequential simulation is never automatically retried.")
        data = sample(); tools = make_tools(data["profile"], data["job"])
        for failures in (0, 2, 3):
            try:
                value, attempts = bounded_read(tools, failures)
                show({"injected_failures": failures, "attempts": attempts, "evidence": value})
            except RecoverableReadError:
                show({"injected_failures": failures, "status": "failed after three attempts"})
    elif option == 16:
        if not all(self_tests().values()): raise RuntimeError("Mandatory self-tests failed")
    elif option == 17:
        if not all(r["passed"] for r in evaluation()): raise RuntimeError("Evaluation failed")
    elif option == 18:
        explain_langsmith()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--cloud-eval", action="store_true", help="Explicitly upload synthetic examples/results")
    parser.add_argument("--demo-all", action="store_true", help="Explicit scripted offline harness, skips live menu 2")
    parser.add_argument("--demo", type=int, choices=range(1, 20))
    parser.add_argument("--live", action="store_true", help="Explicit provider workflow (also requires DEMO_LIVE=1)")
    parser.add_argument("--live-stream", action="store_true")
    parser.add_argument("--trace", action="store_true", help="Explicit LangSmith tracing opt-in")
    parser.add_argument("--database", default=str(BASE / "checkpoints.sqlite"))
    parser.add_argument("--thread", help="Stable thread ID for inspection/resume")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--input-json", type=Path, help="Synthetic request/job/profile JSON; mode forced by CLI")
    parser.add_argument("--report", type=Path, help="Write self-test coverage report")
    args = parser.parse_args()
    try:
        if args.self_test:
            results = self_tests()
            if args.report:
                args.report.write_text("# Verification\n\n" +
                    f"Mandatory offline tests: {sum(results.values())}/{len(results)} passed.\n\n" +
                    coverage_matrix(results) + "\n", encoding="utf-8")
            return 0 if all(results.values()) else 1
        with tracing(args.trace):
            if args.cloud_eval:
                cloud_evaluation(); return 0
            if args.evaluate:
                return 0 if all(r["passed"] for r in evaluation()) else 1
            store = InMemoryStore()
            with runtime(args.database, store) as graph:
                if args.thread:
                    cfg = config(args.thread); snapshot = graph.get_state(cfg)
                    show({"thread": cfg, "next": snapshot.next, "metadata": snapshot.metadata, "state": snapshot.values})
                    if args.resume:
                        if not paused(graph, cfg): raise ValueError("Thread has no pending dynamic interrupt")
                        show(drive(graph, cfg))
                    return 0
                if args.live_stream:
                    configured_model(); streaming(graph, live=True); return 0
                if args.input_json:
                    data = json.loads(args.input_json.read_text())
                    data["mode"] = "live" if args.live else "offline"
                    if args.live: configured_model()
                    print("LIVE" if args.live else "OFFLINE deterministic simulation")
                    result, cfg = execute(graph, data, scripted=False)
                    show({"thread": cfg, "result": result}); return 0 if result["status"] != "failed" else 1
                if args.demo_all:
                    for option in range(1, 19):
                        if option == 2:
                            print("2. SKIPPED live workflow: offline harness never calls provider")
                        else:
                            demo(option, graph, store, scripted=True)
                    return 0
                if args.live or args.demo:
                    demo(2 if args.live else args.demo, graph, store); return 0
                print("Synthetic learning project. OFFLINE by default; SQLite checkpoints durable; Store ephemeral.")
                while True:  # Menu loop only; interrupt retry uses conditional graph edges.
                    for number, label in enumerate(MENU, 1): print(f"{number:2}. {label}")
                    try:
                        choice = int(input("Choose 1-19: "))
                    except ValueError:
                        print("Enter a number from 1 to 19."); continue
                    if choice == 19: return 0
                    if 1 <= choice <= 18:
                        try:
                            demo(choice, graph, store)
                        except (ValueError, RuntimeError) as exc:
                            print(str(exc))
                    else: print("Choose 1-19.")
    except (EOFError, KeyboardInterrupt):
        print("\nStopped. Previously committed SQLite checkpoints remain available."); return 0
    except Exception as exc:
        # Never print provider exceptions/requests, which may expose private data.
        if isinstance(exc, ValueError): print(str(exc), file=sys.stderr)
        else: print(f"Operation failed ({type(exc).__name__}); no external action was performed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
