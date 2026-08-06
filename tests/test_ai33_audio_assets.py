from __future__ import annotations

import asyncio
import io
import json
import math
import urllib.error
from array import array
from pathlib import Path

import pytest
import yaml

from common.media import MediaError
from common.schema import AudioAssetManifest
from scripts import generate_ai33_audio_assets as generator


def _minimal_spec() -> generator.AudioPackSpec:
    return generator.AudioPackSpec.model_validate(
        {
            "version": 1,
            "music": [
                {"asset_id": "music-default", "mood": "default", "prompt": "Seamless neutral instrumental."},
                {"asset_id": "music-action", "mood": "action", "prompt": "Seamless action instrumental."},
            ],
            "sfx": [
                {"asset_id": "sfx-whoosh", "sfx_kind": "whoosh", "prompt": "Short whoosh."},
                {"asset_id": "sfx-impact", "sfx_kind": "impact", "prompt": "Short impact."},
                {"asset_id": "sfx-whoosh-two", "sfx_kind": "whoosh", "prompt": "Second whoosh."},
            ],
        }
    )


class _Response:
    def __init__(self, payload: dict[str, object], *, url: str = "https://api.ai33.pro/result") -> None:
        self.payload = payload
        self.url = url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def geturl(self) -> str:
        return self.url


def test_default_spec_uses_minimum_first_generation_order() -> None:
    spec = generator.load_spec(generator.DEFAULT_SPEC)

    assert [asset.asset_id for asset in generator.ordered_assets(spec)[:3]] == [
        "music-default",
        "sfx-whoosh-soft",
        "sfx-impact-tight",
    ]
    assert len(spec.music) == 7
    assert len(spec.sfx) == 6


def test_dry_run_needs_no_key_and_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("VIVOO_API_KEY", raising=False)
    output_dir = generator.WORK_ROOT / f"pytest-{tmp_path.name}"

    assert generator.main(["--dry-run", "--output-dir", str(output_dir)]) == 0
    assert not output_dir.exists()
    assert "music-default" in capsys.readouterr().out


