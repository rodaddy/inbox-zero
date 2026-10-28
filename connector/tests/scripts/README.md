# tests/scripts

Tests for the build tooling in `scripts/packaging/`. Never shipped
(`tests/scripts/**` is on the `never_ship` list), so these can assert on
packaging internals a customer never sees.

- `test_assert_zip.py` -- the judge accepts a clean `enhanced` or `basic` zip
  and refuses one carrying what its tier forbids.
- `test_build_custom.py`, `test_zip_modes.py` -- per-client profile builds and
  the kit/custom mode split.
- `test_packaging_hardening.py` -- the nasty cases: forbidden content, zip
  comments, config pins, and real end-to-end builds.
- `test_installer_next_steps.py` -- the installer prints the right next steps.
