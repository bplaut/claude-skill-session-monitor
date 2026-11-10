#!/bin/bash
# The session monitor's cluster poller. Read-only.
#
# Every minute it lists your Volcano jobs on each cluster and stores the
# listing in <state-dir>/jobs.json; when a job finishes, fails or disappears it
# appends a line to <state-dir>/events.log, which wakes the monitor.
#
# Run it in a terminal tab, not from a Claude session: the cluster sign-in can
# only refresh outside the sandbox.
#
# usage: poller.sh <state-dir> [context ...]
set -euo pipefail

STATE="${1:?usage: poller.sh <state-dir> [context ...]}"
shift
: "${FLAMINGO_USERNAME:?FLAMINGO_USERNAME is not set; the poller never guesses a username}"
if [ "$#" -gt 0 ]; then
  CONTEXTS=("$@")
else
  CONTEXTS=(h100-prime-002 h100-prime-001 h200-vp-001)
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$STATE"

# One private kubeconfig per cluster, so a context switch elsewhere cannot
# redirect a poll. The directory is mode 700: the files hold credentials.
KDIR=$(mktemp -d "${TMPDIR:-/tmp}/monitor-kube.XXXXXX")
cleanup() {
  for f in "$KDIR"/*; do
    [ -e "$f" ] && rm "$f"
  done
  rmdir "$KDIR"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

for ctx in "${CONTEXTS[@]}"; do
  if ! kubectl config view --raw --flatten --minify --context "$ctx" > "$KDIR/$ctx"; then
    echo "poller: context $ctx is not in your kubeconfig; name the contexts to poll after the state directory" >&2
    exit 1
  fi
done

echo "poller: listing jobs for $FLAMINGO_USERNAME on ${CONTEXTS[*]} every 60s; Ctrl-C to stop"
while true; do
  line="$(date +%H:%M:%S)"
  for ctx in "${CONTEXTS[@]}"; do
    if out=$(kubectl --kubeconfig "$KDIR/$ctx" -n u-eval-awareness --request-timeout=20s get vcjob -l "username=$FLAMINGO_USERNAME" -o json 2> "$KDIR/err"); then
      if printf '%s' "$out" | /usr/bin/python3 "$HERE/monitor_lib.py" record-jobs "$STATE" "$ctx"; then
        line="$line  $ctx ok"
      else
        line="$line  $ctx UNREADABLE OUTPUT"
      fi
    else
      line="$line  $ctx READ FAILED ($(head -c 120 "$KDIR/err" | tr '\n' ' '))"
    fi
  done
  echo "$line"
  sleep 60
done
