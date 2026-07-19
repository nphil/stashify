"""Segment live-preview for the Stashify Windows runner.

In jasna >=0.8.0 smart mode (--segments), each timeline span is encoded to its own
NNNN.ts fragment in a hidden temp dir next to the output, and the temp dir is
deleted when the job ends. This watcher copies the restored (mosaic) fragments out
DURING the run and builds a downscaled before/after mp4 pair per mosaic segment,
so the dashboard can show original-vs-decensored side by side, live, for only the
mosaic parts - with no extra GPU work (the fragments are a free byproduct, and the
small preview re-encodes run off the 3080).

Span -> mosaic identification is by cumulative-time overlap with the scanned ranges
(no reliance on jasna internals): fragments are written in index order, so summing
their durations gives each one's [start,end) on the shared input/output timeline;
a fragment that overlaps a requested range is a restored span.
"""
import os
import glob
import json
import time
import subprocess
import threading

IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

MAX_PREVIEW_SECONDS = 45.0   # cap a segment preview clip. A long fragment must NEVER
                             # trigger a multi-minute before/after encode: a video with a
                             # big mosaic-free gap produces one giant copy fragment, and
                             # encoding its full length over SMB froze the whole preview.


def _run(argv, timeout=120):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NOWIN)


def _safe_run(argv, timeout=120):
    """Run ffmpeg, returning True on success. Never raises - a watcher-thread encode
    must not crash the loop (or block a fragment) on a timeout/OS error."""
    try:
        return _run(argv, timeout=timeout).returncode == 0
    except Exception:  # noqa: BLE001  (incl. subprocess.TimeoutExpired)
        return False


def _probe_duration(ffprobe, path):
    try:
        r = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", path], timeout=30)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def _mosaic_window(start, end, ranges):
    """Return (wstart, wend): the portion of fragment [start,end) that holds restored
    mosaic content (its intersection with the scanned ranges), or None if this fragment
    is not a restored span.

    A render fragment keyframe-pads and largely contains its range, so the intersection
    dominates it. A COPY fragment sits between ranges and at most edge-touches one; a LONG
    copy fragment (a multi-minute mosaic-free gap) must return None - otherwise the watcher
    builds a full-length before/after pair for it, which froze the preview at that segment."""
    lo = hi = None
    for s, e in ranges:
        os_, oe = max(start, s), min(end, e)
        if oe - os_ >= min(0.3, 0.5 * (e - s)):
            lo = os_ if lo is None else min(lo, os_)
            hi = oe if hi is None else max(hi, oe)
    if lo is None:
        return None
    # a long fragment that's mostly outside any range is a copy span, not a segment
    if (hi - lo) < 0.5 * (end - start) and (end - start) > MAX_PREVIEW_SECONDS:
        return None
    return lo, hi


