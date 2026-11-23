# Attachments

Status: implemented, in [epic #170](https://github.com/aadhithya/featherweight/issues/170): the converters and what `ChatInput` refuses (#171), the limits (#172), the provider tests (#173), the adapters (#174), and the guide (#175). It changes [streaming agents](streaming.md) (`ChatInput`, `to_langchain`, `to_agui`, the Vercel adapter), and nothing else.

- [Goal](#goal)
- [What a client can send](#what-a-client-can-send)
- [What the model is given](#what-the-model-is-given)
- [Limits](#limits)
- [Failures](#failures)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Findings](#findings)
- [Tests](#tests)

## Goal

A person in a chat attaches an image, a PDF, a recording or a video to a message, and the model reads it. Today `ChatInput` refuses
every part that is not text ([streaming](streaming.md#messages-and-keeping-the-conversation)).

**featherweight is the transport.** It takes what the client sent and hands it to the LangChain model underneath, in LangChain's
provider-independent content blocks. It does not check what the model can read, it does not download, upload, convert, resize or
store anything. Whether an attachment suits the model is the application's responsibility, and the provider says so when it does not.
The application decides how files reach it: uploads, a store, fetching, scanning.

## What a client can send

AG-UI 1.0 messages from a user can hold `text`, `image`, `audio`, `video` and `document` parts. Each media part has a `source`:

| Source | Meaning | Here |
|---|---|---|
| `data` | the bytes, base64-encoded, with a required `mime_type` | accepted |
| `url` | a URL, "fetched by whoever needs them" | **refused** |

A `url` source is refused because nothing in featherweight should fetch a URL that a client chose, and because what happens to one
depends on the provider: `langchain-google-genai` downloads an `http(s)` URL in the server process, `langchain-anthropic` and
`langchain-openai` pass it to the provider. An application that wants URLs fetches them itself, checks them as it sees fit, and sends
the bytes as a `data` source. We offer no hook for that: how to fetch safely is the application's decision.

The legacy `binary` part of AG-UI 0.x is retired in 1.0 and is not supported.

## What the model is given

`to_langchain` turns each part into a LangChain standard content block, and the provider's package turns that into its own request:

| AG-UI part | LangChain block |
|---|---|
| `image` | `{"type": "image", "base64": ..., "mime_type": ...}` |
| `document` | `{"type": "file", "base64": ..., "mime_type": ..., "filename": ...}` |
| `audio` | `{"type": "audio", "base64": ..., "mime_type": ...}` |
| `video` | `{"type": "video", "base64": ..., "mime_type": ...}` |

- **`filename`** comes from the part's `metadata["filename"]` when there is one. OpenAI wants a name for an inline file, and
  LangChain otherwise puts in a placeholder and warns. We never invent one.
- Parts of a message keep their order, and the text parts around them.
- **`to_agui` is the inverse,** so a stored conversation with an attachment survives `from_langchain` and back.
- **An assistant, tool or system message** carries no attachments; they stay text.
- **Provider-side files** (an uploaded file's `file_id`) cannot be said in AG-UI. An application that uses them builds its own
  LangChain message with a `{"type": "file", "file_id": ...}` block. Nothing here blocks that, and nothing here helps with it.

## Limits

Every turn sends the whole conversation, so an attachment in an earlier message is sent again and counts again. The limits apply to
**all the attachments of the conversation**, not to the last message.

| Limit | Default | Counts |
|---|---|---|
| `max_attachments` | 10 | the media parts of all messages |
| `max_bytes` | 15 MB | the decoded size of all of them |

The application sets them on the agent, with a class attribute, and the runner checks them when a turn starts.

**Where the defaults come from.** The size is the highest value that Anthropic, Google and OpenAI all accept (cautiously read); the count is our own choice. The providers' figures are in [what the providers accept](../attachments-providers.md)
(read it with its warning: it is a dated snapshot, and the providers' pages are the source):

| Provider | Count per request | Size of a request |
|---|---|---|
| Anthropic | 100 images (600 for some models) | 128 MB |
| Google | no count stated for documents; 3,600 images (unconfirmed) | 100 MB inline, 50 MB for PDFs; one page still says 20 MB |
| OpenAI | 1,500 images; none stated for files | 512 MB of images; **50 MB for all files together** |

The default count is **10**, a design choice, not a provider's limit: every provider accepts more (the lowest is Anthropic's 100 images), but a chat
that takes 100 files in one go is a different job, with its own pipeline (batch, jobs, a store). 10 is also what the Gemini **app** allows.
An application that needs more raises `max_attachments`. For size the pages disagree: the lowest figure on any of them is Google's 20 MB, the next is OpenAI's 50 MB
for files. Base64 makes data a third larger, so 15 MB of decoded bytes stays under 20 MB on the wire, with room for the text. The default is the
cautious one, and an application that has checked its provider raises it.
Anthropic also applies a stricter pixel limit past 20 images or documents: that is the provider's to report.
These are **ceilings against a request that cannot work, not a promise** that every request under them does: a provider also limits
pages, pixels and the type of each file, and says so in its error.

## Failures

Consistent with the rest of featherweight: **a thing that is wrong with the input is raised; a thing that goes wrong in the run is a
result.**

- **Raised, before any run exists:** a `url` source; a part type that is not supported; data that is not base64; a missing mime type;
  more attachments or bytes than the agent allows. Nothing is started, and nothing is written to the run log.
- **A result:** everything the provider says no to (a type the model cannot read, a file that is too large for it, too many pages).
  The turn ends `FAILED` with the provider's error, an error event is sent, and **nothing is retried**, as for any streaming agent.

## Decisions

- **Transport only.** No capability checks, no conversion, no fetching, no uploads. The application knows its model and its users.
- **`data` only.** Safe by construction, and the one source that every provider's package accepts.
- **All four media types.** They map in the same way, so supporting two and refusing two would add code and not remove any.
- **Limits are on the agent,** with defaults at the highest value all three big providers accept, so that most applications need no setting.
- **The run log keeps no content.** It holds no message text today, and holds no attachment now: not the bytes, not the name.

## Not supported

- **Fetching URLs,** uploading to a provider's store, and `file_id` sources: the application's.
- **Converting** a PDF into page images, resizing, transcribing or OCR, for a model that cannot read the original.
- **Checking** that a model can read a type, or the provider's own limits (pages, pixels, duration).
- **Attachments on an assistant or tool message,** and in sub-agents' inputs.
- **Stored attachments.** The application keeps them, as it keeps the conversation.

## Findings

Checked while writing this (2026-11-22):

- `ag-ui-protocol` 1.0.0: `ContentPart` is text, image, audio, video or document; a source is `data` (`value`, `mime_type` required) or
  `url`; there is no source for a provider's file id. `BinaryInputContent` is kept only for importing.
- `langchain-core` 1.8.3 has standard blocks `image`, `audio`, `video`, `file` and `text-plain`, each with `base64`, `url`, `file_id`
  and `mime_type`. The three provider packages here translate them.
- **Anthropic** (`langchain-anthropic` 1.7.11): base64 and URL for images and documents, and `file_id`.
- **Google** (`langchain-google-genai` 4.4.2): base64 for all types; `gs://`, YouTube and Google-hosted URLs go through; **any other
  `http(s)` URL is downloaded by our process** and inlined.
- **OpenAI** (`langchain-openai` 1.8.2): base64 images; PDFs inline as `file_data`, on Chat Completions (PDF only) and Responses (more
  types); audio inline; `file_url` for files on Responses only; `file_id`.
- **Ollama** takes images only, as base64 in an `images` array, on vision models; no PDFs. featherweight has no Ollama provider, and
  `langchain-ollama` was not checked: an application that adds one (see [models](models.md)) finds out which blocks it accepts.
- **What the LangChain packages do with our blocks** (checked offline, nothing sent, [tests](https://github.com/aadhithya/featherweight/blob/main/tests/test_attachment_providers.py)):
  Anthropic sends images and PDFs as base64 blocks and **raises for audio and video** while it builds the request; OpenAI Chat Completions sends images, PDFs
  (a `LC_AUTOGENERATED` name and a warning without a `filename`) and audio, and raises for video; OpenAI Responses sends images and files, raises for video, and
  **drops audio without any error**, so the model sees only the text; Google sends every block inline. Only OpenAI gets the filename. A text file passes
  through Anthropic and Chat Completions, although Chat Completions takes only PDFs. This is the packages' behaviour on the dates above and it changes.
- **Observers:** an observer that captures content (`capture_content=True`, and OpenTelemetry with content capture on) is given the attachment bytes with
  the rest of the messages. That is the documented opt-in. By default no event, run log row, observer record or span holds any of it.
- **The Vercel adapter** read only the text of a user message and dropped its `file` parts, so an attachment was lost. It now converts a `file` part with
  a `data:...;base64,` URL into an image, audio, video or document part by its `mediaType`, and refuses any other URL with a `BadRequest`. The AG-UI adapter
  needed no change.
- What each provider accepts, with the limits, is in [its own page](../attachments-providers.md), with a warning that it is a snapshot. It
  changes: Google raised its inline limit from 20 MB to 100 MB in January 2026, and its pages still disagree. Re-read the providers' pages before
  the numbers in [Limits](#limits) are fixed, and again from time to time: **updating that page is part of each release.**

## Tests

Never a real model, and no network.

- **The conversion:** each part type to its block and back; the order of parts; `filename`; a message with only attachments.
- **Refusals:** a `url` source, bad base64, a missing mime type, an unsupported part, a limit that is exceeded by count, by bytes, and by
  the attachments of earlier messages; the agent's own limits over the defaults.
- **The provider's side, offline:** build the request that each of the three packages would send for each block, and check its shape
  without sending it.
- **A fake model** that fails on a block: the run is `FAILED`, the error event is sent, and there is no retry.
