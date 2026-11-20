import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts import release_npm

NAME = "test-release-package"
VERSION = "0.0.1"
COMMIT = "a" * 40
INTEGRITY = "sha512-" + "A" * 86 + "=="
SENTINEL = "synthetic raw npm diagnostic"
MANIFEST = {"name": NAME, "version": VERSION}
EXACT = {**MANIFEST, "gitHead": COMMIT, "dist.integrity": INTEGRITY}
ABSENT = {"error": {"code": "E404", "detail": SENTINEL}}


def response(code, data):
    stdout = data if isinstance(data, bytes) else json.dumps(data).encode()
    return subprocess.CompletedProcess([], code, stdout, SENTINEL.encode())


PACKED = response(0, [{**MANIFEST, "integrity": INTEGRITY}])


class ReleaseNpmTests(unittest.TestCase):
    def run_release(self, *responses, pack=PACKED, lookup_time=0):
        self.now = 0.0
        stdout, stderr = io.StringIO(), io.StringIO()
        results = iter((pack, *responses))

        def advance(seconds):
            self.now += seconds

        def fake_npm(command, **kwargs):
            self.assertEqual(command[0], "npm")
            self.assertIn("--logs-max=0", command)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            if command[1] == "view":
                advance(lookup_time)
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(release_npm.subprocess, "run", side_effect=fake_npm) as npm,
            patch.object(release_npm.time, "monotonic", lambda: self.now),
            patch.object(release_npm.time, "sleep", advance),
            patch.dict(release_npm.os.environ, {"GITHUB_SHA": COMMIT}),
            patch.object(
                release_npm.Path,
                "read_bytes",
                return_value=json.dumps(MANIFEST).encode(),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = release_npm.main()
        self.commands = [call.args[0][1:] for call in npm.call_args_list]
        self.actions = [command[0] for command in self.commands]
        self.timeouts = [call.kwargs["timeout"] for call in npm.call_args_list]
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        self.assertNotIn(SENTINEL, self.stdout + self.stderr)
        return status

    def test_exact_existing_release_does_not_publish_or_retry(self):
        self.assertEqual(self.run_release(response(0, EXACT)), 0)
        self.assertEqual(self.actions, ["pack", "view"])
        self.assertEqual(self.now, 0)
        self.assertIn("skipping publication", self.stdout)
        required = {"--dry-run", "--offline", "--ignore-scripts", "--json"}
        self.assertTrue(required.issubset(self.commands[0]))

    def test_successful_publish_can_propagate_beyond_the_old_window(self):
        not_found = response(1, ABSENT)
        self.assertEqual(
            self.run_release(
                not_found, response(0, b""), *([not_found] * 6), response(0, EXACT)
            ),
            0,
        )
        self.assertEqual(self.now, 60)
        self.assertEqual(self.actions, ["pack", "view", "publish", *(["view"] * 7)])
        publish = self.commands[2]
        registry = "--registry=https://registry.npmjs.org/"
        self.assertTrue(all(arg.startswith("--") for arg in publish[1:]))
        required = {
            registry,
            "--access=public",
            "--provenance",
            "--ignore-scripts",
            "--fetch-retries=0",
        }
        self.assertTrue(required.issubset(publish))
        required = {registry, "--prefer-online", "--fetch-retries=0", "--json", *EXACT}
        for lookup in (self.commands[1], *self.commands[3:]):
            self.assertEqual(lookup[1], f"{NAME}@{VERSION}")
            self.assertTrue(required.issubset(lookup))
        self.assertEqual(self.timeouts[1], 15)
        self.assertIn("exact registry metadata verified", self.stdout)

    def test_propagation_deadline_caps_lookup_time_and_never_republishes(self):
        not_found = response(1, ABSENT)
        self.assertEqual(
            self.run_release(
                not_found, response(0, b""), *([not_found] * 10), lookup_time=2
            ),
            1,
        )
        self.assertEqual(self.now, 122)  # 2 seconds initially, then 120 after publish.
        self.assertEqual(self.actions, ["pack", "view", "publish", *(["view"] * 10)])
        self.assertEqual(self.timeouts[-1], 12)
        self.assertIn("within 120 seconds", self.stderr)

    def test_failed_or_timed_out_publish_never_checks_again_or_retries(self):
        for failed in (
            response(1, {"error": {"code": "E500", "detail": SENTINEL}}),
            subprocess.TimeoutExpired(["npm", "publish"], 120, output=SENTINEL),
        ):
            with self.subTest(result=type(failed).__name__):
                self.assertEqual(self.run_release(response(1, ABSENT), failed), 1)
                self.assertEqual(self.actions, ["pack", "view", "publish"])
                self.assertEqual(self.now, 0)
                self.assertIn("before rerunning", self.stderr)

    def test_bad_registry_responses_stop_immediately_before_or_after_publish(self):
        failures = {
            "unstructured not-found text": response(1, b"E404 Not Found"),
            "non-E404 with E404 in detail": response(
                1, {"error": {"code": "E401", "detail": f"E404 {SENTINEL}"}}
            ),
            "error on successful exit": response(0, ABSENT),
            "signalled exit": response(-15, ABSENT),
            "multiple versions": response(0, [EXACT]),
            "missing integrity": response(
                0, {k: v for k, v in EXACT.items() if k != "dist.integrity"}
            ),
            "timeout": subprocess.TimeoutExpired(["npm", "view"], 15, output=SENTINEL),
            **{
                f"mismatched {key}": response(0, {**EXACT, key: SENTINEL})
                for key in EXACT
            },
        }
        for label, failed in failures.items():
            for after_publish in (False, True):
                with self.subTest(failure=label, after_publish=after_publish):
                    prefix = (
                        (response(1, ABSENT), response(0, b"")) if after_publish else ()
                    )
                    self.assertEqual(self.run_release(*prefix, failed), 1)
                    actions = ["pack", "view"]
                    if after_publish:
                        actions += ["publish", "view"]
                    self.assertEqual(self.actions, actions)
                    self.assertEqual(self.now, 0)
                    self.assertTrue(self.stderr)

    def test_bad_offline_pack_prevents_registry_access(self):
        for pack in (
            response(1, SENTINEL),
            response(0, b"malformed"),
            response(0, []),
            response(0, [{**MANIFEST, "version": "0.0.2", "integrity": INTEGRITY}]),
            response(0, [{**MANIFEST, "integrity": "sha512-not-a-digest"}]),
        ):
            with self.subTest(result=pack.stdout):
                self.assertEqual(self.run_release(pack=pack), 1)
                self.assertEqual(self.actions, ["pack"])
                self.assertIn("offline npm pack", self.stderr)
