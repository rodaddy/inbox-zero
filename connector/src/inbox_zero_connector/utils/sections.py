"""The base class every settings section inherits. Its own module to avoid a cycle.

``config`` is the keystone and ``mailboxscope`` is a section it re-exports, so
both need this base and neither can own it without importing the other. A
three-line module is the cheapest way to make that dependency a tree instead of
a cycle.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Section(BaseModel):
    """Base for every settings section. Frozen, and rejects unknown keys.

    ``extra="forbid"`` turns a typo into a startup error. Without it a
    misspelled key is dropped silently and the default is used, which presents
    as "my setting does nothing" with no error to search for -- and in this
    package one of those settings decides whether a send-capable Gmail scope is
    requested.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
