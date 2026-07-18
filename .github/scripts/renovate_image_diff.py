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


def run(*args, fatal=False):
    """Run a command. Returns stdout, or None on failure.

    A silently-swallowed failure here is the worst outcome for this tool: a
    broken `git diff` or `imagetools inspect` would render as "no upstream
    changes" / "no revision label" and quietly lie in the PR comment. So a
    failure is always surfaced -- fatally for the diff (without it we know
    nothing), loudly for an inspect (that image is reported as un-inspectable).
    """
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        detail = err[-1] if err else "(no stderr)"
        msg = f"`{' '.join(args[:4])} …` exited {proc.returncode}: {detail}"
        if fatal:
            sys.exit(f"error: {msg}")
        print(f"warning: {msg}", file=sys.stderr)
        return None
    return proc.stdout


def refs_in(diff_lines, sign):
    """Map image -> (tag, digest) for added ('+') or removed ('-') diff lines.

    Keyed by image name, so the same image used by two FROM stages with
    DIFFERENT digests would mis-pair. That can't happen in this Dockerfile (each
    stage uses a distinct image), but guard it rather than pair silently wrong.
    """
    out = {}
    for line in diff_lines:
        if not line.startswith(sign) or line.startswith(sign * 3):
            continue
        m = IMAGE_RE.search(line)
        if not m:
            continue
        image, val = m.group("image"), (m.group("tag"), m.group("digest"))
        if image in out and out[image] != val:
            sys.exit(
                f"error: {image} appears on multiple '{sign}' lines with "
                f"different digests ({out[image][1]} vs {val[1]}); "
                f"old/new pairing would be ambiguous."
            )
        out[image] = val
    return out


def labels_for(image, digest):
    """OCI labels for a digest, or None if the image could not be inspected.

    None (inspect failed) is deliberately distinct from {} (inspected fine, but
    the image carries no labels) so a toolchain failure is never reported as
    "this image has no revision label".
    """
    raw = run(
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        f"{image}@{digest}",
        "--format",
        "{{json .Image}}",
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print(
            f"warning: {image}@{digest[:19]} inspect returned non-JSON", file=sys.stderr
        )
        return None
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
        return None, [], {}
    subjects = [c["commit"]["message"].splitlines()[0] for c in data.get("commits", [])]
    # The SAME response already carries every changed file and its patch. Return
    # them so the briefing step does not have to make this call a second time.
    # Keep the STATUS too (added / modified / removed / renamed). Discarding it made
    # a DELETED upstream file report identically to an added one -- so a base bump
    # that removes a rootfs script we do not override would still have been reported
    # as ":rotating_light: theirs runs", for a file that no longer exists.
    files = {
        f["filename"]: {
            "patch": f.get("patch") or "",
            "status": f.get("status") or "modified",
        }
        for f in data.get("files", [])
    }
    return data.get("total_commits"), subjects, files


def main():
    # Everything this script resolves (digests -> labels -> revisions -> the
    # compare payload) is dumped here when RESOLVED_JSON is set, so the briefing
    # step can CONSUME it instead of re-running `docker buildx imagetools inspect`
    # on every digest and calling the compare API all over again. Resolving twice
    # in one job is the same waste we removed across two workflows.
    resolved = {}
    base, head = sys.argv[1], sys.argv[2]
    dockerfile = sys.argv[3] if len(sys.argv) > 3 else "aviation_feeder/Dockerfile"
    token = __import__("os").environ.get("GITHUB_TOKEN", "")

    # fatal: if the diff itself fails we know nothing, and an empty report would
    # be read as "no upstream changes" -- exactly the lie we must not tell.
    diff = run(
        "git", "diff", f"{base}...{head}", "--", dockerfile, fatal=True
    ).splitlines()
    old, new = refs_in(diff, "-"), refs_in(diff, "+")

    rows, details = [], []
    for image in sorted(set(old) & set(new)):
        (old_tag, old_digest), (new_tag, new_digest) = old[image], new[image]
        if old_digest == new_digest:
            continue  # retag only -- same image, nothing upstream changed

        name = image.rsplit("/", 1)[-1]
        change = f"`{old_tag}` → `{new_tag}`" if old_tag != new_tag else f"`{new_tag}`"

        entry = resolved.setdefault(
            image,
            {
                "old_tag": old_tag,
                "new_tag": new_tag,
                "old_digest": old_digest,
                "new_digest": new_digest,
            },
        )
        old_lab, new_lab = labels_for(image, old_digest), labels_for(image, new_digest)
        if old_lab is None or new_lab is None:
            # Inspect failed -- say so. Do NOT fall through to the "no revision
            # label" branch, which would misreport a broken toolchain as an
            # image that simply lacks provenance.
            rows.append(
                f"| `{name}` | {change} | ? | "
                f":warning: could not inspect image — see workflow log |"
            )
            continue
        source = new_lab.get("org.opencontainers.image.source") or ""
        old_rev = old_lab.get("org.opencontainers.image.revision")
        new_rev = new_lab.get("org.opencontainers.image.revision")
        entry.update(source=source, old_rev=old_rev, new_rev=new_rev)

        if source.startswith("https://github.com/") and old_rev and new_rev:
            repo_path = source[len("https://github.com/") :].rstrip("/")
            count, subjects, files = gh_compare(repo_path, old_rev, new_rev, token)
            entry.update(repo_path=repo_path, subjects=subjects, files=files)
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

    dump = __import__("os").environ.get("RESOLVED_JSON")
    if dump:
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump(resolved, fh)

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
