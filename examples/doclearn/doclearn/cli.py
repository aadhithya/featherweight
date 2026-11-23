"""Command line: from a folder of example documents to a new document, in one run.

    python -m doclearn.cli FOLDER --category NAME [--fill manual|auto] [--out-dir out] [-v]

It learns the template from the documents, shows what it learned (the tree, the template, the inputs, and how well the
documents conform to the tree), then fills the form: `manual` opens the inputs in your editor (`$VISUAL`/`$EDITOR`; with
none set it prints the path and waits for you to edit the file), `auto` has an agent invent a case and a story, which you
can then accept, edit or redo. Last it generates the document. Without `--fill` it asks.

The terminal shows high-level progress only; `-v` also shows every log line. Everything is saved in an output folder
`<out-dir>/<time>_<category>/`: the log, the learned tree, template, inputs and conformance (`learned/`), the form
(`facts.json`), the story (`story.txt`), the document (`generated.md`) and what the terminal showed (`summary.md`).
Needs GOOGLE_API_KEY in the environment. Results are also in the database (`FTW_DB_PATH`, default featherweight.db);
`make pipeline ARGS="..."` runs it in the container. Exit codes: 0 done, 1 failed, 2 stopped before generating.
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from doclearn.autofill import AutofillError, FilledForm, check_form, fill_form
from doclearn.generation import GeneratedDocument, GenerationError, generate_document
from doclearn.models import Document, FactsSchema, FieldValue
from doclearn.pipeline import LearningResult, learn_document_tree
from doclearn.report import GUIDANCE_KEY, form_file_content, overview, write_learned_files
from doclearn.store import LearnedDocument, LearnedStore
from featherweight import AgentRunner, JobContext, SqliteDatabase, Status, default_db_path, job, run_job

DONE, FAILED, STOPPED = 0, 1, 2
DEFAULT_OUT_DIR = "out"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_HANDLER_FLAG = "_doclearn_cli_handler"


class CliError(Exception):
    """A problem the user can fix; printed without a traceback."""


class Stopped(Exception):
    """The user chose to stop before generating."""


# the terminal (replaced in tests)


def open_editor(path: Path) -> None:
    """Edit a file in `$VISUAL`/`$EDITOR`; with none set, ask the user to edit it by hand."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor), str(path)], check=False)
    else:
        input(f"  No $EDITOR set. Edit {path}, save it, then press Enter ")


def read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise CliError("There is no terminal to ask in: give --fill manual or --fill auto") from None


class Console:
    """What the command line says and asks. Everything said is kept for `summary.md`."""

    def __init__(self, ask: Callable[[str], str] = read_line, edit: Callable[[Path], None] = open_editor):
        self.ask, self.edit = ask, edit
        self.said: list[str] = []

    def say(self, text: str = "") -> None:
        self.said.append(text)
        print(text, flush=True)

    def step(self, text: str) -> None:
        self.say(f"  ▸ {text} ...")

    def choose(self, prompt: str, options: Mapping[str, str]) -> str:
        """Ask until the answer is one of the options, by its letter or its first word; return its letter."""
        menu = " / ".join(f"[{key}]{label[1:]}" for key, label in options.items())
        while True:
            answer = self.ask(f"{prompt} {menu}: ").strip().lower()
            for key, label in options.items():
                if answer in (key, label.split()[0].rstrip(",")):
                    return key
            self.say(f"  Please answer one of: {', '.join(options)}")


# helpers (no model, no database: tested on their own)


def read_documents(folder: Path) -> list[Document]:
    """Every `*.md` file of the folder, in name order; the id of a document is its file name without the suffix."""
    paths = sorted(Path(folder).glob("*.md"))
    if not paths:
        raise CliError(f"No .md documents in {folder}")
    return [Document(document_id=p.stem, text=p.read_text(encoding="utf-8")) for p in paths]


