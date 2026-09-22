# Enrichment: suggested titles and tags

Plan for two features — proposing titles for notes that lack a useful one, and
tags for notes that have none. Both are **suggest-only**: nothing is written back
to Joplin in this increment.

Status: planned, not built. Benchmarks in §2 were run against the real Ollama
host on 2026-09-22.

## 1. Decisions taken

| Decision | Choice | Consequence |
|---|---|---|
| Apply mechanism | **Suggest-only** | Every existing read-only invariant survives untouched |
| Tag vocabulary | **Closed** — existing tags only | No taxonomy drift; far easier for a small model |
| Write-back | **Next increment**, separate path | Suggestion rows are shaped now to carry what an apply will need |

## 2. What the hardware actually does

Measured on the target box (16 cores, 54 GB, AMD Phoenix3 iGPU via Vulkan,
`OLLAMA_IGPU_ENABLE=1`). The iGPU gives ~1.57x on 8B and nothing on 1.5B; ROCm
refuses gfx1103 unless `HSA_OVERRIDE_GFX_VERSION` maps it to gfx1102, untested.

### Titles

| Model | Per note | 2,205 notes | Notes |
|---|---|---|---|
| llama3.2:1b | 0.51 s | 19 min | Wrong on the hardest test note |
| qwen2.5:1.5b | 0.43 s | 16 min | Inconsistent run to run at the same temperature |
| **llama3.1:8b** | **1.06 s** | **39 min** | Correct and stable; clean output format |
| gpt-oss:20b | 11.4 s | ~7 h | Best single result, but see below |

**Chosen: `llama3.1:8b`.** gpt-oss is a reasoning model — it returns an *empty*
response under a short token budget because thinking consumes all of it, and
`think: false` does not suppress that. Given 512 tokens it produced the best
title of anything tested, but at 10x the cost with unpredictable token
budgeting, for a task containing no reasoning. Keep it as an optional second
pass for rejected suggestions, not the default.

Two measurements that become code:

- The 1B/1.5B models ignored "no quotes" in **3 of 5** cases; the 8B in 0 of 5.
  Output is post-processed, never trusted to follow the format instruction.
- The 1.5B model gave a *different and wrong* answer on one note between two
  runs at the same temperature. Generation uses `temperature: 0` and results are
  cached, so a note is titled once per set of inputs (§4) rather than re-rolled.

The 39 min figure is measured on short notes. Prompt evaluation scales with
input, so long notes cost more; truncation (§5) is what bounds it.

### Tags

Embedding the whole vault is nearly free: **1.1 min** with `bge-large`, 0.4 min
with `nomic-embed-text`. Retrieval quality on a deliberately tiny corpus was
mixed — one clean, one partial, one wrong. The instructive failure:

```
"rotate the key-encryption key on a schedule or only on compromise"
   -> billing (1.02), llm-gateway (0.54), openbao (0.52)
```

`openbao` and `security` are correct; `billing` won outright. A six-document
corpus understates real performance badly, but the failure mode is **frequency
and centroid bias** — tags on generically ops-flavoured notes winning regardless
of topic — and that does not disappear with scale. §6 normalises for it.

## 3. What this does not change

The point of suggest-only is that the existing guarantees hold as written:

- `joplin_client.py` stays GET-only, and the AST scan in
  `tests/unit/test_joplin_client_readonly.py` keeps passing unmodified.

  Be precise about what that buys, because an earlier draft of this document
  overclaimed it. The scan proves **`JoplinClient` is GET-only**. It does not
  prove that the *indexer* cannot mutate, and once a writer module exists it
  will not: nothing stops `indexer.py` or an enrichment worker importing it.
  Scoping the scan to one file preserves a narrow guarantee, it does not
  preserve the broad one.

  So the broad guarantee needs its own enforcement, added **with** the writer
  (§9): an import-boundary test asserting that the indexing and enrichment
  modules never import or reference the writer, and an extension of the
  integration no-mutation guard — which today exercises only rebuild, sync and
  reindex — to cover the enrichment flows too. Until that exists, the honest
  statement is the narrow one.
- **No index schema change.** Everything new lives in a second database file, so
  the index stays at schema v2 and existing users skip the delete-and-resync that
  a v3 would force. `index rebuild` is unaffected.
- `LocalOnlyTransport` is reused, not widened. It is already parameterised by
  base URL, so the Ollama client gets its own instance fenced to the Ollama
  origin. The Joplin fence is not touched. (Its helper is named
  `assert_url_is_joplin_base`; generalise the name when it gains a second
  caller.)

