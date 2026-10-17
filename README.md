# Run OmniVoice on Runpod

Deploy the upstream [OmniVoice Web UI](https://github.com/k2-fsa/OmniVoice) with dependencies installed in the container image. Startup does not run apt or pip. Model weights download at runtime and remain in `/workspace/omnivoice/huggingface` while you retain the same volume.

## Deploy a GPU Pod

1. Open your **OmniVoice Web UI** template in [Runpod Templates](https://console.runpod.io/user/templates). The configured account template ID is `jewf3g5lqm`.
2. Use `ghcr.io/sakondev/omnivoice-runpod:latest` as the container image. Leave the Docker command override empty. For a private GHCR package, select your GHCR registry credential with package read access.
3. Use an NVIDIA GPU host supporting CUDA 12.8 or later. Keep the template's 50 GB container disk and 50 GB persistent mount at `/workspace`.
4. Optionally configure `HF_TOKEN` as described below.
5. Deploy and wait for `Model loaded.` and the Gradio server URL. Open **Connect**, then the HTTP service on port **8001**.

Nginx listens on port 8001 and forwards to OmniVoice on port 8000. Do not start OmniVoice on port 8001. Port 22 provides SSH when Runpod supplies your registered public key.

The upstream UI has no login. Treat the public proxy URL as sensitive. Do not use this deployment as a public multi-user service without adding access control.

## Add a Hugging Face token

The public OmniVoice model works without a token. Authentication can provide higher Hugging Face download rate limits.

1. Create a Hugging Face token with the minimum model read permissions you need.
2. Store the token in Runpod Secrets with the name `HF_TOKEN`.
3. Set the template or Pod environment variable `HF_TOKEN` to `{{ RUNPOD_SECRET_HF_TOKEN }}`.
4. Apply that setting when deploying a new Pod. Updating a template does not change existing Pods.

Leave `HF_TOKEN` empty if you have not created the secret. An unresolved secret reference is not a working token. Never commit a token, put one in a Dockerfile, or pass one as a build argument. The startup script excludes `HF_TOKEN` from the inherited base script's environment export file while retaining it for OmniVoice.

## Publish an image

Push a commit to `main`, or run **Build and publish image** manually in [GitHub Actions](https://github.com/sakondev/omnivoice-runpod/actions). The workflow builds Linux AMD64, checks the installed CLI, serves the real upstream UI through nginx, and then publishes the checked image to GHCR.

Each successful run publishes `latest` and `sha-<full Git commit SHA>`. Use a SHA tag or registry digest to deploy a fixed version. `latest` tracks the last successful publish. The workflow uses GitHub's scoped `GITHUB_TOKEN`; no registry password or HF token is needed in repository secrets.

New GHCR packages default to private. Either attach a Runpod registry credential with read access, or change the package visibility to public in GitHub's package settings. Repository visibility alone does not make its container package public.

The base image and upstream OmniVoice source are pinned in `Dockerfile`. The image installs the application under `/opt/omnivoice/venv`, outside the persistent mount, so an old `/workspace/omnivoice/venv` does not override the new application.

## Build and check locally

```sh
docker build -t omnivoice-runpod:local .
docker run --rm --network none --entrypoint omnivoice-demo omnivoice-runpod:local --help
docker run --rm --network none \
  --mount "type=bind,source=$(pwd)/scripts/smoke-image.py,target=/tmp/smoke-image.py,readonly" \
  --entrypoint python3 omnivoice-runpod:local /tmp/smoke-image.py
```

The CPU smoke check constructs the real upstream Gradio interface with sample-rate metadata only. It verifies nginx forwarding and UI configuration without downloading models. It does not verify GPU inference or generated audio.

To run the full application on a local NVIDIA Docker host, first set `HF_TOKEN` in your shell if you need authentication, then run:

```sh
docker run --rm --gpus all -p 8001:8001 \
  --mount type=volume,source=omnivoice-cache,target=/workspace \
  -e HF_TOKEN omnivoice-runpod:local
```

Retain the volume to reuse cached weights. Runpod host-local persistent storage does not survive Pod termination or host loss. Back up any reference recordings you cannot replace.

## Move an existing Pod

Updating the template affects future deployments only. A restart of an old Pod still uses that Pod's saved image and startup command. Do not delete an existing Pod until you have backed up its data and decided how to retain its model cache.
