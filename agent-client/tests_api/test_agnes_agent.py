"""Tests for the single-agent graph (utils/agnes_agent.py).

No Azure OpenAI and no MCP server: a fake chat model returns scripted
messages, and plain LangChain tools stand in for the MCP ones.

These cover the two things the resource-only iteration depends on and
that nothing else checks -- that the graph really is agent + tools with
no routing, and that ONLY the resource tool group reaches the model.

Run:
    cd agent-client
    uv run --with pytest --with httpx pytest tests_api/ -q
"""

import asyncio
from typing import Any, Optional

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from ease_clients.utils import agnes_agent
from ease_clients.utils.agnes_agent import (
    RESOURCE_TOOL_NAMES,
    _compact_content,
    _trim_messages,
    _NODE_PHASES,
    build_graph,
)


# -- Fakes --

class FakeChatModel(BaseChatModel):
    """Returns scripted AIMessages and records what it was bound with.

    Implements _generate rather than streaming, because the graph node
    calls ainvoke -- BaseChatModel runs the sync path in a thread.
    """

    responses: list = []
    bound_tools: list = []
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        # record the tool objects the graph chose to expose, then return
        # self so the node's llm_with_tools is still this same instance
        self.bound_tools = list(tools)
        return self

    def _generate(
        self,
        messages: list,
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        message = self.responses[len(self.calls) - 1]
        return ChatResult(generations=[ChatGeneration(message=message)])


def make_model(*responses) -> FakeChatModel:
    model = FakeChatModel()
    model.responses = list(responses)
    model.bound_tools = []
    model.calls = []
    return model


@tool
def filter_dataset(dataset: str) -> str:
    """Filter a dataset (stand-in for the MCP tool)."""
    return "one row"


@tool
def count_by_column(dataset: str) -> str:
    """Count by column (stand-in for the MCP tool)."""
    return "one count"


@tool
def search_docs(query: str) -> str:
    """Knowledgebase tool -- must never be bound in this iteration."""
    return "a document page"


@tool
def get_quality_criteria() -> str:
    """Quality tool -- must never be bound in this iteration."""
    return "21 criteria"


ALL_TOOLS = [filter_dataset, count_by_column, search_docs, get_quality_criteria]


def compile_agent(model, tools=ALL_TOOLS):
    return build_graph(model, tools).compile(checkpointer=MemorySaver())


def config(thread="t1"):
    return {"configurable": {"thread_id": thread}, "recursion_limit": 50}


# -- Graph shape --

def test_graph_has_exactly_two_nodes():
    # the whole point of the iteration: no router, no handoff, no
    # second specialist. A new node appearing here is a regression.
    graph = build_graph(make_model(AIMessage(content="hi")), ALL_TOOLS)
    nodes = set(graph.compile().get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"resource_agent", "resource_tools"}


def test_node_phases_match_the_graph_nodes():
    # the API turns node starts into SSE phase events through this map,
    # so a node name that is not a key streams no phase to the client
    assert set(_NODE_PHASES) == {"resource_agent", "resource_tools"}


def test_only_resource_tools_are_bound():
    # the MCP server still serves all 12 tools; binding is what keeps
    # this agent resource-only, not the prompt asking nicely
    model = make_model(AIMessage(content="hi"))
    build_graph(model, ALL_TOOLS)
    assert {t.name for t in model.bound_tools} == {"filter_dataset", "count_by_column"}


def test_knowledgebase_and_quality_tools_are_never_bound():
    model = make_model(AIMessage(content="hi"))
    build_graph(model, ALL_TOOLS)
    bound = {t.name for t in model.bound_tools}
    assert "search_docs" not in bound
    assert "get_quality_criteria" not in bound


def test_no_handoff_tool_is_bound():
    # there is nowhere to hand off to; a bound handoff tool would let
    # the model call into an edge the graph no longer has
    model = make_model(AIMessage(content="hi"))
    build_graph(model, ALL_TOOLS)
    assert not [t for t in model.bound_tools if "hand_off" in t.name]


def test_resource_tool_names_are_the_eight_resource_group_tools():
    assert RESOURCE_TOOL_NAMES == {
        "list_datasets",
        "search_dataset",
        "filter_dataset",
        "filter_dataset_fuzzy",
        "count_by_column",
        "get_column_values",
        "get_request_attributes",
        "raise_entitlement_request",
    }


# -- The tool loop --

def test_plain_answer_ends_the_turn():
    model = make_model(AIMessage(content="Nothing to look up."))
    agent = compile_agent(model)
    state = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, config()))
    assert state["messages"][-1].content == "Nothing to look up."
    assert len(model.calls) == 1          # one LLM call, no tool round-trip


