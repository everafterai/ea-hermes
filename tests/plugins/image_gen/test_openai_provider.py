"""Tests for the bundled OpenAI image_gen plugin (gpt-image-2, three tiers)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import plugins.image_gen.openai as openai_plugin


# 1×1 transparent PNG — valid bytes for save_b64_image()
_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _b64_png() -> str:
    import base64
    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


def _fake_response(*, b64=None, url=None, revised_prompt=None):
    item = SimpleNamespace(b64_json=b64, url=url, revised_prompt=revised_prompt)
    return SimpleNamespace(data=[item])


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return openai_plugin.OpenAIImageGenProvider()


def _patched_openai(fake_client: MagicMock):
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value = fake_client
    return patch.dict("sys.modules", {"openai": fake_openai})


# ── Metadata ────────────────────────────────────────────────────────────────


class TestMetadata:
    def test_name(self, provider):
        assert provider.name == "openai"

    def test_default_model(self, provider):
        assert provider.default_model() == "gpt-image-2-medium"

    def test_list_models_three_tiers(self, provider):
        ids = [m["id"] for m in provider.list_models()]
        assert ids == ["gpt-image-2-low", "gpt-image-2-medium", "gpt-image-2-high"]

    def test_catalog_entries_have_display_speed_strengths(self, provider):
        for entry in provider.list_models():
            assert entry["display"].startswith("GPT Image 2")
            assert entry["speed"]
            assert entry["strengths"]


# ── Availability ────────────────────────────────────────────────────────────


class TestAvailability:
    def test_no_api_key_unavailable(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert openai_plugin.OpenAIImageGenProvider().is_available() is False

    def test_api_key_set_available(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        assert openai_plugin.OpenAIImageGenProvider().is_available() is True


# ── Model resolution ────────────────────────────────────────────────────────


class TestModelResolution:
    def test_default_is_medium(self):
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-medium"
        assert meta["quality"] == "medium"

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2-high")
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-high"
        assert meta["quality"] == "high"

    def test_env_var_unknown_falls_back(self, monkeypatch):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", "bogus-tier")
        model_id, _ = openai_plugin._resolve_model()
        assert model_id == openai_plugin.DEFAULT_MODEL

    def test_config_openai_model(self, tmp_path):
        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"image_gen": {"openai": {"model": "gpt-image-2-low"}}})
        )
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-low"
        assert meta["quality"] == "low"

    def test_config_top_level_model(self, tmp_path):
        """``image_gen.model: gpt-image-2-high`` also works (top-level)."""
        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"image_gen": {"model": "gpt-image-2-high"}})
        )
        model_id, meta = openai_plugin._resolve_model()
        assert model_id == "gpt-image-2-high"
        assert meta["quality"] == "high"


# ── Generate ────────────────────────────────────────────────────────────────


class TestGenerate:
    def test_empty_prompt_rejected(self, provider):
        result = provider.generate("", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = openai_plugin.OpenAIImageGenProvider().generate("a cat")
        assert result["success"] is False
        assert result["error_type"] == "auth_required"

    def test_b64_saves_to_cache(self, provider, tmp_path):
        png_bytes = bytes.fromhex(_PNG_HEX)
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            result = provider.generate("a cat", aspect_ratio="landscape")

        assert result["success"] is True
        assert result["model"] == "gpt-image-2-medium"
        assert result["aspect_ratio"] == "landscape"
        assert result["provider"] == "openai"
        assert result["quality"] == "medium"

        saved = Path(result["image"])
        assert saved.exists()
        assert saved.parent == tmp_path / "cache" / "images"
        assert saved.read_bytes() == png_bytes

        call_kwargs = fake_client.images.generate.call_args.kwargs
        # All tiers hit the single underlying API model.
        assert call_kwargs["model"] == "gpt-image-2"
        assert call_kwargs["quality"] == "medium"
        assert call_kwargs["size"] == "1536x1024"
        # gpt-image-2 rejects response_format — we must NOT send it.
        assert "response_format" not in call_kwargs

    @pytest.mark.parametrize("tier,expected_quality", [
        ("gpt-image-2-low", "low"),
        ("gpt-image-2-medium", "medium"),
        ("gpt-image-2-high", "high"),
    ])
    def test_tier_maps_to_quality(self, provider, monkeypatch, tier, expected_quality):
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", tier)
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["model"] == tier
        assert result["quality"] == expected_quality
        assert fake_client.images.generate.call_args.kwargs["quality"] == expected_quality
        # Always the same underlying API model regardless of tier.
        assert fake_client.images.generate.call_args.kwargs["model"] == "gpt-image-2"

    @pytest.mark.parametrize("aspect,expected_size", [
        ("landscape", "1536x1024"),
        ("square", "1024x1024"),
        ("portrait", "1024x1536"),
    ])
    def test_aspect_ratio_mapping(self, provider, aspect, expected_size):
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(fake_client):
            provider.generate("a cat", aspect_ratio=aspect)

        assert fake_client.images.generate.call_args.kwargs["size"] == expected_size

    def test_revised_prompt_passed_through(self, provider):
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=_b64_png(), revised_prompt="A photo of a cat",
        )

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["revised_prompt"] == "A photo of a cat"

    def test_api_error_returns_error_response(self, provider):
        fake_client = MagicMock()
        fake_client.images.generate.side_effect = RuntimeError("boom")

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "boom" in result["error"]

    def test_empty_response_data(self, provider):
        fake_client = MagicMock()
        fake_client.images.generate.return_value = SimpleNamespace(data=[])

        with _patched_openai(fake_client):
            result = provider.generate("a cat")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_url_response_is_cached_locally(self, provider):
        """OpenAI URL response (if API ever returns one) is cached locally.

        Pre-fix this asserted the bare URL passed through; symmetric to the
        xAI #26942 fix.  Even though gpt-image-2 returns b64 today, every
        ``image_gen`` provider must guarantee the gateway gets a stable
        file path so ephemeral signed URLs can't expire mid-flight.
        """
        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=None, url="https://example.com/img.png",
        )

        with _patched_openai(fake_client), patch(
            "plugins.image_gen.openai.save_url_image",
            return_value=Path("/tmp/openai_gpt-image-2_20260524_000000_deadbeef.png"),
        ) as mock_save_url:
            result = provider.generate("a cat")

        assert result["success"] is True
        assert result["image"].startswith("/")
        assert "example.com" not in result["image"]
        mock_save_url.assert_called_once()

    def test_url_response_falls_back_to_bare_url_when_download_fails(self, provider):
        """Cache failure must not turn into a tool error — symmetric with xAI."""
        import requests as req_lib

        fake_client = MagicMock()
        fake_client.images.generate.return_value = _fake_response(
            b64=None, url="https://example.com/img.png",
        )

        with _patched_openai(fake_client), patch(
            "plugins.image_gen.openai.save_url_image",
            side_effect=req_lib.HTTPError("404 from CDN"),
        ):
            result = provider.generate("a cat")

        assert result["success"] is True
        assert result["image"] == "https://example.com/img.png"


# ── _normalize_references ───────────────────────────────────────────────────


class TestNormalizeReferences:
    """`_normalize_references()` resolves path/URL/data-URI strings to open
    file handles. Returns ``list[tuple[BinaryIO, str]]`` (handle, filename).
    Raises ``ValueError`` with a message prefixed ``reference_images[<idx>]``
    on any bad input; handles opened so far are closed before the exception
    propagates."""

    def test_local_path_returns_handle(self, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))
        handles = openai_plugin._normalize_references([str(img)])
        try:
            assert len(handles) == 1
            handle, filename = handles[0]
            assert hasattr(handle, "read")
            assert filename.endswith(".png")
            assert handle.read(8).startswith(b"\x89PNG")
        finally:
            for h, _ in handles:
                h.close()

    def test_local_path_missing_raises_with_index(self, tmp_path):
        bad = tmp_path / "nope.png"
        with pytest.raises(ValueError) as excinfo:
            openai_plugin._normalize_references([str(bad)])
        msg = str(excinfo.value)
        assert "reference_images[0]" in msg
        assert "not found" in msg.lower()

    def test_local_path_too_large_raises(self, tmp_path):
        big = tmp_path / "big.png"
        big.write_bytes(b"\x00" * (26 * 1024 * 1024))  # 26MB > 25MB cap
        with pytest.raises(ValueError) as excinfo:
            openai_plugin._normalize_references([str(big)])
        msg = str(excinfo.value)
        assert "reference_images[0]" in msg
        assert "25" in msg or "too large" in msg.lower()

    def test_data_uri_returns_handle(self):
        b64 = _b64_png()
        uri = f"data:image/png;base64,{b64}"
        handles = openai_plugin._normalize_references([uri])
        try:
            assert len(handles) == 1
            handle, filename = handles[0]
            assert filename.endswith(".png")
            assert handle.read(8).startswith(b"\x89PNG")
        finally:
            for h, _ in handles:
                h.close()

    def test_data_uri_malformed_raises(self):
        with pytest.raises(ValueError) as excinfo:
            openai_plugin._normalize_references(["data:not-an-image"])
        msg = str(excinfo.value)
        assert "reference_images[0]" in msg
        assert "malformed" in msg.lower() or "invalid" in msg.lower()

    def test_https_url_uses_save_url_image(self, monkeypatch, tmp_path):
        """URLs are routed through ``save_url_image``. Patch it to avoid
        any real network call."""
        saved = tmp_path / "from_url.png"
        saved.write_bytes(bytes.fromhex(_PNG_HEX))
        seen = {}

        def fake_save(url, *, prefix, timeout=60.0, max_bytes=25 * 1024 * 1024):
            seen["url"] = url
            seen["prefix"] = prefix
            return saved

        monkeypatch.setattr(openai_plugin, "save_url_image", fake_save)
        handles = openai_plugin._normalize_references(["https://example/x.png"])
        try:
            assert seen["url"] == "https://example/x.png"
            assert seen["prefix"].startswith("openai_ref")
            handle, _ = handles[0]
            assert handle.read(8).startswith(b"\x89PNG")
        finally:
            for h, _ in handles:
                h.close()

    def test_error_index_is_correct(self, tmp_path):
        """When the second entry fails, the message must say [1], not [0]."""
        good = tmp_path / "good.png"
        good.write_bytes(bytes.fromhex(_PNG_HEX))
        bad = tmp_path / "nope.png"
        with pytest.raises(ValueError) as excinfo:
            openai_plugin._normalize_references([str(good), str(bad)])
        assert "reference_images[1]" in str(excinfo.value)


# ── Dispatch + image-to-image ───────────────────────────────────────────────


class TestImageToImage:
    """``generate()`` dispatches to ``client.images.edit`` when
    ``reference_images`` is provided, and back to ``client.images.generate``
    when it isn't. Normalization errors are mapped to structured
    ``error_type`` values the agent can act on."""

    def test_no_refs_calls_generate(self, provider):
        client = MagicMock()
        client.images.generate.return_value = _fake_response(b64=_b64_png())
        with _patched_openai(client):
            result = provider.generate(prompt="cat")
        assert result["success"] is True
        assert client.images.generate.called
        assert not client.images.edit.called

    def test_refs_present_calls_edit_not_generate(self, provider, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))
        client = MagicMock()
        client.images.edit.return_value = _fake_response(b64=_b64_png())
        with _patched_openai(client):
            result = provider.generate(
                prompt="make it blue", reference_images=[str(img)]
            )
        assert result["success"] is True
        assert client.images.edit.called
        assert not client.images.generate.called

    def test_edit_payload_shape(self, provider, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))
        client = MagicMock()
        client.images.edit.return_value = _fake_response(b64=_b64_png())
        with _patched_openai(client):
            provider.generate(
                prompt="make it blue",
                aspect_ratio="square",
                reference_images=[str(img)],
            )
        kwargs = client.images.edit.call_args.kwargs
        assert kwargs["model"] == openai_plugin.API_MODEL
        assert kwargs["prompt"] == "make it blue"
        assert kwargs["size"] == openai_plugin._SIZES["square"]
        assert kwargs["quality"] == "medium"  # default tier
        assert kwargs["n"] == 1
        assert "response_format" not in kwargs  # gpt-image-2 rejects it
        assert isinstance(kwargs["image"], list)
        assert len(kwargs["image"]) == 1

    def test_edit_response_b64_saved_to_cache(self, provider, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))
        client = MagicMock()
        client.images.edit.return_value = _fake_response(b64=_b64_png())
        with _patched_openai(client):
            result = provider.generate(
                prompt="make it blue", reference_images=[str(img)]
            )
        assert result["success"] is True
        assert Path(result["image"]).exists()
        assert result["image"].endswith(".png")
        assert result["provider"] == "openai"
        assert result.get("reference_count") == 1

    def test_missing_path_returns_reference_not_found(self, provider, tmp_path):
        missing = tmp_path / "missing.png"
        client = MagicMock()
        with _patched_openai(client):
            result = provider.generate(
                prompt="anything", reference_images=[str(missing)]
            )
        assert result["success"] is False
        assert result["error_type"] == "reference_not_found"
        assert "reference_images[0]" in result["error"]
        assert not client.images.edit.called

    def test_malformed_data_uri_returns_reference_invalid(self, provider):
        client = MagicMock()
        with _patched_openai(client):
            result = provider.generate(
                prompt="anything", reference_images=["data:bogus"]
            )
        assert result["success"] is False
        assert result["error_type"] == "reference_invalid"
        assert not client.images.edit.called

    def test_oversized_path_returns_reference_too_large(self, provider, tmp_path):
        big = tmp_path / "big.png"
        big.write_bytes(b"\x00" * (26 * 1024 * 1024))
        client = MagicMock()
        with _patched_openai(client):
            result = provider.generate(
                prompt="anything", reference_images=[str(big)]
            )
        assert result["success"] is False
        assert result["error_type"] == "reference_too_large"
        assert not client.images.edit.called

    def test_url_fetch_failure_returns_reference_fetch_failed(
        self, provider, monkeypatch
    ):
        def fake_save_url(url, *, prefix, timeout=60.0, max_bytes=25 * 1024 * 1024):
            raise RuntimeError("404 not found")

        monkeypatch.setattr(openai_plugin, "save_url_image", fake_save_url)
        client = MagicMock()
        with _patched_openai(client):
            result = provider.generate(
                prompt="anything",
                reference_images=["https://example/missing.png"],
            )
        assert result["success"] is False
        assert result["error_type"] == "reference_fetch_failed"
        assert not client.images.edit.called

    def test_api_failure_returns_api_error(self, provider, tmp_path):
        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))
        client = MagicMock()
        client.images.edit.side_effect = RuntimeError("rate limit")
        with _patched_openai(client):
            result = provider.generate(
                prompt="anything", reference_images=[str(img)]
            )
        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "rate limit" in result["error"]


class TestEndToEndSmoke:
    """End-to-end: real file on disk → provider → mocked images.edit →
    success result with a cached output. Exercises the full happy-path
    chain in one shot to catch wiring regressions the unit tests miss."""

    def test_provider_happy_path_with_real_file(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        img = tmp_path / "ref.png"
        img.write_bytes(bytes.fromhex(_PNG_HEX))

        client = MagicMock()
        client.images.edit.return_value = _fake_response(b64=_b64_png())

        with _patched_openai(client):
            provider = openai_plugin.OpenAIImageGenProvider()
            result = provider.generate(
                prompt="add a red hat",
                reference_images=[str(img)],
            )

        assert result["success"] is True
        assert result["provider"] == "openai"
        assert result["reference_count"] == 1
        assert Path(result["image"]).exists()
        assert client.images.edit.called
        # Confirm the file handle that was passed was actually readable
        # (not a closed handle from premature cleanup).
        kwargs = client.images.edit.call_args.kwargs
        # The handle was closed in our finally — but the test inspects the
        # call args, which capture the object reference. The important
        # invariant is that exactly one handle was supplied.
        assert isinstance(kwargs["image"], list)
        assert len(kwargs["image"]) == 1
