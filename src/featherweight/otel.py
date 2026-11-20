"""OTelObserver: featherweight's spans, in the OpenTelemetry GenAI semantic conventions.

    from featherweight import AgentRunner, OTelObserver

    runner = AgentRunner(db, observers=[OTelObserver()])

It needs only `opentelemetry-api` (`pip install 'featherweight[otel]'`); with no SDK configured the API does nothing, so it costs
nothing. The application configures the SDK and the exporter, as for any other code.

A job is an `invoke_workflow` span, an attempt of an agent an `invoke_agent` span, a model call a `chat` span and a tool call an
`execute_tool` span. Metadata is always on the spans; content (messages, arguments, results) only when `capture_content` is on.
Our own attributes are in the `ftw.` namespace. The conventions are in the Development status and live in their own repository, so
the version we follow is pinned in `GENAI_CONVENTIONS`, and every attribute we emit is in `ATTRIBUTES`.
"""

import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

try:
    from opentelemetry import propagate, trace
    from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
except ImportError as exc:  # pragma: no cover - the extra is not installed
    raise ImportError("OTelObserver needs OpenTelemetry: pip install 'featherweight[otel]'") from exc

from featherweight import __version__
from featherweight.models import RunStatus
from featherweight.observers import (
    AttemptEnd,
    AttemptStart,
    JobEnd,
    JobStart,
    JobSubmitted,
    ModelCallEnd,
    ModelCallStart,
    Observer,
    ReusedRun,
    Scope,
    ToolCallEnd,
    ToolCallStart,
)

logger = logging.getLogger("featherweight")

# The version of the conventions that the attribute names below follow: the repository and the commit that was read, and the
# version of the core semantic conventions that it refers to. A change of either is a change of this line.
GENAI_CONVENTIONS = "semantic-conventions-genai@622b2b6073b15e5ed886c3c12a65a892b9cd5c70"
CORE_CONVENTIONS = "1.45.0"

# Every attribute name a span of ours can have. A test checks what is emitted against it, so a rename shows up here first.
ATTRIBUTES: frozenset[str] = frozenset(
    {
        # the GenAI conventions
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.agent.name",
        "gen_ai.agent.version",
        "gen_ai.agent.description",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.tool.type",
        "error.type",
        # the GenAI conventions, content (opt-in)
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        # ours
        "ftw.job.id",
        "ftw.job.name",
        "ftw.job.version",
        "ftw.job.hash",
        "ftw.job.execution",
        "ftw.agent.hash",
        "ftw.run.id",
        "ftw.invocation.id",
        "ftw.attempt",
        "ftw.attempts",
        "ftw.attempt.final",
        "ftw.tokens.input",
        "ftw.tokens.output",
        "ftw.reused",
        "ftw.reused_from_agent_run_id",
        "ftw.resumed_from",
    }
)

CAPTURE_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT"

# Our provider names, in the conventions' well-known values. Any other provider is passed on as it is.
_PROVIDERS = {"google": "gcp.gen_ai", "anthropic": "anthropic", "openai": "openai"}
# LangChain's message types, in the conventions' roles.
_ROLES = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


def _capture_content_from_env() -> bool:
    """The conventions' own switch: `SPAN_ONLY` puts content on spans. The values that send content as events are not supported."""
    value = os.environ.get(CAPTURE_CONTENT_ENV, "NO_CONTENT").strip().upper()
    if value == "SPAN_ONLY":
        return True
    if value in {"EVENT_ONLY", "SPAN_AND_EVENT"}:
        logger.warning(
            "otel_content_unsupported value=%s: content is not captured (only SPAN_ONLY is supported)", value
        )
    return False


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _attributes(**values: Any) -> dict[str, Any]:
    """The attributes that have a value. Names come with `__` in place of `.` (`gen_ai__operation__name`)."""
    return {name.replace("__", "."): value for name, value in values.items() if value is not None}


def _text(content: Any) -> str:
    """A message's content as text: a string as it is, and a list of blocks as the text of its text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "") if isinstance(b, dict) and b.get("type", "text") == "text" else _json(b) for b in content
        ]
        return "".join(t if isinstance(t, str) else _json(t) for t in texts)
    return "" if content is None else _json(content)


def _object(value: Any) -> Any:
    """What the conventions call an object (tool arguments and results): a mapping as it is, and anything else wrapped."""
    return value if isinstance(value, dict) else {"result": value}


def _messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Our message dicts as the conventions' system instructions and input messages."""
    system: list[dict[str, Any]] = []
    chat: list[dict[str, Any]] = []
    for message in messages:
        role = _ROLES.get(str(message.get("role")), str(message.get("role")))
        text = _text(message.get("content"))
        if role == "system":
            system.append({"type": "text", "content": text})
        elif role == "assistant":
            parts: list[dict[str, Any]] = [{"type": "text", "content": text}] if text else []
            parts += [
                {"type": "tool_call", "id": call.get("id"), "name": call.get("name", ""), "arguments": call.get("args")}
                for call in message.get("tool_calls", [])
            ]
            chat.append({"role": role, "parts": parts})
        elif role == "tool":
            chat.append({"role": role, "parts": [{"type": "tool_call_response", "response": text}]})
        else:
            chat.append({"role": role, "parts": [{"type": "text", "content": text}]})
    return system or None, chat


