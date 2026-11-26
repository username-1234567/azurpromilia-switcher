# azurpromilia-switcher

《蓝色星原：旅谣》国服/国际服客户端切换工具。

两个区各装一份客户端，内容相同的文件用硬链接共享，只下载真正不同的部分。装完之后想玩哪个区，
就开哪个区的启动器。

```
装两份     79.01 + 82.31 = 161.32 GiB
用本工具   79.01 + 29.19 = 108.20 GiB
```

仓库里没有游戏文件，也没有渠道密钥。清单和资源从各区官方 CDN 现拉，密钥从本机启动器的
`app_config.dat` 解出来。

## 原理

启动器接口提供一份逐文件清单，含相对路径、字节数、MD5 和 CDN 地址。两份清单按 MD5 比对：

- 路径和 MD5 都相同：内容一致，在目标目录建硬链接，不占空间。
- 路径相同、MD5 不同：两个区各自的编译产物，必须下载。
- 只在一侧出现：该区独有，必须下载。

硬链接是 NTFS 的原生特性，多个目录项指向同一份文件数据。游戏资源运行时只读，不会写冲突。

## 环境要求

- 源区客户端已装好，能正常进游戏。
- 目标区只装启动器（约 170 MB），别让它开始下载。
- 两个目录在同一块 NTFS 分区，硬链接不能跨卷。
- Python 3.10+，AES 后端二选一：`pycryptodome` 或系统 `openssl`。

## 使用

双击 `quickstart.cmd`，选一个基线，剩下的它跑完。等价命令 `python -m azp quickstart`。

要手动控制就逐步来：

```bash
pip install -r requirements.txt

# 1. 本机装了哪些启动器
python -m azp detect

# 2. 抓两个区的文件清单
python -m azp manifests --force

# 3. 算增量
python -m azp plan --base cn --target global --local "C:/AzurPromilia/AzurPromilia_Game"

# 4. 建硬链接
python -m azp link --plan out/plan_cn_to_global.csv \
    --base "C:/AzurPromilia/AzurPromilia_Game" \
    --local "C:/AzurPromilia/AzurPromilia_Game" \
    --target "D:/AzurPromiliaGlobal/AzurPromilia_TC_game"

# 5. 下载缺口，断了重跑即可
python -m azp fetch --plan out/plan_cn_to_global.csv \
    --target "D:/AzurPromiliaGlobal/AzurPromilia_TC_game" --workers 16

# 6. 写 _index，让启动器认这份目录
python -m azp finalize --target "D:/AzurPromiliaGlobal/AzurPromilia_TC_game" \
    --manifest out/global/manifest.json \
    --index-csv out/global/index.csv \
    --paths-from "C:/AzurPromilia/AzurPromilia_Game"

# 7. 校验
python -m azp verify --manifest out/global/manifest.json \
    --dir "D:/AzurPromiliaGlobal/AzurPromilia_TC_game" --sample 20
```

最后启动目标区启动器，做一次校验修复。

## 命令

| 命令 | 作用 |
|---|---|
| `quickstart` | 交互向导，从抓清单到校验一次走完 |
| `detect` | 从注册表和盘符扫描启动器、游戏目录、渠道代号 |
| `check` | 环境自检：Python、AES 后端、卷类型、是否同卷、硬链接实测、可用空间、长路径 |
| `repair` | 按清单只修坏掉的文件：能重建链接就别下载 |
| `rollback` | 按 `out/journal/` 账本撤销上一次写入 |
| `remove` | 删掉一个区的客户端目录，只减硬链接引用，不动另一区 |
| `manifests` | 抓各区清单到 `out/<区服>/` |
| `plan` | 比对两份清单，输出 link / link_local / download 计划表 |
| `link` | 按计划建硬链接，`--dry-run` 可预演 |
| `fetch` | 多线程下载，断点续传并逐个校验 MD5 |
| `export` | 只导出需要下载的部分，附带 `_handoff.csv`，便于离线拷走 |
| `import` | 从局域网共享只拉需要下载的部分 |
| `finalize` | 写 `_index/version`、`_index/<versionCode>`、`_index/paths` |
| `verify` | 检查缺失、大小不符、抽样 MD5 |

