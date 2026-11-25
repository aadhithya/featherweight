"""A chat endpoint that a web application writes, with the handlers the guide shows: a conversation over the agent's attachment limits is
answered with 413 and its message, a body that is not a chat request with 422, and a conversation within the limits is streamed. A fake
model, an in-process ASGI client, and no network."""

import httpx
import pytest
from fakes import Streams, runner_with
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from test_attachment_adapters import IMAGE, PNG, agui_body, file_part, vercel_body
from test_streaming import Chat

from featherweight import AttachmentLimitError, BadRequest
from featherweight.adapters import agui, vercel


class OneFile(Chat):
    max_attachments = 1


def make_app(runner) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AttachmentLimitError)
    async def too_big(request: Request, exc: AttachmentLimitError):
        return JSONResponse({"error": str(exc)}, status_code=413)

    @app.exception_handler(BadRequest)
    async def bad_request(request: Request, exc: BadRequest):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.post("/vercel")
    async def vercel_chat(request: Request):
        run = runner.stream(OneFile, vercel.parse_request(await request.body()))
        return StreamingResponse(vercel.encode(run), headers=vercel.HEADERS)

    @app.post("/agui")
    async def agui_chat(request: Request):
        run = runner.stream(OneFile, agui.parse_request(await request.body()))
        return StreamingResponse(agui.encode(run), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE)

    return app


@pytest.fixture
async def client(db):
    runner = runner_with(db, Streams(turns=[["hello"], ["hello"], ["hello"], ["hello"]]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(runner)), base_url="http://test") as client:
        yield client


TWO_IMAGES_AGUI = agui_body(IMAGE, IMAGE)
TWO_IMAGES_VERCEL = vercel_body(file_part(f"data:image/png;base64,{PNG}"), file_part(f"data:image/png;base64,{PNG}"))
ONE_IMAGE_AGUI = agui_body(IMAGE)
ONE_IMAGE_VERCEL = vercel_body(file_part(f"data:image/png;base64,{PNG}"))


@pytest.mark.parametrize(
    "path, body", [("/agui", TWO_IMAGES_AGUI), ("/vercel", TWO_IMAGES_VERCEL)], ids=["agui", "vercel"]
)
async def test_a_conversation_over_the_limit_is_a_413_with_the_message(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 413
    assert "2 attachments, 1 over the 1 OneFile allows" in response.json()["error"]


@pytest.mark.parametrize("path, body", [("/agui", {"nope": 1}), ("/vercel", {"nope": 1})], ids=["agui", "vercel"])
async def test_a_body_that_is_not_a_chat_request_is_still_a_422(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 422 and response.json()["error"]


@pytest.mark.parametrize(
    "path, body", [("/agui", ONE_IMAGE_AGUI), ("/vercel", ONE_IMAGE_VERCEL)], ids=["agui", "vercel"]
)
async def test_a_conversation_within_the_limit_is_streamed(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 200 and "hello" in response.text
