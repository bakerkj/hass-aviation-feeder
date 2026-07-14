# Migrating from a local add-on to the published image

Until release `0.0.4` the add-on was only installable as a **local** add-on:
Home Assistant built the image on each host. It is now published to ghcr, so HA
can **pull** it instead:

|         |                                                 |
| ------- | ----------------------------------------------- |
| amd64   | `ghcr.io/bakerkj/amd64-aviation_feeder:0.0.4`   |
| aarch64 | `ghcr.io/bakerkj/aarch64-aviation_feeder:0.0.4` |

Both are public. `config.json` carries
`image: ghcr.io/bakerkj/{arch}-aviation_feeder`, so a store install pulls and
never builds.

## The one thing that can hurt you: the slug changes

A local install is `local_aviation_feeder`. A store install gets a **different**
slug (`<repo-hash>_aviation_feeder`), and Home Assistant keys **both the options
and `/data`** off the slug. So the store install starts with **empty options and
an empty `/data`**.

`/data` holds the state we deliberately persist:

| path                        | what it is                                                      | losing it means                          |
| --------------------------- | --------------------------------------------------------------- | ---------------------------------------- |
| `collectd/`                 | graphs1090 RRDs                                                 | long-term signal/range/rate graphs reset |
| `globe_history/`            | readsb flight history + heatmap                                 | map history/replay resets                |
| `piaware/`, `station_*.txt` | feeder identity — **only if you have not pinned it in options** | see below                                |

### Pin your identity in options FIRST — then `/data` holds no identity at all

Two feeders generate an identity at runtime and keep it in `/data`. Both can be
pinned in **options** instead, and once they are, losing `/data` costs you
nothing but history:

| feeder       | option                     | how to find the current value                                                                                                         |
| ------------ | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| piaware      | `piaware_feeder_id`        | `docker exec addon_local_aviation_feeder cat /data/piaware/feeder_id`                                                                 |
| RadarVirtuel | `radarvirtuel_station_uid` | `docker exec addon_local_aviation_feeder cat /data/station_uid.txt` (the add-on also logs it on every start when the option is empty) |

RadarVirtuel keys the station off the **UID**: the feeder re-registers on every
start, and its entrypoint takes `RV_STATION_UID` **above** the persisted
`/data/station_uid.txt`. So pin the UID and the same station — same `station_id`
— comes back on a fresh volume. Leave it empty and a new `/data` silently
registers you as a **brand-new station**.

Do this **before** migrating, on the still-running local add-on, and confirm the
feeders still report the same site. Everything else — every UUID, key, sharecode
and MQTT credential — already lives in options.

> **`options.json` is NOT the way to copy your config.** It lives in `/data`,
> but Supervisor **regenerates it from its own store every time the add-on
> starts**. Copy it into the new slug's `/data` and it will simply be
> overwritten. It is a perfect thing to _read_ (it is exactly what the running
> add-on uses) and a useless thing to _write_. Config must go through the UI.

> **Uninstalling the local add-on DELETES its `/data`.** Stopping it does not.
> So: never uninstall until the new one is proven. The stopped local add-on is
> your rollback.

## Before you start: find your paths

The add-on's `/data` on the host (verified with `docker inspect`, not guessed):

```sh
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
  addon_local_aviation_feeder
# -> /mnt/data/supervisor/apps/data/local_aviation_feeder
```

Reading that path needs root. Reading it _through the container_ does not:

```sh
docker exec addon_local_aviation_feeder cat /data/options.json
```

## The sequence

Do the host **without an SDR first** (a failure there is cheap).

### 1. Capture the config

Add-on → **Configuration** → **Edit in YAML** → copy the whole block and save it
somewhere. (Or read `/data/options.json` via the `docker exec` above — same
values, and it is the exact set in use.)

### 2. Pin any identity that only exists in `/data`

This is what de-risks the whole migration: get the identities OUT of `/data` and
INTO options, while the old add-on is still running.

- **piaware**: if `piaware_feeder_id` is empty in options, read the generated
  one and set it explicitly:

  ```sh
  docker exec addon_local_aviation_feeder cat /data/piaware/feeder_id
  ```

  Paste it into `piaware_feeder_id`, restart, and confirm FlightAware still
  shows the same site.

- **RadarVirtuel**: read the UID and paste it into `radarvirtuel_station_uid`:

  ```sh
  docker exec addon_local_aviation_feeder cat /data/station_uid.txt
  ```

With both pinned, the slug change cannot take your identity — only your history.

### 3. Add the repository and install

Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add:

```
https://github.com/bakerkj/hass-aviation-feeder
```

Install **Aviation Feeder** from it. It will **pull** the image — if you see a
build log, the `image:` key is not resolving and something is wrong; stop.

Note the new slug from the browser URL: `/hassio/addon/<slug>/info`.

### 4. Cut over

They cannot both run: they would fight over the ingress port, the readsb ports
(30003/30005) and — on an SDR host — the dongle itself.

1. **Stop** the local add-on. Do **not** uninstall it.
2. Paste your saved options YAML into the new add-on's Configuration.
3. **Start** the new add-on and watch the log.

### 5. (Optional) Carry the persisted state across

Do this with **both add-ons stopped**, after the new one has been started once
so its data dir exists. Needs root on the host.

```sh
OLD=/mnt/data/supervisor/apps/data/local_aviation_feeder
NEW=/mnt/data/supervisor/apps/data/<new_slug>

# history (graphs + map). Identity does not need copying IF you pinned
# piaware_feeder_id and radarvirtuel_station_uid in options (step 2).
cp -a "$OLD"/collectd "$OLD"/globe_history "$NEW"/ 2>/dev/null
```

Do **not** copy `options.json` — Supervisor overwrites it (see above).

### 6. Verify, then remove the old one

Check in the new add-on's log:

- every enabled feeder connects (no auth failures)
- MLAT syncs where expected
- the map (ingress) loads

Only then uninstall the local add-on — that is the step that deletes its
`/data`, and with it your rollback.

## Afterwards

Updates now arrive like any other add-on: release-please cuts a version, CI
publishes the image, and HA offers an **Update** button. No more per-host
builds.
