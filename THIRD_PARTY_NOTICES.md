# 第三方来源与适配

`skills/minimax-docx`、`skills/minimax-xlsx`、`skills/pptx-generator`、`skills/minimax-pdf` 来自 [MiniMax-AI/skills](https://github.com/MiniMax-AI/skills)，采用 MIT 许可证，版权归 MiniMaxAI。各目录保留 `LICENSE` 和原始入口 `UPSTREAM-SKILL.md`；上游 commit、目录 tree、入口 SHA-256 记录在 `skills/SOURCES.json`，适配后各文件校验值记录在 `skills/MANIFEST.sha256`。

上游基线为 `33cdc32a8e9905f4eedab7246e4779df48a06922`，对应四个技能目录在复制时没有本地修改。可在仓库根目录运行 `sha256sum -c skills/MANIFEST.sha256` 校验随包提供的技能文件。

本项目针对 AstrBot 隔离执行进行了以下适配：

- 固定使用 `/work/input`、`/work/output` 和只读 `/opt/skills`，通过四个 Office 工具访问。
- 依赖安装与 Word CLI 编译移到镜像构建阶段，任务阶段不下载软件。
- Word CLI 使用预编译入口，C# 使用固定 OpenXML 3.5.1 引用离线编译。
- PDF 使用预装 Chromium 和中文字体，阻止运行时外部 HTTP 资源。
- Excel 重算使用独立输出目录和 LibreOffice profile，避免输入输出同路径假成功。

这些适配不代表 MiniMax 官方提供 AstrBot 插件或支持服务。镜像中的 .NET、LibreOffice、Chromium、Python/Node 库和字体等软件遵循各自许可证；本项目 MIT 许可证不替代其条款。
