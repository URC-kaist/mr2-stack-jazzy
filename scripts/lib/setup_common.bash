#!/usr/bin/env bash

# Keep all three public entrypoints on the same implementation and interpreter.
mr2_setup_main() {
  local profile="$1"
  shift
  local argument
  for argument in "$@"; do
    if [[ "$argument" == --profile || "$argument" == --profile=* ]]; then
      printf 'Choose the matching setup_dev/rover/base entrypoint; --profile is reserved.\n' >&2
      return 2
    fi
  done
  local setup_dir
  setup_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../setup" && pwd)"
  if [[ ! -x /usr/bin/python3 ]]; then
    printf 'MR2 setup requires the Ubuntu system Python at /usr/bin/python3.\n' >&2
    return 1
  fi
  export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
  exec /usr/bin/python3 "$setup_dir/setup.py" --profile "$profile" "$@"
}
