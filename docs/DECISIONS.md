# Decision log (ADRs)

Architecture Decision Records — one entry per non-obvious choice: context,
decision, consequences. Kept short.

---

## ADR-022 — The SQL guardrail validates through DuckDB's own parser

**Status:** Accepted · 2026-07-30 · supersedes the scanner half of ADR-003

**Context.** An external review of this repo found that `sql_guard` accepted
`SELECT * FROM (SELECT * FROM secrets) t`: the relation allow-list never looked
inside a derived table. Patching that scan hole led to a three-round adversarial
review by an instance with no knowledge of the design, whose only job was to
break the guarantee. It found progressively worse things, and the pattern was
always the same class of defect — **the hand-written scanner and DuckDB
disagreed about what the query said.**

What that disagreement actually permitted, all verified end-to-end against a
real connection, not argued on paper:

- `_mask_literals` modelled `'…'` but not dollar-quoting (`$a$…$a$`), escape
  strings (`e'\''`) or quoted identifiers containing an apostrophe. Any of the
  three shifted quote parity, so **the entire rest of the statement was blanked
  out of every check** while the original text — unblanked — was what executed.
  That read `duckdb_settings()`, the ledger's own file path, and tables outside
  the allow-list.
- The same desync hid `;` from the statement splitter. Closing the guard's own
  `SELECT * FROM (…)` wrapper early and reopening it smuggled extra statements
  through, and `CREATE TEMP TABLE` / `TEMP VIEW` / `TEMP MACRO` / `PREPARE` all
  **succeeded on a `read_only=True` connection** and persisted for the session.
- A CTE named after a table function whitelisted that function
  (`WITH duckdb_settings AS (…) SELECT … FROM duckdb_settings()`).
- `"current_setting"('memory_limit')` — a quoted function name — was invisible
  to the regex that looked for `name(`.

**Decision.** Stop scanning text. Parse the statement with **the same parser
that will execute it** (`duckdb.extract_statements` for statement boundaries,
`json_serialize_sql` for the tree) and run every check over the resulting
syntax tree. Parser-versus-executor disagreement is not reduced by this, it is
structurally impossible.

Four checks over the tree, each one shaped by an attack that beat its
predecessor:

1. **Exactly one statement**, established by the parser, not by counting `;`.
   The wrapped output is re-parsed too, so the wrapper cannot be escaped.
2. **Read-only by construction.** `json_serialize_sql` serializes only SELECT
   statements; every other kind returns an error. That replaces a keyword
   deny-list that had to guess.
3. **Relations**: any node carrying a `table_name` is a relation reference —
   not just `BASE_TABLE`. `SHOW`/`DESCRIBE` parse as select statements whose
   source is a `SHOW_REF` carrying the same field, so keying on the node type
   let `SHOW ALL TABLES` enumerate the catalog past two empty allow-lists.
   Catalog listings (no query sub-tree) are refused outright; `DESCRIBE x` /
   `SUMMARIZE x` carry their target as a real sub-tree and are checked like any
   other reference.
4. **Functions**: an allow-list of canonical names, because a deny-list is
   blind to every function nobody remembered and DuckDB ships new table
   functions every release. CTE names never exempt a function.

**CTE scoping is lexical, and a CTE never covers itself.** A flat set of CTE
names let one declared inside a nested subquery exempt that table name in a
*sibling* branch; walking the `WITH` definitions with the full set in scope let
a *forward-declared* name, and a CTE's *own* name, exempt a real table. Each
definition is now validated against only the CTEs declared before it.

A CTE's own name is never in scope in its own body, `RECURSIVE` or not. This is
measured, not cautious: on DuckDB 1.5.3 the **anchor** term of
`WITH RECURSIVE internal_notes AS (SELECT secret FROM internal_notes UNION ALL …)`
binds to the base table and returns its rows. The exemption cannot be made safe
by restricting it to the recursive form.

**Correction (2026-07-30, from the claim audit).** An earlier draft of this ADR
said "recursive CTEs are refused". That is **not what the code does**, and the
imprecision matters. There is no recursion-specific rule at all: a self-reference
is simply checked against the relation allow-list like any other name. So
`WITH RECURSIVE t AS (… FROM t …)` is refused because `t` is not an allow-listed
relation, while `WITH RECURSIVE invoices AS (… FROM invoices …)` is **accepted** —
`invoices` is on the list, and the anchor binding to the real table reads data the
agent is allowed to read anyway. The accurate statement is: **a recursive CTE works
only when its name is an allow-listed relation, and is refused otherwise.**

**Schema qualification never consults the CTE set.** CTEs cannot be
schema-qualified, so `main.internal_notes` must clear the allow-list on its own.
The previous approach compared a synthesised `"schema.table"` string against CTE
names, which was defeated by quoting a dot into a CTE name
(`WITH "main.internal_notes" AS (…)`).

**Amendment (2026-07-30, from a blind code audit) — catalog qualification is
refused outright.** The walk read `table_name` and `schema_name` and never read
`catalog_name`, the third field a `BASE_TABLE` node carries. In a three-part name
the database lands in `catalog_name` and `main` lands in `schema_name`, so the
schema check saw an allow-listed schema, fell through, and only the bare name was
ever tested:

```
SELECT * FROM evildb.main.customers               -> was ACCEPTED
SELECT * FROM "/tmp/other.duckdb".main.customers  -> was ACCEPTED
```

Latent, not live: `ATTACH` does not serialize as a select statement and the
ledger connection carries `enable_external_access=false` with
`lock_configuration=true`, so no second catalog was reachable. What it did cost
immediately is the thing the ADR-003 correction below is about — the
confidentiality half of the guard was standing on one layer again.

The choice is to refuse **every** catalog-qualified reference rather than
allow-list catalog names. A catalog allow-list would have to contain the ledger's
own catalog, which is named after its file (`data/ledger.duckdb` → `ledger`) —
that pins a deployment's file name into the guard and creates a second thing to
keep in sync. With exactly one database open and nothing attachable, a three-part
name is never needed to reach the data. The price is that `ledger.main.customers`
— a true name for a real table — is refused; the refusal names the full path, so
it is diagnosable. If it ever costs a real query, the repair is a schema hint that
stops the model writing three-part names, not an allow-list of catalogs.

Measured by mutation, on the suite as it stands: deleting the branch turns **8**
tests red; replacing it with an allow-list that admits `memory` and `ledger` turns
**2** red; a branch that refuses but does not say *which* catalog turns **7** red —
and only **1** if the message assertion is removed along with it, which is what
that assertion is there to hold.

**Amendment (2026-07-30, same audit) — the executed statement is printed from
the validated tree, not copied from the caller.** The guard validated a *tree*
and executed a *string*, and the gap between them was bridged by hand: a loop
peeled trailing separators off the caller's text so that pasting it into
`SELECT * FROM (…) LIMIT n` would still parse. It peeled only what was literally
last, so a semicolon followed by anything survived into the wrapper and died
there:

```
SELECT count(*) FROM invoices; -- total
  -> was REFUSED: Parser Error: syntax error at or near ";"
     LINE 2: SELECT count(*) FROM invoices; -- total
```

A trailing comment is something a language model writes constantly, and the
refusal it got back was a syntax error about line 2 of a text it never wrote.
The repair is not a better peeling loop — that is the scanner mistake one level
down. `json_deserialize_sql` asks DuckDB to print the statement back from the
tree that just passed every check, and **that** is what gets wrapped. Comments
and separators are lexical: they never reach a tree, so they cannot come out of
a print. The printed text is parsed again and its tree compared with the
validated one; a mismatch is refused rather than executed, so a printer bug in a
future DuckDB release costs a query instead of silently making the statement
that runs differ from the statement that was checked. The caller's text now
reaches nothing but the parser.

Round-trip fidelity was measured before adopting it, not assumed: over every
payload both guard suites accept, **27 of 27** print-and-reparse to a tree
identical to the original once `query_location` byte offsets are dropped, with
identical collected relation and function names, and the print is idempotent.

**Two operator spellings were refused while the same operations were allowed as
calls.** `SELECT name || ' x'` was rejected on `||` while `concat(name, ' x')`
passed, and `^` was missing against `pow` / `power`, which are both listed. This
is the cost of enumerating spellings by hand, and it is paid by the product:
string concatenation is the most common idiom in a collections report. Both are
now on the list. Neither adds capability — every operator-named function in
DuckDB's catalog is `function_type = 'scalar'` (28 names on 1.5.3), so none can
read a file or reach the catalog. The remaining 24 stay off the list because no
receivables query needs a bit shift, and refusal is the cheap direction.

Measured by mutation on the resulting suite (291 tests): removing `||` and `^`
turns **6** red; wrapping the caller's text again turns **10** red; restoring
the peeling loop turns **1** red; adding the rest of the operator family — the
mutation that *relaxes* rather than removes — turns **1** red. Two results
recorded because they are the honest ones: dropping the fixed-point comparison
turns only **1** red, and that test has to fake a lying printer to get there,
because DuckDB 1.5.3 round-trips every real payload exactly; and the branch that
rephrases a wrapper failure is unreachable by any payload once the wrapped text
is generated, so its test fakes a broken normalizer too. Both are insurance
against a future release or refactor, and neither can be proved by a query.

**Amendment (2026-07-30, same audit) — the allow-lists are checked against a
surface, and `guard_query`'s own arguments are part of the policy.** The audit's
third finding was not a bypass. It was that three things the guard depends on
were not pinned by any test, each demonstrated by a mutation that left the
then-291-test suite entirely green:

```
+ read_text, read_blob, sniff_csv  to ALLOWED_FUNCTIONS  -> 291 green
- payments, communications       from ALLOWED_RELATIONS  -> 291 green
+ information_schema, pg_catalog   to ALLOWED_SCHEMAS    -> 291 green
```

Every test proved that a name *absent* from a list is refused; none proved the
lists hold the right names, and three of the nine allow-listed relations appeared
in no payload at all. This is the mutation shaped like a real pull request — it
*relaxes* the policy rather than deleting a check — and enumerating three more
counter-examples would have left the same hole one name wider. So the lists are
now compared against a full surface: the function allow-list against **every**
table function in `duckdb_functions()` (five row generators named as the only
exceptions, so a file reader added by a future release or by hand is caught on
sight), and the relation allow-list against the ledger's own catalog, partitioned
into what policy allows and what it forbids. The schema list is pinned by
payloads whose *bare* name is allow-listed (`information_schema.customers`), the
only form that only the schema check can refuse.

