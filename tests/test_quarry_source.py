#!/usr/bin/env python3
"""resolve_ref against throwaway local repos: a pinned SHA that is already
present needs no network, fetch failures are retried a bounded number of
times, and branch refs still fetch and resolve."""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infrastructure", "build"))
import quarry_source  # noqa: E402


class Log:
    def __init__(self):
        self.lines = []

    def log(self, stage, msg):
        self.lines.append("%s %s" % (stage, msg))


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def make_remote_and_checkout(tmp):
    remote = os.path.join(tmp, "remote")
    os.makedirs(remote)
    git(remote, "init", "-q", "-b", "main")
    git(remote, "commit", "-q", "--allow-empty", "-m", "one")
    checkout = os.path.join(tmp, "ci-checkout")
    subprocess.run(["git", "clone", "-q", remote, checkout], check=True)
    git(checkout, "checkout", "-q", "--detach")
    return remote, checkout


def test_present_sha_skips_fetch():
    with tempfile.TemporaryDirectory() as tmp:
        remote, checkout = make_remote_and_checkout(tmp)
        sha = git(remote, "rev-parse", "HEAD")
        git(checkout, "remote", "set-url", "origin", os.path.join(tmp, "unreachable"))
        log = Log()
        assert quarry_source.resolve_ref(checkout, sha, log) == sha
        assert not any("fetching origin" in ln for ln in log.lines), log.lines


def test_fetch_failure_is_retried_then_reported():
    with tempfile.TemporaryDirectory() as tmp:
        _, checkout = make_remote_and_checkout(tmp)
        git(checkout, "remote", "set-url", "origin", os.path.join(tmp, "unreachable"))
        log = Log()
        quarry_source.FETCH_RETRY_DELAY_SECONDS = 0
        try:
            quarry_source.resolve_ref(checkout, "f" * 40, log)
        except quarry_source.SourceError as e:
            assert "after 3 attempts" in str(e), e
        else:
            raise AssertionError("expected SourceError")
        assert sum("fetching origin" in ln for ln in log.lines) == 3, log.lines


def test_branch_ref_fetches_new_commit():
    with tempfile.TemporaryDirectory() as tmp:
        remote, checkout = make_remote_and_checkout(tmp)
        git(remote, "commit", "-q", "--allow-empty", "-m", "two")
        new = git(remote, "rev-parse", "HEAD")
        log = Log()
        assert quarry_source.resolve_ref(checkout, "main", log) == new
        assert git(checkout, "rev-parse", "HEAD") == new


if __name__ == "__main__":
    test_present_sha_skips_fetch()
    test_fetch_failure_is_retried_then_reported()
    test_branch_ref_fetches_new_commit()
    print("✓ All quarry-source tests passed")
