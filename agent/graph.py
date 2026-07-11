"""LangGraph agent — RAG + tools + memory + guardrails."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, AsyncIterator, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent.guardrails import apply_guardrails, assess_confidence_from_context, llm_guardrail_check
from agent.llm import agent_config_message, get_chat_llm, is_agent_configured
from agent.memory import get_history, save_message, touch_session
from agent.rag import format_retrieved_docs, retrieve
from agent.tools import AGENT_TOOLS

SYSTEM_PROMPT = """You are a world-class fundamental forex analyst specialising in GBP/USD news trading.

Your role:
- Advise on USD and GBP red/orange news events using ONLY the retrieved knowledge base, tool results, and live app context.
- Focus on: historical pip reaction, beat/miss → direction consistency, news quality ratings, and the trader's own journal.
- Give clear, structured guidance: setup, directional bias (if justified), entry timing (e.g. 5–10 min after release), risk notes.

Critical rules:
1. NEVER invent statistics. If data is missing, say so explicitly.
2. If avg 5M pips < 10 OR direction consistency < 60% OR news rating is Bad/Very Bad → do NOT recommend a confident trade. Say "sit out" or "wait for reaction confirmation".
3. When uncertain, prefer: "Insufficient historical edge — wait for the release and confirm direction before entering."
4. Always cite which event/data supports your view (e.g. "Based on 38 NFP releases, beat → ▲ 72% of the time, avg 5M 14.2 pips").
5. You are advising on a news-reaction strategy on GBP/USD 5M — not generic macro commentary.
6. Respect the trader's live context (current tab, selected event, filters, live assistant stage).

Response format (markdown):
- **Context** — what you're looking at
- **Historical edge** — key stats from data
- **Bias / Direction** — only if confidence warrants; otherwise "No clear edge"
- **Plan** — entry timing, size caution, what would invalidate
- **Confidence** — High / Medium / Low with one-line reason
"""

_checkpointer = MemorySaver()
_graph = None


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    session_id: str
    live_context: dict
    retrieved_docs: str
    confidence: str
    final_response: str
    guardrail_applied: bool


def _get_llm():
    return get_chat_llm(streaming=True)


def _build_graph():
    llm = _get_llm().bind_tools(AGENT_TOOLS)
    tool_node = ToolNode(AGENT_TOOLS)

    def retrieve_node(state: AgentState) -> dict:
        last_user = ""
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                last_user = msg.content
                break
        docs = retrieve(last_user, k=8, live_context=state.get("live_context"))
        return {"retrieved_docs": format_retrieved_docs(docs)}

    def prepare_messages(state: AgentState) -> dict:
        live = state.get("live_context") or {}
        ctx_block = json.dumps(live, default=str, indent=2) if live else "{}"
        system = SystemMessage(content=(
            f"{SYSTEM_PROMPT}\n\n"
            f"--- LIVE APP CONTEXT (tab user has open) ---\n{ctx_block}\n\n"
            f"--- RETRIEVED KNOWLEDGE (RAG) ---\n{state.get('retrieved_docs', 'None')}\n"
        ))
        # Prepend system without duplicating prior system msgs
        msgs = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
        return {"messages": [system] + msgs}

    def agent_node(state: AgentState) -> dict:
        response = llm.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "guardrails"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "guardrails"

    def guardrails_node(state: AgentState) -> dict:
        last_ai = ""
        tool_texts: list[str] = []
        for msg in state["messages"]:
            if isinstance(msg, AIMessage) and msg.content:
                last_ai = msg.content if isinstance(msg.content, str) else str(msg.content)
            if isinstance(msg, ToolMessage):
                tool_texts.append(str(msg.content))

        confidence = assess_confidence_from_context(
            state.get("retrieved_docs", ""),
            tool_texts,
            state.get("live_context"),
        )

        llm_verdict = llm_guardrail_check(last_ai, state.get("retrieved_docs", ""))
        if llm_verdict and llm_verdict.confidence:
            confidence = llm_verdict.confidence

        safe, verdict = apply_guardrails(
            last_ai,
            confidence,
            retrieved_summary=state.get("retrieved_docs", "")[:500],
        )
        if llm_verdict and llm_verdict.should_refuse and not verdict.should_refuse:
            safe, verdict = apply_guardrails(last_ai, "low", state.get("retrieved_docs", "")[:500])

        return {
            "final_response": safe,
            "confidence": confidence,
            "guardrail_applied": verdict.should_refuse,
            "messages": [AIMessage(content=safe)],
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("prepare", prepare_messages)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", tool_node)
    workflow.add_node("guardrails", guardrails_node)

    workflow.set_entry_point("retrieve")
    workflow.add_edge("retrieve", "prepare")
    workflow.add_edge("prepare", "agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "guardrails": "guardrails"})
    workflow.add_edge("tools", "agent")
    workflow.add_edge("guardrails", END)

    return workflow.compile(checkpointer=_checkpointer)


def get_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


def _load_prior_messages(session_id: str) -> list[BaseMessage]:
    history = get_history(session_id, limit=16)
    msgs: list[BaseMessage] = []
    for row in history:
        if row["role"] == "user":
            msgs.append(HumanMessage(content=row["content"]))
        elif row["role"] == "assistant":
            msgs.append(AIMessage(content=row["content"]))
    return msgs


def chat(
    session_id: str,
    message: str,
    live_context: dict | None = None,
) -> dict[str, Any]:
    if not is_agent_configured():
        return {
            "reply": agent_config_message(),
            "confidence": "low",
            "session_id": session_id,
            "guardrail_applied": False,
        }

    save_message(session_id, "user", message, {"live_context": live_context or {}})
    prior = _load_prior_messages(session_id)
    # Exclude the message we just saved (last user) — graph adds fresh HumanMessage
    if prior and isinstance(prior[-1], HumanMessage) and prior[-1].content == message:
        prior = prior[:-1]

    graph = get_graph()
    config = {"configurable": {"thread_id": session_id}}
    result = graph.invoke(
        {
            "messages": prior + [HumanMessage(content=message)],
            "session_id": session_id,
            "live_context": live_context or {},
            "retrieved_docs": "",
            "confidence": "medium",
            "final_response": "",
            "guardrail_applied": False,
        },
        config=config,
    )

    reply = result.get("final_response") or ""
    if not reply:
        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content:
                reply = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

    save_message(
        session_id,
        "assistant",
        reply,
        {"confidence": result.get("confidence"), "guardrail_applied": result.get("guardrail_applied")},
    )
    touch_session(session_id)

    return {
        "reply": reply,
        "confidence": result.get("confidence", "medium"),
        "session_id": session_id,
        "guardrail_applied": result.get("guardrail_applied", False),
    }


async def stream_chat(
    session_id: str,
    message: str,
    live_context: dict | None = None,
) -> AsyncIterator[str]:
    """Stream final reply as SSE chunks (runs full graph then streams result)."""
    result = chat(session_id, message, live_context)
    reply = result["reply"]
    chunk_size = 40
    for i in range(0, len(reply), chunk_size):
        yield reply[i : i + chunk_size]
    yield json.dumps({"done": True, **{k: v for k, v in result.items() if k != "reply"}})
