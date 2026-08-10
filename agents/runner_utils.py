"""
Runner utilities to normalize different Runner.run return types.

Some ADK Runner implementations may return a coroutine, an async generator,
or a sync generator (streaming). This helper provides a single async
entrypoint that consumes any of those and returns the final Pydantic
structured output from the agent.

ADK Event Shape (as of google-adk >= 0.1):
  Each Event emitted by Runner.run() may carry:
    - event.actions.state_delta  -- session state mutations
    - event.content              -- a google.genai.types.Content object
        └── .parts[]             -- list of Part objects
            ├── .text            -- plain text string
            └── .function_response / .function_call
  When output_schema is set on the Agent, the LAST event whose author is
  the agent name will have a `.content.parts[0].text` that is valid JSON
  matching the schema.  Some ADK versions also expose a top-level
  `.structured_output` attribute on the final Event or on the runner
  result object.

Extraction priority:
  1. item.structured_output  (direct attribute, newest ADK style)
  2. item.response.structured_output  (wrapped result object)
  3. item.output  (some older ADK versions)
  4. Last non-empty content text part parsed as JSON (fallback)
  5. The raw item itself (last resort)
"""
import inspect
import json
import logging
import asyncio
from typing import Any

from google.genai import types

logger = logging.getLogger("RunnerUtils")


def make_new_message(text: str, role: str = "user") -> types.Content:
    """Constructs a Google GenAI Content object for Runner.run."""
    return types.Content(parts=[types.Part.from_text(text=text)], role=role)


def _try_get_text_from_event(item: Any) -> str | None:
    """Attempt to extract plain text from an ADK Event's content parts."""
    try:
        content = getattr(item, "content", None)
        if content is None:
            return None
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text and isinstance(text, str) and text.strip():
                return text.strip()
    except Exception:
        pass
    return None


def _extract_response_value(item: Any) -> Any:
    """Traverse all known ADK Event/Response shapes to surface the structured Pydantic output."""
    if item is None:
        return None

    # Priority 1: direct .structured_output attribute (newest ADK)
    structured = getattr(item, "structured_output", None)
    if structured is not None:
        return structured

    # Priority 2: nested .response.structured_output
    response_obj = getattr(item, "response", None)
    if response_obj is not None:
        structured = getattr(response_obj, "structured_output", None)
        if structured is not None:
            return structured

    # Priority 3: .output attribute (older ADK)
    output = getattr(item, "output", None)
    if output is not None:
        return output

    # Priority 4: parse JSON text from the event's content parts
    text = _try_get_text_from_event(item)
    if text:
        # Strip markdown code fences if the model wrapped the JSON
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()
        try:
            parsed = json.loads(text)
            return parsed  # caller will validate against the expected schema
        except (json.JSONDecodeError, ValueError):
            pass

    # Priority 5: return as-is (last resort — will likely fail schema validation upstream)
    return item


async def run_runner_and_get_response(maybe_awaitable) -> Any:
    """Consume a coroutine, async generator, or sync generator and return the
    final meaningful structured output from the ADK agent.

    Iterates all emitted Events and keeps the last one that carries a
    meaningful structured_output or JSON content part, so that intermediate
    tool-call events do not shadow the final answer.
    """
    def _is_meaningful(value: Any) -> bool:
        """Returns True if value is a real result (not a raw Event leaking through)."""
        if value is None:
            return False
        # If it's still an Event-like object (has .content but not .structured_output
        # resolved) keep searching; otherwise treat it as meaningful.
        type_name = type(value).__name__
        if type_name == "Event":
            return False
        return True

    # Async generator (async for ...)
    if inspect.isasyncgen(maybe_awaitable):
        last_item = None
        best_response = None
        async for item in maybe_awaitable:
            if inspect.isawaitable(item) or inspect.iscoroutine(item):
                item = await item
            last_item = item
            candidate = _extract_response_value(item)
            if _is_meaningful(candidate):
                best_response = candidate
        return best_response if best_response is not None else _extract_response_value(last_item)

    # Sync generator
    if inspect.isgenerator(maybe_awaitable):
        last_item = None
        best_response = None
        for item in maybe_awaitable:
            if inspect.isawaitable(item) or inspect.iscoroutine(item):
                loop = asyncio.get_running_loop()
                item = await asyncio.ensure_future(item, loop=loop)
            last_item = item
            candidate = _extract_response_value(item)
            if _is_meaningful(candidate):
                best_response = candidate
        return best_response if best_response is not None else _extract_response_value(last_item)

    # Awaitables (coroutines, futures, or objects implementing __await__)
    if inspect.isawaitable(maybe_awaitable):
        result = await maybe_awaitable
        return _extract_response_value(result)

    # Plain value
    return _extract_response_value(maybe_awaitable)
