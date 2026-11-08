"""Quarry source selection and git provenance for HIL builds.

Two ways to choose the Quarry sources a run builds from:

  * --quarry-ref REF (the default, REF=main): resolve REF to an exact commit
    in the dedicated CI-only clone (DEFAULT_CI_CHECKOUT) and check it out
    detached. That clone exists only for this tooling. resolve_ref() refuses
    to touch anything that looks like an interactive checkout: a clone
    attached to a branch, or one with local modifications.
  * --quarry-dir PATH: build from an existing tree exactly as it is. No git
    command that changes state is ever run against it.

Either way, capture_provenance() records the commit, branch, dirty state and
porcelain status. A dirty tree is refused unless the caller explicitly
allows it, and is then marked as dirty in the result.
"""

import hashlib
import os
import re
import subprocess

DEFAULT_CI_CHECKOUT = os.environ.get(
    "QUARRY_HIL_CI_CHECKOUT",
    os.path.expanduser("~/.cache/visioncb-hil/quarry-ci-checkout"),
)
DEFAULT_QUARRY_REMOTE = os.environ.get("QUARRY_HIL_QUARRY_REMOTE", "https://github.com/ip332/quarry.git")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class SourceError(Exception):
    """Raised for any problem selecting or describing a source tree."""


def _git(repo, *args, timeout=120):
    result = subprocess.run(
        ["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git_ok(repo, *args, timeout=120):
    rc, out, err = _git(repo, *args, timeout=timeout)
    if rc != 0:
        raise SourceError("git %s failed in %s: %s" % (" ".join(args), repo, err or out))
    return out


def resolve_ref(checkout, ref, log, remote=DEFAULT_QUARRY_REMOTE):
    """Fetch and check out `ref` (branch, tag or full SHA) detached in the
    dedicated CI clone. Returns the resolved 40-hex commit."""
    fresh = False
    if not os.path.isdir(os.path.join(checkout, ".git")):
        if os.path.exists(checkout) and os.listdir(checkout):
            raise SourceError("%s exists but is not a git clone; refusing to use it" % checkout)
        log.log("SOURCE", "cloning %s into dedicated CI checkout %s" % (remote, checkout))
        os.makedirs(os.path.dirname(checkout), exist_ok=True)
        result = subprocess.run(["git", "clone", "--quiet", remote, checkout], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise SourceError("git clone failed: %s" % result.stderr.strip())
        fresh = True

    rc, branch, _ = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc == 0 and branch and not fresh:
        raise SourceError(
            "%s is attached to branch %r. The dedicated CI checkout is always detached, so this looks "
            "like an interactive checkout and will not be switched. Pass --quarry-dir to build it as it "
            "is, or point --quarry-ci-checkout at the dedicated clone." % (checkout, branch)
        )
    status = _git_ok(checkout, "status", "--porcelain")
    if status:
        raise SourceError(
            "dedicated CI checkout %s has local modifications; refusing to switch it:\n%s" % (checkout, status)
        )

    log.log("SOURCE", "fetching origin in %s" % checkout)
    _git_ok(checkout, "fetch", "origin", "--tags", "--prune", "--force", timeout=600)

    candidates = []
    if _SHA_RE.match(ref):
        candidates.append(ref)
    candidates += ["refs/remotes/origin/%s" % ref, "refs/tags/%s" % ref, ref]
    sha = None
    for candidate in candidates:
        rc, out, _ = _git(checkout, "rev-parse", "--verify", "--quiet", candidate + "^{commit}")
        if rc == 0 and _SHA_RE.match(out):
            sha = out
            break
    if sha is None:
        raise SourceError("could not resolve Quarry ref %r in %s" % (ref, checkout))

    _git_ok(checkout, "checkout", "--quiet", "--detach", sha)
    head = _git_ok(checkout, "rev-parse", "HEAD")
    if head != sha:
        raise SourceError("checkout of %s left HEAD at %s" % (sha, head))
    log.log("SOURCE", "Quarry ref %r resolved to %s (checked out detached in %s)" % (ref, sha, checkout))
    return sha


def capture_provenance(repo, label, log, count_untracked_as_dirty=True):
    """Describe a git tree without modifying it.

    `dirty` is True for tracked modifications, and also for untracked
    (non-ignored) files when `count_untracked_as_dirty` is set. quarry-hil
    itself turns that off: its builds copy an explicit source list, so a
    stray untracked file cannot reach a binary. The untracked files are
    still listed."""
    if not os.path.isdir(repo):
        raise SourceError("%s source tree %s does not exist" % (label, repo))
    commit = _git_ok(repo, "rev-parse", "HEAD")
    tree = _git_ok(repo, "rev-parse", "HEAD^{tree}")
    rc, branch, _ = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    tracked = _git_ok(repo, "status", "--porcelain", "--untracked-files=no")
    untracked_out = _git_ok(repo, "ls-files", "--others", "--exclude-standard")
    untracked = [ln for ln in untracked_out.splitlines() if ln]
    rc_d, describe, _ = _git(repo, "describe", "--tags", "--always", "--dirty")
    rc_u, remote_url, _ = _git(repo, "remote", "get-url", "origin")
    dirty = bool(tracked) or (count_untracked_as_dirty and bool(untracked))
    info = {
        "path": os.path.realpath(repo),
        "commit": commit,
        "tree": tree,
        "branch": branch if rc == 0 and branch else "(detached)",
        "describe": describe if rc_d == 0 else None,
        "remote": remote_url if rc_u == 0 else None,
        "dirty": dirty,
        "status_short": tracked,
        "untracked": untracked,
    }
    if dirty:
        diff = _git_ok(repo, "diff", "HEAD")
        info["dirty_digest"] = hashlib.sha256(
            (diff + "\n".join(untracked if count_untracked_as_dirty else [])).encode()
        ).hexdigest()
    log.log(
        "SOURCE",
        "%s commit=%s branch=%s dirty=%s%s"
        % (label, commit, info["branch"], dirty, (" untracked=%d" % len(untracked)) if untracked else ""),
    )
    return info


def require_clean(info, label, allow_dirty, log):
    if not info["dirty"]:
        return
    detail = info["status_short"] or "\n".join(info["untracked"])
    if not allow_dirty:
        raise SourceError(
            "%s tree %s is dirty; refusing to build a benchmark whose sources do not match commit %s. "
            "Commit/stash the changes or pass --allow-dirty (the result is then marked dirty).\n%s"
            % (label, info["path"], info["commit"], detail)
        )
    log.log("SOURCE", "WARNING: %s tree is dirty and --allow-dirty was given; result will be marked dirty" % label)
