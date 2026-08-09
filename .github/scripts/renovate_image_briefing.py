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
#   2. WHICH UPSTREAM ENV DEFAULTS CHANGED OR WERE ADDED, AND WHETHER WE INHERIT
#      THEM. An ENV default that changes upstream is invisible: nothing errors,
#      the container just behaves differently. Diff the ENV lines of the upstream
#      Dockerfile between the two revisions -- surfacing NEW vars (no prior
#      default) on their own, not folded into the "changed" table -- then check
#      whether WE set that same variable anywhere. If we set it, we override and
#      it cannot bite us. If we do NOT, we silently inherit the new value -- which
#      is exactly the class of bug that hides. This runs for the transitive base
#      too (its Dockerfile ENV is inherited the same way).
#
# It does NOT inline the bulk. Everything heavy is PRE-FETCHED into a directory
# that is handed to the agent, and the briefing is just the index into it:
#
#   upstream/
#     briefing.md                     <- small, high-signal: ranges, hits, ENV table
#     <image>/
#       range.txt                     repo, old/new rev, compare URL, provenance
#       commits.md                    every commit's subject + body (linked issues)
#       changed-files.txt             every file the range touched
#       patches/<flat-path>.diff      the FULL patch per changed file (unclipped)
#       extracted/<flat-path>.before  whole file at the OLD rev  } for files WE
#       extracted/<flat-path>.after   whole file at the NEW rev  } extract + run
#
# Why a directory rather than one big briefing:
#   * the briefing stays small -- the bulk never has to fit in the context window,
#     and nothing needs clipping to protect it. The agent reads only what it needs.
#   * we can stage MORE than a patch. A diff hunk carries 3 lines of context, which
#     is often too little to judge a shell script; the whole before/after file is
#     staged for anything we actually extract and execute.
#   * the agent then needs NO NETWORK. With the data on disk, it holds no gh/api
#     tool at all -- so an instruction injected into an upstream commit message has
#     no egress and no write path.
#   * it is inspectable: the directory uploads as a CI artifact, so when a verdict
#     looks wrong you can read exactly what the agent was handed.
#
# Writes the tree; prints nothing. Exits 0 having written nothing when no digest
# changed.
#
# Usage: renovate_image_briefing.py <base-ref> <head-ref> <out-dir> [dockerfile]

import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from renovate_image_diff import (
    IMAGE_RE,
    labels_for,
    provenance_deps,
    refs_in,
    run,
)

DOCKERFILE = "aviation_feeder/Dockerfile"
# Where we look to decide "do we set this env ourselves?"
OUR_TREE = ["aviation_feeder/Dockerfile", "aviation_feeder/rootfs"]

# FROM <image>:<tag>@<digest> AS <stage>
FROM_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+)\s+AS\s+(?P<stage>[\w.-]+)", re.MULTILINE | re.IGNORECASE
)
# COPY --from=<stage> <src> [<src> ...] <dst>   (we want the srcs)
COPY_RE = re.compile(
    r"^COPY\s+--from=(?P<stage>[\w.-]+)\s+(?P<rest>.+)$", re.MULTILINE | re.IGNORECASE
)
# ENV KEY=VALUE  /  ENV KEY VALUE
ENV_RE = re.compile(
    r"^\s*ENV\s+(?P<key>[A-Z_][A-Z0-9_]*)[=\s]+(?P<val>.*)$", re.MULTILINE
)
# Issue/PR refs in a commit message: `#280`, `owner/repo#280`. The trailing \b
# drops a hex-colour false positive like `#280fff` (letters after the digits).
ISSUE_RE = re.compile(r"#(\d+)\b")


