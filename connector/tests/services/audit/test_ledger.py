"""Ledger behaviour -- security invariant 4 (everything is receipted)."""

from __future__ import annotations

import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- a clean interpreter is the only place an import-time warning is observable.
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from inbox_zero_connector.services.audit import ledger as ledger
from inbox_zero_connector.utils.paths import ledger_path


def test_append_creates_the_file_and_one_row() -> None:
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    rows = ledger_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    parsed = json.loads(rows[0])
    assert parsed["provider"] == "google"
    assert parsed["action"] == ledger.ACTION_FILE
    assert parsed["message_id"] == "m1"
    assert parsed["detail"] == "Action"


def test_appends_accumulate_and_never_overwrite() -> None:
    for index in range(5):
        ledger.append("google", ledger.ACTION_FILE, f"m{index}", "Action")
    entries = ledger.read_all()
    assert len(entries) == 5
    assert [e.message_id for e in entries] == [f"m{i}" for i in range(5)]


def test_existing_rows_survive_a_later_append() -> None:
    """The append-only property: an earlier receipt is still byte-present later."""
    ledger.append("google", ledger.ACTION_FILE, "first", "Action")
    original = ledger_path().read_text(encoding="utf-8")
    ledger.append("microsoft", ledger.ACTION_IMPORTANT, "second", "important")
    updated = ledger_path().read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert len(updated) > len(original)


def test_module_exposes_no_mutating_or_deleting_helper() -> None:
    """No edit/delete/truncate API may exist -- an editable ledger is not evidence."""
    for banned in ("delete", "remove", "edit", "update", "truncate", "clear", "rewrite"):
        assert not hasattr(ledger, banned), f"ledger must not expose {banned}"


def test_ledger_is_opened_append_only() -> None:
    """Guard the actual file mode: 'w' anywhere here would destroy history."""
    from pathlib import Path

    source = Path(ledger.__file__).read_text(encoding="utf-8")
    assert '.open("a"' in source
    assert '.open("w"' not in source


def test_read_all_skips_malformed_lines_without_repairing_them() -> None:
    ledger.append("google", ledger.ACTION_FILE, "good", "Action")
    with ledger_path().open("a", encoding="utf-8") as handle:
        handle.write("{corrupt\n")
    before = ledger_path().read_text(encoding="utf-8")
    entries = ledger.read_all()
    assert [e.message_id for e in entries] == ["good"]
    # Reading must not rewrite the file.
    assert ledger_path().read_text(encoding="utf-8") == before


def test_entries_for_day_filters_by_date() -> None:
    ledger.append("google", ledger.ACTION_FILE, "today", "Action")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    assert len(ledger.entries_for_day(datetime.now(UTC).date())) == 1
    assert ledger.entries_for_day(yesterday) == []


def test_digest_reports_no_activity_for_an_empty_ledger() -> None:
    digest = ledger.daily_digest()
    assert "No mailbox changes were made" in digest


def test_digest_counts_filings_per_folder_and_flags() -> None:
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    ledger.append("google", ledger.ACTION_FILE, "m2", "Action")
    ledger.append("google", ledger.ACTION_FILE, "m3", "Receipts")
    ledger.append("google", ledger.ACTION_IMPORTANT, "m4", "important")

    digest = ledger.daily_digest()
    assert "Filed 3 message(s)" in digest
    assert "Action: 2" in digest
    assert "Receipts: 1" in digest
    assert "Marked important: 1" in digest
    assert "4 change(s)" in digest
    # The digest must restate the safety posture the owner is relying on.
    assert "Nothing was sent" in digest


def test_digest_for_a_day_with_no_rows() -> None:
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    assert "No mailbox changes were made" in ledger.daily_digest(date(2000, 1, 1))


def test_digest_names_the_ledger_file_for_auditing() -> None:
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    assert str(ledger_path()) in ledger.daily_digest()


