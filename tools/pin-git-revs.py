#!/usr/bin/env python3
"""Pin branch-tracking git dependencies to the commit Cargo.lock already names.

Cargo cannot use a vendored replacement for a git dependency that tracks a
branch. Given

    gpui = { git = "https://github.com/zed-industries/zed" }

cargo has to ask the remote what the default branch points at before it can
decide what the vendored directory is meant to be, and offline it gives up with

    the source https://github.com/zed-industries/zed requires a lock file to be
    present first before it can be used against vendored source code

The commit is not actually unknown: Cargo.lock records it in the source
fragment, `git+https://github.com/zed-industries/zed#1a246efd…`. This script
promotes that fragment into an explicit `?rev=` in the lock and a matching
`rev = "…"` wherever the dependency is declared, which is the form cargo will
vendor.

Nothing is chosen here. Every revision written comes from the lock file, so the
build resolves to exactly the commits it would have without this step.

The vendored crates matter as much as the workspace. gpui-component and
gpui-updater both depend on gpui the same unpinned way, and cargo reads their
manifests too, so patching only the workspace root fixes nothing. Their
`.cargo-checksum.json` carries `"files": {}`, so editing a vendored manifest is
not a checksum violation.

Run it after the sources are in place and before cargo, both when generating
cargo-sources.json and inside the build, so the two agree:

    python3 tools/pin-git-revs.py [TREE]        # default: cwd

Stdlib only: it runs inside the Flatpak SDK, which has python3 and nothing else.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# git+<url>#<commit>, with no ?rev= / ?tag= / ?branch= query. A source that
# already carries one of those is what cargo wants and is left alone.
BARE_GIT_SOURCE = re.compile(
    r'^source = "git\+(?P<url>[^"#?]+)#(?P<commit>[0-9a-f]{40})"$', re.MULTILINE
)

PINNED = re.compile(r"^\s*(rev|tag|branch)\s*=", re.MULTILINE)

# Directories that hold sources rather than anything cargo compiles from.
# flatpak-cargo/ is the download staging area; its contents were already copied
# into cargo/vendor/ by the time this runs, so patching it would edit a copy
# nothing reads and make the report misleading.
SKIP_DIRS = {"target", "flatpak-cargo", ".flatpak-builder"}


def find_bare_sources(lock_text: str) -> dict[str, str]:
    """Map repository URL to the commit the lock pins, for unpinned sources."""
    found: dict[str, str] = {}
    for m in BARE_GIT_SOURCE.finditer(lock_text):
        url, commit = m.group("url"), m.group("commit")
        previous = found.setdefault(url, commit)
        if previous != commit:
            # One URL resolved to two commits cannot be expressed as a single
            # rev, and picking one would build something the lock does not
            # describe.
            raise SystemExit(
                f"{url} is pinned to both {previous} and {commit}; "
                "cannot express that as one rev"
            )
    return found


def pin_inline(text: str, url: str, commit: str) -> tuple[str, int]:
    """`gpui = { git = "URL" }` — a whole dependency on one line."""
    pattern = re.compile(
        r"\{(?P<body>[^{}\n]*\bgit\s*=\s*\"" + re.escape(url) + r"\"[^{}\n]*)\}"
    )
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        body = m.group("body")
        if PINNED.search(body) or re.search(r"\b(rev|tag|branch)\s*=", body):
            return m.group(0)
        count += 1
        return "{" + body.rstrip().rstrip(",") + f', rev = "{commit}"' + " }"

    return pattern.sub(sub, text), count


def pin_sections(text: str, url: str, commit: str) -> tuple[str, int]:
    """`[dependencies.gpui]` followed by `git = "URL"` on its own line.

    This is the shape `cargo vendor` writes, and the one the workspace root does
    not use, which is why patching only the root left the build failing in the
    same place.
    """
    lines = text.split("\n")
    # Block boundaries are table headers; everything between two headers belongs
    # to the first of them.
    starts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")]
    bounds = list(zip(starts, starts[1:] + [len(lines)]))

    git_line = re.compile(r"^(?P<indent>\s*)git\s*=\s*\"" + re.escape(url) + r"\"\s*$")
    inserts: list[int] = []
    for start, end in bounds:
        block = lines[start:end]
        hit = next((i for i, ln in enumerate(block) if git_line.match(ln)), None)
        if hit is None:
            continue
        if any(PINNED.match(ln) for ln in block):
            continue
        inserts.append(start + hit)

    for offset, at in enumerate(inserts):
        indent = git_line.match(lines[at + offset]).group("indent")
        lines.insert(at + offset + 1, f'{indent}rev = "{commit}"')

    return "\n".join(lines), len(inserts)


def unpinned_occurrences(text: str, url: str) -> list[str]:
    """Every declaration of `url` that cargo would still read as a branch."""
    problems: list[str] = []

    for m in re.finditer(
        r"\{[^{}\n]*\bgit\s*=\s*\"" + re.escape(url) + r"\"[^{}\n]*\}", text
    ):
        if not re.search(r"\b(rev|tag|branch)\s*=", m.group(0)):
            problems.append(m.group(0))

    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")]
    git_line = re.compile(r"^\s*git\s*=\s*\"" + re.escape(url) + r"\"\s*$")
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        block = lines[start:end]
        if any(git_line.match(ln) for ln in block) and not any(
            PINNED.match(ln) for ln in block
        ):
            problems.append(lines[start].strip())

    return problems


def main(root: Path) -> int:
    lock_path = root / "Cargo.lock"
    if not lock_path.is_file():
        raise SystemExit(f"no Cargo.lock in {root}")

    lock_text = lock_path.read_text(encoding="utf-8")
    bare = find_bare_sources(lock_text)
    if not bare:
        print("no branch-tracking git dependencies; nothing to pin")
        return 0

    for url, commit in sorted(bare.items()):
        print(f"pinning {url} -> {commit}")

    manifests = [
        p
        for p in root.rglob("Cargo.toml")
        if not SKIP_DIRS.intersection(p.relative_to(root).parts)
    ]

    total = 0
    for manifest in sorted(manifests):
        text = original = manifest.read_text(encoding="utf-8")
        changed = 0
        for url, commit in bare.items():
            text, n = pin_inline(text, url, commit)
            changed += n
            text, n = pin_sections(text, url, commit)
            changed += n
        if text != original:
            manifest.write_text(text, encoding="utf-8")
            print(f"  {manifest.relative_to(root)} ({changed})")
            total += changed

    if total == 0:
        raise SystemExit(
            "Cargo.lock names branch-tracking sources but nothing declared "
            "them; refusing to continue with a lock and manifests that would "
            "disagree"
        )

    lock_path.write_text(pin_lock(lock_text), encoding="utf-8")

    # The build runs with --locked, so anything cargo can still read as
    # branch-tracking means the lock and the manifests disagree, and it fails
    # later with a message about vendoring rather than about this. Fail here.
    failures: list[str] = []
    for manifest in sorted(manifests):
        text = manifest.read_text(encoding="utf-8")
        for url in bare:
            for problem in unpinned_occurrences(text, url):
                failures.append(f"{manifest.relative_to(root)}: {problem}")
    if failures:
        raise SystemExit(
            "still unpinned after rewriting:\n  " + "\n  ".join(failures)
        )

    print(f"pinned {total} declaration(s) across {len(manifests)} manifest(s)")
    return 0


def pin_lock(lock_text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        url, commit = m.group("url"), m.group("commit")
        return f'source = "git+{url}?rev={commit}#{commit}"'

    return BARE_GIT_SOURCE.sub(sub, lock_text)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
