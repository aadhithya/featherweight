"""Chapter 4-2: the same assistant behind a web API, for a browser to talk to."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from featherweight import (
    AgentRunner,
    AttachmentLimitError,
    BadRequest,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    StreamingAgent,
    default_db_path,
)
from featherweight.adapters import agui, vercel


class IncidentAssistant(StreamingAgent):  # a streaming agent answers in a chat: no output model, no retries
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, only from the report."
    )
    # the model is a setting of the agent
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as app.state.runner:
        yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(AttachmentLimitError)
async def too_many_files(request: Request, exc: AttachmentLimitError):
    return JSONResponse({"error": str(exc)}, status_code=413)  # more files, or more bytes, than the agent allows


@app.exception_handler(BadRequest)
async def bad_request(request: Request, exc: BadRequest):
    return JSONResponse({"error": str(exc)}, status_code=422)  # a body that is not a chat request


@app.post("/vercel")  # the Vercel AI SDK: useChat
async def vercel_chat(request: Request):
    run = request.app.state.runner.stream(IncidentAssistant, vercel.parse_request(await request.body()))
    return StreamingResponse(vercel.encode(run), headers=vercel.HEADERS)


@app.post("/agui")  # an AG-UI client
async def agui_chat(request: Request):
    run = request.app.state.runner.stream(IncidentAssistant, agui.parse_request(await request.body()))
    return StreamingResponse(agui.encode(run), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE)
