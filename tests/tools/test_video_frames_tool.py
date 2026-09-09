"""Unit tests for the video_frames tool.

The two ffmpeg legs (``_probe_duration`` → ffprobe, ``_extract_frame`` → ffmpeg)
are stubbed; these tests pin the timestamp planning, the argv construction that
keeps the model's input out of a shell, the read guards on the source path,
error shaping, and registration into its own RBAC toolset.
"""
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

import tools.video_frames_tool as vf


def _run(args):
    from model_tools import _run_async

    return json.loads(_run_async(vf._video_frames_handler(args)))


@pytest.fixture
def mp4(tmp_path):
    p = tmp_path / "demo.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake bytes")
    return p


@pytest.fixture
def stub_ffmpeg(monkeypatch, tmp_path):
    """Install both ffmpeg legs, recording every extraction request."""
    calls = []

    async def fake_probe(path):
        return 60.0

    async def fake_extract(path, seconds, out_path, max_width):
        calls.append(
            {"path": str(path), "seconds": seconds,
             "out": Path(out_path), "max_width": max_width}
        )
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n frame")

    monkeypatch.setattr(vf, "_probe_duration", fake_probe)
    monkeypatch.setattr(vf, "_extract_frame", fake_extract)
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))
    return calls


def _out_dir(tmp_path):
    """Stand in for _frames_output_dir, which creates the dir it returns."""
    def _dir():
        path = tmp_path / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _dir


# ─── timestamp planning ──────────────────────────────────────────────────────


def test_samples_evenly_spaced_frames_when_no_timestamps_given(mp4, stub_ffmpeg):
    """The agent cannot know the duration up front, so a bare call must still
    produce a spread across the whole video rather than failing or grabbing 0s."""
    result = _run({"video_path": str(mp4)})

    assert result["success"] is True
    assert [c["seconds"] for c in stub_ffmpeg] == [5.0, 15.0, 25.0, 35.0, 45.0, 55.0]


def test_explicit_timestamps_win_over_even_sampling(mp4, stub_ffmpeg):
    result = _run({"video_path": str(mp4), "timestamps": [3, 42.5]})

    assert result["success"] is True
    assert [c["seconds"] for c in stub_ffmpeg] == [3.0, 42.5]


def test_clock_style_timestamps_are_parsed_to_seconds(mp4, stub_ffmpeg):
    _run({"video_path": str(mp4), "timestamps": ["0:05", "0:45.5", "00:00:12"]})

    assert [c["seconds"] for c in stub_ffmpeg] == [5.0, 45.5, 12.0]


@pytest.mark.parametrize("text,expected", [
    ("12", 12.0),
    ("12.5", 12.5),
    ("1:05", 65.0),
    ("1:05.5", 65.5),
    ("01:02:03", 3723.0),
    ("2:00:00", 7200.0),
])
def test_parse_timestamp_forms(text, expected):
    assert vf._parse_timestamp(text) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["halfway", "-5", "1:2:3:4", "", "1:xx", True])
def test_parse_timestamp_rejects_junk(bad):
    with pytest.raises(ValueError):
        vf._parse_timestamp(bad)


def test_unparseable_timestamp_is_rejected(mp4, stub_ffmpeg):
    result = _run({"video_path": str(mp4), "timestamps": ["halfway"]})

    assert "error" in result
    assert "halfway" in result["error"]
    assert stub_ffmpeg == []


def test_count_is_clamped_to_the_frame_ceiling(mp4, stub_ffmpeg):
    result = _run({"video_path": str(mp4), "count": 500})

    assert len(stub_ffmpeg) == vf._MAX_FRAMES
    assert result["frames"][0]["index"] == 0


def test_too_many_explicit_timestamps_are_rejected(mp4, stub_ffmpeg):
    result = _run({
        "video_path": str(mp4),
        "timestamps": list(range(vf._MAX_FRAMES + 1)),
    })

    assert "error" in result
    assert str(vf._MAX_FRAMES) in result["error"]
    assert stub_ffmpeg == []


