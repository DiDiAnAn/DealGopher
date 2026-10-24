# 蹲价 · 容器镜像（NAS / Linux 服务器版）
#
# 说明：
# - 系统自带发行版的 chromium 与 chromedriver（Debian 包，版本天然匹配）
# - 预装思源黑体，保证商品名里的中文能正常渲染（淘宝页面缺字体会显示方块）
# - 不安装 PyQt5 / matplotlib / pygame：无界面运行不需要
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PM_DATA_DIR=/data \
    PM_CHROME_BINARY=/usr/bin/chromium \
    PM_CHROME_DRIVER=/usr/bin/chromedriver

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-noto-cjk \
        ca-certificates \
        tzdata \
        procps \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-headless.txt /app/
RUN pip install --no-cache-dir -r requirements-headless.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

# 只拷贝运行期需要的模块，避免把桌面版 GUI 代码和资源打进镜像
COPY paths.py platforms.py models.py browser_detector.py driver_manager.py \
     browser_engine.py uploader.py headless_runner.py excel_export.py /app/

RUN useradd -m -u 1000 runner || true
RUN mkdir -p /data && chown -R 1000:1000 /data /app

USER runner

VOLUME ["/data"]

CMD ["python", "-u", "headless_runner.py"]
