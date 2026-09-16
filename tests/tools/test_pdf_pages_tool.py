"""Unit tests for the pdf_pages tool.

The two poppler legs (``_probe_page_count`` → pdfinfo, ``_render_page`` →
pdftoppm) are stubbed; these tests pin page-range planning, the caps, the read
guards on the source path, error shaping, and registration into its own RBAC
toolset — the same contract test_video_frames_tool.py pins for videos.
"""
import json
from pathlib import Path

import pytest

import tools.pdf_pages_tool as pp


def _run(args):
    from model_tools import _run_async

    return json.loads(_run_async(pp._pdf_pages_handler(args)))


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "statement.pdf"
    p.write_bytes(b"%PDF-1.4 fake bytes")
    return p


def _out_dir(tmp_path):
    def _dir():
        path = tmp_path / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return _dir


@pytest.fixture
def stub_poppler(monkeypatch, tmp_path):
    """Install both poppler legs; a 3-page document unless a test overrides."""
    calls = []

    async def fake_count(path):
        return 3

    async def fake_render(path, page, out_path, dpi):
        calls.append({"path": str(path), "page": page, "out": Path(out_path), "dpi": dpi})
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n page")

    monkeypatch.setattr(pp, "_probe_page_count", fake_count)
    monkeypatch.setattr(pp, "_render_page", fake_render)
    monkeypatch.setattr(pp, "_pages_output_dir", _out_dir(tmp_path))
    return calls


# ─── registration ────────────────────────────────────────────────────────────


def test_registered_under_its_own_toolset():
    import toolsets
    from tools.registry import registry
    assert registry.get_toolset_for_tool("pdf_pages") == "pdf_pages"
    assert toolsets.TOOLSETS["pdf_pages"]["tools"] == ["pdf_pages"]
    assert registry.get_tool_names_for_toolset("pdf_pages") == ["pdf_pages"]


# ─── page planning ───────────────────────────────────────────────────────────


def test_bare_call_renders_every_page(pdf, stub_poppler):
    result = _run({"pdf_path": str(pdf)})
    assert result["success"] is True
    assert [c["page"] for c in stub_poppler] == [1, 2, 3]
    assert result["page_count"] == 3


@pytest.mark.parametrize("spec,expected", [
    ("2-3", [2, 3]),
    ("1,3", [1, 3]),
    ("3,1", [1, 3]),          # sorted, deduplicated
    ("1-2,2-3", [1, 2, 3]),
    ("2", [2]),
])
def test_page_spec_selects_pages(pdf, stub_poppler, spec, expected):
    result = _run({"pdf_path": str(pdf), "pages": spec})
    assert result["success"] is True
    assert [c["page"] for c in stub_poppler] == expected


@pytest.mark.parametrize("spec", ["0", "a-b", "3-1", "1--2", "-1", ""])
def test_bad_page_spec_is_an_error(pdf, stub_poppler, spec):
    result = _run({"pdf_path": str(pdf), "pages": spec})
    assert "error" in result
    assert stub_poppler == []


def test_page_past_the_end_is_reported_without_losing_the_good_pages(pdf, stub_poppler):
    result = _run({"pdf_path": str(pdf), "pages": "2,9"})
    assert result["success"] is True
    assert [c["page"] for c in stub_poppler] == [2]
    assert result["skipped"][0]["page"] == 9
    assert "3" in result["skipped"][0]["reason"]   # names the real page count


def test_more_pages_than_the_cap_is_truncated_with_a_note(pdf, stub_poppler, monkeypatch):
    async def many(path):
        return 50
    monkeypatch.setattr(pp, "_probe_page_count", many)
    result = _run({"pdf_path": str(pdf)})
    assert result["success"] is True
    assert len(stub_poppler) == pp._MAX_PAGES
    assert [c["page"] for c in stub_poppler] == list(range(1, pp._MAX_PAGES + 1))
    assert "note" in result and str(pp._MAX_PAGES) in result["note"]


def test_pages_are_written_to_disk_and_returned_as_absolute_paths(pdf, stub_poppler):
    result = _run({"pdf_path": str(pdf), "pages": "1-2"})
    assert len(result["pages"]) == 2
    for entry in result["pages"]:
        path = Path(entry["path"])
        assert path.is_absolute() and path.is_file()
        assert path.suffix == ".png"
        assert entry["size_bytes"] == path.stat().st_size
    assert [e["page"] for e in result["pages"]] == [1, 2]


# ─── dpi ─────────────────────────────────────────────────────────────────────


