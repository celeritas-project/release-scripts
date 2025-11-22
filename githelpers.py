#!/usr/bin/env python
# Copyright 2023 UT-Battelle, LLC, and other Celeritas developers.
# See the top-level COPYRIGHT file for details.
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""
Helper functions for release note generation and other GitHub processing.
"""

import re
import subprocess

from pathlib import Path
from contextlib import contextmanager
from gzip import GzipFile
from io import BytesIO
from typing import IO


MISSING_PR_ID = {
    "Fix linking to CUDA toolkit when using VecGeom": 989,
}
GIT = "git"
REPO = Path.home() / "Code/celeritas-temp"
RE_SUBJECT_PR_SQUASH = re.compile(r"\(#([\d]+)\)$")
RE_SUBJECT_PR_MERGE = re.compile(r"^Merge pull request #([\d]+)")


def gitrun(*args):
    return subprocess.run(
        (GIT,) + args,
        capture_output=True,
        check=True,
        env={"GIT_DIR": str(REPO / ".git")},
    )


def git(*args, split=b"\n") -> list[str]:
    result = gitrun(*args)
    return [s.decode() for s in result.stdout.split(split)]


def gitz(subcommand, *args) -> list:
    return git(subcommand, "-z", *args, split=b"\0")


def git_log_subjects(start, stop, first_parent=True):
    span = stop
    if start:
        span = start + ".." + stop
    args = []
    if first_parent:
        args.append("--first-parent")
    # TODO: return (hash, subject) pairs
    # args += [r"--format=%H\\t%s", span]
    args += [r"--format=%s", span]
    return git("log", *args)


def git_merge_base(a, b) -> str:
    return git("merge-base", a, b)[0]


def git_rev_parse(commitish: str) -> str:
    repr(commitish)
    result = git("rev-parse", commitish)[0]
    assert len(result) == 40
    return result


def subject_to_pr(subj):
    for expr in [RE_SUBJECT_PR_SQUASH, RE_SUBJECT_PR_MERGE]:
        if match := expr.search(subj):
            return int(match.group(1))
    if subj:
        try:
            return MISSING_PR_ID[subj]
        except KeyError:
            print("Can't match log subject to PR:", subj)


def git_renames(old_ref: str, new_ref: str):
    names = iter(gitz("diff", "--name-status", old_ref, new_ref))
    try:
        while True:
            delta = next(names)
            if delta.startswith("R"):
                yield (next(names), next(names))
            else:
                skipped = next(names)
                print(f"WARNING: not a rename: {delta}, {skipped}")
    except StopIteration:
        pass


def git_lstree(ref: str) -> list[str]:
    return gitz("ls-tree", "-r", "--name-only", ref)


def git_archive_tgz(ref: str, compresslevel=9) -> bytes:
    """Export a commit's contents to a tgz file.
    """
    tarbytes = gitrun("archive", "--format=tar", ref).stdout
    buf = BytesIO()
    with GzipFile(fileobj=buf, mode="wb", compresslevel=compresslevel) as gzbuf:
        gzbuf.write(tarbytes)
    return buf.getvalue()


@contextmanager
def open_pbcopy():
    """
    Context manager that opens the macOS pbcopy command for writing to the clipboard.

    Yields:
        TextIO: A string IO-like object connected to pbcopy's stdin that accepts
                UTF-8 encoded text to be copied to the clipboard.

    Example:
        with open_pbcopy() as writer:
            writer.write("Hello, clipboard!")
    """
    """Write to the clipboard as though to a text file."""
    process = subprocess.Popen(
        ["pbcopy"],
        stdin=subprocess.PIPE,
        env={"LANG": "en_US.UTF-8"},
        encoding="utf-8",
    )
    assert process.stdin is not None
    
    try:
        writer: IO[str] = process.stdin
        yield writer
    finally:
        writer.close()
        process.wait()

