"""Model-free, conservative reuse hints and boundary freshness regressions."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import evidence_reuse, broker_store
from pi_tmux_orchestrator.broker import Broker, initialize_broker_run
from pi_tmux_orchestrator.context_capsules import render_run_state_capsule
from pi_tmux_orchestrator.evidence_reuse import EvidenceReuse, worktree_stamp
from test_broker import BrokerFixture


class WorktreeStampTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q")
        (self.root / "code.py").write_text("original\n")
        self.git("add", "code.py")
        self.git(
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "synthetic",
        )

    def git(self, *arguments):
        subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_metadata_changes_invalidate_even_without_head_change(self):
        first = worktree_stamp(self.root)
        self.assertIsNotNone(first)
        self.assertEqual(worktree_stamp(self.root), first)
        (self.root / "code.py").write_text("modified\n")
        second = worktree_stamp(self.root)
        self.assertNotEqual(second, first)
        (self.root / "new.py").write_text("new untracked input\n")
        third = worktree_stamp(self.root)
        self.assertNotEqual(third, second)
        (self.root / "code.py").unlink()
        self.assertNotEqual(worktree_stamp(self.root), third)

    def test_index_and_head_changes_invalidate(self):
        first = worktree_stamp(self.root)
        self.git("update-index", "--chmod=+x", "code.py")
        self.assertNotEqual(worktree_stamp(self.root), first)

    def test_submodules_and_expired_deadlines_are_unavailable(self):
        head = (
            subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"])
            .decode()
            .strip()
        )
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},module")
        self.assertIsNone(worktree_stamp(self.root))
        with self.assertRaisesRegex(ValueError, "timeout"):
            evidence_reuse._git(self.root, "ls-files", deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            evidence_reuse._git(
                self.root, "not-a-subcommand", deadline=time.monotonic() + 2
            )

    def test_unavailable_for_non_root_symlink_excess_inventory_or_race(self):
        child = self.root / "child"
        child.mkdir()
        self.assertIsNone(worktree_stamp(child))
        (self.root / "link").symlink_to("code.py")
        self.assertIsNone(worktree_stamp(self.root))
        (self.root / "link").unlink()
        (self.root / "loop").symlink_to("loop")
        self.assertIsNone(worktree_stamp(self.root))
        (self.root / "loop").unlink()
        with mock.patch.object(evidence_reuse, "MAX_WORKTREE_FILES", 0):
            self.assertIsNone(worktree_stamp(self.root))
        with mock.patch.object(
            evidence_reuse, "_inventory_stamp", side_effect=["a", "b"]
        ):
            self.assertIsNone(worktree_stamp(self.root))
        with mock.patch.object(
            evidence_reuse.subprocess, "Popen", side_effect=FileNotFoundError
        ):
            self.assertIsNone(worktree_stamp(self.root))

    def test_ambient_git_overrides_cannot_redirect_inventory(self):
        first = worktree_stamp(self.root)
        with mock.patch.dict(
            os.environ, {"GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}
        ):
            self.assertEqual(worktree_stamp(self.root), first)

    def test_inventory_output_is_bounded_while_reading(self):
        with subprocess.Popen(
            [sys.executable, "-c", "print('x' * 10000)"], stdout=subprocess.PIPE
        ) as child:
            try:
                with mock.patch.object(evidence_reuse, "MAX_INVENTORY_BYTES", 100):
                    with self.assertRaisesRegex(ValueError, "oversized"):
                        evidence_reuse._bounded_output(child)
            finally:
                child.kill()
                child.wait()


class ReusePolicyTests(unittest.TestCase):
    def test_receipts_are_bounded_and_never_invent_missing_freshness(self):
        cache = EvidenceReuse(Path("/synthetic"), {"reviewer"})
        self.assertEqual(cache.snapshot(), {})
        with mock.patch.object(
            evidence_reuse, "worktree_stamp", return_value="one"
        ) as stamp:
            cache.remember("implementer")
            self.assertEqual(cache.snapshot(), {"implementer": "metadata_unchanged"})
            stamp.return_value = "two"
            self.assertEqual(cache.snapshot(), {"implementer": "worktree_changed"})
            stamp.return_value = None
            self.assertEqual(cache.snapshot(), {"implementer": "unavailable"})
            cache.invalidate_guidance()
            self.assertEqual(cache.snapshot(), {"implementer": "guidance_changed"})
            for _ in range(10):
                cache.remember("implementer")
            self.assertEqual(len(cache.receipts), 1)
            self.assertEqual(cache.snapshot(), {"implementer": "unavailable"})
            restored = EvidenceReuse(Path("/synthetic"), {"reviewer"})
            self.assertEqual(restored.snapshot(), {})
        with self.assertRaises(ValueError):
            cache.remember("unknown")
        with self.assertRaises(ValueError):
            EvidenceReuse(Path("/synthetic"), {"unknown"})

    def test_default_policy_never_observes_the_tree(self):
        cache = EvidenceReuse(Path("/synthetic"), set())
        with mock.patch.object(evidence_reuse, "worktree_stamp") as stamp:
            cache.remember("reviewer")
            cache.invalidate_guidance()
            self.assertEqual(cache.snapshot(), {})
            stamp.assert_not_called()

    def test_hints_preserve_evidence_and_do_not_certify_checks(self):
        events = [
            {
                "role": "reviewer",
                "round": 1,
                "report": {
                    "kind": "review",
                    "verdict": "changes_requested",
                    "summary": "Review",
                    "findings": [{"severity": "high", "message": "required repair"}],
                    "checks": [{"name": "tests", "status": "passed"}],
                },
            }
        ]
        original = render_run_state_capsule(events, 2)
        candidate = render_run_state_capsule(
            events, 2, evidence_reuse={"reviewer": "metadata_unchanged"}
        )
        self.assertTrue(candidate.startswith(original))
        self.assertIn("required repair", candidate)
        self.assertIn("Never reuse historical passes or approval", candidate)
        self.assertIn("not content identity or check freshness", candidate)
        unknown = render_run_state_capsule(
            events, 2, evidence_reuse={"reviewer": "PRIVATE_INVALID_STATUS"}
        )
        self.assertNotIn("PRIVATE_INVALID_STATUS", unknown)
        self.assertIn("source round 1: unavailable", unknown)
        self.assertLessEqual(len(candidate.encode()), 16384)


class ReuseBoundaryTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    def start_broker(self):
        initialize_broker_run(
            self.coord,
            self.manifest,
            "synthetic",
            {},
            worker_context_overrides={"reviewer": "retain"},
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {"implementer": object(), "reviewer": object()}
        broker.deliver = mock.AsyncMock()
        return broker

    async def test_deferred_delivery_rechecks_tree_at_assignment_boundary(self):
        broker = self.start_broker()
        with mock.patch.object(
            evidence_reuse, "worktree_stamp", return_value="one"
        ) as stamp:
            event = {
                "role": "implementer",
                "round": 1,
                "report": {"kind": "implementation", "summary": "done"},
            }
            broker._remember_report(event)
            await broker._deliver_run_state(("reviewer",), 1)
            broker.deliver.assert_not_called()
            stamp.return_value = "two"
            await broker._flush_pending_run_state("reviewer", 2)
            self.assertIn("worktree_changed", broker.deliver.call_args.args[3])
            stamp.return_value = "one"
            await broker._flush_pending_run_state("reviewer", 2)
            self.assertIn("metadata_unchanged", broker.deliver.call_args.args[3])
            self.assertNotIn(
                "Investigation reuse", broker._run_state_capsule(2, "implementer")
            )
            stamp.return_value = "three"
            self.assertIn(
                "worktree_changed", broker._materialize_pending_run_state("reviewer", 2)
            )
            with broker_store.connect_broker_database(
                self.coord, readonly=True
            ) as database:
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    database.execute(
                        "SELECT COUNT(*) FROM meta WHERE value LIKE '%metadata_unchanged%'"
                    ).fetchone()[0],
                    0,
                )

    async def test_operator_guidance_invalidates_receipts_before_next_delivery(self):
        broker = self.start_broker()
        with mock.patch.object(evidence_reuse, "worktree_stamp", return_value="one"):
            broker._remember_report(
                {
                    "role": "reviewer",
                    "round": 1,
                    "report": {"kind": "review", "summary": "prior investigation"},
                }
            )
            with broker_store.connect_broker_database(self.coord) as database:
                await broker._handle_operator_send(
                    database, "implementer", "new requirement", "a" * 32
                )
            await broker._flush_pending_run_state("reviewer", 2)
            self.assertIn("guidance_changed", broker.deliver.call_args.args[3])