def test_default_and_explicit_dpi(pdf, stub_poppler):
    _run({"pdf_path": str(pdf), "pages": "1"})
    assert stub_poppler[0]["dpi"] == pp._DEFAULT_DPI
    _run({"pdf_path": str(pdf), "pages": "1", "dpi": 200})
    assert stub_poppler[1]["dpi"] == 200


@pytest.mark.parametrize("dpi", [10, 1200, "high"])
def test_absurd_dpi_is_rejected(pdf, stub_poppler, dpi):
    result = _run({"pdf_path": str(pdf), "dpi": dpi})
    assert "error" in result
    assert stub_poppler == []


# ─── source guards ───────────────────────────────────────────────────────────


def test_missing_file_is_an_error(tmp_path, stub_poppler):
    result = _run({"pdf_path": str(tmp_path / "nope.pdf")})
    assert "error" in result
    assert "not found" in result["error"].lower()


def test_non_pdf_extension_is_refused(tmp_path, stub_poppler):
    doc = tmp_path / "secrets.env"
    doc.write_text("TOKEN=1")
    result = _run({"pdf_path": str(doc)})
    assert "error" in result
    assert stub_poppler == []


def test_protected_path_is_refused_before_existence_is_revealed(tmp_path, stub_poppler, monkeypatch):
    def boom(path):
        raise ValueError("other users' data")
    monkeypatch.setattr(pp, "raise_if_read_blocked", boom)
    result = _run({"pdf_path": str(tmp_path / "does-not-exist.pdf")})
    assert "error" in result
    assert "other users' data" in result["error"]
    assert "not found" not in result["error"].lower()
    assert stub_poppler == []


def test_file_url_prefix_is_accepted(pdf, stub_poppler):
    result = _run({"pdf_path": f"file://{pdf}", "pages": "1"})
    assert result["success"] is True
    assert stub_poppler[0]["path"] == str(pdf)


# ─── poppler plumbing ────────────────────────────────────────────────────────


def test_absent_binary_is_translated_into_the_install_hint(monkeypatch, pdf, tmp_path):
    async def no_binary(*argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "pdfinfo")
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", no_binary)
    monkeypatch.setattr(pp, "_pages_output_dir", _out_dir(tmp_path))
    result = _run({"pdf_path": str(pdf)})
    assert "error" in result
    assert "poppler" in result["error"].lower()
    assert "install" in result["error"].lower()


def test_every_render_failure_surfaces_as_an_error(pdf, monkeypatch, tmp_path):
    async def fake_count(path):
        return 2

    async def always_fails(path, page, out_path, dpi):
        raise pp.PageRenderError("pdftoppm: Syntax Error")
    monkeypatch.setattr(pp, "_probe_page_count", fake_count)
    monkeypatch.setattr(pp, "_render_page", always_fails)
    monkeypatch.setattr(pp, "_pages_output_dir", _out_dir(tmp_path))
    result = _run({"pdf_path": str(pdf)})
    assert "error" in result
    assert "Syntax Error" in result["error"]


def test_argv_is_fixed_and_never_a_shell_string(pdf, monkeypatch, tmp_path):
    """The model controls the path and the page number; both must arrive as
    separate argv elements, not be interpolated into a command line."""
    seen = []

    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))

        class _P:
            returncode = 0
            async def communicate(self):
                if argv[0] == "pdfinfo":
                    return b"Title: x\nPages:          4\n", b""
                # pdftoppm -singlefile writes <prefix>.png
                Path(argv[-1] + ".png").write_bytes(b"png")
                return b"", b""
        return _P()
    monkeypatch.setattr(pp.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pp, "_pages_output_dir", _out_dir(tmp_path))
    evil = tmp_path / "a; rm -rf ~.pdf"
    evil.write_bytes(b"%PDF")
    result = _run({"pdf_path": str(evil), "pages": "2"})
    assert result["success"] is True
    info, render = seen
    assert info[0] == "pdfinfo" and info[-1] == str(evil)
    assert render[0] == "pdftoppm" and "-singlefile" in render
    assert render[render.index("-f") + 1] == "2" and render[render.index("-l") + 1] == "2"
    assert str(evil) in render          # whole path is one element
    assert not any(";" in a and a != str(evil) for a in render)


def test_check_fn_requires_both_binaries(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    assert pp._check_poppler_available() is True
    monkeypatch.setattr(shutil, "which", lambda b: None if b == "pdftoppm" else "/usr/bin/" + b)
    assert pp._check_poppler_available() is False
