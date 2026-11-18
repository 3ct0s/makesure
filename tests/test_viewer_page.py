"""Viewer page checks that need no browser: assets, CSP compatibility, JS protocol logic."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from makesure.agent.capture import TestPatternCapture
from makesure.agent.encoder import H264Encoder, JpegEncoder
from makesure.common.protocol import FrameHeader

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "makesure" / "viewer" / "static"
NODE = shutil.which("node")


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.inline_handlers: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "script":
            if values.get("src"):
                self.refs.append(values["src"] or "")
            else:
                self.inline_scripts += 1
        if tag == "link" and values.get("href"):
            self.refs.append(values["href"] or "")
        if tag == "style" or "style" in values:
            self.inline_styles += 1
        self.inline_handlers += [name for name in values if name.startswith("on")]


def test_page_is_self_contained_and_csp_clean() -> None:
    collector = _Collector()
    collector.feed((STATIC / "index.html").read_text())
    assert collector.refs and all(ref.startswith("/static/") for ref in collector.refs)
    assert collector.inline_scripts == 0 and collector.inline_styles == 0
    assert collector.inline_handlers == []
    for ref in collector.refs:
        assert (STATIC / ref.removeprefix("/static/")).is_file()
    # every element the script looks up exists in the page
    script = (STATIC / "viewer.js").read_text()
    used_ids = set(re.findall(r'\$\("([\w-]+)"\)', script))
    assert used_ids and used_ids <= collector.ids, used_ids - collector.ids


def test_no_external_urls_or_html_injection_sinks() -> None:
    for path in STATIC.iterdir():
        text = path.read_text()
        assert not re.search(r"https?://(?!localhost)", text), path.name
        assert chr(0x2014) not in text, f"em dash in {path.name}"
    script = (STATIC / "viewer.js").read_text()
    assert "innerHTML" not in script and "eval(" not in script


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_viewer_js_parses() -> None:
    for name in ("viewer.js", "protocol.js"):
        subprocess.run([NODE, "--check", str(STATIC / name)], check=True, capture_output=True)  # type: ignore[list-item]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_protocol_matches_python(tmp_path: Path) -> None:
    capture = TestPatternCapture(640, 360)
    h264 = H264Encoder(640, 360, 15, 1500, 2.0, "ultrafast")
    key = h264.encode(capture.grab(), True)
    delta = h264.encode(capture.grab(), False)
    jpeg = JpegEncoder(320, 180, 15, 800).encode(capture.grab(), False)
    assert key and delta and jpeg
    ts = 1_763_456_789_123
    frames = [
        FrameHeader(h264.codec, True, 41, ts, 640, 360).pack() + key.payload,
        FrameHeader(h264.codec, False, 42, ts + 66, 640, 360).pack() + delta.payload,
        FrameHeader(JpegEncoder.codec, True, 4_000_000_000, ts, 320, 180).pack() + jpeg.payload,
        b"XX" + bytes(40),
    ]
    frames_file = tmp_path / "frames.json"
    frames_file.write_text(json.dumps([f.hex() for f in frames]))
    out = subprocess.run(
        [
            NODE,
            str(ROOT / "tests" / "js" / "protocol_check.mjs"),
            str(STATIC / "protocol.js"),
            str(frames_file),
        ],  # type: ignore[list-item]
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    report = json.loads(out)
    k, d, j, bad = report["results"]
    sps_start = key.payload.index(b"\x00\x00\x00\x01\x67") + 5
    expected_codec = "avc1." + key.payload[sps_start : sps_start + 3].hex().upper()
    assert k == {
        "codec": 1,
        "keyframe": True,
        "seq": 41,
        "captureTsMs": ts,
        "width": 640,
        "height": 360,
        "payloadLength": len(key.payload),
        "nalTypes": k["nalTypes"],
        "codecString": expected_codec,
    }
    assert expected_codec.startswith("avc1.42")  # constrained baseline
    assert {7, 8, 5} <= set(k["nalTypes"])
    assert d["keyframe"] is False and d["seq"] == 42 and d["codecString"] is None
    assert 5 not in d["nalTypes"]
    assert j["codec"] == 2 and j["seq"] == 4_000_000_000 and j["width"] == 320
    assert bad is None
    assert report["offset"] == pytest.approx(500) and report["rtt"] == pytest.approx(10)
    assert all(0 < x <= 4000 for x in report["delays"])
