#!/usr/bin/env bash

# CAS Notes Linux initializer.
# Creates/updates the Conda environment and installs one systemd --user service.

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ADMIN_USERNAME=""
ADMIN_DISPLAY_NAME=""

log() { printf '[init] %s\n' "$*"; }
die() { printf '[init] 错误：%s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：scripts/init_linux.sh [--admin USERNAME] [--display-name NAME]

  --admin USERNAME       初始化后交互式创建首个管理员
  --display-name NAME    管理员显示名称（需与 --admin 一起使用）
  -h, --help             显示帮助
EOF
}

while (($#)); do
  case "$1" in
    --admin) (($# >= 2)) || die "--admin 缺少用户名"; ADMIN_USERNAME="$2"; shift 2 ;;
    --display-name) (($# >= 2)) || die "--display-name 缺少显示名称"; ADMIN_DISPLAY_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[[ -z "$ADMIN_DISPLAY_NAME" || -n "$ADMIN_USERNAME" ]] || die "--display-name 必须与 --admin 一起使用"
[[ "$(uname -s)" == "Linux" ]] || die "此脚本只支持 Linux"
[[ -f "$ENV_EXAMPLE" ]] || die "缺少 $ENV_EXAMPLE"
command -v conda >/dev/null 2>&1 || die "未找到 conda，请先安装 Miniconda 或 Anaconda"
command -v systemctl >/dev/null 2>&1 || die "未找到 systemctl；此部署方式需要 systemd"
command -v systemd-analyze >/dev/null 2>&1 || die "未找到 systemd-analyze"

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

[[ "$CONDA_ENV_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "CONDA_ENV_NAME 含有不支持的字符"
[[ "$PYTHON_VERSION" =~ ^[0-9]+([.][0-9]+){1,2}$ ]] || die "PYTHON_VERSION 格式不正确"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "SYSTEMD_SERVICE_NAME 含有不支持的字符"

if ! "$CONDA_EXE" run -n "$CONDA_ENV_NAME" python --version >/dev/null 2>&1; then
  log "创建 Conda 环境 $CONDA_ENV_NAME（Python $PYTHON_VERSION）"
  "$CONDA_EXE" create --yes --name "$CONDA_ENV_NAME" "python=$PYTHON_VERSION" pip
else
  log "复用 Conda 环境 $CONDA_ENV_NAME"
fi

log "安装/更新 Python 依赖"
"$CONDA_EXE" run -n "$CONDA_ENV_NAME" python -m pip install --requirement "$PROJECT_DIR/requirements.txt"

if [[ -z "$(read_env AUTH_SECRET_KEY)" ]]; then
  AUTH_SECRET_KEY="$("$CONDA_EXE" run --no-capture-output -n "$CONDA_ENV_NAME" python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i "s|^AUTH_SECRET_KEY=.*$|AUTH_SECRET_KEY=$AUTH_SECRET_KEY|" "$ENV_FILE"
  log "已生成独立的 AUTH_SECRET_KEY"
fi

log "初始化数据库结构与内置学习笔记"
(
  cd -- "$PROJECT_DIR"
  "$CONDA_EXE" run -n "$CONDA_ENV_NAME" python -c \
    'from app.database import initialize_database; initialize_database()'
)

mkdir -p -- "$SYSTEMD_USER_DIR"
UNIT_NAME="${SERVICE_NAME}.service"
cat >"$SYSTEMD_USER_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=CAS Notes integrated web service
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$CONDA_EXE run --no-capture-output -n $CONDA_ENV_NAME python -m app.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

log "校验并启用 systemd 用户服务"
systemd-analyze --user verify "$SYSTEMD_USER_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

if [[ -n "$ADMIN_USERNAME" ]]; then
  admin_args=(--username "$ADMIN_USERNAME")
  [[ -z "$ADMIN_DISPLAY_NAME" ]] || admin_args+=(--display-name "$ADMIN_DISPLAY_NAME")
  (
    cd -- "$PROJECT_DIR"
    "$CONDA_EXE" run --no-capture-output -n "$CONDA_ENV_NAME" \
      python -m app.bootstrap_admin "${admin_args[@]}"
  )
fi

log "初始化完成：$UNIT_NAME"
log "查看日志：journalctl --user -u $UNIT_NAME -f"
if command -v loginctl >/dev/null 2>&1; then
  linger="$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || true)"
  if [[ "$linger" != "yes" ]]; then
    log "提示：如需注销后继续运行，请由管理员执行：loginctl enable-linger $USER"
  fi
fi

