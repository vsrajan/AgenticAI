# The branch graph

Verified against the repository on 2026-09-17.

**It was a straight line. It is not any more.** Ten branches still sit in
one stack, each based on the tip of the one before it, but the top has
FORKED: `ResourceAgentOnly` and `stdio` share history up to `c52a46d`
and have diverged since. `prompt_hardening` then branched off the `stdio`
side. So there are two heads, not one tip, and "rebase everything in
order" is no longer a single loop.

**Nothing is merged.** `claude/mcp-html-docs-server-S9jg9` is the default
branch and it sits at the BOTTOM: 50 commits, while the longest head has
123. The other 73 commits -- every performance, deployment, data, agent
and prompt change on this list -- exist only on branches.

## Contents

1. [The graph](#1-the-graph)
2. [Branch by branch](#2-branch-by-branch)
3. [The fork, and what it costs](#3-the-fork-and-what-it-costs)
4. [How the cascade works](#4-how-the-cascade-works)
5. [Strays](#5-strays)
6. [Working with the stack](#6-working-with-the-stack)

## 1. The graph

```
  (oauth docs, never merged, 2026-03)
   ba506f4 claude/oauth-documentation-2exlpm
      |
      +-- forked at bc972ea
      |
  ====v=============================================================
   claude/mcp-html-docs-server-S9jg9   DEFAULT, 50 commits    <- bottom
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
   stdio, first 23 commits                             118 total
      |   ONE container: the agent spawns the MCP server over stdio.
      |   /livez supervision, combined image, podman.md
      |
      +-- c52a46d "Build against an internal package index"  <- FORK
      |
      +----------------------------+
      |                            |
      v                            v
   stdio tip            +4      ResourceAgentOnly       +2      HEAD B
      |   podman.md Redis           resource agent only, sessions
      |   container rig;            in memory, one pod; this document
      |   two RESOURCE_PROMPT       (120 commits)
      |   fixes (122 commits)
      v
   prompt_hardening     +1                                      HEAD A
          GUARDRAILS in all four prompts: scope allowlist,
          tool-results-are-data, non-disclosure (123 commits)
```

## 2. Branch by branch

Rows 1-10 read bottom-up: each is based on the row above it. Rows 11 and
12 both descend from `c52a46d`, partway through row 10.

| # | Branch | Based on | Adds | What it owns |
|---|---|---|---|---|
| 1 | `claude/mcp-html-docs-server-S9jg9` | -- (root, **default**) | 50 | The agent: router + 3 specialists, 12 MCP tools, the HTTP API, auth, web client, session hygiene, the ROUTER/RESOURCE prompt hardening |
| 2 | `streamable-http` | 1 | 1 | Default MCP transport moves sse -> streamable-http; `MCP_STATELESS_HTTP` so replicas can sit behind a load balancer |
| 3 | `Performance-P0` | 2 | 8 | P0: CsvStore reworked onto embedded DuckDB, pluggable `data_sources.py`, size-gated BM25, per-tool `took=ms`, per-node timing. Adds `ParquetDataSource` |
| 4 | `P1.1` | 3 | 15 | Page-level PDF retrieval (BM25 per page, `read_page(pages=...)`). Also the four tutorials: async, langgraph, bm25, duckdb |
| 5 | `P3.1` | 4 | 7 | Redis session state: plain-Redis `RedisSaver`, session registry, `SET NX PX` turn locks. **Implemented but not deployable -- no Redis on the infra** |
| 6 | `Vector` | 5 | 2 | `vector.md` only -- the hybrid lexical+vector search design. No code |
| 7 | `AKS-DEPLOY` | 6 | 5 | The deployment artifacts: two Dockerfiles, one Helm chart, four values files, `.gitlab-ci.yml`, `aks_guide.md` |
| 8 | `denorm` | 7 | 3 | ResourceName/Description inline on Entitlements; `columns=[...]` projection; `count_by_column` over a column LIST. Cuts a round-trip from every answer |
| 9 | `denorm_bugfix` | 8 | 4 | Two silent-wrong-answer bugs in `csv_store`: a shared DuckDB handle across threads, and filters on unknown columns being dropped |
| 10 | `stdio` | 9 | 27 | ONE container, agent spawns the MCP server over stdio (no auth layer to build). Held session, stderr logging, `/livez`, combined Dockerfile, `podman.md` incl. the Redis-in-a-container rig. Last 2 commits are RESOURCE_PROMPT fixes: the routing sentence firing on data-only questions, and descriptions on a GPN lookup |
| 11 | `ResourceAgentOnly` | `c52a46d`, inside 10 | 2 | **Head B.** Resource agent only, sessions in memory, one pod. See `single-agent.md`. Also where this document was written |
| 12 | `prompt_hardening` | 10 (tip) | 1 | **Head A.** `GUARDRAILS` constant in all four prompts: scope allowlist with one-sentence refusal, tool-results-are-data (indirect injection), non-disclosure |

Each branch's design doc, where it has one: `docs/P0.md`, `P1.1.md`,
`P3.1.md`, `vector.md`, `deploy.md`, `aks.md`, `single-container-stdio.md`,
`podman.md`, `single-agent.md`.

## 3. The fork, and what it costs

`ResourceAgentOnly` was cut from `stdio` at `c52a46d` on 2026-09-15.
`stdio` then kept moving -- four more commits -- so the two have diverged:

```
                      +-- 25a2782 8374ca5 fc3024a de574b1 --> stdio, prompt_hardening
c52a46d --------------+
                      +-- 017bed2 e4cbbbc ----------------> ResourceAgentOnly
```

`git merge-base --is-ancestor stdio ResourceAgentOnly` now fails, which
is the machine-checkable version of the same statement.

What this changes:

- **`ResourceAgentOnly` does not have the last four `stdio` commits.** It
  is missing the containerized-Redis rig in `podman.md` and both
  RESOURCE_PROMPT fixes. A GPN lookup on that branch still ends with the
  spurious "ask me separately about the documentation part" line and
  still omits resource descriptions.
- **There is no single tip to branch from any more.** New work has to
  pick a side, and the sides disagree about what the prompts say.
- **The cascade loop in section 4 cannot be one loop.** It has to run the
  common part once, then each head separately.

The cheap fix, if the divergence is not wanted, is to rebase
`ResourceAgentOnly` onto the current `stdio` tip: its two commits are
small and touch different files from the four on the `stdio` side, so it
should replay without conflict. That is a decision, not a cleanup --
leave it alone if `ResourceAgentOnly` is deliberately frozen as a
demo/POC cut.

## 4. How the cascade works

Fixes that belong to the agent itself -- prompt changes, mostly -- land
on the DEFAULT branch, at the bottom. They then have to reach both heads,
which means rebasing every branch above in order:

```bash
# common trunk, bottom to the stdio tip
prev=claude/mcp-html-docs-server-S9jg9
for b in streamable-http Performance-P0 P1.1 P3.1 Vector \
         AKS-DEPLOY denorm denorm_bugfix stdio; do
  git checkout "$b" && git rebase "$prev" || break   # stop on conflict
  prev="$b"
done

# then each head, separately
git checkout prompt_hardening   && git rebase stdio
git checkout ResourceAgentOnly  && git rebase stdio   # see section 3 first
```

The trunk part has been done twice (the ROUTER_PROMPT hardening and the
RESOURCE_PROMPT rework, both 2026-09-08 -- which is why nine branches
share that tip date). The full test matrix is re-run at each step,
because a rebase can leave a branch's code fine and its tests stale.

**Author dates look wrong, and are not.** The per-branch date spans
overlap -- `P1.1` shows commits from 2026-07-11 while `streamable-http`
below it shows 2026-07-14 -- because rebasing preserves author dates and
only moves committer dates. Order the stack by ancestry, never by date.

Verify the shape at any time. The trunk should be all `ok`, and the last
two lines confirm the fork is where you think it is:

```bash
prev=""
for b in claude/mcp-html-docs-server-S9jg9 streamable-http Performance-P0 \
         P1.1 P3.1 Vector AKS-DEPLOY denorm denorm_bugfix stdio; do
  [ -n "$prev" ] && { git merge-base --is-ancestor "$prev" "$b" \
    && echo "ok   $prev -> $b" || echo "FORK $prev -> $b"; }
  prev=$b
done
git merge-base --is-ancestor stdio prompt_hardening \
  && echo "ok   stdio -> prompt_hardening" || echo "FORK stdio -> prompt_hardening"
git merge-base --is-ancestor stdio ResourceAgentOnly \
  && echo "ok   stdio -> ResourceAgentOnly" || echo "FORK stdio -> ResourceAgentOnly"
```

**About this file.** It lives on the DEFAULT branch only, deliberately.
Putting a copy on every branch was tried on 2026-09-17 and reverted
within the hour: a separate commit per branch means no branch is an
ancestor of the next, and the verify loop above went from nine `ok`
lines to thirteen `FORK` lines. The map cannot break the thing it
documents.

Read it from wherever you happen to be checked out:

```bash
git show claude/mcp-html-docs-server-S9jg9:docs/branch.md | less
```

It rides up the stack on the next cascade like any other default-branch
change, which is why the default branch currently sits one commit ahead
of the rest.

## 5. Strays

**`origin/claude/oauth-documentation-2exlpm`** -- one commit, "Add OAuth
2.0 authorization code grant flow documentation", forked 2026-03-15 from
`bc972ea` early in the default branch's history. It is NOT an ancestor of
the stack, so its content has never reached any other branch. Remote
only. Either fold that document in (it overlaps `entra_auth_guide.md`) or
delete the branch.

**`worktree-wf_09baa9b1-2e2-2`** -- local only, created by a tooling
worktree at `.claude/worktrees/wf_09baa9b1-2e2-2`. Its tip `b2aeba6` was
already contained in `denorm_bugfix`, so the branch carried nothing
unique. It does not exist in a fresh clone; check your own working copy:

```bash
git worktree list
git worktree remove .claude/worktrees/wf_09baa9b1-2e2-2
git branch -D worktree-wf_09baa9b1-2e2-2
```

## 6. Working with the stack

**Where to branch from.** Pick a head deliberately -- there are two.
`prompt_hardening` carries everything; `ResourceAgentOnly` is two commits
off an older `stdio`. Branching lower than a head means every branch
above needs a rebase to see the new work.

**What the default branch is missing.** All of it -- transports,
performance, data path, deployment, stdio, the single-agent work, the
prompt hardening. Do not read `claude/mcp-html-docs-server-S9jg9` to
understand what the system does today; read a head.

**Why it was never merged.** Each branch was a reviewable unit of work
with its own design doc, and the stack let later work start before
earlier work was signed off. The cost is this document, and now a fork.
If the stack is going to keep growing, the cheaper end state is to merge
the settled lower branches (everything through `denorm_bugfix` has been
superseded rather than contradicted) and keep only the unsettled ones
stacked.

**Deleting a branch is safe only from the top.** Because every branch on
the trunk is an ancestor of the next, deleting `P3.1` does not delete its
commits -- they are still in `stdio`. What is lost is the NAME, which is
the only thing recording where that unit of work started and stopped.
This is no longer true above `c52a46d`: deleting `ResourceAgentOnly`
WOULD lose its two commits, because nothing else contains them.
