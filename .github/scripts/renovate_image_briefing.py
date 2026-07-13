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


def go_buildinfo(image, digest, candidate_paths):
    """Recover (revision, module) from a Go binary we extract from the image.

    Some images ship NO OCI provenance labels at all (plane-watch is one), so
    the digest -> commit mapping everything else relies on is simply unavailable.
    But a Go binary embeds its own build info: `go version -m` prints
    vcs.revision -- the exact commit it was built from -- straight out of the
    binary WE extract and WE run. That is strictly better evidence than a label:
    it describes the artifact itself, not the image that carried it.

    Only tries paths we actually `COPY --from` this image, since those are the
    only files whose provenance we care about.
    """
    for path in candidate_paths:
        cid = run("docker", "create", f"{image}@{digest}")
        if not cid:
            return None, None
        cid = cid.strip()
        local = f"/tmp/gobin-{path.strip('/').replace('/', '_')}"
        copied = run("docker", "cp", f"{cid}:{path}", local)
        run("docker", "rm", "-f", cid)
        if copied is None:
            continue
        info = run("go", "version", "-m", local)
        if not info:
            continue
        rev = mod = None
        for line in info.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "build" and parts[1].startswith("vcs.revision="):
                rev = parts[1].split("=", 1)[1]
            elif len(parts) >= 2 and parts[0] == "mod":
                mod = parts[1]
            elif len(parts) >= 2 and parts[0] == "path":
                mod = mod or parts[1]
        if rev:
            return rev, mod
    return None, None


def resolve_repo(image, module, rev, token):
    """Which GitHub repo does this commit live in?

    A Go module path is often NOT a URL (plane-watch's pw-feeder reports simply
    `pw-feeder (devel)`), and the binary's source repo is frequently a DIFFERENT
    repo from the one that builds the image -- pw-feeder's commits live in
    plane-watch/pw-feeder, not plane-watch/docker-plane-watch. So propose
    candidates and CONFIRM by asking GitHub whether the commit actually exists
    there. Never guess: a wrong repo yields a plausible, wrong compare link.
    """
    org = image.split("/")[1] if image.count("/") >= 2 else None
    repo_name = image.rsplit("/", 1)[-1]
    base_mod = (module or "").split("/")[0].removesuffix(".git") if module else None

    candidates = []
    if module and module.startswith("github.com/"):
        candidates.append("/".join(module[len("github.com/"):].split("/")[:2]))
    if org and base_mod:
        candidates.append(f"{org}/{base_mod}")
    if org:
        candidates.append(f"{org}/{repo_name}")

    for repo in candidates:
        if gh_json(f"https://api.github.com/repos/{repo}/commits/{rev}", token):
            return repo
    return None


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


MAX_PATCH_LINES = 200


def compare(repo_path, old, new, token):
    """-> ({filename: patch}, [commit subjects]). (None, []) if the compare failed.

    Keeps the PATCH, not just the filename: the compare API already returns the
    diff of every changed file, so fetching the filename list and then making the
    agent go back for the diffs would be paying for the same data twice. Handing
    over the actual changed lines is the whole point -- it is what the agent has
    to exercise judgement on.
    """
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/compare/{old}...{new}", token
    )
    if data is None:
        return None, []
    patches = {f["filename"]: f.get("patch") or "" for f in data.get("files", [])}
    subjects = [
        c["commit"]["message"].splitlines()[0] for c in data.get("commits", [])
    ]
    return patches, subjects


