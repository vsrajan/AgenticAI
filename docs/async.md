# Python async IO, from the ground up

A tutorial for a novice Python programmer, in two parts. Part 1
builds the concepts one at a time -- every sample is a complete,
standalone script (save as a .py file, run with `python3 file.py`,
standard library only). Part 2 walks through
`agent-client/src/ease_clients/utils/agnes_agent_graph.py` and shows
where every one of those concepts does real work: the LangGraph event
stream, the token-by-token answer display, and the spinner.

Read part 1 top to bottom; each concept builds on the previous one.

## Contents

Part 1 -- the concepts
1. [The problem async solves: waiting is not working](#1-the-problem-async-solves-waiting-is-not-working)
2. [Coroutines: async def and await](#2-coroutines-async-def-and-await)
3. [The event loop, and real concurrency with gather](#3-the-event-loop-and-real-concurrency-with-gather)
4. [Tasks: starting work in the background](#4-tasks-starting-work-in-the-background)
5. [Cancellation: how a task is told to stop](#5-cancellation-how-a-task-is-told-to-stop)
6. [Async generators: streams of values](#6-async-generators-streams-of-values)
7. [Async context managers: async with](#7-async-context-managers-async-with)
8. [asyncio.Lock: one at a time](#8-asynciolock-one-at-a-time)
9. [The golden rule: never block the loop](#9-the-golden-rule-never-block-the-loop)

Part 2 -- the concepts in the agent
10. [The map: where each concept lives in the code](#10-the-map-where-each-concept-lives-in-the-code)
11. [The entry point and the graph nodes](#11-the-entry-point-and-the-graph-nodes)
12. [astream_events: consuming the graph as a stream](#12-astream_events-consuming-the-graph-as-a-stream)
13. [The Spinner, line by line](#13-the-spinner-line-by-line)
14. [How the spinner and the tokens share one thread](#14-how-the-spinner-and-the-tokens-share-one-thread)
15. [The same concepts in the API layer](#15-the-same-concepts-in-the-api-layer)

---

## 1. The problem async solves: waiting is not working

Most of what this agent does is WAIT. A call to Azure OpenAI takes
seconds -- and during those seconds your CPU does nothing. It sent
some bytes, and it is waiting for bytes to come back. Same for MCP
tool calls and Redis: the program's time is overwhelmingly spent
waiting on the network, not computing.

Ordinary ("synchronous") Python wastes that waiting time. When you
call `requests.get(...)`, the whole program stands still until the
response arrives. If you want a spinner animating while you wait, or
two requests in flight at once, plain Python has no way to express it
in one thread.

Async IO is Python's way of saying: "while THIS piece of work is
waiting, go run some OTHER piece of work." One thread, many tasks,
switching between them AT THE MOMENTS THEY WAIT. That last clause is
the whole model -- switching happens only at explicitly marked
waiting points, never in the middle of a line. The mark is the word
`await`.

Async does not make anything faster in itself -- one CPU is still one
CPU. It makes WAITING useful.

## 2. Coroutines: async def and await

```python
# sample_1_coroutines.py
import asyncio

async def make_coffee():
    print("grinding beans...")
    await asyncio.sleep(1)          # stand-in for any slow IO wait
    print("coffee ready")
    return "espresso"

async def main():
    print("calling make_coffee() gives:", make_coffee())  # NOT coffee!
    result = await make_coffee()    # THIS actually runs it
    print("got:", result)

asyncio.run(main())
```

Run it. Three things to absorb:

- `async def` does not define a function that runs when called. It
  defines a COROUTINE FACTORY: `make_coffee()` returns a coroutine
  object -- a description of work, frozen at its first line. (The
  first print shows `<coroutine object ...>`, and Python warns that a
  coroutine was never awaited.)
- `await` is what runs a coroutine: "start this work, and while it
  waits, I wait too -- wake me when it is done, and hand me its
  return value."
- `asyncio.run(main())` is the bridge from the ordinary synchronous
  world into the async world. You call it ONCE, at the top of the
  program; it creates the event loop, runs the coroutine to
  completion, and tears the loop down.

`await` is only legal inside `async def` -- the marker is part of the
contract: only code that declared itself pausable may pause.

## 3. The event loop, and real concurrency with gather

The EVENT LOOP is the scheduler that `asyncio.run` starts: a plain
Python loop that keeps a list of paused coroutines, watches their
waits (timers, sockets), and resumes whichever one's wait has
finished. One loop, one thread, one coroutine actually executing at
any instant.

So far our samples awaited things one after another -- no better than
ordinary Python. Concurrency appears when you give the loop MORE THAN
ONE thing to run:

```python
# sample_2_gather.py
import asyncio, time

async def order(name, seconds):
    print(f"  {name}: ordered")
    await asyncio.sleep(seconds)     # the wait -- loop is FREE during this
    print(f"  {name}: arrived")
    return name

async def main():
    t0 = time.perf_counter()
    await order("coffee", 1); await order("bagel", 1); await order("juice", 1)
    print(f"sequential: {time.perf_counter()-t0:.1f}s")   # ~3.0s

    t0 = time.perf_counter()
    await asyncio.gather(order("coffee", 1), order("bagel", 1), order("juice", 1))
    print(f"concurrent: {time.perf_counter()-t0:.1f}s")   # ~1.0s

asyncio.run(main())
```

Same three orders: 3 seconds sequentially, 1 second with
`asyncio.gather` -- because while coffee WAITS, the loop starts bagel,
and while both wait, juice. The waits overlap; the work (the prints)
still happens one at a time. That is the entire trick, and it is why
the agent can animate a spinner while an LLM call is in flight.

## 4. Tasks: starting work in the background

`await coro` runs work and BLOCKS you until it finishes. Sometimes
you want "start this, let it run alongside me, I will deal with it
later." That is a TASK:

```python
# sample_3_tasks.py
import asyncio

async def ticker():
    n = 0
    while True:
        n += 1
        print(f"  tick {n}")
        await asyncio.sleep(0.3)

async def main():
    task = asyncio.create_task(ticker())   # starts NOW, in the background
    print("main: doing my own slow work...")
    await asyncio.sleep(1)                 # ticker ticks during this wait
    print("main: done, stopping the ticker")
    task.cancel()

asyncio.run(main())
```

`asyncio.create_task(ticker())` schedules the coroutine on the loop
and returns immediately with a `Task` handle. The ticker then runs
whenever main is waiting. This is exactly how the agent's spinner
works: a background task animating during the pauses of the real
work. Note the infinite `while True` is fine -- the task lives until
someone cancels it.

## 5. Cancellation: how a task is told to stop

`task.cancel()` in the last sample looks like a kill switch. It is
politer and subtler than that, and the subtlety matters:

```python
# sample_4_cancel.py
import asyncio

async def worker():
    try:
        while True:
            print("  working...")
            await asyncio.sleep(0.3)       # <- cancellation lands HERE
    except asyncio.CancelledError:
        print("  worker: cancelled -- cleaning up my mess")
        # (close files, clear the terminal line, etc.)
        raise                              # re-raise is good manners

async def main():
    task = asyncio.create_task(worker())
    await asyncio.sleep(1)
    task.cancel()          # step 1: only SETS A FLAG, returns instantly
    try:
        await task         # step 2: wait for the worker to actually finish
    except asyncio.CancelledError:
        pass               # expected -- the cancellation was ours
    print("main: worker is fully stopped")

asyncio.run(main())
```

The mechanics, in order:

1. `task.cancel()` does not stop anything. It marks the task; the
   method returns immediately.
2. The next time the task reaches an `await`, the loop raises
   `asyncio.CancelledError` INSIDE the task, at that await. (Remember:
   switching only happens at waiting points -- so cancellation can
   only be delivered at one.)
3. The task's `except CancelledError:` block is its last chance to
   clean up.
4. The canceller should `await task` afterwards if it needs the
   cleanup to have finished before proceeding -- awaiting a cancelled
   task re-raises CancelledError at the caller, hence the try/except.

Keep this sample in mind; the agent's Spinner is this pattern almost
line for line, where "cleaning up my mess" = erasing the spinner from
the terminal.

## 6. Async generators: streams of values

A regular function returns once. A GENERATOR yields many values over
time. An ASYNC generator yields many values over time AND may wait
between them -- which is exactly the shape of an LLM answer: tokens,
arriving one by one, with network waits in between.

```python
# sample_5_asyncgen.py
import asyncio

async def fake_llm(prompt):
    """Yields an answer token by token, like a streaming LLM."""
    for token in f"You asked: {prompt} -- here is a slow answer.".split():
        await asyncio.sleep(0.2)           # network delay per token
        yield token                        # hand ONE value to the consumer

async def main():
    async for token in fake_llm("hello"):  # resumes once per yield
        print(token, end=" ", flush=True)
    print()

asyncio.run(main())
```

`async for` is the consuming side: each iteration awaits the
generator until it yields the next value. The consumer runs
INTERLEAVED with the producer -- print a token, wait for the next,
print, wait... While waiting, the loop is free to run other tasks
(like a spinner). LangGraph's `astream_events` is precisely this: an
async generator you `async for` over, yielding an event dict for
everything that happens inside the graph.

## 7. Async context managers: async with

You know `with open(...) as f:` -- setup, use, guaranteed cleanup.
`async with` is the same idea when setup or cleanup themselves need
to await:

```python
# sample_6_asyncwith.py
import asyncio

class Stopwatch:
    async def __aenter__(self):
        print("  starting stopwatch")
        self.t0 = asyncio.get_event_loop().time()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        elapsed = asyncio.get_event_loop().time() - self.t0
        print(f"  stopped: {elapsed:.2f}s (even if the body raised)")

async def main():
    async with Stopwatch():
        await asyncio.sleep(0.5)

asyncio.run(main())
```

`__aenter__` runs on entry, `__aexit__` on exit -- ALWAYS on exit,
exception or not. The agent's Spinner implements both, so
`async with Spinner("Thinking"):` guarantees the spinner is erased
even when the code inside blows up -- cleanup you cannot forget.

## 8. asyncio.Lock: one at a time

If only one coroutine runs at an instant, why would async code need a
lock? Because a critical SECTION usually spans several awaits -- and
at every await, another coroutine may run and enter the same section:

```python
# sample_7_lock.py
import asyncio

history = []
lock = asyncio.Lock()

async def turn(name, use_lock):
    ctx = lock if use_lock else _NoLock()
    async with ctx:
        history.append(f"{name}: question")
        await asyncio.sleep(0.1)           # "the LLM call"
        history.append(f"{name}: answer")

class _NoLock:
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass

async def main():
    history.clear()
    await asyncio.gather(turn("A", False), turn("B", False))
    print("without lock:", history)   # A/B INTERLEAVED -- corrupted history

    history.clear()
    await asyncio.gather(turn("A", True), turn("B", True))
    print("with lock:   ", history)   # A complete, then B complete

asyncio.run(main())
```

Without the lock, the output interleaves: A's question, B's question,
A's answer, B's answer -- two conversations scrambled into one
transcript. With the lock, whoever enters first finishes first. This
is verbatim the reason the API layer holds a per-session lock across
each turn: a turn is one long critical section full of awaits, and
two concurrent messages for the same session must not interleave
their writes into one conversation history.

## 9. The golden rule: never block the loop

Everything above works because coroutines pause AT awaits. A
coroutine that does something slow WITHOUT awaiting -- `time.sleep`,
a huge computation, a synchronous network call -- freezes the entire
loop: no other task runs, no spinner spins, no request is served.

```python
# sample_8_blocking.py
import asyncio, time

async def ticker():
    while True:
        print("  tick")
        await asyncio.sleep(0.2)

async def polite():
    await asyncio.sleep(1)             # ticker ticks ~5 times

async def rude():
    time.sleep(1)                      # ticker FREEZES for the full second

async def main():
    t = asyncio.create_task(ticker())
    print("polite wait (await asyncio.sleep):"); await polite()
    print("rude wait (time.sleep -- watch the ticks stop):"); await rude()
    t.cancel()

asyncio.run(main())
```

Same one-second wait, radically different behavior: `await
asyncio.sleep` yields the loop to the ticker; `time.sleep` holds the
one thread hostage. The rule: inside async code, every slow thing
must be awaitable. (Small, fast sync operations are fine -- see the
stream-file note in section 12 for a deliberate, measured exception.)

---

## 10. The map: where each concept lives in the code

All line numbers refer to
`agent-client/src/ease_clients/utils/agnes_agent_graph.py` unless
stated otherwise.

| Concept (sample) | In the agent |
|---|---|
| asyncio.run bridge (1) | `cli.py:35` -- `asyncio.run(run_agent_loop())`, the program's single entry into async |
| coroutines + await (1) | every graph node, e.g. `router_node` (527) awaiting `llm.ainvoke` (529) |
| event loop interleaving (3) | spinner animating DURING LLM/tool waits (section 14) |
| background task (4) | `Spinner.start` -> `asyncio.create_task(self._spin())` (94) |
| cancellation (5) | `Spinner.stop` -> `cancel()` + `await` + CancelledError cleanup (96-110, 81-90) |
| async generator + async for (6) | `agent.astream_events(...)` consumed at 1063; also `aget_state_history` consumed by an async list comprehension (912) |
| async with (7) | `Spinner.__aenter__/__aexit__` (112-117) |
| asyncio.Lock (8) | agent_api.py: the per-session turn lock (`async with turn_lock:` in `stream()`) |
| golden rule (9) | the stream-file writes at 1050 -- a deliberate small bend, discussed in section 12 |

## 11. The entry point and the graph nodes

The whole CLI is one `asyncio.run` call (`cli.py:35`): it starts the
loop, runs `run_agent_loop` (965) to completion, and exits. Inside,
everything slow is awaited: loading MCP tools (`await
client.get_tools()`, 980) and each turn (`await _stream_response(...)`,
1016).

The graph nodes are plain coroutines. `router_node` (527) and
`agent_node` (560) each do essentially one thing:

```python
response = await llm_with_tools.ainvoke(...)
```

That await is where the seconds go -- the Azure OpenAI round trip.
Because it is an await, the loop is free during it, which is the only
reason a spinner can animate at the same time in the same thread.
LangGraph itself runs these node coroutines on the same loop; you
wrote coroutines, it schedules them.

## 12. astream_events: consuming the graph as a stream

`_stream_response` (1032) is the heart of the CLI's output, and it is
sample 5's consumer pattern at production scale:

```python
async for event in agent.astream_events({...}, config, version="v2"):
```

`astream_events` is an async generator: it RUNS the whole graph and
yields a dict for every internal event as it happens -- a node
starting, an LLM token arriving, a tool finishing. The comment block
at 1054-1094 is the reference for the event taxonomy
(`on_[type]_(start|stream|end)`); the loop body uses exactly three:

- `on_chain_start` (1097): a graph node began -- look up its
  human-readable phase in `_NODE_PHASES` and update the spinner label
  ("Routing", "Calling tools", ...).
- `on_chat_model_stream` (1105): one chunk from the LLM. Chunks with
  `.content` and no `.tool_call_chunks` are displayable answer tokens:
  the first one flips the `streaming` latch (1111-1114) -- stop the
  spinner, print the header, then write tokens with
  `sys.stdout.write` + `flush` for the live typing effect.
- `on_chain_end` (1124): a node finished -- append its output messages
  to the stream file for `tail -f`.

Two details worth noticing:

- The `try/finally` (1054, 1133): whatever happens inside the stream
  -- including exceptions from the LLM -- the spinner is stopped and
  the file closed. Combine with section 13 and you see the same
  guarantee twice, belt and braces.
- The golden-rule bend: `open()` and `write()` on the stream file
  (1050, via `_write_stream_entry`) are SYNCHRONOUS calls inside async
  code. That is a deliberate, measured exception: local appends are
  microseconds, and the simplicity is worth it for a debug feature.
  The API layer documents the same trade and turns the file OFF in
  production (PerformanceRecommendations P4) -- the rule is real; this
  is a knowing, bounded violation, not an oversight.

## 13. The Spinner, line by line

The Spinner (46-117) composes samples 3, 4, 5, and 7 into thirty
lines. Mapping each piece:

- `start()` (92): `self._task = asyncio.create_task(self._spin())` --
  sample 3. The animation is now a background task; `start` returns
  immediately.
- `_spin()` (68): an infinite loop printing a frame, then
  `await asyncio.sleep(0.08)` (80). That await is not just the frame
  rate -- it is THE yield point. The code comment says it exactly:
  without this await, the spinner would block the entire loop (sample
  8's rude coroutine). The spinner is polite 12 times a second.
- Cancellation (81-90, 96-110): `stop()` calls `self._task.cancel()`
  -- sets the flag (sample 4 step 1). The CancelledError lands at the
  `await asyncio.sleep` inside `_spin`; the `except
  asyncio.CancelledError:` handler (81) erases the spinner line --
  writes `\r`, spaces over the widest label it ever drew, `\r` again
  -- so the next print starts on a clean line. Then `stop()` does
  `await self._task` inside try/except (106-109): it WAITS for that
  cleanup to finish before returning, swallowing the re-raised
  CancelledError because the cancellation was intentional. If stop()
  skipped the await, the "Assistant:" header could print before the
  spinner erased itself -- a garbled line, and a bug you could only
  understand with sample 4's two-step model.
- `__aenter__/__aexit__` (112-117): sample 7 -- `async with Spinner()`
  guarantees the erase-on-exit even when the body raises.

The label mechanics (`update`, `_max_len`) are ordinary Python --
tracking the widest message so shorter labels fully overwrite longer
ones. The async machinery is those three moves: create_task, the
polite sleep, cancel-then-await.

## 14. How the spinner and the tokens share one thread

Put sections 11-13 together and one turn looks like this on the
single thread (time flows down; only ONE column is ever running):

```
_stream_response                      spinner._spin
--------------------------------     -------------------------
await spinner.start()             ->  task created
async for event in astream_events
  (graph awaits Azure OpenAI...)      frame, await sleep(0.08)
  ...loop idle during network...      frame, await sleep(0.08)
  ...                                 frame, await sleep(0.08)
event: on_chain_start (router)        (paused at its sleep)
  spinner.update("Routing")
  (graph awaits the LLM again...)     frame "Routing", sleep...
event: on_chat_model_stream "Acc"
  first token! spinner.stop()     ->  CancelledError at sleep,
  (await: cleanup runs NOW)           erase line, task ends
  print header + token
event: on_chat_model_stream "ess"
  print token
  ...tokens as the LLM streams...
finally: spinner.stop() (no-op), close file
```

Every context switch in that diagram happens at an `await` -- the
graph's network waits are what donate time to the spinner, and the
spinner's 0.08s sleeps are what hand time back. Nothing here is
threads; it is one loop interleaving two politely-written coroutines.
When your 46-second turn shows "agent nodes 46s", that is the
`ainvoke` awaits -- and the reason the terminal stayed responsive the
whole time is every line of this section.

## 15. The same concepts in the API layer

`agent_api.py` consumes the SAME `astream_events` generator, with one
difference that closes the loop on section 6: instead of printing
tokens, its `stream()` is ITSELF an async generator -- it `yield`s
`AgentEvent` objects, which the SSE endpoint `async for`s over and
writes to the HTTP response. Async generators compose: LangGraph's
stream feeds the service's stream feeds the network. A slow browser
simply slows the awaits down; the loop keeps serving other sessions.

Also there: the per-session `asyncio.Lock` (sample 8 verbatim -- a
turn is a multi-await critical section; see agent_api.md section 9),
the TTL sweeper as a long-lived background task (sample 3, started in
the app's lifespan), and in Redis mode (P3.1) every store operation
is awaited through redis-py's asyncio client -- the golden rule
applied to a new dependency: session state moved out of process, and
none of its network calls block the loop.
