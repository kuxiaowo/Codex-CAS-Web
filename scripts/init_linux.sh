#!/usr/bin/env bash

# CAS Gallery Linux initializer.
# Creates/updates the Conda environment and installs one systemd --user service.

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
INSTALL_SYSTEMD=1
START_SERVICE=1

log() { printf '[init] %s\n' "$*"; }
die() { printf '[init] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：scripts/init_linux.sh [--no-systemd] [--no-start]
  --no-systemd         仅初始化环境和数据库，不安装 systemd 服务
  --no-start           安装并启用服务，但暂不启动
  -h, --help             显示帮助
EOF
}

while (($#)); do
  case "$1" in
    --no-systemd) INSTALL_SYSTEMD=0; shift ;;
    --no-start) START_SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || die "此脚本只支持 Linux"
[[ -f "$ENV_EXAMPLE" ]] || die "缺少 $ENV_EXAMPLE"
command -v conda >/dev/null 2>&1 || die "未找到 conda，请先安装 Miniconda 或 Anaconda"
if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl；可使用 --no-systemd"
  command -v systemd-analyze >/dev/null 2>&1 || die "未找到 systemd-analyze"
fi

CONDA_EXE="$(command -v conda)"

if [[ ! -f "$ENV_FILE" ]]; then
  cp -- "$ENV_EXAMPLE" "$ENV_FILE"
  log "已从 .env.example 创建 .env"
else
  while IFS= read -r example_line || [[ -n "$example_line" ]]; do
    if [[ "$example_line" =~ ^([A-Z][A-Z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
        printf '%s\n' "$example_line" >>"$ENV_FILE"
        log "已向 .env 补充 $key"
      fi
    fi
  done <"$ENV_EXAMPLE"
fi
chmod 600 "$ENV_FILE"

read_env() {
  local key="$1"
  local fallback="${2-}"
  local value
  value="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  printf '%s' "${value:-$fallback}"
}

CONDA_ENV_NAME="$(read_env CONDA_ENV_NAME Codex-CAS-Web)"
PYTHON_VERSION="$(read_env PYTHON_VERSION 3.12)"
SERVICE_NAME="$(read_env SYSTEMD_SERVICE_NAME codex-cas-web)"
OIDC_CLIENT_SECRET_VALUE="$(read_env OIDC_CLIENT_SECRET)"

[[ "$CONDA_ENV_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "CONDA_ENV_NAME 含有不支持的字符"
[[ "$PYTHON_VERSION" =~ ^[0-9]+([.][0-9]+){1,2}$ ]] || die "PYTHON_VERSION 格式不正确"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "SYSTEMD_SERVICE_NAME 含有不支持的字符"
(( ${#OIDC_CLIENT_SECRET_VALUE} >= 16 )) || die "请先在 .env 填写 OIDC_CLIENT_SECRET"

if ! "$CONDA_EXE" run -n "$CONDA_ENV_NAME" python --version >/dev/null 2>&1; then
  log "创建 Conda 环境 $CONDA_ENV_NAME（Python $PYTHON_VERSION）"
  "$CONDA_EXE" create --yes --name "$CONDA_ENV_NAME" "python=$PYTHON_VERSION" pip
else
  log "复用 Conda 环境 $CONDA_ENV_NAME"
fi

PYTHON_BIN="$("$CONDA_EXE" run -n "$CONDA_ENV_NAME" python -c 'import sys; print(sys.executable)')"
PYTHON_BIN="${PYTHON_BIN//$'\r'/}"
[[ -x "$PYTHON_BIN" ]] || die "无法确定 Conda Python 路径"

log "安装/更新 Python 依赖"
"$PYTHON_BIN" -m pip install --requirement "$PROJECT_DIR/requirements.txt"

log "初始化数据库结构与资源目录"
(
  cd -- "$PROJECT_DIR"
  "$PYTHON_BIN" -c 'from app.database import initialize_database; initialize_database()'
)

if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  mkdir -p -- "$SYSTEMD_USER_DIR"
  UNIT_NAME="${SERVICE_NAME}.service"
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/render_systemd_unit.py" \
    --app-dir "$PROJECT_DIR" \
    --env-file "$ENV_FILE" \
    --python-bin "$PYTHON_BIN" \
    --output "$SYSTEMD_USER_DIR/$UNIT_NAME"

  log "校验并启用 systemd 用户服务"
  systemd-analyze --user verify "$SYSTEMD_USER_DIR/$UNIT_NAME"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT_NAME"
  if [[ "$START_SERVICE" == "1" ]]; then
    systemctl --user restart "$UNIT_NAME"
  fi

  log "初始化完成：$UNIT_NAME"
  log "查看日志：journalctl --user -u $UNIT_NAME -f"
  if command -v loginctl >/dev/null 2>&1; then
    linger="$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || true)"
    if [[ "$linger" != "yes" ]]; then
      log "提示：如需注销后继续运行，请由管理员执行：loginctl enable-linger $USER"
    fi
  fi
else
  log "初始化完成（未安装 systemd 服务）"
fi
