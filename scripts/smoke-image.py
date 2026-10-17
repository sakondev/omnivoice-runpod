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
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def serve():
    from omnivoice.cli.demo import build_demo

    demo = build_demo(SimpleNamespace(sampling_rate=24000), checkpoint="smoke-metadata-only")
    demo.launch(server_name="0.0.0.0", server_port=8000, share=False)
    # Gradio normally blocks the main thread; retain the process if it returns.
    while True:
        time.sleep(1)


def check_config(config):
    assert isinstance(config, dict), "nginx returned something other than Gradio config"
    assert config.get("title") == "OmniVoice Demo", "real OmniVoice title missing"
    components = config.get("components", [])
    assert isinstance(components, list), "Gradio components missing"
    kinds = {component.get("type") for component in components}
    assert {"textbox", "audio", "button"}.issubset(kinds), "speech UI controls missing"


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    # These flags prevent background analytics and accidental model/network access.
    environment = os.environ.copy()
    environment.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        GRADIO_ANALYTICS_ENABLED="False",
        NO_PROXY="localhost,127.0.0.1,0.0.0.0",
        no_proxy="localhost,127.0.0.1,0.0.0.0",
    )
    for variable in ("HF_HOME", "GRADIO_TEMP_DIR"):
        if environment.get(variable):
            Path(environment[variable]).mkdir(parents=True, exist_ok=True)

    processes = []
    try:
        # Base startup establishes the production nginx:8001 -> localhost:8000
        # path. Suppress its output because base startup may export environment.
        processes.append(subprocess.Popen(
            ["/bin/bash", "/start.sh"], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ))
        processes.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            env=environment, start_new_session=True,
        ))
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 120
        last_error = "server not ready"
        while time.monotonic() < deadline:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"smoke child exited with status {process.returncode}")
            try:
                with opener.open("http://127.0.0.1:8001/config", timeout=3) as response:
                    assert response.status == 200, "Gradio config did not return HTTP 200"
                    config = json.load(response)
                check_config(config)
                print("PASS: real OmniVoice UI and generation wiring served through nginx:8001")
                return
            except (URLError, TimeoutError, ValueError, AssertionError) as error:
                # A placeholder HTML 200 is not a successful Gradio response.
                last_error = str(error)
                time.sleep(1)
        raise RuntimeError(f"OmniVoice /config not ready within 120 seconds: {last_error}")
    finally:
        for process in reversed(processes):
            stop(process)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        main()
