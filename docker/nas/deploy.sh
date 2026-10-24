#!/usr/bin/env bash
#
# 蹲价 · NAS 一键部署脚本（DSM 7 + Container Manager）
#
# 用法：
#   sudo bash docker/nas/deploy.sh
#
# 设计原则：
#   · 只操作项目目录与 /volume3/docker/dunjia/data，绝不碰系统分区和其他卷
#   · 幂等：重复执行安全，不会删除已有数据
#   · 出错即停（set -e），并打印现场日志
#
set -euo pipefail

log()  { printf '\033[1;36m[部署]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${PM_DATA_DIR:-/volume3/docker/dunjia/data}"
COMPOSE_FILE="$APP_DIR/docker-compose.nas.yml"
CONTAINER="dunjia"

log "项目目录: $APP_DIR"
log "数据目录: $DATA_DIR"

# ---------- 0. 权限检查 ----------
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1
log "当前用户: $(id -un) (uid=$(id -u))  root=$IS_ROOT"

if [ "$IS_ROOT" -eq 0 ]; then
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<EOF
[失败] 当前用户既不是 root，也没有 docker 权限。

三条出路（任选其一）：
  1) 直接用 sudo 执行本脚本：
       sudo bash docker/nas/deploy.sh

  2) 把当前用户加入 docker 组（之后 docker 命令免 sudo）：
       sudo synogroup --add docker $(id -un)
       # 重新登录 SSH 生效

  3) 用 root 会话执行：
       sudo -i
       bash /volume3/docker/dunjia/docker/nas/deploy.sh

注意：即使加入了 docker 组，后面「把数据目录属主改成 UID 1000」这一步
仍然需要 root，因为容器内进程以 1000 运行、需要写权限。
EOF
    exit 1
  fi
  warn "以非 root 身份运行：将跳过 chown，若容器报 Permission denied 请用 sudo 重跑"
fi

# ---------- 1. 前置检查 ----------
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先在套件中心安装 Container Manager"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用（需要 v2 插件）"
docker info >/dev/null 2>&1 || die "Docker 守护进程未运行，请先启动 Container Manager"
[ -f "$COMPOSE_FILE" ] || die "缺少 $COMPOSE_FILE，请确认代码已完整上传"

MEM_MB="$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"
log "可用内存: ${MEM_MB} MB"
[ "$MEM_MB" -ge 1500 ] || warn "可用内存低于 1.5GB，Chromium 可能不稳定甚至 OOM"

FREE_KB="$(df -Pk "$APP_DIR" | awk 'NR==2{print $4}')"
log "项目所在分区剩余: $((FREE_KB / 1024 / 1024)) GB"
[ "$FREE_KB" -ge 5242880 ] || warn "剩余空间不足 5GB，建议先清理"

# ---------- 2. 数据目录与权限 ----------
mkdir -p "$DATA_DIR/cookies" "$DATA_DIR/logs"
if [ "$IS_ROOT" -eq 1 ]; then
  chown -R 1000:1000 "$DATA_DIR" 2>/dev/null || warn "chown 失败（容器可能无法写入数据目录）"
else
  warn "非 root：跳过 chown（数据目录需事先归属 UID 1000）"
fi
chmod 755 "$DATA_DIR"
log "数据目录已就绪（容器内以 UID 1000 运行）"

# ---------- 3. 数据自检 ----------
if [ ! -f "$DATA_DIR/products.json" ]; then
  warn "未找到 products.json —— 容器会启动，但不会抓取任何商品"
  warn "请把桌面版的 products.json 复制到: $DATA_DIR/products.json"
fi
if [ -z "$(ls -A "$DATA_DIR/cookies" 2>/dev/null || true)" ]; then
  warn "cookies 目录为空 —— 首次抓取很可能因未登录而失败"
  warn "请把桌面版的 data/cookies/*.json 复制到: $DATA_DIR/cookies/"
fi

# ---------- 4. 构建并启动 ----------
cd "$APP_DIR"
log "开始构建镜像（首次约 5~10 分钟，会下载 Chromium 约 300MB）…"
docker compose -f "$COMPOSE_FILE" up -d --build

# ---------- 5. 启动体检 ----------
log "等待容器启动…"
sleep 8

if ! docker ps --filter "name=$CONTAINER" --format '{{.Names}}' | grep -q "$CONTAINER"; then
  warn "容器没有在运行，最近日志如下："
  docker logs "$CONTAINER" --tail 60 2>&1 || true
  die "启动失败 —— 请把上面的日志发给开发者"
fi

log "容器状态: $(docker ps --filter "name=$CONTAINER" --format '{{.Status}}')"
log "镜像大小: $(docker images dunjia:1.6.0 --format '{{.Size}}' 2>/dev/null || echo '未知')"

echo
log "========== 最近日志 =========="
docker logs "$CONTAINER" --tail 30 2>&1 || true

echo
log "========== 部署完成 =========="
cat <<EOF
常用命令：
  docker logs -f $CONTAINER                      # 实时看日志
  docker stats $CONTAINER --no-stream            # 看内存/CPU
  docker compose -f $COMPOSE_FILE restart        # 重启
  docker compose -f $COMPOSE_FILE down           # 停止（数据不丢）
  docker compose -f $COMPOSE_FILE up -d --build  # 改代码后重建

数据目录（备份只需要它）：
  $DATA_DIR
EOF