**`guard_query` promised `GuardrailError` "on any violation" and five inputs
escaped it.** `guard_query(123)` raised `AttributeError`, `max_rows="5"`
`TypeError`, `max_rows=float("inf")` `OverflowError` — and `tools.py` and
`mcp_server/server.py` catch `GuardrailError` and `duckdb.Error` only, so all
three left the tool as a traceback rather than a refusal the model could act on.
Two more did not raise at all: `max_rows=2.9` truncated to `LIMIT 2` in silence
(the `int()` cast in the wrapper was doing that), and `max_rows=10**30` produced
a LIMIT with thirty zeroes — no row cap, on the surface whose whole job is to
cap. The arguments are now validated like any other input, and the guard carries
its own `MAX_ROWS_CEILING`. `Settings.max_rows` already had `le=10_000`, but that
bounds the *environment variable*; `plan_replay`, the MCP server and every test
pass their own number straight through. The number is deliberately written twice
— `sql_guard` imports nothing from the app, because it is the security surface —
and a test compares the two bounds so the copies cannot drift.

**A deeply nested query crashed instead of being refused.** `SELECT 1+1+1+…` at
494 terms — 996 characters, nothing a model would hesitate to emit — exhausted
Python's stack inside the tree walk and raised `RecursionError`. DuckDB's parser
has a depth limit of its own but only trips at 1000, so the 494–999 band was a
crash rather than a decision, and 494 was not even fixed: it is whatever stack
remains when `guard_query` is called, so the threshold moved with the caller. The
walk now carries a depth limit of 250 frames and refuses past it, measured
against a worst case of **18** across every payload both suites accept plus an
aging report written to be worse than any of them.

Measured by mutation on the resulting suite (319 tests), all red: poisoning the
function list **1** · removing two relations **2** · adding `internal_notes`
**35** · opening the schema list **3** · deleting the `sql` type check **3** ·
deleting the `max_rows` type check **3** · deleting the ceiling **2** · drifting
the ceiling from the settings bound **1** · deleting the depth guard **1** ·
setting the depth limit to 4, the over-restrictive direction, **101** · returning
a wrapper that ignores its input **19** · re-opening the catalog hole above **9**.

Two results recorded because they are the unflattering ones. First, the finding
about the wrapper was overstated in the audit and is corrected here: a
`guard_query` that discarded its input and returned `SELECT * FROM (SELECT 1) …`
passes all **18** `test_allowed` cases, but **12** other tests do turn red — all
of them end-to-end ones that execute the result and read rows. The property was
held by the tests that happen to run queries, not by anything that stated it; it
is stated now. Second, the same mutation exercise applied to the *tests* instead
of the guard: of seven mutations that gut assertions in the new block — emptying
loops, dropping the reason a refusal must give, comparing a tree to `is not None`
— **six stay green**. Nothing covers a test file. What holds it is that these
mutations were run at all.

**Not fixed here, and named rather than left quiet.** *(Closed 2026-07-31 — see
the amendment below. Kept as written because it is the record of a defect that
was known and carried for a release.)* An error raised while
*executing* the guarded query still reaches the model with the wrapper's line
numbering, through `tools.py` and `mcp_server/server.py`:

```
Binder Error: Referenced column "missing" not found in FROM clause!
LINE 2: SELECT count_star() FROM customers GROUP BY missing
```

Same class, different layer, and its severity is product quality rather than
confidentiality — this repo is public, so the wrapper's shape is not a secret
from anyone; the cost is that a model trying to self-correct is handed a line
number into a query it did not compose.

**Amendment (2026-07-31) — the execution path no longer quotes the wrapper
either.** The paragraph above is discharged. `strip_wrapper_line_echo` removes
DuckDB's source echo from execution errors, and both tool surfaces call it before
returning the failure. What goes is the echo and its caret; what stays is the
diagnosis, `Candidate bindings` included — that half is the point, since a
message stripped of its reason costs the model more than a wrong line number
does.

Three things the implementation had to be measured into, none of them visible
from the bug report:

- **The line number is not a constant.** Every example shows `LINE 2`, and a
  string literal containing a real newline moves the failure to `LINE 3`. Pinned
  to `LINE 2`, the strip leaks on exactly the queries that are hardest to read.
- **The echo can be forged from inside the query.** A quoted identifier may carry
  a newline, so `SELECT "a\nLINE 9: injected" FROM invoices` puts an echo-shaped
  line *above* the genuine one, inside `Referenced column …`. Cutting at the
  first match would delete the Candidate bindings below it — a caller able to
  blank its own diagnosis. The cut is anchored at the tail and requires the caret
  line under it, which forged text cannot supply.
- **Rewriting the number instead of dropping it is worse.** The echoed line is
  DuckDB's print of the validated tree, so `amount_due::INTEGER` comes back as
  `CAST(amount_due AS INTEGER)`; a caret computed for that text and shown against
  the caller's own would point at the wrong character. A wrong caret costs more
  than no caret.

Measured on DuckDB 1.5.3 over a 20-query sweep: 14 failures carry the echo, and
in all 14 it is the last two lines; the other 6 (`GROUP BY 9`, `amount.foo`,
`date_trunc('bogus', …)`, …) are returned untouched. The failure direction is
deliberate — an unrecognised shape returns the message unchanged, so a future
DuckDB brings the leak back rather than eating the diagnosis, and the tests that
run real queries into the binder go red on that upgrade.

Suite **324 → 336**. Mutation, **10 of 10 red**: either call site back to
`str(exc)` **1** each · strip as a no-op **8** · dropping the caret requirement
**2** · cutting at the first match **3** · hardcoding `LINE 2` **1** · over-
cutting to the first line **6** · and the two *relaxations*, which is where this
round earned its keep — widening the caret to `^ *\^.*$` and the head to `^LINE `
both left all 335 green until a test was written for the shape itself, **1**
each. Two more results worth the space: an `invoices` fixture typed with
`DECIMAL` instead of the ledger's `DOUBLE`, and the same fixture left empty,
each silently dropped the `WHERE amount > 'x'` case by making it *succeed* —
a fixture one type or one row away from production is not a smaller test, it is
a different one. And the same exercise on the tests instead of the code: **6 of
6** assertion-gutting mutations stay green, unchanged from the 6-of-7 recorded
above. Nothing covers a test file except running the mutations.

**Amendment 2026-07-31 — guarantee 1 was asserted only by two dead tests.**
Section 2 of the adversarial suite ("nothing may be created") carried a payload
that depended on the literal-masking desync this ADR removed. With the masker
gone, `WRAPPER_ESCAPE` is refused by `Parser Error: syntax error at or near ")"`
— it is a syntax error in the caller's own text, not a wrapper escape. The two
end-to-end tests around it went on passing anyway: `contextlib.suppress`
swallowed the parse error exactly as it had swallowed the refusal, and the
`pwned` object it then proved absent had never existed under any implementation.
**Measured: with `guard_query` removed from both calls, the suite stayed at 336
green — zero red.**

The consequence is larger than the dead payload. Deleting the SELECT/WITH-only
check in `_syntax_tree` also left the pre-amendment suite at **336 green**. The
first guarantee in the file's own threat model was pinned by exactly the two
tests that had stopped testing.

The rewrite keeps `WRAPPER_ESCAPE` as a regression floor with its reason named,
and replaces the rest with live payloads — `CREATE TEMP TABLE`, `CREATE
TEMPORARY VIEW`, `PREPARE`, `CREATE TEMP MACRO` — each asserted to be **accepted
by the engine** when the guard is out of the way. That premise is a test, not a
comment: a `connect_readonly` connection is read-only against the ledger file
but its `temp` catalog is writable, so for these four the guard is the only
thing standing there. `contextlib.suppress` is gone. Suite **336 → 346**;
removing `guard_query` from the calls is now **8 red**.

**And the half that cuts the other way, recorded because it weakens the claim.**
Neither guard mutation is a bypass. With the SELECT/WITH check deleted, a
`CREATE TEMP TABLE` is still refused one layer later by `json_deserialize_sql`
("Query could not be normalized"). Relaxing the statement count to tolerate a
trailing statement — the shape a real *"support trailing semicolons"* PR would
take — is 6 red and also not a bypass: `json_serialize_sql` rejects
two-statement text as a unit, so the refusal simply moves to the SELECT/WITH
check. No single-point mutation tried here creates the object. The guarantee is
held redundantly by three layers, and what these tests pin is **which layer
speaks and what it says** — the sentence handed back to the model — not whether
the object appears. Test-side: **4 of 6** mutations of the new tests stay green.

**Consequences.**

- **Positive.** The whole class of scanner/executor disagreement is gone. The
  guard also became *less* restrictive where it was wrong: `EXTRACT(year FROM
  due_date)`, `SUBSTRING … FROM … FOR`, `TRIM(BOTH … FROM …)`, `CAST(x AS
  DECIMAL(18,2))`, `WITH RECURSIVE`, `WITH t(a) AS`, `now()`, `INTERVAL 30 DAY`
  and schema-qualified references to allow-listed tables were all being refused
  by the old relation scanner, which read the `FROM` inside `EXTRACT` as a
  relation list. `INTERVAL n DAY` is *the* aging idiom for a receivables agent.
- **Negative.** Genuinely recursive CTEs are refused (see above). Fixing that
  properly means resolving names against the real schema, not a name list.
- **Negative.** The function allow-list will occasionally refuse a legitimate
  query. That is the designed failure direction: a gap costs a query, not the
  ledger. Grow the list when it happens.
- **Correction to ADR-003.** That ADR called the read-only connection "the
  authoritative half" of a defense-in-depth design. `read_only=True` protects
  *integrity*, not *confidentiality* — every bypass above was a **read**, and
  reading is what an LLM-composed query is made of. The connection now also sets
  `enable_external_access=false`, extension autoloading off and
  `lock_configuration=true`; that is the layer that actually stops exfiltration,
  and it held on every probe across all three rounds (filesystem, network, other
  database files and secrets were unreachable throughout).
