"""The identity of a run: who it is for, as a typed value the application defines.

An identity is a Pydantic model (`class Teacher(BaseModel): user_id: str ...`). The application declares the type once, where the
framework has to check it and to rebuild it (`AgentRunner(db, identity_type=Teacher)`), and passes a value with `identity=`. Tools
read it as `runtime.context.identity`. featherweight carries it: it does not authenticate anyone, decide what they may do, get a
credential for them or keep them. See docs/specs/identity.md.
"""

import types
import typing
from typing import Any

from pydantic import BaseModel

_NONE = type(None)


class IdentitySpec:
    """What a runner (a worker, a client) declared as `identity_type`, and the checks that come with it.

    `None` declares nothing: runs have no identity, and passing one is an error. A Pydantic model class makes an identity required.
    A union of one or more model classes and `None` (`Teacher | None`) allows a run without one. A union of several models (`Teacher | Student`)
    is for an agent or a job that serves different kinds of people: the kind of an identity, the name of its class, says which one it is."""

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
        names = [m.__name__ for m in models]
        if len(set(names)) != len(names):
            raise TypeError(
                f"identity_type has two models called {sorted({n for n in names if names.count(n) > 1})[0]!r}: the name of the class is "
                "the kind of the identity on the queue, so each model needs its own"
            )
        for model in models:
            for field_name, field in model.model_fields.items():
                if field.exclude and field.is_required():  # the worker rebuilds the identity without it
                    raise TypeError(
                        f"{model.__name__}.{field_name} is marked exclude=True and has no default: a field that is not sent needs "
                        "one (for example None), because the worker rebuilds the identity without it"
                    )
        self.models = models
        self.allows_none = _NONE in members
        self._by_kind = {m.__name__: m for m in models}

    @staticmethod
    def _is_union(tp: Any) -> bool:
        return typing.get_origin(tp) is typing.Union or isinstance(tp, types.UnionType)

    @property
    def declared_type(self) -> bool:
        return self.declared is not None

    def describe(self) -> str:
        """The declaration as words, for an error message."""
        return self._name() if self.declared_type else "no identity"

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

    def accepts(self, other: "IdentitySpec") -> bool:
        """Whether a run on this declaration can take every identity that `other` can produce: each of its models is one of ours (or a
        subclass of one), and if it allows no identity, so do we. A worker's type is `other`, and its runner's is `self`."""
        return all(issubclass(m, self.models) for m in other.models) and (not other.allows_none or self.allows_none)

    def dump(self, identity: Any) -> tuple[dict[str, Any] | None, str | None]:
        """The identity as what travels in a queue message: its fields as JSON-ready data, without the ones marked `exclude=True`,
        and its kind (the name of its class). `(None, None)` for none."""
        if identity is None:
            return None, None
        return identity.model_dump(mode="json"), type(identity).__name__

    def load(self, data: Any, kind: str | None = None) -> Any:
        """Rebuild an identity from `dump`'s output: the model of the declared type that `kind` names, validated against `data`
        (`ValidationError` if it does not fit; `ValueError` if the kind is missing or is not one of the declared models). `None` is
        `None` when the type allows it."""
        if data is None:
            if not self.allows_none and self.declared_type:
                raise ValueError("there is no identity, and the declared type requires one")
            return None
        if not self.declared_type:
            raise ValueError("there is an identity, but no identity_type is declared")
        if kind is None:
            raise ValueError("the identity has no kind: a message says which model its identity is")
        model = self._by_kind.get(kind)
        if model is None:
            raise ValueError(f"the kind of the identity is not one of the declared types ({self._name()})")
        return model.model_validate(data)
