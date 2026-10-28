# tests/apps/server

Tests for the MCP server that Claude Desktop connects to.

- `test_server_tools.py`, `test_record_correction_tool.py` -- the exposed tools.
- `test_enforcement_spine.py`, `test_server_startup_refusal.py` -- the server
  refuses to start or act outside its configured posture.
- `test_logging_redaction.py` -- proves no mail content reaches any log sink.