What **does** change: note bodies leave the process for the Ollama host. That is
a real shift in data flow, which is why enrichment is off by default and the
endpoint is explicit configuration rather than a default.

## 4. Storage

A separate `suggestions.sqlite3` beside the index. Rationale: suggestions are
derived and regenerable, but **your accept/reject decisions are not**, and the
documented schema-upgrade path for the index is "delete the file". Non-derived
state must not live somewhere designed to be disposable.

```sql
CREATE TABLE suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id           TEXT NOT NULL,
    kind              TEXT NOT NULL,      -- 'title' | 'tags'
    input_hash        TEXT NOT NULL,      -- identity: every input, see below
    payload           TEXT NOT NULL,      -- JSON: {"title":...} | {"tags":[...]}
    current_value     TEXT,               -- what it would replace, for review and undo
    note_body_hash    TEXT NOT NULL,      -- component of input_hash, kept for diagnostics
    note_updated_time INTEGER,            -- optimistic-concurrency token for the apply path
    corpus_revision   TEXT,               -- tags only; drives staleness, not identity
    model             TEXT NOT NULL,
    prompt_version    INTEGER NOT NULL,
    confidence        REAL,
    reason            TEXT,               -- why the note was a candidate
    stale             INTEGER NOT NULL DEFAULT 0,
    decision          TEXT,               -- NULL | 'accepted' | 'rejected'
    decided_at        INTEGER,
    created_at        INTEGER NOT NULL,
    UNIQUE(note_id, kind, input_hash)
);

-- "Leave this note's title alone", distinct from rejecting one suggestion.
CREATE TABLE opt_outs (
    note_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (note_id, kind)
);

-- Derived, but kept here so the index schema stays untouched.
CREATE TABLE note_embeddings (
    note_id    TEXT PRIMARY KEY,
    body_hash  TEXT NOT NULL,
    model      TEXT NOT NULL,
    vector     BLOB NOT NULL,
    created_at INTEGER NOT NULL
);
```

### Cache identity

`notes.body_hash` is computed from the **body alone** (`repositories._body_hash`).
Keying on it is wrong in three ways that an earlier draft missed:

- Retitle a note without touching the body and the key is unchanged, so an old
  rejection suppresses a suggestion that is now applicable again — the exact
  opposite of the intended behaviour.
- A title suggestion depends on the *current title* (it is what the suggestion
  replaces, and what §5 compares against); a tag suggestion depends on the note's
  *current tags*.
- `model` was absent entirely, so the optional gpt-oss second pass in §2 could not
  store a result alongside the llama3.1:8b one for the same body and prompt
  version. The plan contradicted itself.

So identity is an `input_hash` over everything the suggestion actually depends on:

```
title:  sha256(body_hash | current_title      | model | prompt_version)
tags:   sha256(body_hash | sorted(tag_ids)    | model | prompt_version)
```

The decision lives on the suggestion row and is therefore keyed by that hash: any
change to a real input yields a new row, undecided, rather than inheriting a
stale verdict. `opt_outs` covers the separate case of "my title is fine, stop
asking", which must survive all of it.

### The corpus dependency

Tag suggestions also depend on the tagged-note corpus and its frequency
weighting (§6), which shift every time *any* note is tagged. That deliberately
does **not** go in `input_hash`: accepting one suggestion would invalidate every
other pending one, and the queue would thrash.

Instead `corpus_revision` is recorded alongside, and a row whose recorded
revision has drifted materially from current is marked `stale` and re-queued —
while still being served until a replacement exists. Staleness is a signal to
regenerate, not a reason to hide a suggestion that is probably still right.
Define the revision coarsely (tagged-note count plus tag-vocabulary size, say)
so ordinary day-to-day tagging does not trip it.

## 5. Titles pipeline

**Candidates** — pure SQL and regex over `notes`, no model involved, so this half
is unit-testable on its own:

- empty title
- Joplin defaults: `Untitled`, `New note`, `New to-do`
- bare dates and timestamps: `20260114`, `2026-01-14`, `14/01/2026`
- URLs and web-clipper boilerplate
- filenames (`*.pdf`, `*.docx`, …)
- auto-derived from the body: the title is a prefix of the first body line at or
  near Joplin's truncation length

Each candidate records *why* it was selected, which drives both the review UI and
precision measurement per rule.

