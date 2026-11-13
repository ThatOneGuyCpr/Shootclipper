"""Checks a packaged build end to end. The release workflow runs
`ShootClipper.exe --selftest report.txt` on a real Windows machine and refuses to
publish if anything here fails.
"""
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path


def _checks():
    from . import config, server, util

    def bundled_files():
        need = [server.WEB / "index.html", server.WEB / "app.js", server.WEB / "app.css",
                config.BUNDLED_DATA / "names.txt", config.BUNDLED_DATA / "sources.json",
                config.BUNDLED_DATA / "face_detection_yunet_2023mar.onnx",
                config.BUNDLED_DATA / "fonts" / "Poppins-BlackItalic.ttf"]
        missing = [str(p) for p in need if not p.exists()]
        assert not missing, f"missing: {missing}"
        return f"{len(need)} files"

    def ffmpeg():
        assert util.have("ffmpeg") and util.have("ffprobe"), "ffmpeg/ffprobe not on PATH"
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                                 text=True, creationflags=util.NO_WINDOW).stdout
        assert " ass " in filters and "subtitles" in filters, "ffmpeg lacks libass"
        return shutil.which("ffmpeg")

    def caption_render():
        # Burn an ASS caption in the bundled font, the way render.py does.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copytree(config.BUNDLED_DATA / "fonts", tmp / "fonts")
            (tmp / "subs.ass").write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment\n"
                "Style: Default,Poppins Black,116,&H00FFFFFF,0,1,2\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,TEST\n", encoding="utf-8")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                "color=c=black:s=1080x1920:d=1", "-vf", "ass=subs.ass:fontsdir=fonts",
                                "-frames:v", "1", "out.png"], cwd=tmp, capture_output=True, text=True,
                               creationflags=util.NO_WINDOW)
            assert r.returncode == 0 and (tmp / "out.png").exists(), r.stderr[-400:]
        return "ok"

    def face_detector():
        import cv2
        import numpy as np
        det = cv2.FaceDetectorYN.create(str(config.BUNDLED_DATA / "face_detection_yunet_2023mar.onnx"),
                                        "", (320, 320))
        det.detect(np.zeros((320, 320, 3), np.uint8))
        return cv2.__version__

    def whisper():
        # Downloads the tiny model (about 75 MB) and transcribes a second of tone,
        # which exercises ctranslate2, onnxruntime (VAD) and the bundled assets.
        import numpy as np
        from faster_whisper import WhisperModel
        audio = (0.1 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.float32)
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio, vad_filter=True)
        list(segs)
        import ctranslate2
        import onnxruntime
        return f"ctranslate2 {ctranslate2.__version__}, onnxruntime {onnxruntime.__version__}"

    def libraries():
        import anthropic
        import keyring
        import webview
        import yt_dlp
        from . import api, llm, pipeline, render  # noqa: F401
        keyring.get_keyring()
        return f"anthropic {anthropic.__version__}, yt-dlp {yt_dlp.version.__version__}, " \
               f"pywebview {getattr(webview, '__version__', '?')}"

    return [("bundled files", bundled_files), ("ffmpeg", ffmpeg), ("caption render", caption_render),
            ("face detector", face_detector), ("libraries", libraries), ("whisper", whisper)]


def run(out_path):
    lines, failed = [], 0
    try:
        checks = _checks()
    except Exception:
        checks, failed = [], 1
        lines.append("FAIL setup\n" + traceback.format_exc())
    for name, fn in checks:
        try:
            lines.append(f"ok   {name}: {fn()}")
        except Exception:
            failed += 1
            lines.append(f"FAIL {name}\n{traceback.format_exc()}")
    lines.append("PASSED" if not failed else f"{failed} check(s) failed")
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0
