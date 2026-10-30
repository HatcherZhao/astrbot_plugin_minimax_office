# MiniMax Office 隔离执行服务

该目录提供两个需要在仓库根目录构建的镜像：

- `runtime/Dockerfile.worker`：逐任务、无网络、非 root 的文档执行镜像。
- `runtime/Dockerfile.controller`：常驻 API 与单并发任务调度器，只监听 Docker 网络与宿主机回环端口。

项目没有发布预构建镜像。首次部署必须在本地或自己的服务器构建；Worker 包含 .NET SDK、LibreOffice、Chromium、Pandoc、Java、中文字体和完整 Python/Node 依赖，最终镜像通常约 4～6 GiB，首次构建缓存与下载峰值约 8～12 GiB。

## 运行依赖

- Docker Engine 与 Docker Compose v2。
- 可拉取 Dockerfile 指定的 AstrBot 基础镜像与 Microsoft .NET SDK 构建阶段。
- 构建时可访问 Debian、PyPI、npm 和 NuGet；任务运行阶段固定 `network=none`，不会临时安装依赖。
- 一个已存在、同时连接 AstrBot 的 Docker 网络；默认名称 `astrbot_bot`，可用 `ASTRBOT_NETWORK` 修改。
- 控制器需要访问 Docker socket，以便按固定资源和安全参数创建临时 Worker。请只在可信宿主机运行控制器，不要将 8030 暴露到公网。

Worker 预装 Python 3.12、Node.js、.NET 8 / OpenXML 3.5.1、MiniMax DOCX CLI、pandas、openpyxl、lxml、ReportLab、PyPDF、PyMuPDF、python-pptx、PptxGenJS、Playwright、系统 Chromium、LibreOffice Writer/Calc/Impress、Pandoc、Poppler、Java，以及 WenQuanYi / Noto CJK 字体。根目录 `skills/` 是插件与镜像的唯一技能源，Dockerfile 不维护第二份副本。

## 配置

```bash
cp .env.example .env
openssl rand -hex 32
```

把生成值写入 `OFFICE_RUNTIME_TOKEN`。将 `OFFICE_HOST_DATA_ROOT` 改成 Docker 宿主机上的绝对目录，并创建它：

```bash
mkdir -p /absolute/path/to/astrbot_plugin_minimax_office/runtime-data
```

该绝对路径会挂载到控制器 `/data`，同时原样传给控制器用于给 Docker daemon 挂载每个任务的只读输入。相对路径、容器内路径或不存在的宿主路径都不能正确工作。Docker Desktop 用户还要确保该目录允许文件共享。

确认 AstrBot 网络存在；若部署使用其他网络名，在 `.env` 修改 `ASTRBOT_NETWORK`。先让 Compose 展开 `.env` 并查看最终网络名：

```bash
docker compose --env-file .env config --format json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["office-bot"]["name"])'
docker network inspect <上一步输出的网络名>
```

## 构建和启动

从仓库根目录运行：

```bash
docker compose --profile build-only build office-worker office-runtime
docker compose up -d office-runtime
docker compose ps
docker compose logs --tail 100 office-runtime
```

`office-worker` 使用 `build-only` profile，仅提供镜像构建，不应作为常驻服务启动。控制器默认限制 1 CPU、512 MiB、128 PIDs；每个临时 Worker 固定无网络、2 CPU、2 GiB 内存与交换合计、256 PIDs、只读根文件系统、移除 Linux capabilities，输出限制在 64 MiB tmpfs。控制器全局只运行一个 Worker，最多排队四项，同一 owner 最多一个未完成任务。

宿主机诊断地址默认为 `http://127.0.0.1:8030/health`。AstrBot 插件配置使用：

```text
runtime_url = http://office-runtime:8030
runtime_token = 与 .env 中 OFFICE_RUNTIME_TOKEN 完全相同
```

`8030` 默认只绑定 `127.0.0.1`。AstrBot 通过共同 Docker 网络中的 `office-runtime` 别名访问，不需要公开宿主端口。

## Docker socket 权限与安全边界

控制器必须对 `${DOCKER_SOCKET_PATH}` 有读写 Docker API 的权限；默认挂载 `/var/run/docker.sock`。Docker socket 等同于宿主机高权限，请不要把控制器 API 暴露到不可信网络，也不要给插件以外的客户端共享 token。

任务请求不能提供镜像名、命令、挂载或 Docker 参数。控制器只使用固定 Worker 镜像和固定入口；每个任务只挂载自己的源码与输入。Worker 不获得 Docker socket、控制器 token、AstrBot 数据目录或其他任务目录。

rootless Docker 或非标准 socket 可设置 `DOCKER_SOCKET_PATH`，但挂载到控制器内部后仍固定为 `/var/run/docker.sock`。如果控制器不是 root，需让容器用户加入 socket 对应 GID；默认控制器使用基础镜像的 root 用户以兼容常见 Linux Docker socket。

## 停止与清理

```bash
docker compose stop office-runtime
docker compose down
```

`OFFICE_HOST_DATA_ROOT` 中保存上传、任务状态和产物，不会随 `docker compose down` 自动删除。默认保留七天，并受每 owner 200 MiB、全局 2 GiB 配额限制。删除该目录会永久删除文档任务数据，应先自行备份。

升级时先重新构建两个镜像，再只重建控制器；正在运行的任务在控制器重启后会标记为失败而不会重复执行，排队任务会恢复调度。
