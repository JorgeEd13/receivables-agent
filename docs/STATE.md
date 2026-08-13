# STATE — receivables-agent

> Volatile, short, current. Update at the end of each work session.

## Current focus

**ADR-022 — the SQL guardrail was broken, and is now rebuilt on DuckDB's own parser. ✅ SHIPPED
2026-07-30.** An external review found one bypass (the relation allow-list never looked inside a
derived table). Repairing it triggered **three rounds of adversarial review by an instance with
no knowledge of the design**, which found much worse — all verified end-to-end against a real
connection, not argued on paper:

- `_mask_literals` modelled `'…'` but **not** dollar-quoting (`$a$…$a$`), escape strings
  (`e'\''`) or quoted identifiers containing an apostrophe. Any of the three flips quote parity
  and blanks **the entire rest of the statement** out of every check — while the unblanked
  original is what executes. Read `duckdb_settings()`, the ledger's file path, forbidden tables.
- The same desync hid `;`: closing the guard's own `SELECT * FROM (…)` wrapper early made
  **`CREATE TEMP TABLE` / `VIEW` / `MACRO` and `PREPARE` succeed on a `read_only=True`
  connection**, persisting for the session.
- `SHOW ALL TABLES` enumerated the catalog past two empty allow-lists; CTE names exempted tables
  in sibling scopes, forward-declared, and inside their own bodies; a CTE name with a quoted dot
  defeated the schema check.

**The scanner was replaced, not patched:** the statement is parsed by the **same parser that
executes it** (`extract_statements` + `json_serialize_sql`) and every check runs on the syntax
tree, so parser-vs-executor disagreement is impossible by construction rather than merely fixed.
Full reasoning in **ADR-022**.

**Measured, and it changed a decision:** on DuckDB 1.5.3 the **anchor** term of
`WITH RECURSIVE x AS (SELECT c FROM x UNION ALL …)` binds to a real base table named `x` and
returns its rows — so "a CTE's own name is exempt in its own body" cannot be made safe by
restricting it to the recursive form. A CTE's own name is therefore **never** in scope inside its
own body, `RECURSIVE` or not; recorded in `tests/test_sql_guard_adversarial.py` with the
measurement.

**Precise wording (this line used to be wrong — see the correction block in ADR-022):** there is
**no recursion-specific rule** in the code. A self-reference is checked against the relation
allow-list like any other name, so `WITH RECURSIVE t AS (… FROM t …)` is refused because `t` is
not allow-listed, while `WITH RECURSIVE invoices AS (… FROM invoices …)` is **accepted**. Saying
"recursive CTEs are refused" is false and gets falsified by one example.

**It also got LESS restrictive where it was wrong:** `EXTRACT(year FROM d)`, `SUBSTRING … FROM …
FOR`, `TRIM(BOTH … FROM …)`, `DECIMAL`/`VARCHAR` casts, `WITH t(a)`, `now()`, **`INTERVAL 30
DAY`** and schema-qualified references to allow-listed tables were all being refused. The old
relation scanner read the `FROM` inside `EXTRACT` as a relation list.

**319 tests** (135 pre-existing + 126 adversarial kept as a regression floor + 11 from the
catalog fix below + 19 from the normalization fix below + 28 from the "what the suite did not
pin" block below). **First CI
workflow this repo has ever had** — there was none, so nothing ran unless someone remembered —
plus ruff + mypy gates, both clean.

**2026-07-30, comments only (no behaviour change).** Walking the adversarial suite line by line
surfaced that its section headers describe each defect **in the present tense**, and three had
gone false: §5 claimed only `enable_external_access=false` stops a file string used as a relation
(the tree walk refuses it — `Relation(s) not in allow-list: /etc/passwd`), and §7 / R2-5 claimed
their over-blocking cases are "refused today" when all 13 pass. Rounds 1 and 2 have closing
banners; round 3 has none, because it was last — so its header still describes a walk that no
longer exists. Fixed by adding a "how to read the section headers" note to the module docstring
(the headers are a record of the break, the assertions are the present tense) plus a round-3
closing note, and by moving the three false sentences to the past tense.

**2026-07-30 — §1 of the adversarial suite was a section that proved nothing (tests, no behaviour
change). 319 → 324 tests.** The same walk that produced the note above missed one header: §1
still described the literal-masking desync as live, and its comments name `_mask_literals`, a
function ADR-022 deleted. Measured: the three desync payloads are all refused by the *same*
sentence, `Function(s) not in allow-list: duckdb_settings.` — the quoting construct contributes
nothing, the payload does all the work, and with the payload stripped all three are accepted (and
should be). Fixed three ways: a dated closing banner on the section; the refusal assertion now
asserts the **reason** (`match=`), so a payload that rots into a parse error turns the section red
instead of green — measured as a pair, rot with the reason asserted is 3 red, rot without it is
324 green; and four new cases pin the property that was left standing, that an apostrophe inside
`$a$…$a$`, `$tag$…$tag$`, `e'…'` or `"…"` reaches the engine unchanged. The two cases carrying
content around the apostrophe are the ones that discriminate — a masker mutation leaves `''''`
untouched, so a payload made only of the escaped character cannot tell mangling from fidelity.
Three mutations of the new test code stay green; that is recorded in the section, not hidden.

**2026-07-30 — `catalog_name` was never read (fixed, ADR-022 amendment).** A blind audit of
`sql_guard.py` — two readers with the code and nothing else, no design notes — found that the
tree walk read two of the three qualification fields. A three-part name puts the database in
`catalog_name` and `main` in `schema_name`, so `SELECT * FROM evildb.main.customers` and
`SELECT * FROM "/tmp/other.duckdb".main.customers` were **accepted**. Latent, not live: nothing
could be attached (`ATTACH` does not serialize as a select; the connection has
`enable_external_access=false` + `lock_configuration=true`). Now **any** catalog-qualified
reference is refused, naming the full path — no allow-list of catalog names, because the ledger's
own catalog is just its file name. The price is knowingly paid: `ledger.main.customers` is
refused too, and a test pins that.

**The pre-existing test named `three_part_catalog_qualified` was passing for the wrong reason** —
`system.information_schema.tables` is refused on `tables`, which is not allow-listed either way,
so it stayed green with the hole wide open. The new R4 block uses payloads where the schema and
the bare name are both allow-listed, so only the catalog check can refuse them, and asserts on
the **message**: end-to-end blocking proves nothing here, since a catalog that does not exist is
refused by DuckDB regardless of the guard.

**2026-07-30 — the guard executed the caller's text, not the tree it validated (fixed, ADR-022
amendment).** Same blind audit, two false positives the product pays for:

- `SELECT name || ' x' FROM customers` was **refused** on `||` while `concat(name, ' x')`
  passed — the same operation, two spellings, decided differently. `^` was missing against
  `pow`/`power`, which are both listed. Both added; string concatenation is the most common
  idiom in a collections report. The other 24 operator-named functions stay off the list on
  purpose (all 28 are `function_type = 'scalar'`, so none of them could read a file either way).
- `SELECT count(*) FROM invoices; -- total` was **refused** with a DuckDB syntax error about
  `LINE 2` — the guard's own wrapper. A loop peeled trailing separators off the text by hand and
  peeled only what was literally last, so the `;` travelled into `SELECT * FROM (…)` and broke it
  there. A trailing comment is something a model writes constantly.

**The repair replaced the text handling instead of patching it:** the statement that gets wrapped
is now **printed by DuckDB from the validated tree** (`json_deserialize_sql`), and the printed
text is re-parsed and compared with that tree before anything is wrapped. Comments and separators
are lexical — they never reach a tree, so they cannot come out of a print. Measured before
adopting: over every payload both guard suites accept, **27 of 27** round-trip to an identical
tree (dropping `query_location` offsets), with identical collected names, idempotently. The
peeling loop is gone; the caller's text now reaches nothing but the parser.

**Two mutation results recorded because they are the unflattering ones:** dropping the fixed-point
comparison leaves the suite green except one test that has to fake a lying printer, and the branch
that rephrases a wrapper failure is unreachable by any real payload. Both are insurance against a
future DuckDB release or refactor, and neither can be proved by a query — so both say so in the
code rather than posing as live checks.

**2026-07-30 — what the suite did not pin (fixed, ADR-022 amendment).** Third and last finding
of the same blind audit, and the only one that was not a bypass: three things the guard depends
on had no test holding them. Each was demonstrated by a mutation that left all **291** tests
green — `+ read_text/read_blob/sniff_csv` to the function list (and
`SELECT * FROM read_text('/etc/passwd')` comes back guarded), `- payments/communications` from
the relation list (and `SELECT * FROM payments` comes back refused), `+ information_schema` to
the schema list. Every test proved a name *absent* from a list is refused; none proved the lists
hold the right names. **The lists are now checked against a surface, not against more examples:**
the function list against every table function in `duckdb_functions()` (five row generators named
as the only exceptions), the relation list against the ledger's own catalog partitioned into
allowed/forbidden, the schema list by payloads whose bare name is allow-listed — the only form
only the schema check can refuse.

**`guard_query` promised `GuardrailError` "on any violation" and five inputs escaped it.**
`guard_query(123)` → `AttributeError`, `max_rows="5"` → `TypeError`, `max_rows=float('inf')` →
`OverflowError`, none of which `tools.py` / `mcp_server/server.py` catch; plus two that did not
raise at all — `max_rows=2.9` truncated to `LIMIT 2` silently and `max_rows=10**30` produced a
LIMIT with thirty zeroes, i.e. no cap on the surface whose job is capping. Arguments are now
validated like any other input, with a `MAX_ROWS_CEILING` the guard owns (`Settings.max_rows`
bounds only the env var; a test compares the two so the copies cannot drift). And
`SELECT 1+1+1+…` at 494 terms / 996 characters **crashed** with `RecursionError` inside the tree
walk — DuckDB's own limit only trips at 1000, so that whole band was a crash instead of a
decision, at a threshold that moved with the caller's stack. The walk now refuses past 250
frames, against a measured worst case of **18** over every accepted payload.

**Mutation results, including the ones that do not flatter the work.** Twelve mutations of the
guard, all red (poisoned function list 1 · two relations removed 2 · `internal_notes` added 35 ·
schema list opened 3 · `sql` type check 3 · `max_rows` type check 3 · ceiling 2 · ceiling drift
1 · depth guard 1 · depth limit set to 4 → 101 · `SELECT 1` stand-in 19 · ROUND 4 hole re-opened
9). The audit's claim that *nothing* pinned the wrapper's contents was **overstated and is
corrected**: the stand-in passes all 18 `test_allowed` cases but 12 other tests do fail — all of
them end-to-end ones that execute the result. The property was held by accident, not by
statement. And of seven mutations applied to the **tests** instead of the guard, **six stay
green**: nothing covers a test file, which is why the mutations get run.

> ✅ **CLOSED 2026-07-31 — the same leak through the execution path.** An error raised while
> *executing* the guarded query returned the wrapper's line numbering to the model, via
> `tools.py` and `mcp_server/server.py`: `Binder Error: … LINE 2: SELECT count_star() FROM
> customers GROUP BY missing`. Severity was product quality, not confidentiality — this repo is
> public, so the wrapper's shape is not a secret; the cost was that a model trying to
> self-correct is handed a line number into a query it did not compose.

**2026-07-31 — the execution path stopped quoting the wrapper (ADR-022 amendment). 324 → 336
tests.** `strip_wrapper_line_echo` drops DuckDB's source echo and its caret from execution
errors; both tool surfaces call it. The diagnosis is kept whole, `Candidate bindings` included —
a message stripped of its reason costs the model more than a wrong line number does.

Three things measurement changed about the obvious implementation. **The line number is not a
constant:** a string literal containing a real newline moves the failure to `LINE 3`, so a strip
pinned to `LINE 2` leaks on exactly the queries hardest to read. **The echo can be forged from
inside the query:** a quoted identifier may carry a newline, so `SELECT "a\nLINE 9: injected"
FROM invoices` plants an echo-shaped line *above* the genuine one, inside `Referenced column …` —
cutting at the first match would delete the Candidate bindings below it, handing the caller a way
to blank its own diagnosis. The cut is anchored at the tail and requires the caret line under it.
**Rewriting the number instead of dropping it is worse:** the echoed text is DuckDB's print of the
validated tree (`amount_due::INTEGER` → `CAST(amount_due AS INTEGER)`), so a caret shown against
the caller's own text would point at the wrong character.

Sweep of 20 failing queries on DuckDB 1.5.3: **14** carry the echo, in all 14 it is the last two
lines; the other 6 pass through untouched. Unrecognised shapes are returned unchanged on
purpose — a future DuckDB brings the leak back rather than eating the diagnosis, and the tests
that drive real queries into the binder go red on that upgrade.

**Mutation: 10 of 10 red** — either call site back to `str(exc)` 1 each · strip as a no-op 8 ·
caret requirement dropped 2 · cut at the first match 3 · `LINE 2` hardcoded 1 · over-cut to the
first line 6 · and the two **relaxations**, which is where the round earned its keep: widening
the caret to `^ *\^.*$` and the head to `^LINE ` both left all 335 green until a test was written
for the shape itself, 1 each. **The fixture was the other lesson:** typed with `DECIMAL` instead
of the ledger's `DOUBLE`, and later left with zero rows, it made `WHERE amount > 'x'` *succeed*
and silently dropped the case — a fixture one type or one row from production is a different
test, not a smaller one. On the tests instead of the code, **6 of 6** assertion-gutting mutations
stay green.

**2026-07-31 — section 2 of the adversarial suite was testing a dead payload. 336 → 346
tests.** The "nothing may be created" block relied on the literal-masking desync that ADR-022
removed; without a masker, its payload is just a syntax error in the caller's text, and
`contextlib.suppress` hid that as readily as it hid the refusal. **Measured: `guard_query`
removed from both end-to-end calls left the suite at 336 green — zero red.** Worse, deleting the
SELECT/WITH-only check in the guard *also* left 336 green: the first guarantee in the file's own
threat model was asserted by exactly the two tests that had stopped asserting.