def test_live_generation_requires_explicit_confirmation_and_env_key(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    output_dir = generator.WORK_ROOT / f"pytest-{tmp_path.name}"
    with pytest.raises(generator.AudioGenerationError, match="--confirm-live"):
        generator.main(["--output-dir", str(output_dir)])

    monkeypatch.delenv("VIVOO_API_KEY", raising=False)
    with pytest.raises(generator.AudioGenerationError, match="VIVOO_API_KEY"):
        generator.main(["--confirm-live", "--output-dir", str(output_dir)])


def test_cli_rejects_output_outside_repository_work(tmp_path: Path) -> None:
    with pytest.raises(generator.AudioGenerationError, match="must stay under"):
        generator.main(["--dry-run", "--output-dir", str(tmp_path / "assets")])


def test_ai33_submit_uses_documented_endpoints_headers_and_payloads() -> None:
    requests = []

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        requests.append((request, timeout))
        return _Response({"task_id": f"task-{len(requests)}"})

    client = generator.Ai33Client("secret", opener=opener)
    music = _minimal_spec().music[0]
    sfx = _minimal_spec().sfx[0]

    client.submit(music)
    client.submit(sfx)

    music_request, _ = requests[0]
    sfx_request, _ = requests[1]
    assert music_request.full_url.endswith("/v1s/task/music-generation")
    assert music_request.get_header("Xi-api-key") == "secret"
    assert json.loads(music_request.data)["make_instrumental"] is True
    assert json.loads(music_request.data)["create_mode"] == "simple"
    assert sfx_request.full_url.endswith("/v1/task/sound-effect")
    assert json.loads(sfx_request.data)["model_id"] == "eleven_text_to_sound_v2"
    assert json.loads(sfx_request.data)["duration_seconds"] == 1.0


def test_ai33_http_retries_retry_after_and_server_busy() -> None:
    responses: list[object] = [
        urllib.error.HTTPError(
            "https://api.ai33.pro/x", 429, "busy", {"Retry-After": "3"}, io.BytesIO(b'{"message":"busy"}')
        ),
        urllib.error.HTTPError(
            "https://api.ai33.pro/x", 503, "busy", {}, io.BytesIO(b'{"code":"server_busy"}')
        ),
        _Response({"ok": True}),
    ]
    sleeps: list[float] = []

    def opener(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = generator.Ai33Client("secret", opener=opener, sleep=sleeps.append, jitter=lambda: 0)

    assert client._request_json("/x", method="POST", body={}) == {"ok": True}
    assert sleeps == [3.0, 2.0]


@pytest.mark.parametrize(
    "network_error",
    [TimeoutError("timeout"), urllib.error.URLError("connection lost"), OSError("TLS handshake failed")],
)
def test_paid_submit_does_not_retry_ambiguous_network_failure(network_error: Exception) -> None:
    attempts = 0

    def opener(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise network_error

    client = generator.Ai33Client("secret", opener=opener, sleep=lambda _delay: None)

    with pytest.raises(generator.AudioGenerationError, match="outcome is unknown"):
        client.submit(_minimal_spec().music[0])
    assert attempts == 1


def test_paid_submit_does_not_follow_or_retry_redirect() -> None:
    attempts = 0

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        assert timeout == 60
        raise urllib.error.HTTPError(
            request.full_url,
            302,
            "redirect",
            {"Location": "https://evil.example/collect"},
            io.BytesIO(b""),
        )

    client = generator.Ai33Client("secret", opener=opener)

    with pytest.raises(generator.AudioGenerationError, match="AI33 HTTP 302"):
        client.submit(_minimal_spec().music[0])
    assert attempts == 1


def test_paid_submit_does_not_retry_unclassified_503() -> None:
    attempts = 0

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        assert timeout == 60
        attempts += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "busy",
            {},
            io.BytesIO(b'{"message":"temporary failure without classification"}'),
        )

    client = generator.Ai33Client("secret", opener=opener)

    with pytest.raises(generator.AudioGenerationError, match="AI33 HTTP 503"):
        client.submit(_minimal_spec().music[0])
    assert attempts == 1


def test_api_key_echo_is_redacted_from_exception_and_report(tmp_path: Path) -> None:
    api_key = "do-not-store-this-key"

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 60
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "bad request",
            {},
            io.BytesIO(json.dumps({"message": f"invalid credential {api_key}"}).encode("utf-8")),
        )

    client = generator.Ai33Client(api_key, opener=opener)
    with pytest.raises(generator.AudioGenerationError) as captured:
        client.submit(_minimal_spec().music[0])
    assert api_key not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)

    report = generator.run_live(
        _minimal_spec(),
        output_dir=tmp_path,
        api_key=api_key,
        max_credit_spend=10000,
        resume=False,
        force_assets=set(),
        client=client,
    )
    report_text = (tmp_path / "generation_report.json").read_text(encoding="utf-8")
    assert report["status"] == "partial"
    assert api_key not in report_text
    assert "[REDACTED]" in report_text


def test_download_retries_rate_limit_and_sends_cdn_headers(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []
    captured_headers: dict[str, str] = {}

    class DownloadResponse:
        def __init__(self) -> None:
            self.sent = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def geturl(self) -> str:
            return "https://cdn.ai33.pro/generated.mp3"

        def read(self, _size: int = -1) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b"audio"

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        captured_headers.update(dict(request.header_items()))
        if attempts == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "busy", {"Retry-After": "2"}, io.BytesIO(b'{"message":"busy"}')
            )
        assert timeout == 180
        return DownloadResponse()

    output = tmp_path / "audio.mp3"
    generator.download_audio(
        "https://cdn.ai33.pro/generated.mp3",
        output,
        "secret",
        opener=opener,
        sleep=sleeps.append,
        jitter=lambda: 0,
    )

    assert output.read_bytes() == b"audio"
    assert attempts == 2
    assert sleeps == [2.0]
    assert captured_headers["Xi-api-key"] == "secret"
    assert captured_headers["User-agent"] == "Mozilla/5.0"


