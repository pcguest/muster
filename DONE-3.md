# Goal 3: config generation, assisted mapping, performance and the demo

Muster now proposes its own configuration from real files (with mandatory
human review), can ask an LLM for mapping help without ever applying it or
leaking cell data, consolidates five million rows in seconds on a laptop,
and ships a synthetic demo that shows the whole pipeline off.

## What exists

- **`muster init --from <folder>`** (`scaffold.py`): profiles the folder,
  clusters heading variants (strictly — a missed join is two visible
  proposals, a wrong join could smuggle a bad synonym past review) and
  writes a proposed muster.yaml: most common variant as the canonical
  name, inferred types, required flags, observed synonyms. Every inference
  carries a `# PROPOSED` marker with its rationale, and `load_config`
  refuses to serve a marked file — generation never silently becomes the
  configuration of record. `muster confirm` strips the markers after
  validating the result. Headings are untrusted, so every string in the
  generated YAML is JSON-quoted against structure injection.
- **LLM-assisted mapping, off by default** (`assist.py`): `muster run
  --assist` asks a model to propose targets for columns fuzzy matching
  could not map. Provider-agnostic client — Anthropic messages or any
  OpenAI-compatible chat endpoint — keyed only by the MUSTER_LLM_API_KEY
  environment variable; without it the feature is unavailable and the tool
  fully functional. Privacy is structural and documented prominently: only
  column headings, inferred types and at most five redacted samples
  (digits masked, length truncated, both configurable) are sent — no cell
  data, no file names — and the review file records exactly what left the
  machine. Proposals sit `pending` in mapping-review.yaml with confidence
  and rationale until `muster review` (interactive per-proposal, or
  `--accept-all`/`--reject-all`, or a hand edit) decides; only accepted
  mappings feed later runs, as an `assist` stage below declared synonyms.
  Model output is untrusted input: parsed, validated, dropped if malformed.
- **Performance**: CSV sources stream through Polars' lazy scanner and
  XLSX sources are row slices from calamine, both bounded by
  `limits.chunk_rows`; date/datetime coercion is vectorised for all
  numeric formats (month-name formats stay per-cell because Polars' %b/%B
  fast path panics on unlucky value mixes — a crash untrusted input must
  not be able to cause; regression test pins the mix); reconciliation
  passes unique keys straight through and partitions only duplicates.
  `scripts/bench.py` measures a full pipeline run in its own process:
  5,000,150 rows in 8.9 s (~564k rows/s) at 2.03 GiB peak RSS on an M2
  laptop. docs/PERFORMANCE.md records method, numbers and the honest
  caveats (the typed dataset is held in memory; chunking bounds read
  buffers, not the final frame).
- **`muster demo`** (`demo.py`): writes three invented grain-receival
  spreadsheets — clashing headings, ISO/day-first/month-name dates,
  thousands separators, boolean spellings — plus a ready muster.yaml, and
  runs the pipeline: 17 rows in, 11 published, 5 held (a cross-site ticket
  conflict pair, two uncoercible cells, an unexpected commodity), 1 merged
  duplicate, 4 errors and 4 warnings, all by design. Every value is
  invented; no real growers, sites or organisations.

## Verification

- 81 tests pass via `pytest`: clustering/type/required proposals, the
  YAML-injection heading, refusal-then-confirm round trips, redaction,
  both provider request shapes, untrusted-model-output validation, the
  full assist-review-apply flow (interactive and non-interactive), demo
  numbers against the manifest, chunk equivalence and continuity, the
  month-name crash regression, and a benchmark smoke run. Every LLM test
  mocks the transport; nothing touches the network.
- Live from a clean folder: `muster demo` → published 11 of 17;
  `muster init --from demo/sources` → 58 marked inferences; `muster run`
  refused until `muster confirm`; then exits 2 on the demo's deliberate
  errors. README gained config-generation, Assist (privacy stance stated
  plainly) and Performance sections; version is 0.3.0 in both
  pyproject.toml and the package.

## Deliberately not started

Connectors, any UI, and daemon/scheduling work — later goals cover them.