Rewritten with live payloads — `CREATE TEMP TABLE`, `CREATE TEMPORARY VIEW`, `PREPARE`,
`CREATE TEMP MACRO` — plus a probe that asserts the engine really does accept each one when the
guard is out of the way (a read-only connection is read-only against the *file*; its `temp`
catalog is writable). Removing `guard_query` is now **8 red**. **The half that cuts the other
way:** neither guard mutation is a bypass — the refusal moves one layer down, to
`json_deserialize_sql`. Relaxing the statement count to allow a trailing statement, the shape a
real PR takes, is 6 red and still not a bypass. The guarantee is held redundantly by three
layers; what the new tests pin is **which layer speaks**, not whether the object appears. On the
tests instead of the code, **4 of 6** mutations stay green.

> ⚠️ **OPEN — availability.** The guard bounds what can be READ, not how much WORK a query
> may do. `WITH RECURSIVE invoices(n) AS (… n < 100000000) …`, `repeat('a', 1000000000)` and a
> six-way self-cartesian join are all **accepted** and never return; the outer `LIMIT 200` caps
> rows *returned*, not rows *computed*. Confidentiality held under every vector tried.
> Needs a design call (watchdog interrupt / row budget / killable subprocess) — see ADR-022.

> ⚠️ **OPEN — the live demo still runs the vulnerable guard.** Re-measured 2026-07-30 after the
> pinning fix: `git rev-list --count space/main..origin/main` = **54**, this commit included
> — re-measured **2026-07-31 after `R1-C9`: 66** (50 → 52 → 54 → 56 → 58 → 61 → 63 → 66; every
> guard round widens it). ⚠️ The `63` was taken *before* that session's own STATE commit landed,
> so it undercounted by one; the honest run-up from there is 64 → 65 → **66 with this line
> included**. Of those 66, `R1-C5` shipped `src/` + `tests/`, so the demo is behind on the
> error contract too; `R1-C6`, `R1-C7`, `R1-C8` and `R1-C9` are tests + CI + docs only and add count
> without adding behavioural drift — `R1-C7`'s one runtime-visible change is the `langgraph<2` cap, which only matters when the
> Space image is next rebuilt. The deploy target is the **`space`
> remote** (`space/main`), not a `space-deploy` branch — that branch no longer exists on `origin`
> and the earlier "43 commits behind `space-deploy`" line named a target that is gone. `git push
> origin` does not deploy the HF Space. Still needs a dedicated session: the cherry-pick could not
> be validated because checking the deploy tree out fails on an LFS smudge error
> (`assets/logo.png`). Note what is actually live there: `space/main` predates ADR-022 entirely,
> so the demo runs the **string scanner**, not the parser-based guard — the catalog hole fixed
> above is the smaller of the two gaps.
>
> **Re-measured 2026-08-01 (`git rev-list --count space/main..HEAD`): 69 before this session's
> commit, 70 with it** — counted, not incremented by hand, which is why it does not line up with
> the run-up above (that count stopped being maintained after `R1-C9`). New in kind: `R1-C12`
> changes **runtime behaviour** (the dedup key), so the live demo is no longer behind only on
> tests and docs — there, two questions differing inside a string literal still collapse into
> one, and the second is answered with the first's rows.
>
> **Re-measured 2026-08-05 after `R1-C15`: 74.** Last actual deploy: **2026-07-08**, so *nothing*
> from the R1 audit is live. What that now means for a visitor, in the order it would be noticed:
> the three policy-only one-click chips still answer *"the query was re-run live against the
> ledger, so the numbers are current"* over **zero** executed SQL (fixed here today, not there);
> a question sitting between two plans is still answered by a neighbour (`R1-C13`); a broken cache
> entry can still surface as an error rather than a slow answer (`R1-C14`); and the guard is still
> the pre-ADR-022 string scanner. **This is the largest open gap in the project and it is not a
> code problem** — the cherry-pick cannot even be validated because checking the deploy tree out
> fails on an LFS smudge error (`assets/logo.png`). It needs its own session, and one that starts
> by fixing the LFS fetch, not by picking commits.

> ⚠️ **Verification lesson from this session, worth more than the fix.** The lint gate was
> reported green locally and failed in CI minutes later. The check was
> `ruff check . -q | grep '^Found'` — and `-q` **suppresses that very line**, so the grep could
> never match and "clean" was printed unconditionally. Verify tools by **exit code**, never by
> grepping their output. Also: adding a CI workflow and not polling `gh run list` in the same
> session leaves a new gate indistinguishable from a passing one.

**Phase 9 — demo product-polish (light/dark theme + EN/PT-BR i18n) — ✅ SHIPPED 2026-07-06
(ADR-015).** The React chat UI was single-theme (dark navy) and English-only; Phase 9 adds a
**light/dark theme** (CSS custom props; `prefers-color-scheme` default + a persisted
`data-theme` override that wins both ways — `theme.js`) and a **lightweight EN/PT-BR i18n
layer** (string dict + `translator()` closure, no framework — `i18n.js`), with theme +
language toggles in the header. **Honesty boundary held (ADR-015):** i18n localizes the **UI
shell only** — the agent answers from the model + the **English** policy corpus and is NOT
machine-translated; an `i18nNote` says so in both languages. **The seeded example questions
stay English in every locale on purpose** — they are plan-cache keys (0.90-similarity, ADR-009)
*and* the text sent verbatim to the English-corpus agent, so a PT-BR paraphrase would miss the
cache and hit the slow tiny model. `npm run build` green (34 modules, was 32; `web/dist`
rebuilt for the container). No regression to the streaming/plan-cache paths. **Paired with
`forge-pdm-mlops` F9 (ADR-018)** for one shared design language (hypercube navy+cyan). Front-end
showcase (a strong, under-shown axis). ADR-015 written; PLAN Phase 9 done.