def test_download_redirect_drops_key_before_public_suno_host(tmp_path: Path) -> None:
    requests = []

    class DownloadResponse:
        def __init__(self) -> None:
            self.sent = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def geturl(self) -> str:
            return "https://cdn1.suno.ai/generated.mp3"

        def read(self, _size: int = -1) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b"audio"

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 180
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "redirect",
                {"Location": "https://cdn1.suno.ai/generated.mp3"},
                io.BytesIO(b""),
            )
        return DownloadResponse()

    output = tmp_path / "audio.mp3"
    generator.download_audio("https://cdn.ai33.pro/start.mp3", output, "secret", opener=opener)

    assert requests[0].get_header("Xi-api-key") == "secret"
    assert requests[1].get_header("Xi-api-key") is None
    assert requests[1].get_header("User-agent") == "Mozilla/5.0"
    assert output.read_bytes() == b"audio"


def test_download_rejects_redirect_to_untrusted_host_before_request(tmp_path: Path) -> None:
    requests = []

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 180
        requests.append(request)
        raise urllib.error.HTTPError(
            request.full_url,
            302,
            "redirect",
            {"Location": "https://evil.example/steal.mp3"},
            io.BytesIO(b""),
        )

    with pytest.raises(generator.AudioGenerationError, match="not allowlisted"):
        generator.download_audio("https://cdn.ai33.pro/start.mp3", tmp_path / "audio.mp3", "secret", opener=opener)
    assert len(requests) == 1


def test_poll_task_observes_same_task_until_done() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.responses = iter(
                [
                    {"id": "task-1", "status": "doing"},
                    {"id": "task-1", "status": "done", "metadata": {"audio_url": "https://cdn1.suno.ai/x.mp3"}},
                ]
            )

        def get_task(self, task_id: str) -> dict[str, object]:
            self.calls.append(task_id)
            return next(self.responses)

        def redact(self, value: str) -> str:
            return value

    client = Client()

    payload = asyncio.run(generator.poll_task(client, "task-1", timeout_s=5, interval_s=0))  # type: ignore[arg-type]

    assert payload["status"] == "done"
    assert client.calls == ["task-1", "task-1"]


def test_poll_task_continues_same_task_after_real_temporary_503() -> None:
    requests = []
    responses: list[object] = [
        urllib.error.HTTPError(
            "https://api.ai33.pro/v1/task/task-1",
            503,
            "busy",
            {},
            io.BytesIO(b'{"message":"Task polling temporarily busy"}'),
        ),
        _Response(
            {
                "id": "task-1",
                "status": "done",
                "credit_cost": 100,
                "metadata": {"audio_url": "https://cdn1.suno.ai/task-1.mp3"},
            }
        ),
    ]

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        requests.append(request)
        assert timeout == 60
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = generator.Ai33Client("secret", opener=opener, max_attempts=1)
    payload = asyncio.run(generator.poll_task(client, "task-1", timeout_s=5, interval_s=0))

    assert payload["status"] == "done"
    assert len(requests) == 2
    assert {request.full_url for request in requests} == {"https://api.ai33.pro/v1/task/task-1"}


def test_poll_task_continues_same_task_after_network_retry_exhaustion() -> None:
    requests = []
    responses: list[object] = [
        urllib.error.URLError("temporary connection loss"),
        _Response(
            {
                "id": "task-1",
                "status": "done",
                "credit_cost": 100,
                "metadata": {"audio_url": "https://cdn1.suno.ai/task-1.mp3"},
            }
        ),
    ]

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        requests.append(request)
        assert timeout == 60
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = generator.Ai33Client("secret", opener=opener, max_attempts=1)
    payload = asyncio.run(generator.poll_task(client, "task-1", timeout_s=5, interval_s=0))

    assert payload["status"] == "done"
    assert len(requests) == 2
    assert {request.full_url for request in requests} == {"https://api.ai33.pro/v1/task/task-1"}


