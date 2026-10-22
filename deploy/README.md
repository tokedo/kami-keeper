# Deploying kami-keeper

Two profiles (SPEC P10). `lab` is the default; a lab bring-up should see
`meta.profile == "lab"` on its first `status`. The systemd deployment in
this directory runs `fleet`.

Dry run is the default (`armed: false`, env `KAMI_KEEPER_ARMED=false`):
full perception and decisions, every intent encoded and — when the chain
facts are configured — pre-checked by `eth_call` (a read), nothing
signed, ledger rows marked `dryRun: true` with the pre-check verdict on
each `tx` row. `armed: true` selects the live signer and requires
`rpcUrl`, `worldAddress` and `chainId`; without them the keeper refuses
to run books. **Nothing in this directory flips it.** Going armed is a
human edit of `/srv/keeper/keeper.yaml` AND the unit's
`KAMI_KEEPER_ARMED` line, followed by a restart.

## Layout on the host

| what | where |
|---|---|
| application (one `git archive` of the manifest SHA) | `/srv/keeper/app` (previous: `/srv/keeper/app.prev`) |
| virtualenv | `/srv/keeper/venv` |
| config | `/srv/keeper/keeper.yaml` (`KAMI_KEEPER_CONFIG`) |
| data: books, ledgers, runtime state | `/srv/keeper` (`dataDir`, `KAMI_KEEPER_DATA_DIR`) |
| control socket | `/srv/keeper/keeper.sock` (`KAMI_KEEPER_SOCKET`) |
| lens socket | `/srv/lens/kami-lens.sock` (`KAMI_KEEPER_LENS_SOCKET`) |
| daemon log | `/srv/keeper/daemon.log` |
| optional unit overrides | `/etc/default/kami-keeper` (`KAMI_KEEPER_*=…` lines) |

## systemd (`fleet`)

From a clean checkout, run `deploy/deploy.sh [sha] [host]`. The host
defaults to `$KEEPER_HOST`, and there is no built-in host. The script:

1. `git archive`s the manifest SHA with `KEEPER_PIN.txt` inside. It never
   ships a working tree and refuses a dirty one. It copies the one
   artifact and the unit to the host with `$KEEPER_SCP` (default `scp`)
   and runs the rest there with `$KEEPER_SSH` (default `ssh`).
2. Unpacks into `/srv/keeper/app.new` and asserts the pin. Creates
   `/srv/keeper/venv` with `$KEEPER_PYTHON` (default `python3.11`) if it
   is absent, then runs `pip install --require-hashes` over
   `$KEEPER_REQUIREMENTS` (default `requirements.txt`; add
   `requirements-gcp.txt` for the `gcp-sm` backend). A wheel missing from
   the hash set fails the install.
3. Swaps `app.new` into `/srv/keeper/app` and keeps the previous tree as
   `app.prev`.
4. Installs `$KEEPER_CONFIG` (a path inside the shipped tree, default
   `keeper/config.example.yaml`) as `/srv/keeper/keeper.yaml` ONLY if
   none exists. It refuses to continue unless that file has an
   `armed: true|false` line, and makes the unit's `KAMI_KEEPER_ARMED`
   agree with it.
5. Installs the unit, then runs `daemon-reload`, `enable` and `restart`,
   and prints the first `status`.

Rollback: on the host, move `/srv/keeper/app.prev` back to
`/srv/keeper/app` and `systemctl restart kami-keeper`.

The host needs `kami-lens.service`, and `Wants=` in the unit. A
`NOT_READY` lens is a wait: books declared in the config are retried
until the lens answers and the grant is loaded. It also needs
`python3.11` and the operator keys in the configured backend.

The checked-in `requirements*.txt` were compiled on 3.13. On a first
install on a 3.11 interpreter, re-resolve the pins there:
`python3.11 -m venv /tmp/pc && /tmp/pc/bin/pip install pip-tools && for f in requirements requirements-dev requirements-gcp; do /tmp/pc/bin/pip-compile --generate-hashes --strip-extras -o $f.txt $f.in; done`
(add `--pre` for requirements-dev).

## Keys

Labels only in the config, never a key value. The keeper reads
`<LABEL>_OPERATOR_KEY` at GRANT time and never caches it across
restarts. Under `fleet`, `grants: [<label>, …]` in the config grants at
boot.

