"""The identity of a run: who it is for, as a typed value the application defines.

An identity is a Pydantic model (`class Teacher(BaseModel): user_id: str ...`). The application declares the type once, where the
framework has to check it and to rebuild it (`AgentRunner(db, identity_type=Teacher)`), and passes a value with `identity=`. Tools
read it as `runtime.context.identity`. featherweight carries it: it does not authenticate anyone, decide what they may do, get a
credential for them or keep them. See docs/specs/identity.md.
"""

import types
import typing
from typing import Any

from pydantic import BaseModel, TypeAdapter

_NONE = type(None)


class IdentitySpec:
    """What a runner (a worker, a client) declared as `identity_type`, and the checks that come with it.

    `None` declares nothing: runs have no identity, and passing one is an error. A Pydantic model class makes an identity required.
    A union of one or more model classes and `None` (`Teacher | None`) allows a run without one."""

    def __init__(self, identity_type: Any = None):
        self.declared = identity_type
        self.models: tuple[type[BaseModel], ...] = ()
        self.allows_none = True
        if identity_type is None:
            return
        members = typing.get_args(identity_type) if self._is_union(identity_type) else (identity_type,)
        models = tuple(m for m in members if m is not _NONE)
        if not models or not all(isinstance(m, type) and issubclass(m, BaseModel) for m in models):
            raise TypeError(
                f"identity_type must be a Pydantic model class, or a union of model classes and None, got {identity_type!r}"
            )
        self.models = models
        self.allows_none = _NONE in members
        self._adapter: TypeAdapter[Any] = TypeAdapter(identity_type)

    @staticmethod
    def _is_union(tp: Any) -> bool:
        return typing.get_origin(tp) is typing.Union or isinstance(tp, types.UnionType)

    @property
    def declared_type(self) -> bool:
        return self.declared is not None

    def _name(self) -> str:
        return getattr(self.declared, "__name__", None) or str(self.declared)

    def check(self, identity: Any) -> None:
        """Raise `TypeError` for a programming error: an identity when no type is declared, none when one is required, or one that
        is not an instance of the declared type."""
        if not self.declared_type:
            if identity is not None:
                raise TypeError(
                    "identity= was given, but no identity_type is declared: declare one (identity_type=...) so that the identity "
                    "can be checked, and rebuilt on a worker"
                )
            return
        if identity is None:
            if not self.allows_none:
                raise TypeError(
                    f"this runner declares identity_type={self._name()}, so every run needs identity=: pass one, or declare "
                    f"{self._name()} | None to allow runs without"
                )
            return
        if not isinstance(identity, self.models):
            raise TypeError(f"identity must be a {self._name()}, got {type(identity).__name__}")

    def dump(self, identity: Any) -> dict[str, Any] | None:
        """The identity as JSON-ready data (what travels in a queue message): its fields, without the ones marked `exclude=True`."""
        return None if identity is None else identity.model_dump(mode="json")

    def load(self, data: Any) -> Any:
        """Rebuild an identity from `dump`'s output with the declared type; `ValidationError` if it does not fit. `None` is `None`
        when the type allows it."""
        if data is None:
            if not self.allows_none and self.declared_type:
                raise ValueError("there is no identity, and the declared type requires one")
            return None
        if not self.declared_type:
            raise ValueError("there is an identity, but no identity_type is declared")
        return self._adapter.validate_python(data)