def test_ledger_rows_do_not_carry_mail_content() -> None:
    """A leaked ledger must not be a leaked mailbox: no subjects, bodies, addresses.

    ``voice`` joined the row in schema 2. It holds a profile NAME, which is a
    fixed word chosen by the installer, so it carries no mail content -- that
    is why it is allowed here and why the draft BODY is not.

    ``status`` and ``error_code`` joined in schema 3. ``status`` is a closed
    two-value enum and ``error_code`` holds a provider CODE, never the
    provider's free-text message -- provider prose can quote the subject line
    it refused, which is why the code is the only part kept.

    ``deterministic`` joined in Sprint 3. It is a BOOL derived from which code
    path answered -- the rule layer or the model -- and it is set by
    ``classify`` from which layer ran, never from what an implementation
    claimed. A bool computed from control flow cannot carry mail content by
    construction: there is no value it could take that depends on what a
    message said. It is here because the audit trail has to be able to answer
    "which filings were free", which is the claim the deterministic-first
    posture is sold on, and an answer that only exists in memory is not an
    audit trail.

    ``run_id`` and ``source_container`` joined in schema 4. The first is an
    opaque uuid minted by the round; the second is a container NAME from the
    operator's own taxonomy -- the same alphabet ``detail`` already carries, and
    the field ``undo`` needs to put a filing back without guessing where it came
    from. Neither can hold a subject, a body, or an address.

    ``account`` joined in schema 5. It IS an address, and it is the one address
    this file is allowed to hold -- the owner's own mailbox, typed by the
    installer and already sitting in the config file in plain text. What the
    no-mail-content rule protects is the CORRESPONDENT: a sender, a recipient,
    a subject, a body. The mailbox the connector was pointed at is not a fact
    about anyone's mail, and one ledger now receives every configured account's
    rows (#69), so without it the file cannot say which mailbox it is
    describing.
    """
    entry = ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    fields = set(json.loads(entry.to_json()).keys())
    assert fields == {
        "timestamp",
        "provider",
        "action",
        "message_id",
        "detail",
        "voice",
        "status",
        "error_code",
        "deterministic",
        "run_id",
        "source_container",
        "account",
        "schema",
    }


def test_draft_rows_record_the_voice_and_nothing_from_the_draft() -> None:
    entry = ledger.append("google", ledger.ACTION_DRAFT, "t1", detail="draft:d1", voice="external")
    payload = json.loads(entry.to_json())
    assert payload["voice"] == "external"
    assert payload["action"] == ledger.ACTION_DRAFT
    # The detail is a draft ID, not text the owner wrote.
    assert payload["detail"] == "draft:d1"


def test_a_schema_1_row_still_reads_back(tmp_path: Path) -> None:
    """Additive schema change: an existing ledger must not need migrating.

    An audit trail you rewrite is worth less than one you can only append to,
    so v1 rows are READ, not converted -- ``voice`` reads as "" because v1
    could not create drafts at all.
    """
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-17T10:00:00+00:00",
            "provider": "google",
            "action": "file_message",
            "message_id": "m1",
            "detail": "Action",
            "schema": 1,
        })
        + "\n",
        encoding="utf-8",
    )
    entries = ledger.read_all(target)
    assert len(entries) == 1
    assert entries[0].voice == ""
    assert entries[0].detail == "Action"
    assert entries[0].schema_version == 1


# --------------------------------------------------------------------------
# Schema 3: the outcome field. A classification decided but NOT applied is a
# state the operator needs to see -- the two wrong answers it replaces are
# dropping it silently and recording it as applied.
# --------------------------------------------------------------------------


def test_a_failure_entry_round_trips_through_the_file() -> None:
    """A refused mutation is receipted, and reads back as refused.

    This is the whole point of the field: the row survives the trip to disk
    and back carrying the fact that the mailbox did NOT change.
    """
    ledger.append(
        "microsoft",
        ledger.ACTION_FILE,
        "m1",
        "Action",
        outcome=ledger.failed("ErrorAccessDenied"),
    )
    entries = ledger.read_all()
    assert len(entries) == 1
    assert entries[0].status is ledger.Status.FAILED
    assert entries[0].error_code == "ErrorAccessDenied"
    assert entries[0].message_id == "m1"