def _mock_media(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(generator, "download_audio", lambda _url, path, _key: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"raw")))
    monkeypatch.setattr(generator, "inspect_audio", lambda _path: (1, 60.0))

    def normalize(_raw, output, asset, _duration):  # type: ignore[no-untyped-def]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"normalized:{asset.asset_id}".encode("ascii"))
        return 0.01 if isinstance(asset, generator.MusicSpec) else None

    monkeypatch.setattr(generator, "normalize_generated_audio", normalize)
    monkeypatch.setattr(generator, "measure_loop_seam_ratio", lambda _path: 0.01)


def test_credit_cap_keeps_minimum_partial_pack_and_safe_manifest(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)
    submitted: list[str] = []

    class Client:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            submitted.append(asset.asset_id)
            return {"task_id": asset.asset_id, "endpoint": "endpoint", "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        cost = 9900 if task_id == "music-default" else 50
        return {
            "status": "done",
            "credit_cost": cost,
            "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"},
        }

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    report = generator.run_live(
        _minimal_spec(),
        output_dir=tmp_path,
        api_key="do-not-persist-this-key",
        max_credit_spend=10000,
        resume=False,
        force_assets=set(),
        client=Client(),  # type: ignore[arg-type]
    )

    assert submitted == ["music-default", "sfx-whoosh", "sfx-impact"]
    assert report["status"] == "partial"
    assert report["credits_spent"] == 10000
    assert report["minimum_pack_ready"] is True
    report_text = (tmp_path / "generation_report.json").read_text(encoding="utf-8")
    assert "do-not-persist-this-key" not in report_text
    manifest_payload = yaml.safe_load((tmp_path / "audio_assets.test.yaml").read_text(encoding="utf-8"))
    manifest = AudioAssetManifest.model_validate(manifest_payload)
    assert len(manifest.assets) == 3
    assert {asset.license_source for asset in manifest.assets} == {generator.LICENSE_SOURCE}
    assert (tmp_path / "NOT_FOR_PRODUCTION.txt").is_file()


def test_missing_canary_cost_stops_all_later_submissions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)
    submitted: list[str] = []

    class Client:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            submitted.append(asset.asset_id)
            return {"task_id": asset.asset_id, "endpoint": "endpoint", "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        return {"status": "done", "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"}}

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    report = generator.run_live(
        _minimal_spec(),
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=10000,
        resume=False,
        force_assets=set(),
        client=Client(),  # type: ignore[arg-type]
    )

    assert submitted == ["music-default"]
    assert report["status"] == "partial"
    assert report["observed_music_credit_cost"] is None


def test_resume_repairs_corrupt_file_without_resubmitting_task(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)

    class FirstClient:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            endpoint = (
                "/v1s/task/music-generation"
                if isinstance(asset, generator.MusicSpec)
                else "/v1/task/sound-effect"
            )
            return {"task_id": asset.asset_id, "endpoint": endpoint, "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        cost = 9900 if task_id == "music-default" else 50
        return {
            "status": "done",
            "credit_cost": cost,
            "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"},
        }

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    spec = _minimal_spec()
    generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=10000,
        resume=False,
        force_assets=set(),
        client=FirstClient(),  # type: ignore[arg-type]
    )
    (tmp_path / "music" / "music-default.mp3").write_bytes(b"corrupt")

    class ResumeClient:
        def submit(self, _asset):  # type: ignore[no-untyped-def]
            raise AssertionError("resume must poll the journaled task instead of resubmitting")

    report = generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=10000,
        resume=True,
        force_assets=set(),
        client=ResumeClient(),  # type: ignore[arg-type]
    )

    assert (tmp_path / "music" / "music-default.mp3").read_bytes() == b"normalized:music-default"
    assert report["minimum_pack_ready"] is True


