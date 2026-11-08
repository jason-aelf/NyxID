import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from retry_incremental import should_retry


class RetryIncrementalTests(unittest.TestCase):
    def test_internal_compiler_error_retries_case_insensitively(self):
        self.assertTrue(should_retry("error: Internal Compiler Error: stale query"))

    def test_incremental_markers_retry(self):
        for output in (
            "error: incremental compilation failure",
            "failed to load dep graph: metadata is corrupt",
            "could not read dep-graph file",
            "error: failed to decode dep-graph file: invalid metadata",
            "error: could not read incremental compilation directory `/tmp/cache`",
            "error: incremental cache is corrupt",
            "error: unreadable dep-graph file",
            "error: corrupted incremental compilation cache",
        ):
            with self.subTest(output=output):
                self.assertTrue(should_retry(output))

    def test_ordinary_compile_error_does_not_retry(self):
        self.assertFalse(should_retry("error[E0308]: mismatched types"))

    def test_test_failure_does_not_retry(self):
        self.assertFalse(should_retry("test foo ... FAILED\nthread panicked at assertion"))

    def test_keywords_in_ordinary_output_do_not_retry(self):
        for output in (
            "error[E0425]: cannot find value `dep-graph` in this scope",
            "test incremental compilation ... FAILED",
            "test internal compiler error ... FAILED",
            "test failed to load dep graph ... FAILED",
            "error: expected identifier, found `dep-graph`",
            "error: invalid value for incremental compilation",
            "warning: incremental compilation is disabled",
            '12 | panic!("internal compiler error: stale query");',
            "  | // failed to load dep graph: metadata is corrupt",
            "  = help: incremental compilation failure is a test fixture",
            'thread \'dep-graph\' panicked at "could not read dep-graph file"',
            "internal compiler errors are tested here",
            "dep-graph",
            "incremental compilation",
            "failed to load dependency unrelated to the dep-graph",
        ):
            with self.subTest(output=output):
                self.assertFalse(should_retry(output))

    def test_colored_diagnostics_in_combined_output_retry(self):
        self.assertTrue(should_retry(
            '[ci-resources] {"event": "start"}\n'
            '\x1b[1m\x1b[31merror\x1b[0m: Internal Compiler Error: stale query\n'
            'error: could not compile `example`\n'
        ))
        self.assertFalse(should_retry(
            '\x1b[31merror[E0425]\x1b[0m: cannot find value `dep-graph` in this scope'
        ))


WRAPPER = Path(__file__).with_name("retry_incremental.py").resolve()
CACHE_FILES = (
    "target/debug/incremental/crate/session/cache.bin",
    "target/llvm-cov-target/debug/incremental/crate/session/cache.bin",
)
OTHER_FILES = (
    "target/debug/deps/keep.rlib",
    "target/llvm-cov-target/debug/deps/keep.rlib",
    "target/release/incremental/keep.bin",
    "target/other/incremental/keep.bin",
    "outside-target/keep.bin",
)
CHILD = """
import json
from pathlib import Path
import sys

outputs, codes, paths = json.loads(sys.argv[1])
history = Path('attempts.jsonl')
attempt = len(history.read_text().splitlines()) if history.exists() else 0
with history.open('a') as stream:
    stream.write(json.dumps([Path(path).exists() for path in paths]) + '\\n')
print('attempt-' + str(attempt + 1) + '-stdout', flush=True)
print(outputs[min(attempt, 1)], file=sys.stderr, flush=True)
sys.exit(codes[min(attempt, 1)])
"""


class RetryIncrementalSubprocessTests(unittest.TestCase):
    def run_wrapper(self, root, outputs, codes):
        for name in CACHE_FILES + OTHER_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("sentinel", encoding="utf-8")
        log = root / "logs/compiler.log"
        log.parent.mkdir()
        log.write_text("obsolete log contents", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(WRAPPER), "--log-file", str(log), "--",
             sys.executable, "-c", CHILD,
             json.dumps([outputs, codes, CACHE_FILES + OTHER_FILES])],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        history = [json.loads(line) for line in
                   (root / "attempts.jsonl").read_text().splitlines()]
        return result, log.read_text(encoding="utf-8"), history

    def assert_files_unchanged(self, root, names):
        for name in names:
            self.assertEqual((root / name).read_text(encoding="utf-8"), "sentinel")

    def test_ordinary_failures_run_once_preserving_status_output_and_caches(self):
        for diagnostic in (
            "error[E0425]: cannot find value `dep-graph` in this scope",
            "test incremental compilation ... FAILED",
            '7 | panic!("internal compiler error: stale query");',
            "error[E0308]: mismatched types",
        ):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result, log, history = self.run_wrapper(
                    root, [diagnostic, "unexpected retry"], [17, 0],
                )
                self.assertEqual(result.returncode, 17)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "attempt-1-stdout\n" + diagnostic + "\n")
                self.assertEqual(log, result.stdout)
                self.assertEqual(history, [[True] * 7])
                self.assert_files_unchanged(root, CACHE_FILES + OTHER_FILES)

    def test_recognized_failure_retries_once_with_narrow_cleanup_and_both_logs(self):
        for retry_code in (0, 23):
            with self.subTest(retry_code=retry_code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                diagnostics = [
                    "error: Internal Compiler Error: first attempt stale query",
                    "error: could not read dep-graph file: second attempt corrupt",
                ]
                result, log, history = self.run_wrapper(root, diagnostics, [101, retry_code])
                self.assertEqual(result.returncode, retry_code)
                self.assertEqual(result.stderr, "")
                self.assertEqual(history, [[True] * 7, [False] * 2 + [True] * 5])
                for output in (result.stdout, log):
                    for text in ("attempt-1-stdout", "attempt-2-stdout", *diagnostics):
                        self.assertEqual(output.count(text), 1)
                    self.assertLess(output.index(diagnostics[0]), output.index(diagnostics[1]))
                    self.assertNotIn("obsolete log contents", output)
                self.assertEqual(log.count("[retry-incremental]"), 1)
                for directory in ("target/debug/incremental",
                                  "target/llvm-cov-target/debug/incremental"):
                    self.assertFalse((root / directory).exists())
                self.assert_files_unchanged(root, OTHER_FILES)

    def test_successful_first_command_never_retries_even_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diagnostic = "error: internal compiler error: printed fixture"
            result, log, history = self.run_wrapper(root, [diagnostic, "unexpected"], [0, 23])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(history, [[True] * 7])
            self.assertEqual(result.stdout, "attempt-1-stdout\n" + diagnostic + "\n")
            self.assertEqual(log, result.stdout)
            self.assert_files_unchanged(root, CACHE_FILES + OTHER_FILES)


if __name__ == "__main__":
    unittest.main()
