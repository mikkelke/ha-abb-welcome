"""SIP listener dialog-routing response tests."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import time
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"


def _load(name: str) -> types.ModuleType:
    pkg = sys.modules.setdefault("abb_welcome", types.ModuleType("abb_welcome"))
    pkg.__path__ = [str(_PKG_DIR)]
    full = f"abb_welcome.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[full]


intercom = _load("intercom_dialer")
media = _load("media_pipeline")
sip_listener = _load("sip_listener")


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None


class _OpenWriter(_Writer):
    def is_closing(self) -> bool:
        return False


class _ClosingWriter(_Writer):
    def is_closing(self) -> bool:
        return True


def _request(method: str = "INVITE"):
    headers = [
        ("Via", "SIP/2.0/TLS proxy.invalid;branch=first"),
        ("Record-Route", "<sip:first.invalid;lr>"),
        ("record-route", "<sip:second.invalid;lr>"),
        ("From", "<sip:door@example.invalid>;tag=remote"),
        ("To", "<sip:ha@example.invalid>"),
        ("Call-ID", "dialog-routing-test"),
        ("CSeq", f"1 {method}"),
    ]
    return sip_listener._SipFrame(
        start_line=f"{method} sip:ha@example.invalid SIP/2.0",
        headers=headers,
        body=b"",
        raw=b"",
    )


def _respond(frame, code: int, reason: str) -> str:
    listener = sip_listener.SipListener(
        "gateway.invalid", "ha", "secret", "example.invalid"
    )
    writer = _Writer()
    asyncio.run(listener._respond(writer, frame, code, reason))
    return writer.data.decode("utf-8")


def test_invite_200_copies_all_record_routes_in_wire_order() -> None:
    response = _respond(_request(), 200, "OK")

    first = "Record-Route: <sip:first.invalid;lr>"
    second = "Record-Route: <sip:second.invalid;lr>"
    assert response.count("Record-Route:") == 2
    assert response.index(first) < response.index(second)
    assert response.index("Via:") < response.index(first) < response.index("From:")


@pytest.mark.parametrize(
    ("frame", "code", "reason"),
    (
        (_request(), 100, "Trying"),
        (_request("OPTIONS"), 200, "OK"),
    ),
)
def test_record_routes_are_not_copied_outside_invite_2xx(
    frame, code: int, reason: str
) -> None:
    assert "Record-Route:" not in _respond(frame, code, reason)


def test_register_wait_dispatches_requests_and_matches_exact_response() -> None:
    listener = sip_listener.SipListener(
        "gateway.invalid", "ha", "secret", "example.invalid"
    )
    incoming = _request()
    frames = [
        incoming,
        sip_listener._SipFrame(
            start_line="SIP/2.0 200 OK",
            headers=[("Call-ID", "other"), ("CSeq", "7 REGISTER")],
            body=b"",
            raw=b"",
        ),
        sip_listener._SipFrame(
            start_line="SIP/2.0 180 Ringing",
            headers=[("Call-ID", "register"), ("CSeq", "7 REGISTER")],
            body=b"",
            raw=b"",
        ),
        sip_listener._SipFrame(
            start_line="SIP/2.0 200 OK",
            headers=[("Call-ID", "register"), ("CSeq", "7 REGISTER")],
            body=b"",
            raw=b"",
        ),
    ]
    dispatched = []

    async def read_frame(_reader):
        return frames.pop(0)

    async def dispatch(frame, _writer, _local_ip, _local_port):
        dispatched.append(frame)

    listener._read_frame = read_frame
    listener._dispatch = dispatch
    listener._emit_frame = lambda *_args: None

    async def run():
        return await listener._read_final_response(
            None,
            call_id="register",
            cseq_number=7,
            cseq_method="REGISTER",
            writer=_Writer(),
            local_ip="192.0.2.20",
            local_port=40000,
        )

    result = asyncio.run(run())

    assert dispatched == [incoming]
    assert result.status_code == 200
    assert sip_listener._header(result.headers, "Call-ID") == "register"
    assert sip_listener._header(result.headers, "CSeq") == "7 REGISTER"
    assert frames == []


def _pending_invite(call_id: str, station: str):
    frame = sip_listener._SipFrame(
        start_line="INVITE sip:ha@example.invalid SIP/2.0",
        headers=[
            ("Via", "SIP/2.0/TLS proxy.invalid;branch=first"),
            ("From", f"<sip:{station}@example.invalid>;tag=remote"),
            ("To", "<sip:ha@example.invalid>"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
        ],
        body=b"",
        raw=b"",
    )
    frame._received_at = time.time()
    return frame


def _listener_with_pending(*frames):
    listener = sip_listener.SipListener(
        "gateway.invalid", "ha", "secret", "example.invalid"
    )
    listener._writer = _OpenWriter()
    for frame in frames:
        listener._open_invites[sip_listener._header(frame.headers, "Call-ID")] = frame
    return listener


def test_decline_pending_call_sends_486_busy_here_and_forgets_the_invite() -> None:
    listener = _listener_with_pending(_pending_invite("ring-1", "door"))

    assert asyncio.run(listener.decline_pending_call("door")) is True

    response = listener._writer.data.decode("utf-8")
    assert response.startswith("SIP/2.0 486 Busy Here\r\n")
    assert "\r\nCall-ID: ring-1\r\n" in response
    assert "\r\nCSeq: 1 INVITE\r\n" in response
    assert re.search(r"\r\nTo: <sip:ha@example\.invalid>;tag=[0-9a-f]{8}\r\n", response)
    assert len(re.findall(r"^SIP/2\.0 ", response, re.MULTILINE)) == 1
    assert listener._open_invites == {}
    assert listener._accepted_dialogs == {}


def test_decline_pending_call_leaves_other_stations_invites_alone() -> None:
    listener = _listener_with_pending(
        _pending_invite("ring-1", "door"), _pending_invite("ring-2", "gate")
    )

    assert asyncio.run(listener.decline_pending_call("gate")) is True

    assert "\r\nCall-ID: ring-2\r\n" in listener._writer.data.decode("utf-8")
    assert list(listener._open_invites) == ["ring-1"]


def test_decline_pending_call_returns_false_when_nothing_is_pending() -> None:
    listener = _listener_with_pending()

    assert asyncio.run(listener.decline_pending_call("door")) is False

    assert listener._writer.data == b""


@pytest.mark.parametrize("writer", [None, _ClosingWriter()])
def test_decline_pending_call_keeps_the_invite_when_the_writer_is_gone(writer) -> None:
    listener = _listener_with_pending(_pending_invite("ring-1", "door"))
    listener._writer = writer

    assert asyncio.run(listener.decline_pending_call("door")) is False

    assert list(listener._open_invites) == ["ring-1"]


def _session(listener, station_id: str = "door"):
    return media.StreamSession(
        dialer=object(),
        door=intercom.Door("front", "address", station_id=station_id),
        gateway_host="192.0.2.10",
        incoming_listener=listener,
    )


def test_stream_session_decline_reaches_the_listener_for_its_own_station() -> None:
    listener = _listener_with_pending(
        _pending_invite("ring-1", "gate"), _pending_invite("ring-2", "door")
    )

    assert asyncio.run(_session(listener).decline_pending_call()) is True

    assert listener._writer.data.startswith(b"SIP/2.0 486 Busy Here\r\n")
    assert list(listener._open_invites) == ["ring-1"]


@pytest.mark.parametrize("listener", [None, object()])
def test_stream_session_declines_nothing_without_a_declining_listener(listener) -> None:
    assert asyncio.run(_session(listener).decline_pending_call()) is False


def test_stream_session_declines_nothing_for_a_door_without_a_station_id() -> None:
    listener = _listener_with_pending(_pending_invite("ring-1", "door"))

    session = _session(listener, station_id="")

    assert asyncio.run(session.decline_pending_call()) is False

    assert listener._writer.data == b""
    assert list(listener._open_invites) == ["ring-1"]


def test_stream_session_reports_no_decline_when_the_listener_raises() -> None:
    class _Broken:
        async def decline_pending_call(self, _station_id: str) -> bool:
            raise ConnectionResetError("connection lost")

    assert asyncio.run(_session(_Broken()).decline_pending_call()) is False
