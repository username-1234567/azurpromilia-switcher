# 详细手册

快速开始见 [README](../README.md)。本文面向需要命令行、迁移、排障或了解实现细节的用户。

## 实现方式

启动器接口返回逐文件清单，包含路径、大小、校验值和下载地址。比对两份清单后按以下规则处理：

- 路径与校验值均相同：建立硬链接，不额外占用空间。
- 路径相同、校验值不同：各区编译产物，需下载。
- 仅一侧存在：该区独有文件，需下载。

游戏运行时只读取资源，多个区指向同一硬链接不会产生写入冲突。

## 前置条件

- 已完整安装一个区作为源区。
- 客户端目录位于同一 NTFS 分区，ReFS 亦可；目标区预留约 30 GiB。
- Python 3.10 及以上，安装时勾选 Add python.exe to PATH。
- AES 后端二选一：`pycryptodome`，或系统已安装 `openssl`。
- 日服 CRC64 校验使用内置 `7za`，无需额外安装 7-Zip；需要指定其他版本时可用 `AZP_7ZA`。7-Zip 的许可证随附在 `azp/tools/7-Zip-License.txt`。

新增国服或台服时，先安装启动器（约 170 MB），不触发客户端下载，安装后关闭。

环境不确定时先执行 `python -m azp check`，检查卷类型、是否同卷、硬链接可用性、可用空间和路径长度。

## 命令行方式

不使用图形界面时，可以用命令行完成同样的流程。在 GitHub 页面选择 Code → Download ZIP，解压到普通目录。避免放在 `C:\Program Files`，写入 `out\` 需要管理员权限。

执行 `quickstart.cmd`：选择源区，再选择目标区，后续流程自动完成，包括抓取清单、建立硬链接、下载差异、按清单复核并修复不一致文件。等同 `python -m azp quickstart`。上次的源区、目标区与目标目录记录在 `out\quickstart.json`，再次运行时作为交互默认值。

日服客户端无需预装，但必须安装过日服启动器，抓取清单所需的签名盐仅存在于启动器文件中。安装后在向导中选择「日服」，指定以 `AZUPRO_JP` 结尾的空目录。没有启动器时，可通过 `AZP_YOSTAR_SALT` 直接提供盐值。

已知路径时可省略交互，直接传入参数：

```bat
python -m azp quickstart --base cn --target global --target-dir "C:\AzurPromiliaGlobal\AzurPromilia_TC_game" --dry-run
python -m azp quickstart --base cn --target jp --target-dir "C:\AZUPRO_JP" --yes
```

`--dry-run` 仅计算计划并执行环境检查，不写入客户端目录，计划表仍生成到 `out\`。`--yes` 跳过最终确认。目标目录已有内容时默认只补缺失，其中大小已符合清单的文件计为「已就绪」，不重新下载；需整体重装时加 `--reinstall`，原目录移入回收站。启动器不在标准位置且 `detect` 未识别时，用 `--launcher` 指定 `launcher.exe`、版本目录、安装根或游戏目录。目标区已装反虚化补丁时，复核阶段默认跳过该文件，不覆盖；源区装过补丁时，硬链接会把它带到目标区，向导会提示。

## 分步执行

以下命令在解压后的项目目录中执行，使用 cmd 语法。PowerShell 中将 `set` 改为 `$env:BASE="..."`，`%BASE%` 改为 `$env:BASE`。

```bat
python -m pip install -r requirements.txt

set "BASE=C:\AzurPromilia\AzurPromilia_Game"
set "TGT=C:\AzurPromiliaGlobal\AzurPromilia_TC_game"

