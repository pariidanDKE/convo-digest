"""Regression tests for src/digest.workflow.js, run under stubbed workflow hooks.

Run: python3 -m unittest discover -s tests   (from repo root; needs `node`, else skipped)

The drain loop keys on the workflow's `status`. A zero used to mean both "nothing left"
and "everything broke", so a dead prep agent or a run where every summarizer failed
ended the drain and read as success (#15). These tests pin the three statuses, the
one-shot merge retry, the lost-work accounting, and that chunks are staged through
`index.py --stage-chunk` with the JSON intact (no Write tool, #14).
"""
import json
import os
import shutil
import subprocess
import unittest

HARNESS = os.path.join(os.path.dirname(__file__), "workflow_harness.js")
NODE = shutil.which("node")

CONVOS = [{"key": f"p__{i}", "id": f"id{i}", "work_path": f"/w/{i}.json", "tier": "whole"}
          for i in range(3)]
PREP = {"convos": CONVOS, "counts": {}, "stage_dir": "/digest/work/batches/run1"}


def run(**scenario):
    out = subprocess.run([NODE, HARNESS, json.dumps(scenario)], capture_output=True,
                         text=True, timeout=60)
    data = json.loads(out.stdout)
    if "error" in data:
        raise AssertionError(data["error"])
    return data["result"], data["calls"]


@unittest.skipUnless(NODE, "node is not installed")
class WorkflowStatusTest(unittest.TestCase):
    def test_a_dead_prep_agent_is_a_failure_not_a_zero(self):
        result, _ = run(prepare=None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "prep")

    def test_a_prep_error_is_a_failure(self):
        result, _ = run(prepare={"convos": [], "error": "Command timed out"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("timed out", result["error"])

    def test_nothing_to_do_is_drained(self):
        result, calls = run(prepare={"convos": [], "counts": {}, "stage_dir": "/s"})
        self.assertEqual(result["status"], "drained")
        self.assertEqual(calls, ["prepare"])

    def test_a_normal_batch_is_progress(self):
        result, calls = run(prepare=PREP, merge={"written": 3, "index_size": 800})
        self.assertEqual(result["status"], "progress")
        self.assertEqual(result["indexed"], 3)
        self.assertNotIn("merge-retry", calls)

    def test_every_summarizer_failing_is_a_failure(self):
        result, calls = run(prepare=PREP, sum=[None, None, None])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "summarize")
        self.assertEqual(len(result["lost"]["summaries"]), 3)
        self.assertFalse(any(c.startswith("merge") for c in calls))

    def test_a_lost_summary_is_reported_alongside_progress(self):
        result, _ = run(prepare=PREP, sum=["ok", None, "ok"],
                        merge={"written": 2, "index_size": 800})
        self.assertEqual(result["status"], "progress")
        self.assertEqual(result["lost"]["summaries"], ["p__1"])

    def test_a_dead_merge_agent_is_retried_once(self):
        result, calls = run(prepare=PREP, merge=None,
                            **{"merge-retry": {"written": 3, "replayed": True}})
        self.assertEqual(result["status"], "progress")
        self.assertIn("merge-retry", calls)

    def test_a_merge_that_dies_twice_is_a_failure(self):
        result, _ = run(prepare=PREP, merge=None, **{"merge-retry": None})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "merge")

    def test_no_chunk_staged_is_a_failure(self):
        result, _ = run(prepare=PREP, batch={"count": 0, "error": "invalid chunk"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "stage")

    def test_chunks_are_staged_via_index_py_not_the_write_tool(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "src",
                               "digest.workflow.js"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("--stage-chunk", src)
        self.assertIn("--result-file", src)
        self.assertNotIn("using the Write tool", src)


if __name__ == "__main__":
    unittest.main()
