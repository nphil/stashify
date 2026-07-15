# Self-contained decensor worker: DeepMosaics + Real-ESRGAN + CUDA, all bundled.
#
# Base pinned for the NVIDIA Tesla P40 (Pascal, compute 6.1) and "limited driver"
# hosts:
#   - CUDA 11.6 runtime => works with NVIDIA driver >= 450.80 (very broad; newer
#     unRAID drivers are backward compatible with it).
#   - torch 1.13.1 / torchvision 0.14.1 => Pascal wheels, and avoids the
#     basicsr/torchvision `functional_tensor` breakage seen on torchvision>=0.17.
#   - Real-ESRGAN is forced to --fp32 at runtime (see core.py): Pascal fp16 is
#     ~1/64 speed and can NaN, so fp32 is both faster and correct here.
FROM pytorch/pytorch:1.13.1-cuda11.6-cudnn8-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ffmpeg libgl1 libglib2.0-0 wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps. numpy pinned <2 for torch 1.13 / old opencv compatibility.
# torch/torchvision come from the base image and are NOT reinstalled (none of
# these require a different version).
RUN pip install --no-cache-dir \
        "numpy<2" \
        stashapp-tools \
        opencv-python scikit-image tqdm matplotlib \
        basicsr facexlib gfpgan realesrgan \
        gdown \
    && python -c "import torch; assert torch.version.cuda, 'torch lost CUDA!'; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

# ML tools (cloned, not pip-installed as host packages)
RUN git clone --depth 1 https://github.com/HypoX64/DeepMosaics.git /opt/DeepMosaics \
    && git clone --depth 1 https://github.com/xinntao/Real-ESRGAN.git /opt/Real-ESRGAN

# Pre-fetch Real-ESRGAN weights (best-effort; the inference script auto-downloads
# any missing ones at runtime too). Partial/failed files are removed.
RUN mkdir -p /opt/Real-ESRGAN/weights && cd /opt/Real-ESRGAN/weights && \
    for u in \
      https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth \
      https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
      https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth ; do \
        f=$(basename "$u"); wget -q -O "$f" "$u" || rm -f "$f"; \
    done

# Runtime fixups for the bundled ML tools (all needed for the video pipeline):
#  1. The base image's conda ffmpeg (/opt/conda/bin/ffmpeg, first on PATH) ships
#     with only libopenh264 — no libx264. DeepMosaics and Real-ESRGAN hardcode
#     libx264 for their mp4 output and die with "Unknown encoder 'libx264'".
#     Point ffmpeg/ffprobe at the apt build (has libx264/libx265 + NVENC).
#  2. Real-ESRGAN's inference_realesrgan_video.py does `import ffmpeg`
#     (the ffmpeg-python package), which the base deps don't include.
#  3. The Real-ESRGAN clone has no realesrgan/version.py until its setup runs, and
#     core.py runs the script with cwd=/opt/Real-ESRGAN, so `import realesrgan`
#     resolves to the clone. An editable install generates version.py there.
RUN ln -sf /usr/bin/ffmpeg /opt/conda/bin/ffmpeg \
    && ln -sf /usr/bin/ffprobe /opt/conda/bin/ffprobe \
    && pip install --no-cache-dir ffmpeg-python \
    && pip install --no-cache-dir --no-deps -e /opt/Real-ESRGAN

WORKDIR /app
COPY core.py worker.py server.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Defaults tuned for the Tesla P40 + unRAID. Override in your compose/template.
ENV BACKEND=deepmosaics \
    POST_UPSCALE=true \
    REALESRGAN_MODEL=realesr-animevideov3 \
    REALESRGAN_SCALE=2 \
    REALESRGAN_FP32=true \
    REALESRGAN_TILE=0 \
    DEEPMOSAICS_DIR=/opt/DeepMosaics \
    REALESRGAN_DIR=/opt/Real-ESRGAN \
    MODEL_PATH=/models/clean_youknow_video.pth \
    OUTPUT_DIR=/data/decensored \
    TRIGGER_TAG=Decensor \
    DONE_TAG=Decensored \
    IMPORT_RESULT=true \
    GPU_ID=0 \
    POLL_INTERVAL=0 \
    RUN_MODE=server \
    PORT=8710

EXPOSE 8710
VOLUME ["/models", "/data"]
ENTRYPOINT ["/app/entrypoint.sh"]
