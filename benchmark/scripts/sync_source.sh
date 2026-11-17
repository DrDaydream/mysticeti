#!/usr/bin/env bash
set -euo pipefail
source_dir=$1
revision=$2
repository=$3
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
mkdir -p -- "$(dirname -- "$source_dir")"
if [[ ! -e "$source_dir/.git" ]]; then
    if [[ -e "$source_dir" ]]; then
        if [[ ! -d "$source_dir" ]]; then
            echo 'remote.source_dir must name a separate source directory, not a file.' >&2
            exit 1
        fi
        for source_entry in "$source_dir"/* "$source_dir"/.[!.]* "$source_dir"/..?*; do
            if [[ -e "$source_entry" || -L "$source_entry" ]]; then
                echo 'remote.source_dir is nonempty and has no .git; set it to a new source directory in settings.json. Existing files were preserved.' >&2
                exit 1
            fi
        done
    fi
    git clone -- "$repository" "$source_dir"
fi
cd -- "$source_dir"
[[ "$(git rev-parse --is-inside-work-tree)" == true ]] || {
    echo 'remote.source_dir is not a valid Git worktree; existing files were preserved.' >&2; exit 1;
}
[[ "$(git remote get-url origin)" == "$repository" ]] || {
    echo 'Existing directory has a different origin; refusing to replace it' >&2; exit 1;
}
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || {
    echo 'Commit or save source edits before fab install' >&2; exit 1;
}
# Reuse the current checkout in place, including the controller adapter.
# Do not detach its branch or fetch when it already has the requested source.
if [[ "$(git rev-parse HEAD)" != "$revision" ]]; then
    git fetch --depth 1 origin "$revision"
    git checkout --detach "$revision"
fi
