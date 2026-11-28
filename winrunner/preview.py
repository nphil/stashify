"""Matched stills and short comparison clips from Jasna's real processed frames."""
import json
import math
import os
import subprocess
from pathlib import Path

try:
    from .frame_preview import _probe, _run
except ImportError:  # standalone Windows runner
    from frame_preview import _probe, _run


MAX_PREVIEW_SECONDS = 45.0   # don't re-encode an entire long restoration span


class JasnaFrames:
    """Find readable exports and smart fragments with a known source timeline offset."""

    def __init__(self, frames, output, ranges=None):
        self.frames = frames
        self.output = Path(output)
        self.ranges = tuple(tuple(r) for r in ranges) if ranges else None
        self.checkpoint = self.output.with_name(f".{self.output.stem}.jasna-resume") / "render"
        self._read_ranges = False
        self._last_span = -1
        self._starts = [0.0]
        self.completed = ()

    def _load_ranges(self):
        if self._read_ranges:
            return
        try:
            inputs = json.loads((self.checkpoint / "job.json").read_text(encoding="utf-8"))["inputs"]
            restored, ltx = [], []
            for row in inputs["latents"]["segments"]:
                start, end = float(row["start"]), float(row["end"])
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                    restored.append((start, end))
                    if row["restoration"]["model"] == "ltx":
                        ltx.append((start, end))
            self.ranges = tuple(sorted(ltx or restored)) or self.ranges
            self._read_ranges = True
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _manifest_spans(self, fragments):
        # Jasna writes exact span durations only when assembling the final file.
        try:
            rows = (fragments / "fragments.ffconcat").read_text(encoding="utf-8").splitlines()
            if not rows or rows[0] != "ffconcat version 1.0" or len(rows) % 2 != 1:
                raise ValueError("incomplete concat manifest")
            spans, start = [], 0.0
            for i in range(1, len(rows), 2):
                if not rows[i].startswith("file ") or not rows[i + 1].startswith("duration "):
                    raise ValueError("unrecognized concat row")
                duration = float(rows[i + 1].split(" ", 1)[1])
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("invalid span duration")
                spans.append((start, start + duration))
                start += duration
            if spans:
                return spans
        except (OSError, ValueError):
            pass
        return None

    def _spans(self, fragments, stop):
        manifest = self._manifest_spans(fragments)
        if manifest is not None:
            return manifest
        # Current Jasna writes the fragment with its own zero-based clock.
        # An unbroken prefix of committed fragments lets us recover the next
        # offset without counting frames or guessing a constant frame rate.
        while True:
            n, start = len(self._starts) - 1, self._starts[-1]
            path = fragments / f"{n:04d}-raw.nut"
            if not path.is_file():
                break
            try:
                _, first, last = _probe(self.frames.ffprobe, path, stop=stop)
                if last is None or last < first:
                    break
                end = self.frames.source_time_after(start + last - first, stop=stop)
            except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
                break
            if end is None or end <= start:
                break
            self._starts.append(end)
        return list(zip(self._starts, self._starts[1:])) + [(self._starts[-1], None)]

    def capture(self, finished=False, *, stop=None):
        self._load_ranges()
        if self.frames.capture(self.output, windows=self.ranges, finished=finished, stop=stop):
            return True
        if finished or not self.ranges:
            return False
        fragments = self.checkpoint / "segments"
        spans = self._spans(fragments, stop)
        self.completed = tuple(
            (fragments / f"{n:04d}-raw.nut", start, end)
            for n, (start, end) in enumerate(spans)
            if end is not None and any(lo < end and hi > start for lo, hi in self.ranges)
        )
        for n in range(len(spans) - 1, max(0, self._last_span) - 1, -1):
            start, end = spans[n]
            if not any((end is None or lo < end) and hi > start for lo, hi in self.ranges):
                continue
            fragment = fragments / f"{n:04d}-raw.nut"
            if end is None and not fragment.is_file():
                fragment = fragments / f"{n:04d}-partial.nut"
            if self.frames.capture(fragment, offset=start, duration=end - start if end is not None else None,
                                   windows=self.ranges, stop=stop):
                self._last_span = n
                return True
        return False


