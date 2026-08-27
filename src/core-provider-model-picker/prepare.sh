#!/bin/sh
# Prepare and optionally build the exact upstream revision used by this
# feature. The caller owns the work directory; this script never removes it.
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/upstream-pin.env"

PATCH_FILE=$SCRIPT_DIR/codex-provider-model-picker.patch
WORK_DIR=
BUILD=1

usage() {
    cat >&2 <<EOF
usage: $0 --work-dir PATH [--prepare-only | --build]

PATH must be a dedicated checkout directory. The default target is --build.
EOF
}

die() {
    echo "prepare.sh: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --work-dir)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            WORK_DIR=$2
            shift 2
            ;;
        --prepare-only)
            BUILD=0
            shift
            ;;
        --build)
            BUILD=1
            shift
            ;;
        -h|--help)
            usage >&2
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[ -n "$WORK_DIR" ] || { usage; exit 2; }
case "$WORK_DIR" in
    /*) ;;
    *) WORK_DIR=$(pwd -P)/$WORK_DIR ;;
esac

case "$UPSTREAM_COMMIT" in
    *[!0123456789abcdef]*|'') die "UPSTREAM_COMMIT must be lowercase hexadecimal" ;;
esac
[ "${#UPSTREAM_COMMIT}" -eq 40 ] || die "UPSTREAM_COMMIT must be a 40-character commit"
[ -f "$PATCH_FILE" ] || die "tracked patch is missing: $PATCH_FILE"

if [ -e "$WORK_DIR" ]; then
    [ -d "$WORK_DIR" ] || die "work directory exists but is not a directory: $WORK_DIR"
    TOP=$(git -C "$WORK_DIR" rev-parse --show-toplevel 2>/dev/null) || die "refusing non-git work directory: $WORK_DIR"
    TOP=$(CDPATH= cd "$TOP" && pwd -P)
    [ "$TOP" = "$WORK_DIR" ] || die "refusing git worktree whose root is not $WORK_DIR"
    REMOTE=$(git -C "$WORK_DIR" remote get-url origin 2>/dev/null) || die "refusing checkout without origin remote"
    [ "$REMOTE" = "$UPSTREAM_URL" ] || die "refusing checkout with origin $REMOTE (expected $UPSTREAM_URL)"
    [ -z "$(git -C "$WORK_DIR" status --porcelain --untracked-files=all)" ] || die "refusing dirty checkout: $WORK_DIR"
else
    git clone --no-tags "$UPSTREAM_URL" "$WORK_DIR"
fi

# Fetching the commit explicitly also works when a clean existing checkout
# currently points at another revision. No reset or broad cleanup is used.
git -C "$WORK_DIR" fetch --no-tags origin "$UPSTREAM_COMMIT"
git -C "$WORK_DIR" checkout --detach "$UPSTREAM_COMMIT"
[ "$(git -C "$WORK_DIR" rev-parse HEAD)" = "$UPSTREAM_COMMIT" ] || die "checkout did not reach pinned commit"
[ -z "$(git -C "$WORK_DIR" status --porcelain --untracked-files=all)" ] || die "checkout became dirty before patch application"

git -C "$WORK_DIR" apply --check "$PATCH_FILE" || die "tracked patch does not apply cleanly"
git -C "$WORK_DIR" apply "$PATCH_FILE"

if [ "$BUILD" -eq 1 ]; then
    # codex-cli contains the `codex app-server --stdio` command.
    RUST_VERSION=$(rustc --version 2>/dev/null | awk '{print $2}') || die "rustc is unavailable; install Rust $MINIMUM_RUST_VERSION or newer"
    [ -n "$RUST_VERSION" ] || die "could not determine the installed rustc version"
    LOWEST_RUST_VERSION=$(printf '%s\n%s\n' "$MINIMUM_RUST_VERSION" "$RUST_VERSION" | sort -V | head -n 1)
    [ "$LOWEST_RUST_VERSION" = "$MINIMUM_RUST_VERSION" ] || die "Rust $MINIMUM_RUST_VERSION or newer is required; found $RUST_VERSION"
    cargo build --release --manifest-path "$WORK_DIR/codex-rs/Cargo.toml" --package codex-cli
    BINARY=$WORK_DIR/codex-rs/target/release/codex
    [ -x "$BINARY" ] || die "cargo build completed without $BINARY"
    echo "built $BINARY"
else
    echo "prepared $WORK_DIR at $UPSTREAM_COMMIT with the provider-model picker patch"
fi
