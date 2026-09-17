# LangGraph, from the ground up

The companion to [async.md](async.md), same recipe: part 1 builds
LangGraph's concepts one at a time for a novice Python programmer,
with complete standalone samples; part 2 walks
`agent-client/src/ease_clients/utils/agnes_agent_graph.py` and shows
where each concept does real work -- how the graph is CONSTRUCTED
(state, node factories, conditional edges, the tool loops, sticky
specialist ownership) and what the graph EMITS as an event stream.

Running the samples: they need langgraph and langchain-core, which
the agent project already has -- save each as a .py file inside
`agent-client/` and run `uv run python sample_x.py`. NO Azure
credentials and no MCP server are needed: the samples use rule-based
nodes and, for streaming, a fake chat model that ships with
langchain-core. (Read async.md first -- part 1 here assumes you know
what `async def`, `await`, and `async for` mean.)

## Contents

Part 1 -- the concepts
1. [What LangGraph is, and why a graph at all](#1-what-langgraph-is-and-why-a-graph-at-all)
2. [State and nodes: the smallest graph](#2-state-and-nodes-the-smallest-graph)
3. [Reducers: why returning messages APPENDS](#3-reducers-why-returning-messages-appends)
4. [Conditional edges: decisions become arrows](#4-conditional-edges-decisions-become-arrows)
5. [Cycles: the tool loop](#5-cycles-the-tool-loop)
6. [Checkpointers and threads: memory between turns](#6-checkpointers-and-threads-memory-between-turns)
7. [Streaming a graph: astream](#7-streaming-a-graph-astream)
8. [astream_events with a streaming model](#8-astream_events-with-a-streaming-model)

Part 2 -- the concepts in the agent
9. [The map](#9-the-map)
10. [The state: AgentState](#10-the-state-agentstate)
11. [The node factories](#11-the-node-factories)
12. [The tool split and the custom tool node](#12-the-tool-split-and-the-custom-tool-node)
13. [The edges: how the topology encodes the design](#13-the-edges-how-the-topology-encodes-the-design)
14. [Supersteps, checkpoints, and sessions](#14-supersteps-checkpoints-and-sessions)
15. [What the graph emits: the event stream](#15-what-the-graph-emits-the-event-stream)

---

## 1. What LangGraph is, and why a graph at all

An LLM agent is a loop: call the model, look at what it wants, maybe
run a tool, feed the result back, repeat until there is an answer.
You could write that as ordinary Python -- a while loop with if
statements -- and it would work.

LangGraph asks you to write it as a GRAPH instead: nodes (units of
work) connected by edges (what runs next), all reading and writing a
shared STATE. The reason is not elegance; it is what you get for free
once the structure is explicit:

- **checkpointing** -- the runtime saves state after every node, so
  conversations persist and resume by id (sessions)
- **streaming** -- the runtime can report everything as it happens
  (which node started, each LLM token) because IT runs the nodes
- **inspection** -- the same event stream powers the per-node timing
  in the stream logs

The mental model in one sentence: **state flows along edges; each
node reads the state, does one thing, and returns a PARTIAL update;
the runtime merges updates and follows the edges until END.**

## 2. State and nodes: the smallest graph

```python
# sample_1_smallest.py
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    question: str
    answer: str

def answer_node(state: State) -> dict:
    # read what you need from state...
    q = state["question"]
    # ...return ONLY what you change (a PARTIAL update, not the whole state)
    return {"answer": f"You asked: {q!r}. The answer is 42."}

graph = StateGraph(State)          # 1. declare the state shape
graph.add_node("answer", answer_node)   # 2. add nodes
graph.add_edge(START, "answer")         # 3. wire edges
graph.add_edge("answer", END)
app = graph.compile()                   # 4. compile -> runnable

result = app.invoke({"question": "what is the meaning of life?"})
print(result)      # the FULL final state: question AND answer
```

The four-step ritual (declare state, add nodes, wire edges, compile)
is the whole construction API. Two things to absorb:

- A node is any function (sync or async) taking the state and
  returning a dict of CHANGES. `answer_node` never touches
  `question`; the runtime merges its partial update into the state.
- `START` and `END` are special markers, not nodes you write. An edge
  from START says "begin here"; reaching END stops the run.

## 3. Reducers: why returning messages APPENDS

How does "merge the partial update" work when two nodes both write
the same key? By default: last write REPLACES. But a state field can
declare a REDUCER -- a function that combines old and new instead.
The one you will use constantly is the messages reducer:

```python
# sample_2_reducers.py
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState

# MessagesState = predefined state with ONE field, `messages`, whose
# reducer APPENDS. We extend it with a plain field for contrast.
class State(MessagesState):
    steps: int          # no reducer -> plain overwrite

def first(state: State) -> dict:
    return {"messages": [AIMessage(content="hello from first")], "steps": 1}

def second(state: State) -> dict:
    return {"messages": [AIMessage(content="hello from second")], "steps": 2}

graph = StateGraph(State)
graph.add_node("first", first)
graph.add_node("second", second)
graph.add_edge(START, "first")
graph.add_edge("first", "second")
graph.add_edge("second", END)
app = graph.compile()

result = app.invoke({"messages": [HumanMessage(content="hi")]})
print("steps =", result["steps"], "(overwritten -- last write wins)")
print("messages:")
for m in result["messages"]:
    print("  ", type(m).__name__, "->", m.content)
```

Run it: `steps` is 2 (second overwrote first), but `messages` holds
ALL THREE messages -- the human input plus both AI messages, in
order. Each node returned a one-element list; the reducer appended.

This is the single most important convention in the whole codebase:
**`return {"messages": [response]}` means "append these to the
conversation," never "replace the conversation."** A conversation
GROWS through the graph; nobody has to pass the full history around.

## 4. Conditional edges: decisions become arrows

A plain edge always goes one place. A CONDITIONAL edge calls a
function you provide -- it looks at the state and returns the NAME of
the branch to take:

```python
# sample_3_conditional.py
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState

def router(state: MessagesState) -> dict:
    return {}          # decides nothing itself -- just a waypoint here

def pick_specialist(state: MessagesState) -> str:
    """The EDGE function: reads state, returns a branch NAME."""
    question = state["messages"][-1].content.lower()
    if "policy" in question or "how do i" in question:
        return "docs"
    return "data"

def docs_specialist(state: MessagesState) -> dict:
    return {"messages": [AIMessage(content="[docs specialist answers]")]}

def data_specialist(state: MessagesState) -> dict:
    return {"messages": [AIMessage(content="[data specialist answers]")]}

graph = StateGraph(MessagesState)
graph.add_node("router", router)
graph.add_node("docs", docs_specialist)
graph.add_node("data", data_specialist)
graph.add_edge(START, "router")
graph.add_conditional_edges(
    "router",
    pick_specialist,                       # the decision function
    {"docs": "docs", "data": "data"},      # branch name -> node name
)
graph.add_edge("docs", END)
graph.add_edge("data", END)
app = graph.compile()

for q in ["How do I request access?", "How many admins are in Finance?"]:
    out = app.invoke({"messages": [HumanMessage(content=q)]})
    print(f"{q!r}\n   -> {out['messages'][-1].content}")
```

The decision function here is rule-based (keyword matching); in the
real agent the SAME slot is filled by a function that reads what the
LLM decided. That is the deep trick of section 13: the model's choice
is written into the state as a message, and an ordinary Python edge
function reads it back out and turns it into an arrow.

## 5. Cycles: the tool loop

Edges may point BACKWARDS -- and one particular cycle is the heart of
every tool-using agent: agent asks for a tool, tool runs, results go
back to the agent, until the agent stops asking.

```python
# sample_4_toolloop.py
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph, START, END, MessagesState

def agent(state: MessagesState) -> dict:
    """Stands in for the LLM: asks for a lookup until it has 2 results."""
    results = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    if len(results) < 2:
        # a fake tool call -- same shape the LLM produces
        msg = AIMessage(content="", tool_calls=[
            {"name": "lookup", "args": {"n": len(results)+1}, "id": f"call{len(results)+1}"}
        ])
        return {"messages": [msg]}
    return {"messages": [AIMessage(content=f"Done after {len(results)} lookups.")]}

def tools(state: MessagesState) -> dict:
    """Stands in for the tool executor: answers the pending call."""
    call = state["messages"][-1].tool_calls[0]
    return {"messages": [ToolMessage(content=f"result #{call['args']['n']}",
                                     tool_call_id=call["id"])]}

def should_continue(state: MessagesState) -> str:
    """Tool calls pending -> loop to tools; none -> the answer is final."""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END

graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_node("tools", tools)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")            # <- the cycle
app = graph.compile()

out = app.invoke({"messages": [HumanMessage(content="go")]})
for m in out["messages"]:
    print(f"{type(m).__name__:12} {m.content or m.tool_calls}")
```

Trace the output: agent asks, tool answers, agent asks again, tool
answers, agent concludes -- `agent -> tools -> agent -> tools ->
agent -> END`. The edge convention to memorize: **the last AIMessage
having `tool_calls` means "not done yet."** The runtime protects you
from real infinite loops with a recursion limit (the agent sets 50).

## 6. Checkpointers and threads: memory between turns

Everything so far forgets when `invoke` returns. A CHECKPOINTER saves
state after every node -- keyed by a THREAD ID you pass in config --
so the next invoke on the same id CONTINUES:

```python
# sample_5_checkpointer.py
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import MemorySaver

def count(state: MessagesState) -> dict:
    n = sum(isinstance(m, HumanMessage) for m in state["messages"])
    return {"messages": [AIMessage(content=f"I have now heard {n} human message(s).")]}

graph = StateGraph(MessagesState)
graph.add_node("count", count)
graph.add_edge(START, "count")
graph.add_edge("count", END)
app = graph.compile(checkpointer=MemorySaver())   # <- the one-argument change

alice = {"configurable": {"thread_id": "alice"}}
bob   = {"configurable": {"thread_id": "bob"}}

print(app.invoke({"messages": [HumanMessage(content="hi")]},   alice)["messages"][-1].content)
print(app.invoke({"messages": [HumanMessage(content="again")]}, alice)["messages"][-1].content)
print(app.invoke({"messages": [HumanMessage(content="hello")]}, bob)["messages"][-1].content)
print("alice's full history:", len(app.get_state(alice).values["messages"]), "messages")
```

Alice's second turn says "2 human messages" -- her first turn was
remembered, because on the same thread_id the input messages APPEND
(the reducer again!) to the saved state. Bob starts fresh at 1.

Rename the players and you have the agent's session model exactly: a
**session id IS a thread_id**; "conversation history" IS the
checkpointed messages; evicting a session IS deleting a thread; and
swapping MemorySaver for a Redis-backed saver (P3.1) moves all of it
out of the process without changing the graph.

## 7. Streaming a graph: astream

`invoke` returns only the end. `astream` yields each node's update as
it commits -- node-level progress:

```python
# sample_6_astream.py
import asyncio
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState

def make_step(name):
    """Factory returning an ASYNC node -- the same closure pattern the
    agent's _make_agent_node uses (section 11)."""
    async def step(state: MessagesState) -> dict:
        await asyncio.sleep(0.3)                 # pretend to work
        return {"messages": [AIMessage(content=f"{name} done")]}
    return step

graph = StateGraph(MessagesState)
graph.add_node("plan",    make_step("plan"))
graph.add_node("execute", make_step("execute"))
graph.add_node("report",  make_step("report"))
graph.add_edge(START, "plan")
graph.add_edge("plan", "execute")
graph.add_edge("execute", "report")
graph.add_edge("report", END)
app = graph.compile()

async def main():
    async for update in app.astream({"messages": [HumanMessage(content="go")]}):
        print("update from:", list(update.keys())[0])   # arrives one node at a time

asyncio.run(main())
```

Watch the three updates arrive ~0.3s apart -- you are seeing the
graph run. One trap worth naming (async.md sample 1's lesson, biting
in a new place): a node must BE an `async def` function for LangGraph
to await it -- a sync `lambda` that merely RETURNS a coroutine fails
with InvalidUpdateError, because the runtime sees a sync node that
returned a coroutine object instead of a dict. This granularity --
per NODE -- is often enough; the agent needs finer.

## 8. astream_events with a streaming model

The agent wants two granularities at once: node-level (to update the
spinner phase) and TOKEN-level (to type the answer live).
`astream_events` provides both -- an async generator yielding an
event for everything every runnable does. This sample produces real
LLM token events with no network, using the fake streaming chat model
that ships in langchain-core:

```python
# sample_7_events.py
import asyncio
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END, MessagesState

# streams its canned reply token by token, like a real chat model
fake_llm = GenericFakeChatModel(messages=iter(
    [AIMessage(content="Access requests go through the portal.")]
))

async def agent(state: MessagesState) -> dict:
    response = await fake_llm.ainvoke(state["messages"])
    return {"messages": [response]}

graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_edge(START, "agent")
graph.add_edge("agent", END)
app = graph.compile()

async def main():
    async for event in app.astream_events(
        {"messages": [HumanMessage(content="how do I get access?")]},
        version="v2",
    ):
        kind = event["event"]
        if kind == "on_chain_start" and event.get("metadata", {}).get("langgraph_node"):
            print(f"\n[node starting: {event['metadata']['langgraph_node']}]")
        elif kind == "on_chat_model_stream":
            print(event["data"]["chunk"].content, end="|", flush=True)
        elif kind == "on_chain_end" and event.get("metadata", {}).get("langgraph_node"):
            print(f"\n[node finished: {event['metadata']['langgraph_node']}]")

asyncio.run(main())
```

The output shows the two granularities interleaved: a node-start
line, then the answer arriving token|by|token (each `|` marks one
`on_chat_model_stream` event), then node-end. Three things carry over
directly to the real code:

- events are named `on_[runnable type]_(start|stream|end)`;
- `metadata.langgraph_node` tells you WHICH graph node an event
  belongs to -- including the token events, because children inherit
  the node's metadata;
- this is async.md's sample 5 (async generators) grown up: the graph
  produces, your `async for` consumes, and anything else on the loop
  (a spinner, other API sessions) runs in the gaps.

---

## 9. The map

Line numbers refer to
`agent-client/src/ease_clients/utils/agnes_agent_graph.py`.

| Concept (sample) | In the agent |
|---|---|
| state declaration (1) | `AgentState(MessagesState)` + `active_agent` (151-153) |
| partial updates + messages reducer (2) | every node's `return {"messages": [...]}` (e.g. 565, 547, 587) |
| conditional edges (3) | `route_entry` (653), `route_from_router` (661), `_make_specialist_edge` (680); wired at 762-806 |
| the tool-loop cycle (4) | three specialist sub-loops, e.g. `knowledgebase_agent` <-> `knowledgebase_tools` (786-791) |
| checkpointer + threads (5) | `graph.compile(checkpointer=...)` in cli/agent_api; session id = thread_id; RedisSaver in P3.1 |
| astream (6) | not used -- the agent needs token granularity |
| astream_events (7) | `_stream_response` (1063, CLI) and `AgentService.stream` (agent_api.py) |

## 10. The state: AgentState

```python
class AgentState(MessagesState):
    active_agent: str          # (151-153)
```

Two fields, two different merge behaviors -- exactly sample 2:
`messages` appends (the whole conversation accumulates across nodes
AND across turns, because the checkpointer persists it per thread);
`active_agent` overwrites (it is a switch, not a log: the router sets
it, the handoff node clears it, and whoever wrote last wins).

One subtlety the state does NOT show: `_trim_messages` (499). State
keeps the FULL history; each LLM-calling node trims to the last
KEEP_LAST_N messages just before `ainvoke`. So "what the graph
remembers" and "what the model sees" are deliberately different
things -- trimming is a prompt-size decision at the node, not a state
mutation. (This distinction is also where the P1.3 prompt-caching
work will land.)

## 11. The node factories

The nodes are built by FACTORIES -- functions returning node
functions -- because each node needs its own llm-with-tools binding
and prompt baked in (a closure), while the graph wiring stays
generic:

- `_make_router_node(llm, ROUTING_TOOLS)` (520-548): binds the three
  `route_to_*` tools and awaits the LLM. Here is the trick worth
  staring at: **the routing "tools" are never executed.** They are
  forms for the LLM to fill in -- structured output disguised as tool
  calls. The node itself synthesizes the ToolMessage replies (537-541)
  to keep the transcript valid, records the choice in `active_agent`
  (547), and the EDGE function later reads the same tool call to pick
  the branch. LLM decision -> state -> edge.
- `_make_agent_node(llm, tools, prompt)` (550-567): the specialist
  template -- trim, prepend the specialist's SystemMessage, await
  `ainvoke`, append the response. Note what it does NOT do: no tool
  execution (the ToolNode's job) and no edge decision (the edge
  function's job). One node, one responsibility.
- `_handoff_node` (571-587): pure Python, no LLM. Answers any pending
  tool calls politely (a transcript with an unanswered tool call is
  invalid to OpenAI) and clears `active_agent` (587) so the router
  takes over -- sample 2's overwrite semantics doing design work.

## 12. The tool split and the custom tool node

`build_graph` (711) starts by SPLITTING the 12 MCP tools into
specialist subsets (734-741) -- knowledgebase, resource, quality --
and each specialist's LLM binding gets its subset PLUS
`hand_off_to_router`. That one asymmetry is load-bearing: the LLM may
CALL handoff (it is in the binding), but the ToolNode cannot RUN it
(it is not in the executor) -- handoff is another never-executed
form, handled by topology instead.

`_make_tool_node` (592-648) wraps LangGraph's prebuilt `ToolNode`
(which executes real tool calls -- here, MCP round-trips) to handle
the awkward case: the specialist calls domain tools AND handoff in
one response. The wrapper runs the domain tools normally and stubs
the handoff with "answer your part first" (630-644) -- mixed
questions get their domain answer before any rerouting.

## 13. The edges: how the topology encodes the design

Every design decision from CLAUDE.md's architecture section is
literally an edge (762-810):

- **"Specialists own the conversation across turns"** =
  `route_entry` (653) on START: if `active_agent` names a specialist,
  go straight there; else go to the router. Sample 3's decision
  function, reading the field the router wrote last turn -- and,
  because of section 6, remembered across turns by the checkpointer.
- **"The router picks a specialist or answers directly"** =
  `route_from_router` (661): find the last AIMessage; if its tool
  call names a `route_to_*` tool, branch to that specialist; no tool
  call means the router answered small talk itself -> END.
- **"Answer, use tools, or hand off"** = `_make_specialist_edge`
  (680): tool_calls present? handoff-only -> "handoff"; any domain
  call -> the tool node; no tool_calls -> the answer is final -> END.
  Sample 4's `should_continue`, grown two extra branches.
- **The three sub-loops** (786-807): `agent -> tools -> agent`
  cycles, one per specialist -- sample 4 three times over.
- **`handoff -> router`** (810): after clearing ownership, re-route
  in the SAME turn. Combined with the specialist loops this creates
  the larger cycle `router -> specialist -> handoff -> router`, which
  is how a mixed question migrates between specialists mid-turn.

Read 762-810 with this list and the ASCII diagram in the docstring
(714-728); nothing in the topology is decorative.

## 14. Supersteps, checkpoints, and sessions

The runtime executes one node, merges its update, SAVES A CHECKPOINT
(when a checkpointer is configured), then follows edges to the next
node -- each save point is a "superstep." Consequences you have
already met elsewhere:

- a long turn writes MANY checkpoints (router, specialist, tools,
  specialist, ...) -- this is the per-superstep write volume that
  P3.1 moved to Redis and P3.2 will prune;
- the graph is compiled ONCE and shared by all sessions; per-session
  identity lives entirely in the `thread_id` config (sample 5), which
  is why one process -- or several, with Redis -- serves many
  conversations concurrently;
- `aget_state(config)` (used by `get_history` and the CLI's fallback
  path) is just "read the latest checkpoint for this thread."

## 15. What the graph emits: the event stream

async.md section 12 covered CONSUMING `astream_events`; here is the
EMITTING side, which explains the two details the consumers key on:

- **Every runnable in the tree emits start/stream/end.** A "node" is
  a runnable wrapping YOUR function, which may invoke child runnables
  (the chat model, the tools). So one router step emits
  `on_chain_start` (the node), `on_chat_model_start`, a burst of
  `on_chat_model_stream` chunks, `on_chat_model_end`, then
  `on_chain_end` (the node).
- **Children inherit `metadata.langgraph_node`** -- a token chunk
  knows it belongs to `resource_agent`. This is how the CLI decides a
  chunk is a displayable answer token, and it is why filtering
  matters: metadata alone cannot distinguish the node's OWN start/end
  from its children's.
- **`event["name"]` disambiguates.** The node's own events carry the
  node name in `name`; children carry their own names. The API's
  per-node timing (agent_api.py, the P0 instrumentation) keys on
  exactly `event["name"] == node` to time the node itself and not
  every child -- a one-line filter that is easy to miss and
  impossible to understand without this emission model.
- `_NODE_PHASES` (953) is the last hop: node name -> human-readable
  phase ("Routing", "Searching the knowledgebase", ...) for the
  spinner and the API's phase events.

Trace one real turn end to end and the whole document assembles:
`route_entry` reads `active_agent` -> a specialist node awaits the
LLM (tokens streaming out as `on_chat_model_stream`) -> its edge
function reads the tool calls -> the ToolNode hits the MCP server ->
back to the specialist -> END; every hop checkpointed under the
session's thread_id, every event consumed by `_stream_response` or
`AgentService.stream` to become spinner phases, live tokens, and
`took=` timings.
