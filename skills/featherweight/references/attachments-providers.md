<!-- Copied from docs/attachments-providers.md by scripts/build_skill.py. Edit the original, not this file. -->

# What the providers accept as attachments

> **A snapshot, as of 2026-11-22.** Providers change their limits often and without notice, and this page is not actively maintained. Before you build on a type, a size or a count, read the provider's page (linked in each section) and try it with your model. Where two pages of a provider disagree, this page says so.

featherweight does not check any of this: it passes attachments to the model and the provider's answer comes back as a failed run
([attachments](https://github.com/aadhithya/featherweight/blob/main/docs/specs/attachments.md)). This page is for choosing what to build on.

- [At a glance](#at-a-glance)
- [Anthropic](#anthropic)
- [Google (Gemini API)](#google-gemini-api)
- [OpenAI](#openai)
- [Ollama](#ollama)
- [A file returned by a tool](#a-file-returned-by-a-tool)
- [How we read it](#how-we-read-it)

## At a glance

"Inline" means the bytes travel in the request, as base64. "Stored" means you upload the file to the provider first and send its ID.

| | Anthropic | Google (Gemini API) | OpenAI | Ollama |
|---|---|---|---|---|
| **Images** | inline, URL, stored | inline, URL, stored | inline, URL, stored | inline, vision models only |
| **PDFs** | inline, URL, stored | inline, URL, stored | inline, stored; URL on Responses only | not stated |
| **Other documents** | text files; Office files are not accepted | text, HTML, CSV, JSON and more (Office types are mentioned, unconfirmed) | on Responses only: text, code, Office, spreadsheets | not stated |
| **Audio** | not stated as accepted | inline, stored | `input_audio` on Chat Completions | not stated |
| **Video** | not stated as accepted | inline, stored | not stated | not stated |
| **Most in one request** | 100 images (600 for some models); **past 20 images or documents a stricter pixel limit applies** | no count stated for documents; 3,600 images (unconfirmed) | 1,500 images; no count stated for files | not stated |
| **Size of a request** | 128 MB (first-party API) | 100 MB inline, 50 MB for PDFs (one page says 20 MB) | 512 MB for images; **50 MB for all files together** | not stated |

## Anthropic

Pages: [PDF support](https://platform.claude.com/docs/en/build-with-claude/pdf-support), [vision](https://platform.claude.com/docs/en/build-with-claude/vision), [the Files API](https://platform.claude.com/docs/en/build-with-claude/files).

- **How:** an `image` or `document` content block with a source: base64, a URL, or a `file_id` from the Files API. On Amazon Bedrock and on
  Google Cloud, only base64 sources are available.
- **Images:** JPEG, PNG, GIF and WebP (an animation uses its first frame). At most 8000 x 8000 px and 10 MB each, base64-encoded (5 MB on Bedrock).
- **PDFs:** 128 MB per request on the first-party API, and 600 pages (100 when the model's context window is under 1M tokens). No passwords.
- **Count:** 100 images per request for models with a 200k-token context window, 600 for the others. **Above 20 images or document
  blocks, a stricter limit applies**: each image must fit within 3,000 px, or the request must stay at 20 or fewer.
- **Other documents:** `text/plain` in a `document` block. `.docx` and `.xlsx` are not accepted there: convert them first.
- **Audio and video:** not described as input on the pages read.
- **The Files API:** upload once, send the ID. 500 MB per file, 1 TB per organization, kept until you delete it or it expires (optionally 1 hour
  to 90 days). The beta header is no longer needed. Not eligible for zero data retention.

## Google (Gemini API)

Pages: [file input methods](https://ai.google.dev/gemini-api/docs/generate-content/file-input-methods), [document understanding](https://ai.google.dev/gemini-api/docs/document-processing), [image understanding](https://ai.google.dev/gemini-api/docs/image-understanding), and [the announcement of the new limits](https://blog.google/innovation-and-ai/technology/developers-tools/gemini-api-new-file-limits/) (2026-01-12).

- **How:** inline bytes, a file uploaded through the Files API (kept 48 hours), a URL, or a Google Cloud Storage object that you register.
- **Size:** 100 MB per request inline, 50 MB for PDFs (the January 2026 announcement raised it from 20 MB). **The image page still says 20 MB for the
  whole request.** Files API: 2 GB per file, 20 GB per project. A URL that Gemini fetches itself: 15 MB per file.
- **PDFs:** up to 1,000 pages, as far as the context window allows.
- **Types:** images (BMP, JPEG, PNG, WebP; one page also lists HEIC and HEIF), audio (WAV, MP3, AIFF, AAC, OGG, FLAC and more), video (MP4, MPEG,
  MOV, AVI, WebM and more), documents (PDF, text, HTML, CSS, CSV, JSON, RTF, XML). The page calls its own list "initial guidance, not comprehensive".
- **Count:** no per-request count of files is stated for documents, audio or video. An unread part of the image page is reported to say 3,600
  images per request. **The Gemini app (not the API) limits a prompt to 10 files**, which is probably where "10" comes from.
- **Note on LangChain:** with `langchain-google-genai`, an `http(s)` URL that is not Google's own is **downloaded by your process** and sent
  inline, not fetched by Gemini ([attachments](https://github.com/aadhithya/featherweight/blob/main/docs/specs/attachments.md#what-a-client-can-send)).

## OpenAI

Pages: [file inputs](https://developers.openai.com/api/docs/guides/file-inputs), [images and vision](https://developers.openai.com/api/docs/guides/images-vision), [audio in Chat Completions](https://developers.openai.com/api/docs/guides/audio-chat-completions).

- **Chat Completions and Responses differ.**
  - Chat Completions accepts only PDFs as files, inline (`file_data`) or by `file_id`. No `file_url`.
  - Responses accepts more: text and code, Office documents, presentations, spreadsheets, and `file_url`.
- **Files:** each under 50 MB, and **50 MB combined for all files of the request**. No count stated. For a PDF the API reads the text and the page images;
  for other files, text only.
- **Images:** PNG, JPEG, WebP and non-animated GIF, by URL, as a base64 data URL, or by `file_id`. Up to 1,500 per request and 512 MB in all. A size per
  image is not stated; pixel and "patch" limits apply, and an image over the limit is rejected, not resized.
- **Audio:** an `input_audio` part with base64 data and a format, documented for Chat Completions only. Limits not stated.
- **Video:** not stated.

## Ollama

Page: [vision](https://docs.ollama.com/capabilities/vision).

- **How:** an `images` array on a message of `POST /api/chat`; the REST API expects base64 (its SDKs also take paths, URLs or bytes).
- **Models:** vision models only. A model without vision refuses the field.
- **PDFs, audio, video, a count of images:** not stated. The usual workaround is to turn each PDF page into an image first.
- featherweight has no Ollama provider, and `langchain-ollama` was not read.

## A file returned by a tool

An agent or a job does not carry files ([attachments](https://github.com/aadhithya/featherweight/blob/main/docs/specs/attachments.md#files-for-agents-and-jobs)): it gets a reference, and a tool fetches the file. A tool can return the text of the
file, which every provider takes, or an image or a file block. **Checked once on 2026-11-25** (real calls, the smallest model of each provider in featherweight's settings,
one small file each: a 200 px JPEG and a one-page PDF; the model had to answer from the file):

| Provider (model) | Image returned by a tool | PDF returned by a tool |
|---|---|---|
| Anthropic (`claude-haiku-5-5`) | read | read |
| Google, Gemini (`gemini-3.5-flash-lite`) | read | read |
| OpenAI (`gpt-6-luna`, Responses API) | read | read |
| Google, Gemma (`gemma-4-26b-a4b-it`) | not known: it did not call the tool | not known |

It is a snapshot from one run each, with the same warning as the rest of this page: try your provider, your model and your type of file. OpenAI's Chat Completions API was not tried (featherweight's `OpenAI` settings build the Responses API). [The recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/files-for-an-agent-or-a-job.md) shows the tool.

## What the LangChain packages do with it

featherweight gives the model LangChain content blocks, and these packages turn them into the provider's request. We built the request offline for each
block type (nothing was sent), with `langchain-anthropic` 1.7.11, `langchain-google-genai` 4.4.2 and `langchain-openai` 1.8.2. This is what they did on
2026-11-22, and it changes with their versions:

| Block | `langchain-anthropic` | `langchain-openai`, Chat Completions | `langchain-openai`, Responses | `langchain-google-genai` |
|---|---|---|---|---|
| image | `image` block, base64 | `image_url` data URL | `input_image` | inline data |
| PDF | `document` block, base64; no filename | `file` with `file_data`; a placeholder name and a warning without a filename | `input_file` | inline data; no filename |
| text file | `document` | passes through, though the API takes PDFs only | `input_file` | inline data |
| audio | **raises** | `input_audio` (format `wav`) | **dropped, with no error** | inline data |
| video | **raises** | raises | raises | inline data |

"Raises" is a `ValueError` while the request is built. **A silent drop is the worst case**: the model answers from the text alone. Test the type you plan to
use, with your provider and your model.

## How we read it

- This page was written from the providers' documentation pages, read on 2026-11-22, plus the source of `langchain-core` 1.8.3,
  `langchain-anthropic` 1.7.11, `langchain-google-genai` 4.4.2 and `langchain-openai` 1.8.2.
- "Not stated" means the pages we read do not say, which is not the same as "not supported".
- Some pages were long, and parts of them were summarised, not read in full: a number marked "unconfirmed" comes from such a part.
- We did not call any provider to check a limit.