- **OPEN — availability is not covered, and the outer LIMIT is not a defence.**
  The guard bounds *what can be read*, not *how much work a query may do*. The
  claim audit demonstrated three accepted queries that never return:
  `WITH RECURSIVE invoices(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM invoices
  WHERE n < 100000000) SELECT count(*) FROM invoices` (killed at 60s),
  `SELECT repeat('a', 1000000000) FROM invoices`, and a six-way self-cartesian
  join. `SELECT * FROM (…) LIMIT 200` caps *rows returned*, not *rows computed*,
  so it stops none of them. Confidentiality held under every one of the ~13
  further vectors thrown at it, but a public demo that any visitor can hang is a
  real gap on a surface this ADR calls the priority security surface. Fixing it
  needs a design decision — DuckDB has no statement timeout, and
  `lock_configuration=true` deliberately prevents setting one after connect, so
  the options are an interrupt from a watchdog thread, a row/row-scan budget, or
  running the query in a killable subprocess. Queued, not silently accepted.
- **Process.** The tests were written by an instance that did not write the code
  and was not told how it worked, and were required to produce a failing repro
  and to prove they could go red by mutation. Three rounds, each finding what
  the previous fix introduced. `tests/test_sql_guard_adversarial.py` is that
  work, kept as a regression floor.

---

## ADR-015 — Demo product-polish: light/dark theme + EN/PT-BR i18n (Phase 9)

**Status:** Accepted · 2026-07-06

**Context.** The React chat UI (`web/`) was single-theme (dark-only navy) and
English-only — a curious tester looking for polish/versatility got neither, and the
front-end/product axis (a real but under-shown strength) was invisible. Paired with
the sibling `forge-pdm-mlops` F9 (ADR-018) so the two public demos share one design
language (the hypercube navy+cyan brand).

**Decision.**
- **Light/dark theme (9.1).** CSS custom properties on `:root`, **light as the
  default**, dark applied by (a) `prefers-color-scheme: dark` as the ambient signal
  and (b) a manual, persisted `data-theme` on `<html>` that **wins in both
  directions** — the same override discipline the platform's Artifacts use. Two new
  tiny modules, no dependency: `theme.js` (initial = persisted choice → else OS) and
  the toggle in the header. Dark keeps the existing navy identity; light is a clean
  slate.
