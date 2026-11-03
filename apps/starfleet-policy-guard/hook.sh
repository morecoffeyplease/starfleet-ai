#!/bin/sh
# Normalize startup errors and unexpected interpreter exits to a blocking exit.
interpreter=$1
guard=$2
shift 2
"$interpreter" "$guard" "$@" hook
result=$?
if [ "$result" -ne 0 ]; then
  exit 2
fi
