"""Adapters between a streaming run and a wire format: read a client's request, encode the events.

agui.encode(run)        vercel.encode(run)
"""

from featherweight.adapters.errors import BadRequest

from featherweight.adapters import agui, vercel  # isort: skip

__all__ = ["BadRequest", "agui", "vercel"]