- `env`: lines `<LABEL>_OPERATOR_KEY=<64 hex>` in `keysFile` (default
  `/srv/keys/.env`, `KAMI_KEEPER_KEYS_FILE`). An optional names manifest
  `<keysFile>.secrets.names` restricts which names may be granted.
- `gcp-sm`: a secret named `<LABEL>_OPERATOR_KEY` in the project
  `gcpProject` (`KAMI_KEEPER_GCP_PROJECT`), readable by the host's
  service account (`roles/secretmanager.secretAccessor`). Needs
  `requirements-gcp.txt`.
- `keychain`: macOS login Keychain items
  `<keychainServicePrefix><NAME>` (default prefix `kami-mcp/`,
  `KAMI_KEEPER_KEYCHAIN_SERVICE_PREFIX`). Development only, never on a
  server host.

## Books

Under `fleet`, books may be declared in `keeper.yaml` under `books:`
(`deploy/examples/example-auto.yaml`, `example-feed.yaml`,
`example-craft.yaml` are one-book fragments, every key at its default).
Declared books start at boot and are matched by identity (account +
type + kami / kami set + node / recipe set), so a restart never
duplicates them; `status.configBooks` shows declared / pending / started
/ failed. `deploy/examples/watchlist.example.json` is the shape of
`<dataDir>/watchlist.json`, which the keeper only reads (`dipCapMode`
`measured`).

Reading it on the host:

```
sudo /srv/keeper/venv/bin/python -m keeper status --socket /srv/keeper/keeper.sock
sudo /srv/keeper/venv/bin/python -m keeper books  --socket /srv/keeper/keeper.sock --account <label>
sudo /srv/keeper/venv/bin/python -m keeper ledger --socket /srv/keeper/keeper.sock --book <id> --kind tx
tail -f /srv/keeper/daemon.log
```

In a dry-run ledger:
- `tx` rows carry `status: dry_run`, `precheck: ok | reverted |
  rpc-unavailable`, `gasLimit` and `fn`, plus the node's `reason` on a
  revert;
- `decision` rows carry the HP, item and inventory the decision rests on;
- `tick` rows are written on change and every `tickHeartbeat` ticks;
- `degraded[]` is never silently empty when the lens or the RPC is not
  answering.

## Docker (`lab`)

1. Pick the manifest SHA and build the artifact:
   ```
   SHA=$(git rev-parse HEAD)
   echo "$SHA" > KEEPER_PIN.txt            # gitignored; travels only in the archive
   git archive --format=tar.gz --prefix=kami-keeper/ --add-file=KEEPER_PIN.txt -o kami-keeper-$SHA.tar.gz $SHA
   ```
2. Copy `kami-keeper-$SHA.tar.gz` and `Dockerfile` to an EMPTY build
   directory on the host (the Dockerfile refuses a context with more than
   one artifact) and build:
   ```
   docker build --build-arg KEEPER_PIN=$SHA -t kami-keeper:$SHA .
   ```
3. Start AFTER the lens reports LIVE (`NOT_READY` is a wait, never an
   error). `maxBooksPerAccount` is REQUIRED under lab: without it the
   process serves `describe` / `status` but refuses every `start`.
   ```
   docker run -d --name kami-keeper --network host \
     -v /srv/lens:/srv/lens -v /srv/keys:/srv/keys:ro -v /srv/keeper:/data \
     -e KAMI_KEEPER_MAX_BOOKS_PER_ACCOUNT=<n> \
     kami-keeper:$SHA
   ```
   Grants arrive over the socket only (`grant --account <label>`); the
   keeper reads `<LABEL>_OPERATOR_KEY` from `/srv/keys/.env` at grant
   time, never at boot. No roster file is mounted, and
   `books:` in config is refused under lab (start books over the socket).
4. First check: `python -m keeper status --socket /srv/keeper/keeper.sock`
   must answer `meta.profile == "lab"`.
5. Close-out: pull the whole `/srv/keeper` directory and sweep it for key
   material. The keeper never writes one; the sweep is the proof.

## Both

- The control CLI is `python -m keeper <cmd> --socket <path>`. Every
  argument is declared in `describe`, and an undeclared one is `BAD_ARGS`.
- `stop` (immediate) asks a burst in flight to stop offering further
  chunks, and `stop --afterBurst` lets it finish. Both return the
  quiescence proof (live: pending nonce == latest nonce on the chain).
