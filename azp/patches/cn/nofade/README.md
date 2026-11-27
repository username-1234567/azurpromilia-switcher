# 国服反虚化替换文件

> **警告：这是修改客户端资源，官方用户协议不允许，可能导致封号。**
> 不是可选的美化项。只有明确接受风险、并知道大版本更新前要先恢复的人再用。

这里只有实现反虚化所必需的东西：一个替换文件，加上校验它所需的哈希值。没有任何密钥。

替换 `AzurPromilia_Data/.res/default_package/BundleFiles/rjmh704fkrsjimysndpsg.r`，
去掉角色在大世界里的虚化（俯视消失、仰视半透明、贴墙透明）。只改相机预制体里的 51 个组件，
文件大小不变，逐段 CRC 已配平，启动器按 manifest 校验能过。

| 项 | 值 |
|---|---|
| 适用 | 国服，资源版本 3271113 |
| 原版 MD5 | `0d53b558d0585a35fcf528e2d5df3d03` |
| 补丁 MD5 | `3c884b36b9a26061c107feb2a4603743` |
| 大小 | 4,744,175 字节 |

用法见仓库根 `README.md` 的「风险功能」一节：

```bat
python -m azp nofade status  --target "C:\AzurPromilia\AzurPromilia_Game"
python -m azp nofade install --target "C:\AzurPromilia\AzurPromilia_Game" --accept-risk
python -m azp nofade restore --target "C:\AzurPromilia\AzurPromilia_Game"
```

注意：

- `install` 必须加 `--accept-risk`，只加 `--yes` 会被拒绝。
- 只带国服补丁。台服/日服的资源包内容不同，直接套会坏。
- 大版本更新前先 `restore`；更新后原文件会变，这个补丁大概率不适用，等新版本。
- 改的是游戏资源，可能违反用户协议，风险自负，不在 MIT 许可范围内。
