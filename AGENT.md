# AGENT.md

面向 AI agent 的操作规范。目标是让 agent 在无人监督的间隔内完成切换，且任何一步出错都能如实报告、可回退。

本文中的约束优先级高于 agent 的通用习惯。与本文冲突时，以本文为准；本文未覆盖的情况，停下来询问用户。

## 0. 环境确认

在仓库根目录执行，四项全部通过才可继续：

```bash
python --version                             # 需 3.10+，否则改用 py -3
python -m azp --version                      # 能输出版本号即表示包完整
python -m azp detect                         # 能列出启动器
python -m azp regions                        # cn/bili/global/jp 四个区均应有域名
```

任一检查不通过、运行平台不是 Windows，或 `detect` 未识别到任何启动器时，立即停止并把原始输出交给用户。不要通过 `--root` 尝试猜测路径。

日服启动器没有 `app_config.dat`，只有在扫描到 `*Gamelauncher.exe` 时才会被列出。抓取日服清单依赖其签名盐（仓库不内置，运行时读取），因此必须先安装过日服启动器；确实没有时，请用户设置 `AZP_YOSTAR_SALT`，不要自行编造。

## 1. 开工前确认项

1. **源区**：已完整安装、可正常进入游戏的客户端目录，例如 `C:/AzurPromilia/AzurPromilia_Game`。
2. **目标区位置**：国服或台服填只装了启动器的一侧安装根，例如 `C:/AzurPromiliaGlobal`；日服无需预装客户端，但需已安装日服启动器（用于读取签名盐），目标为以 `AZUPRO_JP` 结尾的空目录。
3. **目标目录名**：由启动器的 `gameFolderName` 决定，台服为 `AzurPromilia_TC_game`，日服为 `AZUPRO_JP`，不可自定义。

`python -m azp detect` 的输出可提供前两项及区服代号（cn / bili / global / jp），应先执行再提问，以减少不必要的交互。

日服（`jp`）可直接使用，但与国服、台服的命令存在以下差异，不可直接套用：

- 目标目录名必须为 `AZUPRO_JP`，游戏文件直接位于该层。
- `plan` 必须加 `--base-dir "%BASE%"`。两区校验方式不同（MD5 / CRC64），需用 7za 实际计算同名同大小文件的校验值，耗时约两分钟。缺少 7za 时会中止，此时应安装 7-Zip，或在用户明确同意后加 `--trust-names`。
- `finalize` 只传 `--target` 与 `--manifest`，**不得**加 `--index-csv`。日服写入 `manifest.json` 与 `game-launcher-config.json`。
- 抓取清单前须能从本机日服启动器读到签名盐。读不到即停止，请用户安装启动器或设置 `AZP_YOSTAR_SALT`，不要联网查找盐值，也不要写死在代码中。

## 2. 红线

违反任意一条均视为事故，宁可中止并询问用户：

- **不写源区**。所有命令的目标均为目标区；`--base` 只用于读取。
- **删除与回退先试运行**。`remove`、`rollback` 必须先执行 `--dry-run`，将数字报给用户（见第 3 节），获得明确同意后才加 `--yes` 实际执行。
- **不修改客户端本体**。不改 `GameAssembly.dll`，不注入进程，不做任何针对 ACE 的对抗性操作。
- **反虚化不属于常规功能**。`nofade install` 会替换客户端资源，违反官方协议，可能导致封号，不属于正常切换流程。仅当用户明确表示「已知会封号，仍要安装」时，才可加 `--accept-risk` 执行。不得主动建议，不得顺带安装，也不得用 `--yes` 绕过。执行前确认游戏已完全退出，并提示大版本更新前需先 `restore`。
- **不触碰密钥**。代码与文档中不得出现任何密钥、盐或凭据。不得将 `app_config.dat` 内容、token、`out/`、`regions.json` 提交进仓库或发布到 issue。渠道密钥、AES 密钥、日服签名盐一律在运行时从用户本机启动器读取。唯一例外为 `manifest.py` 中 `_index` 的固定混淆参数：与官方客户端所用值相同，属公开常量，不是账号凭据。不要改为运行时读取，也不要按泄密处理。
- **不猜测路径**。路径不确定时先执行 `detect`，仍无法确定则询问用户。
- **同一失败最多重试两次**。两次仍失败即停止，并将原始输出交给用户。

## 3. 备份与确认

工具自带三层事后回退机制，其中两层无需询问：

