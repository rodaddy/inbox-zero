"""Model-backed implementations of the ``classify.Classifier`` protocol.

``classify`` owns the DECISION LAYER and ships only ``RuleOnlyClassifier``,
which declines to guess. This package holds the implementations that answer
the ambiguous remainder the deterministic rules did not settle. They live here
rather than in ``classify`` for the reason that module's section 4 states: the
seam is a structural Protocol, so an implementation carries its own
dependencies without the decision layer inheriting them.

Key Components:
    - openai_compatible: one client for the OpenAI chat-completions PROTOCOL,
      which OpenRouter, OpenAI, Azure, and local servers all speak. Named for
      the protocol, never for a vendor.
    - factory: the ONE place ``config`` meets that constructor. ``from_settings``
      returns a built backend, or ``None`` when no ``base_url`` is configured --
      which leaves the caller on the rule-only layer BY DESIGN.

Pattern/Convention:
    An implementation here is NOT trusted. ``classify.classify`` re-runs
    ``coerce`` on whatever comes back and overwrites ``deterministic`` from
    which layer actually ran, so nothing in this package can widen the
    taxonomy, clear the confidence floor, or describe a guess as a rule.

    Every implementation takes its settings through its CONSTRUCTOR and reads
    no environment variable. ``config`` is the keystone -- it is the one module
    permitted an environment read, and ``tests/test_standards_enforcement.py``
    fails the build if a second one appears.

    Adding a backend means adding a module here. It does NOT mean adding a
    class per vendor: a provider speaking the OpenAI protocol is a base_url and
    a model id, which is configuration rather than code.

Example:
    >>> from inbox_zero_connector.classifiers import OpenAICompatibleClassifier
    >>> backend = OpenAICompatibleClassifier(
    ...     base_url="http://classifier.example.internal:11434/v1",
    ...     model="Qwen3-4B-Instruct-2507-4bit",
    ... )
    >>> backend.model
    'Qwen3-4B-Instruct-2507-4bit'

    The same class reaches a hosted vendor by changing only ``base_url`` and
    ``model`` -- there is no per-vendor subclass to write.

See Also:
    - inbox_zero_connector.services.triage.decide: the protocol, the coercion invariant, and
      the confidence floor every implementation here is subject to.
"""

from __future__ import annotations

from inbox_zero_connector.classifiers.openai_compatible import (
    ClassifierBackendError as ClassifierBackendError,
)
from inbox_zero_connector.classifiers.openai_compatible import (
    OpenAICompatibleClassifier as OpenAICompatibleClassifier,
)
