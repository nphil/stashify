# Stashify — thin coordinator: HTTP API + dashboard + Stash integration +
# runner dispatch. Published as ghcr.io/nphil/stashify.
#
# All GPU work (Lada decensoring, SPAN upscaling) happens in the compute runner
# container (Dockerfile.lada, the stashify-runner image); this image needs
# neither CUDA nor ffmpeg. It went
# from a ~6 GB PyTorch stack to ~150 MB when DeepMosaics/Real-ESRGAN moved out.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir stashapp-tools requests

# Cloud GPU rentals (Vast.ai): the coordinator reaches a rented machine with ssh + rsync.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client rsync \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY core.py worker.py server.py cloud_api.py cloud_jasna.py cloud_ssh.py cloud_vast.py entrypoint.sh /app/
COPY webui/ /app/webui/
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Defaults for the runner-backed pipeline. Override in your compose/template.
ENV BACKEND=decensor \
    RUNNER_SCRATCH=/scratch \
    OUTPUT_DIR=/data/stashify \
    TRIGGER_TAG=Decensor \
    DONE_TAG=Decensored \
    IMPORT_RESULT=true \
    GPU_ID=0 \
    RUN_MODE=server \
    PORT=8710

EXPOSE 8710
ENTRYPOINT ["/app/entrypoint.sh"]
