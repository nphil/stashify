"""SshTransport against REAL subprocesses: fake `ssh` and `rsync` scripts on PATH.

Run:  python3 -B -m unittest discover -s tests -v
These prove the parts a fake transport cannot: line streaming and \\r splitting,
cancel/timeout kill the whole process group, the Ctrl-C grace, prompt replies, secrets
staying out of argv, rsync put/get.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cloud_ssh  # noqa: E402
from cloud_ssh import Endpoint, SshTransport  # noqa: E402

FAKE_SSH = r'''#!/bin/bash
cmd="${@: -1}"
echo "$@" >> "$FAKE_LOG"
case "$FAKE_SSH_MODE" in
  denied) echo "root@h: Permission denied (publickey)." >&2; exit 255;;
  down) echo "ssh: connect to host h port 1: Connection refused" >&2; exit 255;;
esac
case "$cmd" in
  true) exit 0;;
  STREAM) printf 'step 10%%\rstep 20%%\rstep 30%%\nlast line\n'; exit 0;;
  SLEEP) echo $$ > "$FAKE_PID"; echo started; sleep 60; exit 0;;
  PROMPT) printf 'Download (15 GB)? [y/N]: '; read -r ans; echo "got:$ans"; exit 0;;
  OTHERPROMPT) printf 'Continue? [y/N]: '; read -r ans; echo "got:$ans"; exit 0;;
  LICENSE) IFS= read -r code; echo "len:${#code}"; exit 0;;
  INTERRUPTIBLE) echo $$ > "$FAKE_PID"; echo running
     while IFS= read -rn1 c; do if [ "$c" = $'\x03' ]; then echo "interrupted cleanly"; exit 130; fi; done;;
  FAIL) echo "boom SECRET123 tail"; exit 3;;
esac
echo "unknown: $cmd" >&2; exit 99
'''

FAKE_RSYNC = r'''#!/bin/bash
args=("$@"); n=${#args[@]}
src="${args[$((n-2))]}"; dst="${args[$((n-1))]}"
echo "$@" >> "$FAKE_LOG"
map() { case "$1" in root@*:*) echo "$FAKE_REMOTE/${1#*:}";; *) echo "$1";; esac; }
s=$(map "$src"); d=$(map "$dst")
printf '  1,024  10%%  1.00MB/s  0:00:01\r  2,048 100%%  2.00MB/s  0:00:00 (xfr#1)\n'
if [ "$FAKE_RSYNC_FAIL" = 1 ]; then echo "rsync: connection unexpectedly closed" >&2; exit 12; fi
mkdir -p "$(dirname "$d")"; cp "$s" "$d"
'''

EP = Endpoint("203.0.113.9", 2222)


class SshTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cloudssh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
            p = os.path.join(bindir, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp, "argv.log")
        self.remote = os.path.join(self.tmp, "remote")
        os.makedirs(self.remote)
        self.pidfile = os.path.join(self.tmp, "pid")
        saved = {k: os.environ.get(k) for k in
                 ("PATH", "FAKE_LOG", "FAKE_PID", "FAKE_REMOTE", "FAKE_SSH_MODE", "FAKE_RSYNC_FAIL")}
        self.addCleanup(self._restore_env, saved)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        os.environ.update(FAKE_LOG=self.log, FAKE_PID=self.pidfile, FAKE_REMOTE=self.remote)
        os.environ.pop("FAKE_SSH_MODE", None)
        os.environ.pop("FAKE_RSYNC_FAIL", None)
        self.t = SshTransport(os.path.join(self.tmp, "state"),
                              redact=lambda s: s.replace("SECRET123", "***"))

    @staticmethod
    def _restore_env(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def argv_log(self):
        with open(self.log) as fh:
            return fh.read()

    # -- key ---------------------------------------------------------------- #

    def test_public_key_is_created_once_and_private_to_us(self):
        a = self.t.public_key()
        self.assertTrue(a.startswith("ssh-ed25519 "))
        self.assertEqual(a, self.t.public_key())              # stable: a restart keeps the key
        mode = stat.S_IMODE(os.stat(self.t.dir).st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.t.key).st_mode) & 0o077, 0)

    # -- probe -------------------------------------------------------------- #

    def test_probe_ok_denied_down(self):
        self.assertEqual(self.t.probe(EP, "7")[0], "ok")
        os.environ["FAKE_SSH_MODE"] = "denied"
        self.assertEqual(self.t.probe(EP, "7")[0], "denied")
        os.environ["FAKE_SSH_MODE"] = "down"
        state, detail = self.t.probe(EP, "7")
        self.assertEqual(state, "down")
        self.assertIn("refused", detail)

    def test_ssh_command_line_is_locked_down_and_per_instance(self):
        self.t.run(EP, "4242", "STREAM")
        line = self.argv_log().splitlines()[0]
        for needle in ("-p 2222", "BatchMode=yes", "StrictHostKeyChecking=accept-new",
                       "known_hosts.4242", "GlobalKnownHostsFile=/dev/null", "IdentitiesOnly=yes",
                       "-T", "root@203.0.113.9"):
            self.assertIn(needle, line)
        self.assertNotIn("-tt", line)
        self.t.run(EP, "4242", "STREAM", tty=True, timeout=20)
        self.assertIn("-tt", self.argv_log().splitlines()[1])

    # -- streaming ---------------------------------------------------------- #

    def test_stream_splits_on_carriage_return_and_newline(self):
        seen = []
        res = self.t.run(EP, "1", "STREAM", on_line=seen.append)
        self.assertEqual(res.code, 0)
        self.assertEqual(seen, ["step 10%", "step 20%", "step 30%", "last line"])
        self.assertEqual(res.tail, seen)

    def test_nonzero_exit_and_redaction(self):
        seen = []
        res = self.t.run(EP, "1", "FAIL", on_line=seen.append)
        self.assertEqual(res.code, 3)
        self.assertEqual(seen, ["boom *** tail"])

    def test_a_raising_progress_callback_does_not_break_the_run(self):
        def bad(_line):
            raise RuntimeError("parser bug")
        self.assertEqual(self.t.run(EP, "1", "STREAM", on_line=bad).code, 0)

    # -- secrets ------------------------------------------------------------ #

    def test_stdin_data_reaches_the_remote_but_never_argv(self):
        secret = "LICENSE-CODE-0123456789"
        seen = []
        res = self.t.run(EP, "1", "LICENSE", stdin_data=(secret + "\n").encode(), on_line=seen.append)
        self.assertEqual(res.code, 0)
        self.assertEqual(seen, ["len:%d" % len(secret)])
        self.assertNotIn(secret, self.argv_log())

    def test_env_values_are_quoted_into_the_command(self):
        self.t.run(EP, "1", "STREAM", env={"A_B": "x y; rm -rf /"})
        line = self.argv_log().splitlines()[0]
        self.assertIn("export A_B='x y; rm -rf /'; STREAM", line)
        with self.assertRaises(cloud_ssh.SshError):
            self.t.run(EP, "1", "STREAM", env={"bad name": "1"})

    # -- prompts ------------------------------------------------------------ #

    def test_prompt_is_answered_once_when_it_appears_without_a_newline(self):
        import re
        seen = []
        prompts = [(re.compile(r"Download \(.*\)\? \[y/N\]:\s*$"), b"y\n"),
                   (re.compile(r"\[y/N\]:\s*$"), b"n\n")]
        res = self.t.run(EP, "1", "PROMPT", tty=True, prompts=prompts, on_line=seen.append, timeout=20)
        self.assertEqual(res.code, 0)
        self.assertIn("got:y", seen)

    def test_any_other_prompt_gets_no(self):
        import re
        seen = []
        prompts = [(re.compile(r"Download \(.*\)\? \[y/N\]:\s*$"), b"y\n"),
                   (re.compile(r"\[y/N\]:\s*$"), b"n\n")]
        self.t.run(EP, "1", "OTHERPROMPT", tty=True, prompts=prompts, on_line=seen.append, timeout=20)
        self.assertIn("got:n", seen)

    # -- cancel / timeout / grace ------------------------------------------- #

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_cancel_kills_the_whole_process_group(self):
        t0 = time.monotonic()
        calls = []

        def cancel():
            calls.append(1)
            return time.monotonic() - t0 > 1.0
        with self.assertRaises(cloud_ssh.Cancelled):
            self.t.run(EP, "1", "SLEEP", cancel=cancel, timeout=30)
        self.assertLess(time.monotonic() - t0, 10)
        with open(self.pidfile) as fh:
            pid = int(fh.read())
        time.sleep(0.3)
        self.assertFalse(self._alive(pid))

    def test_a_raising_cancel_stops_the_process_then_propagates(self):
        class Boom(Exception):
            pass
        t0 = time.monotonic()

        def cancel():
            if time.monotonic() - t0 > 1.0:
                raise Boom("spend cap")
            return False
        with self.assertRaises(Boom):
            self.t.run(EP, "1", "SLEEP", cancel=cancel, timeout=30)
        with open(self.pidfile) as fh:
            pid = int(fh.read())
        time.sleep(0.3)
        self.assertFalse(self._alive(pid))

    def test_timeout_kills(self):
        t0 = time.monotonic()
        with self.assertRaises(cloud_ssh.TimedOut):
            self.t.run(EP, "1", "SLEEP", timeout=1.5)
        self.assertLess(time.monotonic() - t0, 10)
        with open(self.pidfile) as fh:
            pid = int(fh.read())
        time.sleep(0.3)
        self.assertFalse(self._alive(pid))

    def test_cancel_sends_ctrl_c_first_so_the_remote_can_exit_cleanly(self):
        t0, seen = time.monotonic(), []
        with self.assertRaises(cloud_ssh.Cancelled):
            self.t.run(EP, "1", "INTERRUPTIBLE", tty=True, grace=10, on_line=seen.append,
                       cancel=lambda: time.monotonic() - t0 > 1.0, timeout=60)
        self.assertIn("interrupted cleanly", seen)            # it got the chance to clean up
        self.assertLess(time.monotonic() - t0, 9)             # ...and did not wait the full grace
        with open(self.pidfile) as fh:
            self.assertFalse(self._alive(int(fh.read())))

    def test_ctrl_c_grace_ends_in_kill_when_the_remote_ignores_it(self):
        t0 = time.monotonic()
        with self.assertRaises(cloud_ssh.Cancelled):
            self.t.run(EP, "1", "SLEEP", tty=True, grace=2, cancel=lambda: time.monotonic() - t0 > 0.7,
                       timeout=60)
        self.assertLess(time.monotonic() - t0, 12)
        with open(self.pidfile) as fh:
            pid = int(fh.read())
        time.sleep(0.3)
        self.assertFalse(self._alive(pid))

    # -- rsync -------------------------------------------------------------- #

    def test_put_and_get_round_trip_with_progress(self):
        src = os.path.join(self.tmp, "video.mp4")
        with open(src, "wb") as fh:
            fh.write(b"x" * 4096)
        seen = []
        self.t.put(EP, "9", src, "/root/in/source.mp4", on_line=seen.append)
        self.assertTrue(os.path.isfile(os.path.join(self.remote, "root/in/source.mp4")))
        self.assertTrue(any("100%" in s for s in seen))
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        self.t.get(EP, "9", "/root/in/source.mp4", out + "/")
        self.assertEqual(os.path.getsize(os.path.join(out, "source.mp4")), 4096)
        line = self.argv_log().splitlines()[0]
        self.assertIn("--partial", line)
        self.assertNotIn(" -z", line)                          # video is already compressed

    def test_rsync_failure_raises_with_the_reason(self):
        os.environ["FAKE_RSYNC_FAIL"] = "1"
        src = os.path.join(self.tmp, "v.mp4")
        open(src, "wb").close()
        with self.assertRaises(cloud_ssh.SshError) as cm:
            self.t.put(EP, "9", src, "/root/in/v.mp4")
        self.assertIn("exit 12", str(cm.exception))

    def test_unsafe_remote_paths_are_refused(self):
        for bad in ("/root/in/a b.mp4", "/root/../etc/passwd", "relative.mp4", "/root/$(x)", "/a;b"):
            with self.assertRaises(cloud_ssh.SshError):
                self.t.put(EP, "9", __file__, bad)

    def test_cleanup_removes_the_known_hosts_file(self):
        os.makedirs(self.t.dir, exist_ok=True)
        kh = self.t._known_hosts("77")
        open(kh, "w").close()
        self.t.cleanup("77")
        self.assertFalse(os.path.exists(kh))


if __name__ == "__main__":
    unittest.main()
