# scripts

Developer and build tooling. Not part of the running connector; `src/` never
imports from here.

- `audit_docstrings.py`, `check_code_size.py`, `check_except_logging.py` --
  standards checks run over the source tree.
- `generate_folder_docs.py` -- generates the per-package `README.md` files under
  `src/` from each `__init__.py` docstring. Those are generated, never
  hand-written.
- `packaging/` -- builds and judges the customer zips.
- `voice_corpus/` -- the synthetic corpus generator used to exercise voice work.