def test_tool_call_runs_the_tool_and_comes_back_to_the_agent():
    model = make_model(
        AIMessage(content="", tool_calls=[
            {"name": "filter_dataset", "args": {"dataset": "Entitlements"}, "id": "c1"},
        ]),
        AIMessage(content="The row is: one row"),
    )
    agent = compile_agent(model)
    state = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="find")]}, config()))

    types = [m.__class__.__name__ for m in state["messages"]]
    assert types == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert state["messages"][2].content == "one row"
    assert state["messages"][-1].content == "The row is: one row"
    # the tool result was fed back: the second LLM call saw it
    assert len(model.calls) == 2
    assert any(isinstance(m, ToolMessage) for m in model.calls[1])


def test_system_prompt_leads_every_llm_call():
    model = make_model(AIMessage(content="ok"))
    agent = compile_agent(model)
    asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, config()))
    first = model.calls[0][0]
    assert first.__class__.__name__ == "SystemMessage"
    assert first.content.startswith("You are the Resource specialist")


def test_history_accumulates_across_turns_in_memory():
    model = make_model(AIMessage(content="one"), AIMessage(content="two"))
    agent = compile_agent(model)
    cfg = config("same-thread")
    asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="first")]}, cfg))
    state = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="second")]}, cfg))
    assert [m.content for m in state["messages"]] == ["first", "one", "second", "two"]


def test_sessions_are_isolated_by_thread_id():
    model = make_model(AIMessage(content="a"), AIMessage(content="b"))
    agent = compile_agent(model)
    asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="first")]}, config("t1")))
    state = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="second")]}, config("t2")))
    assert len(state["messages"]) == 2      # t2 never saw t1's turn


# -- Prompt scope --

def test_prompt_has_no_handoff_or_routing_language():
    # the handoff passages promised a specialist that no longer exists
    prompt = agnes_agent.RESOURCE_PROMPT
    assert "hand_off_to_router" not in prompt
    assert "WHEN TO HAND OFF" not in prompt
    assert "route it to the right specialist" not in prompt


def test_prompt_tells_the_model_to_refuse_process_questions():
    prompt = agnes_agent.RESOURCE_PROMPT
    assert "=== SCOPE ===" in prompt
    assert "ONLY agent in this deployment" in prompt
    assert "Do NOT answer them from general knowledge" in prompt


# -- Message helpers --

def test_compact_content_joins_mcp_content_blocks():
    # MCP adapters return a list of blocks; OpenAI rejects arrays past
    # 16,384 elements, so they are joined into one string
    msg = ToolMessage(
        content=[{"type": "text", "text": "row one"}, {"type": "text", "text": "row two"}],
        tool_call_id="c1",
    )
    assert _compact_content(msg).content == "row one\nrow two"


def test_compact_content_truncates_oversized_text(monkeypatch):
    monkeypatch.setattr(agnes_agent, "MAX_TOOL_CONTENT_LEN", 10)
    msg = ToolMessage(content="x" * 50, tool_call_id="c1")
    out = _compact_content(msg)
    assert out.content == "x" * 10 + "\n...[truncated]"
    assert out.tool_call_id == "c1"


def test_compact_content_leaves_short_strings_and_other_messages_alone():
    short = ToolMessage(content="fine", tool_call_id="c1")
    assert _compact_content(short) is short
    human = HumanMessage(content="hello")
    assert _compact_content(human) is human


def test_trim_messages_keeps_the_last_n(monkeypatch):
    monkeypatch.setattr(agnes_agent, "KEEP_LAST_N", 3)
    messages = [HumanMessage(content=str(i)) for i in range(10)]
    assert [m.content for m in _trim_messages(messages)] == ["7", "8", "9"]


def test_trim_messages_drops_orphaned_tool_results(monkeypatch):
    # a ToolMessage whose parent AIMessage fell outside the window is
    # rejected by OpenAI as a dangling tool result
    monkeypatch.setattr(agnes_agent, "KEEP_LAST_N", 3)
    messages = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "filter_dataset", "args": {}, "id": "c1"}]),
        ToolMessage(content="rows", tool_call_id="c1"),
        AIMessage(content="answer"),
        HumanMessage(content="next"),
    ]
    kept = _trim_messages(messages)
    assert [m.__class__.__name__ for m in kept] == ["AIMessage", "HumanMessage"]


def test_trim_messages_is_a_no_op_below_the_window(monkeypatch):
    monkeypatch.setattr(agnes_agent, "KEEP_LAST_N", 20)
    messages = [HumanMessage(content="one"), AIMessage(content="two")]
    assert [m.content for m in _trim_messages(messages)] == ["one", "two"]
