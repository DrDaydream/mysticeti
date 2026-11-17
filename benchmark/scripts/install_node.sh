#!/usr/bin/env bash
set -euo pipefail
source_dir=$1
revision=$2
jobs=$3
repository=$4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case "$source_dir" in /home/ubuntu/experiments/*) ;; *) echo 'Invalid source directory' >&2; exit 2;; esac
[[ "$source_dir" != *'/../'* && "$source_dir" != *'/..' ]] || exit 2
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || exit 2
# Invalidate the previous record before any operation that may fail.
if [[ -d "$source_dir" ]]; then rm -f -- "$source_dir/.mysticeti-build.json"; fi
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 && "$(uname -m)" == x86_64 ]] || {
    echo 'This deployment requires Ubuntu 24.04 amd64' >&2; exit 1;
}
export PATH="$HOME/.cargo/bin:$PATH"
sudo -n apt-get -o DPkg::Lock::Timeout=300 update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y \
    build-essential pkg-config libssl-dev libssh2-1-dev zlib1g-dev \
    git curl ca-certificates tmux chrony python3 python3-yaml
if ! command -v rustup >/dev/null 2>&1; then
    installer=$(mktemp)
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --profile minimal --default-toolchain none
    rm -f -- "$installer"
    trap - EXIT
fi
rustup toolchain install 1.72.1 --profile minimal
sudo -n systemctl enable --now chrony
chronyc waitsync 60 0.01 0.0 1
bash "$script_dir/sync_source.sh" "$source_dir" "$revision" "$repository"
bash "$script_dir/build_node.sh" "$source_dir" "$revision" "$jobs" "$repository"