def test_a_failure_row_serialises_the_enum_value_not_its_repr() -> None:
    """On disk the row says ``"failed"``, never ``"Status.FAILED"``.

    A ``StrEnum`` that serialised as its repr would make every written row
    unreadable by anything but this exact Python version of this module.
    """
    entry = ledger.append(
        "google",
        ledger.ACTION_IMPORTANT,
        "m1",
        "important",
        outcome=ledger.failed("403"),
    )
    payload = json.loads(entry.to_json())
    assert payload["status"] == "failed"
    assert payload["error_code"] == "403"


def test_the_default_status_is_applied_so_existing_call_sites_are_unchanged() -> None:
    """Every pre-schema-3 call site keeps recording a success, with no edit.

    The three production call sites in ``server.py`` pass no ``status``. If
    the default were anything but APPLIED, every successful mutation in the
    product would start receipting the wrong outcome.
    """
    entry = ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    assert entry.status is ledger.Status.APPLIED
    assert entry.error_code == ""
    assert json.loads(entry.to_json())["status"] == "applied"


def test_the_status_enum_refuses_an_unknown_value() -> None:
    """The closed set is the point: a typo cannot become a new outcome.

    ``Status.SUCESS`` is a mypy error and ``"sucess"`` is a ValueError here.
    A bare string field would have accepted both and shipped.
    """
    with pytest.raises(ValueError, match="sucess"):
        ledger.Status("sucess")


def test_only_two_outcomes_exist() -> None:
    """Guard against a speculative third state arriving without a call site.

    A state nothing writes is a state no reader can trust, so adding one is a
    deliberate change that has to come through here.
    """
    assert {s.value for s in ledger.Status} == {"applied", "failed"}


def test_a_failure_entry_carries_no_content_bearing_field() -> None:
    """A FAILED row diagnoses the refusal without leaking the message.

    Operation, provider code, and message id -- and nothing whose name could
    hold a subject, a snippet, a sender, or a body.
    """
    entry = ledger.append(
        "microsoft",
        ledger.ACTION_FILE,
        "m1",
        "Action",
        outcome=ledger.failed("ErrorAccessDenied"),
    )
    payload = json.loads(entry.to_json())
    assert not {"subject", "snippet", "sender", "body", "preview", "address"} & set(payload)
    # The three things an operator needs to diagnose it are all present.
    assert payload["action"] == ledger.ACTION_FILE
    assert payload["error_code"] == "ErrorAccessDenied"
    assert payload["message_id"] == "m1"


def test_a_schema_2_row_without_a_status_reads_back_as_applied(tmp_path: Path) -> None:
    """An OLD ledger file still reads, and reads CORRECTLY.

    Before schema 3 the ledger recorded successes only, so APPLIED is the
    true reading of a row with no ``status`` key -- not a defaulted guess.
    The file here is written by hand, exactly as schema 2 wrote it, because
    reading a row this module can no longer produce is the whole test.
    """
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-17T10:00:00+00:00",
            "provider": "google",
            "action": "create_draft",
            "message_id": "t1",
            "detail": "draft:d1",
            "voice": "external",
            "schema": 2,
        })
        + "\n",
        encoding="utf-8",
    )
    entries = ledger.read_all(target)
    assert len(entries) == 1
    assert entries[0].status is ledger.Status.APPLIED
    assert entries[0].error_code == ""
    assert entries[0].voice == "external"
    assert entries[0].schema_version == 2


def test_a_schema_1_row_also_reads_back_as_applied(tmp_path: Path) -> None:
    """The oldest rows on disk survive the second additive bump too."""
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-17T10:00:00+00:00",
            "provider": "google",
            "action": "file_message",
            "message_id": "m1",
            "detail": "Action",
            "schema": 1,
        })
        + "\n",
        encoding="utf-8",
    )
    entries = ledger.read_all(target)
    assert entries[0].status is ledger.Status.APPLIED
    assert entries[0].voice == ""
    assert entries[0].schema_version == 1


def test_a_row_with_an_unknown_status_is_skipped_not_guessed(tmp_path: Path) -> None:
    """A corrupt outcome is dropped loudly rather than read as a success.

    Coercing an unrecognised status to APPLIED would turn an unreadable row
    into a false claim that the mailbox changed, which is worse than the row
    being absent.
    """
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        json.dumps({
            "timestamp": "2026-08-17T10:00:00+00:00",
            "provider": "google",
            "action": "file_message",
            "message_id": "m1",
            "detail": "Action",
            "status": "sucess",
            "schema": 3,
        })
        + "\n",
        encoding="utf-8",
    )
    assert ledger.read_all(target) == []