def _single(role: str, value: Any) -> str:
    """One message, whose text is the JSON of a typed input or output: how a job or an agent's input and output are written."""
    return _json([{"role": role, "parts": [{"type": "text", "content": _json(value)}]}])


class _SpanScope(Scope):
    def __init__(self, observer: "OTelObserver", span: Span):
        self._observer = observer
        self._span = span

    async def finish(self, end: JobEnd | AttemptEnd) -> None:
        self._observer._finish(self._span, end)


class OTelObserver(Observer):
    """Emits OpenTelemetry spans for jobs, attempts, model calls and tool calls, as the GenAI conventions describe them.

    `capture_content` puts messages, tool arguments and results, and a job's or an agent's input and output on the spans. When it
    is not given, it follows `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT` (`SPAN_ONLY` is on; anything else, off). Pass a
    `tracer_provider` to use another one than the global one.
    """

    def __init__(self, *, capture_content: bool | None = None, tracer_provider: TracerProvider | None = None):
        wants = _capture_content_from_env() if capture_content is None else capture_content
        object.__setattr__(self, "capture_content", wants)  # this observer decides, and not its class
        self._tracer = trace.get_tracer("featherweight", __version__, tracer_provider=tracer_provider)
        self._attempts: dict[str, Span] = {}  # agent_run_id -> its span
        self._tools: dict[str, tuple[str, Span]] = {}  # call id -> the attempt, and the span
        self._models: dict[str, tuple[str, Span]] = {}

    # ---------------------------------------------------------------- scopes

    @contextlib.asynccontextmanager
    async def _scope(self, span: Span, attempt: str | None = None) -> AsyncGenerator[Scope]:
        try:
            # The span is the current one while the work runs, so a span of the user's, started in it, is its child.
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                yield _SpanScope(self, span)
        finally:
            if attempt is not None:
                self._attempts.pop(attempt, None)
                self._end_open(attempt)
            span.end()

    def job(self, start: JobStart) -> AbstractAsyncContextManager[Scope]:
        parent = propagate.extract(dict(start.trace_context)) if start.trace_context else None  # the submitter's trace
        content = _single("user", start.input) if self.capture_content and start.input is not None else None
        span = self._tracer.start_span(
            f"invoke_workflow {start.name}",
            context=parent,
            kind=SpanKind.INTERNAL,
            attributes=_attributes(
                gen_ai__operation__name="invoke_workflow",
                gen_ai__agent__name=start.name,
                gen_ai__input__messages=content,
                ftw__job__id=start.job_id,
                ftw__job__name=start.name,
                ftw__job__version=start.version,
                ftw__job__hash=start.job_hash,
                ftw__job__execution=start.execution,
                ftw__resumed_from=start.resumed_from,
            ),
        )
        return self._scope(span)

    def attempt(self, start: AttemptStart) -> AbstractAsyncContextManager[Scope]:
        # The parent is the tool call that launched this agent, or else the attempt that did: a launched agent runs in another
        # task, so the current span is not enough. With neither, it is a child of whatever span is current (the job, or yours).
        launched_by = self._tools.get(start.parent_tool_call_id or "")
        parent = launched_by[1] if launched_by else self._attempts.get(start.parent_agent_run_id or "")
        content = self.capture_content
        span = self._tracer.start_span(
            f"invoke_agent {start.agent_name}",
            context=trace.set_span_in_context(parent) if parent is not None else None,
            kind=SpanKind.INTERNAL,
            attributes=_attributes(
                gen_ai__operation__name="invoke_agent",
                gen_ai__agent__name=start.agent_name,
                gen_ai__agent__version=start.agent_version,
                gen_ai__agent__description=start.agent_description or None,
                gen_ai__request__model=start.model,
                gen_ai__input__messages=_single("user", start.input) if content and start.input is not None else None,
                gen_ai__system_instructions=(
                    _json([{"type": "text", "content": start.system_prompt}])
                    if content and start.system_prompt is not None
                    else None
                ),
                ftw__job__id=start.job_id,
                ftw__agent__hash=start.agent_hash,
                ftw__run__id=start.agent_run_id,
                ftw__invocation__id=start.invocation_id,
                ftw__attempt=start.attempt,
                ftw__attempts=start.max_attempts,
            ),
        )
        self._attempts[start.agent_run_id] = span
        return self._scope(span, attempt=start.agent_run_id)

    def _finish(self, span: Span, end: JobEnd | AttemptEnd) -> None:
        if isinstance(end, AttemptEnd):
            span.set_attributes(
                _attributes(
                    ftw__attempt__final=end.final,
                    ftw__tokens__input=end.input_tokens,
                    ftw__tokens__output=end.output_tokens,
                    gen_ai__response__model=end.reported_model,
                )
            )
        if self.capture_content and end.output is not None:
            span.set_attribute("gen_ai.output.messages", _single("assistant", end.output))
        if end.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            self._error(span, end.error_type or ("CancelledError" if end.status == RunStatus.CANCELLED else "_OTHER"))

    @staticmethod
    def _error(span: Span, error_type: str) -> None:
        """The span failed: its status, and the type of the error. Never the text, which can quote data."""
        span.set_attribute("error.type", error_type)
        span.set_status(Status(StatusCode.ERROR))

    # ---------------------------------------------------------------- events

    async def reused(self, run: ReusedRun) -> None:
        span = self._tracer.start_span(
            f"invoke_agent {run.agent_name}",
            kind=SpanKind.INTERNAL,
            attributes=_attributes(
                gen_ai__operation__name="invoke_agent",
                gen_ai__agent__name=run.agent_name,
                gen_ai__agent__version=run.agent_version,
                gen_ai__output__messages=(
                    _single("assistant", run.output) if self.capture_content and run.output is not None else None
                ),
                ftw__job__id=run.job_id,
                ftw__agent__hash=run.agent_hash,
                ftw__run__id=run.agent_run_id,
                ftw__invocation__id=run.invocation_id,
                ftw__reused=True,
                ftw__reused_from_agent_run_id=run.reused_from_agent_run_id,
                ftw__resumed_from=run.resumed_from,
            ),
        )
        span.end()

    def _child(self, name: str, kind: SpanKind, agent_run_id: str, attributes: dict[str, Any]) -> Span:
        parent = self._attempts.get(agent_run_id)
        return self._tracer.start_span(
            name,
            context=trace.set_span_in_context(parent) if parent is not None else None,
            kind=kind,
            attributes=attributes,
        )

    async def model_call_started(self, call: ModelCallStart) -> None:
        system, chat = _messages(call.messages) if call.messages is not None else (None, [])
        span = self._child(
            f"chat {call.model}",
            SpanKind.CLIENT,
            call.agent_run_id,
            _attributes(
                gen_ai__operation__name="chat",
                gen_ai__provider__name=_PROVIDERS.get(call.provider, call.provider),
                gen_ai__request__model=call.model,
                gen_ai__system_instructions=_json(system) if system else None,
                gen_ai__input__messages=_json(chat) if call.messages is not None else None,
            ),
        )
        self._models[call.call_id] = (call.agent_run_id, span)

    async def model_call_finished(self, call: ModelCallEnd) -> None:
        _, span = self._models.pop(call.call_id, (None, None))
        if span is None:
            return
        output = None
        if call.output is not None:
            _, parts = _messages([call.output])
            output = parts[0] if parts else None
            if output is not None and call.finish_reasons:
                output = {**output, "finish_reason": call.finish_reasons[0]}
        span.set_attributes(
            _attributes(
                gen_ai__response__model=call.response_model,
                gen_ai__response__finish_reasons=call.finish_reasons or None,
                gen_ai__usage__input_tokens=call.input_tokens,
                gen_ai__usage__output_tokens=call.output_tokens,
                gen_ai__output__messages=_json([output]) if output is not None else None,
            )
        )
        if call.error_type is not None:
            self._error(span, call.error_type)
        span.end()

    async def tool_call_started(self, call: ToolCallStart) -> None:
        span = self._child(
            f"execute_tool {call.tool_name}",
            SpanKind.INTERNAL,
            call.agent_run_id,
            _attributes(
                gen_ai__operation__name="execute_tool",
                gen_ai__tool__name=call.tool_name,
                gen_ai__tool__call__id=call.call_id,
                gen_ai__tool__type="function",
                gen_ai__tool__call__arguments=_json(_object(call.arguments)) if call.arguments is not None else None,
            ),
        )
        self._tools[call.call_id] = (call.agent_run_id, span)

    async def tool_call_finished(self, call: ToolCallEnd) -> None:
        _, span = self._tools.pop(call.call_id, (None, None))
        if span is None:
            return
        if call.result is not None:
            span.set_attribute("gen_ai.tool.call.result", _json(_object(call.result)))
        if call.error_type is not None:
            self._error(span, call.error_type)
        span.end()

    def _end_open(self, agent_run_id: str) -> None:
        """End the model and tool spans of an attempt that is over without their end (a cancelled attempt)."""
        for spans in (self._models, self._tools):
            for call_id in [c for c, (run, _) in spans.items() if run == agent_run_id]:
                spans.pop(call_id)[1].end()

    async def submitted(self, job: JobSubmitted) -> Mapping[str, str] | None:
        """The W3C trace context of the current span, for the message: the worker's job span joins this trace."""
        carrier: dict[str, str] = {}
        entry = self._tools.get(job.parent_tool_call_id) if job.parent_tool_call_id else None
        if entry is not None:  # a background agent: its job goes under the tool call that started it
            propagate.inject(carrier, context=trace.set_span_in_context(entry[1]))
        else:
            propagate.inject(carrier)
        return carrier or None
