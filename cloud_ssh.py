"""SSH + rsync transport to a rented cloud box (stdlib only).

Why plain `ssh` and `rsync` subprocesses instead of an SSH library: the image only
needs `openssh-client` and `rsync` (small, boring, everywhere), rsync resumes and
verifies multi-GB video transfers for free, and no new pip dependency is added.

Used by cloud_vast.py; it never talks to Vast itself. Tests swap this whole class
for a fake (see Transport) or point PATH at fake `ssh`/`rsync` scripts.
"""

import collections
import os
import re
import selectors
import shlex
import signal
import subprocess
import time
from typing import NamedTuple

POLL_S = 0.5          # how often a running command re-checks cancel/timeout
TAIL_LINES = 200      # output kept for error messages
INT_GRACE_S = 25.0    # a Ctrl-C'd remote program gets this long to exit cleanly


class SshError(Exception):
    """ssh/rsync could not do the job (message is already redacted)."""


class TimedOut(SshError):
    """The command ran past its timeout and was killed."""


class Cancelled(Exception):
    """The caller's cancel() callable returned true; the command was stopped."""


class Endpoint(NamedTuple):
    host: str
    port: int


class RunResult(NamedTuple):
    code: int
    tail: list


def _identity(text):
    return text


_SAFE_REMOTE = re.compile(r"^/[A-Za-z0-9_./-]+$")


def safe_remote_path(path):
    """Remote paths go into a shell command line and an rsync spec, so only plain
    path characters are allowed (callers generate these names themselves)."""
    if not _SAFE_REMOTE.match(path or "") or ".." in path.split("/"):
        raise SshError("unsafe remote path: %r" % (path,))
    return path


def split_lines(buf):
    """Split a byte buffer on \\r and \\n (tqdm and rsync redraw with \\r).
    Returns (complete_lines_as_bytes, unterminated_rest)."""
    parts = re.split(rb"[\r\n]", bytes(buf))
    rest = parts.pop()
    return [p for p in parts if p.strip()], rest


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)     # start_new_session => pgid == pid
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def stream_process(argv, *, redact=_identity, on_line=None, timeout=None, cancel=None,
                   stdin_data=None, interactive=False, prompts=(), grace=0.0,
                   tail_lines=TAIL_LINES, env=None):
    """Run argv and stream its merged stdout/stderr as lines. Returns RunResult.

    * cancel(): called every POLL_S. It may raise (its exception propagates after the
      process is stopped) or return true (-> Cancelled).
    * timeout: seconds, then the process is stopped and TimedOut raised.
    * stdin_data: bytes written once, then stdin is closed (unless interactive).
    * interactive: keep stdin open (a pty session) so prompts can be answered and
      Ctrl-C can be sent. prompts = [(compiled_regex, reply_bytes)] are matched
      against the still-unterminated output (a prompt has no newline) and answered
      once each time they appear.
    * grace: when stopping an interactive process, send Ctrl-C first and give it
      this many seconds to exit by itself (so it can release its licence) before
      the whole process group is killed.
    """
    use_stdin = interactive or stdin_data is not None
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, bufsize=0, env=env)
    tail = collections.deque(maxlen=tail_lines)
    buf = bytearray()
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = (time.monotonic() + timeout) if timeout else None

    def emit(raw):
        line = redact(raw.decode("utf-8", "replace").strip())
        if not line:
            return
        tail.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 - a progress parser must never break the transfer
                pass

    def pump(wait):
        """Read what's available (waiting up to `wait` s). False at EOF."""
        if not sel.select(wait):
            return True
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            return False
        buf.extend(chunk)
        lines, rest = split_lines(buf)
        del buf[:]
        buf.extend(rest)
        for raw in lines:
            emit(raw)
        if rest and use_stdin:
            for rx, reply in prompts:
                if rx.search(rest.decode("utf-8", "replace")):
                    try:
                        proc.stdin.write(reply)
                        proc.stdin.flush()
                    except OSError:
                        pass
                    emit(bytes(buf))       # show the prompt that was answered
                    del buf[:]
                    break
        return True

    def stop():
        """Stop the process: Ctrl-C + grace for interactive sessions, then kill."""
        if proc.poll() is None and interactive and grace:
            try:
                proc.stdin.write(b"\x03")
                proc.stdin.flush()
            except OSError:
                pass
            end = time.monotonic() + grace
            while proc.poll() is None and time.monotonic() < end:
                try:
                    if not pump(0.2):
                        break
                except OSError:
                    break
        if proc.poll() is None:
            _kill_group(proc)

    try:
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.flush()
            except OSError:
                pass
            if not interactive:
                proc.stdin.close()
        while pump(POLL_S):
            if cancel is not None:
                try:
                    cancelled = cancel()
                except BaseException:
                    stop()
                    raise
                if cancelled:
                    stop()
                    raise Cancelled("cancelled")
            if deadline is not None and time.monotonic() > deadline:
                stop()
                raise TimedOut("timed out after %ds" % int(timeout))
        if buf:
            emit(bytes(buf))
        proc.wait()
        return RunResult(proc.returncode, list(tail))
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - nothing more we can do for a stuck reap
            pass
        for fh in (proc.stdout, proc.stdin):
            try:
                if fh:
                    fh.close()
            except OSError:
                pass
        sel.close()


