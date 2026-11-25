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
| `manifests` | 抓各区清单到 `out/<区服>/` |
| `plan` | 比对两份清单，输出 link / link_local / download 计划表 |
| `link` | 按计划建硬链接，`--dry-run` 可预演 |
| `fetch` | 多线程下载，断点续传并逐个校验 MD5 |
| `export` | 只导出需要下载的部分，附带 `_handoff.csv`，便于离线拷走 |
| `import` | 从局域网共享只拉需要下载的部分 |
| `finalize` | 写 `_index/version`、`_index/<versionCode>`、`_index/paths` |
| `verify` | 检查缺失、大小不符、抽样 MD5 |

区服代号由 appId 和渠道号决定：`354/C000` 是 cn，`354/C001` 是 bili，`434/C500` 是 global。

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