**Generation**: truncate to first 1500 + last 500 characters; versioned prompt;
`temperature: 0`, `num_predict: 32`.

**Post-processing** (not optional, per §2): strip wrapping quotes, strip trailing
punctuation, collapse whitespace, reject when empty, over the word ceiling, or
equal to the existing title.

## 6. Tags pipeline — retrieve then rerank

Embeddings do recall, the model does precision. Both already on the box.

1. Embed the note (`bge-large`, ~30 ms), cached by `body_hash`.
2. kNN against embeddings of **already-tagged** notes; take the top K neighbours.
3. Score each neighbour tag by summed similarity, **divided by a function of that
   tag's corpus frequency**. This is the specific counter to the `billing`
   failure in §2: without it, common tags win on generic phrasing.
4. Keep the top ~8 as candidates.
5. Ask `llama3.1:8b` to choose 0–3 *from that list*, or reply `NONE`.
6. Intersect the reply with the candidate list — a hard constraint in code, not a
   trusted instruction — and map to tag IDs.

Closed vocabulary falls out of step 5 for free: the candidates can only come from
tags that already exist. Disagreement between the embedding ranking and the
model's pick is a usable confidence signal.

## 7. Worker, config, API

Overnight batch with a handful of new notes per day means **a plain serial
queue** — no concurrency tuning, no batch-size search. It runs off the request
path and off the incremental tick, so a multi-second model call cannot block the
event loop or delay sync.

```toml
[enrichment]
enabled = false                 # opt-in: note bodies leave the process
ollama_base_url = "http://…:11434"
title_model = "llama3.1:8b"
tag_model = "llama3.1:8b"
embedding_model = "bge-large:335m-en-v1.5-fp16"
max_tags_per_note = 3
neighbours = 25
```

```
GET  /api/suggestions?kind=title|tags&status=pending
POST /api/suggestions/{id}/accept     # records a decision; writes nothing to Joplin
POST /api/suggestions/{id}/reject
POST /api/enrich/run                  # 202, starts a batch
GET  /api/enrich/status               # progress, queue depth, last error
```

Dashboard: one review column, reusing the existing note-row rendering.

## 8. Phasing

1. `suggestions.sqlite3` schema, repository, and config block.
2. Title candidate detection — no model, fully unit-tested.
3. Ollama client, fenced with `LocalOnlyTransport`; `doctor` check for reachability.
4. Title generation, post-processing, caching by `body_hash`.
5. Embeddings: backfill, storage, kNN with frequency normalisation.
6. Tag candidates, reranked and vocabulary-constrained.
7. API and dashboard review column.
8. Measurement: precision per detection rule against your own decisions.

Step 8 matters. After a few hundred real accept/reject decisions you have ground
truth, and the rules in §5 and the weighting in §6 can be tuned against it rather
than against intuition.

## 9. The write-back increment

Design implications to preserve now, so the later change is additive:

- Goes in a **new module** with its own client — and ships with the two tests
  that make the broad guarantee real rather than assumed (§3): an import-boundary
  scan proving no indexing or enrichment module reaches the writer, and the
  integration no-mutation guard extended over the enrichment flows. Adding the
  writer without those would quietly downgrade the invariant to "one client is
  GET-only".
- **Optimistic concurrency**: refuse to apply when `note.updated_time` differs
  from `note_updated_time` on the suggestion. This is why that column exists now.
  It prevents clobbering an edit made since the suggestion, and avoids generating
  Joplin sync conflicts.
- **Undo log** holding the previous value — `current_value` is captured at
  suggestion time for exactly this.
- Dry-run by default; explicit confirmation; a kill switch that disables the
  writer without disabling enrichment.

## 10. Risks

- **Damage to a real PKM.** 2,205 notes is a corpus you rely on. Suggest-only
  removes this risk entirely for now, which is the main argument for taking the
  increments in this order.
- **Bad candidate detection is worse than bad generation.** A wrong suggestion on
  a note whose title was fine wastes review attention and erodes trust in the
  whole feature faster than a mediocre suggestion on a genuinely untitled note.
  Tune §5 for precision over recall.
- **Review fatigue.** Several hundred pending suggestions is not reviewable in
  one sitting. Order by confidence, support bulk-reject per detection rule, and
  treat "how many did you get through" as a product metric.
- **Model drift.** `prompt_version` and `model` are on every row so a change in
  either can be identified and selectively regenerated.
