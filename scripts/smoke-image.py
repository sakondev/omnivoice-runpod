#!/usr/bin/env python3
"""Exercise installed OmniVoice's real Gradio UI through the image's nginx.

No inference or model loading: build_demo reads only sampling_rate at UI creation.
Run inside the image with its entrypoint overridden to python3.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


# Apply before importing upstream Gradio/OmniVoice, including direct --serve runs.
os.environ.update(
    HF_HUB_OFFLINE="1",
    TRANSFORMERS_OFFLINE="1",
    GRADIO_ANALYTICS_ENABLED="False",
    NO_PROXY="localhost,127.0.0.1,0.0.0.0",
    no_proxy="localhost,127.0.0.1,0.0.0.0",
)


def serve():
    for variable in ("HF_HOME", "GRADIO_TEMP_DIR"):
        if os.environ.get(variable):
            Path(os.environ[variable]).mkdir(parents=True, exist_ok=True)

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    environment = os.environ.copy()
    environment.pop("HF_TOKEN", None)
    # Base startup establishes nginx:8001 -> localhost:8000. Its environment
    # export must neither persist HF_TOKEN nor print credentials into test logs.
    base = subprocess.Popen(
        ["/bin/bash", "/start.sh"], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        from omnivoice.cli.demo import build_demo

        demo = build_demo(SimpleNamespace(sampling_rate=24000), checkpoint="smoke-metadata-only")
        demo.queue().launch(server_name="0.0.0.0", server_port=8000, share=False)
        # Gradio normally blocks; keep direct --serve alive if launch returns.
        while True:
            time.sleep(1)
    finally:
        stop(base)


def check_config(config, expected_root):
    assert isinstance(config, dict), "nginx returned something other than Gradio config"
    assert config.get("root") == expected_root, (
        f"Gradio public root mismatch: {config.get('root')!r} != {expected_root!r}"
    )
    components = config.get("components", [])
    assert isinstance(components, list), "Gradio components missing"
    kinds = {component.get("type") for component in components}
    assert {"textbox", "audio", "button"}.issubset(kinds), "speech UI controls missing"


def stop(process, timeout=10):
    # Signal the group even if its leader exited, so descendants cannot survive.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def main():
    environment = os.environ.copy()
    expected_root = environment.setdefault(
        "GRADIO_ROOT_PATH", "https://proxy-smoke.example.test"
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve"],
        env=environment, start_new_session=True,
    )
    try:
        opener = build_opener(ProxyHandler({}))
        headers = {
            "Host": "100.65.14.203",
            "X-Forwarded-Host": "100.65.14.203",
            "X-Forwarded-Proto": "http",
        }
        config_request = Request("http://127.0.0.1:8001/config", headers=headers)
        deadline = time.monotonic() + 120
        last_error = "server not ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"smoke child exited with status {process.returncode}")
            try:
                response = opener.open(config_request, timeout=3)
            except HTTPError as error:
                if error.code not in (502, 503, 504):
                    raise
                last_error = str(error)
                error.close()
            except (URLError, TimeoutError) as error:
                last_error = str(error)
            else:
                with response:
                    assert response.status == 200, "Gradio config did not return HTTP 200"
                    if response.headers.get_content_type() == "text/html":
                        last_error = "nginx is waiting for Gradio"
                        time.sleep(1)
                        continue
                    config = json.load(response)
                # Once reachable, malformed config or failed checks are real failures.
                check_config(config, expected_root)
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"OmniVoice /config not ready within 120 seconds: {last_error}")

        theme_request = Request(
            "http://127.0.0.1:8001/theme.css?v=proxy-smoke", headers=headers
        )
        with opener.open(theme_request, timeout=10) as response:
            assert response.status == 200, "theme stylesheet did not return HTTP 200"
            assert response.headers.get_content_type() == "text/css", (
                "theme route did not return text/css"
            )
            stylesheet = response.read()
        assert len(stylesheet) > 1000, "theme stylesheet is not substantive"
        assert b"{" in stylesheet and b"}" in stylesheet, "theme CSS rules missing"
        assert not stylesheet.lstrip().lower().startswith((b"<!doctype", b"<html")), (
            "theme route returned placeholder HTML"
        )
        print("PASS: real OmniVoice UI, public config root, and theme CSS served through nginx:8001")
    finally:
        # Allow --serve to stop its separately grouped base startup first.
        stop(process, timeout=15)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        main()
