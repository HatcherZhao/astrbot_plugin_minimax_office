# MiniMax Office for AstrBot

让 AstrBot 通过聊天生成、读取、修改 Word、Excel、PowerPoint 和 PDF，并把文件发回当前会话。普通群成员也能使用，群聊默认需要 @ 机器人。

基于 [MiniMax-AI/skills](https://github.com/MiniMax-AI/skills) 的四套文档技能适配，包含完整技能资料、模板、辅助脚本、Word CLI 构建源码及运行依赖的镜像构建定义。**沿用 AstrBot 已配置的对话模型，不要求额外购买 MiniMax API。** 这是社区适配项目。

## 能做什么

| 格式 | 能力 |
| --- | --- |
| Word | 新建文档、修改文本与结构、应用排版、OpenXML 校验、转换 PDF |
| Excel | 新建表格、读取与修改工作表、公式校验和重算 |
| PowerPoint | 创建演示文稿、修改指定页面、提取内容、转换 PDF 与预览图 |
| PDF | 生成中文报告、表格与图表、提取文本、填写表单、文档转换 |

- 支持先上传文件，再 @ 机器人描述要求。
- 生成的文件自动登记为当前用户输入，可以直接说“把刚生成的再改一下”。
- 修改结果另存，原文件保留；复杂版式仍建议检查生成结果。
- 可接收旧 `.doc/.xls/.ppt`，通过 LibreOffice 转换后处理。不保证任意 PDF 无损重排、扫描件 OCR 或所有 Office 宏功能。
- 长任务转后台，完成后自动回传；发送失败会重试，插件重启可恢复待交付任务。

```text
@机器人 做一份活动报名 Excel，包含姓名、联系方式、报名时间和人数汇总。
@机器人 把刚才上传的 Word 改成正式通知，保留原表格。
@机器人 给刚生成的表增加一列备注。
@机器人 把这份材料整理成 8 页 PPT，再给我一份 PDF。
```

## 组成与依赖

```text
聊天附件 → AstrBot 插件 → Office 控制器 → 临时文档容器 → 文件回传
```

| 位置 | 内容 |
| --- | --- |
| 仓库根目录 | AstrBot 插件、配置、四个模型工具 |
| `skills/` | 四套完整技能、参考资料、模板、辅助脚本及许可证 |
| `runtime/` | 控制器、任务执行器、Dockerfile、依赖清单、测试 |
| `compose.yaml` | 配套运行服务的资源限制、网络与数据卷 |

任务镜像预装 Python、Node、.NET 8、OpenXML、MiniMax DOCX CLI、PptxGenJS、LibreOffice、Chromium、Pandoc、Poppler 及中文字体。构建阶段需要联网，**文档任务阶段不联网、不临时安装软件**。

插件 ZIP 不含数 GiB 的二进制依赖，必须同时部署配套运行服务。当前提供构建文件，未发布可直接拉取的项目镜像。验证环境为 AstrBot v4.29.3、Linux amd64、Docker；其他版本和架构需要自行验证。

## 安装

### 1. 部署运行服务

在 Docker 服务器克隆仓库：

```bash
git clone https://github.com/HatcherZhao/astrbot_plugin_minimax_office.git
cd astrbot_plugin_minimax_office
cp .env.example .env
```

按 [运行服务部署说明](runtime/README.md) 设置随机令牌、宿主机绝对数据路径和 AstrBot 所在网络，再构建并启动。worker 镜像实测约 4.38 GiB，控制器约 1.86 GiB（有共享层）；首次构建及缓存建议预留 12 GiB 以上。

默认资源：控制器 1 CPU / 512 MiB；任务容器 2 CPU / 2 GiB，一次运行一个、最多四个排队，结束后删除。只有控制器常驻。

### 2. 安装 AstrBot 插件

在 AstrBot 后台插件管理中，从以下仓库链接安装：

```text
https://github.com/HatcherZhao/astrbot_plugin_minimax_office
```

| 配置 | 设置 |
| --- | --- |
| `runtime_url` | 同一 Docker 网络中使用 `http://office-runtime:8030` |
| `runtime_token` | 与服务端 `.env` 中 `OFFICE_RUNTIME_TOKEN` 一致 |
| `enable_in_group` | 默认开启，普通群成员可用 |
| `require_at_in_group` | 默认开启，群任务需 @ 或唤醒机器人 |
| `max_upload_mb` | 默认 25 MiB |

保存后确认插件正常加载。已有其他 Office 插件时，先验证新流程，再停用旧插件，避免重复处理附件。AstrBot 的平台、会话白名单和模型工具权限也需允许相应成员使用。

## 文件与执行边界

- 文件按“会话 + 发送者”隔离，群成员之间不共享上传文件，私聊文件不会自动带到群里。
- 单个上传最多 25 MiB；每任务输入总量最多 50 MiB；产物最多 10 个、总量最多 50 MiB。
- 文件默认保留七天；服务端每用户文件配额 200 MiB、全局 2 GiB。插件另存收发副本，按保留期清理。
- 工具只接收已登记的文件 ID，不接受模型指定的宿主机路径、URL、镜像或挂载参数。
- 平台附件 URL 只允许公网 HTTP/HTTPS，逐跳检查重定向和 DNS，并将连接固定到已检查的 IP；不使用环境代理。若平台返回内网下载地址，需要由可信适配器先将文件落地。
- 生成代码在非 root、无网络、只读根目录的任务容器执行，无法读取 Docker socket、AstrBot 数据或服务令牌。
- 控制器使用 Docker socket 调度任务，具有较高宿主机权限。只应供受信任的 AstrBot 使用，妥善保管服务令牌。
- 宿主机 API 默认仅绑定 `127.0.0.1:8030`，机器人通过 Docker 内网访问，不直接暴露到公网。
- 明确的文档请求会隐藏 AstrBot 本机 shell/python 工具，引导模型使用隔离文档工具。其他插件的权限由部署者管理。

## 模型工具

- `minimax_office_guide`：分页读取技能和参考资料。
- `minimax_office_files`：列出当前用户可用输入。
- `minimax_office_run`：提交 Python、JavaScript 或 C# 文档处理代码。
- `minimax_office_status`：查询任务并回传结果。

模型应先读对应技能，再运行代码。输入位于 `/work/input`，输出写到 `/work/output`。接口与运行细节见 [服务说明](runtime/README.md)。

## 验证与迁移

```bash
python -m unittest discover -s tests -v
python -m pip install -r runtime/requirements-controller.txt pytest
python -m pytest -q runtime/tests
```

验证记录与实际边界见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。更新插件与更新任务镜像是独立步骤；技能或依赖更新后应重新构建镜像，保持插件与镜像内的技能一致。

迁移时备份插件配置及数据、服务数据目录、私有 `.env` 和两张运行镜像（或重新构建）。私有令牌和用户文档不要提交到 Git。

## 来源与许可证

本项目代码使用 MIT 许可证。四套技能来自 MiniMax-AI/skills，其 MIT 许可证均保留。来源与适配记录见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[skills/SOURCES.json](skills/SOURCES.json)。镜像内其他软件遵循各自许可证。
