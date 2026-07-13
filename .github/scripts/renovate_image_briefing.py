#!/usr/bin/env python3
# Copyright (c) 2026 Kenneth J Baker <bakerkj@umich.edu>
#
# Mechanize the evidence-gathering for an image-bump review, so the agent is
# handed FACTS instead of a research task.
#
# renovate_image_diff.py answers "what changed upstream". This answers the two
# questions that actually decide whether it MATTERS to us -- and both are pure
# set arithmetic, so they should never be an LLM's job:
#
#   1. WHICH FILES WE EXTRACT CHANGED UPSTREAM.
#      We do not run these containers. We `COPY --from=<stage> <path>` specific
#      files out of 11 upstream images and execute them inside OUR container,
#      under OUR env. So: parse `FROM ... AS <stage>` + `COPY --from=<stage>`
#      out of our Dockerfile to get exactly what we lift from each image, ask
#      the GitHub compare API which files the upstream range touched, and
#      INTERSECT. A hit is the highest-signal finding available -- that change
#      landed in our image and runs, with nothing to fail.
#
#      Matching is by path SUFFIX: upstream repos stage their container tree
#      under rootfs/, so `rootfs/etc/s6-overlay/scripts/pfclient` is the source
#      of the container's `/etc/s6-overlay/scripts/pfclient`.
#
#   2. WHICH UPSTREAM ENV DEFAULTS CHANGED, AND WHETHER WE INHERIT THEM.
#      An ENV default that changes upstream is invisible: nothing errors, the
#      container just behaves differently. Diff the ENV lines of the upstream
#      Dockerfile between the two revisions, then check whether WE set that same
#      variable anywhere. If we set it, we override and it cannot bite us. If we
#      do NOT, we silently inherit the new value -- which is exactly the class of
#      bug that hides.
#
# Emits markdown. Exits 0 with no output when nothing changed.
#
# Usage: renovate_image_briefing.py <base-ref> <head-ref> [dockerfile]

import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from renovate_image_diff import IMAGE_RE, labels_for, refs_in, run

DOCKERFILE = "aviation_feeder/Dockerfile"
# Where we look to decide "do we set this env ourselves?"
OUR_TREE = ["aviation_feeder/Dockerfile", "aviation_feeder/rootfs"]

# FROM <image>:<tag>@<digest> AS <stage>
FROM_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+)\s+AS\s+(?P<stage>[\w.-]+)", re.M | re.I
)
# COPY --from=<stage> <src> [<src> ...] <dst>   (we want the srcs)
COPY_RE = re.compile(
    r"^COPY\s+--from=(?P<stage>[\w.-]+)\s+(?P<rest>.+)$", re.M | re.I
)
# ENV KEY=VALUE  /  ENV KEY VALUE
ENV_RE = re.compile(r"^\s*ENV\s+(?P<key>[A-Z_][A-Z0-9_]*)[=\s]+(?P<val>.*)$", re.M)


def gh_json(url, token):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: GET {url} failed: {exc}", file=sys.stderr)
        return None


def stages_and_extracts(dockerfile_text):
    """-> {stage: image}, {stage: [extracted container paths]}"""
    stage_image = {}
    for m in FROM_RE.finditer(dockerfile_text):
        im = IMAGE_RE.search(m.group("ref"))
        if im:
            stage_image[m.group("stage")] = im.group("image")

    extracts = {}
    for m in COPY_RE.finditer(dockerfile_text):
        parts = m.group("rest").split()
        # last token is the destination; everything before it is a source
        srcs = parts[:-1] if len(parts) > 1 else parts
        extracts.setdefault(m.group("stage"), []).extend(
            s for s in srcs if s.startswith("/")
        )
    return stage_image, extracts


def changed_files(repo_path, old, new, token):
    """Files touched in the upstream range. None if the compare failed."""
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/compare/{old}...{new}", token
    )
    if data is None:
        return None
    return [f["filename"] for f in data.get("files", [])]


def env_at(repo_path, rev, token):
    """{KEY: VALUE} from the upstream Dockerfile at a revision. None on failure."""
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/contents/Dockerfile?ref={rev}", token
    )
    if not data or "content" not in data:
        return None
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None
    return {m.group("key"): m.group("val").strip() for m in ENV_RE.finditer(text)}


def we_set(key):
    """Do WE set this env ourselves (-> we override, it cannot bite us)?"""
    proc = subprocess.run(
        ["grep", "-rqE", rf"(^|[^A-Z_]){re.escape(key)}\s*[=:]", *OUR_TREE],
        capture_output=True,
    )
    return proc.returncode == 0


