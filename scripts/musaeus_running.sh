#!/usr/bin/env bash
# Is a MUSAEUS pipeline actually running?
#
# Do NOT use `pgrep -f "python3 -m musaeus"`. A shell running that very command
# has the pattern in its own /proc/<pid>/cmdline, so pgrep -f matches the
# wrapper and reports RUNNING when nothing is. This has produced a false
# "pipeline: RUNNING" report more than once.
#
# Match the process NAME (python3) and require "-m musaeus" in its arguments,
# which a bash wrapper can never satisfy.
#
# Exit 0 = something is running (PIDs on stdout). Exit 1 = nothing.
ps -eo pid=,comm=,args= | awk '
    $2 ~ /^python3?$/ && $0 ~ /-m[ ]+musaeus/ { print $1; found = 1 }
    END { exit found ? 0 : 1 }
'
