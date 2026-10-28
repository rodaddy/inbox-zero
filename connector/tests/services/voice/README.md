# tests/services/voice

Tests for the voice service -- the enhanced tier only; the basic tier excludes
this tree (`tiers.json` -> `basic.exclude`).

- `test_voiceprofile.py` -- building an owner voice profile from sent mail.
- `test_voice_to_draft.py`, `test_drafting.py` -- turning that profile into a
  reply draft.
- `test_posture_security_surface.py` -- drafting stays off unless the configured
  posture permits it, and drafts are never sent.
