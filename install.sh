#!/usr/bin/env bash
# =============================================================================
# install.sh - FetchLog Service Installer
# =============================================================================
# Usage:
#   sudo ./install.sh            # Full install: setup + install + start
#   sudo ./install.sh setup      # Create virtualenv and install dependencies
#   sudo ./install.sh install    # Create systemd service only (run setup first)
#   sudo ./install.sh start      # Start the service
#   sudo ./install.sh stop       # Stop the service
#   sudo ./install.sh status     # Show service status and recent logs
#   sudo ./install.sh uninstall  # Remove the service (data is preserved)
#
# Environment variable overrides (export before running 'install'):
#   FETCHLOG_UDP_PORT   UDP port for syslog     (default: 5514)
#   FETCHLOG_WEB_PORT   HTTP port for web UI    (default: 8080)
#   FETCHLOG_HOST       Bind address            (default: 0.0.0.0)
#   FETCHLOG_USER       Service user account    (default: fetchlog)
#   FETCHLOG_VENV       Virtual environment dir (default: <project>/.venv)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (resolved at script load time)
# ---------------------------------------------------------------------------
SERVICE_NAME="fetchlog"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ENTRY="${INSTALL_DIR}/app.py"
REQUIREMENTS="${INSTALL_DIR}/requirements.txt"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DATA_DIR="/var/lib/fetchlog"
DB_PATH="${DATA_DIR}/logs.db"

# Overridable via environment
UDP_PORT="${FETCHLOG_UDP_PORT:-5514}"
WEB_PORT="${FETCHLOG_WEB_PORT:-8080}"
HOST="${FETCHLOG_HOST:-0.0.0.0}"
SERVICE_USER="${FETCHLOG_USER:-fetchlog}"
VENV_DIR="${FETCHLOG_VENV:-${INSTALL_DIR}/.venv}"

# Re-running 'install' over an existing unit keeps its ports/host rather than
# silently resetting them to the defaults above. An explicit FETCHLOG_*
# environment variable still wins over the preserved value.
if [[ -f "${SERVICE_FILE}" ]]; then
    _kept=""
    if [[ -z "${FETCHLOG_UDP_PORT:-}" ]]; then
        _v="$(grep -oP '^Environment=FETCHLOG_UDP_PORT=\K[0-9]+' "${SERVICE_FILE}" 2>/dev/null || true)"
        [[ -n "${_v}" && "${_v}" != "${UDP_PORT}" ]] && { UDP_PORT="${_v}"; _kept+=" udp=${_v}"; }
    fi
    if [[ -z "${FETCHLOG_HOST:-}" ]]; then
        _v="$(grep -oP '^Environment=FETCHLOG_HOST=\K\S+' "${SERVICE_FILE}" 2>/dev/null || true)"
        [[ -n "${_v}" && "${_v}" != "${HOST}" ]] && { HOST="${_v}"; _kept+=" host=${_v}"; }
    fi
    if [[ -z "${FETCHLOG_WEB_PORT:-}" ]]; then
        _v="$(grep -oP -- '--bind [^: ]+:\K[0-9]+' "${SERVICE_FILE}" 2>/dev/null || true)"
        [[ -n "${_v}" && "${_v}" != "${WEB_PORT}" ]] && { WEB_PORT="${_v}"; _kept+=" web=${_v}"; }
    fi
    [[ -n "${_kept}" ]] && echo "[INFO]  Preserving settings from existing service file:${_kept}"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[INFO]  $*"; }
success() { echo "[OK]    $*"; }
warn()    { echo "[WARN]  $*" >&2; }
die()     { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This command must be run as root. Try: sudo $0 $*"
}

# ---------------------------------------------------------------------------
# setup: create virtual environment and install Python dependencies
# ---------------------------------------------------------------------------
cmd_setup() {
    require_root

    echo "========================================"
    echo "  FetchLog - Dependency Setup"
    echo "========================================"

    # Verify Python 3.10+
    info "Checking Python version..."
    local python_bin
    python_bin="$(command -v python3 2>/dev/null)" \
        || die "python3 not found. Install it with: apt-get install python3"

    local version
    version="$("${python_bin}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    local major="${version%%.*}"
    local minor="${version##*.}"

    if [[ "${major}" -lt 3 ]] || { [[ "${major}" -eq 3 ]] && [[ "${minor}" -lt 10 ]]; }; then
        die "Python 3.10+ is required, found ${version}. Please upgrade Python."
    fi
    success "Python ${version} at ${python_bin}"

    # Verify venv module is available
    "${python_bin}" -m venv --help >/dev/null 2>&1 \
        || die "Python venv module not found. Install with: apt-get install python3-venv"

    # Create virtual environment
    info "Creating virtual environment at ${VENV_DIR}..."
    "${python_bin}" -m venv "${VENV_DIR}"
    success "Virtual environment created."

    # Install dependencies into the venv
    info "Installing dependencies from ${REQUIREMENTS}..."
    "${VENV_DIR}/bin/pip" install -r "${REQUIREMENTS}" --quiet \
        || die "Dependency installation failed. Check the output above."
    success "Dependencies installed into ${VENV_DIR}."

    # Make app.py directly executable
    chmod +x "${APP_ENTRY}"
    success "Made ${APP_ENTRY} executable."

    echo ""
    success "Setup complete. Run 'sudo $0 install' to create the service."
}

