# rig_guard.sh: a run script takes the rig (rig.py) by itself. Its first command:
#
#   . ~/sleap-bench/rig_guard.sh "$@"
#
# Unless the script already runs inside the rig's current job (started by rig.py run, or by a
# script that took the rig), this re-runs it under rig.py: it waits for the rig, runs holding it,
# and leaves <tag>.rig.log beside the script, ending RIG_CLEAN or RIG_CONTAMINATED.
#   RIG_OWNER  who runs it (the session's name); "unnamed" otherwise
#   RIG_TAG    the run's name; otherwise the script's first argument, or the script's name
#   RIG_NOWAIT=1  exit 75 at once if the rig is busy, instead of waiting
if [ -z "${RIG_JOB:-}" ] || ! grep -qs "job=$RIG_JOB " "${RIG_ROOT:-$HOME/sleap-bench}/RIG_BUSY"; then
	_rig_script=$(readlink -f "$0")
	_rig_tag=${RIG_TAG:-${1:-$(basename "$_rig_script" .sh)}}
	exec /usr/bin/python3 "${RIG_ROOT:-$HOME/sleap-bench}/rig.py" run --log-dir "$(dirname "$_rig_script")" \
		"${RIG_OWNER:-unnamed}" "${_rig_tag//\//_}" -- bash "$_rig_script" "$@"
fi
