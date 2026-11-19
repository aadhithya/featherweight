"""The same agent behind a web API, for a browser to talk to.

    uvicorn --app-dir examples/chat api:app

Needs ANTHROPIC_API_KEY and the `anthropic` extra. Point an AG-UI client at `/agui`, or `useChat` of the Vercel AI SDK at
`/vercel`. The client sends the whole conversation, and `featherweight` keeps none.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from support import Support

import featherweight as ftw


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with ftw.SqliteDatabase(ftw.default_db_path()) as db, ftw.AgentRunner(db) as app.state.runner:
        yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(ftw.BadRequest)
async def bad_request(request: Request, exc: ftw.BadRequest):
    return JSONResponse({"error": str(exc)}, status_code=422)


@app.post("/agui")
async def agui_chat(request: Request):
    run = request.app.state.runner.stream(Support, ftw.agui.parse_request(await request.body()))
    return StreamingResponse(ftw.agui.encode(run), headers=ftw.agui.HEADERS, media_type=ftw.agui.CONTENT_TYPE)


@app.post("/vercel")
async def vercel_chat(request: Request):
    run = request.app.state.runner.stream(Support, ftw.vercel.parse_request(await request.body()))
    return StreamingResponse(ftw.vercel.encode(run), headers=ftw.vercel.HEADERS)


@app.post("/stop/{run_id}")
async def stop(run_id: str, request: Request):
    if not request.app.state.runner.cancel(run_id):  # the run id is in the first event, RUN_STARTED
        raise HTTPException(404, "No such run")
    return {"stopped": True}