> **Prior:** Phases 0–5 done. Phase 6 L1 (plan-cache) + L2 (grammar-constrained, ADR-011) +
7.1 (hardware-aware, ADR-010) done. Suite **110 green**. **Phase 6 LAYER 3 SHIPPED
+ LIVE (ADR-012): the self-contained tiny-Ollama Space is deployed and answering.**
🚀 **https://jorgeed-receivables-agent.hf.space** — `/api/health` ok, a seeded
question replays in ~3 s via the plan-cache (real `query_ledger`, 13,000 customers).
Built, smoke-tested, AND deployed from the DESKTOP (Docker + git push to HF both work
here). README carries the live link. **NEXT — Layer 4: tiny-vs-STRONG number** on the
notebook GPU (run `evals.run` + one latency vs a strong model for the "shines with a
better model" delta); everything else for the live link is done.

**Phase 8 — agent reliability at the tiny-model budget ceiling — ✅ SHIPPED 2026-07-05 (ADR-014).**
All of it, two same-day slices, **suite 134 green**: dedup (8.2) + forced finalization (8.1/8.3) +
progress narration with a raised narrated cap split from a soft wall-clock budget (8.5); the
"continue" affordance (8.4) is satisfied-by-design (history-resend), no checkpointer added. Details
below + in PLAN.md / ADR-014. Original scoping (IMPROVE the existing ADR-013 graceful ceiling, do
NOT add one): Motive: observed the tiny model burning its 8-step budget on redundant tool calls then
dead-ending on a generic apology that "reads as 'I just don't work'." Fixes: richer partial
answer + redundant-call dedup + budget-aware forced finalization + `thread_id`/checkpointer
"continue" affordance + **progress narration (Jorge's insight)**. Key reframe: the cap of 8 fused
a *loop guard* and a *wait guard* — the 200–250s thrash was bad because it was **silent + answerless**,
not merely long; so **narrating progress lets the cap RISE safely** (paired with dedup so extra steps
are productive + forced finalization so it always answers), plus split off a **soft wall-clock budget**
as the real wait guard. NOT "blindly raise the cap" (that re-creates the silent thrash). **Phase 9
— demo product-polish** (light/dark theme + EN/PT-BR i18n on the React UI; paired with
`forge-pdm-mlops` F9). Both scoped from Jorge's 2026-07-04 review; ADR-014 (Phase 8) / ADR-015
(Phase 9) when built.

> **2026-07-05 — Phase 8 follow-up (from live testing): curated top-N-per-bucket plan + richer
> no-gather finalization.** Live test surfaced a real gap: a natural follow-up — *"give me the top 5
> of each age group"* — hit the ceiling and returned the bare no-progress floor. Two fixes: **(1)** a
> **curated plan** (`data/curated_plans.py`) for top-5-customers-*within-each*-aging-bucket — a
> `QUALIFY row_number() OVER (PARTITION BY aging_bucket …)` window query the tiny model reliably
> can't write — under 4 phrasings + a 5th UI chip, validated against the live ledger (20 rows, 5/bucket);
> now instant + correct instead of a failure. **(2)** richer finalization: when the model *attempted*
> the ledger but every query **errored** (vs gathered nothing at all), `finalize_answer` now names the
> attempt + gives a concrete decomposition (*"ask for one group at a time, e.g. the top 5 in the 90+
> bucket"*) instead of the generic floor (`_NO_USABLE_RESULT` vs `_NO_PROGRESS`). +2 tests, **suite
> 135**, UI rebuilt. ADR-014 (the "widen curated seeds" deferred item, now partly done).
>
> **2026-07-05 — Phase 8 slice 2: progress narration + step/time guard split (8.5), 8.4 closed.**
> The narrated streaming path now streams a human `step` line per tool start/end (`narrate_start`/
> `narrate_end`: "Checking the collections policy on '…'" → "Found 12 rows in the ledger"), rendered
> live by the UI (`web/src/App.jsx`, coalescing repeats). ADR-013's single step cap is **split into
> two guards**: the *silent* `invoke`/`ainvoke` path keeps the tight cap (8, `agent_recursion_limit`)
> because a silent long wait reads as hung; the *narrated* `astream` path gets a higher cap (16,
> `agent_narrated_step_cap`) **plus** a soft wall-clock budget (45 s, `agent_wall_clock_budget_s`)
> that narrates a wrap-up and finalizes — a visible, time-bounded, productive run instead of a silent
> thrash. The cap only rose *because* narration + dedup + finalization ship with it. Clock is
> injectable (tests don't fight asyncio's own `time.monotonic`). **8.4 (checkpointer "continue") is
> closed as satisfied-by-design:** the API already resends full history per turn, so a follow-up
> carries context and 8.1's next-step invite makes it useful — a checkpointer would double-count
> history (decision in ADR-014). +4 tests (narration lines, `step` events on a normal run, wall-clock
> early-finalize). **Suite 134.** UI rebuilt (`npm run build` OK). Phase 8 COMPLETE.
>
> **2026-07-05 — Phase 8 slice 1: graceful ceiling = dedup + forced finalization (ADR-014).**
> Fixes the worst live first-impression: a novel typed question that hit the step cap dead-ended on
> a generic apology that "reads as 'I just don't work'." Now (a) an **identical repeated tool call**
> is served from a per-turn memo + a firm nudge instead of re-executing (8.2 — "don't squander the
> budget we have"), and (b) on a ceiling hit the agent **finalizes from what it gathered** — the
> latest *successful* ledger query and/or a policy finding + the specific gap + a narrowed next step
> (8.1/8.3) — instead of the canned message. New `src/agent/turn_control.py` (`ToolCallTracker`
> dedup + observation log in a **`ContextVar`** so the process-wide shared agent is concurrency-safe;
> `finalize_answer`, deterministic — surfaces only what a tool actually returned, never a fabricated
> number; errored/guard-rejected queries don't count as facts). Tools gained an optional `wrap`;
> wired into `invoke`/`ainvoke` (which previously **raised** on the ceiling) **and** `astream`.
> `tests/test_turn_control.py` (12, offline, incl. a looping-stub end-to-end through both invoke and
> astream, and dedup through a real `StructuredTool`). **Suite 130 green.** Step cap deliberately
> **unchanged at 8** — raising it is only safe with 8.5 (narration) + this dedup + finalization, per
> the phase reframe. **Next:** 8.5 (progress narration → a raised, narrated cap + a soft wall-clock
> budget), then 8.4 (a "continue" affordance via a LangGraph checkpointer). Live-on-Space
> confirmation of the tiny-model path is deferred (the tiny-CPU loop can't run cheaply here), as
> with ADR-013's streaming.
>
> **2026-07-03 — Cached questions now hit MID-CONVERSATION + scroll no longer yanks
> (VERIFIED LIVE).** Live transcript showed a curated question worked only as the FIRST
> message: `_try_cache` bailed on `len(messages) != 1`, so every suggested question clicked
> AFTER an answer carried history → bypassed the cache → slow model → recursion limit
> ("Sorry, need more steps"). Fix: look up the **latest** user question regardless of history
> (a cached plan is self-contained, ADR-009; a real follow-up won't clear 0.90, so it still
> falls to the LLM). Warming stays single-turn. Also: chat auto-scrolled on every tick →
> yanked a reader down; now only scrolls when already near the bottom. **Verified LIVE:** three
> curated Qs in a row, each WITH accumulating history, all `cached=True` in 1.0–1.5 s. Tests:
> known-Q hits after prior turns; novel follow-up still → LLM. **Suite 118.** (`deploy_space.sh`
> now requires explicit commit SHAs — auto-detect is unsafe on the LFS-rewritten space-deploy.)
>
> **2026-07-03 — Tiny-model prompt/routing/recursion (ADR-013) + UI quick-bar — VERIFIED
> LIVE.** From browser testing: a novel "which 5 customers have the most overdue money?"
> over-called `search_policy`, looped, thrashed ~250 s, then "did not return an answer".
> Fixes: a **57 %-shorter** tiny-tier prompt (`SYSTEM_PROMPT_BRIEF`/`SCHEMA_HINTS_BRIEF`,
> 729→310 tok) with ONE firm rule (default `query_ledger`; policy only for explicit rule
> Qs; then stop); `agent_recursion_limit=8` on every call so a loop fails in seconds and
> `astream` catches `GraphRecursionError` → graceful "couldn't finish, try an example"
> answer. UI: a persistent **"Instant:" quick-bar** keeps cached Qs one click away after the
> first answer (was empty-chat only). **Verified on the live Space:** the same question now
> uses **only `query_ledger`**, no policy, and **answers in ~82 s with real customers/numbers**
> (Lang/Velasquez/Johnson $13.37 M…) — correct, single-tool, completes, live progress the whole
> time. Also fixed `deploy_space.sh` (patch-id forwarding via `git cherry` + auto-sync the
> literal Dockerfile). **Suite 117.** Still: tiny-CPU is slow by nature (deferred: widen
> curated seeds; Layer 4 tiny-vs-strong on GPU). ADR-013.
>
> **2026-07-03 — Baked curated plan-cache + ONNX model (instant & correct from first
> request).** Every redeploy wiped the plan-cache (warmed at startup) → a multi-minute slow
> window even on the suggested chips. Now baked at BUILD time: `data/curated_plans.py`
> (hand-authored, `sql_guard`-validated plans for the 8 seed Qs — correct SQL/policy queries,
> re-run LIVE on a hit so ADR-009 honesty holds), `seed_plan_cache.py --curated` (model-free),
> run in `Dockerfile.hf` after the ledger bake → `data/chroma` ships warm (count=8, verified
> in-image). Entrypoint dropped the slow tiny-model warm; a cheap curated re-seed self-heals.
> **Gotcha (caught on the live Space, fixed):** the build downloaded the ONNX MiniLM embedder as
> ROOT (`/root/.cache`), but the container runs as appuser → runtime re-download, which on HF
> produced a TRUNCATED `model.onnx` → `INVALID_PROTOBUF`, Exit 3, startup crash-loop. Fix: COPY
> the known-good 90 MB model into `/home/appuser/.cache` at build; verified with
> `docker run --network none` (embedder loads offline, lookup hits). **Verified LIVE:** Space
> recovers, a seeded Q answers in **1.4 s the moment it starts** (baked cache, no warm window),
> and a NOVEL Q streams a `query_ledger` tool event at ~28 s (correct routing + live progress,
> not a frozen screen). Tests: curated plans guard-valid + UI suggestions ⊆ curated. **Suite 115.**
> Follow-up: `deploy_space.sh` should auto-sync the literal `Dockerfile` from `Dockerfile.hf`
> (manual each deploy today). Still deferred: trim the ~2500-tok tiny-model prompt; firm up
> routing for paraphrases like "top 5 / most overdue money"; Layer 4 tiny-vs-strong number.
>
> **2026-07-03 — SSE streaming (live "watch it think") + demo-UX hardening.** Real
> browser testing showed novel (typed) questions are slow (tiny 1.5B on free CPU) and
> a static "Thinking…" reads as hung. Added a **streaming endpoint** `POST /api/chat/stream`
> (SSE): `CachedAgent.astream` emits `cached`/`tool`/`answer`/`error` events — real
> `on_tool_start` events on a cache MISS (via LangGraph `astream_events`), synthetic tool
> events on a HIT. Shared `src/agent/message_utils.py` (final-text + tools-used, de-duped
> from `app.py`). UI (`web/src/App.jsx`) consumes the stream: live step list ("Querying the
> ledger" → "Reading the collections policy"), an **elapsed-seconds** counter, and an honest
> "tiny model on a free CPU, it's working not stuck" hint after 6 s. 3 new streaming API
> tests. **Suite 113 green.** NOTE: local container verification is impractical here (the
> throttled proxy makes the in-container `ollama pull` take ~1 h); streaming is
> unit-tested + will be verified LIVE on HF (clean fast network). **Still deferred (plan):
> bake the plan-cache at build (every redeploy currently re-warms → slow window), trim the
> tiny-model prompt (~2500 tok/turn → the real speed lever), firm up tool-routing so
> "top N customers" hits `query_ledger` not the policy, and the Layer 4 tiny-vs-strong number.**
>
> **2026-07-03 — Phase 6 LAYER 3 LIVE.** Space deployed via a `space-deploy` branch
> (HF front-matter README + tiny-Ollama image as the literal `Dockerfile` + `.gitattributes`
> LFS-tracking `*.png`/`*.gif` — HF rejects plain-git binaries). HF builds `Dockerfile.hf`
> on its clean runners (the optional-CA build step no-ops there) and it serves. **Demo-UX bugs
> caught by real browser testing (both fixed):** (a) the UI's *first suggested* question was
> NOT in the plan-cache seed set → clicking it fell through to the slow tiny model and hung
> on "Thinking…" — fixed by making `web/src/App.jsx` SUGGESTIONS mirror `data/seed_plan_cache.py`
> exactly (every chip is now a ~3 s cache hit); (b) added an honest latency hint (chips =
> instant/cached, typed = live tiny model, may take up to a minute) so "Thinking…" never
> reads as hung. **Deploy gotchas (portfolio-worthy findings, portable to the other Spaces):** git &
> the Docker daemon can trust a supplied CA (push/pull work) even though `curl`/schannel fails
> revocation; HF binaries need LFS; `git lfs migrate` rewrites shared history → keep it OFF
> `main` (had to hard-reset local `main` to the clean `origin/main` after a leak; origin was
> never polluted).

> **2026-07-03 — Phase 6 LAYER 3 DONE on the DESKTOP (self-contained tiny-Ollama HF
> image, ADR-012).** Decision: the public Space runs a **tiny Ollama model baked into
> one container** (no key, no cloud quota, always up) — not a Gemini-key Space. New:
> `Dockerfile.hf` (multi-stage; installs the Ollama binary + `zstd`; bakes the ledger;
> non-root UID 1000; runtime deps `requirements-hf.txt` = runtime set + `langchain-ollama`,
> no Gemini SDK; pins `OLLAMA_MODEL=qwen2.5:1.5b` for a deterministic demo footprint),
> `scripts/hf_entrypoint.sh` (`ollama serve` → pull the model → serve uvicorn NOW →
> **background** plan-cache warm), `scripts/deploy_space.sh` (mirrors forge-pdm F6),
> `docs/DEPLOY.md`, ADR-012. **Built + ran end-to-end here on Docker Desktop.**
>
> **Findings from actually building it (each fixed):** (1) `docker build` pip/npm hit a
> a **network that terminates TLS** — the daemon pulls base images
> fine, but installs *inside* the build don't trust the terminating CA. Fix: OPTIONAL
> `ca.cr[t]` (glob) COPY’d in + `update-ca-certificates`; **git-ignored, never
> shipped**; a NO-OP on HF's clean runners.
> (2) Ollama installer needs **`zstd`** (slim image lacks it). (3) `--select` prints TWO
> stdout lines (`OLLAMA_MODEL=` **and** `HAS_GPU=`) → the entrypoint must grep just the
> model line. (4) The runtime **ONNX embedding download** (ChromaDB MiniLM) also hit the
> terminating CA → set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` to the system bundle
> (AFTER pip, so it doesn't bust the cached dep layer); verified live: `EMBEDDING_OK
> dim=384`. (5) **UX fix:** warming 8 seed Qs on the tiny CPU model is slow (~2 tok/s,
> ~25 s/answer) — doing it *before* serving left `/api/health`+UI dead for minutes → moved
> the warm to the **background** so uvicorn is live immediately. Tiny-CPU inference is slow
> by nature; the plan-cache hides it on headline Qs. **These CA/zstd findings are portable
> to the other public Spaces (forge-pdm already deployed) — worth writing up.**

> **2026-07-02 — Phase 6 LAYER 2 DONE (grammar-constrained tool-calls, ADR-011).**
> **Tested first (per the "measure, don't assume" call) and it refuted the plan's
> premise:** native tool-calling on tiny models fails, but NOT from malformed JSON.
> Measured on the real tools/prompt (5 golden Qs, temp 0): `qwen2.5:0.5b` emits
> valid JSON but **invents fake arg fields + omits `sql`** (~1/5); `qwen2.5:1.5b`
> writes **correct SQL in prose, no tool call at all** (0/5). So a valid-JSON grammar
> would fix nothing. Fix: constrain the WHOLE reply to a `{tool, sql|query, answer}`
> schema via Ollama `format` (GBNF underneath) + translate to `tool_calls`. New
> `src/agent/constrained.py::ConstrainedToolModel` (BaseChatModel wrapper; `final_answer`
> option lets the ReAct loop end); tier-gated in `providers.should_constrain`
> (`constrained_tool_calls=auto` wraps only tiny catalog models, reusing ADR-010's
> `quality`); strong models keep native tool-calling (constraining them would hurt).
> Added `ollama_keep_alive`/`num_ctx` (KV cache). **Measured: ≤1/5 → 5/5 valid+routed+
> filled.** End-to-end on the real agent+ledger, `qwen2.5:1.5b` completes the full loop
> (tool call → guarded SQL → final answer with a real number; top-3 customers w/ balances,
> ~2–4 s on GPU). Caveat (honest, in ADR): `format` fixes STRUCTURE not SQL correctness —
> a 0.5B still writes weak SQL (may not filter "90 days" precisely); plan-cache serves
> curated-correct SQL for headline Qs, guard makes bad SQL safe on misses. This IS the
> "shines with a better model" story, now with numbers. `tests/test_constrained.py` (11).
> **Demo decision: ship BOTH tiny models, default `qwen2.5:1.5b`** (better SQL; 0.5b =
> extreme floor a tester can flip to via `OLLAMA_MODEL`). ADR-011 + PLAN L2 done.
>
> **NEXT — Phase 6 Layer 3 + 4 (this notebook).** L3: self-contained `Dockerfile.hf`
> (tiny model baked/pulled at startup, distinct from the Gemini `Dockerfile`) +
> `space-deploy` branch with HF front-matter, reusing forge-pdm F6 mechanics + its 3
> container gotchas. L4: README live Space link + capture the two pending numbers
> (`python -m evals.run` pass-rate + one latency), ideally tiny-vs-strong to show the delta.

> **2026-07-02 — Phase 7.1 DONE (hardware-aware selection, ADR-010).** New
> `src/core/hardware.py`: detects RAM/VRAM(nvidia-smi)/CPU → *effective memory*
> heuristic (VRAM if real GPU else ~80% RAM) → picks the best-fitting **downloaded**
> model from a public LLM catalog (falls back to best-that-would-fit). Wired into
> `resolve_ollama_model` (`src/agent/providers.py`): `OLLAMA_MODEL=auto` (now the
> **default**) resolves the local model at construction; a concrete tag overrides.
> LLM-only catalog (embeddings stay ChromaDB ONNX MiniLM, ADR-005) → no Ollama
> embedder to pick. **No new dependency** (psutil optional; stdlib `/proc`/`wmic`/
> `nvidia-smi` fallbacks so the diagnostic runs with the system Python). CLI:
> `python -m src.core.hardware` (table) · `--json` · `--select` (emits
> `OLLAMA_MODEL=…` for a container entrypoint). `tests/test_hardware.py` — 11
> offline tests (effective-memory GPU-vs-CPU, tiny-iGPU-ignored, best-downloaded-
> that-fits, skip-too-big-downloaded, fallback-to-best-fits, tag-tolerant match,
> `auto` resolves + never-empty). **Full suite: 99 green** (was 88 pre-7.1).
> **Verified live on the notebook** (RTX 4050 / 6 GB VRAM / `qwen2.5:7b` pulled) →
> `--select` emits `OLLAMA_MODEL=qwen2.5:7b`; a 16 GB CPU tier would pick a tiny
> model instead. This makes Phase 6's "shines with a better model" real from run one.
>
> **Remaining Phase 7 (later sessions):**
> - **7.2 (dedicated full-scan session):** audit the *whole* FIA feature set. Start
>   by mining my private engineering-findings log, then
>   sweep the FIA repo. Record kept/improved/dropped per item.
> - **7.3:** upgrade `docker-compose.yml` single-service/Gemini-only →
>   **multi-service** (app + `ollama` + models volume); a compose entrypoint can
>   call `python -m src.core.hardware --select` to choose the model to pull.

> **NEXT — Phase 6 Layer 2 (needs this notebook: model + normal network).**
> Hardened tiny local LLM (`qwen2.5:0.5b`/`1.5b` Q4_K_M) with **GBNF
> grammar-constrained tool-calls** (kills malformed-JSON, the #1 small-model
> failure) + KV/prompt caching on the long system-prompt prefix. Then Layer 3
> (self-contained `Dockerfile.hf` + `space-deploy` branch reusing forge-pdm F6
> mechanics) and Layer 4 (README live link + the two pending numbers: `evals.run`
> pass-rate + one latency, ideally tiny-vs-strong to show the delta). Also here:
> `python -m data.seed_plan_cache` to pre-warm the demo cache, and `python -m evals.run`.

> **2026-07-02 — Phase 6 LAYER 1 DONE (semantic plan-cache, ADR-009).** Built entirely
> here on the CPU-only desktop, no model needed. Cache the question→**PLAN** (the agent's
> guard-validated tool calls), **never** the answer; on a hit the plan is **re-executed
> live** (SQL re-validated through `sql_guard` + read-only, policy re-retrieved) and the
> answer composed deterministically from **fresh** results → the LLM is skipped but the
> number is always current. Proven offline by mutating an in-memory ledger and re-running
> the same cached plan (freshness test). New: `src/agent/plan_cache.py` · `plan_replay.py`
> · `cached_agent.py` (drop-in `CachedAgent` wrapping `build_agent`, config-gated by
> `plan_cache_enabled`) · `data/seed_plan_cache.py` (optional warm-up) · `tests/test_plan_cache.py`
> (12 tests). Reuses the existing ChromaDB + MiniLM embeddings — no new dependency.
> **Gotcha:** ChromaDB's in-process store can share state across ephemeral clients, so the
> test fixture uses a unique collection name per test (a fixed name bled between tests).

> **2026-08-01 — line-by-line audit of the plan-cache engine. Items 1, 2 and 3 are now CLOSED
> (addenda below).** `plan_cache` / `plan_replay` / `cached_agent` were walked line by line and
> put under **43 valid mutations against the suite; 27 stayed green** (plus 6 test-side
> mutations, all green, and 6 crossed pairs of which 4 leave the suite fully green). Every
> number below was re-measured by a separate blind pass before being written here.
> 1. **"Even a wrong match cannot do harm" is false as written** (module docstring of
>    `plan_cache.py`, and ADR-009 Decision 3). The guard is a *security* boundary — read-only,
>    allow-listed relation — and never looks at intent, so a wrong match cannot corrupt data or
>    return a stale number **and can answer a different question**. Measured with the shipped
>    MiniLM over the curated plans baked into the demo image: the typed question *"Which
>    customers have the largest overdue balances?"* has as its nearest neighbour *"Which single
>    customer has the largest overdue balance?"* at **0.9767**, so a request for a list replays
>    `… LIMIT 1`; *"Who is the top customer by overdue balance?"* matches *"Who are the top 10
>    customers by overdue balance?"* at **0.9424** and replays `… LIMIT 10`. Two curated
>    questions sit **0.9156** apart, above the shipped 0.90 threshold. Raising the threshold does
>    not fix it — the bad match is higher than paraphrases worth keeping. (The docstring's own
>    example holds: *check* vs *send this account* is 0.743.)
> 2. **The fall-through to the LLM has no owner.** ADR-009 Decision 2 promises that a
>    `ReplayError` makes the caller fall back to the model. Replacing
>    `except ReplayError: return None` in `CachedAgent._try_cache` with `raise` leaves the suite
>    at **382 passed**. Sibling hole, measured through the real class: a corrupted cache entry
>    makes `Plan.from_json` raise `JSONDecodeError` / `KeyError`, which that `except` does not
>    catch either — both propagate out of `_try_cache`.
> 3. **The freshness banner promises more than the replay delivers** (found by a blind
>    adversarial pass, three executable repros, all reproduced here). `replay_plan` prefixes
>    *every* reply with "the query was re-run live against the ledger, so the numbers are
>    current" — including plans with **no `query_ledger` step at all**: **3 of the 12 curated
>    plans are `search_policy`-only**, so three one-click demo questions assert ledger freshness
>    over a number read from a static document, with zero SQL executed. Two more, same family:
>    `guard_query("SELECT 425000.00 AS total_overdue")` **passes** (the relation allow-list is
>    satisfied *vacuously* when the query reads no relation), so a plan can carry a frozen
>    literal that replay prints as fresh; and a date literal inlined in cached SQL freezes the
>    *semantics* — measured, a ledger with 4 overdue invoices answering `1`.
>
> Also measured, not guarantee holes → queued under §Next: the metric space, the three copies of
> the 0.90 default, the synthetic `tool_calls`, the SSE `error` event, and the rendering
> branches.

> **Addendum 2026-08-01 (`R1-C13`) — item 1 closed. The cache now refuses to guess between two
> plans (382 → 396).**
> Both measurements were re-confirmed first. The fix is a second rule, not a bigger threshold:
> 0.9767 (the wrong match) is *higher* than paraphrases worth keeping, so no threshold separates
> them — what does is **how much closer the winner is than the nearest different plan**. `lookup`
> now examines every neighbour within `AMBIGUITY_MARGIN` (0.10, sitting between the widest
> ambiguous gap of 0.0482 and the tightest legitimate paraphrase gap of 0.1589, both re-measured
> on every run) and misses as soon as one of them holds a **different** plan. Comparing **plans**
> rather than wording is what keeps the four curated phrasings of the top-5 question hitting.
> **The margin alone would have broken the demo:** two one-click chips are 0.9156 apart, so
> typing either exactly leaves a 0.0844 gap — inside the margin. Exact questions therefore
> short-circuit the check (the stored ID *is* the question, so this is a key lookup, not a guess,
> and there is no float tolerance involved) — on the same *question*, not the same bytes:
> whitespace and case are normalised by one owner used on both sides.
> Split in two files on purpose: the **mechanism** stays offline on the deterministic embedding,
> the **geometry** cannot — a hashing stand-in says nothing about where MiniLM puts two real
> questions, which is precisely why this defect lived under a green suite.
> `tests/test_plan_routing.py` embeds the real corpus with the real model (all 66 curated pairs,
> all 12 questions routed, both walls) and **does not skip** when the model is missing; CI caches
> the ONNX download instead. Cost, stated honestly: the suite goes from ~5 s to ~12 s.
> **Then a blind pass broke it, twice — and that is the story of this entry.** An instance with
> no knowledge of the design got the four guarantees and the repo and was told to falsify them by
> running code. Both holes are now closed, with tests:
> 1. **The check read the runner-up, not the neighbourhood.** Four curated phrasings share the
>    top-5 plan, so for a question in that cluster ranks 0 and 1 are the *same* plan — the guard
>    compared a plan with itself and served the hit while the real rival sat at rank 2, **0.0738**
>    away, inside the margin, unexamined. Three reproducing questions, 3 of 108 probes. Textbook
>    enumerated guard: it looked at a fixed *position* instead of the *surface* the margin covers.
> 2. **"Exact" was byte equality, and over-blocked.** A chip typed with a trailing space or a
>    lower-cased first letter embeds at distance **0.0000** and was refused — it missed the
>    shortcut, then the 0.0844 neighbour tripped the margin. The demo would have answered its own
>    suggestion with the slow model.
>
> It also confirmed the half that matters: **12 poisoned plans** injected straight into the cache
> (`DELETE`, stacked statements, CTAS, `COPY TO`, `read_csv_auto('/etc/passwd')`, `ATTACH`, …) were
> all refused by `guard_query` on replay against a read-only connection, row counts unchanged.
> **Mutations, two batteries: 17 code mutations 16 red against the first design; 11 valid, 9 red
> against the code as it now stands.** Five limits: the *substring* relaxation of the exact match
> still has a **single owner** (measured twice — it passed the whole repo until an assertion
> existed, and deleting that assertion re-opens it even now, because the byte-variant surface test
> accepts a substring); two mutations are indistinguishable by construction (`>=` vs `>`, and
> `break` vs `continue` on ordered neighbours); the census's first count anchor was **tautological**
> and the blind pass found the same defect in two more places — probe tables looped with no anchor,
> shortenable one row at a time; ownership is uneven on purpose (9 of 11 reds have two owners);
> and the red rate is inflated by construction, since these mutations were written alongside the
> tests that catch them.
> **Still not guaranteed:** a paraphrase *confidently* closest to a plan that answers a slightly
> different question is still served — the margin rejects ambiguity, not wrongness. The blind pass
> left a clean example that survives the fix: *"top 10 customers by overdue balance in each aging
> bucket"* sits at **0.9841** from the top-5-per-bucket plan and is served with the right shape and
> the wrong N — its neighbourhood is crowded (runner-up 0.0853 behind) but every neighbour in it is
> another phrasing of the *same* plan, so there is no rival for the margin to find. Details in
> **ADR-009 Amendment 2026-08-01**.

> **Addendum 2026-08-01 (`R1-C14`) — item 2 closed. A broken cache now costs a slow turn, never
> an error (396 → 429).**
> The fall-through the ADR promises in writing had **no owner**: replacing the whole
> `except ReplayError` clause with `raise` left the suite at 396 passed. The sibling hole needed
> no mutation at all — the cache is persistent and outlives the code that wrote it, so a stored
> document in an older shape is ordinary, and `Plan.from_json` answered it with `JSONDecodeError`
> / `KeyError` / `TypeError`, plus `AttributeError` from a non-dict `args` one layer down at
> replay. Measured out of **all three** entry points: `astream`'s own `except Exception` never
> covered it, because the lookup runs *before* that `try`.
> The promise was restated one level up — **no failure of lookup, replay or warm can turn a turn
> into an error the visitor sees** — and implemented at three layers with three owners:
> `Plan.from_json` is **total** (a `Plan` or a `PlanFormatError`); an unreadable entry is "no
> plan" and logs at WARNING; `_try_cache` / `_warm` keep a backstop in **two** clauses, kept
> distinguishable by log level so neither can be deleted while the other covers for it. `_warm`'s
> is the load-bearing one: it runs *after* the model answered, so a raise there discards work the
> visitor already paid for.
> **21 code mutations, 19 red — all 8 that RELAX included**; 9 test-side, 7 green; 6 crossed
> pairs, 2 become holes (both single-owner assertions).
> ⚔️ **The blind pass then broke this fix in two of three claims.** (1) `from_json` was **not**
> total: `except json.JSONDecodeError` misses the bare `ValueError` CPython raises past its
> 4300-digit limit and the `RecursionError` from deep nesting — and since `PlanFormatError`
> *subclasses* `ValueError`, `_plan_at` let a real `ValueError` out of `lookup`. The
> enumerated-guard disease one layer below where it had just been fixed. (2) `_NEIGHBOURS_SCANNED
> = 32` was a **cost** ceiling silently deciding a **correctness** question: when every fetched
> neighbour sat inside the margin the loop just ran out of rows and served the winner, so a rival
> at rank 32 was never examined — a stopped scan is not a finished one. Both closed. (3) The third
> break was **my claim, not the code**: the exact-question shortcut is deliberate and documented,
> so the guarantee gained its missing qualifier (*on the semantic path*) and nothing changed.
> ⚔️ **A second blind pass, aimed at the repaired code, broke it twice more** (424 → 429), both
> inside this session's own work. (1) The claim's **first statement was outside its own `try`** —
> `_try_cache`, `_warm` and `astream` each read the question before the block that was supposed to
> make the cache incapable of failing a turn; a LangGraph-native message has no `.get`, so all
> three entry points raised, and `astream` yielded **nothing at all**, not even an `error` event.
> `_last_user_message` is now total: one owner for four call sites, instead of three `try` blocks
> each remembering to be wide enough. (2) **The normalisation had two copies again** — the ADR-014
> defect one layer up: `_same_question` folded whitespace and case while `warm` keyed rows by the
> **raw** text, so two spellings of one question were two rows for storage and one question for the
> shortcut, sitting at distance **0.0000** and resolved by rank. Not a slow answer — a *different
> plan* under the "numbers are current" seal. `_question_key` is the single owner now, and the
> reader stops assuming the invariant too (a collection seeded by an older version can still hold
> the duplicate, so a same-key conflict is treated as ambiguity).
> 🔎 **And the claim audit caught one overstatement of mine** (423 → 424): *"an unreadable entry
> is a miss, never a raise"* held for nine metadata shapes and failed on a **truthy non-mapping**
> row entry, which walked past `row[index] or {}` and died on `.get`. Outside ChromaDB's declared
> contract — and that is the point, since `_plan_at` exists precisely so a turn need not depend on
> that contract, and the backstop would have hidden it. Fixed with `isinstance`, owned by a
> ten-shape test with a readable control.
> Still green and honest about it: `>=` vs `>` on the window is indistinguishable by construction;
> the `neighbour_distance is None` branch is unreachable through ChromaDB; and `BaseException`
> escapes both backstops **on purpose** — a cancelled request is not a cache failure to degrade
> around, and swallowing it would be the bug.

> **Addendum 2026-08-05 (`R1-C15`) — item 3 closed. The freshness seal is now assembled from the
> work that actually happened (429 → 449).**
> The sentence a cache hit prints — *"the query was re-run live against the ledger, so the numbers
> are current"* — was a constant, not a description. Three of the twelve one-click questions the
> image bakes are **policy-only**: zero SQL executed, full seal, live on the Space. It is now
> composed per tool from `tools_used`, and the key set is compared against `REPLAYABLE_TOOLS` so a
> third replayable tool cannot inherit a sentence written about the other two.
> The two sibling holes became one owner, `plan_cache.freshness_violation`, enforced on **three**
> sides (write, serve, image build — the collection is persistent and ships pre-seeded, so a
> write-side check alone fixes nothing already stored):
> a statement must **name a relation**, every output of every answering select list must be
> **decided by the ledger**, and the statement must not **fix a point in time**.
> ⚔️ **Both blind passes broke my first attempt at the last two, and both times for the same
> reason: I had written a check on the *shape* of the input and called it a check on meaning.**
> Round one: a scan for date-shaped strings misses `make_date(2026, 8, 1)`,
> `strptime('01/08/2026', '%d/%m/%Y')` and two more — none of which contains anything DuckDB reads
> as a date; and "names a relation" misses `SELECT 425000.00 AS total_overdue FROM invoices WHERE
> 1 = 0 UNION ALL SELECT 425000.00`. Round two broke the **repairs**: vetoing a fixed date on any
> mention of the clock made `make_date(2026, 8, 1 + (current_date - current_date))` permanent
> (mentioning is not depending — and that veto existed only to fix a false refusal I had just
> caused, which is a guard against noise becoming a channel for silence); and "an output that
> fails to bind alone proves the list reads data" was satisfied by `row_number() OVER ()`, which
> reads nothing at all. Both rules are positive now and both ask the **binder**, not a pattern.
> The third finding was an over-block of my own making: `EXISTS (SELECT 1 …)`, the semi-join every
> dialect writes, was refused until predicate subqueries were excluded from the output rule.
> **27 code mutations, 25 red — 14 of the 16 that RELAX included**; both greens named (node-type
> vs field keying, indistinguishable over a 147-statement sweep; and an unreachable `fetchone()`
> branch written for its direction). One earlier mutation of mine was **invalid** and is not
> counted — reordering two rules never changes the verdict, only the reason printed. 7 test-side
> mutations, 7 green; 12 crossed pairs, 2 holes, both the single ownership this session designed.
> Two repairs fell out of the tests, in code this session did not set out to touch:
> `_statement_and_tree` is the single owner of *text → one statement → tree* (the empty string was
> reporting itself as a clean statement that reads nothing, because `json_serialize_sql('')`
> returns `{"statements": []}` with **no error**), and `guard_query` had kept its own inline
> `.strip()` while `_statement_text` claimed in prose to be "the one owner" and named it as a
> caller — identical behaviour, false sentence, the ADR-014 defect in documentation-only form.
> Details in **ADR-009 Amendment 2026-08-05**.

> **NEXT (needs the notebook — model + normal network):** Layers 2–3 — hardened tiny local
> LLM (`qwen2.5:0.5b/1.5b` Q4_K_M) with **GBNF grammar-constrained tool-calls** + KV cache;
> self-contained `Dockerfile.hf` + `space-deploy` branch reusing forge-pdm F6 mechanics.
> Layer 4 — README live link + the two still-pending numbers (`evals.run` pass-rate + one
> latency), ideally tiny-vs-strong model to show the delta. Also on the notebook: run
> `python -m data.seed_plan_cache` (needs a provider) to pre-warm the demo cache, and
> `python -m evals.run`. Demo provider so far was local Ollama `qwen2.5:7b`.

> **2026-06-10 — deferred again, on purpose (handoff).** Confirmed these two numbers
> (eval pass-rate + one latency) are recorded **nowhere** yet — they need a live run.
> NOT done on the production desktop (i3 / 8 GB / no GPU; Ollama server was down) —
> per the compute split, inference belongs on the **personal Linux notebook** (GPU,
> normal network, no TLS termination). Dedicated session to do, with full context already
> here + in `docs/DEMO.md` §6:
> 1. On the notebook: `ollama serve` + pull `qwen2.5:7b` (or `llama3.1`); `pip install -e ".[ollama,gemini,data,dev]"`; `python data/generate.py` (build the ledger).
> 2. `python -m evals.run` → record the **pass-rate** (e.g. "7/7 golden questions pass").
> 3. Hit `POST /api/chat` once (or watch the demo run) → note **one response latency**.
> 4. Add a small "Evals" + "Latency" line to the README — all real, none guessed.

> **2026-07-31 — items 1–4 and the follow-on `R1-C11` are CLOSED (see the addenda below). Item 5
> is not a guarantee gap; it was triaged to the cleanup entry under §Next. The list below is the
> audit as it was written, kept as the record.**
> A line-by-line audit of `graph/tools/providers/schema_hints/message_utils/constrained` ran **28
> mutations against the suite; 19 stayed green**. Nothing was fixed in code this session — three
> false comments were corrected and ADR-004 got a dated amendment. What is open:
> 1. **No test calls `build_agent`.** Deleting the fallback wiring (`model = primary`), dropping
>    `has_credentials`, or dropping the `fb != primary` check each leaves the suite at **346
>    passed**. ADR-004's "covered by a build smoke check" was false.
> 2. **Re-measured on langgraph 1.2.4:** `create_react_agent` no longer *rejects* a
>    `RunnableWithFallbacks` — it constructs and **silently drops the fallback**. The dynamic-model
>    callable is still what keeps it alive. The API is **deprecated since v1.0** (removal in v2.0),
>    the pin `langgraph>=1.0` has **no ceiling**, and `langchain.agents.create_agent` **rejects the
>    callable** — migration is not an import swap.
> 3. **`SCHEMA_HINTS` has no owner.** Renaming `v_dso` to a non-existent view = **346 green**,
>    while `ALLOWED_RELATIONS` *is* pinned against the real catalog (`R1-C3`). No drift today
>    (checked by hand against `ledger.duckdb`); nothing enforces it tomorrow.
> 4. **Constrained-path test gaps:** emptying the JSON-protocol hint (the tiny model's stopping
>    criterion) = 346 green; `test_bind_tools_then_invoke…` asserts against `build_schema` itself
>    (circular oracle — two schema mutations never turn it red); the tier threshold `<=` vs `<` is
>    **indistinguishable by construction** (default max 3, catalog ranks jump 2 → 4).
> 5. **`evals/run.py` carries a byte-identical third copy** of `final_text`/`tools_used` instead of
>    importing `message_utils` — the evals can silently measure older behaviour.
>
> Tracked as `R1-C7`–`R1-C10`. Full walkthrough + reproducible snippets in the private study doc
> (`repo-base-career/aprofundamentos/receivables-agent/R1-T3…md`), not in this repo.
>
> **Addendum 2026-07-31 (`R1-C7`) — items 1 and 2 closed, and item 2 was wrong.**
> `tests/test_provider_fallback.py` now covers the wiring offline (**346 → 359**): the backup
> answers when the primary raises, the order is config-driven, and nothing is wired for a keyless
> provider, a fallback equal to the primary, or none at all. **7 of 7 mutations of
> `build_dynamic_model` now fail**, including the two *relaxing* ones that the audit above measured
> at 346 green. `langgraph` is capped `<2` in `pyproject.toml` and `requirements.txt`.
> **Item 2 above is false and stays as written for the record:** the direct pass does *not*
> silently drop the fallback. `RunnableWithFallbacks.__getattr__` broadcasts `bind_tools` to the
> fallbacks whenever the wrapped model annotates it as returning a `Runnable` — which `ChatOllama`,
> `ChatGoogleGenerativeAI` and our `ConstrainedToolModel` all do. The audit's fake declared no
> return type, so it measured the unannotated branch and reported it as the library's behaviour;
> writing the test is what caught it. The callable stays because it is independent of that
> annotation, and migrating to `langchain.agents.create_agent` means **deleting** it (that API
> takes the wrapped runnable directly, fallback intact) rather than redesigning the fallback.
> Full correction in **ADR-004 Amendment 2**.
>
> **Addendum 2026-07-31 (`R1-C8`) — item 3 closed. The prompt's schema map now has an owner.**
> `tests/test_schema_hints.py` (**359 → 368**) checks both prompts against a **real ledger built
> by `data/generate.py` through its own entry point** — relations, columns, and the value sets the
> text promises (`status ∈ {paid, open, overdue}`, the five dunning stages). Surface against
> surface, iterating the *catalog*: the earlier version of this pattern parametrized over the list
> under test, and a list cannot pin itself (`R1-C3`). **15 of 15 mutations now fail** — the seven
> that edit the prompt (including *relaxing* ones: a plausible extra column, a widened value set,
> a brand-new false `∈` clause), the four that grow or rename things in the generator, and the one
> that drops a relation from `ALLOWED_RELATIONS` while the prompt still advertises it. Renaming
> `v_dso` in the hint, measured at 346 green in the audit, is now **4 red**.
> The audit's "no drift today" held: the hand-check covered relation names, and the columns and
> value sets it never checked turned out to be correct as well.
> **Addendum 2026-07-31 (`R1-C9`) — item 4 closed. The suite now reads the protocol, not just
> the grammar.** `tests/test_constrained.py` grew three tests (**368 → 371**) and the fake base
> model records the *messages* it saw, not only the `format`. The JSON nudge is checked against
> the whole menu — every tool with the field it takes, a count anchor so no option is invented or
> dropped, the stopping criterion, the grounding line — and its position (last, after the caller's
> turns, which survive in order). The circular oracle is gone: both schema assertions now compare
> against a **spelled-out literal**, and the schema test compares whole surfaces instead of four
> `in` checks. The threshold test moves the ceiling to **2**, the only place `<=` and `<` differ
> given the catalog's 2 → 4 jump. The async `_agenerate` twin, previously uncovered, is pinned
> against the sync path (differential, so neither side is the oracle).
> **14 of 14 mutations now fail, 9 of them measured green before** — including the relaxing ones:
> an extra `reason` field in the grammar, a hint that keeps `final_answer` but drops the tool menu
> / the stopping criterion / the grounding line, and `quality <= 10`. No production code changed.
> Two honest limits: dropping the stopping criterion is owned by **one** assertion (the first
> mutation of it looked redundant only because it destroyed two sentences at once), and the literal
> schema in the wrapper test and the whole-surface schema test are now **redundant owners** — each
> catches the extra-field mutation alone. Details in ADR-011 **Amendment 2026-07-31**.
>
> **Still open, and new (`R1-C11`):** the curated plans baked into the demo image
> (`data/curated_plans.py`) carry literal SQL that is only ever passed through `guard_query`, which
> validates *relation names* and never touches columns — measured: renaming `v_customer_ar
> .overdue_amount` in the generator leaves every plan-cache test green (only the two new hint tests
> go red), and `guard_query("SELECT no_such_column FROM v_customer_ar")` is accepted and wrapped.
>
> **Addendum 2026-07-31 (`R1-C11`) — closed. The baked SQL now runs before it ships.**
> Both measurements above were re-confirmed, then closed: `tests/test_plan_cache.py` replays every
> curated plan through `replay_plan` — the function a cache hit runs — against a ledger built by
> `data/generate.py` through its own entry point (**371 → 374**). The `ledger` fixture moved to
> `tests/conftest.py` so the prompt suite and the replay suite share one, rather than each
> building a ledger that could drift from the other. Three properties kept apart on purpose: the
> plan replays and reports the tools it declares, every ledger query still returns **rows**, and
> every curated policy query still targets a section the document has.
> **15 of 15 mutations now fail**, the relaxing ones included: a curated plan added with a column
> that belongs to another relation, a curated policy question added without naming its section,
> and the two value drifts that keep the SQL valid and return nothing.
> Three measured limits: the empty-result test is the **single owner** of value drift *inside the
> curated SQL* (weaken it and that mutation goes green repo-wide) but not of value drift in the
> generator, which the schema-hint value-set tests also catch; the policy half was **already owned** for
> two of its three sections by `tests/test_rag.py` — only `Payment plans` was an orphan, so that
> assertion is redundant defence for the other two; and retrieval **ranking** is not measured
> offline at all, since the deterministic embedding only stands in for MiniLM. Details in
> **ADR-009 Amendment 2026-07-31**.
>
> **2026-08-01 (`R1-C12`) — the dedup key told the model it had already asked a question it
> hadn't, and turn isolation had no owner. Both closed (374 → 382).**
> A line-by-line audit of `turn_control.py` on 2026-07-31 measured **20 mutations, 7 green**.
> Two of the findings were not cleanup:
> 1. **The dedup key merged different queries.** `_arg_key` collapsed whitespace across the
>    whole string, literals included, so `WHERE name = 'John  Doe'` and `WHERE name = 'John
>    Doe'` shared a key: the second never ran and was served the first's rows with *"you already
>    ran this exact call"*. `ADR-014` stated the opposite ("different literals never collide").
>    `query_ledger` now keys on the parse tree (`sql_guard.statement_identity`), chosen at the
>    construction site via `wrap(..., key=)`; prose tools keep the text key; unparseable SQL
>    falls back to text under a separate prefix, because a tree handed back as the `sql`
>    argument would otherwise be served another call's memo.
> 2. **Turn isolation was claimed and untested.** Replacing the `ContextVar` with a module
>    global left the suite at **374 green** while two interleaved turns read each other's state
>    — the only isolation test ran turns in *sequence*, which a global also passes.
>
> **12 of 12 mutations of this code are now red**, the four relaxations included (lower-case
> the SQL before keying · collapse whitespace before keying · empty identity instead of "no
> opinion" · a fallback key that ignores the arguments). Three of those twelve were **green
> until the tests that close them were written this session** — they were found by mutating,
> not by reading. Honest counterpart: **all 7 single-assertion weakenings of the new tests pass
> green on their own**; what changed since `R1-T4` is that no *pair* (weakened assertion + the
> code mutation it should own) opens a hole — each guarantee has at least two owners.
> The concurrency test earns that only because it opens both turns before either calls a tool:
> with the simpler ordering, one assertion was the sole owner.
>
> **Then an instance with no knowledge of the design attacked the fix and found a third hole,
> in the fix itself.** `guard_query` parses `sql.strip()`; `statement_identity` parsed the raw
> text. A leading `\x0b` — whitespace to Python, a parse error to DuckDB — made the identity
> answer "no opinion" for a statement the guard **stripped and executed**, so that query fell
> back to the text key and the literal collision returned through the crack. The strip now has
> one owner (`_statement_text`) used by both, and the invariant *anything the guard runs can be
> identified* is asserted against **every character Python's `str.strip()` removes**, derived
> from the language rather than listed (a hand-picked list of three is itself caught, by the
> count anchor). Same session, same family as the original defect: **two copies of one
> normalization**. It also re-scoped a claim rather than code — turn isolation is per **asyncio
> context**, so two turns driven from one task share state; unreachable over HTTP, now written
> down instead of implied. Suite **380 → 382**; 14 code mutations, 13 red. Details in
> **ADR-014 Amendment 2026-08-01**.

## Done
- 2026-07-02: **Phase 6 Layer 2 — grammar-constrained tool-calls (ADR-011).**
  - Probed native tool-calling on `qwen2.5:0.5b`/`1.5b` (real tools+prompt, 5
    golden Qs) → refuted the malformed-JSON premise: 0.5b invents fake arg fields
    + omits `sql` (~1/5); 1.5b writes SQL in prose, 0 tool calls. Structured
    output (`format`=schema) → both **5/5** valid+routed+filled.
  - `src/agent/constrained.py`: `ConstrainedToolModel` (BaseChatModel wrapper) —
    `bind_tools` builds a `{tool, sql|query, answer}` schema (+ `final_answer` to
    end the loop); `_generate`/`_agenerate` bind `format=schema`, parse, return an
    `AIMessage` with one `tool_calls` entry or final content; malformed/off-menu
    degrades to plain content. Presents as a chat model → create_react_agent,
    plan-cache, guard, evals all unchanged.
  - `src/agent/providers.py`: `should_constrain()` tier-gates via ADR-010 catalog
    `quality` (`auto` wraps tiny only; `on`/`off` force); `build_chat_model` wraps
    the tiny Ollama path + passes `num_ctx`/`keep_alive`. New `hardware.catalog_quality()`.
  - Config: `constrained_tool_calls` / `constrained_quality_max` / `ollama_num_ctx`
    / `ollama_keep_alive` (+ `.env.example`).
  - `tests/test_constrained.py`: 11 offline tests (schema, parse tool-call/final/
    malformed/foreign-field/unknown-tool, format passed through, tier gating).
    Full suite: **110 passing**. Verified end-to-end live on the notebook.
  - ADR-011 written; PLAN Layer 2 done; CLAUDE.md file-map updated.
- 2026-07-02: **Phase 7.1 — hardware-aware local model selection (ADR-010).**
  - `src/core/hardware.py`: `detect_hardware()` (RAM via psutil/`/proc`, CPU,
    NVIDIA VRAM via `nvidia-smi`); `HardwareProfile.effective_memory_gb` (VRAM if
    real GPU, else ~80% RAM); `recommend_model()` picks the best-quality model
    that fits **and** is downloaded from a public LLM catalog (`_CATALOG`,
    tiny→strong), falling back to best-that-would-fit. `list_downloaded_models()`
    hits Ollama `/api/tags` (stdlib urllib, fails soft). CLI `python -m
    src.core.hardware` + `--json` + `--select`.
  - `src/agent/providers.py`: `resolve_ollama_model()` — `OLLAMA_MODEL=auto`
    (new default in `config.py` + `.env.example`) resolves via hardware at model
    construction; concrete tag passes through; always returns a real tag.
  - Clean-room reuse of FIA's `utils/hardware.py` engineering (public data only),
    rewritten in English; dropped the embed catalog (embeddings are ChromaDB ONNX,
    ADR-005). No new dependency (psutil optional; stdlib fallbacks).
  - `tests/test_hardware.py`: 11 offline tests (mock detectors). Full suite: **99
    passing**. Verified live on the notebook.
  - ADR-010 written; PLAN 7.1 marked done; CLAUDE.md file-map updated.
- 2026-07-02: **Phase 6 — Layer 1: semantic plan-cache (ADR-009).**
  - `src/agent/plan_cache.py`: a *plan* = the agent's tool calls (validated
    `query_ledger` SQL + `search_policy` query), stored in a ChromaDB collection
    keyed by the question embedding (same MiniLM embeddings as RAG, no new dep).
    `plan_from_messages` only caches read-only, guard-valid turns (re-checks SQL
    via `sql_guard`); `PlanCache.lookup` = cosine similarity, conservative
    threshold (default 0.90).
  - `src/agent/plan_replay.py`: on a hit, re-validate + read-only re-execute the
    SQL (and re-run policy retrieval), compose the answer **deterministically
    from fresh results** — LLM untouched. `ReplayError` → caller falls back to
    the LLM.
  - `src/agent/cached_agent.py`: `CachedAgent`, a drop-in `invoke`/`ainvoke`
    wrapper; `build_agent` wraps the compiled agent when `plan_cache_enabled`.
    Multi-turn requests bypass the cache.
  - `data/seed_plan_cache.py`: optional one-time warm-up via the live agent.
  - Config: `plan_cache_enabled` / `plan_cache_collection` / `plan_cache_threshold`.
  - `tests/test_plan_cache.py`: 12 offline tests — plan extraction (grounded /
    ungrounded / unsafe-SQL / dedupe), semantic hit-vs-miss, **live-replay
    freshness** (mutate the ledger → the replayed number moves), guard
    re-validation on replay, and `CachedAgent` (miss→LLM+warm, hit→replay
    without the LLM, multi-turn bypass). Full suite: **88 passing**.
  - ADR-009 written; ARCHITECTURE + PLAN (Phase 6) + CLAUDE.md updated.
- 2026-06-08: **Ship gate closed — live demo GIF.**
  - First end-to-end live run, on the personal Linux notebook (normal network):
    agent answered real questions using `query_ledger` (and `search_policy`),
    HTTP 200. Provider: **local Ollama `qwen2.5:7b`** (tool-capable).
  - Ran **outside Docker** (the repo's intended Ollama path): host `uvicorn`
    reads `.env` (`PRIMARY_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://localhost:11434`),
    `web/` built with `VITE_API_KEY` matching `APP_API_KEY`. The one-time ONNX
    MiniLM embedding download succeeded (no TLS termination on this network).
  - **Gotcha found & documented:** `docker-compose.yml` pins `PRIMARY_PROVIDER:
    gemini` in its `environment:` block, which **overrides `.env`** — Compose only
    uses the root `.env` for `${VAR}` *substitution*, it is not injected into the
    container (no `env_file:`). So a `.env` set to Ollama is silently ignored by
    the container, and the placeholder Gemini key then 500s. The Docker image is
    also Gemini-only by design (`requirements.txt` omits `langchain-ollama`), so
    Ollama-in-Docker is not a supported path — the host run is the clean one. Left
    a git-ignored `docker-compose.override.yml` locally for reference only.
  - `docs/demo.gif` (≈628 KB, inline-friendly) recorded and embedded in the README
    (replaced the placeholder comment).
- 2026-06-08: **Phase 5 — AI-native layer.**
  - `mcp_server/server.py`: MCP server exposing `query_ledger` (tool) +
    `schema://ledger` (resource) via FastMCP. Reuses the SAME guardrail as the
    app through `run_guarded_query` (connect_readonly + guard_query). Named
    `mcp_server` (not `mcp`) — a top-level `mcp/` shadowed the MCP SDK package
    and broke its import; caught by the import test. `mcp_server/README.md` has
    Claude Code registration. Smoke-verified: builds, registers tool + resource.
  - `evals/`: `checks.py` (pure property checks — used_tool / mentions_all /
    contains_number, tolerant + format-agnostic), `golden.py` (7 cases with
    ledger-verified expectations spanning ledger-only / policy-only / both),
    `run.py` (`python -m evals.run`, live harness, non-zero exit on failure).
  - `.claude/skills/eval-agent/SKILL.md`: Claude Code skill that runs the evals.
  - `tests/test_mcp.py` (guardrail parity: legal SELECT, row cap, write/unknown
    rejected with nothing executed) + `tests/test_evals.py` (check primitives +
    golden well-formedness). Full suite: **76 passing**.
  - ADR-008 written (shared-guardrail MCP · `mcp_server` naming · property-based
    evals). README "AI-native layer" section; ARCHITECTURE + CLAUDE.md + PLAN +
    pyproject (`mcp` extra) updated.
  - Offline by design: `evals/run.py` (live LLM) not run here — the accuracy
    number is captured on the live-network machine, same trip as the GIF.

### Earlier
- 2026-06-08: **Phase 4 — full-stack + ship.**
  - `src/api/app.py`: FastAPI via `create_app(agent_builder)` factory. Async
    `lifespan` builds the agent **once** (read-only ledger + policy index) onto
    `app.state`; `GET /api/health` (open) + `POST /api/chat` (API-key via
    `X-API-Key`, constant-time compare). Mounts a built `web/dist` at `/` so one
    container serves UI + API same-origin. Builder is injectable → offline tests.
  - `src/api/schemas.py`: Pydantic v2 boundary (`ChatRequest` message+history,
    `ChatResponse` reply+`tools_used`, role-restricted `Message`).
  - `web/`: minimal React (Vite) chat UI — history in state, posts to `/api/chat`,
    renders replies + a `tools_used` badge; Vite proxies `/api` in dev. `npm run
    build` verified (32 modules, ~145 kB). API key baked via `VITE_API_KEY`.
  - `Dockerfile` (multi-stage Node→Python) + `docker-compose.yml` + `.dockerignore`
    + `requirements.txt`: one-command `docker compose up`; ledger generated at
    image build; `$PORT` override for a Space.
  - `tests/test_api.py`: 7 offline tests (health open, auth required/wrong-key,
    happy path reply+tools, history forwarding, empty-message + bad-role 422),
    injected stub agent. Full suite: **65 passing**.
  - ADR-007 written (single same-origin container · build agent once · baked UI
    key · build-time ledger). README Run section + ARCHITECTURE api/web/deploy
    sections + `.env.example` note updated. pyproject deps: fastapi, uvicorn, httpx.
  - Verified here: static mount serves the SPA at `/` and `/api/health` → ok
    against a stub agent (LLM/ONNX/`docker build`/`npm install` registry are
    blocked by the sandbox SSL cert — code paths verified, not exercised live).

### Earlier
- 2026-06-08: **Phase 3 — RAG over the collections policy.**
  - `src/rag/chunking.py`: split the policy on `##` sections (one citable rule
    each) into chunks with deterministic IDs (`"{source}::{heading-slug}"`).
  - `src/rag/embeddings.py`: local default (ChromaDB ONNX `all-MiniLM-L6-v2`,
    no key — ADR-005) + injectable `DeterministicEmbeddingFunction` (offline
    hashing vectorizer) for tests.
  - `src/rag/index.py`: idempotent `build_index` (upsert by ID + prune orphans →
    collection mirrors the doc; ADR-006); `ensure_policy_index` builds once,
    reuses after.
  - `src/agent/tools.py`: `search_policy` tool — top-k chunks as JSON with the
    section heading to cite.
  - `src/agent/graph.py`: registered `search_policy` alongside `query_ledger`;
    rewrote the system prompt to use BOTH tools together (number from the ledger
    + governing rule from the policy, cited).
  - `tests/test_rag.py`: 5 offline tests — idempotency (same input → same
    IDs/count), pruning of removed sections, and retrieval finds the expected
    rule (deterministic embeddings, ephemeral client). Full suite: **58 passing**.
  - ADR-005 (local embeddings) + ADR-006 (idempotent indexer) written.

### Earlier
- 2026-06-08: **Phase 2 — guarded text-to-SQL + agent core.**
  - `src/agent/sql_guard.py`: defense-in-depth guardrail. String-literal-aware
    scanner (strip comments, split top-level `;`), `SELECT`/`WITH` only,
    deny-list (writes/DDL/catalog/filesystem fns), relation allow-list (CTE-aware),
    outer `LIMIT` wrap. Never relaxes — rejects with a reason. ADR-003.
  - `src/agent/ledger.py`: read-only DuckDB connection (authoritative backstop)
    + `run_query`.
  - `src/agent/tools.py`: `query_ledger` tool (guard → read-only exec → JSON;
    rejections/errors returned as text so the loop self-corrects).
  - `src/agent/providers.py` + `graph.py`: dual provider (Ollama/Gemini) with
    active fallback via a dynamic-model callable (ADR-004); config-driven order.
  - `src/core/config.py`: `pydantic-settings`. `schema_hints.py`: prompt schema.
  - `tests/`: 53 passing. `test_sql_guard.py` is the priority offline suite
    (injection/escape + over-blocking); `test_ledger.py` proves the read-only
    connection blocks writes and the tool caps rows (skips if ledger absent).
  - ADR-002 (project name), ADR-003 (guardrail), ADR-004 (fallback) written.
  - Verified end-to-end against the real ledger (top overdue customers, DSO).

### Earlier
- 2026-06-08: Repo scaffolded. README, CLAUDE.md, PLAN.md, docs skeletons,
  LICENSE (MIT), .gitignore, .env.example, pyproject.toml.
- 2026-06-08: **Phase 1 — synthetic data engine.**
  - `data/generate.py`: Faker customer dimension + DuckDB set-based facts.
    ~1.06M invoices / 923k payments / 305k communications from 13k customers in
    ~45 s → `data/ledger.duckdb` (21 MB, git-ignored). Deterministic
    (seed + single thread). CLI: `--customers --as-of --seed --out`.
  - Signal verified: overdue rate rises with behaviour profile
    (prompt ≈0.5% → defaulter ≈51%). Views: `v_invoices`, `v_customer_ar`,
    `v_dso`.
  - `data/collections_policy.md`: RAG knowledge base (aging buckets + dunning
    cadence match the ledger).
  - ADR-001 written (DuckDB over Spark/Pandas). pyproject deps + ARCHITECTURE
    data-model section updated.

## Next
- **Ship gate (do first):** record `docs/demo.gif` — full step-by-step in
  [`docs/DEMO.md`](DEMO.md). **Must run on a network that does not terminate
  TLS:** termination blocks both the Gemini API and the
  one-time ONNX embedding download (verified — same cert failure as npm). The
  personal Linux notebook is the right host (no TLS termination; GPU for local Ollama if
  preferred). That GIF + the repo is the "shipped link". Optionally deploy to a
  Hugging Face Space (Docker SDK; secrets `GEMINI_API_KEY`, `APP_API_KEY`,
  build-arg `VITE_API_KEY`; `PRIMARY_PROVIDER=gemini`).
- **Same live-network session, capture the README numbers:** run
  `python -m evals.run` (with a provider) for the accuracy/pass-rate figure, and
  note one response latency. Then add the GIF + a small "evals" and "latency"
  line to the README — all real, none guessed.
- **Cleanup, low priority (queued 2026-07-31):** `evals/run.py` L23–42 carries a
  third byte-identical copy of `final_text` / `tools_used` (canonical versions
  live in `src/agent/message_utils.py`). Import the shared helpers and delete
  the copies — while the duplication lasts, the evals can silently measure stale
  behavior if the canonical helpers change. Bonus while there: make `final_text`
  filter content blocks by `type == "text"`.
  **Correction 2026-08-12 (blind claim audit): "byte-identical" is false.** The
  copies in `evals/run.py` are named `_final_text` / `_tools_used` (leading
  underscore), carry no docstring, and drop the `# some providers return content
  blocks` comment. Verified by `inspect.getsource` comparison: not identical,
  and not identical after stripping whitespace either. The accurate description
  is **logic-equivalent duplicates under different names**. The cleanup and its
  rationale are unaffected — divergence risk is the same — but the wording had
  been repeated unchecked since 2026-07-31 and is corrected here rather than
  rewritten away.
- **Cleanup, low priority (queued 2026-07-31, turn-control pass):** five small items in
  `src/agent/turn_control.py` / `tests/test_turn_control.py`, none of them a guarantee
  hole — each was measured to leave the suite fully green.
  - `finalize_answer(observations, question=None)` — the `question` parameter is **never
    read**. It was a hook for question-aware finalization that was not built. Either use it
    or drop it from the signature and from `CachedAgent._finalized`.
  - `_render_ledger` previews `rows[:5]`, but no fixture has more than five rows, so the
    slice has no owner (changing it to 50 stays green). Add a fixture that actually
    truncates and assert the `(showing 5 of N rows)` note.
  - `narrate_start` drops the `query.strip()` emptiness check with no test noticing — a
    whitespace-only query would render `on “”`. Cosmetic; pin it or simplify it.
  - `@functools.wraps` on `ToolCallTracker.wrap` is currently **redundant defense**:
    `tools.py` passes `args_schema` explicitly, so nothing depends on signature inference.
    Keep it, but the note is here so a future reader doesn't mistake it for load-bearing.
  - `tests/test_turn_control.py` defines `_NoCache` **three times** (two local, one module
    level, only one of which has `warm`). Collapse into one fixture.
- **Cleanup, low priority (queued 2026-08-01, plan-cache pass):** six items, none of them a
  guarantee hole — each measured to leave the suite fully green.
  - **The metric space has no owner.** `_CACHE_METADATA = {"hnsw:space": "cosine"}` is what makes
    `distance <= 1 - threshold` correct; switching it to `l2` or `ip` kept the whole suite green
    when this was measured (at 382 tests).
    Verified against the installed chromadb **1.5.9** that the metadata key is still honoured
    (without it a collection is created as `l2`). For normalised vectors `ip` is identical to
    cosine and `l2` returns exactly twice the cosine distance — so `l2` would silently turn the
    0.90 knob into 0.95: a slower demo, not a wrong answer. Pin the space in a test.
  - **`0.90` is written in three places** — `Settings.plan_cache_threshold` (the one production
    uses), `PlanCache.__init__` and `get_plan_cache`. Changing either default to `0.10` stays
    green, because `build_agent` always passes the setting. Give the number one owner.
    *Partly discharged 2026-08-01 (`R1-C13`):* two of the three copies are now pinned **to each
    other** — `test_the_shipped_threshold_is_the_one_these_tests_measure` fails if the setting
    and `get_plan_cache`'s default disagree, so the routing measurements cannot describe a cache
    nobody runs. `PlanCache.__init__` is still a third copy, and the number still has no single
    owner. The new `AMBIGUITY_MARGIN` deliberately does not repeat the pattern: one constant,
    referenced by both entry points.
  - **The synthetic `tool_calls` in `_replay_as_messages` have no owner.** Emptying them stays
    green, and it would make a cached answer report no tools to the UI and to the eval suite,
    whose golden checks assert which tool ran.
  - **The SSE event vocabulary is a comment, not a contract.** Dropping the `cached` event or the
    `error` event each stays green.
  - **Presentation in `plan_replay` is untested:** the scalar branch of `_render_rows`, both
    formatting branches of `_fmt`, and the `n_results=search_k` that fetches *k* and renders one.
  - **The truncation note is wrong by construction:** `note = … f"showing the first {len(rows)}
    rows"` prints the number of rows *shown*, not the total, so it implies a truncation that did
    not happen; and the literal `25` is unrelated to the `max_rows` cap that actually truncates.
    Changing 25 to 1000 stays green.
- **Cleanup, low priority (queued 2026-08-09, curated-data pass over `data/curated_plans.py`,
  `data/seed_plan_cache.py`, `tests/test_plan_cache.py`):** ten items. The first pass could not
  run the suite, so every item below was found by **reading** and carried no measured
  green/red count. **Addendum 2026-08-09 (second pass, suite runnable):** the last two items —
  the ambiguity geometry and the duplicated `dim` default — are now **measured by mutation**
  and say so inline; the tenth item did not exist before that measurement. Items one through
  eight are unchanged and remain reading-only findings. None is a public false claim about the
  product or an externally reachable hole; the first is the only one that touches prose a
  contributor would follow.
  - **Two comments swear a lockstep that does not hold, and name a list nobody owns.**
    `web/src/App.jsx` L6 says `SUGGESTIONS` must mirror `data/seed_plan_cache.py`;
    `data/seed_plan_cache.py` L28–31 says `SUGGESTIONS` must be a subset of `SEED_QUESTIONS`.
    Neither is true: `"Top 5 customers by overdue balance in each aging bucket"` is in
    `SUGGESTIONS` (5 entries) and in `CURATED_PLANS` (12 entries) and **absent from
    `SEED_QUESTIONS`** (8 entries). The real invariant — every chip is a curated question — is
    owned by `test_curated_questions_match_ui_suggestions`, which compares against
    `CURATED_PLANS` and is correct. `SEED_QUESTIONS` has **no reader outside its own module** —
    its only consumer is `seed_via_model` (L141, L152), which no test exercises, so the list is
    effectively dead code and can drift forever without a red line. Fix: point both comments at
    `CURATED_PLANS`, or derive `SEED_QUESTIONS` from it and delete the standalone list. The
    same comment's tail
    (*"a paraphrase misses the 0.90 similarity threshold"*) also predates `_question_key` /
    `_same_question` (2026-08-01), where a chip is a **key** lookup, not a distance one.
  - **The image build accepts a partial bake.** `seed_curated` ends with
    `return 0 if written else 1`, so — **read, not executed** — the `RUN` in `Dockerfile.hf`
    fails only when *every* curated plan is rejected. Eleven rejected and one accepted would be
    a green image advertising a pre-warmed cache with four of five demo chips falling through
    to the slow model. `test_the_image_build_refuses_a_curated_plan_it_could_not_replay`
    narrows this but does **not** close it: it asserts `rejection_reason(plan) is None` over
    today's static corpus against today's guards, and never calls `seed_curated()` nor looks at
    an exit code. Anything that drops a plan for a reason `rejection_reason` does not model —
    `cache.warm` raising, or the image build's environment differing from the test's — still
    exits 0 with no test red anywhere. One line closes it:
    `written == len(curated_plans())`. Verify by running the seeder against a deliberately
    poisoned corpus and reading `$?`; that measurement has not been made.
  - **The seeder's module docstring and ADR-012 describe only the model path.** Both show
    `python -m data.seed_plan_cache`; `Dockerfile.hf` L111 and `scripts/hf_entrypoint.sh` L80
    run `--curated`, which needs no provider. A reader following the ADR reproduces the slow
    build the curated corpus was created to remove.
  - **`rejection_reason(plan: Any)` gives away a type for free.** The `Any` avoids a top-level
    import of `Plan`, but the file already has `from __future__ import annotations`, so the
    annotation is a string and costs nothing. Annotate it `Plan`.
  - **`seed_via_model` reaches into `agent._cache`** (private attribute of `CachedAgent`).
    Nothing in CI runs that path (it needs a provider), so a rename there breaks silently until
    a human runs the dev path.
  - **One-character substring oracles in the replay tests.** Three `assert "2" in …`
    (`test_plan_cache.py` L258, L318, L355) plus the sibling `assert "3" in second.reply`
    (L264), which carries the same weakness. The rendering is deterministic (`**n**: 2`), so
    these can be equalities against the rendered tail; today they pass only because
    `_FRESHNESS_CLAIMS` and the `"_(Answered from a cached plan — …)_"` wrapper
    (`plan_replay.py` L60–65, L127) contain no digit — a fact of the current strings, not a
    property anything enforces.
  - **`test_curated_plans_are_all_guard_valid_and_replayable` does not replay anything.** It
    checks tool names against `REPLAYABLE_TOOLS` and runs `guard_query`; it opens no ledger and
    never calls `freshness_violation`. The real owners are
    `test_every_curated_plan_replays_against_the_real_ledger` and, for freshness,
    `test_replay_claims.py`. Not a hole — a name that teaches the wrong coverage. Rename to
    `..._are_all_guard_valid_and_well_formed`.
  - **Two ADR-013 tests live in `tests/test_plan_cache.py`** (L536–586).
    `test_select_prompt_uses_brief_for_constrained_tiny_model` imports nothing from
    `plan_cache` and tests `graph.select_prompt`. Anyone looking for prompt-tier coverage will
    look in `test_constrained.py`, not find it, and write it a second time.
  - **The ambiguity tests' geometry has no anchor.** Six tests are calibrated against distances
    measured with `DeterministicEmbeddingFunction(dim=256)` — 0.857, 0.926, 0.935/0.802, 0.164,
    0.143 — all of which live in **comments**. No test pins `dim`; the fixture calls the
    constructor with no argument. Changing that default (an implementation detail nobody would
    treat as a rule) silently moves the space six correctness tests measure. Pin the dimension
    in the `embedding_function` fixture so the assumption and the calibration sit together.
    **Measured 2026-08-09:** flipping the default to 512 leaves the suite at **449 green**,
    while the largest distance shift across the 66 curated-corpus pairs is **0.126** (0.174 at
    dim=4096) — wider than the **0.05** margin three of those six tests use as their rule. The
    space moves enough to change a verdict; the suite does not notice because the pairs it
    exercises are not the pairs that moved. Note for whoever fixes this: `dim` reaches the
    geometry only through hash collisions, so the sharper missing anchor is on the **tokeniser**
    (`_TOKEN_RE`, which lowercases and drops punctuation) — that is what decides the shared-word
    sets the distances are made of. The tokeniser mutation has **not** been run.
    **Addendum 2026-08-12 — the tokeniser mutation was run, and this recommendation is wrong.**
    Dropping digits from `_TOKEN_RE` (`[a-z0-9]+` → `[a-z]+`) leaves the suite at **449 green**.
    Reason: `tests/test_plan_routing.py`, which owns the curated-corpus geometry
    (`AMBIGUITY_MARGIN`, the `(12, 66)` anchors, the walls at 0.0482/0.1589), does **not** use
    `DeterministicEmbeddingFunction` at all — it imports `default_embedding_function`, i.e. the
    same embedder production uses (ChromaDB's `DefaultEmbeddingFunction`; that this is the bundled
    ONNX all-MiniLM-L6-v2 is the library's documented default, not something measured here).
    The tests that do use the hashing embedder spell their numbers as words
    (`"how many customers…"`, `"top five…"` / `"top ten…"`), so no digit ever crosses it. So both
    tokeniser and `dim` are green for the same structural reason, and neither is the sharp anchor
    this note predicted. What remains genuinely unpinned is the `dim` **literal pair** in the
    next bullet, which has a real failure mode.
  - **Two independent literals for the same `dim` default** (`src/rag/embeddings.py`).
    `__init__(self, dim: int = 256)` at L40 and `config.get("dim", 256)` inside
    `build_from_config` at L69 hard-code the same number separately. **Measured 2026-08-09:**
    set them to disagree (constructor 512, fallback 256) and the suite stays at **449 green** —
    nothing compares the two. Today the path is narrow because `get_config` always emits a
    `dim` key, so a persisted collection round-trips correctly; the exposure is a collection
    baked by an older version, or any future edit to `get_config`. The failure mode is not an
    exception but a **different vector space answering confidently**, which is the ADR-009
    Amendment 2026-08-01 (`R1-C13`) failure mode one layer down. Fix: one module-level constant
    both sites read. This is the third instance of two-copies-of-one-owner in this repo after
    ADR-014 (`R1-C12`, whitespace strip) and `R1-C14` (question normalisation).
- **Cleanup, queued 2026-08-12 (RAG / ledger / evals pass, `R1-T7`):** seven findings, each
  measured, none fixed — the study programme documents, it does not repair. None is a live public
  falsehood or an externally reachable hole. Baseline for every measurement below: **449 green**,
  tree restored afterwards.
  - **T7-1 — `enable_external_access=false` has no test at all** (`src/agent/ledger.py` L28–34).
    This is the **confidentiality** half of the guardrail: `read_only=True` blocks writes, but
    exfiltration is a *read*, so what stops `read_csv('/etc/passwd')` at engine level is this key.
    **Measured:** delete it → **449 green**, nothing changes colour. And it is load-bearing, not
    decorative — probed directly with the key removed, the connection returns the contents of
    `/etc/passwd`; with the key restored, `PermissionException: file system operations are
    disabled by configuration`. Not urgent: `guard_query` refuses `read_csv` before **execution**
    (function allow-list, pinned against `duckdb_functions()` since `R1-C3`; verified directly —
    `GuardrailError: Function(s) not in allow-list: read_csv`), so this is loss of a
    defence-in-depth layer, not of the only gate. Two qualifiers recorded by the 2026-08-12 claim
    audit, both narrowing the reassurance: the payload **does** reach the DuckDB *parser*, since
    `guard_query` itself validates via `json_serialize_sql` — "before the engine sees it" would be
    wrong, "before it executes" is right; and the refusal was exercised at `guard_query` level,
    not end-to-end through the agent tool path, so "unreachable" holds only as far as every path
    really does go through `guard_query`. Fix: one test that asserts the engine refuses an
    external read, so a cleanup PR cannot silently remove the layer.
  - **T7-2 — the `dim` literal pair** — see the bullet above; re-confirmed by reading in this
    pass, not re-measured (measurement budget).
  - **T7-3 — the golden expectations are pinned by a docstring, not by a test.**
    `evals/golden.py` ends with *"Update the expectations if `data/generate.py` or the policy doc
    changes"* and the values were verified against `data/ledger.duckdb` on 2026-06-08. That is an
    instruction to a human, so nothing goes red when the generator changes — the evals just start
    measuring a world that no longer exists, and the published pass rate becomes fiction. Same
    disease `R1-C11` closed for curated plans; the fix there was to *execute* the curated data
    against the real artifact, and that does not exist here yet.
  - **T7-4 — `evals/run.py` is covered by nothing, and its docstring overclaims.**
    `tests/test_evals.py` says it covers *"everything that doesn't need an LLM"*; the pure
    functions `_final_text` and `_tools_used` need no LLM and are untested, as are the exit
    codes. Aggravated by their being the third copy flagged in the 2026-07-31 cleanup bullet
    above — logic-equivalent to `src/agent/message_utils.py`, not byte-identical (see the
    correction on that bullet); while the copies last, the eval harness can measure stale
    behaviour.
  - **T7-5 — the heading boundary lives only in a comment** (`src/rag/chunking.py` L19–21).
    **Measured:** relaxing `_HEADING_RE` from `^##` to `^#{2,}` (i.e. `###` also starts a section)
    leaves **449 green**, because neither the policy document nor the chunking fixture contains a
    `###` today. This is the mutation that most resembles a real PR ("also accept subsections").
    The failure is not an exception: a rule splits across two chunks and the lower half loses the
    heading that makes it citable. Fix: one fixture with a subsection.
  - **T7-6 — the eval oracle can be relaxed from `all` to `any` unnoticed**
    (`evals/checks.py` L30, `mentions_all`). **Measured:** the flip leaves **449 green**. The
    three tests that exercise it all happen to use cases where the operators agree (two terms both
    present, one term absent, empty reply); the discriminating case — **two terms, one present** —
    is missing. Under `any`, the `credit_hold_rule` case (`["60", "credit limit"]`) passes on a
    reply that says *"we place accounts on hold after 60 days"* and omits the credit limit, i.e. a
    false pass entering the pass rate the README publishes. Fix: one two-term/one-present case.
  - **T7-7 — `{"hnsw:space": "cosine"}` is written twice**, in `src/rag/index.py` L23
    (`_COLLECTION_METADATA`, the policy collection) and `src/agent/plan_cache.py` L498
    (`_CACHE_METADATA`, the plan cache). **Measured:** dropping the metadata from the *policy*
    collection leaves **449 green** — unsurprising, since 13 chunks under a hashing embedder rank
    correctly under any sane metric. ⚠️ **The plan-cache one was NOT measured in this pass**
    (6-mutation budget spent), and it is the one the older 382-test note was really about; it is
    load-bearing there, because with normalised vectors `l2` is exactly 2× cosine distance and
    `AMBIGUITY_MARGIN = 0.10` is calibrated against walls measured at 0.0482 and 0.1589. Flagged
    here explicitly as *not measured* so the next pass picks it up. Fourth instance of
    two-copies-of-one-decision in this repo, and the most benign of the four — the collections are
    legitimately distinct and nothing requires them to share a metric.
- **Cleanup, queued 2026-08-12 (API / MCP / schemas pass, `R1-T8`):** eleven findings, each
  measured unless marked otherwise, none fixed — the study programme documents, it does not
  repair. Baseline for every measurement below: **449 green**, tree restored afterwards. None is
  a live public falsehood: ADR-007 already declares that the UI-baked key is visible to anyone
  inspecting the bundle, and `DEPLOY.md` publishes the demo key itself. One item (T8-1) is an
  externally reachable unhandled exception, but it grants no access — see the reasoning there.
  - **T8-1 — a non-ASCII `X-API-Key` returns 500 instead of 401** (`src/api/app.py` L67).
    `secrets.compare_digest` on two `str` requires both to be pure ASCII and raises `TypeError`
    otherwise. Starlette decodes header values as latin-1, so any byte ≥ 0x80 in the header
    passes the `not key` check and then explodes inside the compare. **Measured** with a
    throwaway probe: `compare_digest("café", "test-key")` → `TypeError: comparing strings with
    non-ASCII characters is not supported`; the same request through `TestClient` with the
    header sent as **bytes** propagates the exception; with `raise_server_exceptions=False`
    (what a deployed uvicorn does) the response is **500**. ⚠️ The first attempt sent the header
    as a `str` and **httpx refused to encode it** — a refusal in the client library, not in the
    protocol or the server, which would have produced a false "not reachable" verdict. HTTP
    headers on the wire are bytes. Not urgent: the caller is not authenticated either way and
    gains no access; the cost is log noise, 5xx alerting, and an unhandled path. Note the
    irony, measured as mutation E2: replacing `compare_digest` with `==` **removes** this bug.
    Fix: compare encoded bytes — `secrets.compare_digest(key.encode(),
    settings.app_api_key.encode())` — which is constant-time **and** byte-safe (probed: `False`
    for the wrong pair, `True` for the right one, no raise).
  - **T8-2 — the MCP server shares one DuckDB connection across all tool calls**
    (`mcp_server/server.py` L85, with `src/agent/ledger.py` `run_query`). `create_server` opens
    one connection and closures it into the tool; `run_query` does `cur = con.execute(sql)` then
    `cur.fetchall()`, and in DuckDB `con.execute()` returns the connection itself, so those are
    two steps over one shared object. FastMCP runs sync tools in a threadpool, so concurrent
    calls can interleave and one client can receive another's rows. ⚠️ **NOT MEASURED** —
    reading only, recorded as a grounded hypothesis, not a confirmed defect (the 6-mutation
    budget was spent and reproducing a race needs a harness this pass did not have). What is
    unknown: whether DuckDB's C layer serialises execute/fetch enough to close the window.
    Fix candidates: a connection per call, or `con.cursor()`.
  - **T8-3 — the streaming endpoint's central guarantee has no owner** (`src/api/app.py`
    L129–130). The comment says *"never leave the stream half-open"*. **Measured:** delete the
    whole `try/except` and the suite stays at **449 green**. Scope qualifier from the claim
    audit: an exception *is* exercised on the stream path one layer down —
    `tests/test_turn_control.py` L507 raises `GraphRecursionError` inside the stub feeding
    `CachedAgent.astream`. What has no owner is the **API-level** backstop, the outer catch that
    exists for when the inner layer does not hold. Consequence: the generator ends, the client
    gets a truncated body with no
    `[DONE]`, and the UI (which reads to the end of the reader) spins forever. There is no HTTP
    status left to send, because `200 OK` went out with the first byte. Note the asymmetry: the
    non-streaming route does not need this guarantee (an exception becomes a 500 that FastAPI
    handles), while the streaming route depends on it entirely. Fix: a stub whose `astream`
    raises on the second event, asserting an `error` event followed by `[DONE]`.
  - **T8-4 — the route/mount order is a guarantee held only by line position** (`src/api/app.py`
    L139–142). `app.mount("/", StaticFiles(...))` matches every path and Starlette matches routes
    in registration order, so the API survives because those lines sit last. **Measured:** move
    the block to just after `FastAPI(...)` → **10 red**, all of `tests/test_api.py`, failing with
    `405 Method Not Allowed` (StaticFiles matched the path and refused the POST). ⚠️ But the
    mount is conditional on `web/dist`, which is git-ignored, so **in CI the same mutation is a
    no-op** — that half is *reasoning, not measurement*: the suite was not run with the directory
    removed. A "group the config together" refactor therefore deletes the API and CI approves the
    PR. Fix: a fixture that builds the app with a temporary `dist` present and asserts
    `/api/health` still answers.
  - **T8-5 — no input ceiling has an owner, and there is no rate limit** (`src/api/schemas.py`
    L27–28). `max_length=4000` and `max_length=50` are the only context budget and the only cost
    boundary on the most expensive endpoint. **Measured:** multiply both by a thousand
    (4_000_000 and 100_000) → **449 green**; `grep -rn "4000\|max_length" tests/` returns
    nothing. Honest framing: the missing rate limit is not a regression, it is a control that
    never existed, and a synthetic-data demo survives without it — what it should not do is ship
    without either.
  - **T8-6 — the role allow-list is pinned by an enumerated guard** (`src/api/schemas.py` L15,
    `tests/test_api.py` L107–113). `Role = Literal["user", "assistant"]` is the boundary defence
    against a client injecting a system prompt through `history`. Its only owner tests **one**
    value, `"system"`. **Measured** by probe: the roles accepted today are exactly
    `["user", "assistant"]` (of user/assistant/system/tool/function/developer), so the policy is
    correct now — but widening the `Literal` with `"tool"` leaves the existing test green. Same
    disease `R1-C9` closed elsewhere in this repo by comparing the whole surface. Fix:
    `assert set(get_args(Role)) == {"user", "assistant"}`.
  - **T8-7 — four of the five MCP tests do not run in CI, and none asserts *why* a payload was
    refused** (`tests/test_mcp.py`). (a) `data/ledger.duckdb` is git-ignored and four tests carry
    `@needs_ledger`, so CI **skips** them and a skip reports as green. (b) The refusal tests
    assert only `error == "rejected_by_guardrail"`, but the connection is `read_only=True` and
    `secrets` does not exist — the **engine** would refuse those payloads anyway, so the tests do
    not discriminate which layer refused and would stay green with the guard disabled. Measured
    by reading plus the `skipif` marker; on a machine with the ledger they do run (that is why
    mutation E5 went red). Mitigating and worth stating: `tests/test_tool_error_contract.py`
    (written during `R1-C5`) builds an in-memory table, runs in CI, and does drive the MCP
    surface end to end — mutation E6 confirmed it with exactly 1 red. So the lesson already
    exists in a neighbouring file and was not propagated here. (c) Smaller, same family: the
    `con` fixture calls `duckdb.connect(...)` directly instead of `connect_readonly`, i.e.
    without `HARDENED_CONFIG` / `enable_external_access=false` — the suite meant to prove
    security parity tests against a *less* hardened connection than production opens. Compounds
    `T7-1`.
  - **T8-8 — `truncated` is an inference presented as a fact** (`mcp_server/server.py` L75).
    The guard wraps the query in `LIMIT max_rows`, and the code infers "there was more" from
    "we got the maximum". A result of exactly `max_rows` rows is reported as truncated when it
    was not. **Measured (E5):** the field does have an owner — flipping `>=` to `>` gives 1 red
    (`test_row_cap_truncates`) — but that test uses a cap of 5 against 13,000 customers, far from
    the boundary; the case `len(records) == max_rows == total` is untested. Errs toward warning,
    which is the safe direction. Fix costs one row: ask for `max_rows + 1` and return `max_rows`.
  - **T8-9 — the MCP tool docstring is a prompt, and nothing pins it** (`mcp_server/server.py`
    L90–95). FastMCP publishes it as the tool description in the protocol, so it is the text the
    *other side's* model reads to decide how to call. It promises SELECT/WITH-only over
    allow-listed relations, single statement, row cap. **Measured** by grep: no test mentions it,
    so if `guard_query`'s policy changes the description keeps advertising the old one and
    nothing goes red. Same class of surface as `schema_hints.py`, where `R1-C8` did add a
    dedicated test. Failure mode is efficiency (more or fewer client retries), not security.
  - **T8-10 — `/api/health` answers `"ok"` with the agent down** (`src/api/app.py` L82–84,
    `src/api/schemas.py` L38–40). `status` is a `Literal["ok"]` constant, so the route returns
    **200 / `"ok"`** even when `agent_ready` is `False`. **Measured** by probe with
    `agent_builder=lambda s: None`: health returns `200 {'status': 'ok', 'agent_ready': False}`
    while `/api/chat` correctly returns `503 {'detail': 'Agent is not ready.'}` — which also
    establishes that the 503 branch works and **has no test of its own**. Any orchestrator
    reading the status code (the Docker `HEALTHCHECK` default, every Kubernetes probe) keeps a
    container that answers nothing. This conflates *liveness* with *readiness*, which are
    deliberately different probes. Fix: return 503 when not ready, or split into two routes.
  - **T8-11 — small, grouped:** the two message-assembly lines are duplicated between `chat` and
    `chat_stream` (fourth instance of two-copies-of-one-owner in this repo, after `R1-C12`,
    `R1-C14` and the `embeddings.py` `dim` pair); `import pytest` appears twice in
    `tests/test_mcp.py` (L15, L27) and `import json as _json` sits inside a function in
    `tests/test_api.py`; `zip(..., strict=False)` in `run_guarded_query` picks the silent-truncate
    direction where `strict=True` would cost nothing (the invariant does hold — both come from
    the same cursor); `_jsonify` coerces `Decimal` to `float`, an undeclared precision loss in a
    monetary domain (`str(value)` would preserve it); `test_stream_emits_tool_then_answer_events`
    compares the first event by whole surface but the last field by field;
    `test_chat_forwards_history_then_new_message` checks roles and the last message but not the
    history contents; `create_server` and `main` have no tests (testing the core
    `run_guarded_query` is the right call — recorded as declared coverage, not a defect).
- After that: optional polish only. The MVP (Phases 0–5) is functionally done.

## Open decisions / notes
- RAG default embeddings (ONNX MiniLM) **download the model once on first use**;
  this sandbox blocks the fetch (SSL cert), so the live ONNX path is verified by
  code but not exercised here. Tests run offline on the deterministic hashing
  embedding by design. On the demo Space the one-time download is expected.
- `data/chroma/` (the persisted index) is built on demand and git-ignored like
  the ledger; rebuild is automatic (idempotent) on first agent run.
- Agent **exercised live on 2026-06-08** (host run, Ollama `qwen2.5:7b`): real
  tool-using answers, HTTP 200. Tests stay offline by design. A live run needs
  `ollama serve` + a tool-capable model (note: the default `llama3.1` works, and
  `qwen2.5:7b` is confirmed) or a `GEMINI_API_KEY`.
- Install providers per env: `pip install -e ".[ollama]"` and/or `".[gemini]"`
  (lazy imports — only the one you run is required).
- Regenerate the ledger with `python data/generate.py` (needs `faker` extra:
  `pip install -e ".[data]"`). The `.duckdb` file is not committed.
- Provider order: **Ollama primary** in dev (zero cost / no network), **Gemini
  fallback**; flip via `PRIMARY_PROVIDER=gemini` for the cloud demo (ADR-004).
