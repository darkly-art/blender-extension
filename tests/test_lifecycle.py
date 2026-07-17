"""Tests for `lifecycle.run_guarded` - the structural guarantee that teardown
steps are unskippable. The port-leak regression this defends against: a raise
in an early `stop()` step used to skip `server.stop_server`, leaving the port
held with no UI path to recover."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly"))

import lifecycle  # noqa: E402

log = logging.getLogger("test_lifecycle")


class RunGuardedTest(unittest.TestCase):
    def test_all_steps_run_when_one_raises(self):
        ran = []
        boom = RuntimeError("a exploded")

        def a():
            raise boom

        failures = lifecycle.run_guarded(
            [
                ("a", a),
                ("b", lambda: ran.append("b")),
                ("c", lambda: ran.append("c")),
            ],
            log,
        )
        # b and c ran despite a raising; the failure list names a.
        self.assertEqual(ran, ["b", "c"])
        self.assertEqual(failures, [("a", boom)])

    def test_steps_run_in_order(self):
        ran = []
        failures = lifecycle.run_guarded(
            [(name, lambda name=name: ran.append(name)) for name in "abc"],
            log,
        )
        self.assertEqual(ran, ["a", "b", "c"])
        self.assertEqual(failures, [])

    def test_empty_steps_is_a_no_op(self):
        self.assertEqual(lifecycle.run_guarded([], log), [])

    def test_multiple_failures_are_all_collected(self):
        first, second = ValueError("x"), KeyError("y")
        failures = lifecycle.run_guarded(
            [
                ("first", lambda: (_ for _ in ()).throw(first)),
                ("second", lambda: (_ for _ in ()).throw(second)),
            ],
            log,
        )
        self.assertEqual(failures, [("first", first), ("second", second)])


if __name__ == "__main__":
    unittest.main()
