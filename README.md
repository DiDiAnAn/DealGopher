# 蹲价 · DealGopher

> 盯住你的心理价，跌到了就叫醒你 —— Windows 桌面端商品价格监控工具

基于 PyQt5 + Selenium，自动定时抓取商品价格、绘制历史走势图、价格达标时弹窗与播放提示音，并把每次记录导出为可分析的 Excel 表格。

> 作者：**安安** · 版本 1.7.2
> 仅供个人使用，未经授权不得分享或商用（详见文末）

---

## 功能特性

- **5 个平台**：淘宝、天猫、京东、拼多多、抖音商城
- **2 种浏览器**：启动时自动检测本机 Chrome / Edge，由用户选择，运行中可随时切换
- **定时监控**：每日定时 或 间隔刷新（最小 15 分钟），最多 20 个商品
- **多级兜底解析**：CSS 选择器 → JS 提取 → 页面脚本 JSON → 文本标签，页面改版也能尽量抓取
- **原价解析**：四层策略（选择器 / 删除线样式 / 脚本字段 / 「优惠前」等标签）
- **走势图**：matplotlib 嵌入界面，含预期价基准线
- **价格提醒**：达标弹窗 + 自定义提示音循环播放
- **Excel 记录**：自动维护 `price_records.xlsx`，价格以**数值**存储、时间为**日期类型**，可直接求和/排序/做透视
- **手动导出**：支持另存为 `.xlsx` / `.csv`
- **状态识别**：区分 无货 / 已下架 / 规格失效 / 价格区间，买不到时不记录错误价格
- **规格锁定**：记住你要的版本·套餐·颜色，只抓那一款的价格，不再抓到"最低价起"
- **补货提醒**：从无货恢复可购买时静默弹窗（不播声音，避免反复补货时吵闹）
- **日志与会话**：登录状态持久化，崩溃有日志可查

## 支持的平台

| 平台 | 识别域名 | 解析器 |
|---|---|---|
| 淘宝 | `taobao.com`、`tb.cn` | 阿里系（与天猫共用） |
| 天猫 | `tmall.com` | 阿里系 |
| 京东 | `jd.com`、`jd.hk` | 京东 |
| 拼多多 | `pinduoduo.com`、`yangkeduo.com` | 通用解析 |
| 抖音商城 | `douyin.com`、`jinritemai.com` | 通用解析 |

平台定义集中在 `platforms.py`，新增平台需补：`platforms.py` 一项配置 + `browser_engine.py` 一个解析器。

## 环境要求

- Windows 10 / 11（64 位）
- Python 3.13（源码运行）
- 本机已安装 Chrome 或 Edge

## 源码运行

```bash
cd price_monitor
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

依赖见 `requirements.txt`（PyQt5 / selenium / matplotlib / openpyxl / pygame）。

## 打包

```bash
.venv\Scripts\python -m PyInstaller build.spec --noconfirm --clean
```

产物在 `dist/蹲价/`，双击 `蹲价.exe` 运行。

打包要点（`build.spec` 内已注释说明）：

- 使用 **onedir** 而非 onefile：启动快，且 `data/`、`logs/` 落在 exe 同级目录便于备份
- **不打包 `data/`**：其中是登录态（`chrome_profile`、`edge_profile`、`cookies`），不应随包分发
- `selenium-manager.exe` 由 `hook-selenium` 自动带入，打包后需确认 `_internal/selenium/webdriver/common/windows/selenium-manager.exe` 存在
- **不要**在 `excludes` 里排除 `unittest`：matplotlib 在导入期会用到它，排除后程序直接崩溃
- Edge 驱动由 `driver_manager.py` 自行下载（Selenium Manager 使用的旧域名 `msedgedriver.azureedge.net` 已失效）

## 目录结构

```
price_monitor/
├── main.py                 入口（高 DPI、字体、全局样式、浏览器选择）
├── main_window.py          主窗口：调度、串行抓取队列、记录表、导出台
├── browser_engine.py       浏览器管理、登录、各平台解析器
├── browser_detector.py     检测本机 Chrome / Edge 与版本
├── browser_dialog.py       浏览器选择对话框
├── driver_manager.py       驱动下载与缓存（Edge 必需）
├── models.py               数据模型与 JSON 持久化
├── platforms.py            平台配置（单一事实来源）
├── paths.py                源码/打包两种模式的路径解析
├── excel_export.py         Excel / CSV 导出
├── chart_widget.py         走势图控件
├── add_product_dialog.py   添加/编辑商品
├── alert_dialog.py         价格达标提醒
├── about_info.py           应用信息与版权声明
├── version_info.txt        exe 文件属性资源
├── build.spec              PyInstaller 配置
└── 使用说明.txt            面向最终用户的中文手册
```

## 运行时生成的文件

| 路径 | 说明 |
|---|---|
| `data/products.json` | 商品列表与历史价格（内存中只保留最近 500 条/商品） |
| `data/config.json` | 提示音路径、浏览器选择、是否清空历史等 |
| `data/cookies/*.json` | 各平台登录凭证（**明文**，请勿外传） |
| `data/{chrome,edge}_profile/` | 浏览器用户数据目录，登录态真正存放处 |
| `data/drivers/` | 浏览器驱动缓存 |
| `logs/app_YYYYMMDD.log` | 运行日志 |
| `price_records.xlsx` | 自动维护的价格记录表 |

配置项说明：

```jsonc
{
  "alert_sound_path": "D:\\music\\alert.mp3",  // 提示音，文件不存在会自动清空
  "browser": "chrome",                          // 上次选择的浏览器
  "remember_browser": true,                     // 是否跳过启动时的选择框
  "reset_history_on_startup": true              // 启动时清空历史（false=跨次累积）
}
```

## 下载与安装

- **普通用户**：到 [Releases](https://github.com/DiDiAnAn/DealGopher/releases/latest) 下载 `蹲价V1.7.2_win64.zip`，解压后双击 `蹲价V1.7.2.exe` 即可运行（`_internal` 文件夹必须与 exe 放在一起）。
- **开发者**：按下方「源码运行」从仓库拉取代码自行运行或打包。

> 运行环境：Windows 10/11 64 位 + 已安装 Chrome 或 Edge。首次运行需在程序内登录一次购物平台。

## 已知限制

- 拼多多、抖音商城的解析为**通用策略**，页面反爬较强，若遇到滑块验证或登录墙会抓取失败，需要先手动登录
- 商品价格来自页面渲染结果，若平台改为纯接口下发且页面不显示，可能抓不到
- 单个浏览器实例串行抓取，商品数量与刷新频率需要匹配（20 个商品建议间隔 ≥ 15 分钟）

## 版权与许可

作者：**安安**（GitHub: [DiDiAnAn](https://github.com/DiDiAnAn)）· 版权所有 (C) 2026 安安

本项目采用**个人使用许可协议**（[LICENSE](LICENSE)），要点：

- ✅ 允许：个人学习、自用、修改源码（修改版同样受此许可约束）
- ❌ 禁止（未经书面授权）：商业用途、二次分发 / 转售 / 捆绑发布、删除作者署名

抓取数据时请遵守目标平台的服务条款与相关法律法规，合理设置刷新频率、避免高频访问。软件按「现状」提供，因使用本软件产生的任何后果由使用者自行承担。

> 注意：本许可限制商业使用与再分发，因此**不属于** OSI 定义的开源许可证，属「源码公开（source-available）」。