def test_the_schema_version_is_five() -> None:
    """The bump is what tells a reader which fields a row may be missing."""
    assert ledger.SCHEMA_VERSION == 5


def test_the_digest_counts_a_failed_filing_separately_from_an_applied_one() -> None:
    """A refused action must not inflate the "we filed it" count.

    The digest is what the owner reads to answer "what did it do to my
    mailbox today?". A failed filing changed nothing, so counting it as a
    filing would make the digest overstate the mutation -- the exact defect
    the ledger exists to prevent.
    """
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    ledger.append(
        "google",
        ledger.ACTION_FILE,
        "m2",
        "Action",
        outcome=ledger.failed("403"),
    )
    digest = ledger.daily_digest()
    assert "Filed 1 message(s)" in digest
    assert "Action: 1" in digest
    assert "1 action(s) were refused" in digest
    assert "403" in digest


def test_the_digest_stays_silent_about_failures_when_there_are_none() -> None:
    """No failure section on a clean day -- an empty heading trains people to skip it."""
    ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    assert "refused" not in ledger.daily_digest()


def test_the_failed_constructor_builds_a_failed_outcome() -> None:
    """``ledger.failed(code)`` is the only ergonomic way to record a refusal.

    A named constructor rather than a raw ``Outcome(...)`` at each call site,
    so recording a failure without its diagnostic code is awkward to write by
    accident.
    """
    outcome = ledger.failed("ErrorAccessDenied")
    assert outcome.status is ledger.Status.FAILED
    assert outcome.error_code == "ErrorAccessDenied"


def test_the_default_outcome_is_applied_with_no_code() -> None:
    """``ledger.APPLIED`` is the default ``append`` uses for every old call site."""
    assert ledger.APPLIED.status is ledger.Status.APPLIED
    assert ledger.APPLIED.error_code == ""


# ---------------------------------------------------------------------------
# Sprint 4: the draft idempotency oracle.
# ---------------------------------------------------------------------------


def test_thread_has_draft_is_false_on_an_empty_ledger() -> None:
    assert ledger.thread_has_draft("t-1") is False


def test_thread_has_draft_answers_true_only_for_the_thread_that_was_drafted() -> None:
    ledger.append("google", ledger.ACTION_DRAFT, "t-1", "draft:d1", voice="external")

    assert ledger.thread_has_draft("t-1") is True
    assert ledger.thread_has_draft("t-2") is False


def test_a_filing_row_on_the_same_key_is_not_read_as_a_draft() -> None:
    """The ACTION is checked, not merely the id. Filing does not block drafting."""
    ledger.append("google", ledger.ACTION_FILE, "t-1", "Business-Action")

    assert ledger.thread_has_draft("t-1") is False


def test_a_failed_draft_row_still_blocks_a_second_attempt() -> None:
    """A recorded failure counts, deliberately.

    The attempt reached the provider boundary and its real outcome is not
    knowable from the ledger. Re-attempting on the strength of a FAILED row is
    how one contended thread becomes a retry loop that eventually lands two
    drafts in the owner's mailbox.
    """
    ledger.append(
        "google",
        ledger.ACTION_DRAFT,
        "t-1",
        "draft-attempt",
        outcome=ledger.failed("503"),
    )

    assert ledger.thread_has_draft("t-1") is True


def test_thread_has_draft_refuses_a_blank_key_without_scanning() -> None:
    ledger.append("google", ledger.ACTION_DRAFT, "t-1", "draft:d1")

    assert ledger.thread_has_draft("") is False
    assert ledger.thread_has_draft("   ") is False


def test_the_answer_survives_when_the_replay_window_would_not() -> None:
    """The ledger is append-only and nothing in this package clears it.

    That is the entire reason this is the idempotency oracle rather than
    ``WatchState``: a cursor resync drops the cursor and re-reads the stream,
    and the ``seen`` set is bounded, so both can forget. A row here cannot.
    """
    ledger.append("google", ledger.ACTION_DRAFT, "t-1", "draft:d1")
    rows_before = len(ledger.read_all())

    ledger.append("google", ledger.ACTION_FILE, "m9", "Receipts")

    assert ledger.thread_has_draft("t-1") is True
    assert len(ledger.read_all()) == rows_before + 1


