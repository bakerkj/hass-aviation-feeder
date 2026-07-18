#!/usr/bin/env python3
"""Build-time guard: assert the two ENROLLED s6 boot surfaces match an explicit
allowlist, so a base bump can't quietly enroll something new without our approval.

The container inherits its base FROM docker-adsb-ultrafeeder, which enrolls its
OWN services and startup hooks on top of ours. We layer feeders on top but never
controlled that inherited set. This guard makes it explicit: it prunes the dead
upstream wrappers we reject, then FAILS THE BUILD if either boot surface drifts
from what we approved -- so a base bump that quietly adds a service or hook stops
the build and forces a human to classify it, instead of shipping unnoticed.

Scope/limitation: this polices ENROLLMENT (the `user` bundle + startup.d), not
transitive `dependencies.d` pulls. A unit named only in a kept unit's
`dependencies.d` still runs at boot but is invisible here -- e.g. the base's
`09-rtlsdr-biastee`, pulled via `readsb/dependencies.d`, is not enrolled yet runs
(a harmless no-op unless BIASTEE is set). Auditing dependencies.d is a possible
future extension.

Two surfaces are policed, because "what runs at boot" lives in two places:
  1. user/contents.d/ -- the enrolled s6 SERVICES (the `user` bundle).
  2. startup.d/       -- one-shot HOOKS the approved `startup` service iterates at
     boot. The aggregator auto-registration hooks (e.g. 52-adsbitalia-register)
     live HERE, not in user/contents.d -- a services-only guard would miss a base
     bump adding another. This is the ADSBItalia-class early warning the guard
     exists for.

Invoked (COPY'd to /tmp, run, rm'd) at the END of the Dockerfile, after
`COPY rootfs/ /` and every other s6 edit, so it validates the FINAL tree.

ON FAILURE (base bump): read the named unit, then either
  - KEEP it:   add its name to APPROVED_SERVICES / APPROVED_STARTUP below, or
  - REJECT it: add it to the DROP_SERVICES / DROP_STARTUP prune lists below.
"""

import os
import shutil
import sys
from pathlib import Path

S6 = "/etc/s6-overlay/s6-rc.d"
CONTENTS = f"{S6}/user/contents.d"
STARTUP = "/etc/s6-overlay/startup.d"

# --- Approved ENROLLED services (user/contents.d) --------------------------
# 15 base-provided units we consciously keep + our 29 add-on units (44 total).
BASE_SERVICES = {
    "adsbx-stats",
    "aggregator-urls",
    "autogain",
    "cleanup_globe_history",
    "collectd",
    "graphs1090",
    "graphs1090-writeback",
    "libseccomp2",
    "mlat-client",
    "mlathub",
    "nginx",
    "readsb",
    "startup",
    "tar1090",
    "tar1090-update",
}
OUR_SERVICES = {
    "01-adsbhubclient",
    "01-fr24feed",
    "01-opensky-network",
    "01-pfclient",
    "01-piaware",
    "01-show-rbfeeder-changelog",
    "02-rbfeeder",
    "02-show-architecture",
    "03-show-architecture",
    "adsbhubclient",
    "dump978",
    "fr24feed",
    "fr24uat-feed",
    "ha-mqtt",
    "opensky-feeder",
    "pfclient",
    "piaware",
    "planewatch-mlat",
    "pw-feeder",
    "radarvirtuel",
    "radarvirtuel-mlat",
    "rbfeeder",
    "sdrmap",
    "sdrmap-mlat",
    "sdrmap-stunnel",
    "uat-stats",
    "uk1090",
    "wait-dump978",
    "wait-readsb",
}
APPROVED_SERVICES = BASE_SERVICES | OUR_SERVICES

# --- Approved STARTUP hooks (startup.d) ------------------------------------
# 9 base hooks (the 11 shipped, minus the two we prune below). All are gated and
# self-noop unless their feature is enabled; 52-adsbitalia-register is the
# aggregator auto-register we now consciously approve.
APPROVED_STARTUP = {
    "01-print-container-version",
    "01-sanity-check",
    "04-tar1090-configure",
    "06-range-outline",
    "07-nginx-configure",
    "08-graphs1090-init",
    "50-store-uuid",
    "52-adsbitalia-register",
    "99-prometheus-conf",
}

# --- Rejected base units to prune ------------------------------------------
# telegraf:      InfluxDB/Prometheus exporter. The binary isn't even in this base
#                (the service self-disables); we publish to HA via ha-mqtt.
# timelapse1090: the program isn't shipped -- its startup hook wget-clones it from
#                GitHub at boot when enabled. Unaudited runtime code fetch we do
#                not want. Removed entirely (marker, unit dir, startup hook).
DROP_SERVICES = ["telegraf", "timelapse1090"]
DROP_STARTUP = ["10-telegraf-conf", "11-timelapse1090"]


def rm(path: str) -> None:
    """Delete a file, symlink, or directory tree; a no-op if already absent."""
    p = Path(path)
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def listdir(directory: str) -> set:
    try:
        return set(os.listdir(directory))
    except FileNotFoundError:
        return set()


def check(label: str, approved: set, directory: str) -> bool:
    """Return True if `directory` holds exactly `approved`; else report the drift."""
    actual = listdir(directory)
    unexpected = sorted(actual - approved)  # present in image, not approved
    missing = sorted(approved - actual)  # approved, absent from image
    if unexpected:
        print(f"ERROR: unapproved {label} present (base bump enrolled something new):")
        for name in unexpected:
            print(f"  + {name}")
        print(
            "  -> classify each: KEEP (add to the allowlist) or REJECT (add to the prune list) in assert-units.py."
        )
    if missing:
        print(
            f"ERROR: approved {label} missing (base bump renamed/removed it, or a COPY drifted):"
        )
        for name in missing:
            print(f"  - {name}")
        print("  -> confirm the change and update the allowlist in assert-units.py.")
    if not unexpected and not missing:
        print(f"ok: {label} matches the allowlist ({len(approved)} approved)")
        return True
    return False


def main() -> int:
    # Prune the rejected base units first, so the assertions below see the final tree.
    for name in DROP_SERVICES:
        rm(f"{CONTENTS}/{name}")
        rm(f"{S6}/{name}")
    for name in DROP_STARTUP:
        rm(f"{STARTUP}/{name}")

    ok = check("enrolled services (user/contents.d)", APPROVED_SERVICES, CONTENTS)
    ok = check("startup hooks (startup.d)", APPROVED_STARTUP, STARTUP) and ok

    if not ok:
        print(
            "assert-units.py: s6 boot surface drifted from the allowlist -- see above."
        )
        return 1
    print("assert-units.py: s6 boot surface matches the allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