# ---------------------------------------------------------------------------
# install: create systemd service
# ---------------------------------------------------------------------------
cmd_install() {
    require_root

    echo "========================================"
    echo "  FetchLog - Service Installation"
    echo "========================================"

    # Confirm virtual environment and dependencies are in place
    [[ -x "${VENV_DIR}/bin/python3" ]] \
        || die "Virtual environment not found at ${VENV_DIR}. Run 'sudo $0 setup' first."

    info "Verifying installed packages in ${VENV_DIR}..."
    "${VENV_DIR}/bin/python3" -c "import fastapi, uvicorn, websockets, jinja2, aiofiles, dateutil, gunicorn, werkzeug, multipart" \
        2>/dev/null \
        || die "Required packages are missing. Run 'sudo $0 setup' first."
    success "All packages present."

    # Create dedicated system user (no login, no home directory)
    if ! id "${SERVICE_USER}" &>/dev/null; then
        info "Creating system user '${SERVICE_USER}'..."
        useradd --system \
                --no-create-home \
                --shell /usr/sbin/nologin \
                --comment "FetchLog service account" \
                "${SERVICE_USER}"
        success "User '${SERVICE_USER}' created."
    else
        info "User '${SERVICE_USER}' already exists."
    fi

    # Create persistent data directory
    info "Creating data directory ${DATA_DIR}..."
    mkdir -p "${DATA_DIR}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
    chmod 750 "${DATA_DIR}"
    success "Data directory ready: ${DATA_DIR}"

    # Grant the service user read access to the application files and venv.
    # (Files remain owned by the deploying user so git pull keeps working.)
    chmod o+rX "${INSTALL_DIR}"
    find "${INSTALL_DIR}" \
        -not -path "${INSTALL_DIR}/.git" \
        -not -path "${INSTALL_DIR}/.git/*" \
        -exec chmod o+r {} \; 2>/dev/null || true

    # Ensure venv directories are traversable so the service user can
    # execute the Python interpreter and import installed packages.
    find "${VENV_DIR}" -type d -exec chmod o+rx {} \; 2>/dev/null || true
    find "${VENV_DIR}" -type f -name "python*" -exec chmod o+rx {} \; 2>/dev/null || true

    # Ensure every parent directory is traversable so the service user
    # can reach INSTALL_DIR (e.g. /home/user needs o+x when installed there).
    local parent
    parent="$(dirname "${INSTALL_DIR}")"
    while [[ "${parent}" != "/" ]]; do
        chmod o+x "${parent}" 2>/dev/null || true
        parent="$(dirname "${parent}")"
    done

    # Ports below 1024 are privileged; grant the unprivileged service user
    # the bind capability instead of running as root.
    local cap_line=""
    if (( UDP_PORT < 1024 )) || (( WEB_PORT < 1024 )); then
        cap_line="AmbientCapabilities=CAP_NET_BIND_SERVICE"
        info "Privileged port configured - adding CAP_NET_BIND_SERVICE to the unit."
    fi

    # Write the systemd unit file
    info "Writing ${SERVICE_FILE}..."
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=FetchLog Universal Syslog Server & Log Viewer
Documentation=https://github.com/ShowSysDan/FetchLog
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
${cap_line}

# WorkingDirectory is required: web_server.py uses relative paths
# for static files and Jinja2 templates ("static/" and "templates/")
WorkingDirectory=${INSTALL_DIR}

# FetchLog runs as a SINGLE process on purpose: one process binds the UDP
# syslog socket and fans out the live WebSocket feed from in-memory state.
# Gunicorn must therefore run exactly one worker (--workers 1). The UDP port
# and bind host are passed via the environment because the web app's startup
# lifespan (not a CLI flag) starts the UDP listener under gunicorn.
Environment=FETCHLOG_HOST=${HOST}
Environment=FETCHLOG_UDP_PORT=${UDP_PORT}
Environment=FETCHLOG_DB_CONFIG=${INSTALL_DIR}/db_config.json
# The service user has no real home; point HOME at the writable data dir so
# gunicorn can create its control socket (\$HOME/.gunicorn) without errors.
Environment=HOME=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/gunicorn web_server:app \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --workers 1 \\
    --bind ${HOST}:${WEB_PORT} \\
    --timeout 120

Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fetchlog

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR} ${INSTALL_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    success "Service file written."

    # Reload systemd and enable the service
    info "Reloading systemd daemon..."
    systemctl daemon-reload

    info "Enabling ${SERVICE_NAME} to start on boot..."
    systemctl enable "${SERVICE_NAME}"

    echo ""
    success "Service installed and enabled."
    info "Configuration baked into service:"
    info "  UDP syslog port : ${UDP_PORT}"
    info "  Web UI port     : ${WEB_PORT}"
    info "  Bind address    : ${HOST}"
    info "  Database        : ${INSTALL_DIR}/logs.db (default, see db_config.json)"
    info "  Running as user : ${SERVICE_USER}"
    info "  Virtual env     : ${VENV_DIR}"
    info "  Web server      : gunicorn (1 worker) + uvicorn worker class"
    info "  Auth            : configured in db_config.json ('auth' block; admins only)"
    echo ""
    info "To change these, edit ${SERVICE_FILE} then run: sudo systemctl daemon-reload"
    echo ""
    # Check if PostgreSQL is configured; remind user about psycopg2
    local db_cfg="${INSTALL_DIR}/db_config.json"
    if [[ -f "${db_cfg}" ]] && grep -q '"postgresql"' "${db_cfg}" 2>/dev/null; then
        if ! "${VENV_DIR}/bin/python3" -c "import psycopg2" 2>/dev/null; then
            warn "db_config.json uses PostgreSQL but psycopg2 is not installed."
            info "Install it now with: ${VENV_DIR}/bin/pip install psycopg2-binary"
        fi
    fi
    echo ""
    success "Run 'sudo $0 start' to launch FetchLog."
}

