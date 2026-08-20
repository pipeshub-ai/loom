#!/bin/zsh
# Run a command inside the isolated hardening worktree with the shared venv,
# shadowing the editable install so imports resolve to THIS tree.
export PYTHONPATH=/Users/abhishek/opensource/loom-hardening/src
export PATH=/Users/abhishek/opensource/loom/.venv/bin:$PATH
cd /Users/abhishek/opensource/loom-hardening
exec "$@"
