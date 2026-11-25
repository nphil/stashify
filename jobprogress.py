"""One honest, always-current status for a job that runs on a runner.

The runner reports its own live job (stage, progress, message, and on newer runners phase,
step/steps, pass_no/passes, pass_progress, detail, estimate, idle_s). The coordinator turns each
poll of it into the fields the dashboard shows: a plain-words phase, the step, a whole-job
progress that never goes backwards, the time left and when the job last gave a sign of life.

Whole-job progress is weighted by TIME, not by equal shares: a decensor pass that took two hours
must not count the same as the upscale after it. Passes already finished are measured; a pass
that has not started yet is a guess (LATER_PASS_RATIO of the first pass), and the job says so
(`estimate`). Older runners send none of the new fields and still work: the same bands as
before, the runner's own message as the phase, and "last update" from when anything changed.
"""

import time

# where the early, short stages sit in the whole bar; the main work fills the rest
STAGE_BANDS = {"copy": (0.0, 0.06), "scan": (0.06, 0.12)}
MAIN_BAND = (0.12, 1.0)
# an unmeasured later pass (the upscale after a decensor) is guessed at this share of the first
LATER_PASS_RATIO = 0.5

PHASE_BY_STAGE = {"copy": "Copying video to the PC", "scan": "Scanning for mosaics"}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def plain_phase(jb):
    """The runner's phase, else a plain version of what an older runner said: its stage, or its
    message without a trailing percentage."""
    ph = str(jb.get("phase") or "").strip()
    if ph:
        return ph
    stage = jb.get("stage") or ""
    if stage in PHASE_BY_STAGE:
        return PHASE_BY_STAGE[stage]
    msg = str(jb.get("message") or "").strip()
    for sep in (" — ", " - "):
        if sep in msg and msg.rsplit(sep, 1)[1].rstrip().endswith("%"):
            msg = msg.rsplit(sep, 1)[0]
    return msg or None


class Tracker:
    """Per job. update(runner_job) -> (fraction, message, stats) for the coordinator's progress callback.
    The fraction lies in [lo, hi] and only ever grows."""

    def __init__(self, lo=0.0, hi=1.0, clock=time.time):
        self.lo, self.hi, self.clock = lo, hi, clock
        self.starts = {}        # pass number -> when the main work of that pass was first seen
        self.spent = {}         # pass number -> how long it took, once the next pass began
        self.best = 0.0
        self.sig = None
        self.stamp = None

    # -- the main work, one or more passes --------------------------------------------- #
    def _main(self, jb, now):
        """-> (progress 0..1 within the main band, seconds left or None, estimate?)"""
        passes = max(1, _int(jb.get("passes"), 1))
        k = min(passes, max(1, _int(jb.get("pass_no"), 1)))
        prog = _num(jb.get("progress"))
        if prog is None:           # a runner that reports frame/total but no 0-1 progress (lada)
            fr, tot = jb.get("frame"), jb.get("total_frames")
            prog = (fr / tot) if (fr and tot) else 0.0
        prog = min(1.0, max(0.0, prog))
        eta = _num(jb.get("eta"))
        estimate = bool(jb.get("estimate"))
        if passes == 1:
            return prog, eta, estimate

        pp = _num(jb.get("pass_progress"))
        pp = min(1.0, max(0.0, pp if pp is not None else prog * passes - (k - 1)))
        self.starts.setdefault(k, now)
        for j in range(1, k):
            if j not in self.spent and j in self.starts and (j + 1) in self.starts:
                self.spent[j] = max(0.0, self.starts[j + 1] - self.starts[j])
        elapsed = now - self.starts[k]
        known = all(j in self.spent for j in range(1, k))
        left = eta if eta is not None else (elapsed * (1 - pp) / pp if pp >= 0.02 and elapsed >= 20 else None)
        base = self.spent.get(1)
        if left is None and k > 1 and base:        # the pass has not reported a speed yet: guess from the first
            left = max(0.0, LATER_PASS_RATIO * base - elapsed)
            estimate = True
        if not known or left is None:              # nothing measured to weigh by: equal shares
            return (k - 1 + pp) / passes, None, True
        done = sum(self.spent[j] for j in range(1, k))
        cur = elapsed + left
        first = base or cur
        future = (passes - k) * LATER_PASS_RATIO * first
        total = done + cur + future
        frac = (done + elapsed) / total if total > 0 else 0.0
        return min(1.0, max(0.0, frac)), left + future, estimate or future > 0

    def update(self, jb):
        now = self.clock()
        stage = jb.get("stage") or ""
        band = STAGE_BANDS.get(stage)
        if band:
            a, b = band
            prog = _num(jb.get("progress"))
            m = min(1.0, max(0.0, prog or 0.0))
            eta, estimate = None, False        # a copy's or scan's own time left is not the job's
        else:
            a, b = MAIN_BAND
            m, eta, estimate = self._main(jb, now)
        raw = self.lo + (self.hi - self.lo) * (a + (b - a) * m)
        self.best = max(self.best, raw)
        frac = self.best
        stats = {
            "stage": stage or None,
            "frame": jb.get("frame"),
            "total_frames": jb.get("total_frames"),
            "fps": jb.get("fps"),
            "eta": None if eta is None else int(eta),
            "phase": plain_phase(jb),
            "step": jb.get("step"),
            "steps": jb.get("steps"),
            "detail": jb.get("detail"),
            "pass_no": jb.get("pass_no") if _int(jb.get("passes"), 1) > 1 else None,
            "passes": jb.get("passes") if _int(jb.get("passes"), 1) > 1 else None,
            "estimate": estimate,
        }
        # A runner that sends the flag is authoritative. One that does not (lada) would leave the
        # seeded True on forever, so real numbers assert determinate; before any, leave it unset.
        has_num = (_num(jb.get("progress")) or 0) > 0 or bool(jb.get("frame") and jb.get("total_frames"))
        if jb.get("indeterminate") is not None:
            stats["indeterminate"] = bool(jb.get("indeterminate"))
        elif has_num:
            stats["indeterminate"] = False
        # last sign of life: how long the runner was silent (no clock skew), else when anything changed
        idle = _num(jb.get("idle_s"))
        if idle is not None:
            self.stamp = now - max(0.0, idle)
        else:
            sig = (round(frac, 3), jb.get("message"), jb.get("frame"), stats["phase"])
            if sig != self.sig or self.stamp is None:
                self.sig, self.stamp = sig, now
        stats["updated_at"] = self.stamp
        return frac, jb.get("message"), stats
