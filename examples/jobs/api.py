"""The same jobs behind a web API: submit one, follow it with server-sent events, read its result.

    uvicorn --app-dir examples/jobs api:app        (and, in another terminal, python examples/jobs/worker.py)

The API has no worker in it: it only submits, follows and reads. Needs `fastapi` and `uvicorn` (in the dev group).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from shout import Text, shout

import featherweight as ftw


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with ftw.SqliteDatabase(ftw.default_db_path()) as db:
        app.state.client = ftw.JobClient(db, ftw.SqliteQueue(db))
        yield


app = FastAPI(lifespan=lifespan)


@app.post("/jobs")
async def submit(body: Text, request: Request):
    return {"job_id": await request.app.state.client.submit(shout, body)}


@app.get("/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    after = int(request.headers.get("last-event-id", 0))  # a browser that reconnects resumes where it was
    stream = ftw.sse.encode(request.app.state.client.events(job_id, after=after))
    return StreamingResponse(stream, headers=ftw.sse.HEADERS)


@app.get("/jobs/{job_id}/result")
async def result(job_id: str, request: Request):
    return await request.app.state.client.result(shout, job_id)