python -m azp detect
python -m azp manifests --force
python -m azp plan  --base cn --target global --local "%BASE%"
python -m azp link  --plan out\plan_cn_to_global.csv --base "%BASE%" --local "%BASE%" --target "%TGT%"
python -m azp fetch --plan out\plan_cn_to_global.csv --target "%TGT%" --workers 16
python -m azp finalize --target "%TGT%" --manifest out\global\manifest.json --index-csv out\global\index.csv --paths-from "%BASE%"
python -m azp verify --manifest out\global\manifest.json --dir "%TGT%" --sample 20
```

目标目录名由启动器的 `gameFolderName` 决定，不可自定义：台服为 `AzurPromilia_TC_game`，日服为 `AZUPRO_JP`。完成后启动一次目标区启动器，执行其内置的校验修复。

## 命令

| 命令 | 说明 |
|---|---|
| `quickstart` | 完整流程：抓清单、建链接、下载、校验、修复 |
| `detect` | 列出本机的启动器、游戏目录与区服代号 |
| `regions` | 查看区服表与 `regions.json` 本地覆盖 |
| `check` | 环境自检：卷类型、同卷、硬链接、空间、长路径 |
| `manifests` | 抓取各区清单至 `out/<区服>/` |
| `plan` | 比对清单，生成 link / link_local / download 计划 |
| `link` | 按计划建立硬链接，支持 `--dry-run` |
| `fetch` | 多线程下载，断点续传并逐文件校验 |
| `finalize` | 写入启动器索引：Manjuu 系为 `_index`，日服为 `manifest.json` |
| `verify` | 检查缺失、大小并抽样校验内容 |
| `repair` | 仅修复损坏文件，可重建链接时不重新下载 |
| `rollback` | 按 `out/journal/` 账本撤销上一次写入 |
| `remove` | 删除一个区的客户端，`--trash` 移入回收站 |
| `export` | 导出增量（含 `_handoff.csv`），`--full` 导出整个客户端 |
| `import` | 从局域网共享拉取需下载的部分 |
| `gui` | 启动图形界面 |

`detect`、`regions`、`plan`、`verify` 只读，不修改客户端；`check` 仅在目标卷执行硬链接探测，不写入源区。其余命令会按功能写入或删除目标文件，执行前需确认提示信息。

`nofade` 会替换客户端资源，存在封号风险，未列入上表，见文末「风险功能」。

区服代号：`354/C000` 为 `cn`，`354/C001` 为 `bili`，`434/C500` 为 `global`，`AZUPRO_JP` 为 `jp`。

## 官服与哔哩哔哩服切换

官服与 B 服同 AppID（354）、同客户端目录，区别是渠道号（C000 / C001）与登录组件。切换方式为替换：

- `<game>\mj_sdk\res\app_config.dat`
- `<game>\AzurPromilia_Data\Plugins\mj_sdk`

B 服的 `mj_sdk` 下有 `BLPlatform64`，官服没有，用它判断当前渠道。切换前把当前渠道文件备份到 `out/backup/channel/`，再从 `out/cn_sources/<server>/` 覆盖。切换需要先提取两边的渠道源，且游戏必须完全退出。

## 日服

日服使用 Yostar 启动器，命令与国服一致，差异如下：

```bat
set "BASE=C:\AzurPromilia\AzurPromilia_Game"
set "JP=C:\AZUPRO_JP"

python -m azp manifests --region jp
python -m azp plan   --base cn --target jp --base-dir "%BASE%" --local "%BASE%"
python -m azp link   --plan out\plan_cn_to_jp.csv --base "%BASE%" --target "%JP%"
python -m azp fetch  --plan out\plan_cn_to_jp.csv --target "%JP%"
python -m azp finalize --target "%JP%" --manifest out\jp\manifest.json
```

日服根目录为 `AzurPromiliaJP_Data`，国服为 `AzurPromilia_Data`；`plan` 依据清单中的映射自动换算。

两区校验方式不同（MD5 与 CRC64），无法直接比较字符串，因此 `plan` 必须带 `--base-dir`，由工具在源区实际计算同名同大小文件的校验值。该步骤耗时约两分钟，结果缓存在 `out/cache/crc64.json`。实测 31,491 个文件中，有 6 个文件名与大小相同但内容不同，仅比对清单无法发现。

`finalize` 写入 `manifest.json` 与 `game-launcher-config.json`（均含 `vc` 校验），不写 `_index`。目录名必须为 `AZUPRO_JP`，大小写敏感。

抓取清单前需已安装日服启动器，签名盐位于其 `resources\*.asar` 中，仓库不内置该常量，运行时读取。

实测国服 → 日服：78.38 GiB 客户端中，31,485 个文件（46.71 GiB）直接建立链接，另有 254 个文件复用源区，实际下载 29.61 GiB。

完成后打开日服启动器，不再提示下载即为识别成功；若仍提示，见「故障处理」。

## 三区共存

以一个已安装的区为源，其余区向就近的已安装区计算增量，流程与安装第二个区相同：

```bat
set "GL=C:\AzurPromiliaGlobal\AzurPromilia_TC_game"
set "JP=C:\AZUPRO_JP"

