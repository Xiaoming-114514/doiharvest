# DoiHarvest — 文献检索·下载·OCR·筛选全流程自动化流水线

> 面向系统综述 / Meta 分析 / Scoping Review 的一站式文献处理工具。从一份 DOI 列表出发，自动完成 **下载 → 全文 OCR → LLM 智能筛选**，把原本需要数周的文献筛选工作压缩到几小时。

[![Python](https://img.shields.io/badge/Python-3.10~3.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 它能做什么

输入一份包含 DOI 的文献清单，DoiHarvest 自动完成四个阶段：

| 阶段 | 功能 | 核心技术 |
|------|------|----------|
| **Phase 1** 下载 | 从 Sci-Hub / 开放获取镜像批量下载 PDF | ALTCHA 验证码自动求解、OA 镜像回退 |
| **Phase 2** 机构下载 | 下载需要订阅的论文（需机构权限） | WebVPN / 校内 VPN 直连、Chrome TLS 指纹、反爬浏览器、半自动人工辅助 |
| **Phase 3** OCR | 把 PDF 转成结构化 Markdown 全文 | MinerU 高精度文档解析（支持公式/表格/版面还原） |
| **Phase 4** 筛选 | 按纳入/排除标准自动筛选文献 | DeepSeek 大模型 + 4 步决策树（或用户自定义标准） |

最终输出一份 Excel：每篇文献的 **下载状态 + OCR 结果 + 纳入/排除判定 + 理由**，全程可视化。

## 核心特性

- 🖥️ **Web 仪表盘**：浏览器里上传 CSV、启动各阶段、看实时进度、下载结果，无需命令行
- 📡 **实时进度推送**：WebSocket 推送每一篇的下载/OCR/筛选状态
- 🔁 **断点续传**：已下载的 PDF 自动跳过，中断后重启不重复劳动
- 🛡️ **多重反爬**：Sci-Hub 验证码求解、Chrome TLS 指纹（curl_cffi）、undetected-chromedriver 真实浏览器、半自动浏览器人工辅助
- 🧹 **数据自愈**：脏 DOI 自动识别、PDF 内容去重、损坏 PDF 检测隔离
- 📝 **元数据自动命名**：PDF 按 `作者_年份_期刊_标题.pdf` 统一命名（Crossref 元数据）
- 🤖 **LLM 决策可复现**：低温采样 + 结构化 JSON 输出 + 置信度不足自动标记"人工复核"

## 四阶段流程

```
┌─────────────┐   ┌──────────────────┐   ┌──────────────┐   ┌──────────────────┐
│  输入 DOI 列表 │ → │ Phase 1: Sci-Hub │ → │ Phase 2: 机构  │ → │  Phase 3: OCR    │
│  (CSV 文件)   │   │   批量下载 PDF    │   │   补充下载     │   │  PDF → Markdown  │
└─────────────┘   └──────────────────┘   └──────────────┘   └──────────────────┘
                                                                      │
                                              ┌───────────────────────┘
                                              ▼
                                     ┌──────────────────┐
                                     │ Phase 4: LLM 筛选 │
                                     │ Include/Exclude/  │
                                     │ Uncertain + 理由  │
                                     └──────────────────┘
```

## 快速开始

**三步上手：**

```bash
# 1. 一键安装（Windows 双击 install.bat，或运行）
python install.py

# 2. 启动
python start.py
# 浏览器自动打开 http://127.0.0.1:8765

# 3. 在网页里：上传 CSV → 填 DeepSeek Key → 点「启动」
```

> 详细的操作步骤、配置说明、各阶段使用方法和常见问题，见 **[使用指南](docs/USAGE_GUIDE.md)**。

## 依赖要求

| 组件 | 用途 | 是否必需 |
|------|------|----------|
| Python 3.10~3.12 | 运行环境 | ✅ 必需 |
| Google Chrome | Phase 2 反爬下载 | ⚠️ 建议（Phase 2 需要） |
| MinerU | Phase 3 OCR | ⚠️ 可选（Phase 3 需要） |
| DeepSeek API Key | Phase 4 LLM 筛选 | ⚠️ 可选（Phase 4 需要，免费申请） |
| 机构图书馆权限 | Phase 2 订阅文献下载 | 可选 |

一键安装脚本 `install.py` 会自动处理 Python 依赖、Playwright 浏览器、Chrome 检测，并可选择安装 MinerU。

## 项目结构

```
doiharvest_oa/
├── backend/
│   ├── main.py              # FastAPI 主应用 + WebSocket 实时推送
│   ├── pipeline.py          # 流水线编排：Phase 1 → Phase 2 自动切换
│   ├── phase1_scihub.py     # Phase 1：Sci-Hub 下载（ALTCHA 求解 + OA 回退）
│   ├── phase2_webvpn.py     # Phase 2：WebVPN / 校内VPN 多 provider 下载
│   ├── phase3_mineru.py     # Phase 3：MinerU OCR（含损坏 PDF 预扫描）
│   ├── phase4_screening.py  # Phase 4：DeepSeek LLM 筛选（4 步决策树 / 自定义）
│   └── metadata.py          # DOI → Crossref 元数据 + 文件名生成
├── frontend/
│   └── dashboard.html       # Web 仪表盘（单文件，无构建步骤）
├── data/                    # 放你的 CSV 文件
├── papers/                  # 下载的 PDF
├── output/                  # 结果 Excel / CSV
├── install.py               # 一键安装脚本
├── install.bat              # Windows 双击安装入口
├── start.py                 # 启动脚本
├── config.py                # 配置文件
├── requirements.txt         # Python 依赖
└── docs/
    └── USAGE_GUIDE.md       # 详细使用指南
```

## 技术架构

- **后端**：FastAPI + Uvicorn，WebSocket 实时推送进度
- **前端**：原生 HTML/JS 单文件仪表盘，无框架、无构建
- **下载**：requests / curl_cffi（Chrome 指纹）/ Playwright / undetected-chromedriver 多通道自动降级
- **OCR**：MinerU（独立子进程 CLI 调用）
- **LLM**：DeepSeek（OpenAI 兼容 API）

## 适用场景

- 系统综述 / Meta 分析的文献检索与筛选
- Scoping review 的大规模文献初筛
- 文献计量学的批量全文获取
- 任何"拿到 DOI 列表 → 需要全文 + 筛选"的场景

## 许可证

MIT License

## 免责声明

本工具仅供学术研究用途。请遵守你所在机构的数据库使用协议和版权规定，合理使用下载的文献。
