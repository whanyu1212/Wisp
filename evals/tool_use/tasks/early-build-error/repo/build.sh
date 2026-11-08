#!/usr/bin/env bash
# Prints its only error first, then thousands of progress lines, so a tool that
# keeps just the end of the output loses the error.
set -u
missing=0
if ! grep -Eq '^output_dir *= *[^ ]+' config.ini; then
  echo "ERROR: config.ini must set output_dir (for example: output_dir = dist)"
  missing=1
fi
for module in $(seq 1 3000); do
  echo "compiling module ${module} ... ok"
done
if [ "${missing}" = 1 ]; then
  echo "build failed: see the first error above"
  exit 1
fi
echo "build ok"