def suffix_hits(extracted, upstream_files):
    """Upstream files that ARE the source of a path we extract.

    Upstream repos stage the container tree under rootfs/, so
    rootfs/etc/s6-overlay/scripts/pfclient -> /etc/s6-overlay/scripts/pfclient.
    """
    hits = []
    for path in extracted:
        tail = path.lstrip("/")
        for f in upstream_files:
            if f == tail or f.endswith("/" + tail):
                hits.append((path, f))
    return hits


def main():
    base, head = sys.argv[1], sys.argv[2]
    dockerfile = sys.argv[3] if len(sys.argv) > 3 else DOCKERFILE
    token = __import__("os").environ.get("GITHUB_TOKEN", "")

    diff = run("git", "diff", f"{base}...{head}", "--", dockerfile, fatal=True).splitlines()
    old, new = refs_in(diff, "-"), refs_in(diff, "+")

    head_df = run("git", "show", f"{head}:{dockerfile}", fatal=True)
    stage_image, extracts = stages_and_extracts(head_df)
    image_stages = {}
    for stage, image in stage_image.items():
        image_stages.setdefault(image, []).append(stage)

    sections = []
    for image in sorted(set(old) & set(new)):
        (_, old_digest), (_, new_digest) = old[image], new[image]
        if old_digest == new_digest:
            continue  # retag only

        name = image.rsplit("/", 1)[-1]
        new_lab = labels_for(image, new_digest) or {}
        old_lab = labels_for(image, old_digest) or {}
        source = new_lab.get("org.opencontainers.image.source") or ""
        old_rev = old_lab.get("org.opencontainers.image.revision")
        new_rev = new_lab.get("org.opencontainers.image.revision")

        if not (source.startswith("https://github.com/") and old_rev and new_rev):
            sections.append(
                f"### `{name}`\n\n"
                ":warning: no OCI provenance labels — upstream range could NOT be "
                "resolved, so no mechanical analysis was possible for this image. "
                "Do not guess a range.\n"
            )
            continue

        repo_path = source[len("https://github.com/"):].rstrip("/")
        files = changed_files(repo_path, old_rev, new_rev, token)
        if files is None:
            sections.append(f"### `{name}`\n\n:warning: compare API failed.\n")
            continue

        # 1. files we EXTRACT that changed upstream
        extracted = []
        for stage in image_stages.get(image, []):
            extracted.extend(extracts.get(stage, []))
        hits = suffix_hits(extracted, files)

        # 2. upstream ENV defaults that changed, and whether we inherit them
        env_old, env_new = env_at(repo_path, old_rev, token), env_at(repo_path, new_rev, token)
        env_rows = []
        if env_old is not None and env_new is not None:
            for key in sorted(set(env_old) | set(env_new)):
                before, after = env_old.get(key), env_new.get(key)
                if before == after:
                    continue
                inherited = not we_set(key)
                env_rows.append((key, before, after, inherited))

        body = [f"### `{name}`  ({len(files)} upstream file(s) changed)\n"]

        body.append(f"**Files we `COPY --from` this image: {len(extracted)}**\n")
        if hits:
            body.append(
                ":rotating_light: **We extract files that CHANGED upstream — "
                "these land in our image and run. Read their diffs first.**\n"
            )
            body.append("| we extract | upstream source file |")
            body.append("|---|---|")
            for path, f in hits:
                body.append(f"| `{path}` | [`{f}`]({source}/blob/{new_rev}/{f}) |")
            body.append("")
        else:
            body.append(
                "No file we extract was touched in this range "
                "(mechanically verified: the extracted paths do not intersect the "
                "upstream changed-file list).\n"
            )

        if env_old is None or env_new is None:
            body.append(
                ":grey_question: Could not read the upstream Dockerfile at both "
                "revisions, so ENV defaults were NOT compared.\n"
            )
        elif env_rows:
            body.append("**Upstream ENV defaults that changed:**\n")
            body.append("| env | before | after | do we set it? |")
            body.append("|---|---|---|---|")
            for key, before, after, inherited in env_rows:
                verdict = (
                    ":rotating_light: **NO — we INHERIT the new value**"
                    if inherited
                    else "yes — we override it, no impact"
                )
                body.append(
                    f"| `{key}` | `{before or '(unset)'}` | `{after or '(removed)'}` | {verdict} |"
                )
            body.append("")
        else:
            body.append("No upstream ENV default changed in this range.\n")

        sections.append("\n".join(body))

    if not sections:
        return

    print("## Mechanical briefing\n")
    print(
        "_Computed, not inferred: the extracted-path intersection and the ENV-default "
        "diff below are set arithmetic over our Dockerfile and the upstream compare "
        "API. Trust these facts and spend your effort on JUDGEMENT — what the changed "
        "code actually does inside our container._\n"
    )
    print("\n".join(sections))


if __name__ == "__main__":
    main()
