#!/bin/sh
set -u

PATH=/run/opensandbox-apt/bin:$PATH
export PATH

if [ "$#" -eq 0 ]; then
    exec /bin/sh
fi
exec "$@"