def test_resume_known_canary_cost_clears_budget_block_and_submits_minimum_sfx(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)
    spec = _minimal_spec()
    ordered = generator.ordered_assets(spec)
    records = {asset.asset_id: generator._new_record(asset) for asset in ordered}
    records["music-default"].update(status="failed", task_id="journaled-canary", error="temporary polling failure")
    for asset in ordered[1:]:
        records[asset.asset_id].update(status="skipped_budget", error="credit cap prevents another task submission")
    generator._write_report(
        tmp_path / "generation_report.json",
        {
            "version": 1,
            "status": "partial",
            "max_credit_spend": 10000,
            "historical_credit_spend": 0,
            "credits_spent": 0,
            "credit_spend_complete": False,
            "observed_music_credit_cost": None,
            "minimum_pack_ready": False,
            "assets": [records[asset.asset_id] for asset in ordered],
        },
    )
    submitted: list[str] = []
    polled: list[str] = []

    class Client:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            assert asset.asset_id != "music-default"
            submitted.append(asset.asset_id)
            endpoint = (
                "/v1s/task/music-generation"
                if isinstance(asset, generator.MusicSpec)
                else "/v1/task/sound-effect"
            )
            return {"task_id": asset.asset_id, "endpoint": endpoint, "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        polled.append(task_id)
        cost = 9900 if task_id == "journaled-canary" else 50
        return {
            "status": "done",
            "credit_cost": cost,
            "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"},
        }

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    report = generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=10000,
        resume=True,
        force_assets=set(),
        client=Client(),  # type: ignore[arg-type]
    )

    assert polled[0] == "journaled-canary"
    assert polled.count("journaled-canary") == 1
    assert submitted == ["sfx-whoosh", "sfx-impact"]
    assert report["observed_music_credit_cost"] == 9900
    assert report["credit_spend_complete"] is True
    assert report["minimum_pack_ready"] is True
    assert report["credits_spent"] == 10000


def test_media_failure_is_recorded_without_aborting_pack(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)
    submitted: list[str] = []

    class Client:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            submitted.append(asset.asset_id)
            endpoint = (
                "/v1s/task/music-generation"
                if isinstance(asset, generator.MusicSpec)
                else "/v1/task/sound-effect"
            )
            return {"task_id": asset.asset_id, "endpoint": endpoint, "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        cost = 9900 if task_id == "music-default" else 50
        return {
            "status": "done",
            "credit_cost": cost,
            "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"},
        }

    def normalize(_raw, output, asset, _duration):  # type: ignore[no-untyped-def]
        if asset.asset_id == "music-default":
            raise MediaError("encoder failed")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"normalized")
        return None

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    monkeypatch.setattr(generator, "normalize_generated_audio", normalize)

    report = generator.run_live(
        _minimal_spec(),
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=10000,
        resume=False,
        force_assets=set(),
        client=Client(),  # type: ignore[arg-type]
    )

    records = {record["asset_id"]: record for record in report["assets"]}
    assert submitted == ["music-default", "sfx-whoosh", "sfx-impact"]
    assert records["music-default"]["status"] == "failed"
    assert "encoder failed" in records["music-default"]["error"]
    assert report["status"] == "partial"


def _generate_complete_pack(tmp_path: Path, monkeypatch) -> generator.AudioPackSpec:  # type: ignore[no-untyped-def]
    _mock_media(monkeypatch)

    class Client:
        def submit(self, asset):  # type: ignore[no-untyped-def]
            endpoint = (
                "/v1s/task/music-generation"
                if isinstance(asset, generator.MusicSpec)
                else "/v1/task/sound-effect"
            )
            return {"task_id": asset.asset_id, "endpoint": endpoint, "response": {}}

    async def fake_poll(_client, task_id, **_kwargs):  # type: ignore[no-untyped-def]
        cost = 100 if task_id.startswith("music-") else 50
        return {
            "status": "done",
            "credit_cost": cost,
            "metadata": {"audio_url": f"https://cdn1.suno.ai/{task_id}.mp3"},
        }

    monkeypatch.setattr(generator, "poll_task", fake_poll)
    spec = _minimal_spec()
    report = generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=350,
        resume=False,
        force_assets=set(),
        client=Client(),  # type: ignore[arg-type]
    )
    assert report["status"] == "complete"
    assert report["credits_spent"] == 350
    return spec


