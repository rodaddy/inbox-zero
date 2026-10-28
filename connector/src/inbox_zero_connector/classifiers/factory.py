"""The ONE place ``config`` meets the model-backed classifier's constructor.

WHY THIS MODULE EXISTS (#115): ``ClassifierSettings`` was a complete,
documented vendor switch -- base URL, model id, key requirement -- that
nothing ever read. ``OpenAICompatibleClassifier`` appeared only in tests and
docstrings, and ``watch`` built its ``RoundContext`` with no ``classifier`` at
all, so every deployment silently fell back to the rule-only layer regardless
of what the operator configured. Configuration with no consumer is worse than
absent configuration: it reads as a working feature and produces no error.

Key Components:
    - from_settings: settings in, a constructed backend or ``None`` out.

Pattern/Convention:
    THIS IS THE ONLY PLACE settings meet the classifier constructor. The
    classifier itself reads no environment (``config`` is the keystone), and
    every other caller takes the backend as an injected argument -- so a second
    construction site here would be a second place the vendor switch could
    drift from what the operator set.

    ``None`` IS A LEGITIMATE ANSWER, not a failure. No ``base_url`` means the
    operator named no egress destination, and this connector's posture is that
    an unconfigured destination means mail text goes nowhere: the watcher stays
    rule-only, by design, and says so once at INFO rather than raising.

    A REQUIRED-BUT-ABSENT KEY IS NOT PRE-CHECKED HERE. The constructor already
    raises ``ClassifierBackendError`` naming the variable to set, and checking
    the same condition twice means two error messages to keep in agreement.

    The gateway question is settled: the Bifrost gateway accepts the OpenAI
    SDK's own ``Authorization: Bearer`` header (probed live 2026-08-22, HTTP
    200 for both ``Bearer`` and ``x-bf-vk``), so no ``default_headers``
    override is needed and none is passed.

See Also:
    - inbox_zero_connector.config.ClassifierSettings: the vendor switch itself.
    - inbox_zero_connector.classifiers.openai_compatible: the constructed class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from loguru import logger

from inbox_zero_connector.classifiers.openai_compatible import OpenAICompatibleClassifier

if TYPE_CHECKING:
    from inbox_zero_connector.config import Settings


def _host_only(base_url: str) -> str:
    """The host of ``base_url``, for a log line that names the destination.

    The full URL can carry a path or query an operator pasted a token into, so
    the log records WHERE mail text goes without echoing anything else in it.
    """
    return urlparse(base_url).netloc or base_url


def from_settings(active: Settings) -> OpenAICompatibleClassifier | None:
    """Build the configured model backend, or ``None`` for rule-only.

    Args:
        active: The loaded settings keystone. Read for ``classifier`` (the
            vendor switch) and ``credentials.classifier_api_key``.

    Returns:
        A constructed ``OpenAICompatibleClassifier``, or ``None`` when no
        ``base_url`` is configured -- which leaves the caller rule-only.

    Raises:
        ClassifierBackendError: The endpoint is configured as requiring a key
            and none was supplied. Raised by the constructor, naming the
            variable the operator must set.
    """
    section = active.classifier
    if section.base_url is None:
        logger.info("CLASSIFIER: no base_url configured, rule-only")
        return None

    secret = active.credentials.classifier_api_key
    logger.info(
        "CLASSIFIER: model backend configured base_url={host} model={model} "
        "requires_key={requires_key}",
        host=_host_only(section.base_url),
        model=section.model,
        requires_key=section.requires_key,
    )
    # The egress is settled and complete: sender, subject, snippet, and the
    # installed taxonomy vocabulary. Telling the model which domain is the
    # owner's own is a separate, deliberate wiring decision tracked on #166 --
    # its earlier plumbing was never supplied here and so never rendered.
    return OpenAICompatibleClassifier(
        secret.get_secret_value() if secret is not None else "",
        base_url=section.base_url,
        model=section.model,
        requires_key=section.requires_key,
    )
