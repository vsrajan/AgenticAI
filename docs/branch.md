# The branch graph

Verified against the repository on 2026-09-15.

**It is a stack, not a tree.** Eleven branches in one straight line,
each based on the tip of the one before it. There is no fork anywhere in
the working set: every branch's tip is a strict ancestor of the next.
That is why `git log` on any branch shows all the work below it, and why
a fix made low in the stack has to be carried up by rebasing rather than
merging.

**Nothing is merged.** `claude/mcp-html-docs-server-S9jg9` is the default
branch and it sits at the BOTTOM: 52 commits, while the tip of the stack
has 121. The other 69 commits -- every performance, deployment, data and
agent change on this list -- exist only on branches.

## Contents

1. [The graph](#1-the-graph)
2. [Branch by branch](#2-branch-by-branch)
3. [How the cascade works](#3-how-the-cascade-works)
4. [Two strays](#4-two-strays)
5. [Working with the stack](#5-working-with-the-stack)

## 1. The graph

```
  (oauth docs, never merged, 2026-03)
   ba506f4 claude/oauth-documentation-2exlpm
      |
      +-- forked at bc972ea
      |
  ====v=============================================================
   claude/mcp-html-docs-server-S9jg9   DEFAULT, 52 commits    <- bottom
      |   the agent itself: StateGraph, 3 specialists, MCP tools,
      |   HTTP API, web client, prompt hardening
      v
   streamable-http                     +1
      |   default MCP transport sse -> streamable-http
      v
   Performance-P0                      +8
      |   CsvStore onto DuckDB, data_sources.py, per-tool timing
      v
   P1.1                                +15
      |   page-level PDF retrieval; async/langgraph/bm25/duckdb tutorials
      v
   P3.1                                +7
      |   Redis session state (checkpointer, registry, distributed locks)
      v
   Vector                              +2
      |   vector.md -- hybrid search design, no code
      v
   AKS-DEPLOY                          +5
      |   Dockerfiles, the Helm chart, four values files, GitLab CI
      v
   denorm                              +3
      |   denormalized Entitlements export; column projection; grouped counts
      v
   denorm_bugfix                       +4
      |   two csv_store bugs that returned wrong answers silently
      v
   stdio                               +23
      |   ONE container: the agent spawns the MCP server over stdio.
      |   /livez supervision, combined image, podman.md
      v
   ResourceAgentOnly                   +1                     <- tip
          resource agent only, sessions in memory, one pod
```

## 2. Branch by branch

Read bottom-up: each row is based on the row above it.

| # | Branch | Based on | Adds | What it owns |
|---|---|---|---|---|
| 1 | `claude/mcp-html-docs-server-S9jg9` | -- (root, **default**) | 52 | The agent: router + 3 specialists, 12 MCP tools, the HTTP API, auth, web client, session hygiene, the ROUTER/RESOURCE prompt hardening |
| 2 | `streamable-http` | 1 | 1 | Default MCP transport moves sse -> streamable-http; `MCP_STATELESS_HTTP` so replicas can sit behind a load balancer |
| 3 | `Performance-P0` | 2 | 8 | P0: CsvStore reworked onto embedded DuckDB, pluggable `data_sources.py`, size-gated BM25, per-tool `took=ms`, per-node timing. Adds `ParquetDataSource` |
| 4 | `P1.1` | 3 | 15 | Page-level PDF retrieval (BM25 per page, `read_page(pages=...)`). Also the four tutorials: async, langgraph, bm25, duckdb |
| 5 | `P3.1` | 4 | 7 | Redis session state: plain-Redis `RedisSaver`, session registry, `SET NX PX` turn locks. **Implemented but not deployable -- no Redis on the infra** |
| 6 | `Vector` | 5 | 2 | `vector.md` only -- the hybrid lexical+vector search design. No code |
| 7 | `AKS-DEPLOY` | 6 | 5 | The deployment artifacts: two Dockerfiles, one Helm chart, four values files, `.gitlab-ci.yml`, `aks_guide.md` |
| 8 | `denorm` | 7 | 3 | ResourceName/Description inline on Entitlements; `columns=[...]` projection; `count_by_column` over a column LIST. Cuts a round-trip from every answer |
| 9 | `denorm_bugfix` | 8 | 4 | Two silent-wrong-answer bugs in `csv_store`: a shared DuckDB handle across threads, and filters on unknown columns being dropped |
| 10 | `stdio` | 9 | 23 | ONE container, agent spawns the MCP server over stdio (no auth layer to build). Held session, stderr logging, `/livez`, combined Dockerfile, `podman.md` |
| 11 | `ResourceAgentOnly` | 10 | 1 | **Tip.** Resource agent only, sessions in memory, one pod. See `single-agent.md` |

Each branch's design doc, where it has one: `docs/P0.md`, `P1.1.md`,
`P3.1.md`, `vector.md`, `deploy.md`, `aks.md`, `single-container-stdio.md`,
`podman.md`, `single-agent.md`.

## 3. How the cascade works

Fixes that belong to the agent itself -- prompt changes, mostly -- land
on the DEFAULT branch, at the bottom. They then have to reach the tip,
which means rebasing every branch above in order:

```bash
prev=claude/mcp-html-docs-server-S9jg9
for b in streamable-http Performance-P0 P1.1 P3.1 Vector \
         AKS-DEPLOY denorm denorm_bugfix stdio ResourceAgentOnly; do
  git checkout "$b" && git rebase "$prev" || break   # stop on conflict
  prev="$b"
done
```

That has been done twice (the ROUTER_PROMPT hardening and the
RESOURCE_PROMPT rework, both 2026-09-08 -- which is why nine branches
share that tip date). The full test matrix is re-run at each step,
because a rebase can leave a branch's code fine and its tests stale.

**Author dates look wrong, and are not.** The per-branch date spans
overlap -- `P1.1` shows commits from 2026-07-11 while `streamable-http`
below it shows 2026-07-14 -- because rebasing preserves author dates and
only moves committer dates. Order the stack by ancestry, never by date.

Verify the stack is still a clean line at any time:

```bash
prev=""
for b in claude/mcp-html-docs-server-S9jg9 streamable-http Performance-P0 \
         P1.1 P3.1 Vector AKS-DEPLOY denorm denorm_bugfix stdio \
         ResourceAgentOnly; do
  [ -n "$prev" ] && { git merge-base --is-ancestor "$prev" "$b" \
    && echo "ok   $prev -> $b" || echo "FORK $prev -> $b"; }
  prev=$b
done
```

## 4. Two strays

**`origin/claude/oauth-documentation-2exlpm`** -- one commit, "Add OAuth
2.0 authorization code grant flow documentation", forked 2026-03-15 from
`bc972ea` early in the default branch's history. It is NOT an ancestor of
the stack, so its content has never reached any other branch. Remote
only. Either fold that document in (it overlaps `entra_auth_guide.md`) or
delete the branch -- leaving it is the thing that makes the branch list
look like it has a fork in it.

**`worktree-wf_09baa9b1-2e2-2`** -- local only, created by a tooling
worktree still checked out at
`.claude/worktrees/wf_09baa9b1-2e2-2`. Its tip `b2aeba6` "Fix two
csv_store bugs that returned wrong answers silently" is already
contained in `denorm_bugfix`, so the branch carries nothing unique:

```bash
git worktree remove .claude/worktrees/wf_09baa9b1-2e2-2
git branch -D worktree-wf_09baa9b1-2e2-2
```

## 5. Working with the stack

**Where to branch from.** New work goes on top of the tip
(`ResourceAgentOnly`) unless it genuinely belongs lower. Branching lower
means every branch above needs a rebase to see it.

**What the default branch is missing.** All of it -- transports,
performance, data path, deployment, stdio, the single-agent work. Do not
read `claude/mcp-html-docs-server-S9jg9` to understand what the system
does today; read the tip.

**Why it was never merged.** Each branch was a reviewable unit of work
with its own design doc, and the stack let later work start before
earlier work was signed off. The cost is this document. If the stack is
going to keep growing, the cheaper end state is to merge the settled
lower branches (everything through `stdio` has been superseded rather
than contradicted) and keep only the unsettled ones stacked.

**Deleting a branch is safe only from the top.** Because every branch is
an ancestor of the next, deleting `P3.1` does not delete its commits --
they are still in `ResourceAgentOnly`. What is lost is the NAME, which is
the only thing recording where that unit of work started and stopped.
