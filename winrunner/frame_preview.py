"""Small, timestamp-aligned previews shared by the runners and coordinator."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PAIR_ID = re.compile(r"[0-9]{20}-[0-9a-f]{8}")
KEEP_PAIRS = 8
# Accept small muxer rounding differences, never seek to an unrelated frame.
MATCH_SECONDS = 0.008


def _pair_ids(directory):
    try:
        return sorted(p.name for p in Path(directory).iterdir() if PAIR_ID.fullmatch(p.name))
    except OSError:
        return []


def pair_path(directory, side, pair):
    if side not in ("before", "after") or not pair or not PAIR_ID.fullmatch(pair):
        return None
    base = Path(directory) / pair
    before, after = base / "before.jpg", base / "after.jpg"
    if before.is_file() and after.is_file():
        return before if side == "before" else after
    return None


def latest_pair(directory):
    return next((pair for pair in reversed(_pair_ids(directory))
                 if pair_path(directory, "before", pair)), None)


def _publish(directory, pair, before, after):
    # One rename publishes both immutable images. A reader can never observe
    # an updated after frame next to an older before frame with the same ID.
    directory = Path(directory)
    target = directory / pair
    if target.is_dir():
        return
    directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pair-", dir=directory))
    try:
        (staging / "before.jpg").write_bytes(before)
        (staging / "after.jpg").write_bytes(after)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for old in _pair_ids(directory)[:-KEEP_PAIRS]:
        shutil.rmtree(directory / old, ignore_errors=True)


def mirror_pair(source, dest):
    """Copy only a complete generation, including the last one before scratch is removed."""
    try:
        pair = latest_pair(source)
        if pair and not pair_path(dest, "before", pair):
            src = Path(source) / pair
            _publish(dest, pair, (src / "before.jpg").read_bytes(), (src / "after.jpg").read_bytes())
    except OSError:
        # The writer might have pruned this pair, or the job removed scratch.
        pass


def _run(argv):
    return subprocess.run(argv, capture_output=True, check=True, timeout=6, creationflags=NOWIN)


def _number(value, default=None):
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default


def _probe(ffprobe, path):
    data = json.loads(_run([ffprobe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=start_time,duration:format=start_time,duration",
                            "-of", "json", str(path)]).stdout)
    stream = data["streams"][0]
    fmt = data.get("format", {})
    start = _number(fmt.get("start_time"), 0.0)
    return start, _number(stream.get("start_time"), start), (
        _number(stream.get("duration")) or _number(fmt.get("duration")))


def _grab(ffmpeg, path, media, t):
    file_start, video_start, _ = media
    # ffmpeg seeks relative to the container's start (which may be earlier
    # than the video), while showinfo reports the original frame timestamp.
    seek = max(0.0, video_start - file_start + t)
    try:
        r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-copyts",
                  "-threads", "1", "-ss", f"{seek:.6f}", "-i", str(path), "-map", "0:v:0",
                  "-filter_threads", "1", "-vf", "scale=480:-2,settb=AVTB,showinfo=checksum=0",
                  "-vsync", "0", "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1"])
    except (OSError, subprocess.SubprocessError):
        return None                         # unfinished files can fail at one seek and succeed at another
    match = re.search(rb"showinfo[^\n]*?\bpts:\s*(-?\d+)", r.stderr)
    if match and r.stdout.startswith(b"\xff\xd8") and r.stdout.endswith(b"\xff\xd9"):
        return int(match[1]) / 1_000_000 - video_start, r.stdout
    return None


class FramePreview:
    """Read actual output frames without depending on a processing counter or nominal FPS."""

    def __init__(self, source, directory, ffmpeg, ffprobe):
        self.source = Path(source)
        self.directory = directory
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._source_media = None
        self._attempts = {}
        self._shown = None

    def capture(self, output, *, offset=0.0, duration=None, windows=None, finished=False):
        """Capture a whole-timeline video, or an encoded span whose source offset is known."""
        try:
            output = Path(output)
            if output == self.source:
                return False
            stat = output.stat()
            version = (stat.st_size, stat.st_mtime_ns, offset, duration, windows)
            if not stat.st_size or (not finished and self._attempts.get(output) == version):
                return False
            self._attempts[output] = version
            media = _probe(self.ffprobe, output)
            if self._source_media is None:
                self._source_media = _probe(self.ffprobe, self.source)
            length = duration or media[2]
            previous = self._shown[1] if self._shown and self._shown[0] == output else None
            if windows:
                end = offset + length if length else float("inf")
                cuts = ((max(offset, lo), min(end, hi)) for lo, hi in windows)
                candidates = [((lo + hi) / 2 - offset, (lo, hi)) for lo, hi in cuts if hi > lo][-2:]
                candidates.reverse()
            elif length and length > 0:
                candidates = [(max(0.0, length - min(2.0, length / 2)), None)]
            else:
                # A growing container may not expose a duration yet. A seek
                # is just a candidate: publish only if the media itself decodes.
                candidates = [(previous + 1.0 if previous is not None else 0.0, None)]
            if not windows and (not finished or previous is None):
                fallback = previous + 1.0 if previous is not None else 0.0
                if abs(fallback - candidates[0][0]) > MATCH_SECONDS:
                    candidates.append((fallback, None))
            for t, bounds in candidates:
                after = _grab(self.ffmpeg, output, media, t)
                if not after:
                    continue
                absolute = after[0] + offset
                if ((bounds and not bounds[0] <= absolute < bounds[1])
                        or (previous is not None and after[0] <= previous)):
                    continue
                before = _grab(self.ffmpeg, self.source, self._source_media, max(0.0, absolute - MATCH_SECONDS))
                if not before or abs(before[0] - absolute) > MATCH_SECONDS:
                    continue
                pair = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
                _publish(self.directory, pair, before[1], after[1])
                self._shown = (output, after[0])
                return True
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError):
            # A video being written is often temporarily unreadable. No pair is published.
            pass
        return False


@contextmanager
def watch_frames(poll):
    """Stop reading an intermediate before the producing pass can remove it."""
    stop = threading.Event()

    def run():
        while not stop.wait(3.0):
            try:
                poll()
            except OSError:
                pass                        # a growing file can be renamed during discovery

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
