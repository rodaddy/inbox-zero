# tests/services/watch

Tests for the watch loop that sweeps the mailbox and files mail.

- `test_watcher.py`, `test_watcher_container_stream.py`,
  `test_watcher_destination_window.py` -- the sweep and where it may write.
- `test_mailbox_scope*.py` -- the mailbox-scope boundary and its persistence.
- `test_watcher_containment_edges.py`, `test_watcher_retry_bound.py` -- the
  loop stays inside its bounds and gives up rather than retrying forever.
- `test_watcher_drafting.py`, `test_watcher_without_voice.py`,
  `test_drafting_loader_real_modules.py` -- the loop with voice present and
  with it excluded, as the basic tier ships it.
- `test_watchloop_config_error.py`, `test_sprint3_demo.py` -- config failure
  and the end-to-end demo path.