| 机制 | 产生方式 | 覆盖范围 | 是否询问 |
|---|---|---|---|
| 账本 `out/journal/*.json` | link / fetch / finalize / quickstart / nofade 自动写入 | 本次新建或覆盖的文件 | 否，必定写入 |
| `out/backup/<时间>/` | 覆盖启动器索引前自动复制（日服为两个 json） | 仅被覆盖的文件 | 否 |
| 回收站 | `remove --trash` | 整个目标目录 | 是 |

真正需要开口的只有两处。

**A. 首次 `link` 之前**。一次问完，避免分多轮打扰：

```
计划：硬链接 32,323 个 / 51.00 GiB，下载 8,169 个 / 29.19 GiB，新增占用 29.19 GiB
目标：C:/AzurPromiliaGlobal/AzurPromilia_TC_game（不存在 / 已有 N 个文件）
两个问题：现在开始吗？是否需要先做一份备份？
```

备份选项按用户目的给出，不要只给出「建议备份」这类笼统结论：

| 用户需求 | 方案 | 代价 |
|---|---|---|
| 本步骤可撤销 | 默认具备：`out/journal` 账本 + `python -m azp rollback` | 无，自动 |
| 整个目录误删后可还原 | `remove --trash` | 占用回收站配额 |
| 保留一份可拷贝到其他机器 | `python -m azp export --hardlink --plan out/plan_cn_to_global.csv --source "%TGT%" --dest <另一块盘>` | 同分区时零额外占用 |
| 原样复制一份 95 GiB | 不建议 | 成本高于重新下载，且无法改变「共用数据仅一份」的事实 |

**B. `remove` / `rollback` 之前**。必须询问，且必须提供三选一：

```
删除后：与源区共用 32,323 个（不释放）、与别处共用 8,430 个（不释放）、本区独占 1,589 个 / 12.82 GiB（释放）
选：直接删 / 删进回收站（可还原）/ 取消
```

- 用户未明确表示「删除」时不得执行，沉默不等同于同意。
- 用户以腾出空间为目的时应直接删除；仅为「重装」时应推荐 `--trash`。
- 非交互终端下未加 `--yes` 会自行退出，这是设计行为，不得用 `echo y |` 绕过。
- 用户表示无需备份时直接继续，不要反复劝说。

**备份的三条硬性约束**：不得改动源区目录（移动或改名会使全部硬链接失效，`repair` 也将失去同内容来源）；备份目标不得位于源区或目标区内部；整目录复制前先报告体积与剩余空间，空间不足时明确说明会失败，不要开始执行。

## 4. 运行期注意事项

**执行前**

- 确认游戏与启动器均已退出：`tasklist | findstr /i "AzurPromilia launcher"` 应无输出。ACE 运行时会锁定游戏目录中的文件，导致 `remove` 中断、`link` / `repair` 报占用。
- 剩余空间至少为「新增占用 + 5 GiB」。`check` 报告的是缺口（目标中已存在且大小一致的文件不计入），不要用 `plan` 的全量数字判断。

**执行期间**

- `fetch` 是长任务：29 GiB 约需 20 分钟至数小时。开始前告知用户预估时间，并提醒勿关闭窗口。中断后重跑同一命令即可续传并逐文件校验，不要更换参数。账本每数百个文件落盘一次，中断后 `rollback` 仍可用。
- 下载会占满带宽。开始前询问用户是否处于会议等带宽敏感场景。
- 不要清理 `out/`。清单、计划表、账本、`out/backup/` 均为后续 `repair` / `rollback` 的依据。
- 不要手动复制、robocopy 或移动文件「协助」处理。链接、清单、启动器索引三者不一致时，`verify` 会将正常文件误报为损坏。
- `verify --full` / `repair --full` 在游戏运行时受文件占用干扰，报「读不到」属正常现象，不代表文件损坏。

**禁止事项**

- 不同时运行任意两个区的客户端，ACE 通常只允许单实例。
- 不使用 `remove` 删除启动器目录。它只处理客户端目录，启动器需通过系统卸载。
- 不修改目录名、不移动文件以「适配」。目录名只认清单中的 `game_folder_name`。
- 大版本更新后链接会断开，应重跑 `manifests --force` → `plan` → `link` 恢复，不要整包重装。

## 5. 标准流程

```bat
set "BASE=C:\AzurPromilia\AzurPromilia_Game"
set "TGT=C:\AzurPromiliaGlobal\AzurPromilia_TC_game"
```

