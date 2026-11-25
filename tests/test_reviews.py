"""A job awaiting review survives a restart of the coordinator."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_dashboard as td          # noqa: E402,F401  (stand-in for `requests`, test env)
import server                        # noqa: E402


class ReviewStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.old = server.REVIEW_STORE
        server.REVIEW_STORE = os.path.join(self.dir.name, "reviews.json")
        self.addCleanup(setattr, server, "REVIEW_STORE", self.old)
        self.addCleanup(server._jobs.clear)
        server._jobs.clear()

    def test_review_ready_job_comes_back_after_a_restart_and_leaves_when_decided(self):
        job = server.new_job("42", {}, title="Scene")
        server.set_job(job["id"], state="running")
        server.set_job(job["id"], state="review_ready", _info={"orig_scene_id": "42", "output_path": "/x.mp4"})
        server._jobs.clear()                                  # the restart
        server.load_reviews()
        back = server._jobs[job["id"]]
        self.assertEqual(back["state"], "review_ready")
        self.assertEqual(back["_info"]["output_path"], "/x.mp4")
        server.set_job(job["id"], state="replaced")
        server._jobs.clear()
        server.load_reviews()
        self.assertEqual(server._jobs, {})


if __name__ == "__main__":
    unittest.main()
