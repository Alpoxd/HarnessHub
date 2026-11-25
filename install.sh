#!/bin/sh
set -eu

# HarnessHub one-line installer for Linux and macOS.
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/Alpoxd/HarnessHub/main/install.sh | sh
#
# Environment variables:
#   HARNESSHUB_REPO            GitHub owner/repo (default: Alpoxd/HarnessHub)
#   HARNESSHUB_REF             Branch, tag, or commit SHA (default: main)
#   HARNESSHUB_SOURCE          Explicit package source URL or local path
#   HARNESSHUB_PYTHON          Python version spec for uv (default: >=3.11)
#   HARNESSHUB_NO_MODIFY_PATH  Set to 1 to skip shell PATH updates
#   HARNESSHUB_UNINSTALL       Set to 1 to uninstall harnesshub

log_info() {
    printf '[harnesshub] %s\n' "$*"
}

log_warn() {
    printf '[harnesshub] warning: %s\n' "$*" >&2
}

log_err() {
    printf '[harnesshub] error: %s\n' "$*" >&2
}

die() {
    log_err "$*"
    exit 1
}

usage() {
    cat <<'EOF'
HarnessHub installer for Linux and macOS

Usage:
  curl -LsSf https://raw.githubusercontent.com/Alpoxd/HarnessHub/main/install.sh | sh
  sh install.sh [OPTIONS]

Options:
  --repo <owner/name>   GitHub repository (default: Alpoxd/HarnessHub)
  --ref <ref>           Branch, tag, or commit SHA (default: main)
  --source <src>        Explicit package URL, archive, wheel, or local path
  --python <spec>       Python version spec for uv (default: >=3.11)
  --no-modify-path      Do not modify shell profile PATH configuration
  --uninstall           Uninstall harnesshub (keeps local data and profiles)
  -h, --help            Show this help message
EOF
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    for _dir in \
        "${UV_INSTALL_DIR:-}" \
        "${XDG_BIN_HOME:-}" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/../bin" \
        "$HOME/.local/bin" \
        "$HOME/.cargo/bin"
    do
        if [ -n "$_dir" ] && [ -x "$_dir/uv" ]; then
            printf '%s\n' "$_dir/uv"
            return 0
        fi
    done

    return 1
}

download_to_file() {
    _url="$1"
    _dest="$2"

    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$_url" -o "$_dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$_dest" "$_url"
    else
        die "Neither curl nor wget is installed. Install curl, wget, or uv (https://docs.astral.sh/uv/) and try again."
    fi
}

install_uv() {
    _uv_url="${HARNESSHUB_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"
    _no_modify="$1"
    _tmp_script="$(mktemp "${TMPDIR:-/tmp}/harnesshub-uv-install.XXXXXX")"

    log_info "uv not found; installing uv from ${_uv_url}..."
    if ! download_to_file "$_uv_url" "$_tmp_script"; then
        rm -f "$_tmp_script"
        die "Failed to download uv installer from ${_uv_url}. Check network or HTTP_PROXY/HTTPS_PROXY settings."
    fi

    if [ "$_no_modify" = "1" ]; then
        if ! UV_NO_MODIFY_PATH=1 sh "$_tmp_script"; then
            rm -f "$_tmp_script"
            die "uv installation failed."
        fi
    else
        if ! sh "$_tmp_script"; then
            rm -f "$_tmp_script"
            die "uv installation failed."
        fi
    fi

    rm -f "$_tmp_script"
}

normalize_dir() {
    if [ -d "$1" ]; then
        (CDPATH='' cd -- "$1" && pwd)
    else
        printf '%s\n' "$1"
    fi
}

path_contains() {
    case ":${PATH:-}:" in
        *":$1:"*) return 0 ;;
        *) return 1 ;;
    esac
}

main() {
    REPO="${HARNESSHUB_REPO:-Alpoxd/HarnessHub}"
    REF="${HARNESSHUB_REF:-main}"
    SOURCE="${HARNESSHUB_SOURCE:-}"
    PYTHON_SPEC="${HARNESSHUB_PYTHON:->=3.11}"
    NO_MODIFY_PATH="${HARNESSHUB_NO_MODIFY_PATH:-0}"
    UNINSTALL="${HARNESSHUB_UNINSTALL:-0}"

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --repo)
                [ "$#" -ge 2 ] || die "--repo requires a value"
                REPO="$2"
                shift 2
                ;;
            --ref)
                [ "$#" -ge 2 ] || die "--ref requires a value"
                REF="$2"
                shift 2
                ;;
            --source)
                [ "$#" -ge 2 ] || die "--source requires a value"
                SOURCE="$2"
                shift 2
                ;;
            --python)
                [ "$#" -ge 2 ] || die "--python requires a value"
                PYTHON_SPEC="$2"
                shift 2
                ;;
            --no-modify-path)
                NO_MODIFY_PATH="1"
                shift
                ;;
            --uninstall)
                UNINSTALL="1"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown argument: $1 (use --help for usage)"
                ;;
        esac
    done

    if [ "$UNINSTALL" = "1" ]; then
        UV_BIN="$(find_uv || true)"
        if [ -z "$UV_BIN" ]; then
            die "uv was not found; nothing to uninstall."
        fi
        log_info "Uninstalling harnesshub..."
        "$UV_BIN" tool uninstall harnesshub
        log_info "Done. Local data and profiles in ~/.local/share/harnesshub were kept."
        exit 0
    fi

    UV_BIN="$(find_uv || true)"
    if [ -z "$UV_BIN" ]; then
        install_uv "$NO_MODIFY_PATH"
        UV_BIN="$(find_uv || true)"
        if [ -z "$UV_BIN" ]; then
            die "uv was installed, but the uv executable could not be found in PATH or standard bin directories."
        fi
    fi

    if [ -z "$SOURCE" ]; then
        SOURCE="https://github.com/${REPO}/archive/${REF}.tar.gz"
    fi

    log_info "Installing HarnessHub from ${SOURCE}..."
    "$UV_BIN" tool install --force --reinstall-package harnesshub --python "$PYTHON_SPEC" "$SOURCE"

    BIN_DIR="$(normalize_dir "$("$UV_BIN" tool dir --bin)")"
    if [ -x "${BIN_DIR}/hhub" ]; then
        BIN_PATH="${BIN_DIR}/hhub"
    else
        BIN_PATH="${BIN_DIR}/harnesshub"
    fi

    if ! path_contains "$BIN_DIR"; then
        if [ "$NO_MODIFY_PATH" != "1" ]; then
            log_info "Adding ${BIN_DIR} to shell PATH..."
            if ! "$UV_BIN" tool update-shell >/dev/null 2>&1; then
                log_warn "Could not update shell profile automatically. Add ${BIN_DIR} to your PATH manually."
            fi
        fi
    fi

    if [ -x "$BIN_PATH" ]; then
        VERSION="$("$BIN_PATH" --version 2>/dev/null || printf 'unknown')"
        log_info "Installed HarnessHub ${VERSION} -> ${BIN_PATH}"
    else
        die "Installation finished, but ${BIN_PATH} was not found."
    fi

    if ! path_contains "$BIN_DIR"; then
        printf '\n'
        printf 'To use hhub in this terminal session, run:\n'
        printf '  export PATH="%s:$PATH"\n' "$BIN_DIR"
        printf 'Or open a new terminal window.\n'
    fi

    printf '\n'
    printf 'Quick start:\n'
    printf '  hhub          # launch TUI (alias: harnesshub)\n'
    printf '  hhub doctor   # check environment and clients\n'
    printf '  hhub list     # list supported harnesses\n'
}

main "$@"