class SegmentPreview:
    """Watcher started as a daemon thread for a preview-enabled decensor job."""

    def __init__(self, *, jasna, src, prev_dir,
                 ffmpeg, ffprobe, encoder="libx264", height=720, on_update=None, log=None):
        self.jasna = jasna
        self.src = src
        self._emitted = set()
        self.prev_dir = prev_dir              # local dir we write seg<N>_before/after.mp4
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.encoder = encoder
        self.height = int(height)
        self.on_update = on_update            # callback(list_of_segment_dicts)
        self._log = log or (lambda m: None)
        self.segments = []
        os.makedirs(prev_dir, exist_ok=True)

    # -- encode a before/after pair for a restored span ------------------------
    def _vf(self):
        return "scale=-2:%d:flags=bilinear" % self.height

    def _encode(self, src, start, dur, dst, stop):
        try:
            file_start, video_start, _ = _probe(self.ffprobe, src, stop=stop)
            ss = max(0.0, start + video_start - file_start)
            _run([self.ffmpeg, "-nostdin", "-loglevel", "error", "-abort_on", "empty_output_stream", "-y",
                  "-ss", f"{ss:.6f}", "-i", str(src), "-t", f"{dur:.6f}", "-map", "0:v:0",
                  "-an", "-vf", self._vf(), "-c:v", self.encoder, "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", dst], timeout=120, stop=stop, shared_input=src)
            return os.path.getsize(dst) > 0
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            return False

    def _emit(self, frag, start, lo, hi, stop=None):
        dur = min(hi - lo, MAX_PREVIEW_SECONDS)
        n = len(self.segments)
        after = os.path.join(self.prev_dir, f"seg{n}_after.mp4")
        before = os.path.join(self.prev_dir, f"seg{n}_before.mp4")
        if (self._encode(frag, lo - start, dur, after, stop)
                and self._encode(self.src, lo, dur, before, stop)):
            self.segments.append({"n": n, "start": round(lo, 3), "end": round(lo + dur, 3),
                                  "dur": round(dur, 3)})
            self._log("preview: segment %d ready [%.1f-%.1fs]" % (n, lo, lo + dur))
            if self.on_update:
                self.on_update(list(self.segments))
            return True
        if stop is None or not stop.is_set():
            self._log("preview: segment %d encode failed" % n)
        return False

    def finish(self):
        # A fast render can remove its checkpoint before the sidecar finishes.
        # In that case use the restored file (not the original) for one short
        # comparison; never delay the next pass by re-encoding every range.
        if not self.segments and self.jasna.ranges:
            lo, hi = self.jasna.ranges[0]
            self._emit(self.jasna.output, 0.0, lo, min(hi, lo + 8.0))
        return self.build_sample()

    def build_sample(self):
        """Concatenate the decensored segment clips (in time order) into one smoothly
        playable 'restored portions only' reel for review - no seeking needed. Returns
        the sample path or None. Clips are all the same 720p h264, so -c copy is clean."""
        if not self.segments:
            return None
        ordered = sorted(self.segments, key=lambda s: s["start"])
        listing = os.path.join(self.prev_dir, "sample_concat.txt")
        sample = os.path.join(self.prev_dir, "sample.mp4")
        try:
            with open(listing, "w", encoding="utf-8") as fh:
                for s in ordered:
                    clip = os.path.join(self.prev_dir, "seg%d_after.mp4" % s["n"])
                    if os.path.isfile(clip):
                        fh.write("file '%s'\n" % clip.replace("\\", "/").replace("'", "'\\''"))
            _run([self.ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                  "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart", sample], timeout=60)
            if os.path.getsize(sample) > 0:
                self._log("preview: built decensored sample (%d segments)" % len(ordered))
                return sample
        except (OSError, subprocess.SubprocessError):
            self._log("preview: sample build failed")
        return None

    def run(self, stop_event, poll=1.5):
        while not stop_event.wait(poll):
            for path, start, end in self.jasna.completed:
                if not path.is_file():
                    continue
                for lo, hi in self.jasna.ranges:
                    if stop_event.is_set():
                        return
                    lo, hi = max(lo, start), min(hi, end)
                    if lo >= hi or (path, lo) in self._emitted:
                        continue
                    self._emitted.add((path, lo))
                    self._emit(path, start, lo, hi, stop_event)