def go_buildinfo(image, digest, candidate_paths):
    """Recover (revision, module, path) from a Go binary we extract from the image.

    Returns the PATH that actually yielded the build info, not just the revision:
    a stage can extract several files (57 paths across 11 stages here), and only
    one of them may be the Go binary. Labelling the wrong file as "the compiled
    binary we execute" would hand the agent a false identity for the single
    highest-priority finding category this workflow has.

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
            return None, None, None
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
            if (
                len(parts) >= 2
                and parts[0] == "build"
                and parts[1].startswith("vcs.revision=")
            ):
                rev = parts[1].split("=", 1)[1]
            elif len(parts) >= 2 and parts[0] == "mod":
                mod = parts[1]
            elif len(parts) >= 2 and parts[0] == "path":
                mod = mod or parts[1]
        if rev:
            return rev, mod, path
    return None, None, None


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
        candidates.append("/".join(module[len("github.com/") :].split("/")[:2]))
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


def compare(repo_path, old, new, token):
    """-> ({filename: patch}, [commit dicts]). (None, []) if the compare failed.

    Keeps the PATCH, not just the filename: the compare API already returns the
    diff of every changed file, so fetching the filename list and then making the
    agent go back for the diffs would be paying for the same data twice. Handing
    over the actual changed lines is the whole point -- it is what the agent has
    to exercise judgement on.

    Each commit dict is {sha, subject, body}: the BODY carries linked-issue refs
    (e.g. `…#280`) the subject line drops. Staging it here saves the reviewer a
    round-trip to the compare API just to read a commit message.
    """
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/compare/{old}...{new}", token
    )
    if data is None:
        return None, []
    patches = {
        f["filename"]: {
            "patch": f.get("patch") or "",
            "status": f.get("status") or "modified",
        }
        for f in data.get("files", [])
    }
    commits = [
        {
            "sha": (c.get("sha") or "")[:7],
            "subject": c["commit"]["message"].splitlines()[0],
            "body": "\n".join(c["commit"]["message"].splitlines()[1:]).strip(),
        }
        for c in data.get("commits", [])
    ]
    return patches, commits


def file_at(repo_path, path, rev, token):
    """Whole file content at a revision, or None. Used to stage the BEFORE/AFTER
    of files we extract: a patch hunk carries 3 lines of context, which is often
    too little to judge a shell script that runs in our container."""
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/contents/{path}?ref={rev}", token
    )
    if not data or "content" not in data:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def flat(path):
    """A filesystem-safe leaf name for an upstream path."""
    return path.strip("/").replace("/", "__")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def format_commits(commits):
    """Render commits.md: subject, any linked issues, then the indented body.

    The body is where a commit says WHY (and links the issue, e.g. `…#280`) --
    dropping it to the subject line alone was a documented gap: the reviewer had to
    call the compare API again just to read one message. Bodies are indented so
    they read as a block under their subject.
    """
    if not commits:
        return "(none)"
    blocks = []
    for c in commits:
        sha = c.get("sha") or ""
        head = f"- {c['subject']}" + (f"  (`{sha}`)" if sha else "")
        block = [head]
        body = (c.get("body") or "").strip()
        refs = sorted(
            {int(m.group(1)) for m in ISSUE_RE.finditer(c["subject"] + "\n" + body)}
        )
        if refs:
            block.append("  linked: " + ", ".join(f"#{n}" for n in refs))
        if body:
            block.append("")
            block += [f"      {ln}" for ln in body.splitlines()]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


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
    """HOW do we set this env ourselves? -> a short string, or None if we do not.

    Getting this WRONG is worse than not checking: the prompt tells the agent to
    trust the briefing, so a false "we do not set it" turns an override we already
    make into a confirmed finding -- a systematic false alarm.

    The naive `KEY=` grep did exactly that. This add-on does NOT set container env
    with `KEY=value`: 00-haos-options bridges HA options into the s6
    container-environment directory through a helper --

        setenv TAR1090_SITENAME "${SITE}"

    -- which writes one file per variable. So `KEY=` never appears, and TAR1090_SITENAME,
    MLAT_USER, READSB_DEVICE_TYPE and friends all read as "inherited" when we in
    fact override every one of them.

    Match the real forms, and skip comment lines so a variable merely MENTIONED in
    prose is not mistaken for one we set (a false positive here silently suppresses
    a real finding, which is the dangerous direction).
    """
    k = re.escape(key)
    forms = (
        (rf"^[^#]*\bsetenv\s+{k}\b", "setenv (00-haos-options bridge)"),
        (rf"^[^#]*\bexport\s+{k}\s*=", "export"),
        (rf"^[^#]*\bENV\s+{k}[=\s]", "Dockerfile ENV"),
        (rf"^[^#]*(^|[^A-Z_]){k}\s*=", "assignment"),
    )
    for pattern, how in forms:
        proc = subprocess.run(
            ["grep", "-rqE", pattern, *OUR_TREE], capture_output=True, check=False
        )
        if proc.returncode == 0:
            return how
    return None


def env_diff_lines(env_old, env_new, source=""):
    """Markdown lines for the ENV delta between two Dockerfile revisions.

    Splits the delta three ways so a BRAND-NEW upstream var is surfaced on its own.
    The old single "changed" table folded new vars into a `(unset) -> value` row,
    which is why a genuinely new default (GRAPHS1090_ETHERNET_DEVICE=eth0, added in
    the transitive base) slipped past the reviewer -- it read as noise among value
    changes instead of a new default we now silently inherit. `source` names the
    layer (e.g. a transitive base) for the headings; empty for the built image.
    """
    added = sorted(k for k in env_new if k not in env_old)
    changed = sorted(k for k in env_new if k in env_old and env_new[k] != env_old[k])
    removed = sorted(k for k in env_old if k not in env_new)
    label = f"`{source}` " if source else ""
    if not (added or changed or removed):
        return [
            f"No {label}upstream ENV default added, changed, or removed in this range.",
            "",
        ]

    def verdict(key, removed=False):
        how = we_set(key)
        if how:
            # We set it ourselves either way -- so a changed/added upstream default
            # is overridden, and a removed one never mattered to us.
            tail = (
                "we set it ourselves, upstream's removal has no impact"
                if removed
                else "we override it, no impact"
            )
            return f"yes, via {how} — {tail}"
        # We do NOT set it. For added/changed we silently inherit the new value; for
        # removed there is nothing left to inherit -- the default simply disappears.
        return (
            ":rotating_light: **NO — the default disappears; falls back to the app/lower-layer default**"
            if removed
            else ":rotating_light: **NO — we INHERIT it**"
        )

    lines = []
    if added:
        lines += [
            f"**New {label}upstream ENV vars (no prior default — we inherit unless we set it):**",
            "",
            "| env | new default | do we set it? |",
            "|---|---|---|",
        ]
        for key in added:
            lines.append(
                f"| `{key}` | `{env_new[key] or '(empty)'}` | {verdict(key)} |"
            )
        lines.append("")
    if changed:
        lines += [
            f"**{label}upstream ENV defaults that changed:**",
            "",
            "| env | before | after | do we set it? |",
            "|---|---|---|---|",
        ]
        for key in changed:
            lines.append(
                f"| `{key}` | `{env_old[key] or '(empty)'}` | "
                f"`{env_new[key] or '(empty)'}` | {verdict(key)} |"
            )
        lines.append("")
    if removed:
        lines += [
            f"**{label}upstream ENV vars removed (default no longer set upstream):**",
            "",
            "| env | was | do we set it? |",
            "|---|---|---|",
        ]
        for key in removed:
            lines.append(
                f"| `{key}` | `{env_old[key] or '(empty)'}` | {verdict(key, removed=True)} |"
            )
        lines.append("")
    return lines


OUR_ROOTFS = Path("aviation_feeder/rootfs")


def inherited_hits(upstream_files, statuses=None):
    """Changed files in the BASE image's rootfs -- i.e. code that runs in OUR container.

    The base image is not a `COPY --from` source: we inherit its ENTIRE filesystem,
    including its whole s6 service tree. So the analogue of "a file we extract
    changed" is "a file in its rootfs/ changed" -- that file lands in our container
    and executes, with nothing to fail.

    Found the hard way: build-939 -> build-942 ADDED a 443-line startup script
    (rootfs/etc/s6-overlay/startup.d/52-adsbitalia-register) that runs in our
    container on every start. The COPY --from intersection is empty for the base
    image BY DEFINITION, so it reported "no file we extract changed" -- true, and
    beside the point.

    Also reports whether WE ship our own file at the same container path, since our
    rootfs is COPY'd over theirs and therefore wins.
    """
    statuses = statuses or {}
    hits = []
    for f in upstream_files:
        if not f.startswith("rootfs/"):
            continue
        container_path = f[len("rootfs") :]  # rootfs/etc/... -> /etc/...
        ours = OUR_ROOTFS / container_path.lstrip("/")
        hits.append((container_path, f, ours.exists(), statuses.get(f, "modified")))
    return hits


def suffix_hits(extracted, upstream_files, statuses=None):
    """Upstream files that ARE the source of a path we extract.

    Upstream repos stage the container tree under rootfs/, so
    rootfs/etc/s6-overlay/scripts/pfclient -> /etc/s6-overlay/scripts/pfclient.
    """
    statuses = statuses or {}
    hits = []
    for path in extracted:
        tail = path.lstrip("/")
        for f in upstream_files:
            if f == tail or f.endswith("/" + tail):
                hits.append((path, f, statuses.get(f, "modified")))
    return hits


# FROM <ref> [AS <stage>] -- captures the ref and whether it names a build stage.
FROM_ANY_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def final_base_image(repo_path, rev, token):
    """The image named by the base's OWN final (runtime) `FROM`.

    The last `FROM` with no `AS` is the layer we actually inherit; earlier
    `FROM … AS <stage>` lines are build-only and never reach our container (e.g.
    ultrafeeder's `docker-baseimage:mlatclient AS buildimage`). Its tag is usually
    FLOATING (`:latest`), so this only NAMES the image -- the exact build-time
    digest comes from the base image's provenance (provenance_deps).

    Returns (image_name, None) on success, or (None, reason) explaining WHY the
    runtime FROM couldn't be resolved -- the caller surfaces that reason as a note
    rather than silently dropping the transitive analysis. Distinguishing the
    reasons matters: an unreadable Dockerfile is a transient/tooling failure worth
    flagging, whereas an ARG-based FROM is an expected can't-resolve-here.
    """
    data = gh_json(
        f"https://api.github.com/repos/{repo_path}/contents/Dockerfile?ref={rev}", token
    )
    if not data or "content" not in data:
        return None, f"could not fetch the base image's Dockerfile at `{rev[:7]}`"
    try:
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None, "could not decode the base image's Dockerfile"
    final = None
    for m in FROM_ANY_RE.finditer(text):
        if m.group("stage"):
            continue  # build stage -- not inherited into the final image
        ref = m.group("ref")
        if ref.startswith("$") or "/" not in ref:
            continue  # ARG-based FROM or a bare stage alias -- not resolvable here
        final = ref
    if not final:
        return (
            None,
            "the base image's final `FROM` is not a literal registry image (ARG-based?)",
        )
    # strip tag/digest -> bare name
    return final.split("@", 1)[0].split(":", 1)[0], None


def transitive_base(image, old_digest, new_digest, repo_path, new_rev, token, out_dir):
    """Analyse the base's OWN floating `FROM …:latest` the same way we analyse the
    base itself -- one level up, because its rootfs also lands in our container.

    Our base is inherited whole and is itself built `FROM <sdr image>:latest`, a
    floating tag no digest in our Dockerfile pins. A change one layer up is just as
    live in our container but invisible to the top-level diff. We recover that
    parent's EXACT old/new digests from the base image's provenance, resolve them
    the same way (revision label -> compare), and run the same inherited-rootfs
    intersection. Scoped to ONE level (the direct runtime FROM); it does NOT recurse
    into that parent's own base.

    Returns markdown lines to append to the base image's section (empty when there
    is nothing to add), staging any patches under out_dir/<parent>/.
    """
    tname, why = final_base_image(repo_path, new_rev, token)
    if not tname:
        # Consistent with every other branch below: surface WHY, never drop the
        # section silently -- a silent [] would reproduce the exact invisible
        # transitive change this analysis exists to catch (e.g. a transient GitHub
        # API hiccup fetching the base's Dockerfile).
        return [f":grey_question: Transitive base not analysed: {why}.", ""]
    tshort = tname.rsplit("/", 1)[-1]
    old_deps = provenance_deps(image, old_digest)
    new_deps = provenance_deps(image, new_digest)
    if not old_deps or not new_deps:
        return [
            (
                f":grey_question: Transitive base **`{tshort}`** not resolved: the base "
                "image ships no SLSA provenance attestation, so its floating parent's "
                "build-time digest is unrecoverable."
            ),
            "",
        ]
    t_old, t_new = old_deps.get(tname), new_deps.get(tname)
    if not (t_old and t_new):
        return [
            (
                f":grey_question: Transitive base **`{tshort}`** not found among the base "
                "image's provenance dependencies; cannot resolve its range."
            ),
            "",
        ]
    if t_old == t_new:
        return [f"_Transitive base `{tshort}` unchanged in this bump._", ""]

    t_old_lab = labels_for(tname, t_old) or {}
    t_new_lab = labels_for(tname, t_new) or {}
    t_source = t_new_lab.get("org.opencontainers.image.source") or ""
    t_old_rev = t_old_lab.get("org.opencontainers.image.revision")
    t_new_rev = t_new_lab.get("org.opencontainers.image.revision")
    t_repo = (
        t_source[len("https://github.com/") :].rstrip("/")
        if t_source.startswith("https://github.com/")
        else None
    )
    if not (t_repo and t_old_rev and t_new_rev):
        return [
            (
                f":warning: Transitive base **`{tshort}`** changed "
                f"(`{t_old[7:14]}` → `{t_new[7:14]}`) but its revision could not be "
                "resolved from OCI labels; range unavailable."
            ),
            "",
        ]

    patches, commits = compare(t_repo, t_old_rev, t_new_rev, token)
    if patches is None:
        return [f":warning: Transitive base **`{tshort}`** compare API failed.", ""]
    statuses = {f: m["status"] for f, m in patches.items()}
    base_hits = inherited_hits(list(patches), statuses)

    t_repo_url = f"https://github.com/{t_repo}"
    t_compare = f"{t_repo_url}/compare/{t_old_rev}...{t_new_rev}"
    td = out_dir / tshort
    write(
        td / "range.txt",
        f"image:      {tname} (transitive base, one level up)\n"
        f"repo:       {t_repo}\n"
        f"old_rev:    {t_old_rev}\n"
        f"new_rev:    {t_new_rev}\n"
        f"compare:    {t_compare}\n"
        "provenance: base image SLSA provenance -> OCI `revision` label\n",
    )
    write(td / "commits.md", format_commits(commits))
    write(td / "changed-files.txt", "\n".join(patches) or "(none)")
    for f, meta in patches.items():
        if meta["patch"]:
            write(
                td / "patches" / f"{flat(f)}.diff",
                f"# status: {meta['status']}\n{meta['patch']}",
            )

    lines = [
        (
            f"#### Transitive base: `{tshort}` "
            f"([`{t_old_rev[:7]}…{t_new_rev[:7]}`]({t_compare}))"
        ),
        "",
        (
            f":rotating_light: **`{tshort}` is the base image's OWN floating "
            "`FROM …:latest` — inherited into our container one layer up, and NOT "
            "pinned by any digest in our Dockerfile.** It changed in this bump; its "
            "`rootfs/` lands in our container exactly as the base's does."
        ),
        "",
        f"- {len(patches)} upstream file(s) changed; {len(commits)} commit(s)",
        (
            f"- prefetched: `{td}/` "
            "(`range.txt`, `commits.md`, `changed-files.txt`, `patches/`)"
        ),
        "",
    ]
    if base_hits:
        lines += [
            "| runs in our container as | upstream file | change | do we override it? |",
            "|---|---|---|---|",
        ]
        for container_path, f, overridden, status in base_hits:
            if status == "removed":
                verdict = "n/a — **file was DELETED upstream**, it no longer runs"
            elif overridden:
                verdict = "yes — our rootfs wins, no impact"
            else:
                verdict = ":rotating_light: **NO — theirs runs**"
            lines.append(
                f"| `{container_path}` | [`{f}`]({t_repo_url}/blob/{t_new_rev}/{f}) "
                f"| `{status}` | {verdict} |"
            )
        lines += ["", f"Full diffs: `{td}/patches/`.", ""]
    else:
        lines += [
            (
                "No file in this transitive range lands in our container's `rootfs/` "
                "(mechanically verified: no changed path is under `rootfs/`)."
            ),
            "",
        ]

    # ENV defaults declared in the transitive base's Dockerfile are inherited into
    # our container just like the top-level base's are, and were previously NOT
    # compared here -- a new default (e.g. GRAPHS1090_ETHERNET_DEVICE=eth0) only
    # surfaced by hand-reading the raw Dockerfile diff. Diff them the same way.
    t_env_old, t_env_new = (
        env_at(t_repo, t_old_rev, token),
        env_at(t_repo, t_new_rev, token),
    )
    if t_env_old is None or t_env_new is None:
        lines += [
            (
                f":grey_question: Could not read `{tshort}`'s Dockerfile at both "
                "revisions, so its ENV defaults were NOT compared."
            ),
            "",
        ]
    else:
        lines += env_diff_lines(t_env_old, t_env_new, source=tshort)
    return lines


def main():
    base, head = sys.argv[1], sys.argv[2]
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "upstream")
    dockerfile = sys.argv[4] if len(sys.argv) > 4 else DOCKERFILE
    token = __import__("os").environ.get("GITHUB_TOKEN", "")

    # Consume what the resolve step ALREADY worked out (digest -> label -> revision,
    # plus the whole compare payload). Re-deriving it here would mean a second
    # `docker buildx imagetools inspect` per digest and a second compare API call
    # per image, in the same job -- exactly the duplication we removed between the
    # two workflows, just moved one step over. Falls back to computing it when run
    # standalone (e.g. a local replay), so the script still works on its own.
    resolved = {}
    rj = __import__("os").environ.get("RESOLVED_JSON")
    if rj and Path(rj).exists():
        resolved = json.loads(Path(rj).read_text())

    diff = run(
        "git", "diff", f"{base}...{head}", "--", dockerfile, fatal=True
    ).splitlines()
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
            continue  # retag only -- same image, nothing upstream changed

        name = image.rsplit("/", 1)[-1]
        d = out / name

        extracted = []
        for stage in image_stages.get(image, []):
            extracted.extend(extracts.get(stage, []))

        cached = resolved.get(image, {})
        if cached:
            source = cached.get("source") or ""
            old_rev, new_rev = cached.get("old_rev"), cached.get("new_rev")
        else:  # standalone run -- resolve it ourselves
            new_lab = labels_for(image, new_digest) or {}
            old_lab = labels_for(image, old_digest) or {}
            source = new_lab.get("org.opencontainers.image.source") or ""
            old_rev = old_lab.get("org.opencontainers.image.revision")
            new_rev = new_lab.get("org.opencontainers.image.revision")
        repo_path = (
            source[len("https://github.com/") :].rstrip("/")
            if source.startswith("https://github.com/")
            else None
        )
        provenance = "OCI `revision` label"
        go_provenance = False
        go_path = None

        # No labels? Ask the BINARY -- see go_buildinfo().
        if not (old_rev and new_rev):
            new_rev, module, go_path = go_buildinfo(image, new_digest, extracted)
            old_rev, _, _ = go_buildinfo(image, old_digest, extracted)
            if new_rev and old_rev:
                repo_path = resolve_repo(image, module, new_rev, token)
                provenance = (
                    "Go build info (`vcs.revision`) -- image ships no OCI labels"
                )
                go_provenance = True

        if not (repo_path and old_rev and new_rev):
            sections.append(
                f"### `{name}`\n\n:warning: upstream range could NOT be resolved -- no "
                "OCI provenance labels, and no Go build info recoverable from the files "
                "we extract. No mechanical analysis was possible. **Do not guess a "
                "range.**\n"
            )
            continue

        # The compare payload is in the cache when the resolve step ran first.
        if cached.get("files") is not None and cached.get("repo_path") == repo_path:
            patches, commits = cached["files"], cached.get("commits", [])
        else:
            patches, commits = compare(repo_path, old_rev, new_rev, token)
        if patches is None:
            sections.append(f"### `{name}`\n\n:warning: compare API failed.\n")
            continue

        repo_url = f"https://github.com/{repo_path}"
        compare_url = f"{repo_url}/compare/{old_rev}...{new_rev}"

        # ---- stage the bulk on disk (the agent reads only what it needs) ----
        write(
            d / "range.txt",
            f"image:      {image}\n"
            f"repo:       {repo_path}\n"
            f"old_rev:    {old_rev}\n"
            f"new_rev:    {new_rev}\n"
            f"compare:    {compare_url}\n"
            f"provenance: {provenance}\n",
        )
        write(d / "commits.md", format_commits(commits))
        write(d / "changed-files.txt", "\n".join(patches) or "(none)")
        for f, meta in patches.items():
            if meta["patch"]:
                write(
                    d / "patches" / f"{flat(f)}.diff",
                    f"# status: {meta['status']}\n{meta['patch']}",
                )

        statuses = {f: m["status"] for f, m in patches.items()}
        hits = suffix_hits(extracted, list(patches), statuses)
        # The base image is inherited whole, not COPY'd from: its rootfs IS our
        # container's filesystem.
        base_hits = inherited_hits(list(patches), statuses) if not extracted else []

        # Whole before/after of the files we EXTRACT AND RUN. A hunk's 3 lines of
        # context are often not enough to judge a script; give the agent the file.
        for path, f, status in hits:
            before = file_at(repo_path, f, old_rev, token)
            # A removed file has no `after` -- do not silently write nothing and
            # leave the table pointing at a file that was never created.
            after = (
                None if status == "removed" else file_at(repo_path, f, new_rev, token)
            )
            if before is not None:
                write(d / "extracted" / f"{flat(path)}.before", before)
            if after is not None:
                write(d / "extracted" / f"{flat(path)}.after", after)

        # ---- the INDEX entry (small, high-signal) --------------------------
        body = [
            f"### `{name}`",
            "",
            f"- range: [`{old_rev[:7]}…{new_rev[:7]}`]({compare_url}) in `{repo_path}`",
            f"- provenance: {provenance}",
            (
                f"- {len(patches)} upstream file(s) changed; "
                f"{len(commits)} commit(s); we `COPY --from` {len(extracted)} path(s)"
            ),
            (
                f"- prefetched: `{d}/` "
                f"(`range.txt`, `commits.md`, `changed-files.txt`, `patches/`"
                f"{', `extracted/`' if hits else ''})"
            ),
            "",
        ]

        if go_provenance:
            # the path that ACTUALLY yielded the build info -- not extracted[0]
            binary = go_path or "(binary)"
            body += [
                (
                    f":rotating_light: **`{binary}` is a COMPILED binary built from "
                    f"`{repo_path}`.** It has no source file in the image repo, so the "
                    "extracted-path intersection cannot apply -- and 'no file we extract "
                    "changed' would be MISLEADING. **Every commit in this range is a "
                    f"change to code we execute.** Read the diffs in `{d}/patches/`."
                ),
                "",
            ]
        elif base_hits:
            body += [
                (
                    ":rotating_light: **This is the INHERITED BASE IMAGE — we take its "
                    "whole filesystem, including its entire s6 service tree. Every file "
                    "below changed in its `rootfs/`, so it lands in OUR container and "
                    "RUNS.** (`COPY --from` intersection is empty for the base image by "
                    "definition; that is not evidence of safety.)"
                ),
                "",
                "| runs in our container as | upstream file | change | do we override it? |",
                "|---|---|---|---|",
            ]
            for container_path, f, overridden, status in base_hits:
                if status == "removed":
                    verdict = "n/a — **file was DELETED upstream**, it no longer runs"
                elif overridden:
                    verdict = "yes — our rootfs wins, no impact"
                else:
                    verdict = ":rotating_light: **NO — theirs runs**"
                body.append(
                    f"| `{container_path}` | [`{f}`]({repo_url}/blob/{new_rev}/{f}) "
                    f"| `{status}` | {verdict} |"
                )
            body += [
                "",
                (
                    f"Full diffs: `{d}/patches/`. For a NEW file, read it in full and "
                    "find its enable-gate: an opt-in we never set is inert; an "
                    "on-by-default script is not."
                ),
                "",
            ]
        elif hits:
            body += [
                (
                    ":rotating_light: **We extract files that CHANGED upstream — these "
                    "land in our image and RUN. This is the highest-signal finding "
                    "available; read these first.**"
                ),
                "",
                "| we extract | upstream source | change | full diff | whole file |",
                "|---|---|---|---|---|",
            ]
            for path, f, status in hits:
                if status == "removed":
                    whole = (
                        ":rotating_light: **DELETED upstream** — only "
                        f"`{d}/extracted/{flat(path)}.before` exists"
                    )
                else:
                    whole = f"`{d}/extracted/{flat(path)}.{{before,after}}`"
                body.append(
                    f"| `{path}` | [`{f}`]({repo_url}/blob/{new_rev}/{f}) "
                    f"| `{status}` | `{d}/patches/{flat(f)}.diff` | {whole} |"
                )
            body.append("")
        else:
            body += [
                (
                    "No file we extract was touched in this range (mechanically "
                    "verified: the extracted paths do not intersect the upstream "
                    "changed-file list)."
                ),
                "",
            ]

        # One level up: the base image is itself built `FROM <sdr>:latest`, a
        # floating tag no digest here pins. That parent's rootfs is inherited into
        # our container too, so analyse it the same way (only for the base image --
        # a COPY --from stage has no inherited transitive base).
        if not extracted:
            body += transitive_base(
                image, old_digest, new_digest, repo_path, new_rev, token, d
            )

        # ENV defaults -- only meaningful for the repo that BUILDS the image.
        if go_provenance:
            body += [
                (
                    "_ENV defaults not compared: the resolved repo is the BINARY's "
                    "source repo, which builds no image and defines no ENV._"
                ),
                "",
            ]
        else:
            env_old, env_new = (
                env_at(repo_path, old_rev, token),
                env_at(repo_path, new_rev, token),
            )
            if env_old is None or env_new is None:
                body += [
                    (
                        ":grey_question: Could not read the upstream Dockerfile at both "
                        "revisions, so ENV defaults were NOT compared."
                    ),
                    "",
                ]
            else:
                body += env_diff_lines(env_old, env_new)

        sections.append("\n".join(body))

    if not sections:
        return

    index = [
        "## Mechanical briefing",
        "",
        (
            "_Computed, not inferred. The extracted-path intersection and the "
            "ENV-default diff are set arithmetic over our Dockerfile and the upstream "
            "compare API. The bulk (full patches, whole before/after files, commit "
            "lists) is PREFETCHED to disk — read only what you need with Read/Grep. "
            "You need no network._"
        ),
        "",
    ] + sections
    write(out / "briefing.md", "\n".join(index))
    print(f"wrote {out}/briefing.md and prefetched upstream data", file=sys.stderr)


if __name__ == "__main__":
    main()
