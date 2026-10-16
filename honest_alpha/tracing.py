"""Arize AX tracing. `setup` has to run before anything imports anthropic."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace

_ENABLED = {"on": False}


def setup() -> bool:
    space, key = os.environ.get("ARIZE_SPACE_ID"), os.environ.get("ARIZE_API_KEY")
    if not (space and key):
        return False
    from arize.otel import Transport, register
    from openinference.instrumentation.anthropic import AnthropicInstrumentor

    transport = Transport.HTTP if os.environ.get("ARIZE_TRANSPORT", "").lower() == "http" else Transport.GRPC
    provider = register(space_id=space, api_key=key, transport=transport, verbose=False,
                        project_name=os.environ.get("ARIZE_PROJECT_NAME", "honest-alpha"))
    AnthropicInstrumentor().instrument(tracer_provider=provider)
    _ENABLED["on"] = True
    return True


def enabled() -> bool:
    return _ENABLED["on"]


def current():
    """The active context, to hand to a worker thread so its spans nest under this one."""
    return otel_context.get_current()


def _value(v):
    return v if isinstance(v, (str, bool, int, float)) else json.dumps(v, default=str)[:8000]


@contextmanager
def span(name: str, kind: str, parent=None, **attrs):
    """An OpenInference span: kind is AGENT, CHAIN, TOOL, RETRIEVER, GUARDRAIL, EVALUATOR or LLM."""
    tracer = trace.get_tracer("honest_alpha")
    with tracer.start_as_current_span(name, context=parent) as s:
        s.set_attribute("openinference.span.kind", kind)
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, _value(v))
        yield s


def put(s, **attrs) -> None:
    for k, v in attrs.items():
        if v is not None:
            s.set_attribute(k, _value(v))


def flush() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
