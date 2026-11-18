<!-- Copied from docs/example-pipeline.md by scripts/build_skill.py. Edit the original, not this file. -->

# Example pipeline: `doclearn`

[`examples/doclearn`](https://github.com/aadhithya/micro/blob/main/examples/doclearn) is a worked example of a pipeline on top of `micro`: learn the template of a document
family from a few examples, then write a new document from a few facts. It is a demonstration of what the harness makes
easy, not a finished product, and it is not specific to any domain. The sample documents (project proposals and
incident reports) are in [`examples/doclearn/samples`](https://github.com/aadhithya/micro/blob/main/examples/doclearn/samples).

- [What it does](#what-it-does)
- [Running it](#running-it)
- [The agents](#the-agents)
- [The learning pipeline](#the-learning-pipeline)
- [The generation pipeline](#the-generation-pipeline)
- [What it shows about the harness](#what-it-shows-about-the-harness)
- [Limits](#limits)

## What it does

- **Learn** a template from several documents of one category. Structure, standard wording and the values that change
  come from the whole set, not from one document.
- **Generate** a new document from the learned template and the facts of a new case (a *case* is one new document and
  its facts). Standard wording is copied; only the case-specific parts are written.
- **Check** both. The learned template is verified against its sources, and every generated document is checked before
  it is accepted.

Four rules shaped it:
1. The model does only what needs a model: reading text into structure, judging whether two names mean the same, and
   writing the parts that vary. Everything else is code.
2. Agents are small and know nothing about each other. The pipeline functions own the order, the hand-offs and what a
   failure means.
3. Every model output is checked in code before it is used.
4. Every pipeline is a job, so every agent input and output of a run is in the run log.

## Running it

```
cp .env.example .env     # set GOOGLE_API_KEY
make build
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"
```

Without Docker: `uv sync`, then `PYTHONPATH=src:examples/doclearn uv run python -m doclearn.cli examples/doclearn/samples/project_proposals --category project_proposals`.

It learns from the documents, shows what it learned (the tree, the template, the inputs, and how well each source
document conforms to the tree), then fills the form: **manual** opens the inputs in `$EDITOR`, **auto** has an agent
invent a case and a story, which you accept, edit or redo. Last it generates the document. `--fill manual|auto` skips
the question and `-v` shows every log line. Everything is saved in `runs/<time>_<category>/` (`run.log`, `learned/`,
`facts.json`, `story.txt`, `generated.md`, `summary.md`) and in `micro.db`. Exit codes: 0 done, 1 failed, 2 stopped
before generating.

## The agents

| Agent | Model | Calls | Task |
|---|---|---|---|
| Document Analyzer ([analyzer.py](https://github.com/aadhithya/micro/blob/main/examples/doclearn/doclearn/analyzer.py)) | `gemini-3.8-flash`, low | one per document, in parallel | Read a document into typed blocks, mark the case-specific values as `{{spans}}`, record the markdown style of each element type |
| Reconciliation Agent ([reconciler.py](https://github.com/aadhithya/micro/blob/main/examples/doclearn/doclearn/reconciler.py)) | `gemini-3.8-flash`, medium | one for the set | Give every element type and field name one standard name, because documents name the same thing differently |
| Semantic Alignment ([alignment.py](https://github.com/aadhithya/micro/blob/main/examples/doclearn/doclearn/alignment.py)) | `gemini-3.8-flash`, low | one per section, in parallel | Learn one subtree from the same section of every document: which blocks are fixed, which are slots, which are generated, which are conditional |
| Generator ([generation.py](https://github.com/aadhithya/micro/blob/main/examples/doclearn/doclearn/generation.py)) | `gemini-3.8-flash`, low | one, with tool rounds | Write the document from the compiled tree, the facts and optional guidance. Tools: `calculate`, the date tools, `check_document` |
| Form Filler ([autofill.py](https://github.com/aadhithya/micro/blob/main/examples/doclearn/doclearn/autofill.py)) | `gemini-3.5-flash-lite`, low | one, retried on problems | Invent a coherent case for a learned template, for demos and tests |

## The learning pipeline

Documents in, a learned template out. Three artifacts are saved together, tagged with the category:

- **Learned tree:** the document as a tree of blocks. Each has a `type`, the standard `text` (with `{{field}}`
  placeholders), and a `kind`: `fixed` (the same in every document, copied verbatim), `slot` (the same sentence with
  placeholders) or `generated` (varies with the case, written each time). It can have `alternatives` (other wordings),
  `conditions` (the block is included only if they hold) and `children`.
- **Template:** the element types with their markdown patterns.
- **Facts schema:** the inputs a user has to give, derived in code from the tree.

The steps: the Analyzer reads each document (in parallel); the Reconciler gives names one standard form; code splits any
merge that would give one field two values in a document; code matches the sections of the documents by their heading
text; Semantic Alignment learns one subtree per section (in parallel); code assembles the tree, checks it and derives the
facts schema; and last, **each source document is parsed against the tree** and must be at least 80% reproduced, or the
job fails and nothing is saved.

Where the model was unreliable, there is code:
- **Flat output, tree in code.** Nested schemas came back silently truncated, so agents return flat lists with parent
  ids and code builds the tree.
- **Contexts, not values, for the Reconciler.** Shown only values, it merged a subtotal into an item that happened to
  have the same amount. Each name is shown with the line it is used in.
- **The agent decides one thing per block, whether it is `generated`.** Code derives the rest of the `kind` from the
  text; asked for the kind directly, the model contradicted its own text.
- **An output check on the alignment:** the subtree must have at least half the blocks of the largest input section, so
  a reply with only a root is rejected and retried.

| What goes wrong | What happens |
|---|---|
| A model call fails or returns invalid output | The runner retries (3 attempts); auth and config errors fail fast |
| One document cannot be analyzed | It is skipped and reported; the rest carry on. None left: the job fails |
| One section cannot be aligned | The job fails: a hole in the tree is worse than no tree |
| Documents do not fit the tree (below 80%) | The job fails and nothing is saved |
| Any failure | The job is marked FAILED with the error, and every agent output stays in the trace |

## The generation pipeline

Code first: every given fact must be a field of the tree and every required input must have a value (or it raises before
any model call), and blocks whose conditions do not hold are cut out, so the agent never sees a condition. Then the
Generator copies `fixed` blocks (with small grammatical adaptations), fills `slot` blocks with the facts exactly as given,
writes `generated` blocks in the style of the example from the facts only, and computes totals with `calculate`.

**`check_document` is the only way to hand the document in.** It is a tool that runs code, not a model: no `{{placeholder}}`
is left, every `fixed` and `slot` block is present and within 85% similarity of one of its wordings, the blocks are in
the tree's order, and every fact appears exactly as given. If it passes, the document is stored as accepted. If it fails,
the problems go back to the agent. After the third failed check the document is accepted as it is and the remaining
problems are returned in the result.

## What it shows about the harness

- A pipeline is an ordinary async function, `learn_document_tree(runner, store, documents, category, job_id=...)`. It
  calls `runner.run` and `runner.run_many`, runs code between the calls, and is wrapped in `run_job`.
- Typed agents make hand-offs cheap: the output model of one agent is the input of a function, which builds the input
  model of the next.
- Retries and `check_output` absorb an occasional bad reply; pipeline code decides what is fatal (one section failing)
  and what is not (one document failing).
- The trace is what makes development bearable: the prompts were improved by reading the `agent_runs` rows of failed
  attempts, not by guessing.

To build your own: define the agents, write the pipeline as an async function of `(runner, ..., job_id)`, run it with
`run_job`, and test it with a mocked model (`tests/fakes.py` has a scripted fake chat model, and `tests/builders.py`
shows how to script a whole pipeline). Add a few real-model tests on small fixtures for what mocks cannot show.

## Limits

This is a prototype, and these were left out on purpose, to keep the scope small.

- **No evaluation.** The tests show it works; there is no held-out set, prompt comparison or quality metric. The
  conformance score is computed against the Analyzer's own reading of each source, and a `generated` block accepts any
  text of its type, so it measures self-consistency, not quality.
- **No personal-data handling.** Documents go to the model as they are and are kept in plaintext in the run log. The
  examples are invented; do not run it on real client documents.
- **Generation is lenient.** Fixed text is checked by similarity (85%), so a small but meaningful edit, such as a
  changed negation, can pass; rendering fixed blocks in code is what a product should do. Totals are computed by the
  model with `calculate` and are not re-checked. `generated` blocks are not verified, only present and in order.
- **Few, very different documents** can be learned as optional slots instead of a `generated` block. Required inputs
  follow the examples. Repeated items are not modelled as lists.
- **Gemini only,** one language per category, markdown in and out. Learning from a user's edits, detecting documents
  unlike the examples, and resuming a failed job are designed but not built.