class SegmentPreview:
    """Watcher started as a daemon thread for a preview-enabled decensor job."""

    def __init__(self, *, jid, watch_dir, out_stem, src, ranges, prev_dir,
                 ffmpeg, ffprobe, encoder="libx264", height=720, on_update=None,
                 log=None):
        self.jid = jid
        self.watch_dir = watch_dir            # dir where jasna writes .<stem>.segments-*
        self.out_stem = out_stem              # jasna --output stem (<input>_decensored)
        self.src = src                        # original input (for the "before" cut)
        self.ranges = ranges                  # [[start,end], ...] scanned mosaic ranges
        self.prev_dir = prev_dir              # local dir we write seg<N>_before/after.mp4
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.encoder = encoder
        self.height = int(height)
        self.on_update = on_update            # callback(list_of_segment_dicts)
        self._log = log or (lambda m: None)
        self.segments = []                    # published: {n,start,end,before,after,dur}
        self._cum = 0.0
        self._next_idx = 0
        self._seg_no = 0
        os.makedirs(prev_dir, exist_ok=True)

    # -- fragment discovery ----------------------------------------------------
    def _seg_temp_dir(self):
        # hidden TemporaryDirectory: .<stem>.segments-XXXXXXXX next to the output
        hits = glob.glob(os.path.join(self.watch_dir, ".*.segments-*"))
        hits = [d for d in hits if os.path.isdir(d)]
        return hits[0] if hits else None

    def _fragment(self, temp_dir, idx):
        for suf in (".ts", ".mkv"):
            p = os.path.join(temp_dir, "%04d%s" % (idx, suf))
            if os.path.isfile(p):
                return p
        return None

    def _is_done(self, temp_dir, idx, path):
        # a fragment is complete once the NEXT span's raw/normalized file exists
        # (jasna writes spans strictly in order), or it stops growing.
        nxt = self._fragment(temp_dir, idx + 1)
        if nxt or os.path.exists(os.path.join(temp_dir, "%04d-raw.nut" % (idx + 1))):
            return True
        if os.path.exists(os.path.join(temp_dir, "assembled.ts")) or \
           os.path.exists(os.path.join(temp_dir, "assembled.mkv")):
            return True   # all spans emitted; final assembly started
        return False

    # -- encode a before/after pair for a restored span ------------------------
    def _vf(self):
        return "scale=-2:%d:flags=bilinear" % self.height

    def _encode_after(self, frag, ss, t, dst):
        # downscale only the mosaic window [ss, ss+t) of the restored fragment off the
        # 3080 (no -c copy: light, browser-friendly, faststart, normalized size). ss/t
        # keep this bounded so a long fragment can't trigger a multi-minute encode.
        argv = [self.ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                "-ss", "%.3f" % ss, "-i", frag, "-t", "%.3f" % t,
                "-an", "-vf", self._vf(), "-c:v", self.encoder, "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", dst]
        return _safe_run(argv, timeout=120)

    def _encode_before(self, start, dur, dst):
        # cut the matching original window (accurate seek + re-encode for exact
        # frame alignment with the after clip)
        argv = [self.ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                "-ss", "%.3f" % start, "-i", self.src, "-t", "%.3f" % dur,
                "-an", "-vf", self._vf(), "-c:v", self.encoder, "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", dst]
        return _safe_run(argv, timeout=120)

    def _emit(self, idx, frag_start, win, frag):
        wstart, wend = win
        pdur = min(wend - wstart, MAX_PREVIEW_SECONDS)
        ss = max(0.0, wstart - frag_start)     # mosaic window offset inside the fragment
        n = self._seg_no
        after = os.path.join(self.prev_dir, "seg%d_after.mp4" % n)
        before = os.path.join(self.prev_dir, "seg%d_before.mp4" % n)
        ok_a = self._encode_after(frag, ss, pdur, after)
        ok_b = self._encode_before(wstart, pdur, before)
        if ok_a and ok_b:
            self._seg_no += 1
            seg = {"n": n, "start": round(wstart, 3), "end": round(wstart + pdur, 3),
                   "dur": round(pdur, 3)}
            self.segments.append(seg)
            self._log("preview: segment %d ready [%.1f-%.1fs]" % (n, wstart, wstart + pdur))
            if self.on_update:
                try:
                    self.on_update(list(self.segments))
                except Exception:  # noqa: BLE001
                    pass
        else:
            self._log("preview: segment encode failed (after=%s before=%s)" % (ok_a, ok_b))

    def build_sample(self):
        """Concatenate the decensored segment clips (in time order) into one smoothly
        playable 'restored portions only' reel for review - no seeking needed. Returns
        the sample path or None. Clips are all the same 720p h264, so -c copy is clean."""
        if not self.segments:
            return None
        ordered = sorted(self.segments, key=lambda s: s["n"])
        listing = os.path.join(self.prev_dir, "sample_concat.txt")
        with open(listing, "w", encoding="utf-8") as fh:
            for s in ordered:
                clip = os.path.join(self.prev_dir, "seg%d_after.mp4" % s["n"])
                if os.path.isfile(clip):
                    fh.write("file '%s'\n" % clip.replace("\\", "/").replace("'", "'\\''"))
        sample = os.path.join(self.prev_dir, "sample.mp4")
        r = _run([self.ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                  "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart",
                  sample], timeout=180)
        if r.returncode == 0 and os.path.isfile(sample) and os.path.getsize(sample) > 0:
            self._log("preview: built decensored sample (%d segments)" % len(ordered))
            return sample
        # concat -c copy can fail on timestamp gaps; retry with a re-encode
        r = _run([self.ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                  "-safe", "0", "-i", listing, "-c:v", self.encoder, "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", sample], timeout=300)
        if r.returncode == 0 and os.path.isfile(sample) and os.path.getsize(sample) > 0:
            self._log("preview: built decensored sample (%d segments, re-encoded)" % len(ordered))
            return sample
        self._log("preview: sample build failed")
        return None

    # -- main loop -------------------------------------------------------------
    def run(self, stop_event, poll=1.5):
        try:
            self._loop(stop_event, poll)
        except Exception as exc:  # noqa: BLE001 - never let preview crash the job
            self._log("preview: watcher error: %r" % exc)

    def _loop(self, stop_event, poll):
        # wait for the temp dir to appear (jasna reaches smart-render)
        temp_dir = None
        while temp_dir is None:
            if stop_event.wait(poll):
                return
            temp_dir = self._seg_temp_dir()
        self._log("preview: watching %s" % os.path.basename(temp_dir))
        drained = False
        while True:
            stopped = stop_event.wait(poll)
            # process every fragment that is complete, in strict index order
            while True:
                frag = self._fragment(temp_dir, self._next_idx)
                if not frag or not self._is_done(temp_dir, self._next_idx, frag):
                    break
                dur = _probe_duration(self.ffprobe, frag)
                if dur is None or dur <= 0:
                    dur = 0.0
                start = self._cum
                self._cum += dur
                win = _mosaic_window(start, start + dur, self.ranges) if dur > 0 else None
                if win:
                    self._emit(self._next_idx, start, win, frag)
                self._next_idx += 1
            if stopped:
                if drained or not os.path.isdir(temp_dir):
                    return
                drained = True   # one more sweep after stop, then exit
