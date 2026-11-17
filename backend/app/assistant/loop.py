"""Assistant: a plain tool-calling loop. Tools are discovered over MCP; the LLM never touches the DB or provider directly."""

import json
import logging
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import get_settings
from app.errors import AppError
from app.schemas import AssistantOut, ToolCallOut

log = logging.getLogger("assistant")

SYSTEM = """You are a read-only travel-planning assistant inside a trip planner. Today's trip id is {trip_id}.
Rules:
- Ground every claim in tool results. Never invent prices, availability, dates, offers or bookings. Prices are indicative totals for the whole party in USD; a search result is not a booking.
- Start by reading the trip with get_trip. Use search_offers for options; use list_bookings when booking state matters.
- Relate recommendations to the trip's dates, budget (remaining vs total), traveler count and the itinerary issues you see; name gaps and conflicts explicitly.
- If a tool fails, returns partial results, or nothing suitable exists, say so plainly. Do not present failures as success.
- You cannot change the itinerary or make bookings. You may suggest changes; the user performs them in the app.
- Be concise."""


class LLM(Protocol):
    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Returns Anthropic-style content blocks: {'type':'text','text'} and {'type':'tool_use','id','name','input'}."""
        ...


class AnthropicLLM:
    def __init__(self) -> None:
        from anthropic import AsyncAnthropic

        s = get_settings()
        self._client = AsyncAnthropic(api_key=s.anthropic_api_key, timeout=s.assistant_timeout_s)
        self._model = s.anthropic_model

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        r = await self._client.messages.create(model=self._model, max_tokens=1500, system=system, messages=messages, tools=tools)  # type: ignore[arg-type]
        return [b.model_dump() for b in r.content]


_llm: LLM | None = None


def set_llm(llm: LLM | None) -> None:
    global _llm
    _llm = llm


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        if not get_settings().anthropic_api_key:
            raise AppError(503, "ASSISTANT_NOT_CONFIGURED", "The assistant needs ANTHROPIC_API_KEY. The rest of the app works without it.")
        _llm = AnthropicLLM()
    return _llm


def _tool_text(result: Any) -> str:
    parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    return "\n".join(parts)[:20000] or "(empty result)"


async def run_assistant(token: str, trip_id: str, message: str, history: list[dict[str, str]], llm: LLM | None = None, mcp_url: str | None = None) -> AssistantOut:
    s = get_settings()
    llm = llm or get_llm()
    calls: list[ToolCallOut] = []
    msgs: list[dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history if h.get("role") in ("user", "assistant") and h.get("content")]
    msgs.append({"role": "user", "content": message})
    try:
        async with streamablehttp_client(mcp_url or s.mcp_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in listed.tools]
                for _step in range(s.assistant_max_steps):
                    blocks = await llm.complete(SYSTEM.format(trip_id=trip_id), msgs, tools)
                    uses = [b for b in blocks if b["type"] == "tool_use"]
                    if not uses:
                        return AssistantOut(answer="\n".join(b["text"] for b in blocks if b["type"] == "text").strip(), tool_calls=calls)
                    msgs.append({"role": "assistant", "content": blocks})
                    results = []
                    for u in uses:
                        try:
                            res = await session.call_tool(u["name"], u["input"])
                            text, ok = _tool_text(res), not res.isError
                        except Exception as e:  # transport/protocol failure for this call
                            text, ok = f"Tool call failed: {e}", False
                        calls.append(ToolCallOut(name=u["name"], arguments=u["input"], ok=ok, error=None if ok else text[:300]))
                        results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text, "is_error": not ok})
                    msgs.append({"role": "user", "content": results})
                return AssistantOut(answer="I could not finish within the allowed number of tool steps; here is what I gathered so far was not conclusive. Please narrow the question.", tool_calls=calls, truncated=True)
    except AppError:
        raise
    except Exception as e:
        log.exception("assistant failed")
        raise AppError(502, "ASSISTANT_FAILED", f"The assistant could not complete ({type(e).__name__}). Your trip data is unaffected.") from e


__all__ = ["run_assistant", "set_llm", "get_llm", "LLM", "json"]
