#!/usr/bin/env bash
set -euo pipefail
source_dir=$1
revision=$2
jobs=$3
repository=$4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ "$revision" =~ ^[0-9a-f]{40}$ && "$jobs" =~ ^[1-9][0-9]*$ ]] || exit 2
cd -- "$source_dir"
rm -f -- .mysticeti-build.json .mysticeti-build.json.tmp
[[ "$(git rev-parse HEAD)" == "$revision" ]] || { echo 'Source revision mismatch' >&2; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo 'Tracked source files are modified' >&2; exit 1; }
[[ "$(git remote get-url origin)" == "$repository" ]] || { echo 'Repository mismatch' >&2; exit 1; }
git cat-file -e "$revision:mysticeti-core/src/adversary.rs" || { echo 'Source lacks adversary support' >&2; exit 1; }
export RUSTFLAGS='-C target-cpu=native'
export CARGO_BUILD_JOBS="$jobs"
echo "Building Mysticeti commit $revision with Rust 1.72.1 ($jobs parallel jobs)..."
cargo +1.72.1 build --locked --release -p mysticeti --bin mysticeti --target-dir target
test -x target/release/mysticeti
python3 "$script_dir/record_build.py" "$source_dir" "$revision" "$repository"