class Transport:
    """What cloud_vast needs from a transport. SshTransport is the real one."""

    def public_key(self):
        raise NotImplementedError

    def probe(self, ep, tag, cancel=None):
        """('ok'|'denied'|'down', detail): can we log in to the box right now?"""
        raise NotImplementedError

    def run(self, ep, tag, cmd, *, env=None, stdin_data=None, on_line=None, timeout=None,
            cancel=None, tty=False, prompts=(), grace=0.0):
        raise NotImplementedError

    def put(self, ep, tag, local, remote, *, on_line=None, timeout=None, cancel=None):
        raise NotImplementedError

    def get(self, ep, tag, remote, local, *, on_line=None, timeout=None, cancel=None):
        raise NotImplementedError

    def cleanup(self, tag):
        """Forget per-box state (its known_hosts file)."""


class SshTransport(Transport):
    def __init__(self, state_dir, redact=_identity, ssh="ssh", rsync="rsync",
                 keygen="ssh-keygen"):
        self.dir = state_dir
        self.redact = redact
        self.ssh, self.rsync, self.keygen = ssh, rsync, keygen
        self.key = os.path.join(state_dir, "id_ed25519")

    # -- key ---------------------------------------------------------------- #

    def public_key(self):
        """One ed25519 key per coordinator, created on first use and kept in the
        persisted config dir so a restart doesn't strand a running box."""
        if not os.path.isfile(self.key + ".pub") or not os.path.isfile(self.key):
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            for stale in (self.key, self.key + ".pub"):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            r = subprocess.run([self.keygen, "-q", "-t", "ed25519", "-N", "", "-C",
                                "stashify-coordinator", "-f", self.key],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if r.returncode != 0:
                raise SshError("ssh-keygen failed: " + self.redact((r.stderr or "").strip()))
        with open(self.key + ".pub", encoding="utf-8") as fh:
            return fh.read().strip()

    # -- command lines ------------------------------------------------------ #

    def _known_hosts(self, tag):
        return os.path.join(self.dir, "known_hosts." + re.sub(r"[^A-Za-z0-9_.-]", "_", str(tag)))

    def _opts(self, tag, connect_timeout=10):
        # A rented box gets a new host key every time and ports/IPs get reused, so the
        # known_hosts file is per instance (never a stale-key clash) and first use is
        # accepted; the key then pins for that box's life.
        return ["-i", self.key,
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "UserKnownHostsFile=" + self._known_hosts(tag),
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=%d" % connect_timeout,
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                "-o", "LogLevel=ERROR", "-o", "PreferredAuthentications=publickey"]

    def _ssh_argv(self, ep, tag, remote_cmd, tty=False, connect_timeout=10):
        return ([self.ssh, "-p", str(ep.port)] + self._opts(tag, connect_timeout)
                + ["-tt" if tty else "-T", "root@" + ep.host, remote_cmd])

    # -- operations --------------------------------------------------------- #

    def probe(self, ep, tag, cancel=None):
        try:
            res = stream_process(self._ssh_argv(ep, tag, "true", connect_timeout=8),
                                 redact=self.redact, timeout=25, cancel=cancel)
        except SshError as exc:
            return "down", str(exc)
        if res.code == 0:
            return "ok", ""
        text = " ".join(res.tail)
        return ("denied" if "Permission denied" in text else "down"), text[-300:]

    def run(self, ep, tag, cmd, *, env=None, stdin_data=None, on_line=None, timeout=None,
            cancel=None, tty=False, prompts=(), grace=0.0):
        if env:
            for k in env:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                    raise SshError("bad environment variable name: %r" % (k,))
            cmd = "; ".join("export %s=%s" % (k, shlex.quote(str(v))) for k, v in env.items()) + "; " + cmd
        return stream_process(
            self._ssh_argv(ep, tag, cmd, tty=tty), redact=self.redact, on_line=on_line,
            timeout=timeout, cancel=cancel, stdin_data=stdin_data, interactive=tty,
            prompts=prompts, grace=grace)

    def _rsync(self, ep, tag, src, dst, on_line, timeout, cancel):
        ssh_cmd = shlex.join([self.ssh, "-p", str(ep.port)] + self._opts(tag) + ["-T"])
        argv = [self.rsync, "-a", "--no-owner", "--no-group", "--partial", "--timeout=120",
                "--info=progress2", "-e", ssh_cmd, src, dst]
        res = stream_process(argv, redact=self.redact, on_line=on_line, timeout=timeout,
                             cancel=cancel)
        if res.code != 0:
            raise SshError("rsync failed (exit %d): %s" % (res.code, " | ".join(res.tail[-5:])))

    def put(self, ep, tag, local, remote, *, on_line=None, timeout=None, cancel=None):
        local = os.path.abspath(local)      # absolute: a ':' in a name must not look like host:path
        self._rsync(ep, tag, local, "root@%s:%s" % (ep.host, safe_remote_path(remote)),
                    on_line, timeout, cancel)

    def get(self, ep, tag, remote, local, *, on_line=None, timeout=None, cancel=None):
        local = os.path.abspath(local)
        self._rsync(ep, tag, "root@%s:%s" % (ep.host, safe_remote_path(remote)), local,
                    on_line, timeout, cancel)

    def cleanup(self, tag):
        try:
            os.remove(self._known_hosts(tag))
        except OSError:
            pass
        try:
            os.remove(self._known_hosts(tag) + ".old")
        except OSError:
            pass