def clip(patch):
    """Bound a patch so one huge file cannot crowd out the rest of the briefing."""
    lines = patch.splitlines()
    if len(lines) <= MAX_PATCH_LINES:
        return patch
    kept = "\n".join(lines[:MAX_PATCH_LINES])
    return f"{kept}\n… [truncated {len(lines) - MAX_PATCH_LINES} more diff lines]"


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

        extracted = []
        for stage in image_stages.get(image, []):
            extracted.extend(extracts.get(stage, []))

        repo_path = (
            source[len("https://github.com/"):].rstrip("/")
            if source.startswith("https://github.com/")
            else None
        )
        provenance = "OCI `revision` label"
        go_provenance = False

        # No labels? Ask the BINARY. A Go binary embeds vcs.revision, so we can
        # recover the exact commit from the artifact we actually extract and run
        # -- and its source repo is often NOT the repo that builds the image
        # (pw-feeder's commits live in plane-watch/pw-feeder, not
        # plane-watch/docker-plane-watch), so the repo is confirmed against
        # GitHub rather than guessed.
        if not (old_rev and new_rev):
            new_rev, module = go_buildinfo(image, new_digest, extracted)
            old_rev, _ = go_buildinfo(image, old_digest, extracted)
            if new_rev and old_rev:
                repo_path = resolve_repo(image, module, new_rev, token)
                provenance = "Go build info (`vcs.revision`) — image ships no OCI labels"
                go_provenance = True

        if not (repo_path and old_rev and new_rev):
            sections.append(
                f"### `{name}`\n\n"
                ":warning: upstream range could NOT be resolved — no OCI provenance "
                "labels, and no Go build info recoverable from the files we extract. "
                "No mechanical analysis was possible. **Do not guess a range.**\n"
            )
            continue

        patches, subjects = compare(repo_path, old_rev, new_rev, token)
        if patches is None:
            sections.append(f"### `{name}`\n\n:warning: compare API failed.\n")
            continue
        files = list(patches)

        # 1. files we EXTRACT that changed upstream
        hits = suffix_hits(extracted, files)

        # 2. upstream ENV defaults that changed, and whether we inherit them.
        # Only meaningful for the repo that BUILDS the image: when provenance came
        # from a Go binary, repo_path is the binary's SOURCE repo, which has no
        # Dockerfile and no ENV defaults of its own. Don't 404 chasing one.
        if go_provenance:
            env_old = env_new = None
        else:
            env_old, env_new = env_at(repo_path, old_rev, token), env_at(repo_path, new_rev, token)
        env_rows = []
        if env_old is not None and env_new is not None:
            for key in sorted(set(env_old) | set(env_new)):
                before, after = env_old.get(key), env_new.get(key)
                if before == after:
                    continue
                inherited = not we_set(key)
                env_rows.append((key, before, after, inherited))

        repo_url = f"https://github.com/{repo_path}"
        body = [
            f"### `{name}`  ({len(files)} upstream file(s) changed)\n",
            f"- range: [`{old_rev[:7]}…{new_rev[:7]}`]"
            f"({repo_url}/compare/{old_rev}...{new_rev}) in `{repo_path}`",
            f"- provenance: {provenance}\n",
        ]

        body.append(f"**Files we `COPY --from` this image: {len(extracted)}**\n")

        # A COMPILED artifact has no counterpart file in the upstream repo, so the
        # path intersection below can never hit for it -- and reporting "no file we
        # extract changed" would be actively misleading. When provenance came from
        # the binary's own Go build info, that repo IS the source of the binary we
        # execute: every commit in the range is a change to code we run. Say so,
        # and hand over all the diffs.
        if go_provenance:
            body.append(
                f":rotating_light: **`{extracted[0] if extracted else '(binary)'}` is a "
                f"COMPILED binary built from `{repo_path}` — it has no source file in "
                "the image repo, so the path intersection below cannot apply. EVERY "
                "commit in this range is a change to code we execute. The diffs are "
                "inlined; judge them.**\n"
            )
            for f, patch in list(patches.items())[:10]:
                body.append(
                    f"<details open><summary><code>{f}</code></summary>\n"
                )
                body.append("```diff")
                body.append(clip(patch) or "(no textual diff)")
                body.append("```\n</details>\n")
            if len(patches) > 10:
                body.append(f"_… and {len(patches) - 10} more changed file(s)._\n")
        elif hits:
            body.append(
                ":rotating_light: **We extract files that CHANGED upstream — these "
                "land in our image and run. Their diffs are inlined below; that is "
                "the code you must judge.**\n"
            )
            for path, f in hits:
                body.append(
                    f"<details open><summary><code>{path}</code> "
                    f"&larr; <a href=\"{repo_url}/blob/{new_rev}/{f}\">"
                    f"<code>{f}</code></a></summary>\n"
                )
                body.append("```diff")
                body.append(clip(patches.get(f, "")) or "(no textual diff — binary or renamed)")
                body.append("```\n</details>\n")
        else:
            body.append(
                "No file we extract was touched in this range "
                "(mechanically verified: the extracted paths do not intersect the "
                "upstream changed-file list).\n"
            )

        if go_provenance:
            body.append(
                "_ENV defaults not compared: the resolved repo is the BINARY's source "
                "repo, which builds no image and defines no ENV._\n"
            )
        elif env_old is None or env_new is None:
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

        # The full commit list, so the agent can scan the range for the things
        # that CANNOT be mechanized (a renamed s6 service, a new option upstream
        # now expects, a behavioural change inside a compiled binary) without
        # having to go and fetch it.
        if subjects:
            body.append(
                f"<details><summary>all {len(subjects)} upstream commit(s) in range"
                "</summary>\n"
            )
            body.extend(f"- {s}" for s in subjects[:40])
            if len(subjects) > 40:
                body.append(f"- … and {len(subjects) - 40} more")
            body.append("\n</details>\n")

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
