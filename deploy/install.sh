#!/usr/bin/env bash
# Instala o actualiza vertice-linkedin-agent en el VPS (AlmaLinux 9, como root).
# Es idempotente: se puede ejecutar las veces que haga falta.
#
#   git clone https://github.com/miguelmartt/vertice-linkedin-agent.git /root/vertice-linkedin-agent
#   bash /root/vertice-linkedin-agent/deploy/install.sh
#
# No arranca el servicio hasta que /etc/vertice-linkedin-agent/env tenga los tokens.

set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true

readonly APP="vertice-linkedin-agent"
readonly SERVICE_USER="vla"
readonly SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly INSTALL_DIR="/opt/${APP}"
readonly STATE_DIR="/var/lib/${APP}"
readonly CONF_DIR="/etc/${APP}"
readonly ENV_FILE="${CONF_DIR}/env"
readonly UNIT_FILE="/etc/systemd/system/${APP}.service"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'AVISO: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "fallo en la línea ${LINENO}"' ERR

[[ ${EUID} -eq 0 ]] || die "ejecútalo como root"
[[ -f "${SRC_DIR}/vla/__main__.py" ]] || die "no encuentro el código en ${SRC_DIR}"
command -v systemctl >/dev/null || die "hace falta systemd"
command -v python3 >/dev/null || die "falta python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "hace falta Python 3.9 o superior"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    info "Creando el usuario de sistema ${SERVICE_USER}"
    useradd --system --home-dir "${STATE_DIR}" --shell /sbin/nologin "${SERVICE_USER}"
fi

info "Copiando el código a ${INSTALL_DIR}"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
staging="$(mktemp -d "${INSTALL_DIR}/.vla.XXXXXX")"
cp -a "${SRC_DIR}/vla" "${staging}/vla"
chown -R root:root "${staging}"
chmod -R u=rwX,go=rX "${staging}"
if [[ -d "${INSTALL_DIR}/vla" ]]; then
    mv "${INSTALL_DIR}/vla" "${staging}/vla.old"
fi
mv "${staging}/vla" "${INSTALL_DIR}/vla"
rm -r --one-file-system -- "${staging}"

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONF_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
    install -o root -g "${SERVICE_USER}" -m 0640 "${SRC_DIR}/deploy/env.example" "${ENV_FILE}"
    info "Creado ${ENV_FILE}: rellénalo antes de arrancar"
fi
# Los tokens: los lee root (systemd) y el grupo del servicio; nadie más.
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"

install -o root -g root -m 0644 "${SRC_DIR}/deploy/${APP}.service" "${UNIT_FILE}"
systemctl daemon-reload
systemctl enable "${APP}" >/dev/null

# Atajo para la línea de órdenes: `vla status`, `vla collect`… como el usuario del servicio.
# La configuración la lee Python como datos (--env-file), igual que systemd: no pasa por un shell.
cat > /usr/local/bin/vla <<EOF
#!/usr/bin/env bash
cd ${INSTALL_DIR} || exit 1
exec runuser -u ${SERVICE_USER} -- env -i HOME=${STATE_DIR} VLA_STATE_DIR=${STATE_DIR} DISABLE_AUTOUPDATER=1 \\
    PYTHONPATH=${INSTALL_DIR} PATH=${STATE_DIR}/.local/bin:/usr/local/bin:/usr/bin:/bin \\
    python3 -m vla --env-file ${ENV_FILE} "\$@"
EOF
chmod 0755 /usr/local/bin/vla

claude_bin="${STATE_DIR}/.local/bin/claude"
if [[ ! -x "${claude_bin}" ]]; then
    warn "Claude Code no está instalado para ${SERVICE_USER}. Instálalo con:"
    warn "  runuser -u ${SERVICE_USER} -- env HOME=${STATE_DIR} bash -c 'curl -fsSL https://claude.ai/install.sh | bash'"
fi

if grep -qE '^VLA_TELEGRAM_TOKEN=.+' "${ENV_FILE}" && grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=.+' "${ENV_FILE}"; then
    info "Comprobando la configuración"
    if /usr/local/bin/vla check; then
        systemctl restart "${APP}"
        info "Servicio en marcha: journalctl -u ${APP} -f"
    else
        warn "La comprobación ha fallado: corrige ${ENV_FILE} y vuelve a ejecutar este script"
    fi
else
    info "Servicio habilitado pero parado: rellena ${ENV_FILE} y vuelve a ejecutar este script"
fi
