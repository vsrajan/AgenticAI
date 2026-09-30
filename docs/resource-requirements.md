# Resource Agent -- business requirements

What the Resource specialist is for, stated against the capability that
exists today on branch `stdio`. Written 2026-09-30.

This is deliberately a requirements document and not a design one: it
says what the agent must do for a user, and section 3 says which of
those things a conventional lookup application could not do at all.
That distinction is the justification for the LLM being in the path,
and it is narrower than it first appears -- section 4 is honest about
where the LLM adds nothing.

## Contents

1. [The requirement](#1-the-requirement)
2. [Where each capability lives](#2-where-each-capability-lives)
3. [What the LLM makes possible](#3-what-the-llm-makes-possible)
4. [What the LLM does not add](#4-what-the-llm-does-not-add)

## 1. The requirement

The Resource Agent answers access-entitlement questions for existing
staff and new joiners against two datasets: entitlement assignments
(one row per person-to-right grant, carrying the holder's job title,
business hierarchy and financial unit) and the resource catalogue. It
must serve four questions -- what access a named person holds, given
their GPN; what access comparable people hold, given a job title plus
one scope term; which right grants a described capability; and, on
request, submission of an access request. It must accept these in the
user's own words with partial or approximate terms, and resolve those
terms to exact data values itself rather than requiring the user to
supply them, asking a question only when a needed dimension is entirely
absent. Every right must be presented with its name, owning system and
description rather than a bare identifier, and the agent must
distinguish access a peer group holds in common from an individual's
personal extras. It must keep the business hierarchy (area > sector >
segment > function) separate from the financial hierarchy (OU), never
mixing or broadening across the two, and must decline questions outside
access governance.

## 2. Where each capability lives

Every requirement above is implemented in `RESOURCE_PROMPT` in
`agent-client/src/ease_clients/utils/agnes_agent_graph.py`, against the
schema-agnostic MCP tools in `mcp-server/src/mcp_docs_server/server.py`.
Nothing in this document describes work still to be done.

| Requirement | Implementation |
|---|---|
| Access held by a named person | Strategy 0 -- exact `EMPLOYEEID` filter, one call, projecting id, name, system and description |
| Access held by comparable people | Strategy 1 -- `count_by_column` grouped by `[ResourceID, ResourceName]`, ranked by peer count |
| Resolving a partial term to real values | Step R -- fuzzy `count_by_column` per candidate column, with outcome rules for 1 / 2-10 / >10 / 0 matches |
| Which right grants a capability | Strategy 2 -- BM25 search over the catalogue, which also covers rights nobody currently holds |
| Exploring the organisation | Strategy 3 -- distinct values and fuzzy counts over the hierarchy columns |
| Raising a request | Strategy 4 -- `get_request_attributes` then `raise_entitlement_request` (placeholder implementation) |
| Names and descriptions, never bare ids | Output section, plus the rule against showing a `ResourceID` without its name |
| Common set vs personal extras | Strategy 0's peer-widening offer, which is offered and never run unasked |
| Business and financial hierarchies kept apart | Dataset section and the broadening rule in Strategy 1(d) |
| Declining out-of-scope questions | The SCOPE block, on branch `prompt_hardening` |

The last row is the one exception: scope refusal is built but sits one
commit above this branch. Everything else is on `stdio`.

## 3. What the LLM makes possible

A conventional application can do the lookups. `GPN -> rights` is a
join; `job title + segment -> ranked rights` is a `GROUP BY`. The LLM
earns its place in the gap between what a user can say and what a query
needs.

**Resolving language to data values.** "I'm an engineer in risk"
becomes confirmed values for `JOBTITLE` and a hierarchy column, with
candidate matches and their population counts offered back for
confirmation. A form demands the exact string up front, which means the
user must already know the data dictionary in order to use the tool.

**Not knowing which field your word belongs to.** Given "TISO", a form
makes you choose whether that is a sector, a segment or a function
before it will search. The agent probes each business-hierarchy column
in turn and reports what matched where. This alone disqualifies the
form for anyone who does not already work with the taxonomy daily.

**Choosing the query shape from a sentence.** "Same access as GPN
1234", "what do people on my team have", and "what grants access to the
trade blotter" are three different execution plans -- exact filter,
grouped count, free-text search. A conventional application makes the
user pick the right screen first, and picking wrong returns an empty
result with no indication why.

**Recovering from a thin result correctly.** Too few peers at function
level, so widen to segment, then sector, then area -- while never
crossing into the financial hierarchy, where the population would look
superficially similar and mean something entirely different. A
conventional application returns "no results" and stops.

**Conducting the new-joiner conversation.** A joiner has no assignments
of their own, so their own GPN returns nothing useful, and they rarely
know their exact job title or segment. The agent pivots to a
colleague's GPN, which yields both the answer and the peer criteria
from a single number. There is no form field for "ask a different
question instead".

**Interpreting the distribution, not just returning it.** Separating
what a role commonly holds from one colleague's unusual extras is the
difference between a defensible request and copying someone's
privileges by accident. That is a judgement about the counts, not a row
in them.

## 4. What the LLM does not add

Stated plainly, because a requirements document that overclaims gets
taken apart in the first review.

The retrieval is ordinary database work. For a user who already knows
the exact GPN, or the exact job title and segment, a purpose-built
screen would be faster, cheaper and more predictable than a model turn
-- a filtered group-by returns in about 25ms, while a turn is dominated
by seconds of LLM time (`docs/P0.md`).

The LLM also adds failure modes a form does not have: it can choose the
wrong strategy, and every answer is non-deterministic in wording even
when the underlying data is fixed. The controls for that are the
prompt's verification rules and, eventually, the evaluation harness
recorded as the first enterprise gap in `CLAUDE.md`.

The case for the LLM is not that it queries better. It is that most
users cannot state their question as an exact filter, and every one of
those questions is one the conventional application cannot accept at
all.
