"""Tests for the log-redaction filter's handling of numeric diagnostics."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"


def _load_redaction():
    spec = importlib.util.spec_from_file_location(
        "abb_redaction", _PKG_DIR / "redaction.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redaction = _load_redaction()


def _filtered(msg: str, args: tuple):
    record = logging.LogRecord("t", logging.INFO, "f", 1, msg, args, None)
    redaction.ABBWelcomeRedactionFilter().filter(record)
    return record.args


# "talkback" is itself a sensitive-context word, so this message is the exact
# case that motivated the fix: the one diagnostic built to debug talkback was
# blanked because of its own name.
_TALKBACK_MSG = "[abb] talkback stats %s camera_index=%s: %s"


def test_numeric_stats_dict_survives_a_sensitive_context() -> None:
    stats = {"packets": 412, "underrun_packets": 0, "gain_db": 3.0, "talking": False}
    args = _filtered(_TALKBACK_MSG, ("Front Door", "default", stats))
    assert args[2] == stats
    # The door name is still an identifier and must not leak.
    assert args[0] == redaction.REDACTED


def test_numeric_sequence_survives() -> None:
    assert _filtered(_TALKBACK_MSG, ("d", "default", [1, 2, 3.5]))[2] == [1, 2, 3.5]


def test_dict_containing_any_string_is_still_blanked() -> None:
    secret = {"caller": "sip:100000001@gateway", "key": "AAAA"}
    assert _filtered(_TALKBACK_MSG, ("d", "default", secret))[2] == redaction.REDACTED


def test_mixed_dict_is_blanked_rather_than_partially_leaked() -> None:
    """One string among the numbers must condemn the whole container."""
    mixed = {"packets": 5, "caller": "sip:100000001@gateway"}
    assert _filtered(_TALKBACK_MSG, ("d", "default", mixed))[2] == redaction.REDACTED


def test_nested_container_is_blanked() -> None:
    nested = {"session": {"caller": "sip:100000001@gateway"}}
    assert _filtered(_TALKBACK_MSG, ("d", "default", nested))[2] == redaction.REDACTED


def test_empty_container_stays_conservative() -> None:
    assert _filtered(_TALKBACK_MSG, ("d", "default", {}))[2] == redaction.REDACTED


def test_non_sensitive_message_is_untouched() -> None:
    stats = {"packets": 1}
    assert _filtered("[abb] counters %s", (stats,)) == {"packets": 1}


def test_sip_response_code_survives_without_its_reason_phrase() -> None:
    """The code answers "why did the dial fail?"; the phrase is far-end free text."""
    args = _filtered("[abb] dialer: inbound SIP response %s cseq=%s", ("SIP/2.0 486 Busy Here", "2 INVITE"))
    assert args[0] == "SIP/2.0 486"


def test_reason_phrase_cannot_smuggle_private_text() -> None:
    """RFC 3261 lets the far end put anything in the reason phrase."""
    args = _filtered("BYE result for %s: %s", ("station", "SIP/2.0 200 private-call"))
    assert "private-call" not in str(args[1])


def test_sip_request_start_line_is_still_redacted() -> None:
    """Request lines carry the target URI and must keep being blanked."""
    args = _filtered(
        "[abb] dialer: outbound SIP %s", ("INVITE sip:100000001@ipgw8ce6d3c51e10 SIP/2.0", "x")
    )
    assert args[0] == redaction.REDACTED


def test_call_end_reason_label_survives() -> None:
    args = _filtered("[abb] media: incoming call ended by SIP dialog reason=%s", ("remote_bye", "x"))
    assert args[0] == "remote_bye"


def test_a_sip_uri_smuggled_into_a_response_line_is_redacted() -> None:
    """The anchored pattern must not become a bypass."""
    args = _filtered("[abb] dialer: %s", ("SIP/2.0 200 OK sip:100000001@gateway", "x"))
    assert "100000001" not in str(args[0])
