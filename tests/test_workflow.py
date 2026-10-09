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
        # nothing to summarize, and the tag check finds nothing waiting either
        self.assertEqual(calls, ["prepare", "untagged:0"])

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

NOTHING = {"convos": [], "counts": {}, "stage_dir": "/s"}
TAGS = {"tags": [{"key": "k1", "workstream": "Atlas AI tool access", "kind": "build"}]}


@unittest.skipUnless(NODE, "node is not installed")
class WorkflowTagTest(unittest.TestCase):
    def test_older_records_are_tagged_a_few_batches_per_run(self):
        result, calls = run(prepare=NOTHING,
                            untagged={"path": "/w/tags/batch.json", "count": 20, "remaining": 50},
                            tag=TAGS, apply={"applied": 20, "new": [], "skipped": []})
        self.assertEqual(result["status"], "progress")
        self.assertEqual(result["tagged"], 80)
        self.assertEqual([c for c in calls if c.startswith("tag:")], ["tag:0", "tag:1", "tag:2", "tag:3"])
        self.assertFalse(any(c.startswith("curate") for c in calls))

    def test_new_workstreams_go_past_the_curator_and_duplicates_are_merged(self):
        merges = {"merges": [{"from": "Atlas access", "into": "Atlas AI tool access"}]}
        result, calls = run(prepare=NOTHING,
                            untagged={"path": "/w/tags/batch.json", "count": 5, "remaining": 0},
                            tag=TAGS, apply={"applied": 5, "new": ["Atlas access"], "skipped": []},
                            **{"curate-file": {"path": "/w/tags/curate.json", "count": 1},
                               "curate": merges,
                               "merge-ws": {"merged": [{"from": "Atlas access",
                                                        "into": "Atlas AI tool access", "records": 3}]}})
        self.assertEqual(result["status"], "progress")
        self.assertIn("curate", calls)
        self.assertEqual(result["merged_workstreams"][0]["into"], "Atlas AI tool access")

    def test_a_summary_run_that_creates_a_workstream_is_curated_too(self):
        result, calls = run(prepare=PREP, merge={"written": 3, "index_size": 800,
                                                 "new_workstreams": ["Mission Control and digest"]},
                            **{"curate-file": {"path": "/w/tags/curate.json", "count": 1},
                               "curate": {"merges": []}})
        self.assertEqual(result["status"], "progress")
        self.assertEqual(result["new_workstreams"], ["Mission Control and digest"])
        self.assertNotIn("merge-ws", calls)          # nothing to merge, nothing run

    def test_tagging_that_fails_with_work_waiting_is_a_failure_not_drained(self):
        result, _ = run(prepare=NOTHING,
                        untagged={"path": "/w/tags/batch.json", "count": 20, "remaining": 50},
                        tag=None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "tag")


if __name__ == "__main__":
    unittest.main()