python -m azp plan   --base global --target jp --base-dir "%GL%"
python -m azp link   --plan out\plan_global_to_jp.csv --base "%GL%" --target "%JP%"
python -m azp fetch  --plan out\plan_global_to_jp.csv --target "%JP%"
python -m azp finalize --target "%JP%" --manifest out\jp\manifest.json
```

三个目录须位于同一分区，名称分别为 `AzurPromilia_Game`、`AzurPromilia_TC_game`、`AZUPRO_JP`。实测总占用 132.81 GiB（国服 79.01、台服 29.19、日服 24.61），独立安装则为 239.70 GiB。

任一区热更后相关链接会断开，重跑该区的 `plan` 与 `link` 即可恢复。两个区的客户端不可同时运行，ACE 通常只允许单实例。

## 迁移到其他机器

硬链接仅在本机有效，节省的空间不随文件迁移。对方所需下载量取决于现有资源：

| 对方现状 | 处理方式 |
|---|---|
| 已安装任一区客户端 | 仅下载差异，约 29 GiB |
| 无客户端，可下载 | 先用官方启动器安装一个区（约 79 GiB），再用本工具添加第二个区，合计 108 GiB |
| 无客户端，不便下载 | 将完整客户端复制到移动硬盘，对方安装启动器后直接使用 |

导出完整客户端：

```bat
python -m azp export --full --source "D:\AzurPromiliaGlobal\AzurPromilia_TC_game" --dest "E:\AzurPromilia_TC_game"
```

导出结果含 42,474 个文件、95.58 GiB，包含启动器索引（国服与台服为 `_index`，日服为 `manifest.json` 与 `game-launcher-config.json`）。

删除客户端前可先评估影响：

```bat
python -m azp remove --target "D:\AzurPromiliaGlobal\AzurPromilia_TC_game" --base "C:\AzurPromilia\AzurPromilia_Game" --dry-run
```

## 故障处理

撤销本次写入：`python -m azp rollback --target "<目录>"`。仅删除本次新建的文件并还原被覆盖的 `_index`，源区不受影响。

修复损坏文件：`python -m azp repair --target "<目录>" --manifest out\<区服>\manifest.json --base "<源区>"`。源区存在同内容文件时重建链接，否则仅下载该文件；加 `--full` 执行全量校验。

误删恢复：`python -m azp remove --target "<目录>" --trash` 将整个目录移入回收站。

共享文件被删除不影响数据完整性，源区保留同一份数据，重建链接即可。无法恢复的情况仅限：该文件为该区独有、已删除，且无网络与备份。

下载中断（Ctrl+C）后账本已落盘，`rollback` 可撤销本次写入；重跑同一命令将续传。

装完仍提示下载时依次检查：

1. 目录名是否正确：台服为 `AzurPromilia_TC_game`，日服为 `AZUPRO_JP`。
2. `finalize` 是否执行：日服目录中应存在 `manifest.json` 与 `game-launcher-config.json`。
3. 执行 `python -m azp verify --manifest out\<区服>\manifest.json --dir "<目录>" --sample 50` 查看缺失项。

`link` 报跨卷表示两个目录不在同一分区，可用 `python -m azp check --base "<源>" --target "<目标>"` 提前确认。

## 说明

- 日服未经过实测，仅在国服与台服间测试过，理论上可行。若出现问题请提供 `out\plan_cn_to_jp.csv`、`out\plan_global_to_jp.csv`、`out\jp\manifest.json`、`out\jp\game-launcher-config.json` 及 `out/cache/crc64.json`，并提交 issue。
- 大版本更新会替换共享文件并断开链接，重跑 `manifests --force`、`plan`、`link` 即可恢复，无需整包重装。
- `plan --local` 按文件名与大小匹配本地额外文件，命中后校验内容。跨校验方式（国服 ↔ 日服）需加 `--base-dir`，按映射在源区逐个计算 CRC64。按内容全盘匹配尚未实现，预计可再节省约 1 GiB。
- 仅支持 Windows。Manjuu 清单接口偶发缓慢，15 MB 清单可能耗时十几分钟，属 CDN 侧问题；日服接口通常数秒完成。
- 请勿将 `app_config.dat` 内容或 token 提交至 issue。`export` 输出的文件属于游戏资源，仅限本机之间传输。
- 仓库不含账号凭据：渠道密钥、AES 解密密钥、日服签名盐均在运行时从本机启动器读取，`out\`、`regions.json`、`app_config.dat` 不纳入仓库。唯一内置常量为 `_index` 的固定混淆参数，与官方客户端一致，不属于凭据，说明见 `manifest.py` 注释。
- 实测：安装台服并完成热更后，链接断开 0 条，共享文件被改写 0 个，源区 42,734 个文件无变动。

## 风险功能：反虚化

**目前实测能使用不代表以后能够稳定使用**，官方可能随时更新客户端资源包，导致补丁失效甚至封号。使用前请自行评估风险。

> `nofade` 会替换游戏客户端资源包，属于修改客户端文件，官方用户协议不允许，
> 被检测到可能导致**封号**，账号与数据风险由使用者承担。
> 常规流程只写入目标区、不修改源区；但与补丁共用同一文件时可能相互影响，规则见本节末尾。

仅支持国服，替换 `.res/default_package/BundleFiles/rjmh704fkrsjimysndpsg.r`，移除角色在大世界中的虚化效果。修改范围为相机预制体中的 51 个组件，文件大小不变，逐段 CRC 已配平。仓库仅包含该替换文件，不涉及任何密钥。

```bat
python -m azp nofade status  --target "C:\AzurPromilia\AzurPromilia_Game"
python -m azp nofade install --target "C:\AzurPromilia\AzurPromilia_Game" --accept-risk
python -m azp nofade restore --target "C:\AzurPromilia\AzurPromilia_Game"
```

- `install` 必须显式添加 `--accept-risk`，仅加 `--yes` 会被拒绝，防止脚本或 agent 误装。
- 仅支持国服资源版本 3271113，原文件校验值不符时拒绝执行。
- 原文件备份至客户端 `.nofade-backup\`，由 `restore` 恢复；`restore --base <另一区>` 可同时重建硬链接。
- 该文件与其它区共享硬链接时，仅断开当前区，其它区不受影响。
- **大版本更新前应先执行 `restore`**，否则启动器可能判定资源损坏并重新下载数十 GB。
- 游戏运行期间拒绝执行。
- `quickstart` 检测到补丁时会提示，且默认保留：目标区已装补丁不参与复核修复；源区已装补丁会经硬链接带到目标区，目标区要官方文件时先对源区执行 `restore`。
- `--reinstall` 会把目标区的 `.nofade-backup\` 一并移入回收站，之后 `restore` 无可用备份。

## 开发

核心逻辑提供离线测试，不联网、不接触真实游戏目录，补丁相关用例使用临时构造的样例资源：

```bat
python -m unittest discover -s tests -v
```

当前 230 个用例，覆盖区服与清单解析、硬链接、断点续传（含 416 与忽略 Range 的服务端）、账本与回退、`quickstart` 全流程（含日服与选择记忆，基于假清单离线运行）、已装文件的就绪判定、反虚化补丁的保留规则、渠道切换以及各入口的越界路径防护。