- **i18n (9.2), EN + PT-BR.** A **lightweight** locale layer — a string dictionary +
  a `translator(locale)` closure, no framework (two locales don't warrant one) —
  in `i18n.js`. Initial locale = a persisted manual choice → else the browser
  language (`pt*` → PT-BR, else EN), mirroring the theme's ambient-then-override
  idea. A language toggle localizes all chrome, the hint text, and the progress
  states.
- **The seeded example questions stay ENGLISH in every locale** — they are
  plan-cache keys (a 0.90-similarity ChromaDB cache, ADR-009) *and* the text sent
  verbatim to the English-corpus agent. A PT-BR paraphrase would miss the cache and
  fall to the slow tiny model. i18n localizes the chrome *around* them, not the
  question the agent receives.

**The honesty boundary (load-bearing).** i18n covers the **UI shell only**. The
agent's answers come from the model + the **English** policy corpus and are **not**
machine-translated on the fly. A localized interface must not imply localized
answers, so an `i18nNote` states this explicitly in both languages (it would be a
larger, separate claim to translate model output — we don't make it).

**Consequences.** First-impression polish + accessibility (theme) and a broader
tester pool (language) at near-zero cost and no new dependency; no regression to the
streaming/plan-cache paths (the cache keys are untouched). The build is +2 modules
(34 vs. 32), `web/dist` rebuilt for the container. The visual language is shared with
`forge-pdm-mlops` F9.

---

## ADR-014 — Graceful ceiling: dedup, finalization, narration + wall-clock budget (Phase 8)

**Status:** Accepted · 2026-07-05

> Built in two slices, same day. **Slice 1** — tool-call dedup (8.2) + forced
> finalization (8.1/8.3). **Slice 2** — progress narration + a raised narrated step
> cap split from a soft wall-clock budget (8.5), and the decision to treat the
> "continue" affordance (8.4) as satisfied-by-design. Both are recorded below.

### Slice 2 — narration + the step/time split (8.5), and why no checkpointer (8.4)

**Context.** Slice 1 stopped a ceiling-hit from dead-ending, but the ADR-013 step
cap (8) was a **clumsy proxy for "don't make them wait."** Its real sin was a
*silent* wait ending in nothing — not merely a long one. Two consequences: good
questions that just needed a few more steps were cut off, and the single cap
conflated a **loop guard** (stop a degenerate cycle) with a **wait guard** (don't
make the user wait).

**Decision.**
- **Progress narration (8.5).** `turn_control.narrate_start` / `narrate_end` turn
  each tool call into a human line — an intent ("Checking the collections policy on
  '…'") on `on_tool_start` and a finding ("Found 12 rows in the ledger") on
  `on_tool_end` — streamed as a new `step` SSE event the UI renders live (coalescing
  repeats). This upgrades ADR-012's bare "using query_ledger" tool event into a
  readable running commentary, so a longer run *looks* productive instead of frozen.
- **Split the one cap into two guards.** The **silent** path (`invoke`/`ainvoke`)
  keeps the tight ADR-013 loop guard (`agent_recursion_limit`, 8) — no narration, so
  a long wait still reads as hung. The **narrated** streaming path (`astream`) gets a
  **higher step cap** (`agent_narrated_step_cap`, 16) as its loop guard **plus** a
  **soft wall-clock budget** (`agent_wall_clock_budget_s`, 45 s) as the wait guard:
  past the budget it narrates a wrap-up and finalizes (8.3) from what it has. Raising
  the cap is safe **only** because it ships together with narration + dedup (cheap
  repeats) + finalization (always an answer); a raised cap *alone* would recreate the
  silent thrash — that's the part that would stay wrong. The clock is injectable so
  tests don't fight the asyncio loop's own `time.monotonic`.

**Why 8.4 (a checkpointer "continue") is not built.** The plan scoped a LangGraph
checkpointer + `thread_id` so a follow-up continues with prior context. But the API
**already resends the full conversation history every turn** (`/api/chat` +
`/api/chat/stream`), so a follow-up *already* carries context — and slice 1's
finalization ends with a **narrowed next-step invite** ("ask me to sort by amount")
that makes that continue useful. A server-side checkpointer on top of history-resend
would **double-count** the conversation. So 8.4's user goal is met by the existing
stateless-history contract; a checkpointer is deferred as an unnecessary (and
conflicting) optimization for this design, not shipped as dead machinery.

**Consequences.** A question that previously hit the ceiling now narrates its
progress, is allowed more productive steps bounded by a visible time budget, and
always ends in a finalized answer naming what it found + the gap — the honest
"I made a tiny model degrade gracefully" story, end to end. The step cap is no
longer asked to mean two things at once. Verified offline (narration lines; the
`step` events on a normal run; the wall-clock early-finalize via an injected clock);
the live tiny-model behaviour is confirmed on the Space (the tiny-CPU loop can't run
cheaply locally), as with ADR-013's streaming.

---

### Slice 1 — dedup + forced finalization (8.1/8.2/8.3)

**Status:** Accepted · 2026-07-05

### Context

ADR-013 gave the free-CPU tiny model a low step cap (8) and a graceful
`GraphRecursionError` catch so a loop fails in seconds, not the old 200–250 s
thrash. But live use showed two residual problems on **novel typed questions**:

- The tiny model **re-issues the same tool call** (or over-calls `search_policy`
  on a pure-data question), **burning its small step budget on work it already
  did** — so it hits the cap without ever answering.
- When it hits the cap, the graceful message is **generic and dead-ends**
  (*"couldn't finish — try an example or rephrase"*): it throws away whatever the
  agent *did* gather, so it "reads as 'I just don't work for no apparent reason.'"

This is the first slice of Phase 8. It **improves** the ADR-013 seam; it does not
add a second ceiling or raise the cap. (Narration + the raised narrated cap +
wall-clock budget (8.5) land in slice 2 above; the "continue" affordance (8.4) is
handled there too — met by the existing history-resend contract.)

### Decision

A **turn-scoped control layer** (`src/agent/turn_control.py`), wired by wrapping
the two tools in `build_agent` and driven by `CachedAgent`:

1. **Dedup / redundant-call short-circuit (8.2).** `ToolCallTracker.wrap`
   decorates each tool so an **identical** call within one turn (args normalized
   only for whitespace — case preserved, so different literals never collide)
   returns the **memoized** result plus a firm nudge ("you already have this —
   answer now"), *without re-executing*. It doesn't lower the step count, but it
   stops paying for the same query twice and pushes the model to finalize —
   "not a bigger budget, but not squandering the budget we have."
2. **Forced finalization (8.1 + 8.3).** The tracker records every
   `(tool, args, result)` of the turn. On a ceiling hit, `finalize_answer`
   composes a **deterministic** best-effort answer from those observations — the
   most recent *successful* ledger query (errors/rejections don't count as
   gathered facts) and/or a policy finding, plus the specific gap and a narrowed
   next step ("I pulled the overdue list but didn't rank it — ask me to sort by
   amount") — instead of the canned apology. No extra LLM call: the partial work
   is surfaced, not re-reasoned. With nothing usable gathered it returns the
   honest no-progress message (the ADR-013 floor). Wired into **all three** ceiling
   paths — `invoke`/`ainvoke` (which previously *raised*) and `astream`.
3. **Concurrency.** The agent is built once and shared across requests
   (`app.state`), so turn state lives in a **`ContextVar`** (`begin()` installs a
   fresh state per turn); concurrent requests never cross-contaminate, and outside
   a turn the wrapper is a transparent pass-through (tools stay unit-testable).

### Consequences

- A ceiling-hitting question now ends in a **useful partial answer naming what it
  found + the gap**, and a repeated tool call is served from memo with a nudge —
  the "reads as broken" first impression is fixed without touching the cap.
- **Honesty preserved:** finalization renders only what a tool actually returned
  (no fabricated numbers), and errored/guard-rejected queries are never presented
  as facts. It complements — doesn't touch — the ADR-009 plan-cache (which replays
  *successful* runs).
- **Verification:** the seams are exercised offline via a looping stub agent that
  calls the wrapped tool then raises `GraphRecursionError`, through both `invoke`
  and `astream` (`tests/test_turn_control.py`, 12). Live behaviour on the tiny
  model is confirmed on the Space (the tiny-CPU loop can't run cheaply locally),
  as with ADR-013's streaming.
- Strong/cloud models are unaffected — they rarely loop, and dedup only fires on a
  genuine exact repeat.

### Amendment 2026-08-01 — "different literals never collide" was false, and the isolation had no test

Point 1 above claimed the arguments were "normalized only for whitespace — case
preserved, so different literals never collide". The second half was wrong. The
collapse ran over the whole string, so it reached **inside** string literals:
`WHERE name = 'John  Doe'` and `WHERE name = 'John Doe'` produced one key, and the
second query — a different question about a different customer — never ran and was
handed the first one's rows plus the nudge *"you already ran this exact call"*
(measured 2026-07-31). A dedup key that merges two questions doesn't waste a step;
it answers one of them with the wrong data.

`query_ledger` now keys on the **statement**, not on its text.
`sql_guard.statement_identity` returns the tree DuckDB parses
(`json_serialize_sql`, byte offsets dropped), so indentation, keyword case, a
comment and a trailing `;` still dedup, while anything that differs inside a
literal separates — the same principle as ADR-022: *ask the parser, don't model the
language in text*. Three deliberate boundaries:

- **The tracker stays language-agnostic.** `wrap(..., key=)` is supplied by the tool
  that knows what its argument is (`tools.py`); tools taking prose keep the text
  key, where collapsing whitespace changes nothing a reader means.
- **Unparseable SQL falls back to text.** It has no tree, and the guard refuses it,
  so it never reaches the ledger; merging two of them only spares the model a step
  it was wasting. The two key spaces are prefixed (`tree:` / `text:`) because
  without that a caller can pass a tree as its `sql` and be served another call's
  memo — constructible, and now tested.
- **It errs toward separating.** The tree keeps identifiers as typed, so
  `FROM customers` and `FROM CUSTOMERS` are two identities. That costs one repeated
  call; the opposite error costs a caller someone else's rows.

Point 3's concurrency claim was true but **unowned**: swapping the `ContextVar` for
a module global kept the whole suite green, because the only isolation test ran
turns in sequence, which a global also passes.
`test_interleaved_turns_do_not_cross_contaminate` now runs two turns through
`asyncio.gather`, opening both before either calls a tool, and checks the memo
*and* the observation log.

The isolation is per **asyncio context**, and the ADR now says so: `begin()` writes
into the caller's context rather than creating one, so requests are isolated
because the ASGI server gives each its own task. Two turns driven from the same
task share a state. Not reachable over HTTP, and not a code change — but
"concurrent requests never cross-contaminate" without that sentence claims more
than the mechanism delivers.

One more hole came from an attack on this change rather than from reading it: the
guard parses `sql.strip()` while `statement_identity` parsed the raw text, so a
leading `\x0b` — whitespace to Python, a parse error to DuckDB — made the identity
return "no opinion" for a statement the guard happily executed, and the text key
came back through that crack, literal collision included. The strip now has one
owner (`_statement_text`), used by both, and the invariant *"anything the guard
runs can be identified"* is tested against every character Python's `str.strip()`
removes, derived from the language rather than listed by hand.

Suite **374 → 382**. Fourteen mutations of this code — four of them relaxations
(lower-casing the SQL before keying, collapsing whitespace before keying, an empty
identity for unparseable text, a fallback that ignores the arguments) — leave
thirteen red. The one that stays green moves the shared strip for *both* callers
at once, which is the single-owner property working, not a gap.

---

## ADR-013 — Tiny-model prompt, firm tool-routing, and a low recursion cap

**Status:** Accepted · 2026-07-03

### Context

Live testing of the free-CPU Space surfaced three failure modes on **novel typed
questions** (the seeded ones are instant + correct from the baked cache, ADR-012):

- A plain data question ("which 5 customers have the most overdue money?") made the
  tiny model call **both** tools — `query_ledger` **and** `search_policy` — when the
  policy is irrelevant, then loop.
- With the default LangGraph recursion limit (25), a looping tiny model **thrashed for
  200–250 s** and then returned no final answer ("The agent did not return an answer").
- Every ReAct turn fed the model the **full ~729-token system prompt** (written for a
  strong model) + a 386-token schema block — slow on a 2-vCPU CPU.

### Decision

Tier-specific prompt + a hard step cap:

- **Compact tiny-model prompt** (`graph.SYSTEM_PROMPT_BRIEF` + `schema_hints.SCHEMA_HINTS_BRIEF`),
  selected by `graph.select_prompt` when the primary is the constrained tiny tier
  (`providers.should_constrain`); strong/cloud models keep the full prompt. **57 % shorter**
  (729 → 310 tokens), so every turn is cheaper.
- **One firm routing rule** in that prompt: *default to `query_ledger`; call `search_policy`
  ONLY when the question explicitly asks about a rule/policy/threshold; then answer — stop
  calling tools.* Small models follow a single terse imperative better than examples.
- **`agent_recursion_limit` (default 8)** passed as `config={"recursion_limit": …}` to every
  agent call. A well-behaved turn is 1–2 tool calls, so 8 lets the loop breathe but fails an
  unproductive loop in seconds, not minutes. `CachedAgent.astream` catches
  `GraphRecursionError` and yields a **graceful answer** ("couldn't finish on the free-tier
  tiny model — try an example or rephrase"), never a broken stream or a bare error.

### Consequences

- Novel questions are faster (shorter prompt) and route correctly more often (the rule),
  and a genuine loop degrades gracefully instead of thrashing.
- **Does not** make a 1.5B as capable as a strong model on free-text — some paraphrases will
  still mis-route or give weak SQL. That remains the "shines with a better model" story
  (ADR-011); the baked curated cache (ADR-012) covers the showcased questions with certainty.
- Strong/cloud models are unaffected (full prompt, native tool-calling, same high cap in
  practice since they rarely loop).

### Amendment 2026-07-31 — the second prompt is a second copy of the schema, and neither had an owner

The decision above accepted a duplicated schema description (`SCHEMA_HINTS` and
`SCHEMA_HINTS_BRIEF`) on the grounds that the two texts serve different audiences. What it did
not say is that **both are unverified copies of what `data/generate.py` builds**. A line-by-line
audit measured the gap: renaming `v_dso` to a non-existent view inside the prompt left the suite
at **346 green**. The relation allow-list has been pinned against a real catalog since ADR-022's
round 6; the text the *model* reads had nothing.

The failure mode is the expensive kind precisely because nothing is broken: the SQL is
syntactically perfect, the guard accepts it, DuckDB raises `Catalog Error`, the ReAct loop feeds
that back as an observation and tries again until the step cap of this ADR fires — and the
degradation message blames the tiny model for a prompt defect.

`tests/test_schema_hints.py` closes it by generating a real ledger (60 customers, ~0.3 s, through
`generate.main()` so the fixture cannot drift from the pipeline) and comparing **surfaces**: the
catalog decides what is checked, the prompts are checked against it, in both directions — plus
the value sets the text promises about *data*, which no schema check can see. Suite **359 → 368**;
15 of 15 mutations across the two prompts, the generator and the allow-list now fail.

Two limits worth stating. The brief prompt is a deliberate subset, so its columns are checked by
containment, not equality — what pins its *scope* is one assertion, that it covers exactly the
ledger's views, and weakening that assertion is the only measured way to drop a view from the
tiny-model prompt unnoticed. And the curated plans of ADR-009/ADR-012 are still not executed
against a real ledger anywhere: `guard_query` checks relation names, never columns.

> Closed later the same day — see **ADR-009 Amendment 2026-07-31 (`R1-C11`)**. The sentence
> above is kept as written: it is the record of how the gap was found.

---

## ADR-012 — Self-contained tiny-Ollama Hugging Face Space (`Dockerfile.hf`)

**Status:** Accepted · 2026-07-03

### Context

Phase 6 Layer 3 needs a **reachable public URL** — the "click here and try it"
link that turns "I containerized an app" into a page a reviewer can use. The
question was what the Space actually runs for inference. Two viable paths:

- **Gemini-key Space** — fast, strong answers, but depends on a `GEMINI_API_KEY`
  secret + the free-tier quota; a stray demo run can burn the key, and the story
  becomes "it calls a cloud model," which any wrapper does.
- **Self-contained local model** — the container runs its *own* tiny model
  (Ollama), no key, no quota, always up.

### Decision

Ship a **self-contained tiny-Ollama image** ([`Dockerfile.hf`](../Dockerfile.hf))
as the public Space:

- Base `python:3.11-slim`, multi-stage (Node builds `web/dist`), the **Ollama
  server binary installed in the image**, and the synthetic ledger **baked at
  build time** (`python data/generate.py`). Non-root UID 1000 with a writable
  `HOME` so `~/.ollama` and `data/chroma` are writable at runtime.
- Runtime deps are `requirements-hf.txt` — the runtime set **+ `langchain-ollama`,
  minus `langchain-google-genai`** (the Space is the local path; dropping the
  cloud SDK keeps the image lean).
- The model is **pulled at startup, not baked** ([`scripts/hf_entrypoint.sh`](../scripts/hf_entrypoint.sh)):
  `ollama serve` → resolve the tag via `python -m src.core.hardware --select`
  (ADR-010; on HF free CPU = 2 vCPU / 16 GB it picks the tiny floor, default
  `qwen2.5:1.5b`) → `ollama pull` → **pre-warm the plan-cache**
  (`python -m data.seed_plan_cache`, ADR-009) → `exec uvicorn`. Pulling at startup
  keeps the image small and lets `OLLAMA_MODEL=auto` choose per the host box.
- Env pins `PRIMARY_PROVIDER=ollama`, `PLAN_CACHE_ENABLED=true`, an
  `APP_API_KEY` baked to match `VITE_API_KEY` (same-origin demo, no login).

### Consequences

- **Zero secrets, zero cloud quota, always up** — the cleanest portfolio story: a
  free URL that runs its own model. No key to leak, nothing to rate-limit.
- **Latency shape:** the plan-cache replays the **headline questions
  deterministically without the LLM** (ADR-009) → instant even on CPU. A *novel*
  question pays the tiny-CPU cost (~tens of seconds). Acceptable for a demo, and
  it makes ADR-011's "shines with a better model" line tangible.
- **Deploy mechanics** (mirroring the forge-pdm F6 Space): a dedicated
  `space-deploy` branch carries the HF README front-matter + this image as the
  *literal* `Dockerfile` (HF ignores `dockerfile_path`); `main` keeps the Gemini
  `Dockerfile` and an LFS-free tree. See [`docs/DEPLOY.md`](DEPLOY.md).
- **Deferred (needs a GPU box):** the tiny-vs-**strong** comparison number for the
  README — the tiny live link and its own eval/latency numbers ship from here; the
  strong-model delta is added later.

---

## ADR-011 — Grammar-constrained tool-calling for tiny local models

**Status:** Accepted · 2026-07-02

### Context

Phase 6's demo runs a tiny local model on a free CPU tier. But **measured** on
this repo's real tools + prompt (5 golden questions, `temperature=0`), native
tool-calling via `bind_tools` is unusable on tiny models — and *not* for the
reason the plan assumed (malformed JSON):

- `qwen2.5:0.5b` — emits **syntactically valid** JSON but **invents fictional
  argument fields** (`issue_date`, `aging_bucket`, …) and omits the real `sql`
  field; routes to the wrong tool most of the time. ~1/5 usable.
- `qwen2.5:1.5b` — writes **correct SQL in prose/markdown** and emits **no tool
  call at all**. 0/5.

So a GBNF grammar that only guarantees *valid JSON* would fix nothing here. The
real defect is that the native tool-calling **channel** is unreliable on tiny
models — one produces schema-non-conformant args, the other bypasses the channel.

### Decision

Constrain the model's **entire reply** to a JSON schema (Ollama's `format`
option — a GBNF grammar under the hood) that encodes the whole ReAct decision,
then translate that JSON back into `AIMessage.tool_calls`:

```json
{"tool": "query_ledger" | "search_policy" | "final_answer",
 "sql": "…", "query": "…", "answer": "…"}   // only `tool` required
```

- `src/agent/constrained.py::ConstrainedToolModel` is a `BaseChatModel` wrapper:
  `bind_tools` builds the schema from the bound tools (+ a `final_answer` option
  so the loop can terminate); `_generate` binds `format=schema`, parses the reply,
  and returns either an `AIMessage` with one `tool_calls` entry or a final-answer
  content message. Malformed / off-menu output degrades to plain content rather
  than crashing.
- Because it presents as a normal chat model, **`create_react_agent`, the
  plan-cache (ADR-009), the SQL guard (ADR-003) and the evals are unchanged** —
  only the local model's *channel* changes.
- **Tier-gated** (`providers.should_constrain`, reusing ADR-010's catalog
  `quality`): `constrained_tool_calls=auto` wraps only tiny models
  (`quality <= constrained_quality_max`, default 3); strong/cloud models keep
  native tool-calling (constraining them would *reduce* quality). `on`/`off`
  force it. Also added `ollama_num_ctx` / `ollama_keep_alive` (KV/prompt cache).

### Consequences

- **Measured:** structured output takes both tiny models from ≤1/5 to **5/5**
  valid + routed + filled tool calls. End-to-end on the real agent + ledger,
  `qwen2.5:1.5b` now completes the full loop (tool call → guarded SQL → final
  answer with a real number; e.g. top-3 customers with balances, ~2–4 s on GPU).
- **`format` fixes structure, not SQL correctness** — a 0.5B model still writes
  weak SQL (e.g. it may not filter "90 days" precisely). That is by design the
  "shines with a better model" story, now backed by numbers: the *architecture*
  is reliable at 0.5B, *answer quality* climbs with model size. The plan-cache
  serves curated-correct SQL for the headline demo questions; the SQL guard makes
  a tiny model's bad SQL safe (rejected/handled) on novel cache-miss questions.
- Feeds Layer 3 (the HF demo image pins a tiny model + this shim) and Layer 4
  (the tiny-vs-strong numbers the README will quote).

### Amendment — 2026-07-31 (`R1-C9`): the *meaning* half of ADR-011 had no test

The decision above has two halves. The **grammar** (`format=schema`) fixes the
shape of the reply; the **system nudge** built by `_render_hint` — which tool
takes which field, when to stop, "ground every number in a tool result" — is what
makes the model choose *well*. A line-by-line audit found only the first half was
tested, and the gap was wide enough that three separate edits to the second half
left the whole suite green (346 at the time, 368 when re-confirmed today):

- deleting the nudge entirely (`_prepare` → `list(messages)`), and emptying it
  (`_render_hint` → `""`) — the model would keep emitting valid JSON and pick badly;
- `test_bind_tools_then_invoke…` asserted `_seen_format == build_schema(TOOLS)`,
  a **circular oracle**: the same function on both sides of `==` agrees with any
  edit to itself. Two schema mutations never turned it red;
- the tier threshold: `<=` → `<` in `should_constrain` was green, and the reason
  is structural rather than a missing case — the default ceiling is **3** and the
  catalog ranks jump **2 → 4**, so no model exists that could tell the two
  operators apart. A numeric threshold only has an owner if a real value lands on it.

Closed by `tests/test_constrained.py` (**368 → 371**). The fake base model now
records the messages it was called with, so the protocol is asserted against the
whole menu (every tool with its field, a count anchor so no option is invented or
dropped, the stopping criterion, the grounding instruction) rather than a sample;
the schema is compared against a **spelled-out literal**; the threshold test moves
the ceiling to **2**, a rank that exists, where `<=` and `<` finally differ. The
async `_agenerate` twin — a hand-copied duplicate of `_generate`, previously
uncovered — is pinned against the sync path.

**14 of 14 mutations now fail**, including every *relaxing* one (an extra
`reason` field in the grammar; a hint that keeps `final_answer` but drops the tool
menu, the stopping criterion, or the grounding line; `quality <= 10`). **Nine of
the fourteen were green before this change.** No production code changed: the gap
was entirely in what the suite could see.

---

## ADR-010 — Hardware-aware local model selection (`OLLAMA_MODEL=auto`)

**Status:** Accepted · 2026-07-02

### Context

Phase 6 sells the demo as *"runs a tiny model on free CPU, shines with a better
model."* Pinning a single `OLLAMA_MODEL` in config makes that a **README
promise**: on a 16 GB / 2-CPU free tier a 7B tag stalls or OOMs, while on a dev
box with a GPU a pinned 0.5B needlessly throws away capability. The local path
should scale to the box it runs on — from the first request, with no manual
retune per machine. (The private `fleet_intelligence_agent` already does this;
its `utils/hardware.py` holds **zero confidential data** — public Ollama model
names + generic `psutil`/`nvidia-smi` detection — so the clean-room rule permits
reusing our own engineering, adapted to this repo's English/style.)

### Decision

Add `src/core/hardware.py`: detect RAM / NVIDIA-VRAM / CPU, compute an
**effective memory** heuristic (VRAM if a real GPU is present, else ~80 % of RAM),
and pick from a **public catalog** the highest-quality model that both *fits* and
is *already downloaded* (falling back to the best that *would* fit if none is
pulled yet). `OLLAMA_MODEL=auto` (now the default) triggers this in
`resolve_ollama_model` (`src/agent/providers.py`) at model construction; a
concrete tag still overrides — the HF CPU demo pins the tiny floor explicitly.

- **LLM-only catalog.** Embeddings are ChromaDB's bundled ONNX MiniLM (ADR-005),
  so there is no Ollama embedder to select — dropped FIA's embed catalog.
- **No new dependency.** `psutil` is used *if present*; every detector has a
  stdlib fallback (`/proc`, `os`, `nvidia-smi`, `wmic`), so the diagnostic runs
  with the system Python before any `pip install`.
- **Diagnostic + machine-readable modes.** `python -m src.core.hardware` prints a
  readable table; `--json` and `--select` (emits `OLLAMA_MODEL=…`) let a
  container entrypoint / compose consume the choice.
- **Offline-testable.** Detectors are separable; `tests/test_hardware.py`
  constructs `HardwareProfile` directly and monkeypatches detection, honouring
  the "unit tests run offline" rule (no `nvidia-smi`, no Ollama).

### Consequences

- Phase 6's tiny-vs-strong claim is **real from run one**: verified live on the
  notebook (RTX 4050, 6 GB VRAM, `qwen2.5:7b` pulled) → `auto` resolves to
  `qwen2.5:7b`; a 16 GB CPU tier would resolve to a tiny model instead.
- `resolve_ollama_model` **always returns a concrete tag** (tiny floor if
  detection yields nothing), so `ChatOllama` construction never fails; a
  genuinely unusable local box is still covered by the provider fallback (ADR-004)
  at call time.
- Feeds Phase 6 Layer 2 (the hardened tiny local LLM) and Phase 7.3 (a
  multi-service compose can call `--select` to choose the model to pull).

---

## ADR-009 — Semantic plan-cache: cache the *plan*, never the answer

**Status:** Accepted · 2026-07-02

### Context

Phase 6 turns the "shipped link" into a **zero-key click-and-try** demo on a free
CPU tier: no install, no API key for the visitor. On that tier the LLM is the
slow, expensive part of every turn, and demo visitors ask overlapping questions
(top overdue, DSO, aging). A cache is the obvious win — but the naive cache is a
**correctness trap**: caching a question → *answer* freezes a number over mutable
data. Regenerate the ledger with more customers, or test a new scenario, and a
cached answer silently lies. In a portfolio piece, a confidently-wrong number is
worse than a slow one.

### Decision

Cache the question → **plan**, not the question → **answer**, and always
**re-execute the plan live**.

1. **A plan is the agent's tool calls** — the guard-validated `query_ledger` SQL
   and the `search_policy` query text (*intent*, `src/agent/plan_cache.py`). It
   carries **no answer text and no numbers**. Only a **read-only, guard-valid**
   turn is cacheable: `plan_from_messages` re-checks every `query_ledger` SQL
   through `sql_guard` before storing, and refuses to cache a turn with no
   groundable tool call.
2. **A hit is replayed live** (`src/agent/plan_replay.py`): each cached step is
   **re-validated through `sql_guard` and executed read-only** (SQL) / re-run
   against the live index (policy), then the answer is **composed
   deterministically from the fresh results** — the LLM is never called. So the
   number always reflects the *current* data; only the *reasoning /
   tool-selection* is skipped. If a cached query no longer validates or runs
   (`ReplayError`), the caller falls through to the LLM.
3. **Semantic lookup reuses the existing RAG stack** — the same ChromaDB client
   and local MiniLM embeddings as the policy index (ADR-005), so this adds **no
   new dependency**. Cosine similarity with a **conservative threshold** (default
   0.90); a miss simply calls the LLM and warms the cache. Precision over recall:
   "check this account" never matches "send this account" — and even a wrong
   match is harmless, because every replayed plan is re-validated and read-only.
   > ⚠️ The last clause of point 3 is **false as written** and was corrected on
   > 2026-08-01 — see the amendment below. A wrong match cannot corrupt data or
   > serve a stale number, which is what "re-validated and read-only" buys; it
   > could still answer a *neighbouring* question. Nothing else in point 3 changed.
4. **Drop-in wrapper.** `CachedAgent` presents the same `invoke`/`ainvoke`
   interface as the compiled agent, so the API, evals and tests are unchanged;
   `build_agent` wraps the agent when `plan_cache_enabled` (config-gated).
   Multi-turn requests (a follow-up needs prior context) are not cache-eligible.

This is caching the **reasoning / structured intent**, not output caching (cf.
the semantic-cache literature).

### Consequences

- **Fast *and* honest:** a hit is ~tens of ms (no LLM), yet the number is always
  live — proven offline by mutating the ledger and re-running the same cached
  plan (`tests/test_plan_cache.py`), no model required.
- The whole mechanism is unit-testable **offline** (deterministic embeddings +
  an in-memory DuckDB), which is why Layer 1 was built entirely on the CPU-only
  desktop; only seeding real example plans and the strong-vs-tiny model numbers
  need a live provider (the notebook).
- Reuses the guardrail as the safety backstop on the replay path too — one
  audited security surface, no second model.
- Layers 2–3 of Phase 6 (a hardened tiny local LLM with grammar-constrained
  tool-calls + a self-contained `Dockerfile.hf` / `space-deploy` branch) build on
  this and are deferred to a live-network session.

### Amendment 2026-07-31 (`R1-C11`) — "re-validated through the guard" was never a check that the SQL runs

Point 2 above is accurate and was read as more than it says. The guard is a **security**
boundary: it decides whether a statement is read-only and whether it touches an allow-listed
relation. It never looks at a column. `guard_query("SELECT no_such_column FROM v_customer_ar")`
returns a wrapped query, happily — measured.

For a model-warmed plan that is fine, because the SQL was written against a ledger that existed
and a `ReplayError` falls through to the LLM. For the **curated** plans of ADR-012 it is not:
that SQL is hand-authored, baked into the image, and wired to the UI's one-click chips, and its
only check was the guard plus a JSON-shape assertion. Measured before the fix: renaming
`v_customer_ar.overdue_amount` in `data/generate.py` left **every plan-cache test green** — while
three of the twelve curated plans became a `Binder Error` at replay, so three chips on the live
demo silently degrade to the tiny model on a free CPU, which is the exact failure the curated
cache exists to prevent. Only the two schema-hint tests of ADR-013's amendment went red, and they
say nothing about the plans.

`tests/test_plan_cache.py` now replays **every curated plan through `replay_plan`** — the same
function a cache hit runs — against a ledger built by `data/generate.py` through its own entry
point (the `ledger` fixture moved to `tests/conftest.py`, one owner for the two suites that need
it). Three properties, deliberately separate: the plan replays and reports the tools it declares;
every ledger query still comes back with **rows**; and each curated policy query still targets a
section the document actually has. Suite **371 → 374**.

**15 of 15 mutations now fail**, including the relaxing ones — adding a curated plan whose SQL
names a column of another relation, adding a curated policy question that names no section, and
the two value drifts that keep the SQL perfectly valid and return nothing (`status = 'past_due'`
in the plan; the generator renaming the status it writes).

Three limits worth stating, all measured. The empty-result test is the **single owner** of the
zero-row family: weakened, the value drift in the curated SQL goes green and every other test in
the repo stays quiet. The policy half was **not** the orphan it looked like — `tests/test_rag.py`
already pinned `Credit holds` and `Write-off thresholds` through retrieval, so only `Payment
plans` had no owner; the new assertion is redundant defence for two of the three sections. And
retrieval **ranking** is not measured here at all: the offline suite uses the deterministic
hashing embedding as a stand-in for MiniLM, so "this query finds that section first" is a claim
the shipped embedding would have to answer for, and does not.

### Amendment 2026-08-01 (`R1-C13`) — "even a wrong match is harmless" was false, and the threshold alone could not make it true

Point 3 above ended on a claim the code did not support. The correct statement splits in two,
and only the first half was ever true:

* a wrong match **cannot corrupt data or serve a stale number** — that is what re-validation
  through the guard plus live read-only replay buys, and it holds;
* a wrong match **can answer a neighbouring question**, with the same *"the numbers are
  current"* seal on it. Measured with the shipped MiniLM over the curated corpus baked into
  the demo image: the typed question *"Which customers have the largest overdue balances?"*
  (a list) is **0.9767** from the curated plan that ends in `LIMIT 1`, and *"Who is the top
  customer by overdue balance?"* (one customer) is **0.9424** from the plan that ends in
  `LIMIT 10`. Both clear the 0.90 threshold, so both were served.

**Raising the threshold does not fix this**, which is why the fix is a second rule rather than a
bigger number: 0.9767 is *higher* than paraphrase matches worth keeping (measured, e.g. "total
overdue by aging bucket" at 0.9324). What separates the two cases is not how close the winner
is — it is **how much closer the winner is than the runner-up**. The wrong matches above are
0.048 and 0.020 clear of a *different* plan; the legitimate ones are 0.159 to 0.598 clear.

**Decision.** `PlanCache.lookup` examines **every neighbour within `AMBIGUITY_MARGIN` (0.10) of
the winner** and refuses the hit as soon as one of them carries a **different** plan — the
question sits between two plans, so the LLM answers it. The comparison is on the **plan**, not
on the wording, which is what lets the four curated phrasings of the top-5-per-bucket question
keep hitting: they are near-duplicates of each other but point at one plan, so they are never
rivals.

*Looking at the runner-up alone is not enough, and this corpus is exactly why* — see the blind
pass below. With four phrasings sharing one plan, the two nearest neighbours of a question in
that cluster are the **same** plan, and the real rival sits at rank 2, unexamined. The scan is
bounded at 32 neighbours, which is a ceiling on how many same-plan near-duplicates can hide a
rival, not a limit on the check: it stops at the margin either way.

**The margin alone would have broken the demo.** Two curated one-click questions —
*"Which single customer has the largest overdue balance?"* and *"Who are the top 10 customers
by overdue balance?"* — are **0.9156** apart, so typing either exactly leaves a runner-up gap
of 0.0844, *inside* the margin. Every hit on those two chips would have fallen through to the
tiny CPU model. So an exact question short-circuits the check: the stored ID **is** the question
text (`warm` keys on it), so an exact match is a key lookup, not a nearest-neighbour guess — a
string comparison, with no float tolerance to get wrong.

"Exact" means the same *question*, not the same *bytes*: `_same_question` collapses whitespace
and case on both sides, one owner for the normalisation so the two sides cannot drift apart the
way two copies of a `strip` did in ADR-014. Byte equality was measurably too strict — a chip
typed with a trailing space or a lower-cased first letter embeds at distance **0.0000** and was
still refused, because it missed the shortcut and then tripped the margin on that 0.0844
neighbour.

**Where 0.10 comes from.** Not taste: it sits between two measured walls **on the semantic
path** — above the widest ambiguous gap (0.0482) and below the tightest legitimate paraphrase
gap (0.1589) — and closer to the safe side, which is the ordering this ADR already chose ("a
confidently-wrong number is worse than a slow one"). The walls are measured over paraphrases
because exact questions never reach the margin; without that qualifier the claim is false, since
two curated questions are 0.0844 apart. `tests/test_plan_routing.py` re-measures both walls on
every run, so the constant cannot drift away from the corpus that justifies it.

**Suite 382 → 396**, in two files on purpose. The mechanism (does an ambiguous neighbourhood
miss? does an exact question survive it?) is tested offline with the deterministic embedding in
`tests/test_plan_cache.py`. The **geometry** cannot be: a hashing stand-in says nothing about
where MiniLM puts two real questions, which is exactly why this defect lived under a green
suite. `tests/test_plan_routing.py` therefore embeds the real corpus with the real model — a
census of all 66 curated pairs, all 12 questions routed to their own plan, and the two walls.
It does **not** skip when the model is absent; CI caches the ONNX download instead, because a
routing suite that quietly skips is the missing owner it was written to replace. The cost is
honest: the suite goes from ~5 s to ~12 s.

**Mutations, in two batteries.** Against the first design, 17 code mutations, 16 red. Against the
code as it stands after the blind pass, **11 valid code mutations, 9 red**. The eleven, so the
ratio is checkable rather than asserted — each is a one-line edit to `src/agent/plan_cache.py`:
`AMBIGUITY_MARGIN` to 0.02 · to 0.30 (over-rejecting is also a defect) · `_NEIGHBOURS_SCANNED` to
2 · the neighbour loop bounded to the runner-up · the rival test by identity (`is not`) instead
of equality · `_same_question` to byte equality · to a substring test · the exact-match shortcut
taken unconditionally · the `PlanCache.__init__` copy of the threshold to 0.50 · the window
comparison `>=` to `>` · its `break` to `continue`. **The last two are the two that stay green**,
and limit 2 says why. Five limits worth stating:

1. **One relaxation still has a single owner** — loosening the exact match to a *substring* test,
   which would let *"Who are the top 10 customers by overdue balance? Only enterprise ones."*
   skip the ambiguity check and be served the plan for the question it merely contains. Measured
   twice: it passed the entire repo until an assertion was written for it, and deleting that one
   assertion re-opens it even now, with the byte-variant surface test in place (that test accepts
   the substring version, since a variant does contain its original).
2. Two mutations are **indistinguishable by construction**, not gaps: `>=` vs `>` on the window
   (no measured gap lands exactly on 0.10) and `break` vs `continue` on it (neighbours arrive
   ordered by distance, so nothing inside the window can follow something outside it).
3. The census's first count anchor was **tautological**: it compared the loop's own iteration
   count against a formula over the same list, so it held for any corpus. The blind pass found
   the same defect in two more places — the probe tables were looped with no anchor, so either
   could be shortened one row at a time and stay green. All three now pin literals.
4. **Ownership is uneven, on purpose.** Nine of the eleven reds have two or more owners (the
   mechanism suite and the routing suite catch the same relaxation from different angles); the
   corpus census is the single owner of "a new curated plan collides with an existing one", and
   the substring probe is the single owner of point 1.
5. The high red rate is not evidence of a strong suite in general: these mutations were written
   alongside the tests that catch them. The informative results are the ones that came out green
   — and the ones a blind instance found that no mutation of mine did.

**Then a blind pass attacked it, and two of the four claims fell.** An instance with no
knowledge of the design was given the four guarantees and the repo, and told to falsify them by
running code. It broke the first version of this fix in two places, both now closed and both
with the tests above:

1. **The ambiguity check read the runner-up, not the neighbourhood.** Four curated phrasings
   share the top-5-per-bucket plan, so for a question in that cluster ranks 0 and 1 are the
   *same* plan — the check compared a plan with itself and served the hit, while the real rival
   (the top-10 plan) sat at rank 2, **0.0738** away, inside the margin and never examined. Three
   reproducing questions, found in 3 of 108 generated probes. This is the enumerated-guard
   failure in its usual costume: the guard looked at a fixed *position* instead of the
   *surface* of neighbours the margin actually covers.
2. **"Exact" was byte equality, and over-blocked.** A shipped chip typed with a trailing space
   or a lower-cased first letter embeds at distance 0.0000 and was refused — the shortcut
   missed, and the 0.0844 neighbour then tripped the margin. The demo would have answered its
   own suggestion with the slow model.

It also confirmed the claim that matters most: **12 poisoned plans injected straight into the
cache** — `DELETE`, stacked `SELECT 1; DELETE`, CTAS, `COPY TO` a file, `read_csv_auto('/etc/passwd')`,
`INSTALL`, `ATTACH`, `UPDATE … RETURNING` — were all refused by `guard_query` on replay against a
`read_only=True` connection, row counts unchanged. The "cannot corrupt data, cannot serve a stale
number" half of point 3 survived the attack it was written for.

**What is still not guaranteed.** A paraphrase that is *confidently* closest to a plan that
answers a slightly different question is still served — the margin rejects ambiguity, not
wrongness, and no threshold can tell "closest" from "right". The blind pass produced a clean
example, still true after the fix: *"top 10 customers by overdue balance in each aging bucket"*
sits at **0.9841** from the top-5-per-bucket plan and is served — the right shape, the wrong N,
under an honest freshness banner. Its neighbourhood is not empty, and that detail is the point:
the runner-up is only 0.0853 behind, but every neighbour that close is **another phrasing of the
same plan**, so there is no rival for the margin to find. The rule behaves exactly as designed
and the answer is still to the wrong question. Fixing *that* is not a threshold problem; it needs
the model, which is the thing the cache exists to skip.

## ADR-008 — AI-native layer: shared-guardrail MCP server + property-based evals

**Status:** Accepted · 2026-06-08

### Context

Phase 5 adds the layer aimed at AI-native employers: an MCP (Model Context
Protocol) server, a Claude Code skill, and an eval suite. Three choices weren't
obvious: how the MCP surface relates to the in-app tool, how to name the package,
and how to judge a non-deterministic agent's answers.

### Decision

1. **The MCP server reuses the same guardrail — no second security model.**
   `mcp_server/server.py` exposes `query_ledger` over MCP by calling
   `run_guarded_query`, which uses the *same* `connect_readonly` +
   `guard_query` (SELECT/WITH only, allow/deny lists, single statement, row cap)
   as the in-app tool. A new surface must not be a weaker surface. The core is a
   plain function so it's unit-tested offline (`tests/test_mcp.py`) without an
   MCP client; the schema is exposed as a `schema://ledger` resource.
2. **Package named `mcp_server`, not `mcp`.** A top-level `mcp/` package (which
   `pythonpath=["."]` puts on `sys.path`) *shadows the MCP SDK's own `mcp`
   package*, breaking `from mcp.server.fastmcp import FastMCP`. Renamed to
   `mcp_server` to avoid the collision — caught immediately by the import test.
3. **Evals assert properties, not exact strings.** An LLM phrases the same fact
   many ways, so golden cases (`evals/golden.py`) check that the answer (a) used
   the right tool(s), (b) cites the governing policy keywords, and (c) states the
   right number within a tolerance — primitives in `evals/checks.py`. Numeric
   expectations are computed from the ledger (reproducible via the fixed seed)
   and policy keywords come from the policy doc, so a pass means *grounded in
   both sources*. The checks are pure and offline-tested; only the runner
   (`evals/run.py`, `python -m evals.run`) needs a live LLM.

### Consequences

- One guardrail, audited once, protects both the app and the MCP surface; a
  guardrail regression fails tests for both.
- The eval suite is a real regression gate (non-zero exit on failure) and the
  source of a defensible accuracy number for the README — captured on a machine
  with a live provider, never guessed.
- The `mcp` extra is optional (`pip install -e ".[mcp]"`); the web/API container
  doesn't carry it.

---

## ADR-007 — Single same-origin container; agent built once; key baked into the UI

**Status:** Accepted · 2026-06-08

### Context

Phase 4 turns the agent into a runnable "shipped link": a FastAPI service, a
React UI, and a one-command run that also has to work on a free cloud Space.
Three choices weren't obvious: (a) how to host the UI and API, (b) when to build
the agent, and (c) how the browser authenticates to a key-protected API on a
public demo.

### Decision

1. **One same-origin container.** A multi-stage `Dockerfile` builds the React
   bundle (Node stage) and serves it as static files from the same FastAPI app
   that exposes `/api` (Python stage). No separate web server, no CORS, no
   second service to orchestrate — and it matches what a free Space runs (one
   container, `$PORT`). In dev the two run apart (Vite proxies `/api`), so the
   UI is same-origin in both modes.
2. **Build the agent once, in an async `lifespan`.** Constructing the agent
   opens the read-only ledger and builds the policy index — startup work, not
   per-request. It lives on `app.state`; requests reuse it. `create_app` takes
   an injectable `agent_builder` so the HTTP stack is testable offline against a
   stub (no LLM), which is how `tests/test_api.py` runs.
3. **API-key auth, baked into the UI build.** `/api/chat` requires an
   `X-API-Key` matching `Settings.app_api_key` (constant-time compare);
   `/api/health` stays open for probes. The same key is baked into the bundle at
   build time (`VITE_API_KEY`) so the same-origin browser can call the API. It
   demonstrates the auth boundary without a login system; the key isn't a
   user-secret here (the data is synthetic), it gates the public demo endpoint.
4. **Generate the ledger at image-build time.** The synthetic ledger is
   deterministic and ~45 s to build, so `RUN python data/generate.py` bakes it
   into the image (the `.dockerignore` excludes any local `*.duckdb`) — the
   container is self-contained and needs no data volume.

### Consequences

- One artifact to ship and one command to run; the same image runs locally and
  on a Space by flipping `PRIMARY_PROVIDER=gemini` and setting secrets.
- The baked UI key is visible to anyone who inspects the bundle — acceptable for
  a synthetic-data demo, and rotating it is a rebuild. A real multi-tenant app
  would use per-user auth instead; out of scope here.
- The policy index's one-time ONNX embedding download still happens at first
  run (ADR-005), not build time — the first request after a cold start pays it.

---

## ADR-006 — Idempotent policy indexer (deterministic IDs + prune)

**Status:** Accepted · 2026-06-08

### Context

The policy index is rebuilt on demand — on the agent's first run, in tests, and
whenever the policy doc changes. A naive "embed all chunks and `add`" duplicates
every chunk on each run (ChromaDB's `add` appends), so retrieval would return
stale copies and counts would drift. The index must instead converge to exactly
mirror the current document, however many times it runs.

### Decision

Make `build_index` idempotent on two mechanisms:

1. **Deterministic chunk IDs.** Chunk on the policy's `##` sections (each is a
   self-contained, citable rule by design) and ID each as
   `"{source}::{heading-slug}"`. `upsert` by that ID overwrites a section in
   place — same input → same IDs → no duplicates, and an *edited* section
   updates rather than appends.
2. **Prune orphans.** After upserting the current chunks, delete any ID in the
   collection that the current document no longer produces, so a removed or
   renamed section can't leave a stale chunk behind.

The net guarantee: after any run, the collection equals the document — same IDs
and count regardless of history. This is the property the offline tests assert
(`tests/test_rag.py`: re-index → identical IDs/count; drop a section → it's
pruned).

### Consequences

- Safe to call on every agent build (`ensure_policy_index` reuses a populated
  collection; first run builds it once).
- IDs are heading-derived, so two sections must not share a heading; the policy
  doc uses unique `##` headings, and a collision would surface as a lost chunk
  in the count assertions.

---

## ADR-005 — Local embeddings for RAG, with an injectable function

**Status:** Accepted · 2026-06-08

### Context

`search_policy` needs to embed the policy chunks and the query. Two routes: a
**provider embedding API** (e.g. Gemini embeddings) or a **local model**. The
project ships as a self-contained "shipped link" that runs on a free cloud
Space and must keep unit tests offline — and the LLM provider is already
swappable (Ollama/Gemini, ADR-004), so coupling retrieval to one provider's
embedding API would re-introduce a key/quota/network dependency the rest of the
design avoids.

### Decision

Use **local embeddings** by default: ChromaDB's bundled ONNX `all-MiniLM-L6-v2`
(`DefaultEmbeddingFunction`) — no API key, no per-call cost, runs in-process via
`onnxruntime`. The model file downloads once on first use and is cached.

Make the embedding function **injectable** (`build_index`/`get_collection` take
it as a parameter). That keeps retrieval decoupled from the LLM provider and
lets the tests pass a `DeterministicEmbeddingFunction` — a dependency-free
hashing bag-of-words vectorizer that needs no download, so the suite indexes and
retrieves fully offline and reproducibly.

### Consequences

- The demo Space embeds locally; no embedding key or quota, and retrieval is
  independent of which LLM provider is active.
- One-time MiniLM download on first run (cached after). In a network-restricted
  environment the injectable hashing function is a no-download fallback.
- The deterministic function is lexical, not semantic — fine for the tests'
  shared-vocabulary assertions, but it is a test/fallback aid, not the shipping
  retriever.

---

## ADR-004 — Dual-provider fallback via a dynamic-model callable

**Status:** Accepted · 2026-06-08

### Context

The agent must run on two interchangeable LLMs: a local one (Ollama) in dev and
a cloud one (Gemini) on the demo Space, with **active fallback** if the primary
fails at call time. LangChain expresses fallback as
`primary.with_fallbacks([secondary])` — but LangGraph's `create_react_agent`
auto-binds tools to a *single* `BaseChatModel` and rejects a `RunnableWithFallbacks`
passed as `model` (it is neither a chat model nor a `RunnableBinding`).

### Decision

Bind the tools to **each** provider, combine them with `with_fallbacks`, and
hand the result to `create_react_agent` as a **dynamic model callable** — a plain
`(state, runtime) -> model` function. LangGraph treats a callable as a pre-built
model and skips its own tool-binding, so the fallback runnable flows through
every model turn. The primary/fallback order is config-driven; setting
`PRIMARY_PROVIDER=gemini` inverts dev↔deploy with no code change. The fallback is
wired only when it has credentials, so an Ollama-only box still builds.

### Consequences

- One agent definition serves both environments; the demo Space just flips an
  env var. Provider SDKs are imported lazily, so neither is a hard dependency.
- The callable signature is coupled to a LangGraph internal contract; pinned via
  `langgraph>=1.0` and covered by a build smoke check.

### Amendment 1 · 2026-07-31 — the premise changed, and the last bullet above was wrong

> ⚠️ **Point 1 below was itself wrong — see Amendment 2.** It is kept as written
> because how it went wrong is the useful part. Points 2 and 3 stand, with point 3
> reaching the wrong conclusion about what migration costs.

Re-measured against the installed **langgraph 1.2.4 / langchain-core 1.4.0** (this ADR
was written against 1.0). Three corrections, all reproducible with the snippet in
`aprofundamentos/receivables-agent/R1-T3…md` §4-E4:

1. **"rejects a `RunnableWithFallbacks` passed as `model`" no longer holds.**
   `create_react_agent` now *accepts* it at construction and **silently discards the
   fallback**: the primary's exception propagates, while the same runnable falls back
   correctly when invoked on its own. The decision here is unchanged — the dynamic-model
   callable is still what keeps the fallback live — but the failure it guards against went
   from a loud construction error to a silent loss of redundancy.
2. **The API is deprecated.** `langgraph.prebuilt.create_react_agent` warns
   `LangGraphDeprecatedSinceV10` — removal announced for v2.0 — and `pyproject.toml` pins
   `langgraph>=1.0` with **no upper bound**. The successor, `langchain.agents.create_agent`,
   **rejects the dynamic-model callable** (`AttributeError: 'function' object has no
   attribute 'bind_tools'`), so migration requires rethinking how the fallback is composed,
   not swapping an import.
3. **"covered by a build smoke check" is false.** No test calls `build_agent`. Measured:
   deleting the fallback wiring entirely (`model = primary`), dropping the credential check,
   or dropping the `fb != primary` check each leaves the suite at **346 passed**. The
   mechanism this ADR exists to describe has no test at all.

Follow-ups tracked as `R1-C7` in `sistema/APROFUNDAMENTOS_ROADMAP.md`: a smoke test that
proves the fallback actually fires, and a version ceiling on `langgraph`.

### Amendment 2 · 2026-07-31 — writing the test falsified Amendment 1

Amendment 1 claimed the direct pass *silently discards* the fallback. It does not. That
measurement was an artefact of the test double used to take it, and writing the missing
test is what exposed it — the same experiment against a differently-shaped fake produced
the opposite result.

**What actually happens.** `create_react_agent` sees something that is not a
`RunnableBinding`, so it calls `.bind_tools(tools)` on it (`_should_bind_tools`).
`RunnableWithFallbacks` has no such method, so `__getattr__` runs, and it decides what to
do by reading the **return type annotation** of the wrapped model's `bind_tools`
(`_returns_runnable`):

- annotated as returning a `Runnable` → the call is **broadcast** to the primary *and*
  every fallback, and the wrapper survives with the fallback live;
- not annotated → the primary's bound method is returned alone and **the backup is
  dropped in silence**.

Amendment 1's fake declared no return type; it measured the second branch and reported it
as the library's behaviour. Measured here: `ChatOllama`, `ChatGoogleGenerativeAI` and this
repo's own `ConstrainedToolModel` all declare a `Runnable` return, so on the real code path
the direct pass **keeps the fallback**. Two fakes differing only in that annotation take
opposite branches — pinned by `test_the_direct_pass_survives_only_by_a_return_annotation`.

**What this changes:**

1. **The decision stands, for a new reason.** The callable is no longer a workaround for a
   rejection; it is what makes the fallback independent of a type-hint heuristic in
   `langchain_core.runnables.fallbacks`. That is a thin thing to bet a production backup on.
2. **Migration is cheaper than Amendment 1 said, not dearer.** `langchain.agents.create_agent`
   rejects the callable *and accepts the wrapped runnable directly, fallback intact* (measured
   on langchain 1.3.4). Porting means **deleting** the callable, not redesigning the fallback.
3. **The gap Amendment 1 found is closed.** `tests/test_provider_fallback.py` covers the
   wiring offline: the backup answers when the primary raises, the order is config-driven,
   and no fallback is wired for a keyless provider, for a fallback equal to the primary, or
   for none at all. Measured: **7 of 7 mutations of `build_dynamic_model` now fail**, the two
   *relaxing* ones included (dropping `has_credentials` = 1 red; dropping `fb != primary` =
   1 red), where all three left the suite fully green before. Suite **346 → 359**.
4. **`langgraph` is capped at `<2`** in `pyproject.toml` and `requirements.txt`, so the
   announced removal of `create_react_agent` arrives as a deliberate upgrade.

**Standing caveat:** the annotation guard for the two cloud/local providers is skipped where
their extras are absent, which includes CI (`[dev,mcp]`). It guards a dev box, not the pipeline.

---

## ADR-003 — Defense-in-depth guardrail for the text-to-SQL tool

**Status:** Accepted · 2026-06-08

### Context

The agent writes SQL from natural language and runs it against the ledger. That
is the project's main attack surface: prompt injection could try to mutate data,
read the filesystem (DuckDB's `read_csv`/`COPY`), or exfiltrate via stacked
statements. A single prompt-level filter is not trustworthy on its own — LLM
output is adversarial-by-default.

### Decision

Two independent layers, neither of which may be relaxed "to make a query pass":

1. **Read-only connection** (authoritative). The ledger is opened
   `duckdb.connect(path, read_only=True)`; the engine itself refuses every
   write/DDL regardless of what the prompt produced.
2. **Prompt-layer filter** (`sql_guard.guard_query`, fast feedback for the
   ReAct loop): a string-literal-aware scanner that strips comments and splits
   on top-level `;` (rejecting stacked statements), requires `SELECT`/`WITH`,
   applies a deny-list of write/DDL/catalog/filesystem keywords, checks every
   referenced relation against an **allow-list** (CTE names excepted), and wraps
   the query in an outer `LIMIT` so the model can never dump the ledger. Keyword
   and relation matching run on a copy with string contents masked, so data like
   a customer named `'DROP TABLE x'` is not mistaken for an attack.

Rejections and SQL errors are returned to the model as text (not raised) so the
loop can read the reason and self-correct.

### Why not just one layer

- Prompt-only is bypassable (novel encodings, parser gaps). The read-only
  connection is the hard stop.
- Connection-only gives the model opaque engine errors and still lets it read
  *any* table; the allow-list + row cap scope it to the intended relations and
  keep tool output small. Each layer covers the other's weakness.

### Consequences

- **Positive:** mutations are impossible by construction; the filter is unit-
  testable offline (the priority `tests/test_sql_guard.py` suite — injection and
  over-blocking cases) with no LLM or DB.
- **Limits:** the relation extractor is a regex/token scanner, not a full SQL
  parser; it is best-effort at the prompt layer and leans on the read-only
  connection as the backstop for anything it doesn't model.

---

## ADR-002 — Project name avoids third-party trademarks

**Status:** Accepted · 2026-06-08

### Context

The project was first sketched as "receivables-copilot". "Copilot" is a Microsoft
product brand; using it in a public portfolio repo invites trademark confusion
and dates the project to a single vendor's framing.

### Decision

Name it **receivables-agent**. It is accurate ("agent" describes the ReAct
architecture), vendor-neutral, and carries no third-party mark.

### Consequences

- No brand collision; the name describes the architecture, not a product.
- One-time rename of the repo/package early, before any external links exist.

---

## ADR-001 — DuckDB over Spark/Pandas for data generation and queries

**Status:** Accepted · 2026-06-08

### Context

The project needs (a) to *generate* ~1M+ synthetic invoices with realistic,
signal-carrying distributions, and (b) to *serve* read-only analytical queries
to the agent's text-to-SQL tool (aging, DSO, per-customer AR). The deliverable
is a runnable "shipped link": it must start with a single command and run on a
laptop or a free cloud Space — no cluster, no external database service.

The candidate engines:

- **Pandas** — generate row-by-row in Python, hold everything in memory.
- **Spark / PySpark** — a distributed engine built for data that does not fit on
  one machine.
- **DuckDB** — an embedded, columnar, vectorised SQL engine (the "SQLite for
  analytics"): a single file, an in-process library, full analytical SQL.

### Decision

Use **DuckDB** as both the generation engine and the query engine.

- **Generation is set-based in SQL.** Faker produces only the small customer
  dimension (13k rows) in Python; the ~1M-row fact tables are produced by one
  `CREATE TABLE ... AS SELECT` that cross-joins each customer with a correlated
  `range()` and draws every random attribute (lognormal amount via Box-Muller,
  exponential days-late, Bernoulli default) in SQL. The whole ledger builds in
  ~45 s into a 21 MB file.
- **Serving is the same file.** The agent opens `ledger.duckdb` on a *read-only*
  connection — which also reinforces the SQL guardrail (Phase 2): even a
  jailbroken prompt cannot mutate the database.

### Why not the alternatives

- **Pandas:** a Python loop over a million rows is slow and memory-hungry, and it
  separates "generation" from "query" into two mental models. DuckDB lets the
  same engine that serves the data also generate it, set-based.
- **Spark:** built to scale *across machines*. At ~1M rows / 21 MB on a single
  node, the JVM, the cluster/session overhead, and the deployment weight are all
  cost with no benefit — it would actively work against the one-command,
  free-Space constraint. Spark earns its keep when data outgrows one machine;
  this data does not, by design.

### Consequences

- **Positive:** one dependency, one file, one-command run; trivial to ship to a
  free Space; the read-only connection is a security primitive, not just a perf
  choice; set-based generation is fast and fully reproducible (fixed seed +
  single thread → deterministic `random()`).
- **Negative / limits:** single-node only; if a future version needed
  genuinely large or streaming data this decision would be revisited.
- **Deferred:** PySpark is intentionally out of scope here and parked for a
  dedicated data-engineering showcase where distributed data justifies it.
  Reaching for Spark on this dataset would be over-engineering.
