from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest

from tts.__main__ import build_parser, run_tts_with_client
from tts.capcut import CapCutClient, load_voice_catalog
from tts.capcut import signer
from tts.capcut.config import DEFAULT_DEVICE, TTS_SIGN_PUBLIC_KEY_PEM
from tts.providers import (
    CAPCUT_MODEL,
    CapCutProvider,
    ProviderResult,
    TtsProviderClient,
    TtsProviderError,
    resolve_capcut_voice,
    resolve_provider_order,
)

VOICE = "BV074_streaming"
VOICE_RESOURCE_ID = "7102355709945188865"
AUDIO_BYTES = b"ID3-capcut-mp3"


def ok_create(task_id: str = "t1") -> httpx.Response:
    return httpx.Response(200, json={"ret": "0", "data": {"tasks": [{"id": task_id, "token": "tok1"}]}})


def succeed_query() -> httpx.Response:
    payload = {"audio_subtitles": [{"speech_url": "https://cdn.test/a.mp3", "duration": 1500}]}
    return httpx.Response(
        200,
        json={"ret": "0", "data": {"tasks": [{"id": "t1", "status": "succeed", "payload": json.dumps(payload)}]}},
    )


def busy_create() -> httpx.Response:
    return httpx.Response(200, json={"ret": "1", "errmsg": "server_busy, please retry"})


class FakeCapCut:
    """httpx MockTransport handler that replays queued responses per CapCut endpoint."""

    def __init__(self, create: list[httpx.Response], query: list[httpx.Response] | None = None) -> None:
        self.create = list(create)
        self.query = list(query or [succeed_query()])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/common_task/new"):
            return self._next(self.create)
        if request.url.path.endswith("/common_task/query"):
            return self._next(self.query)
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=AUDIO_BYTES)
        return httpx.Response(404)

    @staticmethod
    def _next(queue: list[httpx.Response]) -> httpx.Response:
        template = queue.pop(0) if len(queue) > 1 else queue[0]
        # Fresh copy per request: httpx binds a response to the request that received it.
        return httpx.Response(template.status_code, headers=template.headers, content=template.content)

    def count(self, suffix: str) -> int:
        return sum(1 for request in self.requests if request.url.path.endswith(suffix))


