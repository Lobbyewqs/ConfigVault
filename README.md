# ConfigVault

为 CS2 单个 `.cfg` 文件保存不可变历史，支持手动推送、版本比较、冲突检测、恢复和带备份的拉取。服务端 Java 17 / Spring Boot / Spring Security / MyBatis / MySQL / Flyway；客户端 Python 标准库。每用户最多 20 个库，每库最多 100 个版本，每文件不超过 256 KiB。

**当前状态：本机测试版。** 先在专用测试目录使用，不要直接对真实游戏配置操作。详见 [验证记录](docs/验证记录.md)。

## 运行服务

需要 Docker Compose 或 JDK 17、Maven 3.9 和 MySQL 8.4。示例为本机私有演示，不包含公开部署方案。

### Docker Compose

在 PowerShell 中进入项目目录，分别交互输入数据库应用密码和 root 密码（不要把真实密码写入文件或命令历史）：

```powershell
$env:MYSQL_PASSWORD = Read-Host 'MySQL 应用密码' -MaskInput
$env:MYSQL_ROOT_PASSWORD = Read-Host 'MySQL root 密码' -MaskInput
docker compose up --build
```

浏览 `http://127.0.0.1:8080`，注册并登录。Compose 仅将应用端口绑定到本机，数据库不向宿主机发布端口。数据位于 `mysql_data` 卷；普通 `docker compose down` 不删除卷。

### 本机 MySQL + Maven

手动创建 `configvault` 数据库和受限用户，并授予该库建表及读写权限。使用本机 MySQL、JDK 17 和 Maven 3.9 后：

```powershell
$env:JAVA_HOME = 'C:\path\to\jdk-17' # 按实际路径调整
$env:DB_URL = 'jdbc:mysql://127.0.0.1:3306/configvault?useUnicode=true&characterEncoding=utf8&serverTimezone=UTC'
$env:DB_USER = 'configvault'
$env:DB_PASSWORD = Read-Host '数据库密码' -MaskInput
mvn -f server/pom.xml spring-boot:run
```

应用默认为 `127.0.0.1:8080`，Flyway 在启动时执行迁移。默认 HTTP 只适合本机演示；跨设备使用前需自行配置可信 HTTPS 反向代理、网络访问控制和生产安全措施。

## 页面操作

注册账号、登录、创建配置库，选择文件与编码上传；版本历史中可下载、比较与恢复。配置库可重命名；删除需要确认，将永久删除该库及全部历史版本，但不会删除本地 CFG 文件。恢复会生成新的版本。配置原文只作为文本显示，不被执行。编码未知时仍可下载，不能做差异比较。令牌仅在创建时显示一次，默认 90 天过期，可在页面撤销。

页面请求遇到网络中断时，保持页面不刷新并再次点击相同操作，会复用先前请求与幂等键。刷新或关闭页面会丢失尚未确认的请求：此时先查看版本历史再决定是否重新操作。CLI 的待重试记录则持久化保存。

## 客户端

使用 Python 3.9+，不需要安装第三方包。以下命令在项目目录运行，所有路径和库 ID 用自己的测试值替换：

```powershell
python client/configvault.py init --server http://127.0.0.1:8080 --repo 1 --file 'C:\path\to\test.cfg'
python client/configvault.py push --message '初始配置' --encoding utf-8
python client/configvault.py status
python client/configvault.py history
python client/configvault.py pull --dry-run
python client/configvault.py pull --apply
python client/configvault.py backup-list
python client/configvault.py backup-restore '<备份 ID>'
```

网络命令每次通过不回显的交互提示读取令牌，不将令牌放进命令行、状态文件或日志。`init` 不验证服务器，并且不会改写已有状态文件；不同客户端可用 `--state` 指定独立状态路径。空库可直接首推；已有库应先 `pull --dry-run`。如本地有未同步的内容，确认采用云端后才能加 `--accept-cloud`，应用之前会备份。推送中断后再次执行 `push` 将重试原始字节、元数据和幂等键，即使本地文件已修改；待处理推送存在时可以预览云端，但须先重试或检查云端历史后执行 `abandon-push`，才能应用拉取。服务器明确拒绝的请求（例如基线冲突、文件超限）会清除待处理请求，超时、限流和服务器错误则保留原请求。状态文件可能包含待重试的配置内容，请妥善保管。

备份位于所选文件所在目录的 `.configvault-backups`。应用时关闭游戏和其他编辑器。外部进程仍可能在最终检查之后写入，原子替换也不能承诺完全抵御断电。详见 [设计记录](docs/设计记录.md)。

## API

浏览器通过 `/api/session` 表单登录并使用 CSRF token；客户端使用 `Authorization: Bearer <token>`。创建令牌仅允许浏览器会话。主要接口：

- `POST /api/register`、`GET /api/csrf`、`POST /api/session`、`POST /api/tokens`、`DELETE /api/tokens/{id}`。
- `POST/GET /api/config-repositories`；`GET/PATCH/DELETE /api/config-repositories/{id}`。重命名使用 JSON `name`；删除使用 JSON `baseVersionId`（空库为 `null`），版本变化时返回 409。
- `POST /api/config-repositories/{id}/versions`：multipart 字段 `file`、`baseVersionId`（空库传空字符串）、`message`、`encodingLabel`；请求头 `Idempotency-Key`。
- `GET /api/config-repositories/{id}/versions?page=0&size=30`，`GET .../versions/{versionId}/content`。
- `GET .../diff?from={id}&to={id}`；`POST .../restore` 使用 JSON `sourceVersionId`、`baseVersionId`、`message` 和 `Idempotency-Key`。

基线不匹配或相同键用于不同请求返回 409；跨用户和跨库资源返回 404。下载响应含 `X-Content-SHA256` 和 `X-Content-Size`。同字节普通推送返回 `noChange`；恢复明确创建新版本。

## 测试

```powershell
python -m unittest discover -s client/tests -v
mvn -f server/pom.xml test
$env:CV_TEST_URL = 'http://127.0.0.1:18082' # 指向独立测试服务
python -m unittest discover -s tests -v
```

集成测试会在指定服务上注册随机测试账号并创建数据，只应指向使用专用临时数据库的测试服务。过期令牌专项测试需要额外的隔离 MySQL 测试配置；未配置时跳过。测试不应被视为游戏内有效性证明。