# --------------------------------------------------------------------------
# The `schema` field name is load-bearing ON DISK but must not cost a warning
# at import. Pydantic warns because `schema` shadows the deprecated v1
# `BaseModel.schema()` classmethod; the fix renames the ATTRIBUTE and keeps the
# on-disk key via an alias, so neither the operator nor an existing ledger pays
# for it.
# --------------------------------------------------------------------------


def test_importing_the_package_emits_no_warning() -> None:
    """No CLI invocation may print a pydantic shadow warning.

    Model classes are built once at import, so the warning fires during the
    FIRST import in a process and never again. A subprocess is what makes this
    observable -- in-process the module is already imported by the time any
    test runs, and `importlib.reload` would not rebuild the parent modules.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::UserWarning",
            "-c",
            "import inbox_zero_connector.services.audit.ledger",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "shadows an attribute" not in result.stderr


def test_the_on_disk_key_is_still_schema_after_the_rename() -> None:
    """The rename is INTERNAL. Every reader of a real ledger keys on `schema`."""
    entry = ledger.append("google", ledger.ACTION_FILE, "m1", "Action")
    payload = json.loads(entry.to_json())
    assert "schema" in payload
    assert payload["schema"] == ledger.SCHEMA_VERSION
    # The internal attribute name must not leak into the row.
    assert "schema_version" not in payload


def test_a_row_round_trips_through_the_file_keeping_the_schema_key() -> None:
    """Write then read: the key on disk and the value read back both survive."""
    written = ledger.append("google", ledger.ACTION_FILE, "m9", "Action")
    raw = json.loads(ledger_path().read_text(encoding="utf-8").strip())
    assert raw["schema"] == ledger.SCHEMA_VERSION
    entries = ledger.read_all()
    assert len(entries) == 1
    assert entries[0].schema_version == written.schema_version == ledger.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# #169: an unknown post-move id is NAMED, never recorded as None
# ---------------------------------------------------------------------------


def test_the_unknown_sentinel_is_a_value_no_provider_mints() -> None:
    """The sentinel must not collide with a real id, or a row becomes ambiguous.

    Provider ids are opaque base64-ish tokens (Graph) or hex (Gmail); a
    hyphenated English phrase is outside both alphabets, which is what makes
    `message_id == MESSAGE_ID_UNKNOWN` a safe test for "the provider declined
    to name it" rather than a guess.
    """
    assert ledger.MESSAGE_ID_UNKNOWN == "message-id-unknown"


def test_append_records_the_unknown_sentinel_as_an_ordinary_id() -> None:
    """The sentinel is a normal `str`, so nothing on the write path special-cases it."""
    entry = ledger.append("microsoft", ledger.ACTION_FILE, ledger.MESSAGE_ID_UNKNOWN, "Action")
    assert entry.message_id == ledger.MESSAGE_ID_UNKNOWN
    raw = json.loads(ledger_path().read_text(encoding="utf-8").strip())
    assert raw["message_id"] == ledger.MESSAGE_ID_UNKNOWN


def test_append_refuses_a_none_message_id_rather_than_recording_it() -> None:
    """`None` reaches this via `getattr` on an `Any`, which mypy cannot see.

    Recording it would write the literal string `"None"` into an audit row and
    make an unknown id indistinguishable from a message actually named `None`.
    """
    with pytest.raises(ValueError, match="message_id"):
        ledger.append("microsoft", ledger.ACTION_FILE, None, "Action")  # type: ignore[arg-type]
    assert not ledger_path().exists()


def test_append_refuses_an_empty_message_id() -> None:
    """An empty id names nothing; `MESSAGE_ID_UNKNOWN` is how unknown is spelled."""
    with pytest.raises(ValueError, match="message_id"):
        ledger.append("microsoft", ledger.ACTION_FILE, "   ", "Action")
    assert not ledger_path().exists()
