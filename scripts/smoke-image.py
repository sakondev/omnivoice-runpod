#!/usr/bin/env python3
"""Check the real Studio HTTP app through nginx, without loading a model."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
SERVER = Path("/opt/omnivoice/studio/server.py")
HEADERS = {"Host": "100.65.14.203", "X-Forwarded-Host": "100.65.14.203", "X-Forwarded-Proto": "http"}


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def serve():
    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    environment = os.environ.copy()
    environment.pop("HF_TOKEN", None)
    base = subprocess.Popen(["/bin/bash", "/start.sh"], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    app = None
    try:
        app = subprocess.Popen([sys.executable, str(SERVER), "--metadata-only", "--ip", "0.0.0.0", "--port", "8000"], start_new_session=True)
        raise SystemExit(app.wait())
    finally:
        if app:
            stop(app)
        stop(base)


def check_workspace_cleanup():
    sys.path.insert(0, str(SERVER.parent.parent))
    from studio.server import job_workspace
    previous = os.environ.get("OMNIVOICE_TEMP_DIR")
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        stale = base / "omnivoice-studio-abandoned"
        stale.mkdir()
        (stale / "upload").write_bytes(b"private reference")
        keep = base / "unrelated"
        keep.write_bytes(b"keep")
        os.environ["OMNIVOICE_TEMP_DIR"] = temporary
        try:
            with job_workspace() as active:
                assert not stale.exists(), "Abandoned reference survived startup"
                assert keep.read_bytes() == b"keep", "Cleanup touched unrelated files"
                try:
                    with job_workspace():
                        raise AssertionError("Two servers acquired the same temporary base")
                except RuntimeError:
                    pass
                assert active.is_dir(), "Second server removed the active workspace"
            assert not active.exists(), "Graceful shutdown retained private artifacts"
        finally:
            if previous is None:
                os.environ.pop("OMNIVOICE_TEMP_DIR", None)
            else:
                os.environ["OMNIVOICE_TEMP_DIR"] = previous


def main():
    check_workspace_cleanup()
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve"], start_new_session=True)
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None, trusted=True):
        headers = dict(HEADERS)
        if body is not None:
            boundary = "StudioSmokeBoundary"
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"plan\"\r\n\r\n{json.dumps(body)}\r\n--{boundary}--\r\n").encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            if trusted:
                headers["X-OmniVoice-Request"] = "1"
                headers["Idempotency-Key"] = str(uuid.uuid4())
        try:
            response = opener.open(Request("http://127.0.0.1:8001" + path, body, headers), timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers.get_content_type(), response.read()

    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Studio smoke server exited: {process.returncode}")
            try:
                status, kind, body = request("/api/config")
                if status == 200 and kind == "application/json":
                    config = json.loads(body)
                    break
            except (URLError, TimeoutError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Studio /api/config not reachable through nginx within 120 seconds")

        from omnivoice.utils.lang_map import LANG_NAMES, lang_display_name
        assert config["languages"] == ["Auto"] + sorted(lang_display_name(n) for n in LANG_NAMES), "Studio omitted or changed supported languages"
        assert config["ready"] is False, "Metadata-only mode must not claim model readiness"
        status, kind, _ = request("/")
        assert (status, kind) == (200, "text/html"), "Studio page not served through nginx"
        status, kind, body = request("/healthz")
        assert status == 503 and json.loads(body)["ready"] is False, "Unloaded model reported healthy"
        phrase = {"text": "Hello.", "language": "Auto", "whisper": False, "pitch": "ปกติ", "speed": 1, "pause": 0}
        plan = {"mode": "design", "gender": "female", "reference_text": "", "phrases": [phrase]}
        assert request("/api/jobs", {**plan, "phrases": []})[0] in (400, 422), "Empty scripts accepted"
        assert request("/api/jobs", {**plan, "phrases": [{**phrase, "language": "NotALanguage"}]})[0] in (400, 422), "Unsupported language accepted"
        assert request("/api/jobs", {**plan, "phrases": [{**phrase, "speed": 0}]})[0] in (400, 422), "Invalid speed accepted"
        assert request("/api/jobs", plan, trusted=False)[0] == 403, "Cross-origin form-compatible submission accepted"
        assert request("/api/jobs", plan)[0] == 503, "Metadata-only mode pretended to synthesize"
        assert request("/api/jobs/unknown")[0] == 404, "Unknown job did not return404"
        print("PASS: Studio page/config through nginx, complete languages, input boundaries and honest model readiness; no inference performed")
    finally:
        stop(process)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        main()
