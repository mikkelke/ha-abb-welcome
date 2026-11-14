"""Door unlock over the dialer's established call with the same station."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import voluptuous

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"
_PACKAGE = "abb_active_call_unlock_test"


def _load(name: str) -> types.ModuleType:
    package = sys.modules.setdefault(_PACKAGE, types.ModuleType(_PACKAGE))
    package.__path__ = [str(_PKG_DIR)]
    full_name = f"{_PACKAGE}.{name}"
    if full_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            full_name, _PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
    return sys.modules[full_name]


const = _load("const")
redaction = _load("redaction")
sip_client = _load("sip_client")
intercom = _load("intercom_dialer")

FRONT = {
    "name": "Front",
    "station_id": "100000001",
    "address": "sip:100000001@example.invalid",
    "body": "1",
    "index": 0,
}
BACK = {
    "name": "Back",
    "station_id": "100000002",
    "address": "sip:100000002@example.invalid",
    "body": "1",
    "index": 1,
}
BACK_TARGET = "sip:100000002@example.invalid;transport=tls"
OK = [("200 OK", {})]
CHALLENGE = 'Digest realm="gateway", nonce="abc123", qop="auth"'


def _parse_request(raw: str) -> tuple[str, dict[str, str], str]:
    head, _, body = raw.partition("\r\n\r\n")
    request_line, *lines = head.split("\r\n")
    headers = {}
    for line in lines:
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    return request_line, headers, body


class _Gateway:
    """Writer stand-in that answers each request from a script."""

    def __init__(self, dialer, scripts, *, fail_write: bool = False) -> None:
        self._dialer = dialer
        self._scripts = list(scripts)
        self._fail_write = fail_write
        self.requests: list[str] = []
        self.closing = False

    def write(self, data: bytes) -> None:
        if self._fail_write:
            raise ConnectionResetError("synthetic reset")
        raw = data.decode()
        self.requests.append(raw)
        _, headers, _ = _parse_request(raw)
        base = {"Call-ID": headers["Call-ID"], "CSeq": headers["CSeq"]}
        for status, extra in self._scripts.pop(0) if self._scripts else []:
            self._dialer._inbound_queue.put_nowait(
                intercom.SipFrame(
                    start_line=f"SIP/2.0 {status}",
                    headers=list({**base, **extra}.items()),
                    body=b"",
                    raw=b"",
                )
            )

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    async def wait_closed(self) -> None:
        return None


def _dialer(station_id: str | None = "100000001", scripts=(OK,), **gateway_options):
    dialer = intercom.IntercomDialer(
        "192.0.2.10", "synthetic-user", "synthetic-password", "example.invalid"
    )
    dialer._local_ip, dialer._local_port = "192.0.2.20", 40000
    dialer._cseq = 7
    gateway = _Gateway(dialer, scripts, **gateway_options)
    dialer._writer = gateway
    if station_id is not None:
        dialer._call = intercom.CallState(
            door=intercom.Door(
                "Door", f"sip:{station_id}@example.invalid", station_id=station_id
            ),
            call_id="dialog-call",
            local_tag="dialog-local",
            remote_tag="dialog-remote",
            invite_cseq=3,
            request_uri=f"sip:{station_id}@example.invalid",
            remote_contact=f"sip:{station_id}@192.0.2.10:5061",
            audio_local_port=6000,
            video_local_port=6002,
            answer=intercom.ParsedSdp(),
        )
    return dialer, gateway


@pytest.mark.parametrize(
    "call_station, asked, expected",
    [
        ("100000001", "100000001", True),
        ("100000001", "100000002", False),
        ("100000001", "", False),
        ("", "", False),
        (None, "100000001", False),
    ],
)
def test_dialer_knows_the_station_of_its_call(call_station, asked, expected):
    dialer, _ = _dialer(call_station)
    assert dialer.in_call_with(asked) is expected


def test_unlock_message_is_sent_out_of_dialog_on_the_dialer_connection():
    async def scenario():
        dialer, gateway = _dialer()
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is True
        assert len(gateway.requests) == 1
        request_line, headers, body = _parse_request(gateway.requests[0])
        assert request_line == f"MESSAGE {BACK_TARGET} SIP/2.0"
        assert headers["To"] == f"<{BACK_TARGET}>"
        assert headers["CSeq"] == "1 MESSAGE"
        assert headers["Content-Type"] == "text/plain"
        assert headers["Content-Length"] == "1"
        assert body == "1"
        assert headers["Call-ID"].endswith("@example.invalid")
        assert headers["Call-ID"] != "dialog-call"
        assert headers["From"].startswith(
            "<sip:synthetic-user@example.invalid;transport=tls>;tag="
        )
        assert "dialog-local" not in headers["From"]
        assert headers["Via"].startswith("SIP/2.0/TLS 192.0.2.20:40000;branch=z9hG4bK-")
        assert "Authorization" not in headers
        assert dialer._cseq == 7
        assert dialer.in_call_with("100000001")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "script",
    [
        [("100 Trying", {}), ("200 OK", {})],
        [("200 OK", {"Call-ID": "other-transaction"}), ("200 OK", {})],
        [("200 OK", {"CSeq": "9 MESSAGE"}), ("200 OK", {})],
    ],
)
def test_only_the_matching_final_response_counts(script):
    async def scenario():
        dialer, gateway = _dialer(scripts=[script])
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is True
        assert len(gateway.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["486 Busy Here", "403 Forbidden", "500 Error"])
def test_non_200_final_response_fails_after_one_send_and_is_logged(status, caplog):
    async def scenario():
        dialer, gateway = _dialer(scripts=[[(status, {})]])
        with caplog.at_level(logging.WARNING):
            assert (
                await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is False
            )
        assert len(gateway.requests) == 1

    asyncio.run(scenario())
    code = status.split()[0]
    assert any(
        record.levelno >= logging.WARNING and f"SIP/2.0 {code}" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "status, challenge_header, auth_header",
    [
        ("401 Unauthorized", "WWW-Authenticate", "Authorization"),
        (
            "407 Proxy Authentication Required",
            "Proxy-Authenticate",
            "Proxy-Authorization",
        ),
    ],
)
def test_challenge_is_answered_once_in_the_same_transaction(
    status, challenge_header, auth_header
):
    async def scenario():
        dialer, gateway = _dialer(
            scripts=[[(status, {challenge_header: CHALLENGE})], OK]
        )
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is True
        assert len(gateway.requests) == 2
        first = _parse_request(gateway.requests[0])
        _, headers, body = _parse_request(gateway.requests[1])
        assert headers["CSeq"] == "2 MESSAGE"
        assert headers["Call-ID"] == first[1]["Call-ID"]
        assert headers["From"] == first[1]["From"]
        assert headers["To"] == first[1]["To"]
        assert headers["Via"] != first[1]["Via"]
        assert headers[auth_header].startswith(
            'Digest username="synthetic-user", realm="gateway", nonce="abc123", '
            f'uri="{BACK_TARGET}", response="'
        )
        assert body == "1"
        assert dialer._cseq == 7

    asyncio.run(scenario())


def test_repeated_challenge_fails_without_a_third_send():
    async def scenario():
        challenge = ("401 Unauthorized", {"WWW-Authenticate": CHALLENGE})
        dialer, gateway = _dialer(scripts=[[challenge], [challenge]])
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is False
        assert len(gateway.requests) == 2

    asyncio.run(scenario())


def test_challenge_without_parameters_fails_without_a_resend():
    async def scenario():
        dialer, gateway = _dialer(scripts=[[("401 Unauthorized", {})]])
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is False
        assert len(gateway.requests) == 1

    asyncio.run(scenario())


def test_missing_response_times_out_as_failure():
    async def scenario():
        dialer, gateway = _dialer(scripts=[[]])
        assert (
            await dialer.send_unlock_message(
                "100000001", BACK_TARGET, "1", timeout=0.05
            )
            is False
        )
        assert len(gateway.requests) == 1

    asyncio.run(scenario())


def test_connection_error_fails_instead_of_raising():
    async def scenario():
        dialer, gateway = _dialer(fail_write=True)
        assert await dialer.send_unlock_message("100000001", BACK_TARGET, "1") is False
        assert gateway.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "call_station, asked, closing",
    [
        ("100000002", "100000001", False),
        (None, "100000001", False),
        ("", "", False),
        ("100000001", "100000001", True),
    ],
)
def test_refuses_without_sending_unless_the_call_is_with_that_station(
    call_station, asked, closing
):
    async def scenario():
        dialer, gateway = _dialer(call_station)
        gateway.closing = closing
        assert await dialer.send_unlock_message(asked, BACK_TARGET, "1") is None
        assert gateway.requests == []

    asyncio.run(scenario())


def _client(*, strategy: str = "hybrid", doors=(FRONT, BACK)):
    return sip_client.SIPClient(
        host="192.0.2.10",
        username="synthetic-user",
        password="synthetic-password",
        domain="example.invalid",
        doors=list(doors),
        unlock_strategy=strategy,
    )


def test_invite_route_message_describes_only_non_default_doors():
    client = _client()
    assert client.invite_route_message(FRONT) is None
    assert client.invite_route_message(BACK) == sip_client.UnlockMessage(
        station_id="100000002", target_uri=BACK_TARGET, body="1"
    )


@pytest.mark.parametrize(
    "strategy, front_on_invite_route",
    [("hybrid", False), ("standard", True), ("fast", False)],
)
def test_invite_route_message_follows_the_unlock_strategy(
    strategy, front_on_invite_route
):
    message = _client(strategy=strategy).invite_route_message(FRONT)
    assert (message is not None) is front_on_invite_route


def test_invite_route_message_is_none_for_an_unusable_door():
    assert _client().invite_route_message({"name": "Nothing"}) is None


class _Stream:
    def __init__(self) -> None:
        self.sock = types.SimpleNamespace(
            sendall=lambda _data: None, settimeout=lambda _timeout: None
        )

    def recv_frame(self):
        return sip_client.SipFrame(
            "SIP/2.0 200 OK",
            [("To", "<sip:ignored@example.invalid>;tag=remote")],
            b"",
        )


@pytest.mark.parametrize(
    "address",
    [
        "sip:100000002@example.invalid",
        "sip:100000002@example.invalid;transport=tls",
    ],
)
def test_invite_route_message_targets_the_uri_the_invite_route_uses(address):
    door = {**BACK, "address": address}
    client = _client(doors=(FRONT, door))
    gw = sip_client._build_gateway_config("example.invalid", "192.0.2.10", "tls")
    session = sip_client._start_invite_call(
        _Stream(),
        "192.0.2.20",
        40000,
        gw,
        "synthetic-user",
        "synthetic-password",
        client._normalize_door(door, None),
        1.0,
        "192.0.2.20",
        "127.0.0.1",
    )
    session.close_media()
    assert client.invite_route_message(door).target_uri == session.target_uri


@pytest.fixture(scope="module")
def button_module():
    stubs = {
        "homeassistant.components.button": {
            "ButtonEntity": type("ButtonEntity", (), {})
        },
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.const": {
            "EntityCategory": types.SimpleNamespace(DIAGNOSTIC="diagnostic")
        },
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {
            "HomeAssistantError": type("HomeAssistantError", (Exception,), {})
        },
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        f"{_PACKAGE}.coordinator": {"ABBWelcomeCoordinator": object},
        f"{_PACKAGE}.device": {"gateway_device_info": lambda data: {}},
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, attrs in stubs.items():
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            monkeypatch.setitem(sys.modules, name, module)
        yield _load("button")


def _press(
    button_module,
    door,
    *,
    station=None,
    scripts=(OK,),
    second_lock=False,
    strategy="hybrid",
    with_dialer=True,
):
    """Press a door button; return (unlock_door calls, wire requests, error)."""

    async def scenario():
        client = _client(strategy=strategy)
        client_calls = []
        client.unlock_door = lambda *args: client_calls.append(args) or True
        dialer, gateway = _dialer(station, scripts)
        entry_data = {"intercom_dialer": dialer} if with_dialer else {}

        async def executor(function, *args):
            return function(*args)

        hass = types.SimpleNamespace(
            data={const.DOMAIN: {"synthetic-entry": entry_data}},
            async_add_executor_job=executor,
        )
        button = button_module.ABBWelcomeDoorButton(
            client,
            door,
            "synthetic-gateway",
            "synthetic-entry",
            {},
            second_lock=second_lock,
        )
        button.hass = hass
        error = None
        try:
            await button.async_press()
        except button_module.HomeAssistantError as err:
            error = err
        return client_calls, gateway.requests, error

    return asyncio.run(scenario())


def test_non_default_door_unlocks_inside_the_active_call(button_module):
    client_calls, requests, error = _press(button_module, BACK, station="100000002")
    assert error is None
    assert client_calls == []
    assert len(requests) == 1
    request_line, headers, body = _parse_request(requests[0])
    assert request_line == f"MESSAGE {BACK_TARGET} SIP/2.0"
    assert headers["CSeq"] == "1 MESSAGE"
    assert body == "1"


def test_failure_inside_the_active_call_raises_without_a_second_invite(button_module):
    client_calls, requests, error = _press(
        button_module, BACK, station="100000002", scripts=[[("486 Busy Here", {})]]
    )
    assert "Failed to unlock door: Back" in str(error)
    assert client_calls == []
    assert len(requests) == 1


def test_standard_strategy_uses_the_active_call_for_every_door(button_module):
    client_calls, requests, error = _press(
        button_module, FRONT, station="100000001", strategy="standard"
    )
    assert error is None
    assert client_calls == []
    assert len(requests) == 1


@pytest.mark.parametrize(
    "door, station, strategy, with_dialer",
    [
        (FRONT, "100000001", "hybrid", True),
        (FRONT, "100000001", "fast", True),
        (BACK, "100000001", "hybrid", True),
        (BACK, None, "hybrid", True),
        (BACK, "100000002", "hybrid", False),
        (BACK, "", "hybrid", True),
    ],
)
def test_other_presses_keep_the_old_unlock_path(
    button_module, door, station, strategy, with_dialer
):
    client_calls, requests, error = _press(
        button_module,
        door,
        station=station,
        strategy=strategy,
        with_dialer=with_dialer,
    )
    assert error is None
    assert client_calls == [(door,)]
    assert requests == []


def test_second_lock_press_keeps_the_old_unlock_path(button_module):
    client_calls, requests, error = _press(
        button_module, BACK, station="100000002", second_lock=True
    )
    assert error is None
    assert client_calls == [(BACK, "a")]
    assert requests == []


NO_CALL = "[abb] unlock_in_call: no call to unlock over for this station, nothing sent"


class _Services:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, object] = {}

    def has_service(self, _domain: str, service: str) -> bool:
        return service != "unlock_in_call"

    def async_register(self, _domain, service, handler, schema=None) -> None:
        self.handlers[service] = handler
        self.schemas[service] = schema


def _register_unlock_in_call(entries):
    """Register the real unlock_in_call service from __init__.py.

    ``entries`` maps an entry id to ``(dialer, client, doors)``. Returns the
    registered handler and schema.
    """
    source = _PKG_DIR / "__init__.py"
    wanted = {"_doors_for_station", "_async_register_services"}
    nodes = [
        node
        for node in ast.parse(source.read_text()).body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id.startswith("SERVICE_")
                for target in node.targets
            )
        )
    ]
    namespace = {
        "DOMAIN": const.DOMAIN,
        "vol": voluptuous,
        "callback": lambda function: function,
        "HomeAssistant": object,
        "ServiceCall": object,
        "unlock_in_active_call": intercom.unlock_in_active_call,
        "_LOGGER": redaction.get_redacting_logger("abb_unlock_in_call_test"),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(source), "exec"), namespace)

    config_entries = {
        entry_id: types.SimpleNamespace(data={"doors": list(doors)})
        for entry_id, (_, _, doors) in entries.items()
    }
    services = _Services()
    hass = types.SimpleNamespace(
        data={
            const.DOMAIN: {
                entry_id: {"intercom_dialer": dialer, "sip_client": client}
                for entry_id, (dialer, client, _) in entries.items()
            }
        },
        config_entries=types.SimpleNamespace(async_get_entry=config_entries.get),
        services=services,
    )
    namespace["_async_register_services"](hass)
    return services.handlers["unlock_in_call"], services.schemas["unlock_in_call"]


def _call_service(handler, schema, station_id, caplog):
    call = types.SimpleNamespace(data=schema({"station_id": station_id}))
    with caplog.at_level(logging.INFO):
        asyncio.run(handler(call))
    return [(record.levelno, record.getMessage()) for record in caplog.records]


def _unlock_in_call(
    caplog, station_id, *, station="100000002", scripts=(OK,), with_dialer=True
):
    """Call the service once; return (unlock_door calls, wire requests, log)."""
    client = _client()
    unlock_door_calls = []
    client.unlock_door = lambda *args: unlock_door_calls.append(args) or True
    dialer, gateway = _dialer(station, scripts)
    handler, schema = _register_unlock_in_call(
        {"synthetic-entry": (dialer if with_dialer else None, client, (FRONT, BACK))}
    )
    log = _call_service(handler, schema, station_id, caplog)
    return unlock_door_calls, gateway.requests, log


def test_service_unlocks_inside_the_call_it_holds_with_the_station(caplog):
    unlock_door_calls, requests, log = _unlock_in_call(caplog, "100000002")
    assert unlock_door_calls == []
    assert len(requests) == 1
    request_line, _, body = _parse_request(requests[0])
    assert request_line == f"MESSAGE {BACK_TARGET} SIP/2.0"
    assert body == "1"
    assert (logging.INFO, "[abb] unlock_in_call: unlock MESSAGE accepted") in log


@pytest.mark.parametrize(
    "station, with_dialer",
    [("100000001", True), (None, True), ("", True), ("100000002", False)],
)
def test_service_does_nothing_without_a_call_with_the_station(
    caplog, station, with_dialer
):
    unlock_door_calls, requests, log = _unlock_in_call(
        caplog, "100000002", station=station, with_dialer=with_dialer
    )
    assert unlock_door_calls == []
    assert requests == []
    assert (logging.INFO, NO_CALL) in log


def test_service_never_places_a_call_for_a_door_without_an_invite_route(caplog):
    unlock_door_calls, requests, log = _unlock_in_call(
        caplog, "100000001", station="100000001"
    )
    assert unlock_door_calls == []
    assert requests == []
    assert (logging.INFO, NO_CALL) in log


def test_service_with_an_unknown_station_raises_nothing_and_says_so(caplog):
    unlock_door_calls, requests, log = _unlock_in_call(caplog, "100000099")
    assert unlock_door_calls == []
    assert requests == []
    assert (
        logging.WARNING,
        "[abb] unlock_in_call: no such station, nothing sent",
    ) in log


def test_service_logs_a_message_the_gateway_refuses_and_sends_it_once(caplog):
    unlock_door_calls, requests, log = _unlock_in_call(
        caplog, "100000002", scripts=[[("486 Busy Here", {})]]
    )
    assert unlock_door_calls == []
    assert len(requests) == 1
    assert (logging.INFO, "[abb] unlock_in_call: unlock MESSAGE not accepted") in log


def test_service_uses_the_entry_that_holds_the_call(caplog):
    idle_dialer, idle_gateway = _dialer(None)
    busy_dialer, busy_gateway = _dialer("100000002")
    handler, schema = _register_unlock_in_call(
        {
            "idle": (idle_dialer, _client(), (FRONT, BACK)),
            "busy": (busy_dialer, _client(), (FRONT, BACK)),
        }
    )
    _call_service(handler, schema, "100000002", caplog)
    assert idle_gateway.requests == []
    assert len(busy_gateway.requests) == 1


def test_service_takes_one_non_empty_station_id():
    _, schema = _register_unlock_in_call(
        {"synthetic-entry": (None, _client(), (FRONT, BACK))}
    )
    assert schema({"station_id": " 100000002 "}) == {"station_id": "100000002"}
    for data in ({}, {"station_id": ""}, {"station_id": "  "}, {"station_id": None}):
        with pytest.raises(voluptuous.Invalid):
            schema(data)
