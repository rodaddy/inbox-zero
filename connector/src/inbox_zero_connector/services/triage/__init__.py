"""The decision layer - which folder, and how that answer improves.

Rules before model (Rico ruling 2026-08-19): deterministic rules are the
engine, the classifier is the fallback, and Review is the honest home for
what neither can place. Corrections the owner makes become learned rules
that fire BEFORE the model on the next round.

Key Components:
    - decide: THE decision - which folder, and nothing else.
    - taxonomy: the closed folder vocabulary and label-name validation.
    - learned: the learned-rule store - corrections kept as rows that
      deterministically outrank the model.
    - corrections: the ``record_correction`` handler that turns an owner's
      fix into a learned rule.

Pattern/Convention:
    The decision path never mutates a mailbox - it returns an answer the
    watch round applies. Extend the vocabulary in ``taxonomy`` first; a
    label that is not in the taxonomy cannot be decided into existence.

Example:
    >>> from inbox_zero_connector.services.triage.taxonomy import validate_label_name
    >>> validate_label_name("Reference")
    'Reference'

See Also:
    - inbox_zero_connector.classifiers: the model backends behind the
      fallback path.
    - inbox_zero_connector.services.watch: the caller that applies verdicts.
"""

from __future__ import annotations