def test_force_asset_retains_lifetime_spend_exactly_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spec = _generate_complete_pack(tmp_path, monkeypatch)

    class NoSubmitClient:
        def submit(self, _asset):  # type: ignore[no-untyped-def]
            raise AssertionError("lifetime credit cap must block the forced replacement")

    first_resume = generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=350,
        resume=True,
        force_assets={"music-default"},
        client=NoSubmitClient(),  # type: ignore[arg-type]
    )
    second_resume = generator.run_live(
        spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=350,
        resume=True,
        force_assets={"music-default"},
        client=NoSubmitClient(),  # type: ignore[arg-type]
    )

    assert first_resume["historical_credit_spend"] == 100
    assert first_resume["credits_spent"] == 350
    assert second_resume["historical_credit_spend"] == 100
    assert second_resume["credits_spent"] == 350
    default_record = next(record for record in second_resume["assets"] if record["asset_id"] == "music-default")
    assert default_record["status"] == "skipped_budget"


def test_spec_change_retires_prior_cost_without_resetting_cap(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spec = _generate_complete_pack(tmp_path, monkeypatch)
    changed_payload = spec.model_dump(mode="json")
    changed_payload["music"][1]["prompt"] = "A materially changed action prompt."
    changed_spec = generator.AudioPackSpec.model_validate(changed_payload)

    class NoSubmitClient:
        def submit(self, _asset):  # type: ignore[no-untyped-def]
            raise AssertionError("lifetime credit cap must block regeneration after a spec change")

    report = generator.run_live(
        changed_spec,
        output_dir=tmp_path,
        api_key="secret",
        max_credit_spend=350,
        resume=True,
        force_assets=set(),
        client=NoSubmitClient(),  # type: ignore[arg-type]
    )

    assert report["historical_credit_spend"] == 100
    assert report["credits_spent"] == 350
    action_record = next(record for record in report["assets"] if record["asset_id"] == "music-action")
    assert action_record["status"] == "skipped_budget"


def test_normalization_builds_loop_crossfade_and_pcm_sfx(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    commands: list[list[str]] = []

    def fake_run(command):  # type: ignore[no-untyped-def]
        commands.append(command)
        Path(command[-1]).write_bytes(b"audio")

    monkeypatch.setattr(generator, "run_command", fake_run)
    monkeypatch.setattr(generator, "measure_loop_seam_ratio", lambda _path: 0.1)
    raw = tmp_path / "raw.mp3"
    raw.write_bytes(b"raw")
    music_output = tmp_path / "music.mp3"
    sfx_output = tmp_path / "sfx.wav"
    spec = _minimal_spec()

    assert generator.normalize_generated_audio(raw, music_output, spec.music[0], 60.0) == 0.1
    assert generator.normalize_generated_audio(raw, sfx_output, spec.sfx[0], 1.0) is None

    assert "acrossfade=d=1.5" in commands[0][commands[0].index("-filter_complex") + 1]
    assert "libmp3lame" in commands[0]
    assert "pcm_s16le" in commands[1]
    assert all("48000" in command for command in commands)


def test_windowed_loop_seam_metric_accepts_continuity_and_rejects_jump() -> None:
    sample_rate = 48000
    frame_count = int(sample_rate * 0.05)
    values = [int(1000 * math.sin(2 * math.pi * 440 * index / sample_rate)) for index in range(frame_count * 2)]

    def stereo(frames: list[int]) -> array[int]:
        return array("h", [sample for value in frames for sample in (value, value)])

    tail = stereo(values[:frame_count])
    continuous_head = stereo(values[frame_count:])
    jumped_head = stereo([value + 20000 for value in values[frame_count:]])

    pass_ratio = generator.boundary_discontinuity_ratio(tail, continuous_head, sample_rate=sample_rate)
    fail_ratio = generator.boundary_discontinuity_ratio(tail, jumped_head, sample_rate=sample_rate)

    assert pass_ratio < generator.LOOP_SEAM_MAX_DISCONTINUITY_RATIO
    assert fail_ratio > generator.LOOP_SEAM_MAX_DISCONTINUITY_RATIO
