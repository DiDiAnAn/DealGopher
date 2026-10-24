# 蹲价 · NAS（DSM 7 + Container Manager）部署说明

适用环境：DSM 7.2 + Container Manager（Docker 24.x）、x86_64、内存 ≥ 4GB 可用。

> 目标机器已实测：DSM 7.2.1-69057 U5、Docker 24.0.2、compose v2.20.1、
> 镜像根目录 `/volume1/@docker`、可用内存约 4.6GB、`/volume3` 余 6.8T。

---

## 一、准备（在 NAS 上）

### 1. 建立目录

```bash
sudo mkdir -p /volume3/docker/dunjia
sudo chown -R 1000:1000 /volume3/docker/dunjia
```

### 2. 上传项目

把仓库里的这些文件放进 `/volume3/docker/dunjia/`（File Station 拖拽即可）：

```
docker-compose.nas.yml
.dockerignore
docker/nas/Dockerfile
paths.py  platforms.py  models.py  browser_detector.py  driver_manager.py
browser_engine.py  uploader.py  headless_runner.py  excel_export.py
requirements-headless.txt
```

### 3. 放入数据

```bash
/volume3/docker/dunjia/data/products.json      # 从桌面版复制
/volume3/docker/dunjia/data/cookies/*.json     # 从桌面版 data/cookies 复制（关键：登录态）
```

`products.json` 可以不复制，第一次用空目录启动也可以（程序会保持运行等你放文件）。

---

## 二、启动

```bash
cd /volume3/docker/dunjia
sudo docker compose -f docker-compose.nas.yml up -d --build
sudo docker logs -f dunjia
```

首次构建约 5~10 分钟（要装 Chromium 约 300MB）。看到下面这样就算成了：

```
蹲价 · 无界面运行器启动   数据目录=/data   headless=True
[启动] 浏览器: /usr/bin/chromium
[启动] 商品数: 8
[抓取] 开始: 【百亿】索尼PS5 Pro主机
[抓取] 成功: 【百亿】索尼PS5 Pro主机 当前¥11910.00 原价¥12999.00
```

---

## 三、验证清单

| 检查 | 命令 | 期望 |
|---|---|---|
| 容器在跑 | `sudo docker ps \| grep dunjia` | Up |
| 抓取成功 | `sudo docker logs dunjia \| grep 抓取` | 有「成功」 |
| 数据写回 | `ls -l /volume3/docker/dunjia/data/` | `products.json` 更新时间在变 |
| 登录态生效 | 日志里不应反复出现「可能是登录」 | 价格能抓到 |

---

## 四、登录态怎么来（重要）

当前版本**复用桌面版导出的 `data/cookies/*.json`**：

1. 在 Windows 上打开蹲价 → 「登录平台」→ 各平台登录一次
2. 复制 `data/cookies/` 整个目录到 NAS 的 `/volume3/docker/dunjia/data/cookies/`
3. 重启容器：`sudo docker compose -f docker-compose.nas.yml restart`

**如果淘宝返回的价格始终为空**（cookie 设备指纹校验不通过），说明这条路不通，
需要等下一版「容器内 noVNC 扫码登录」功能——那是目前唯一稳妥的容器登录方案。

---

## 五、常见问题

**Q1：构建时 apt 下载很慢**
Debian 源慢，可在 Dockerfile 里把 `deb.debian.org` 换成国内镜像：

```dockerfile
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources
```

**Q2：拉 `python:3.13-slim` 失败或极慢**
你已有的镜像源可以复用，把 Dockerfile 第一行改成：

```dockerfile
FROM docker.1ms.run/library/python:3.13-slim
```

**Q3：Chromium 起不来 / 页面崩溃**
- 确认 `shm_size: 1gb`（compose 里已配）
- 保留 `security_opt: seccomp=unconfined`
- 内存不足会 OOM：`sudo docker stats dunjia` 看内存占用

**Q4：容器反复重启**
`sudo docker logs dunjia --tail 50` 看最后几十行，通常是
`products.json` 格式错误（程序会停止保存以保护原文件）或缺 Chromium。

**Q5：想让容器内生成 Excel**
把 compose 里的 `PM_WRITE_XLSX` 改成 `"1"`，文件会出现在 `/data/price_records.xlsx`。

---

## 六、升级与维护

```bash
# 更新代码后重建
cd /volume3/docker/dunjia
sudo docker compose -f docker-compose.nas.yml up -d --build

# 查看资源占用
sudo docker stats dunjia

# 停止 / 删除（数据在 data 卷里，不会丢）
sudo docker compose -f docker-compose.nas.yml down
```

数据全部集中在 `data/` 目录，备份只需备份这个目录。
