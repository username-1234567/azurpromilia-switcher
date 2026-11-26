# azurpromilia-switcher

《蓝色星原：旅谣》国服/国际服客户端切换工具。两个区各装一份，内容相同的文件用硬链接共享，只下载差异部分。

```
装两份     79.01 + 82.31 = 161.32 GiB
用本工具   79.01 + 29.19 = 108.20 GiB
```

仓库不含游戏文件和渠道密钥；清单、资源从官方 CDN 拉取，密钥从本机 `app_config.dat` 解出。

## 原理

启动器接口给出逐文件清单（路径、大小、MD5、CDN 地址），两份清单按 MD5 比对：

- 路径和 MD5 都相同：建硬链接，不占空间
- 路径相同、MD5 不同：各区编译产物，必须下载
- 只在一侧出现：该区独有，必须下载

游戏资源运行时只读，硬链接不会写出冲突。

## 环境要求

- 源区客户端已装好；目标区只装启动器（约 170 MB），别让它开始下载
- 两个目录在同一块 NTFS 分区（ReFS 也行），硬链接不能跨卷
- Python 3.10+，AES 后端二选一：`pycryptodome` 或系统 `openssl`

不确定就先 `azp check`：卷类型、是否同卷、硬链接实测、可用空间、长路径。

## 使用

双击 `quickstart.cmd`，选基线，剩下的它跑完。等价于 `python -m azp quickstart`。收尾会按清单再校验一遍，对不上的就地修。

手动分步：

```bash
pip install -r requirements.txt
BASE="C:/AzurPromilia/AzurPromilia_Game"                    # 已装好的源区
TGT="D:/AzurPromiliaGlobal/AzurPromilia_TC_game"            # 目标区目录名见下

python -m azp detect                                        # 本机有哪些启动器
python -m azp manifests --force                             # 抓两个区的清单
python -m azp plan  --base cn --target global --local "$BASE"
python -m azp link  --plan out/plan_cn_to_global.csv --base "$BASE" --local "$BASE" --target "$TGT"
python -m azp fetch --plan out/plan_cn_to_global.csv --target "$TGT" --workers 16
python -m azp finalize --target "$TGT" --manifest out/global/manifest.json \
    --index-csv out/global/index.csv --paths-from "$BASE"
python -m azp verify --manifest out/global/manifest.json --dir "$TGT" --sample 20
```

目标区目录名不能随便起名，启动器按 `gameFolderName` 找，国际服是 `AzurPromilia_TC_game`。最后启动目标区启动器做一次校验修复。

交给你的agent执行：使用[`AGENT.md`](agent.md)

## 命令

| 命令 | 作用 |
|---|---|
| `quickstart` | 向导：抓清单、建链接、下载、校验、修 |
| `detect` | 扫出启动器、游戏目录、区服代号 |
| `check` | 环境自检：卷类型、同卷、硬链接、空间、长路径 |
| `manifests` | 抓各区清单到 `out/<区服>/` |
| `plan` | 比对清单，出 link / link_local / download 计划表 |
| `link` | 按计划建硬链接，`--dry-run` 可预演 |
| `fetch` | 多线程下载，断点续传 + 逐文件 MD5 |
| `finalize` | 写 `_index`，让启动器认这份目录 |
| `verify` | 查缺失、大小、抽样 MD5 |
| `repair` | 只修坏文件：能重建链接就不下载 |
| `rollback` | 按 `out/journal/` 账本撤销上一次写入 |
| `remove` | 删一个区的客户端，`--trash` 进回收站 |
| `export` | 只导出需下载的部分，带 `_handoff.csv` |
| `import` | 从局域网共享只拉需下载的部分 |

区服代号：`354/C000 → cn`、`354/C001 → bili`、`434/C500 → global`。

删客户端前先看一眼会删多少、释放多少：

```bash
python -m azp remove --target "D:/AzurPromiliaGlobal/AzurPromilia_TC_game" \
    --base "C:/AzurPromilia/AzurPromilia_Game" --dry-run
```

## 出错处理

- 撤销上一次写入：`azp rollback --target <目录>`。只删本次新建的文件、还原覆盖前的 `_index`，源区不动
- 修坏掉的文件：`azp repair --target <目录> --manifest <清单> --base <源区>`。源区有同 MD5 就重建链接，否则只下那一个；`--full` 全量校验
- 删错了：`remove --trash` 把整个目录挪进回收站

共用文件被删没关系，数据就是源区那份，重建链接即可。真正救不回来的只有：独占文件被删、没网络、也没有别的副本。

## 注意

- 大版本更新后共用文件会被替换、链接断开，重跑 `manifests --force`、`plan`、`link` 接回去
- `plan --local` 按文件名 + 大小匹配本地额外文件，命中后验 MD5；按内容全盘匹配还没做，估计还能多省 1 GiB
- 只支持 Windows；清单接口偶尔很慢（15 MB 十几分钟），CDN 侧问题
- 两个区的客户端不要同时开，ACE 一般只允许单实例
- 别把自己的 `app_config.dat`、token 贴到 issue；`export` 出来的文件是游戏资源，只在自己机器之间搬
- 实测：装完国际服并跑完热更，链接断开 0 条，共用文件被改写 0 个，源区 42,734 个文件无变动

## 免责

工具只读本机启动器配置调官方接口，不含游戏资源、渠道密钥和账号凭据。改本地文件可能违反游戏用户协议，账号与数据风险自负。非官方项目，与开发商、发行商无关。

License: MIT，见 [`LICENSE`](LICENSE)；许可范围、风险与第三方依赖见 [`NOTICE`](NOTICE)。
