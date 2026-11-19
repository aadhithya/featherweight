# doclearn

A complete pipeline built on the harness: learn the template of a document family from a few examples, then write a new
document from a few facts. It has two pipelines and five agents, with code checks between them. It is not specific to any
domain.

```
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"
```

Without Docker: `PYTHONPATH=src:examples/doclearn uv run python -m doclearn.cli examples/doclearn/samples/project_proposals --category project_proposals`.

It learns from the documents, shows what it learned, fills a form (by hand, or an agent invents a case) and writes a new
document. Everything is saved in `runs/<time>_<category>/` and in `micro.db`. Add `--fill manual` or `--fill auto` to
skip the question, and `-v` to see every log line.

## What is here

| | |
|---|---|
| [`doclearn/`](doclearn) | The package: the agents, the pipelines and the command line |
| [`samples/`](samples) | Two document families to learn from: `project_proposals` and `incident_reports` (invented) |

It has no tests of its own: it is an example, and `make pipeline` runs it. The harness's tests check that its agents build.

## Where to look

How the agents and the two pipelines work, and the limits of this example, are in
[docs/example-pipeline.md](../../docs/example-pipeline.md). To see how a pipeline is built on the harness, read
[`doclearn/pipeline.py`](doclearn/pipeline.py) and [`doclearn/learning.py`](doclearn/learning.py), and
[`doclearn/generation.py`](doclearn/generation.py) for an agent with a check tool.
