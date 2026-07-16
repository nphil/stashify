#!/usr/bin/env bash
# Lada runner entrypoint:
#   1. fetch model weights once into the (persisted) weights dir
#   2. probe NVENC through PyAV, latest-first: keep the stock (latest-libav)
#      wheel if its hevc_nvenc opens; otherwise swap in the sdk/13.0-linked
#      build (works on Pascal / driver 580, whose NVENC tops out at API 13.0);
#      if neither opens, restore the modern wheel and default to libx264.
#   3. export LADA_DEFAULT_ENCODER (unless preset) and start the HTTP runner.
# Idempotent: a swapped wheel persists across restarts, so later boots probe-pass
# instantly; recreating the container on a newer GPU auto-returns to latest.
set -euo pipefail

MW="${LADA_MODEL_WEIGHTS_DIR:-/models}"
mkdir -p "$MW"
HF="https://huggingface.co/ladaapp/lada/resolve/main"

fetch() {  # url filename
  if [ -s "$MW/$2" ]; then
    echo "[lada] have $2"
  else
    echo "[lada] downloading $2 ..."
    curl -fL --retry 3 "$1?download=true" -o "$MW/$2.part" && mv "$MW/$2.part" "$MW/$2"
  fi
}

fetch "$HF/lada_mosaic_detection_model_v4_fast.pt"                 lada_mosaic_detection_model_v4_fast.pt
fetch "$HF/lada_mosaic_detection_model_v4_accurate.pt"            lada_mosaic_detection_model_v4_accurate.pt
fetch "$HF/lada_mosaic_restoration_model_generic_v1.2.pth"       lada_mosaic_restoration_model_generic_v1.2.pth

VENVPY=/opt/lada/.venv/bin/python

probe_nvenc() {
  "$VENVPY" - <<'EOF'
import sys
try:
    import av
    c = av.CodecContext.create("hevc_nvenc", "w")
    c.width, c.height, c.pix_fmt = 256, 256, "yuv420p"
    c.framerate = 25
    c.open()                       # the real avcodec_open2 test
except Exception as exc:           # noqa: BLE001
    print("[probe] hevc_nvenc unavailable: %s" % exc, file=sys.stderr)
    sys.exit(1)
EOF
}

swap_av() {  # dir
  uv pip install --python "$VENVPY" --no-index --find-links "$1" \
    --force-reinstall --no-deps av >/dev/null 2>&1
}

ENC=libx264
if probe_nvenc; then
  ENC=hevc_nvenc
  echo "[lada] NVENC OK with current PyAV/libav"
elif ls /opt/wheels/legacy/av-*.whl >/dev/null 2>&1; then
  echo "[lada] NVENC failed on current libav; trying legacy sdk/13.0 build (Pascal path)"
  swap_av /opt/wheels/legacy
  if probe_nvenc; then
    ENC=hevc_nvenc
    echo "[lada] NVENC OK via legacy PyAV (driver supports API <= 13.0)"
  else
    echo "[lada] NVENC unavailable on this GPU/driver; restoring latest libav, encoding with libx264"
    swap_av /opt/wheels/modern || true
  fi
fi

export LADA_DEFAULT_ENCODER="${LADA_DEFAULT_ENCODER:-$ENC}"
echo "[lada] default encoder: $LADA_DEFAULT_ENCODER"
echo "[lada] starting runner on :${PORT:-8711}"
exec python3 /opt/lada/lada_runner.py
