#!/usr/bin/env bash
# Deploy (or redeploy) kami-keeper on a host as systemd unit `kami-keeper`,
# fleet profile, DRY RUN. Ship exactly one artifact (a `git archive` of the
# manifest SHA, carrying KEEPER_PIN.txt), install it, assert the pin,
# install the unit, restart.
#
# What it never does: flip `armed` in keeper.yaml, write a key, or overwrite
# an existing /srv/keeper/keeper.yaml (the config is the operator's; the
# template is installed only when none exists). The unit's KAMI_KEEPER_ARMED
# is made to AGREE with keeper.yaml, so a redeploy of an armed keeper stays
# armed.
#
# Usage: ./deploy/deploy.sh [sha] [host]
#   sha   the commit to ship (default: HEAD; must be committed)
#   host  the target host (default: $KEEPER_HOST; required)
# Environment:
#   KEEPER_HOST          target host when no second argument is given
#   KEEPER_SSH           remote shell, called as `$KEEPER_SSH <host> <command>` (default: ssh)
#   KEEPER_SCP           copy command, called as `$KEEPER_SCP <files> <host>:/tmp/` (default: scp)
#   KEEPER_PYTHON        interpreter for the host venv (default: python3.11, SPEC D4)
#   KEEPER_REQUIREMENTS  requirement files installed with --require-hashes
#                        (default: "requirements.txt"; add requirements-gcp.txt for the gcp-sm backend)
#   KEEPER_CONFIG        path inside the shipped tree installed as keeper.yaml when
#                        none exists (default: keeper/config.example.yaml)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
SHA=$(git -C "$ROOT" rev-parse "${1:-HEAD}")
SHORT=${SHA:0:12}
HOST="${2:-${KEEPER_HOST:-}}"
[ -n "$HOST" ] || { echo "refusing: no host (pass it as the second argument or set KEEPER_HOST)" >&2; exit 1; }
SSH="${KEEPER_SSH:-ssh}"; SCP="${KEEPER_SCP:-scp}"
PY="${KEEPER_PYTHON:-python3.11}"
REQS="${KEEPER_REQUIREMENTS:-requirements.txt}"
CONFIG="${KEEPER_CONFIG:-keeper/config.example.yaml}"
REQ_ARGS=""; for r in $REQS; do REQ_ARGS="$REQ_ARGS -r /srv/keeper/app.new/$r"; done
TGZ="/tmp/kami-keeper-$SHORT.tar.gz"

if [ -n "$(git -C "$ROOT" status --porcelain)" ] && [ "$SHA" = "$(git -C "$ROOT" rev-parse HEAD)" ]; then
  echo "refusing: working tree is dirty; commit first (the archive is the manifest SHA, nothing else)" >&2
  exit 1
fi
echo "$SHA" > "$ROOT/KEEPER_PIN.txt"   # gitignored; travels only inside the archive
git -C "$ROOT" archive --format=tar.gz --prefix=kami-keeper/ --add-file=KEEPER_PIN.txt -o "$TGZ" "$SHA"
rm -f "$ROOT/KEEPER_PIN.txt"

$SCP "$TGZ" "$HERE/kami-keeper.service" "$HOST":/tmp/
$SSH "$HOST" "set -e
sudo mkdir -p /srv/keeper
sudo rm -rf /srv/keeper/app.new && sudo mkdir -p /srv/keeper/app.new
sudo tar -xzf /tmp/$(basename "$TGZ") -C /srv/keeper/app.new --strip-components=1
pin=\$(cat /srv/keeper/app.new/KEEPER_PIN.txt)
[ \"\$pin\" = \"$SHA\" ] || { echo \"FATAL: KEEPER_PIN.txt \$pin != $SHA\"; exit 1; }
if [ ! -x /srv/keeper/venv/bin/python ]; then sudo $PY -m venv /srv/keeper/venv; fi
sudo /srv/keeper/venv/bin/pip install -q --upgrade pip >/dev/null
sudo /srv/keeper/venv/bin/pip install -q --require-hashes $REQ_ARGS
sudo rm -rf /srv/keeper/app.prev
[ -d /srv/keeper/app ] && sudo mv /srv/keeper/app /srv/keeper/app.prev || true
sudo mv /srv/keeper/app.new /srv/keeper/app
if [ ! -f /srv/keeper/keeper.yaml ]; then sudo install -m 600 /srv/keeper/app/$CONFIG /srv/keeper/keeper.yaml; echo 'installed $CONFIG as /srv/keeper/keeper.yaml (dry run)'; fi
armed=\$(sudo grep -E '^armed:' /srv/keeper/keeper.yaml | awk '{print \$2}')
[ \"\$armed\" = true ] || [ \"\$armed\" = false ] || { echo \"FATAL: /srv/keeper/keeper.yaml has no armed: true|false line\"; exit 1; }
echo \"keeper.yaml armed: \$armed (this script never changes it; the unit's KAMI_KEEPER_ARMED must agree)\"
unit_armed=\$(grep -E '^Environment=KAMI_KEEPER_ARMED=' /tmp/kami-keeper.service | cut -d= -f3)
if [ \"\$armed\" = true ] && [ \"\$unit_armed\" != true ]; then sed -i 's/^Environment=KAMI_KEEPER_ARMED=false/Environment=KAMI_KEEPER_ARMED=true/' /tmp/kami-keeper.service; echo 'unit set to KAMI_KEEPER_ARMED=true to match keeper.yaml'; fi
sudo install -m 644 /tmp/kami-keeper.service /etc/systemd/system/kami-keeper.service
sudo systemctl daemon-reload && sudo systemctl enable kami-keeper >/dev/null
sudo systemctl restart kami-keeper
rm -f /tmp/$(basename "$TGZ") /tmp/kami-keeper.service
sleep 3
cd /srv/keeper/app
sudo env PYTHONPATH=/srv/keeper/app /srv/keeper/venv/bin/python -m keeper status --socket /srv/keeper/keeper.sock | head -40
echo \"kami-keeper $SHORT installed at /srv/keeper/app; unit kami-keeper restarted (profile fleet, dry run)\""
rm -f "$TGZ"