| # | 命令 | 通过标准 | 不通过时 |
|---|---|---|---|
| 1 | `python -m azp check --base "%BASE%" --target "%TGT%"` | 全部为 `[ok]` | 出现 `[x]` 即停止。空间不足最常见，报告给用户 |
| 2 | `python -m azp manifests --force` | 输出「清单 N 个文件 / X GiB」 | 多为 CDN 缓慢，重试一次；仍失败则停止 |
| 3 | `python -m azp plan --base cn --target global --local "%BASE%"` | 输出硬链接 / 下载 / 新增占用三行 | 下载量明显超出预期时先停止，可能选错源区 |
| 4 | `python -m azp link --plan out/plan_cn_to_global.csv --base "%BASE%" --local "%BASE%" --target "%TGT%"` | 「新建 N 个…失败 0 个」 | 失败多因跨卷或目标已存在，按提示处理 |
| 5 | `python -m azp fetch --plan out/plan_cn_to_global.csv --target "%TGT%" --workers 16` | 「失败 0」 | 支持断点续传，重跑同一命令；连续两次失败则停止 |
| 6 | `python -m azp finalize --target "%TGT%" --manifest out/global/manifest.json --index-csv out/global/index.csv --paths-from "%BASE%"` | 输出写入文件清单（Manjuu 系 3 个，日服 2 个） | 目录名不符时会拒绝写入，改名后重跑 |
| 7 | `python -m azp verify --manifest out/global/manifest.json --dir "%TGT%" --sample 20` | 「缺失 0，大小不符 0，抽样 20 通过 20」 | 转第 6 节处理 |

执行第 4 步（`link`）前，须按第 3 节 A 报告数字、询问备份并等待回答。这是正常流程中唯一需要中断等待的环节。

上表以国服 → 台服为例。更换区服只需替换代号，其余不变：`--base cn` / `--target bili`、计划表 `out/plan_cn_to_bili.csv`、清单 `out/bili/manifest.json`。

目标为日服时需两处调整：`plan` 加 `--base-dir "%BASE%"`，`finalize` 去掉 `--index-csv`。

安装第三个区即重复执行本表：源区替换为任一已安装完成的区（以日服为源时需加 `--base-dir`），目标替换为新目录，命令结构不变。

若用户只需要「一条命令完成」，可使用 `python -m azp quickstart`，其内部依次执行第 1–7 步并在收尾自动修复。但 agent 自行执行时仍应优先分步，因为每步可单独报告、单独重试。向导同样支持日服：在「加哪个区」中选择日服，再给出以 `AZUPRO_JP` 结尾的空目录，它会自动抓取清单、按 CRC64 比对并写入两个 json。上次选择记在 `out/quickstart.json`，再次运行作为交互默认值；检测到反虚化补丁时会提示并按保留处理。

已知路径时不要依赖交互，直接传参：

```bat
python -m azp quickstart --base cn --target global --target-dir "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" --dry-run
python -m azp quickstart --base cn --target jp --target-dir "C:/AZUPRO_JP" --yes
```

- `--dry-run` 仅计算计划并做环境检查，用于先向用户提供数字。它会写入 `out/` 中的计划表，但不接触客户端目录。
- `--launcher` 用于 `detect` 未能识别的启动器，可传 `launcher.exe`、版本目录、安装根或游戏目录。
- 目标目录已有内容时，`--yes` 默认仅补缺失。需重装时加 `--reinstall`，原目录移入回收站。
- 目标区已有且大小符合清单的文件计为「已就绪」，不重新下载；内容由收尾抽样复核与 `repair` 兜底。
- 上次的源区、目标区与目标目录记在 `out/quickstart.json`，再次运行作为交互默认值；删除该文件即清除记忆。
- 检测到反虚化补丁时：目标区已装补丁默认跳过复核修复；源区已装补丁会经硬链接带到目标区，需官方文件时先对源区执行 `nofade restore`；`--reinstall` 会把 `.nofade-backup` 一并移入回收站。

## 6. 故障与修复

