#!/usr/bin/env python3
# Copyright (c) 2026 Kenneth J Baker <bakerkj@umich.edu>
#
# Render the UPSTREAM source diff for a base-image bump.
#
# Renovate cannot do this itself: a docker digest bump has no version range, and
# Renovate never maps a digest back to the commit it was built from -- so its PR
# shows an opaque `f986374 -> 54531eb` and nothing else. But the sdr-enthusiasts
# images carry OCI provenance labels (org.opencontainers.image.revision +
# .source), so we can resolve BOTH digests in the diff to their upstream commits
# and emit a real `compare/<old>...<new>` link.
#
# Images without a revision label (e.g. ghcr.io/plane-watch/docker-plane-watch)
# degrade to a plain "browse commits" repo link rather than dropping out.
#
# Usage:  renovate_image_diff.py <base-ref> <head-ref> [dockerfile]
# Writes markdown to stdout (empty if no image digests changed).

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ghcr.io/<org>/<repo>:<tag>@sha256:<64 hex>
IMAGE_RE = re.compile(
    r"(?P<image>[a-z0-9.-]+\.[a-z]{2,}/[a-z0-9._/-]+)"
    r":(?P<tag>[\w][\w.-]*)"
    r"@(?P<digest>sha256:[0-9a-f]{64})"
)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def refs_in(diff_lines, sign):
    """Map image -> (tag, digest) for added ('+') or removed ('-') diff lines."""
    out = {}
    for line in diff_lines:
        if not line.startswith(sign) or line.startswith(sign * 3):
            continue
        m = IMAGE_RE.search(line)
        if m:
            out[m.group("image")] = (m.group("tag"), m.group("digest"))
    return out


def labels_for(image, digest):
    """OCI labels for a digest. Handles both single-image and multi-arch output."""
    raw = sh(
        "docker", "buildx", "imagetools", "inspect",
        f"{image}@{digest}", "--format", "{{json .Image}}",
    )
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, dict) and "config" in data:
        return (data.get("config") or {}).get("Labels") or {}
    # multi-arch: {"linux/amd64": {...}, ...} -- any platform's labels will do
    if isinstance(data, dict):
        for entry in data.values():
            lab = (entry or {}).get("config", {}).get("Labels") or {}
            if lab:
                return lab
    return {}


def gh_compare(repo_path, old, new, token):
    """GitHub compare API -> (commit_count, [subjects]). ([], []) on failure."""
    url = f"https://api.github.com/repos/{repo_path}/compare/{old}...{new}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None, []
    subjects = [
        c["commit"]["message"].splitlines()[0]
        for c in data.get("commits", [])
    ]
    return data.get("total_commits"), subjects


def main():
    base, head = sys.argv[1], sys.argv[2]
    dockerfile = sys.argv[3] if len(sys.argv) > 3 else "aviation_feeder/Dockerfile"
    token = __import__("os").environ.get("GITHUB_TOKEN", "")

    diff = sh("git", "diff", f"{base}...{head}", "--", dockerfile).splitlines()
    old, new = refs_in(diff, "-"), refs_in(diff, "+")

    rows, details = [], []
    for image in sorted(set(old) & set(new)):
        (old_tag, old_digest), (new_tag, new_digest) = old[image], new[image]
        if old_digest == new_digest:
            continue  # retag only -- same image, nothing upstream changed

        name = image.rsplit("/", 1)[-1]
        change = f"`{old_tag}` → `{new_tag}`" if old_tag != new_tag else f"`{new_tag}`"

        old_lab, new_lab = labels_for(image, old_digest), labels_for(image, new_digest)
        source = new_lab.get("org.opencontainers.image.source") or ""
        old_rev = old_lab.get("org.opencontainers.image.revision")
        new_rev = new_lab.get("org.opencontainers.image.revision")

        if source.startswith("https://github.com/") and old_rev and new_rev:
            repo_path = source[len("https://github.com/"):].rstrip("/")
            count, subjects = gh_compare(repo_path, old_rev, new_rev, token)
            link = f"{source}/compare/{old_rev}...{new_rev}"
            rows.append(
                f"| `{name}` | {change} | {count if count is not None else '?'} "
                f"| [{old_rev[:7]}…{new_rev[:7]}]({link}) |"
            )
            if subjects:
                body = "\n".join(f"- {s}" for s in subjects[:20])
                more = "\n- …" if len(subjects) > 20 else ""
                details.append(
                    f"<details><summary><code>{name}</code> — "
                    f"{count} commit(s)</summary>\n\n{body}{more}\n\n</details>"
                )
        else:
            # No provenance labels -> best effort: link the upstream repo.
            guess = source or "https://github.com/" + "/".join(image.split("/")[1:])
            rows.append(
                f"| `{name}` | {change} | ? | "
                f"[browse commits]({guess}/commits) — no `revision` label |"
            )

    if not rows:
        return

    print("### Upstream changes in this image bump\n")
    print("| image | version | commits | diff |")
    print("|---|---|---|---|")
    print("\n".join(rows))
    if details:
        print()
        print("\n\n".join(details))
    print(
        "\n<sub>Resolved from each digest's "
        "`org.opencontainers.image.revision` label — Renovate can't produce this "
        "for a digest bump on its own.</sub>"
    )


if __name__ == "__main__":
    main()