def test_timestamp_past_the_end_is_reported_without_losing_the_good_frames(
    mp4, stub_ffmpeg
):
    """One bad timestamp must not throw away the frames that did extract."""
    result = _run({"video_path": str(mp4), "timestamps": [10, 900]})

    assert result["success"] is True
    assert [f["timestamp_seconds"] for f in result["frames"]] == [10.0]
    assert len(result["skipped"]) == 1
    assert "900" in result["skipped"][0]["reason"]
    assert "60" in result["skipped"][0]["reason"]


# ─── output ──────────────────────────────────────────────────────────────────


def test_frames_are_written_to_disk_and_returned_as_absolute_paths(
    mp4, stub_ffmpeg, tmp_path
):
    """vision_analyze and webflow_asset_upload both take a path, so the frames
    have to land somewhere readable and be reported back by absolute path."""
    result = _run({"video_path": str(mp4), "count": 2})

    assert len(result["frames"]) == 2
    for frame in result["frames"]:
        path = Path(frame["path"])
        assert path.is_absolute()
        assert path.exists()
        assert frame["size_bytes"] == path.stat().st_size
    assert result["duration_seconds"] == 60.0


def test_max_width_is_passed_through_to_the_scaler(mp4, stub_ffmpeg):
    _run({"video_path": str(mp4), "count": 1, "max_width": 1200})

    assert stub_ffmpeg[0]["max_width"] == 1200


def test_absurd_max_width_is_rejected(mp4, stub_ffmpeg):
    result = _run({"video_path": str(mp4), "max_width": 99999})

    assert "error" in result
    assert stub_ffmpeg == []


# ─── source guards ───────────────────────────────────────────────────────────


def test_missing_file_is_an_error(tmp_path, stub_ffmpeg):
    result = _run({"video_path": str(tmp_path / "nope.mp4")})

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_non_video_extension_is_refused(tmp_path, stub_ffmpeg):
    doc = tmp_path / "secrets.env"
    doc.write_text("TOKEN=1")

    result = _run({"video_path": str(doc)})

    assert "error" in result
    assert ".env" in result["error"] or "unsupported" in result["error"].lower()
    assert stub_ffmpeg == []


def test_protected_path_is_refused(mp4, stub_ffmpeg, monkeypatch):
    """The cross-user read guard must gate this tool too — it writes a copy of
    whatever it reads into a cache other tools happily publish to a CDN."""
    def boom(path):
        raise ValueError("other users' data")

    monkeypatch.setattr(vf, "raise_if_read_blocked", boom)

    result = _run({"video_path": str(mp4)})

    assert "error" in result
    assert "other users' data" in result["error"]
    assert stub_ffmpeg == []


def test_file_url_prefix_is_accepted(mp4, stub_ffmpeg):
    result = _run({"video_path": f"file://{mp4}", "count": 1})

    assert result["success"] is True
    assert stub_ffmpeg[0]["path"] == str(mp4)


# ─── ffmpeg plumbing ─────────────────────────────────────────────────────────


def test_missing_ffmpeg_gives_an_actionable_error(mp4, monkeypatch, tmp_path):
    async def missing(*a, **kw):
        raise vf.FfmpegMissingError(vf._FFMPEG_MISSING_HINT)

    monkeypatch.setattr(vf, "_probe_duration", missing)
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))

    result = _run({"video_path": str(mp4)})

    assert "error" in result
    assert "ffmpeg" in result["error"].lower()
    assert "install" in result["error"].lower()


def test_absent_binary_is_translated_into_the_install_hint(monkeypatch, mp4, tmp_path):
    """exec raises a bare FileNotFoundError when the binary is gone; that must
    not reach the model as an unexplained path error."""
    async def no_binary(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr(vf.asyncio, "create_subprocess_exec", no_binary)
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))

    result = _run({"video_path": str(mp4)})

    assert "error" in result
    assert "install" in result["error"].lower()