# ---------------------------------------------------------------------------
# start: start the service and show status
# ---------------------------------------------------------------------------
cmd_start() {
    require_root
    info "Starting ${SERVICE_NAME}..."
    systemctl start "${SERVICE_NAME}"
    sleep 1
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
    echo ""
    success "FetchLog is running."
    info "Web UI:  http://localhost:${WEB_PORT}"
    info "Logs:    journalctl -u ${SERVICE_NAME} -f"
}

# ---------------------------------------------------------------------------
# stop: stop the service
# ---------------------------------------------------------------------------
cmd_stop() {
    require_root
    info "Stopping ${SERVICE_NAME}..."
    systemctl stop "${SERVICE_NAME}"
    success "Service stopped."
}

# ---------------------------------------------------------------------------
# status: show service status
# ---------------------------------------------------------------------------
cmd_status() {
    systemctl status "${SERVICE_NAME}" --no-pager -l || true
}

# ---------------------------------------------------------------------------
# uninstall: remove service (preserve data)
# ---------------------------------------------------------------------------
cmd_uninstall() {
    require_root

    echo "========================================"
    echo "  FetchLog - Uninstall Service"
    echo "========================================"
    warn "This removes the systemd service."
    warn "Data at ${DATA_DIR} will NOT be deleted."
    echo ""
    read -rp "Continue? [y/N] " confirm
    [[ "${confirm}" =~ ^[Yy]$ ]] || { info "Uninstall cancelled."; exit 0; }

    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Stopping service..."
        systemctl stop "${SERVICE_NAME}"
    fi

    if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Disabling service..."
        systemctl disable "${SERVICE_NAME}"
    fi

    if [[ -f "${SERVICE_FILE}" ]]; then
        info "Removing ${SERVICE_FILE}..."
        rm "${SERVICE_FILE}"
    fi

    systemctl daemon-reload

    echo ""
    success "Service removed."
    info "Data preserved at: ${DATA_DIR}"
    info "To fully clean up:"
    info "  sudo rm -rf ${DATA_DIR}"
    info "  sudo rm -rf ${VENV_DIR}"
    info "  sudo userdel ${SERVICE_USER}"
}

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $(basename "$0") [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  (none)     Full install: setup + install + start"
    echo "  setup      Create virtualenv and install Python dependencies"
    echo "  install    Create and enable systemd service"
    echo "  start      Start the service"
    echo "  stop       Stop the service"
    echo "  status     Show service status"
    echo "  uninstall  Remove the service (preserves database)"
    echo ""
    echo "Environment overrides (set before running 'install'):"
    echo "  FETCHLOG_UDP_PORT   UDP syslog port           (default: 5514)"
    echo "  FETCHLOG_WEB_PORT   Web UI HTTP port          (default: 8080)"
    echo "  FETCHLOG_HOST       Bind address              (default: 0.0.0.0)"
    echo "  FETCHLOG_USER       Service user              (default: fetchlog)"
    echo "  FETCHLOG_VENV       Virtual environment dir   (default: <project>/.venv)"
}

main() {
    local command="${1:-all}"
    case "${command}" in
        setup)     cmd_setup ;;
        install)   cmd_install ;;
        start)     cmd_start ;;
        stop)      cmd_stop ;;
        status)    cmd_status ;;
        uninstall) cmd_uninstall ;;
        all)
            cmd_setup
            echo ""
            cmd_install
            echo ""
            cmd_start
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            warn "Unknown command: ${command}"
            echo ""
            usage
            exit 1
            ;;
    esac
}

main "$@"
