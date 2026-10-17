# OmniVoice Studio on Runpod

A Thai-language Studio interface for [OmniVoice](https://github.com/k2-fsa/OmniVoice), using Google Sans, reference voice cloning, voice design, microphone recording, and per-phrase speech settings. This is the application served by the Docker image, not a separate HTML mockup. Dependencies are installed at image build time. Startup does not run apt or pip. Model weights remain in `/workspace/omnivoice/huggingface` while the same volume is retained.

## Deploy a GPU Pod

1. Open **OmniVoice Web UI** in [Runpod Templates](https://console.runpod.io/user/templates). The account template ID is `jewf3g5lqm`.
2. Use the immutable `sha-<commit>` image selected in the template, or `ghcr.io/sakondev/omnivoice-runpod:latest` for the last successful build. Leave the Docker command override empty.
3. Choose an NVIDIA GPU host supporting CUDA12.8 or later. Keep the 50GB container disk and 50GB persistent mount at `/workspace`.
4. Optionally configure `HF_TOKEN` below.
5. Wait for model loading and the Uvicorn startup message. Open **Connect**, then the HTTP service on port **8001**.

Nginx8001 forwards to Studio8000. Do not start the application on8001. Port22 supplies SSH when Runpod provides your registered public key. UI, uploads, job status and audio use relative same-origin URLs; no previous Pod URL or `GRADIO_ROOT_PATH` setting is needed.

The service has no login. Treat its public proxy URL as sensitive. Do not expose it as a public multi-user service without access control. Anyone with the URL can use its compute. The application does not expose `HF_TOKEN` to the browser.

## Make speech

1. Expand **เสียงที่ใช้**. Upload reference audio, record with **อัดเสียงอ้างอิง**, or select **ออกแบบเสียง**. Microphone recording requires HTTPS or localhost, an available input device, and browser/OS permission. Stop recording before generating. The interface reports permission and device failures without pretending a recording succeeded.
2. For cloning, enter the words spoken in the reference clip. If left blank, the default server uses Whisper to transcribe. This can take longer on the first request. If the server was started with `--no-asr`, a transcript is required.
3. Start with one phrase. Use **เพิ่มช่วงเสียง** in the fixed bottom bar to add another. Use **ลบ** at a phrase's header to remove it. At least one phrase remains. Select a phrase to set its language, whisper instruction, pitch instruction, speed, and following pause. The language selector contains the original upstream list plus Auto. Language selection is not translation.
4. Insert any of the13 documented nonverbal tags using the menu inside the text field. Tags, whisper, pitch and design gender are model instructions, not guaranteed acoustic transformations or arbitrary per-word emotion controls.
5. Use **ตรวจแผนการสร้าง** in the fixed bottom bar, then generate from the reviewed plan. Follow real phrase completion progress. Listen to the completed audio and download the combined PCM16 WAV.

Each phrase is synthesized separately. Reference mode reuses one clone prompt across the script. Design mode is instruction-based and does not promise an identical speaker across independently generated phrases. Pauses are inserted between phrases by Studio, not sent as an emotion command. The final phrase does not add trailing silence.

The server accepts up to 24 phrases, 2000 characters per phrase and 6000 characters per script. Reference uploads are limited to 32MiB and 60seconds. WAV, MP3, M4A, FLAC, OGG and browser WebM/Opus are normalized to mono PCM with FFmpeg before cloning. If the browser cannot preview a codec, you can still submit the file for server validation. A short, clean reference clip is preferable.

One generation job runs at a time. Jobs run in the background, so a long script is not held inside one HTTP request. If the submission response or polling connection is lost, use the existing-job status button to recover without creating another job. Do not close or refresh the page while generating; the page does not restore a job session after reload. Inputs are locked during generation.

Completed downloads expire after 30minutes and at most eight completed jobs are retained. Reference uploads and normalized input files are removed after processing. Startup removes abandoned application directories after acquiring an exclusive lock on its temporary base. Generated files are temporary, not a permanent library. Download outputs you want to keep. A restart loses job history. Script edits and selected local files are held in the current browser page only.

Direct API clients must send `X-OmniVoice-Request: 1` and a unique UUID in `Idempotency-Key` on `POST /api/jobs`. Reusing a retained job key returns that job instead of synthesizing again. Use the same UUID with `GET /api/jobs/<id>` if the submission response was lost. Cross-origin browser forms cannot supply these headers.

## Hugging Face token

The public model works without authentication. A token can increase Hugging Face download rate limits.

1. Create a token with the minimum model-read permissions needed.
2. Store it in Runpod Secrets as `HF_TOKEN`.
3. Set the template or Pod environment variable `HF_TOKEN` to `{{ RUNPOD_SECRET_HF_TOKEN }}`.
4. Apply the setting when deploying a new Pod. Template changes do not change existing Pods.

Leave the variable empty if there is no secret. Never commit a token, bake it into an image or pass it as a build argument. The startup script retains it for model downloads but excludes it from the inherited base script's environment export.

## Build and verify

```sh
docker build -t omnivoice-runpod:local .
docker run --rm --network none --entrypoint python3 omnivoice-runpod:local /opt/omnivoice/studio/server.py --help
docker run --rm --network none \
  --mount "type=bind,source=$(pwd)/scripts/smoke-image.py,target=/tmp/smoke-image.py,readonly" \
  --entrypoint python3 omnivoice-runpod:local /tmp/smoke-image.py
```

The offline smoke starts the real Studio HTTP application in explicit `--metadata-only` mode through nginx. It checks complete language choices, validation, readiness, and refusal to synthesize without a model. It does not return fabricated audio. CI additionally runs `scripts/browser-smoke.cjs` through an HTTPS proxy with an intentionally incorrect internal Host. That check covers tag insertion, language persistence, upload/recording controls with a synthetic microphone device, script validation and desktop/mobile layout. It is not GPU-inference or physical-microphone verification.

Run the actual model on a local NVIDIA Docker host:

```sh
docker run --rm --gpus all -p 8001:8001 \
  --mount type=volume,source=omnivoice-cache,target=/workspace \
  -e HF_TOKEN omnivoice-runpod:local
```

For CPU inference verification, append `--device cpu --no-asr --steps 4` to that command and omit `--gpus all`. CPU uses float32, can be slow, and requires enough RAM for the model. Four steps are a smoke setting, not the production quality default of32. Supply a reference transcript with `--no-asr`. Use `--help` for model and ASR overrides.

For the real browser inference check, start the model with the same port mapping, then run the browser script with `SMOKE_INFERENCE=1`, `SMOKE_REFERENCE_FILE` pointing to a real reference clip, `SMOKE_CERT_FILE`/`SMOKE_KEY_FILE` containing a local HTTPS test certificate, and Playwright available through `NODE_PATH`. `SMOKE_UPSTREAM` defaults to `http://127.0.0.1:18001`; map the container there or override it. The script performs actual design and two-phrase cloning, downloads WAVs, and checks sample rate, non-silent samples and the requested pause.

## Publish and update

Push to `main`, or run **Build and publish image** in [GitHub Actions](https://github.com/sakondev/omnivoice-runpod/actions). The workflow publishes only after HTTP and browser checks pass. It publishes `latest` and `sha-<full commit SHA>` and records the registry digest. Runtime inference requires a model-capable environment and is separate from model-free CI.

The base image and upstream source are pinned in `Dockerfile`. Studio lives under `/opt/omnivoice/studio` and Python dependencies under `/opt/omnivoice/venv`, outside the persistent mount. An old `/workspace/omnivoice/venv` cannot override it. `OMNIVOICE_TEMP_DIR` defaults to the non-persistent `/tmp/omnivoice-studio`. Use a dedicated temporary base per server process, separate from the model cache.

GHCR packages may be private independently of repository visibility. Use a Runpod registry credential with package-read access when necessary.

Updating the template affects future deployments only. An existing Pod keeps its saved image and startup command. Back up important data and decide how to retain the model cache before replacing a Pod. Host-local persistent storage does not survive Pod termination or host loss.
