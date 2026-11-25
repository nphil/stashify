"""What a rented GPU must be able to do, per cloud operation. The ONE place these rules live:
cloud_jasna.requirements() turns a Need into the search a rental uses, and cloud_api.offers()
asks problem() for the plain reason an offer is greyed out. Nothing else may carry its own
GPU list or VRAM number.

Every operation Stashify runs at home can run on a rented card. Which cards can do it:

  Jasna ops (decensor, decensor+upscale, upscale)
      An RTX-class NVIDIA card (upstream: data-centre cards do not work; "RTX" is Turing or
      newer, compute capability >= 7.5, which Jasna's README also requires), driver >= 580, and
      10 GB of video memory: the README says 10 GB is enough at the default clip size, and it
      is what the home RTX 3080 has. Any such card is allowed, not just the fastest few.
  transcode
      Any NVIDIA card with a hardware video encoder (NVENC). GeForce, RTX and Quadro cards and
      the L4/L40/A10/A16/A40/T4 data-centre parts have one; A100, H100, H200, B200, V100, P100,
      A30 and the like do not, so they are greyed out ("No video encoder"). The static ffmpeg
      build the box installs is made against nv-codec-headers 13, which needs driver >= 570.
"""

import dataclasses
import re

from hireling.offers import SLACK

JASNA_OPS = ("decensor", "decensor+upscale", "upscale")
TRANSCODE = "transcode"
OPS = JASNA_OPS + (TRANSCODE,)


@dataclasses.dataclass(frozen=True)
class Need:
    """What one operation needs from any machine."""
    gpu_pattern: str          # regex the GPU model must match
    gpu_reason: str           # why a model that does not match is greyed out
    min_vram_gb: float
    min_driver: int
    min_inet_down: int        # Mbps: what the box needs to fetch its software
    base_disk_gb: int         # disk for the OS and the software, before the video itself


JASNA = Need(gpu_pattern=r"^RTX", gpu_reason="Needs an RTX card", min_vram_gb=10, min_driver=580,
             min_inet_down=200, base_disk_gb=80)

# NVIDIA cards with an NVENC encoder. The lookahead keeps "A10" from matching "A100" and "L4" from
# matching "L40S" by accident. An unknown name is NOT assumed to have an encoder.
NVENC_GPU = (r"^(RTX|GTX|Titan|Q RTX|Quadro|(Tesla )?(T4|P4|P40)|L4|L40S?|A10G?|A16|A40|A2)"
             r"(?![0-9A-Za-z])")
TRANSCODE_NEED = Need(gpu_pattern=NVENC_GPU, gpu_reason="No video encoder", min_vram_gb=2, min_driver=570,
                      min_inet_down=100, base_disk_gb=20)


def uses_jasna(op):
    return op in JASNA_OPS


def need_for(op):
    """The Need of a cloud operation (anything but transcode is a Jasna run)."""
    return TRANSCODE_NEED if op == TRANSCODE else JASNA


def problem(op, gpu, vram_gb=None):
    """Why a machine with this GPU model and per-GPU memory cannot do `op`, in a few plain
    words (what the market browser shows on the greyed row), or None when it can."""
    need = need_for(op)
    if not re.search(need.gpu_pattern, gpu or "", re.I):
        return need.gpu_reason
    if vram_gb is not None and need.min_vram_gb and vram_gb < need.min_vram_gb * SLACK:
        return "Needs %g GB (this has %g)" % (need.min_vram_gb, round(vram_gb, 1))
    return None