def test_every_extraction_failure_surfaces_as_an_error(mp4, monkeypatch, tmp_path):
    async def fake_probe(path):
        return 60.0

    async def always_fails(path, seconds, out_path, max_width):
        raise vf.FrameExtractionError("ffmpeg: Invalid data found")

    monkeypatch.setattr(vf, "_probe_duration", fake_probe)
    monkeypatch.setattr(vf, "_extract_frame", always_fails)
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))

    result = _run({"video_path": str(mp4), "count": 2})

    assert "error" in result
    assert "Invalid data" in result["error"]


def test_ffmpeg_argv_never_reaches_a_shell(monkeypatch, mp4, tmp_path):
    """Everything here is model-supplied. The command must be a fixed argv list
    handed to exec — never a string, never shell=True."""
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return FakeProc()

    monkeypatch.setattr(vf.asyncio, "create_subprocess_exec", fake_exec)

    out_path = tmp_path / "frame.png"
    from model_tools import _run_async

    _run_async(vf._extract_frame(mp4, 12.5, out_path, 800))

    argv = seen["argv"]
    assert all(isinstance(part, str) for part in argv)
    assert argv[0] == "ffmpeg"
    assert "shell" not in seen["kwargs"]
    # the timestamp went in as its own argv entry, not spliced into a string
    assert "12.5" in argv
    assert str(out_path) == argv[-1]
    scale = [part for part in argv if "scale" in part]
    assert scale and "800" in scale[0]
    # scales DOWN only — a 640px-wide source must not be blown up to 800
    assert "min(" in scale[0]


def test_probe_duration_reads_ffprobe_output(monkeypatch, mp4):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"93.482000\n", b""

    async def fake_exec(*argv, **kwargs):
        assert argv[0] == "ffprobe"
        return FakeProc()

    monkeypatch.setattr(vf.asyncio, "create_subprocess_exec", fake_exec)

    from model_tools import _run_async

    assert _run_async(vf._probe_duration(mp4)) == pytest.approx(93.482)


# ─── registration ────────────────────────────────────────────────────────────


def test_registered_in_its_own_toolset():
    """A separate toolset is the whole point — reading frames out of a user's
    upload is not implied by any other grant."""
    from tools.registry import registry

    entry = registry.get_entry("video_frames")
    assert entry is not None
    assert entry.toolset == "video_frames"


def test_toolset_exists_so_rbac_can_gate_it():
    import toolsets

    assert "video_frames" in toolsets.get_all_toolsets()
    assert "video_frames" in toolsets.TOOLSETS["video_frames"]["tools"]


# ─── end to end (real ffmpeg) ────────────────────────────────────────────────

ffmpeg_required = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed on this host",
)


@ffmpeg_required
def test_end_to_end_against_real_ffmpeg(tmp_path, monkeypatch):
    """Every other test stubs the subprocess, so nothing above proves the argv
    ffmpeg actually receives is valid. This one runs it for real."""
    video = tmp_path / "real.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x180:rate=10:duration=4",
         "-pix_fmt", "yuv420p", "-y", str(video)],
        check=True,
    )
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))

    result = _run({"video_path": str(video), "count": 2, "max_width": 160})

    assert result["success"] is True
    assert result["duration_seconds"] == pytest.approx(4.0, abs=0.3)
    assert len(result["frames"]) == 2
    for frame in result["frames"]:
        data = Path(frame["path"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a real PNG"
        width, height = struct.unpack(">II", data[16:24])
        assert width == 160, "max_width was not applied"
        assert height == 90, "aspect ratio was not preserved"


@ffmpeg_required
def test_real_ffmpeg_never_upscales_a_smaller_source(tmp_path, monkeypatch):
    video = tmp_path / "small.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=200x100:rate=10:duration=2",
         "-pix_fmt", "yuv420p", "-y", str(video)],
        check=True,
    )
    monkeypatch.setattr(vf, "_frames_output_dir", _out_dir(tmp_path))

    result = _run({"video_path": str(video), "count": 1, "max_width": 4000})

    data = Path(result["frames"][0]["path"]).read_bytes()
    width, _height = struct.unpack(">II", data[16:24])
    assert width == 200, "a 200px source was blown up to the requested ceiling"
