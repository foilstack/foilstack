# Backups

The database is the one thing here that cannot be rebuilt. Containers, model
weights and the card catalogue are all reproducible; a seller's reviewed matches
and inventory are hours of somebody's work.

## The sidecar

Backups are a service, not a cron job you have to remember:

```bash
docker compose up -d          # the `backup` sidecar starts with everything else
```

It dumps whenever the newest dump is older than `FOILSTACK_BACKUP_INTERVAL`
(default 24h), verifies the gzip, rejects a suspiciously small file, keeps the
last `FOILSTACK_BACKUP_KEEP` (default 14) and writes `BACKUP_FAILING` into the
backup directory when a run produces nothing usable. **Checking for that file is
the whole of your monitoring.**

It also mirrors your scans into the same directory. They are write-once, so each
file is copied exactly once and later runs cost milliseconds — where dated
tarballs would keep a fourth identical copy of bytes that cannot change.
Deletions do not propagate: discarding a scan should not also remove it from the
backup.

Dumps land in `FOILSTACK_BACKUP_DIR` on the host — a bind mount, not a named
volume, because a backup that `docker compose down -v` can destroy is not a
backup.

## Getting it off the machine

None of the above survives the disk dying, because the dumps and the scan mirror
sit on the same disk as the database they protect. That is what the `offsite`
profile is for:

```bash
rclone config                                 # once, on the host
FOILSTACK_OFFSITE_REMOTE=r2:foilstack-backups \
  docker compose --profile offsite up -d
```

Any rclone remote works, and object storage is the cheap answer at this size. It
runs `rclone copy`, never `sync` — sync makes the remote match local, so a disk
that has just wiped itself would replicate that faithfully to the one copy that
survived. `OFFSITE_FAILING` appears in the backup directory when a run fails,
alongside `BACKUP_FAILING`.

## Restoring

```bash
scripts/restore.sh ~/backups/foilstack/foilstack-latest.sql.gz
```

`restore.sh` puts the scans back before it starts the web service, taking them
from a `scans/` directory beside the dump unless you name another. It warns
loudly if there is no mirror to restore from, because a database restored
without them is an inventory whose every image is a broken link.
