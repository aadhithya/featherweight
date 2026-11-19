"""The same agent behind a web API, for a browser to talk to.

    uvicorn --app-dir examples/chat api:app

Needs ANTHROPIC_API_KEY and the `anthropic` extra. Point an AG-UI client at `/agui`, or `useChat` of the Vercel AI SDK at
`/vercel`. The client sends the whole conversation, and `featherweight` keeps none.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from support import Support

from featherweight import AgentRunner, BadRequest, SqliteDatabase, default_db_path
from featherweight.adapters import agui, vercel


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as app.state.runner:
        yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(BadRequest)
async def bad_request(request: Request, exc: BadRequest):
    return JSONResponse({"error": str(exc)}, status_code=422)


@app.post("/agui")
async def agui_chat(request: Request):
    run = request.app.state.runner.stream(Support, agui.parse_request(await request.body()))
    return StreamingResponse(agui.encode(run), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE)


@app.post("/vercel")
async def vercel_chat(request: Request):
    run = request.app.state.runner.stream(Support, vercel.parse_request(await request.body()))
    return StreamingResponse(vercel.encode(run), headers=vercel.HEADERS)


@app.post("/stop/{run_id}")
async def stop(run_id: str, request: Request):
    if not request.app.state.runner.cancel(run_id):  # the run id is in the first event, RUN_STARTED
        raise HTTPException(404, "No such run")
    return {"stopped": True}
