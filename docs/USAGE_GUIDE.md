# DoiHarvest 使用指南

本指南面向**没有太多计算机经验**的用户，一步一步带你完成从安装到拿到筛选结果的全流程。

---

## 目录

1. [准备工作](#1-准备工作)
2. [一键安装](#2-一键安装)
3. [准备你的文献清单](#3-准备你的文献清单)
4. [启动程序](#4-启动程序)
5. [Phase 1：批量下载 PDF](#5-phase-1批量下载-pdf)
6. [Phase 2：机构文献下载（可选）](#6-phase-2机构文献下载可选)
7. [Phase 3：OCR 转全文（可选）](#7-phase-3ocr-转全文可选)
8. [Phase 4：LLM 智能筛选（可选）](#8-phase-4llm-智能筛选可选)
9. [查看与导出结果](#9-查看与导出结果)
10. [常见问题 FAQ](#10-常见问题-faq)

---

## 1. 准备工作

在开始之前，请先准备好以下内容：

| 需要的东西 | 说明 | 什么时候用 |
|-----------|------|-----------|
| **电脑** | Windows 10/11，8GB 以上内存 | 全程 |
| **Python 3.10~3.12** | 见下方安装说明 | 全程 |
| **Google Chrome** | 浏览器 | Phase 2（可选） |
| **DeepSeek 账号** | 免费申请，用于 AI 筛选 | Phase 4（可选） |
| **你的文献清单** | 一份含 DOI 的 CSV/Excel | 全程 |

### 安装 Python

1. 打开 https://www.python.org/downloads/
2. 下载 **Python 3.11 或 3.12** 的 Windows 安装包
3. 双击安装，**务必勾选底部的 `Add Python to PATH`**（这一步很关键！）
4. 一路下一步直到完成

> ⚠️ 不要装 Python 3.13，Phase 3（MinerU）在 Windows 上还不支持它。

### 申请 DeepSeek API Key（免费，Phase 4 用）

1. 打开 https://platform.deepseek.com/
2. 注册并登录
3. 进入「API Keys」页面，创建一个 Key，复制保存好
4. 这个 Key 会用在 Phase 4 的 AI 筛选中（新用户通常有免费额度）

---

## 2. 一键安装

拿到本项目代码后（下载解压或 `git clone`），进入项目目录：

### Windows 用户（推荐）

**直接双击 `install.bat`**，脚本会自动：

1. 检查 Python 环境
2. 创建虚拟环境
3. 安装所有 Python 依赖
4. 安装 Playwright 浏览器（约 150MB）
5. 检查 Chrome 是否安装
6. 询问你是否安装 MinerU（Phase 3 才需要，约 20GB 磁盘）

### 命令行方式

```bash
python install.py              # 交互式安装
python install.py --mineru     # 额外自动安装 MinerU
python install.py --no-mineru  # 跳过 MinerU
```

> 安装过程中会询问「是否安装 MinerU」。如果暂时不做 Phase 3（OCR），可以先选 N，以后需要时再运行 `python install.py --mineru`。

---

## 3. 准备你的文献清单

把你的文献清单整理成一个 **CSV 文件**（Excel 可以「另存为 → CSV UTF-8」）。

**最小要求：至少有一列叫 `DOI`**（大小写不限，`doi`、`Doi` 都可以）。

推荐的列结构：

| DOI | Title |
|-----|-------|
| 10.1038/s41586-021-03819-2 | Highly accurate protein structure prediction |
| 10.1016/j.jpsychores.2023.111172 | Persistent physical symptoms after COVID-19 |
| ... | ... |

其他可选的列（有会更好，没有也能自动补全）：
- `Title`（标题，用于生成更准确的文件名）
- `Authors` / `Year` / `Journal`（元数据）

> 💡 提示：如果你从 EndNote / Zotero / Rayyan 导出文献，直接导出成 CSV 即可，通常都带 DOI 列。

把 CSV 文件放到项目的 `data/` 目录里。

---

## 4. 启动程序

- **Windows**：双击 `start.bat`
- **命令行**：

```bash
python start.py
```

程序会自动打开浏览器，进入 **http://127.0.0.1:8765** 的仪表盘。

你会看到四个阶段卡片：**Phase 1（下载）→ Phase 2（机构下载）→ Phase 3（OCR）→ Phase 4（筛选）**，以及一个日志区域。

---

## 5. Phase 1：批量下载 PDF

这是第一步，自动从 Sci-Hub 和开放获取镜像下载 PDF。

1. 在仪表盘点击「上传 CSV」，选择你的文献清单文件
2. 点击「**Start Phase 1**」
3. 等待即可——日志区会实时显示每一篇的下载进度

**下载结果分类**：

| 状态 | 含义 |
|------|------|
| `downloaded` | 下载成功 ✅ |
| `download_failed` | Sci-Hub 没找到，进入 Phase 2 处理 |
| `no_doi` | 清单里这篇没有 DOI，需手动补充 |

> Phase 1 请求间隔约 10 秒，几百篇文献可能需要几小时，可以放着慢慢跑。中断后重新点击「Start Phase 1」，已下载的会自动跳过（断点续传）。

---

## 6. Phase 2：机构文献下载（可选）

Phase 1 失败的论文，需要借助**机构图书馆权限**下载。这一步需要你的学校/单位购买了相应期刊数据库，并提供了 VPN 访问方式。

> ⚠️ 这一步门槛较高，如果你没有机构权限，可以跳过 Phase 2，直接用「手动补充」的方式补 PDF（见下文）。

Phase 2 支持多种下载方式，其中对普通用户最有用的是**半自动模式**：

1. 程序弹出浏览器窗口
2. 你手动登录数据库、通过验证码、点「Download PDF」
3. 程序自动捕获下载的 PDF 并归档

其余方式（WebVPN 直连、反爬浏览器自动下载）需要先在 `config.py` 里配置好机构 VPN 信息。

**手动补充 PDF**（没有机构权限时推荐）：

1. 在浏览器里自己登录数据库下载 PDF
2. 把 PDF 文件拖到 `papers/` 目录（文件名建议用 `作者_年份_标题.pdf` 格式）
3. 运行 `python import_manual_pdfs.py` 自动归档并更新结果

---

## 7. Phase 3：OCR 转全文（可选）

把下载的 PDF 批量转成 Markdown 全文，供 Phase 4 的 AI 阅读。

**前提**：已安装 MinerU（安装时选 Y，或运行 `python install.py --mineru`）。

1. 点击「**Start Phase 3**」
2. 程序自动逐篇调用 MinerU 做 OCR

> - 首次运行 MinerU 会自动下载约 2GB 的模型，请保持网络畅通、耐心等待。
> - 有 NVIDIA 显卡会快很多；纯 CPU 也能跑，只是慢一些。
> - 程序会自动检测并隔离损坏的 PDF，不会被单个坏文件卡住。

---

## 8. Phase 4：LLM 智能筛选（可选）

用 DeepSeek 大模型，按你的纳入/排除标准自动筛选全文。

1. 在 Phase 4 卡片填入你的 **DeepSeek API Key**
2. （可选）填入你的**纳入标准**和**排除标准**
3. 点击「**Start Phase 4**」

**两种筛选模式**：

| 情况 | 行为 |
|------|------|
| **不填纳入/排除标准** | 使用内置的「4 步决策树」（针对躯体症状障碍 SSD 评估工具的 scoping review 场景，通用性较强） |
| **填了自己的标准** | **完全按你填的标准筛选**，忽略内置规则 |

填入标准示例：

```
纳入标准：
- 研究对象为躯体症状障碍 / 医学无法解释的症状人群
- 使用了至少一个标准化临床评估量表

排除标准：
- 综述、评论、会议摘要
- 动物实验
```

**筛选结果**：

| 判定 | 含义 |
|------|------|
| `Include` | 纳入 ✅ |
| `Exclude` | 排除 ❌（附理由） |
| `Uncertain` | 不确定，需人工复核 ⚠️ |
| `Error` | 调用失败，程序会自动重试 2 轮 |

> 第一轮全部跑完后，程序会**自动重试**出错的篇目，无需手动干预。日志区能看到重试的逐篇过程。

---

## 9. 查看与导出结果

### 最终 Excel

全部跑完后，在 `output/` 或你的工作目录下会生成：

- `results_final_screened_*.xlsx` — **最终结果**（Phase 4 后），包含每篇的：
  - 下载状态、文件名
  - OCR 是否成功
  - 纳入/排除判定 + 理由

### PDF 文件

- `papers/` 目录：所有下载的 PDF，统一命名为 `作者_年份_期刊_标题.pdf`
- `ocr/` 目录：OCR 生成的 Markdown 全文

### 在网页里导出

仪表盘上也可以直接点击按钮下载结果 Excel。

---

## 10. 常见问题 FAQ

**Q：安装时报错 / 卡住不动？**
A：大概率是网络问题。install.py 会先用清华镜像，失败自动回退官方源。可以多试几次，或挂代理后重试。

**Q：Phase 1 跑得很慢？**
A：正常，Sci-Hub 有请求间隔限制（防封）。几百篇文献跑几小时是正常的，可以中断后继续。

**Q：某篇文献一直下载失败？**
A：可能是 Sci-Hub 没有收录。检查 DOI 是否正确（尤其注意 DOI 末尾有没有混入期刊缩写之类的脏数据），或手动下载后导入。

**Q：Phase 3 报错 MinerU 找不到？**
A：确认已运行 `python install.py --mineru`，且 `config.py` 里的 `MINERU_EXECUTABLE` 路径正确。

**Q：Phase 4 筛选结果不准？**
A：可以：
- 填入你自己的纳入/排除标准（会完全覆盖内置规则）
- 对 `Uncertain` 的篇目人工复核
- 调整 `config.py` 里的 `SCREENING_TEMPERATURE`（越低越稳定）

**Q：中途断了怎么办？**
A：直接重新启动程序，重新点对应的 Start 按钮即可。已完成的会**自动跳过**，不会重复劳动。

**Q：能处理多少篇文献？**
A：没有硬性限制。实测几千篇也能跑，只是时间较长。

---

## 附：手动补充 PDF 的完整流程

当自动下载拿不到某篇文献时：

1. 自己想办法下载 PDF（数据库、馆际互借、作者索取等）
2. 把 PDF 放进 `papers/` 目录，文件名建议 `作者_年份_标题.pdf`
3. 运行：

```bash
python import_manual_pdfs.py
```

4. 脚本会自动识别 PDF、匹配元数据、更新结果清单
