FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404@sha256:0a360022e8de4375af99430f84e8b38951acc397252163a37ceac7204d01be35

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Reuse the base image's CUDA-enabled PyTorch rather than installing another copy.
RUN python3 -m venv --system-site-packages /opt/omnivoice/venv \
    && /opt/omnivoice/venv/bin/python -m pip install --no-cache-dir \
        'torch==2.8.0+cu128' \
        'torchaudio==2.8.0+cu128' \
        'transformers==5.20.1' \
        'gradio==6.30.0' \
        'https://github.com/k2-fsa/OmniVoice/archive/08be0b4ccbac3e13e374e86fbfead4b4cac343e2.tar.gz'

ENV PATH="/opt/omnivoice/venv/bin:${PATH}" \
    HF_HOME=/workspace/omnivoice/huggingface \
    OMNIVOICE_TEMP_DIR=/tmp/omnivoice-studio

RUN python -m pip install --no-cache-dir \
        'fastapi==0.143.3' 'uvicorn==0.54.0' \
        'pydantic==2.14.1' 'python-multipart==0.0.32'

WORKDIR /opt/omnivoice
COPY studio/ /opt/omnivoice/studio/
COPY nginx.conf /etc/nginx/nginx.conf
COPY --chmod=755 start-omnivoice.sh /usr/local/bin/start-omnivoice.sh

EXPOSE 8001 22
ENTRYPOINT ["/usr/local/bin/start-omnivoice.sh"]
CMD []
