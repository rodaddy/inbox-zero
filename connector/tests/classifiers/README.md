# tests/classifiers

Tests for the mail classifier layer.

- `test_factory.py`, `test_factory_egress_shape.py` -- which classifier gets
  built from config, and exactly what shape leaves the machine when it runs.
- `test_openai_classifier.py` -- the OpenAI-compatible client.
- `test_classifier_hardening.py` -- malformed, hostile, and missing responses.