| 现象 | 判断 | 处理 |
|---|---|---|
| verify 报缺失 / 大小不符 / 校验值不符 | 文件损坏或未下载 | `python -m azp repair --target "%TGT%" --manifest out/global/manifest.json --base "%BASE%" --full`，再执行 `verify` |
| 上一步后仍有失败 | 可能为 CDN 或网络问题 | 重跑 `repair` 一次；仍失败则将清单中的失败项交给用户 |
| 目标区被改乱，需回到执行前状态 | 存在账本即可撤回 | `python -m azp rollback --target "%TGT%" --dry-run` → 报告数字 → 用户同意后加 `--yes` |
| 需整体重装目标区 | 先评估删除影响 | `python -m azp remove --target "%TGT%" --base "%BASE%" --dry-run` → 按第 3 节 B 询问 → 执行 |
| `link` 报「硬链接不能跨卷」 | 源与目标不在同一分区 | 停止，请用户将目录移至同一磁盘 |
| `finalize` 报「目录名应为 …」 | 目标目录名与清单不符 | 改为清单中的 `game_folder_name` 后重跑 |
| `plan` 报「没有找到 7za」 | 目标区为日服，需 CRC64 核对 | 安装 7-Zip；用户同意跳过核对后才可加 `--trust-names` |
| `finalize` 报「日服目录名必须是 AZUPRO_JP」 | 日服目录名错误 | 改名，不要修改代码 |
| `nofade` 报「当前文件不是资源版本」 | 游戏已更新，补丁不适用 | 停止，请用户先 `restore`；等待对应版本补丁，不要强行覆盖 |
| `nofade install` 提示「已拒绝」 | 未提供 `--accept-risk`，`--yes` 不生效 | 停止，先说明封号风险；用户明确同意后才加 `--accept-risk` |
| `nofade` 报游戏正在运行 | 进程占用 | 请用户完全退出游戏与启动器后重试 |
| `manifests --region jp` 报「找不到日服签名盐」 | 本机无日服启动器，或无法读取其 asar | 请用户安装日服启动器或设置 `AZP_YOSTAR_SALT`；不要写死常量，也不要从其他来源获取 |
| 提示「没有配置接口域名」 | 自建渠道在 `regions.json` 中缺少 domains（内置四区不会出现） | 停止，请用户补全后执行 `python -m azp regions` 复查，不要编造域名 |
| `manifests` 只抓到一个区 | 另一区的启动器未安装或未被识别 | 先用 `python -m azp regions` 确认代号，再用 `python -m azp detect --root <安装根>` 指定路径 |

`repair` 优先重建硬链接而非下载：源区存在同内容文件时零下载。因此修复前不要删除源区。

## 7. 交付报告

向用户交付四项信息，避免长篇叙述：

1. 目标目录最终位置及占用（取自 `plan` / `remove` 的数字）。
2. 校验结果（`verify` 的最后一行）。
3. 账本位置（`out/journal/*.json`），说明出问题可执行 `python -m azp rollback`；如有删除操作，说明是否在回收站中。
4. 下一步：启动一次目标区启动器完成校验修复，且任意两个区不要同时运行。

## 8. 主动提醒

不必逐步重复，只在对应时机提示一次。

| 时机 | 提示内容 |
|---|---|
| 开工前 | 将新增数十 GiB（参考 `plan` 的「新增占用」），下载耗时数分钟至数小时；期间不要关机、休眠或关闭窗口 |
| 开工前 | 源区不要移动、改名或删除。它是全部硬链接的数据来源，一旦变动链接全部失效 |
| 下载中 | 带宽被占满属正常现象。断网后重跑同一命令即可续传，无需从头开始 |
| 收尾 | 先进入目标区启动器执行一次「校验 / 修复」，再进入游戏；首次启动触发热更属正常 |
| 收尾 | 任意两个区的客户端不要同时运行，ACE 通常只允许单实例 |
| 收尾 | 账本位于 `out/journal/`，误操作后可用 `python -m azp rollback`；如有删除操作，说明是否在回收站 |
| 使用 `--trash` 前 | 回收站需要配额，数十 GiB 的目录可能放不下；放不下时会提示，并按配置回退处理 |
| 安装 `nofade` 前 | 先说明这会修改客户端且可能封号；用户明确接受后才加 `--accept-risk`。大版本更新前必须先 `restore` |
| 事后 | 大版本更新后链接会断开，重跑 `manifests --force` → `plan` → `link` 恢复，不要整包重装 |
| 任何时候 | 修改本地文件可能违反用户协议，账号风险由用户承担 |
| 任何时候 | 不要把 token、cookie、`app_config.dat` 发布到仓库或 issue；导出的文件属于游戏资源，仅在本机之间传输 |

汇报节奏：每步一行结论，不要粘贴整屏日志；仅在报错时附上原始输出。

## 9. 常见误解

- 本工具不是单纯的「切换器脚本」，不会修改配置或注册表。它只负责准备文件，所谓切换即启动对应区的启动器。
- 硬链接不是复制，删除任一侧都不影响另一侧，因此「卸载一个区」是安全的。
- 硬链接仅在本机有效，迁移机器不会继承这部分节省。对方没有任何客户端时，至少需下载一次完整内容，或通过 `export --full` 从移动硬盘复制过去。
- 共享文件只有一份，损坏时多个区同时受影响；修复依赖清单与 CDN，不依赖备份。
- `out/` 中的清单、计划表、账本均非临时文件，不要清理。