def make_provider(fake: FakeCapCut, sleeps: list[float], **kwargs) -> CapCutProvider:  # type: ignore[no-untyped-def]
    return CapCutProvider(
        lambda: CapCutClient(client=httpx.Client(transport=httpx.MockTransport(fake))),
        sleep=sleeps.append,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_client_sleep(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # In-client short retries and task polling must not slow the suite down.
    monkeypatch.setattr("tts.capcut.client.time.sleep", lambda _seconds: None)


def freeze_signer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(signer.secrets, "token_bytes", lambda n: b"\x01" * n)
    monkeypatch.setattr(signer.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(signer.uuid, "uuid4", lambda: uuid.UUID("12345678123456781234567812345678"))


def test_signer_is_deterministic_with_fixed_seed_and_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    freeze_signer(monkeypatch)

    sign = signer.make_tts_payload_sign(
        "<speak>xin chào</speak>", '{"benefit_info":{}}', DEFAULT_DEVICE["device_id"], DEFAULT_DEVICE["aid"]
    )
    headers = signer.base_headers(DEFAULT_DEVICE, '{"a":1}', appid=True)

    # Golden values were cross-checked against trans-zh src/zhsub/dub/capcut/signer.py under the same freeze.
    assert sign == signer.make_tts_payload_sign(
        "<speak>xin chào</speak>", '{"benefit_info":{}}', DEFAULT_DEVICE["device_id"], DEFAULT_DEVICE["aid"]
    )
    assert hashlib.sha256(sign.encode("ascii")).hexdigest() == (
        "b76e9b36599d9cc065b58ef885dfa3607e798ba80b2c7a4bd9b00843890da0d3"
    )
    assert signer.make_sign_header(
        "https://editor-api-sg.capcutapi.com/lv/v1/common_task/new?a=1", "8.7.0", "1700000000", DEFAULT_DEVICE["tdid"]
    ) == "5846627a68f5bbf16d522f46dd6b7a6d"
    assert headers["x-ss-stub"] == "bb6cb5c68df4652941caf652a366f2d8"
    assert headers["device-time"] == headers["x-khronos"] == "1700000000"
    assert headers["x-tt-trace-id"] == "00-12345678123456781234567812345678-1234567812345678-01"
    assert headers["appid"] == DEFAULT_DEVICE["aid"]


def test_signer_rsa_output_is_pkcs1_v15_encryption_of_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    freeze_signer(monkeypatch)
    modulus, exponent = signer.rsa_public_numbers_from_pem(TTS_SIGN_PUBLIC_KEY_PEM)
    key_len = (modulus.bit_length() + 7) // 8
    message = b"appid:359289&did:1&creditDisable:false"

    ciphertext = base64.b64decode(signer.rsa_encrypt_pkcs1v15(message))

    encoded = b"\x00\x02" + b"\x01" * (key_len - len(message) - 3) + b"\x00" + message
    assert len(ciphertext) == key_len
    assert int.from_bytes(ciphertext, "big") == pow(int.from_bytes(encoded, "big"), exponent, modulus)


def test_packaged_voice_catalog_contains_default_voice() -> None:
    voices = {voice.voice_type: voice for voice in load_voice_catalog()}

    assert voices[VOICE].resource_id == VOICE_RESOURCE_ID
    assert voices[VOICE].lang == "vi-VN"


def test_capcut_provider_success_writes_mp3_with_resolved_voice(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCapCut(create=[ok_create()])
    sleeps: list[float] = []
    output = tmp_path / "raw" / "0.mp3"

    result = make_provider(fake, sleeps).synthesize(text="Xin chào & tạm biệt", voice_id=VOICE, speed=1.0, output_path=output)

    assert result == ProviderResult(
        provider="capcut", voice_id=VOICE, audio_url="https://cdn.test/a.mp3", model=CAPCUT_MODEL
    )
    assert output.read_bytes() == AUDIO_BYTES
    assert sleeps == []
    create_request = next(request for request in fake.requests if request.url.path.endswith("/common_task/new"))
    assert create_request.headers["sign"]
    payload = json.loads(json.loads(create_request.content)["tasks"][0]["payload"])
    assert f'name="{VOICE}"' in payload["ssml"]
    assert f'resource_id="{VOICE_RESOURCE_ID}"' in payload["ssml"]
    assert '<prosody rate="1.00">Xin chào &amp; tạm biệt</prosody>' in payload["ssml"]
    assert payload["audio_format"] == "mp3"


@pytest.mark.parametrize(
    "busy_responses",
    [[busy_create()], [httpx.Response(503)] * 5],
    ids=["ret-server-busy", "http-503-exhausted-in-client"],
)
def test_capcut_provider_retries_server_busy_then_succeeds(tmp_path, busy_responses) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCapCut(create=[*busy_responses, ok_create()])
    sleeps: list[float] = []

    result = make_provider(fake, sleeps).synthesize(
        text="Câu chuyện tiếp tục.", voice_id=VOICE, speed=1.0, output_path=tmp_path / "0.mp3"
    )

    assert result.provider == "capcut"
    assert (tmp_path / "0.mp3").read_bytes() == AUDIO_BYTES
    assert fake.count("/common_task/new") == len(busy_responses) + 1
    assert len(sleeps) == 1
    assert 3.0 <= sleeps[0] < 4.0


def test_capcut_provider_backs_off_exponentially_then_gives_up(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCapCut(create=[busy_create()])
    sleeps: list[float] = []

    with pytest.raises(TtsProviderError, match="still failing after 3 attempts"):
        make_provider(fake, sleeps, max_attempts=3).synthesize(
            text="Mở đầu.", voice_id=VOICE, speed=1.0, output_path=tmp_path / "0.mp3"
        )

    assert fake.count("/common_task/new") == 3
    assert [int(value) for value in sleeps] == [3, 6]


def test_capcut_task_failure_is_not_retried(tmp_path) -> None:  # type: ignore[no-untyped-def]
    failed = httpx.Response(200, json={"ret": "0", "data": {"tasks": [{"id": "t1", "status": "failed"}]}})
    fake = FakeCapCut(create=[ok_create()], query=[failed])
    sleeps: list[float] = []

    with pytest.raises(TtsProviderError, match="CapCut task t1 failed: CapCut TTS task failed"):
        make_provider(fake, sleeps).synthesize(text="Mở đầu.", voice_id=VOICE, speed=1.0, output_path=tmp_path / "0.mp3")

    assert fake.count("/common_task/new") == 1
    assert fake.count("/common_task/query") == 1
    assert sleeps == []
    assert not (tmp_path / "0.mp3").exists()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(401, text="unauthorized"), httpx.Response(200, json={"ret": "1001", "errmsg": "invalid sign"})],
    ids=["http-401", "ret-invalid-sign"],
)
def test_capcut_auth_and_validation_errors_fail_without_retry(tmp_path, response) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCapCut(create=[response])
    sleeps: list[float] = []

    with pytest.raises(TtsProviderError, match="CapCut task creation failed"):
        make_provider(fake, sleeps).synthesize(text="Mở đầu.", voice_id=VOICE, speed=1.0, output_path=tmp_path / "0.mp3")

    assert fake.count("/common_task/new") == 1
    assert sleeps == []


def test_unknown_capcut_voice_fails_before_any_request(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fake = FakeCapCut(create=[ok_create()])

    with pytest.raises(TtsProviderError, match="Unknown CapCut voice: 'not_a_voice'"):
        resolve_provider_order("capcut", voice_id="not_a_voice", genmax_voice_id=None, environ={})
    with pytest.raises(TtsProviderError, match="Unknown CapCut voice"):
        make_provider(fake, []).synthesize(text="Mở đầu.", voice_id="not_a_voice", speed=1.0, output_path=tmp_path / "0.mp3")

    assert fake.requests == []


def test_resolve_capcut_voice_accepts_display_name_and_rejects_ambiguous_names() -> None:
    assert resolve_capcut_voice("Cô Gái Hoạt Ngôn").voice_type == VOICE
    assert resolve_capcut_voice("bv074_streaming").resource_id == VOICE_RESOURCE_ID
    with pytest.raises(TtsProviderError, match="ambiguous"):
        resolve_capcut_voice("Trickster")


def test_provider_mode_capcut_is_explicit_and_never_in_auto() -> None:
    all_keys = {"VIVOO_API_KEY": "k", "GENMAX_API_KEY": "k", "OPENAI_API_KEY": "k"}

    assert resolve_provider_order("capcut", voice_id=VOICE, genmax_voice_id=None, environ={}) == ["capcut"]
    assert "capcut" not in resolve_provider_order("auto", voice_id=VOICE, genmax_voice_id="gx", environ=all_keys)
    with pytest.raises(TtsProviderError, match="provider_mode=auto requires"):
        resolve_provider_order("auto", voice_id=VOICE, genmax_voice_id=None, environ={})
    args = build_parser().parse_args(
        ["--review-script", "r.json", "--output-audio", "v.mp3", "--output-timing", "t.json",
         "--voice-id", VOICE, "--provider-mode", "capcut"]
    )
    assert args.provider_mode == "capcut"


class RecordingClient(TtsProviderClient):
    def __init__(self, fail_capcut: bool = False) -> None:
        self.fail_capcut = fail_capcut
        self.calls: list[tuple[str, str]] = []

    async def _synthesize_capcut(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        self.calls.append(("capcut", voice_id))
        if self.fail_capcut:
            raise TtsProviderError("capcut unavailable")
        output_path.write_bytes(AUDIO_BYTES)
        voice = resolve_capcut_voice(voice_id)
        return ProviderResult(provider="capcut", voice_id=voice.voice_type, audio_url="https://cdn.test/a.mp3", model=CAPCUT_MODEL)

    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        raise AssertionError("ai33 must not be called for provider_mode=capcut")

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        raise AssertionError("genmax must not be called for provider_mode=capcut")

    async def _synthesize_openai(self, text: str, voice_id: str, model: str, speed: float, output_path: Path) -> ProviderResult:
        raise AssertionError("openai must not be called for provider_mode=capcut")


def test_capcut_failure_does_not_fall_back_to_paid_providers(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VIVOO_API_KEY", "ai33")
    monkeypatch.setenv("GENMAX_API_KEY", "genmax")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    client = RecordingClient(fail_capcut=True)

    with pytest.raises(TtsProviderError, match="capcut: capcut unavailable"):
        asyncio.run(
            client.synthesize(
                text="Mở đầu.",
                voice_id=VOICE,
                genmax_voice_id="gx",
                model="m",
                speed=1.0,
                provider_mode="capcut",
                output_path=tmp_path / "0.mp3",
            )
        )

    assert client.calls == [("capcut", VOICE)]


def write_review_script(tmp_path: Path) -> Path:
    data = [
        {"beat_id": 0, "narration": "Mở đầu căng thẳng.", "from_seg_id": 0, "to_seg_id": 0,
         "src_tc_start": 1.0, "src_tc_end": 2.0, "is_hook": True},
        {"beat_id": 1, "narration": "Câu chuyện tiếp tục.", "from_seg_id": 1, "to_seg_id": 1,
         "src_tc_start": 2.0, "src_tc_end": 3.0, "is_hook": False},
    ]
    path = tmp_path / "review_script.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_tts_cli_capcut_cache_follows_voice_and_speed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    review_script = write_review_script(tmp_path)
    monkeypatch.setattr("tts.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("tts.__main__.normalize_audio", lambda src, dst: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr("tts.__main__.concat_voiceover", lambda paths, pause, work_dir, output: output.write_bytes(b"vo"))
    monkeypatch.setattr("tts.__main__.probe_duration", lambda path: 1.0)
    for key in ("VIVOO_API_KEY", "GENMAX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def run(voice_id: str, speed: str = "1.0") -> tuple[RecordingClient, object]:
        args = build_parser().parse_args([
            "--review-script", str(review_script),
            "--output-audio", str(tmp_path / "out" / "voiceover.mp3"),
            "--output-timing", str(tmp_path / "out" / "beats_timing.json"),
            "--work-dir", str(tmp_path / "work" / "tts"),
            "--voice-id", voice_id,
            "--provider-mode", "capcut",
            "--speed", speed,
        ])
        client = RecordingClient()
        _timings, meta = asyncio.run(run_tts_with_client(args, client))
        return client, meta

    first, meta = run(VOICE)
    cached, cached_meta = run(VOICE)
    changed_voice, changed_meta = run("BV421_vivn_streaming")
    changed_speed, _ = run("BV421_vivn_streaming", speed="1.1")

    assert len(first.calls) == 2
    assert cached.calls == []
    assert cached_meta.cache_hits == ["audio/0.mp3", "audio/1.mp3"]
    assert changed_voice.calls == [("capcut", "BV421_vivn_streaming")] * 2
    assert len(changed_speed.calls) == 2
    assert (meta.provider_mode, meta.voice_id, meta.model) == ("capcut", VOICE, CAPCUT_MODEL)
    assert meta.provider_counts == {"capcut": 2}
    assert meta.fallback_count == 0
    assert changed_meta.voice_id == "BV421_vivn_streaming"
    manifest = json.loads((tmp_path / "work" / "tts" / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["provider"] for entry in manifest.values()} == {"capcut"}
    assert {entry["voice_id"] for entry in manifest.values()} == {"BV421_vivn_streaming"}
