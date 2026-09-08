"""Tests for the per-media-id talkback PCM cache and its in-flight dedup."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"


def _install_homeassistant_stubs() -> None:
    homeassistant = sys.modules.setdefault(
        "homeassistant", types.ModuleType("homeassistant")
    )
    homeassistant.__path__ = []
    components = sys.modules.setdefault(
        "homeassistant.components", types.ModuleType("homeassistant.components")
    )
    components.__path__ = []
    ffmpeg = types.ModuleType("homeassistant.components.ffmpeg")
    ffmpeg.get_ffmpeg_manager = lambda hass: hass.ffmpeg
    sys.modules[ffmpeg.__name__] = ffmpeg
    media_source = types.ModuleType("homeassistant.components.media_source")
    media_source.async_resolve_media = None
    sys.modules[media_source.__name__] = media_source
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules[core.__name__] = core
    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    sys.modules[exceptions.__name__] = exceptions
    helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    helpers.__path__ = []
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.session
    sys.modules[aiohttp_client.__name__] = aiohttp_client
    network = types.ModuleType("homeassistant.helpers.network")
    network.get_url = lambda hass, **kwargs: "http://127.0.0.1:8123"
    sys.modules[network.__name__] = network


def _load_audio() -> types.ModuleType:
    _install_homeassistant_stubs()
    pkg = sys.modules.setdefault("abb_welcome", types.ModuleType("abb_welcome"))
    pkg.__path__ = [str(_PKG_DIR)]
    full = "abb_welcome.talkback_audio"
    sys.modules.pop(full, None)
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "talkback_audio.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


audio = _load_audio()


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}


def test_second_call_for_the_same_media_id_does_not_rerun_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _resolver(_hass, media_id):
        nonlocal calls
        calls += 1
        return b"pcm-" + media_id.encode()

    monkeypatch.setattr(audio, "async_prepare_talkback_audio", _resolver)

    async def scenario() -> tuple[bytes, bytes]:
        hass = _Hass()
        first = await audio.async_prepare_talkback_audio_cached(hass, "tts://a")
        second = await audio.async_prepare_talkback_audio_cached(hass, "tts://a")
        return first, second

    first, second = asyncio.run(scenario())
    assert first == b"pcm-tts://a"
    assert second == b"pcm-tts://a"
    assert calls == 1


def test_concurrent_calls_for_the_same_id_run_the_resolver_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _resolver(_hass, media_id):
        nonlocal calls
        calls += 1
        # Yield once so the second concurrent caller runs its own lookup
        # while this one is still "synthesising" - it must find the future
        # already in the cache rather than starting a second resolve.
        await asyncio.sleep(0)
        return b"pcm-" + media_id.encode()

    monkeypatch.setattr(audio, "async_prepare_talkback_audio", _resolver)

    async def scenario() -> tuple[bytes, bytes]:
        hass = _Hass()
        return await asyncio.gather(
            audio.async_prepare_talkback_audio_cached(hass, "tts://b"),
            audio.async_prepare_talkback_audio_cached(hass, "tts://b"),
        )

    first, second = asyncio.run(scenario())
    assert first == b"pcm-tts://b"
    assert second == b"pcm-tts://b"
    assert calls == 1


def test_a_resolver_exception_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _resolver(_hass, _media_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise audio.HomeAssistantError("cold synth failed")
        return b"pcm-ok"

    monkeypatch.setattr(audio, "async_prepare_talkback_audio", _resolver)

    async def scenario() -> bytes:
        hass = _Hass()
        with pytest.raises(audio.HomeAssistantError, match="cold synth failed"):
            await audio.async_prepare_talkback_audio_cached(hass, "tts://c")
        return await audio.async_prepare_talkback_audio_cached(hass, "tts://c")

    result = asyncio.run(scenario())
    assert result == b"pcm-ok"
    assert calls == 2


def test_cache_evicts_beyond_16_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolver(_hass, media_id):
        return b"pcm-" + media_id.encode()

    monkeypatch.setattr(audio, "async_prepare_talkback_audio", _resolver)

    async def scenario() -> dict:
        hass = _Hass()
        for i in range(17):
            await audio.async_prepare_talkback_audio_cached(hass, f"tts://{i}")
        return hass.data[audio._PCM_CACHE_DATA_KEY]

    cache = asyncio.run(scenario())
    assert len(cache) == 16
    assert "tts://0" not in cache
    assert "tts://1" in cache
    assert "tts://16" in cache