def read_form(path: Path) -> tuple[dict[str, str], str]:
    """The values and the story of a form file. Keys starting with `_` are notes, except `_guidance`, the story.
    Empty values count as not provided."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError(f"{path} must be a JSON object of input name to value")
    facts = {k: str(v).strip() for k, v in data.items() if not k.startswith("_") and v is not None and str(v).strip()}
    return facts, str(data.get(GUIDANCE_KEY) or "").strip()


def form_problems(schema: FactsSchema, facts: Mapping[str, str]) -> list[str]:
    return check_form(schema.fields, [FieldValue(name=n, value=v) for n, v in facts.items()])


def make_output_dir(out_dir: Path, category: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = Path(out_dir) / f"{stamp}_{category}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def setup_logging(output_dir: Path, verbose: bool) -> Callable[[], None]:
    """Log everything of `featherweight` and `doclearn` to `doclearn.log`, and to the terminal too if `verbose`. Returns the function
    that removes the handlers again."""
    handlers: list[logging.Handler] = [logging.FileHandler(output_dir / "doclearn.log", encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    loggers = [logging.getLogger("featherweight"), logging.getLogger("doclearn")]
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, LOG_HANDLER_FLAG, True)
    previous = [(logger, logger.level, logger.propagate) for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.propagate = False  # nothing reaches the root logger: no stray warnings on the terminal
        for handler in handlers:
            logger.addHandler(handler)

    def undo() -> None:
        for logger, level, propagate in previous:
            for handler in handlers:
                logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
        for handler in handlers:
            handler.close()

    return undo


# the steps


class LearnInput(BaseModel):
    documents: list[Document]
    category: str


def learn_job(store: LearnedStore, console: Console):
    """The learning job. It is declared here, in a function, because it needs the store and the terminal; its input is the data."""

    @job(name="learn")
    async def learn(ctx: JobContext, input: LearnInput) -> LearningResult:
        return await learn_document_tree(
            ctx.runner, store, input.documents, input.category, job_id=ctx.job_id, progress=console.step
        )

    return learn


async def learn(
    runner: AgentRunner, store: LearnedStore, documents: list[Document], category: str, console: Console
) -> tuple[LearnedDocument, LearningResult]:
    outcome = await run_job(runner, learn_job(store, console), LearnInput(documents=documents, category=category))
    if outcome.status != Status.SUCCESS or outcome.result is None:
        raise CliError(f"Learning failed: {outcome.error} (job {outcome.job_id}; the agent runs are in the database)")
    learned = await store.get(outcome.result.learned_id)
    assert learned is not None
    return learned, outcome.result


def edit_form(console: Console, path: Path, schema: FactsSchema, facts: Mapping[str, str], story: str) -> FilledForm:
    """Let the user fill the form in the editor until it is complete and valid (or they stop)."""
    path.write_text(form_file_content(schema, facts, story), encoding="utf-8")
    while True:
        console.edit(path)
        try:
            new_facts, new_story = read_form(path)
            problems = form_problems(schema, new_facts)
        except CliError as exc:
            problems = [str(exc)]
        if not problems:
            return FilledForm(facts=new_facts, story=new_story)
        console.say("  The form has problems:")
        for problem in problems:
            console.say(f"    - {problem}")
        if console.choose("  Edit it again?", {"y": "yes", "n": "no, stop"}) == "n":
            raise Stopped


def show_form(console: Console, form: FilledForm, schema: FactsSchema) -> None:
    console.say("Form:")
    names = {f.name for f in schema.fields}
    for field in schema.fields:
        if field.name in form.facts:
            console.say(f"  {field.name}: {form.facts[field.name]}")
    for name in sorted(set(form.facts) - names):
        console.say(f"  {name}: {form.facts[name]}")
    console.say(f"Story:\n  {form.story}")


class FillInput(BaseModel):
    learned: LearnedDocument


class GenerateInput(BaseModel):
    learned: LearnedDocument
    form: FilledForm


def fill_job(console: Console):
    @job(name="autofill")
    async def autofill(ctx: JobContext, input: FillInput) -> FilledForm:
        return await fill_form(ctx.runner, input.learned, job_id=ctx.job_id, progress=console.step)

    return autofill


def generate_job(console: Console):
    @job(name="generate")
    async def generate_a_document(ctx: JobContext, input: GenerateInput) -> GeneratedDocument:
        form = input.form
        return await generate_document(
            ctx.runner, input.learned, form.facts, form.story, job_id=ctx.job_id, progress=console.step
        )

    return generate_a_document


async def auto_form(runner: AgentRunner, learned: LearnedDocument, console: Console, path: Path) -> FilledForm:
    """Have the agent invent a case; the user accepts it, edits it or has it redone."""
    schema = learned.facts_schema
    while True:
        outcome = await run_job(runner, fill_job(console), FillInput(learned=learned))
        if outcome.status != Status.SUCCESS or outcome.result is None:
            raise CliError(f"Filling the form failed: {outcome.error} (job {outcome.job_id})")
        form = outcome.result
        while True:
            path.write_text(form_file_content(schema, form.facts, form.story), encoding="utf-8")
            show_form(console, form, schema)
            choice = console.choose("Generate with this?", {"g": "generate", "e": "edit", "r": "redo", "q": "quit"})
            if choice == "g":
                return form
            if choice == "q":
                raise Stopped
            if choice == "r":
                break
            form = edit_form(console, path, schema, form.facts, form.story)


async def generate(
    runner: AgentRunner, learned: LearnedDocument, form: FilledForm, console: Console
) -> GeneratedDocument:
    outcome = await run_job(runner, generate_job(console), GenerateInput(learned=learned, form=form))
    if outcome.status != Status.SUCCESS or outcome.result is None:
        raise CliError(f"Generation failed: {outcome.error} (job {outcome.job_id})")
    return outcome.result


def report(console: Console, document: GeneratedDocument, path: Path) -> None:
    path.write_text(
        document.markdown if document.markdown.endswith("\n") else document.markdown + "\n", encoding="utf-8"
    )
    console.say("Document written.")
    if document.notes:
        console.say("Notes from the writer:")
        for note in document.notes:
            console.say(f"  - {note}")
    if document.problems:
        console.say("Problems the check still found in the document (not fixed by the writer):")
        for problem in document.problems:
            console.say(f"  - {problem}")
    if document.not_provided:
        console.say(f"Optional inputs without a value: {', '.join(document.not_provided)}")
    if document.unused_inputs:
        console.say(
            f"Inputs whose value is not in the document (no place for them in the template): {', '.join(document.unused_inputs)}"
        )


# the command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m doclearn.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="folder with the example documents (*.md)")
    parser.add_argument("--category", required=True, help="document category, e.g. project_proposals")
    parser.add_argument("--fill", choices=["manual", "auto"], help="how to fill the form; asked if not given")
    parser.add_argument("--out-dir", type=Path, default=Path(DEFAULT_OUT_DIR), help="where the output folders go")
    parser.add_argument("-v", "--verbose", action="store_true", help="also show every log line")
    return parser


async def run(args: argparse.Namespace, console: Console, output_dir: Path) -> None:
    category = args.category
    documents = read_documents(args.folder)
    started = time.monotonic()
    console.say(f"Learning '{category}' from {len(documents)} documents in {args.folder}")
    async with SqliteDatabase(default_db_path()) as db:
        store = await LearnedStore.open(db)
        runner = AgentRunner(db)

        learned, result = await learn(runner, store, documents, category, console)
        console.say(f"  ✓ Learned in {time.monotonic() - started:.0f}s\n")
        write_learned_files(output_dir / "learned", learned, result)
        console.say(overview(learned, result))
        console.say(f"\nSaved in {output_dir / 'learned'}\n")

        schema = learned.facts_schema
        mode = (
            args.fill[0]
            if args.fill
            else console.choose(
                "Fill the form", {"m": "manual (in your editor)", "a": "auto (an agent invents a case)", "q": "quit"}
            )
        )
        if mode == "q":
            raise Stopped
        form_path = output_dir / "facts.json"
        if mode == "m":
            form = edit_form(console, form_path, schema, {}, "")
        else:
            form = await auto_form(runner, learned, console, form_path)
        form_path.write_text(form_file_content(schema, form.facts, form.story), encoding="utf-8")
        (output_dir / "story.txt").write_text(form.story + "\n", encoding="utf-8")

        document = await generate(runner, learned, form, console)
        report(console, document, output_dir / "generated.md")
        console.say(f"\nOutput folder: {output_dir}")


def main(argv: Sequence[str] | None = None, console: Console | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = console or Console()
    output_dir: Path | None = None
    undo_logging = None
    try:
        output_dir = make_output_dir(args.out_dir, args.category)
        undo_logging = setup_logging(output_dir, args.verbose)
        asyncio.run(run(args, console, output_dir))
        return DONE
    except Stopped:
        console.say("Stopped before generating.")
        return STOPPED
    except (CliError, GenerationError, AutofillError) as exc:
        print(str(exc), file=sys.stderr)
        return FAILED
    finally:
        if undo_logging:
            undo_logging()
        if output_dir is not None and console.said:
            (output_dir / "summary.md").write_text("\n".join(console.said) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
