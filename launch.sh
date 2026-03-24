#!/bin/bash
set -a; source "/Users/intern/work/leg-tech/.env"; set +a
exec claude --effort max --chrome "$(<prompt.txt)"