区服代号由 appId 和渠道号决定：`354/C000` 是 cn，`354/C001` 是 bili，`434/C500` 是 global。

## 环境自检

不放心可以先查一遍，不碰任何文件：

```bash
python -m azp check --base "C:/AzurPromilia/AzurPromilia_Game" \
    --target "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" \
    --manifest out/global/manifest.json --plan out/plan_cn_to_global.csv
```

检查项是卷类型（NTFS/ReFS）、源与目标是否同一卷、在该卷上真建一对硬链接试试、可用空间够不够、
清单里最长的路径会不会撞上 255 字符限制。有致命项时退出码是 2。`quickstart` 在动手前会自动跑这套。

## 卸载一个区

```bash
python -m azp remove --target "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" \
    --base "C:/AzurPromilia/AzurPromilia_Game" --dry-run
```

`--dry-run` 只统计，输出把文件分成三类：与源区共用的（删了不释放、源区照常）、与别处共用的
（暂存或备份目录还拿着同一份数据，得一起删才释放）、本区独占的（删了才真正释放）。确认无误去掉
`--dry-run`，加 `--yes` 跳过确认。

删之前关掉启动器和游戏，占用中的文件删不掉。`remove` 只删客户端目录，启动器还留在原处。

加 `--trash` 会整个目录移进回收站，删错了能从回收站还原。

## 改坏了怎么退

按代价从低到高，有三条路。

**一、撤销上一次写入。** `link`、`fetch`、`finalize`、`quickstart` 动手时都会在 `out/journal/` 记一笔：
新建了哪些文件、覆盖前备份了哪些（备份放 `out/backup/`）。

```bash
python -m azp rollback --target "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" --dry-run
python -m azp rollback --target "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" --yes
```

只删本次新建的文件、把覆盖前的 `_index` 放回去，源区不动。操作之后又被别的程序改过的文件会保留，不会误删。

**二、只修坏的那几个。** 清单里每个文件都有大小、MD5 和下载地址：

```bash
python -m azp repair --target "C:/AzurPromiliaGlobal/AzurPromilia_TC_game" \
    --manifest out/global/manifest.json \
    --base "C:/AzurPromilia/AzurPromilia_Game" --full
```

源区有同 MD5 的文件就重建硬链接，零下载；没有就只重下这一个。`--full` 全量校验 MD5，95 GiB 几分钟；
不加只比大小，另用 `--sample N` 抽样验 MD5。

**三、删错了。** `remove --trash` 把整目录移进回收站，能还原。

本地真正救不回来的只有一种情况：独占文件被删、没有网络、也没有别的副本。共用文件不存在这个问题——
它就等于源区那一份，重建链接即可。

## 注意

- 大版本更新后，共用文件会被逐批替换，链接断开、变成两份占空间。重跑 `manifests --force`、`plan`、`link` 接回去。
- `plan --local` 按文件名加大小匹配本地额外文件（比如游戏内补下的 BundleFiles），命中后再验 MD5。按内容全盘匹配还没做，估计还能多省 1 GiB。
- 只支持 Windows，依赖 NTFS 硬链接和注册表。
- 清单接口偶尔很慢，15 MB 的清单可能十几分钟，是 CDN 侧的问题。

## 已验证

装完国际服、跑完热更后复查：新增 1,581 个文件 / 12.81 GiB，硬链接断开 0 条，共用文件被原地改写 0 个，源区 42,734 个文件核对无变动。官方客户端走的是新增加替换，不碰共用文件。

更新后建议跑一次 `verify`。另外两个区的客户端不要同时开，ACE 反作弊驱动通常只允许单个实例。

## 免责

工具只读本机启动器的配置去调官方接口，不含账号凭据。改本地文件可能违反游戏用户协议，风险自负。

## License

MIT
