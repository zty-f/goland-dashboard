#!/bin/bash
set -u
cd "$(dirname "$0")"
exec python3 app.py --open "$@"
