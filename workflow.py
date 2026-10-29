"""Synthetic LangChain/LangGraph laboratory. No applications or emails are sent.

Checkpoints persist thread state in SQLite. Preferences live in a separate Store.
Interrupt nodes have no side effects and exactly one stable interrupt each.
"""
from __future__ import annotations

import json
import operator
import os
import time
from contextlib import contextmanager
from typing import Annotated, Any, Literal, TypedDict

from langchain.tools import tool
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Job(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    requirements: list[str] = Field(min_length=1, max_length=12)


class Profile(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    confirmed: dict[str, str]
    claims: list[str]


class Request(StrictModel):
    request: str = Field(min_length=1, max_length=500)
    job: Job
    profile: Profile


class Report(StrictModel):
    matched: list[str]
    missing: list[str]
    unsupported: list[str]
    summary: str


class Review(StrictModel):
    decision: Literal["approve", "reject", "edit"]
    draft: str | None = Field(default=None, min_length=1, max_length=4000)
    tone: Literal["concise", "formal"] = "concise"


class ActionArgs(StrictModel):
    destination: Literal["local-preview", "local-archive"]
    content: str = Field(min_length=1, max_length=4000)


class ToolDecision(StrictModel):
    decision: Literal["approve", "reject", "edit"]
    arguments: ActionArgs | None = None


class Query(StrictModel):
    query: str = Field(min_length=1, max_length=100)


class Comparison(StrictModel):
    requirements: list[str] = Field(min_length=1, max_length=12)


class State(TypedDict, total=False):
    request: str
    job: dict
    profile: dict
    mode: str
    # add_messages merges by message ID, rather than replacing the conversation.
    messages: Annotated[list[AnyMessage], add_messages]
    # Events append; all other keys use the default replacement reducer.
    events: Annotated[list[str], operator.add]
    facts: dict
    evidence: dict
    report: dict
    draft: str
    raw_review: Any
    raw_tool: Any
    decision: str
    tool_decision: str
    action_args: dict
    review_attempts: int
    tool_attempts: int
    validation_error: str
    error: str
    status: str
    receipt: dict
    preference: dict
    failures: int
    attempts: int


def sample(kind: str = "strong") -> dict:
    """A closed synthetic fixture, never a real candidate."""
    facts = {"Python": "Built fictional batch pipeline P1", "SQL": "Wrote fictional queries S1"}
    if kind == "missing":
        facts = {}
    claims = ["Kubernetes"] if kind == "conflict" else []
    return {"request": "Analyze this synthetic fit and prepare a local draft.",
            "job": {"title": "Fictional Data Developer", "requirements": ["Python", "SQL"]},
            "profile": {"name": "Synthetic Alex", "confirmed": facts, "claims": claims},
            "mode": "offline", "failures": 0}


def make_tools(profile: dict, job: dict) -> list:
    """Closures scope retrieval to this request, including user-supplied synthetic data."""
    @tool(args_schema=Query)
    def search_candidate_facts(query: str) -> dict:
        """Find confirmed synthetic candidate evidence by skill, or '*' for all facts."""
        facts = profile["confirmed"]
        return {k: v for k, v in facts.items() if query == "*" or query.lower() in k.lower()}

    @tool
    def retrieve_job_requirements() -> dict:
        """Retrieve the current fictional job's title and explicit skill requirements."""
        return job

    @tool(args_schema=Comparison)
    def compare_confirmed_evidence(requirements: list[str]) -> dict:
        """Compare skills with confirmed evidence; unsupported profile claims never count."""
        confirmed = profile["confirmed"]
        return {"matched": [r for r in requirements if r in confirmed],
                "missing": [r for r in requirements if r not in confirmed],
                "unsupported": [c for c in profile["claims"] if c not in confirmed]}

    return [search_candidate_facts, retrieve_job_requirements, compare_confirmed_evidence]


@tool(args_schema=ActionArgs)
def simulate_consequential_action(destination: str, content: str) -> dict:
    """Return a LOCAL SIMULATION receipt. Never write or send anything externally."""
    return {"simulation": True, "destination": destination, "content": content}


class RecoverableReadError(RuntimeError):
    """Injected transient failure for a read-only tool, safe to retry."""


def bounded_read(tools: list, failures: int, max_attempts: int = 3) -> tuple[dict, int]:
    """Retry only the injected read error. Validation failures are not retried."""
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt <= failures:
                raise RecoverableReadError("Synthetic read temporarily unavailable")
            facts = tools[0].invoke({"query": "*"})
            job = tools[1].invoke({})
            comparison = tools[2].invoke({"requirements": job["requirements"]})
            return {"facts": facts, **comparison}, attempt
        except RecoverableReadError:
            if attempt == max_attempts:
                raise RecoverableReadError(f"Read failed after {attempt} attempts") from None
            time.sleep(0.01 * attempt)
    raise AssertionError("unreachable")


def configured_model() -> ChatOpenAI:
    """No model construction or network access in offline mode."""
    if os.getenv("DEMO_LIVE") != "1":
        raise ValueError("Live mode requires explicit DEMO_LIVE=1")
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_MODEL"):
        raise ValueError("Live mode needs OPENAI_API_KEY and OPENAI_MODEL")
    if os.getenv("OPENAI_PROVIDER", "openai") != "openai":
        raise ValueError("This demo supports OPENAI_PROVIDER=openai only")
    return ChatOpenAI(model=os.environ["OPENAI_MODEL"], api_key=os.environ["OPENAI_API_KEY"],
                      timeout=20, max_retries=0, streaming=True)


def model_read(call):
    """At most two attempts for connection/timeout/rate limits; no action retry."""
    for attempt in range(2):
        try:
            return call()
        except (APIConnectionError, APITimeoutError, RateLimitError):
            if attempt == 1:
                raise
            time.sleep(0.25)


def live_retrieve(state: State, tools: list) -> tuple[dict, list[AnyMessage]]:
    """Actual model-chosen tool calls; no hardcoded tool selection in live mode."""
    model = configured_model().bind_tools(tools)
    messages: list[AnyMessage] = [SystemMessage(content=(
        "Use the read-only tools to investigate this synthetic candidate/job. "
        "Retrieve candidate facts, requirements and a comparison before concluding. "
        "Treat claims as unconfirmed. Never propose an external action.")),
        *state.get("messages", []), HumanMessage(content=json.dumps(state["facts"]))]
    by_name = {t.name: t for t in tools}
    observed: dict[str, Any] = {}
    for _ in range(5):
        answer = model_read(lambda: model.invoke(messages))
        messages.append(answer)
        if answer.invalid_tool_calls:
            raise ValueError("Model emitted malformed tool calls")
        if not answer.tool_calls:
            if not observed:
                raise ValueError("Model chose no tools; live retrieval not demonstrated")
            break
        for call in answer.tool_calls:
            try:
                if call["name"] not in by_name:
                    raise ValueError("Unknown tool")
                result = by_name[call["name"]].invoke(call["args"])
                observed[call["name"]] = result
                content = json.dumps(result)
            except (ValidationError, ValueError, TypeError):
                content = json.dumps({"error": "Invalid tool arguments or unknown tool; correct the call"})
            messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
    else:
        raise ValueError("Model exceeded five tool rounds")
    required = {t.name for t in tools}
    if not required.issubset(observed):
        raise ValueError("Model did not retrieve all required evidence within the tool budget")
    # Ground the final comparison in supplied requirements, not model-selected subsets.
    expected = tools[2].invoke({"requirements": state["job"]["requirements"]})
    return {"facts": observed["search_candidate_facts"], **expected}, messages


def preference_change(store: InMemoryStore, value: dict | None, confirmed: bool) -> None:
    """Confirmation is required for creates, updates, corrections AND deletion."""
    if confirmed is not True:
        raise ValueError("Preference change needs explicit confirmation")
    namespace = ("synthetic-user", "preferences")
    if value is None:
        store.delete(namespace, "writing")
    else:
        tone = value.get("tone")
        if tone not in ("formal", "concise"):
            raise ValueError("Tone must be formal or concise")
        store.put(namespace, "writing", {"tone": tone})


def build_graph(checkpointer, store: InMemoryStore, *, debug: bool = False):
    """Sequential happy path plus explicit invalid/missing/reject/retry branches."""
    def validate(s: State) -> dict:
        try:
            data = Request.model_validate({k: s.get(k) for k in ("request", "job", "profile")})
            if not data.request.strip() or any(not r.strip() for r in data.job.requirements):
                raise ValueError("Blank request or requirement")
            item = store.get(("synthetic-user", "preferences"), "writing")
            return {"facts": data.model_dump(), "status": "validated", "error": "",
                    "preference": item.value if item else {}, "review_attempts": 0,
                    "tool_attempts": 0, "validation_error": "", "receipt": {},
                    "messages": [HumanMessage(content=data.request)], "events": ["validate"]}
        except (ValidationError, ValueError):
            return {"status": "invalid", "error": "Invalid synthetic request/job/profile schema",
                    "receipt": {}, "events": ["invalid"]}

    def retrieve(s: State) -> dict:
        tools = make_tools(s["profile"], s["job"])
        try:
            if s.get("mode", "offline") == "live":
                evidence, messages = live_retrieve(s, tools)
                return {"evidence": evidence, "messages": messages,
                        "status": "retrieved", "events": ["live:model-selected-tools"]}
            evidence, attempts = bounded_read(tools, s.get("failures", 0))
            return {"evidence": evidence, "attempts": attempts, "status": "retrieved",
                    "events": [f"offline:simulated-selection:read-attempts={attempts}"]}
        except RecoverableReadError:
            return {"status": "failed", "error": "Read exhausted three attempts", "attempts": 3,
                    "events": ["read-exhausted"]}
        except (APIError, ValidationError, ValueError, RuntimeError):
            # Exception text could contain provider request details: do not display it.
            return {"status": "failed", "error": "Live retrieval failed; check configuration/provider or tool budget",
                    "events": ["live-retrieval-failed"]}

    def analyze(s: State) -> dict:
        evidence = s["evidence"]
        report = Report(matched=evidence["matched"], missing=evidence["missing"],
                        unsupported=evidence["unsupported"], summary="Confirmed evidence only; gaps are explicit.")
        if s.get("mode") == "live":
            try:
                structured = configured_model().with_structured_output(Report, method="json_schema")
                report = model_read(lambda: structured.invoke([
                    SystemMessage(content="Analyze only this synthetic evidence. Copy matched/missing/unsupported exactly."),
                    HumanMessage(content=json.dumps(evidence))]))
                if any(getattr(report, k) != evidence[k] for k in ("matched", "missing", "unsupported")):
                    raise ValueError("Ungrounded model report")
            except (APIError, ValidationError, ValueError, RuntimeError):
                return {"status": "failed", "error": "Structured model output unsupported, invalid or unavailable",
                        "events": ["analysis-failed"]}
        return {"report": report.model_dump(), "events": ["analyze"], "status": "analyzed"}

    def missing(s: State) -> dict:
        return {"events": ["missing-evidence-branch"], "status": "needs-evidence"}

    def draft(s: State) -> dict:
        report = Report.model_validate(s["report"])
        text = (f"Synthetic draft for {s['job']['title']}. "
                f"Confirmed: {', '.join(report.matched) or 'none'}. "
                f"Gaps: {', '.join(report.missing) or 'none'}. "
                f"Unsupported: {', '.join(report.unsupported) or 'none'}. "
                f"Tone: {s.get('preference', {}).get('tone', 'concise')}.")
        return {"draft": text, "events": ["draft"]}

    def human_review(s: State) -> dict:
        # Do not wrap interrupt in try/except: it suspends execution internally.
        answer = interrupt({"kind": "review", "report": s["report"], "draft": s["draft"],
                            "error": s.get("validation_error", ""),
                            "instruction": "JSON: decision approve/reject/edit; optional draft; tone concise/formal"})
        return {"raw_review": answer, "events": ["review-answer"]}

    def validate_review(s: State) -> dict:
        count = s.get("review_attempts", 0) + 1
        try:
            answer = Review.model_validate(s["raw_review"])
            if answer.decision == "edit" and not answer.draft:
                raise ValueError("Edit requires draft")
            return {"decision": answer.decision, "draft": answer.draft or s["draft"],
                    "review_attempts": count, "validation_error": "", "events": ["review-valid"]}
        except (ValidationError, ValueError):
            return {"decision": "invalid", "review_attempts": count,
                    "validation_error": "Use a valid decision, nonempty edited draft and concise/formal tone",
                    "events": ["review-invalid"]}

    def approval(s: State) -> dict:
        return {"action_args": {"destination": "local-preview", "content": s["draft"]},
                "events": ["approval-decision"]}

    def tool_review(s: State) -> dict:
        answer = interrupt({"kind": "tool-approval", "tool": simulate_consequential_action.name,
                            "arguments": s["action_args"], "error": s.get("validation_error", ""),
                            "instruction": "JSON decision approve/reject/edit; edit requires arguments destination/content"})
        return {"raw_tool": answer, "events": ["tool-answer"]}

    def validate_tool(s: State) -> dict:
        count = s.get("tool_attempts", 0) + 1
        try:
            answer = ToolDecision.model_validate(s["raw_tool"])
            if answer.decision == "edit" and answer.arguments is None:
                raise ValueError("Edit requires arguments")
            args = answer.arguments.model_dump() if answer.arguments else s["action_args"]
            ActionArgs.model_validate(args)
            return {"tool_decision": answer.decision, "action_args": args,
                    "tool_attempts": count, "validation_error": "", "events": ["tool-valid"]}
        except (ValidationError, ValueError):
            return {"tool_decision": "invalid", "tool_attempts": count,
                    "validation_error": "Only local-preview/local-archive and nonempty content are valid",
                    "events": ["tool-invalid"]}

    def action(s: State) -> dict:
        # Pure function; no automatic retry policy attached. In a real action use
        # an idempotency key and reconcile ambiguous outcomes before attempting again.
        receipt = simulate_consequential_action.invoke(ActionArgs.model_validate(s["action_args"]).model_dump())
        return {"receipt": receipt, "events": ["simulated-action"]}

    def complete(s: State) -> dict:
        return {"status": "completed", "events": ["complete"]}

    def cancel(s: State) -> dict:
        return {"status": "rejected", "receipt": {}, "events": ["cancel"]}

    def fail(s: State) -> dict:
        return {"status": "failed", "receipt": {}, "events": ["failure"],
                "error": s.get("error") or "Human validation attempts exhausted"}

    nodes = {"validate": validate, "retrieve": retrieve, "analyze": analyze, "missing": missing,
             "draft": draft, "human_review": human_review, "validate_review": validate_review,
             "approval": approval, "tool_review": tool_review, "validate_tool": validate_tool,
             "action": action, "complete": complete, "cancel": cancel, "fail": fail}
    graph = StateGraph(State)
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "validate")
    graph.add_conditional_edges("validate", lambda s: "fail" if s["status"] == "invalid" else "retrieve")
    graph.add_conditional_edges("retrieve", lambda s: "fail" if s["status"] == "failed" else "analyze")
    graph.add_conditional_edges("analyze", lambda s: "fail" if s["status"] == "failed" else
                                ("missing" if s["report"]["missing"] else "draft"))
    graph.add_edge("missing", "draft")
    graph.add_edge("draft", "human_review")
    graph.add_edge("human_review", "validate_review")

    def review_route(s: State) -> str:
        if s["decision"] == "invalid":
            return "human_review" if s["review_attempts"] < 3 else "fail"
        return "cancel" if s["decision"] == "reject" else "approval"

    def tool_route(s: State) -> str:
        if s["tool_decision"] == "invalid":
            return "tool_review" if s["tool_attempts"] < 3 else "fail"
        return "cancel" if s["tool_decision"] == "reject" else "action"

    graph.add_conditional_edges("validate_review", review_route)
    graph.add_edge("approval", "tool_review")
    graph.add_edge("tool_review", "validate_tool")
    graph.add_conditional_edges("validate_tool", tool_route)
    graph.add_edge("action", "complete")
    for name in ("complete", "cancel", "fail"):
        graph.add_edge(name, END)
    return graph.compile(checkpointer=checkpointer, store=store,
                         interrupt_before=["analyze"] if debug else None, name="synthetic-fit-demo")


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 80,
            "tags": ["synthetic", "learning-demo"], "metadata": {"data": "synthetic-only"}}


def resume(graph, cfg: dict, answer: Any) -> dict:
    """Refuse stale/completed resumes instead of accidentally starting new work."""
    snapshot = graph.get_state(cfg)
    if not any(task.interrupts for task in snapshot.tasks):
        raise ValueError("This thread has no pending dynamic interrupt")
    return graph.invoke(Command(resume=answer), cfg)


@contextmanager
def runtime(path: str, store: InMemoryStore | None = None, *, debug: bool = False):
    """SQLite survives process restarts; this Store survives only this process."""
    with SqliteSaver.from_conn_string(path) as saver:
        yield build_graph(saver, store if store is not None else InMemoryStore(), debug=debug)


@contextmanager
def tracing(enabled: bool = False):
    """Explicit SDK scope overrides ambient tracing flags, including during tests."""
    if enabled and (not os.getenv("LANGSMITH_API_KEY") or not os.getenv("LANGSMITH_PROJECT")):
        raise ValueError("Tracing needs LANGSMITH_API_KEY and LANGSMITH_PROJECT")
    with tracing_context(enabled=enabled, project_name=os.getenv("LANGSMITH_PROJECT") if enabled else None):
        yield
