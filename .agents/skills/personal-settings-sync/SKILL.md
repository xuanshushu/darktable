---
name: personal-settings-sync
description: "记录和应用这位用户的个人软件设定及跨电脑同步规则。用于个人偏好、默认路径、特殊设定的长期记录，以及 darktable 的 NAS 照片调整同步。"
---

# 个人设定与同步

本 Skill 的共享副本位于 darktable 仓库 `.agents/skills/personal-settings-sync`，随个人分支 `codex/dev` 维护。另一台电脑拉取该分支后可在项目中使用 `$personal-settings-sync`。维护时核对仓库副本和本机个人安装副本，同步本次更新的内容；提交和上传遵循当前用户请求，不因记录偏好而自动推送。

## 持续维护

用户于 2026-10-11 明确要求：以后凡是个人设定的特殊要求，都写进 Skill，不能只留在聊天中。处理这类要求时，在完成当前任务的同时更新本 Skill；如果已有更合适的专用 Skill，更新那里，并在本 Skill 留下链接。内容变多时按软件拆到 `references/`，从这里索引，不重复记录。

- 记录用户明确要求长期遵守的偏好、适用软件、作用范围和原因。区分用户要求、实际核对的本机状态，以及操作建议；一次性选择不自动变成永久偏好。
- 用户纠正或撤销偏好时，更新已有条目，移除冲突规则。这里只记录有用的设置，不保存密码、令牌或整份软件配置。
- 用户要求不要接管鼠标键盘。个人配置优先通过配置文件、CLI 或已验证的后台 API 完成；只有用户明确要求界面操作时才使用电脑控制。
- 应用设置与记录规则是两项工作。报告实际落盘的内容、何时生效和仍待核实的机器；不能把文档更新当作配置成功。

## darktable：同一 NAS 上的照片

### 用户选择

- 两台电脑访问 NAS 上同一份照片。通过照片旁边的 XMP 交换暗房调整；不要为了同步调整再复制 RAW，也不要把正在使用的 `library.db` 放到 NAS 上让两台电脑共用。
- 每台电脑保留自己的照片库、缓存和机器相关配置。不同系统的挂载路径可以不同，关键是访问同一份 NAS 文件；新照片仍需各机分别导入。
- 保留 XMP 写入，并在启动时检查其他电脑写回的修改。当前个人分支支持后台检查，保持后台检查选项开启。

需要的三个配置值：

```ini
write_sidecar_files=on import
run_crawler_on_start=TRUE
run_crawler_in_background=TRUE
```

`run_crawler_in_background` 只决定启动扫描在后台执行，不是持续监视 NAS；`run_crawler_on_start` 没开时不会启动扫描。

### 应用配置

先确认实际运行实例和配置目录。2026-10-11 核对的本机位置：

- 程序：`C:\Program Files\darktable-dev\bin\darktable.exe`
- 配置：`C:\Users\xuanshushu\AppData\Local\darktable\darktablerc`，同时检查旁边的 `darktablerc-common`
- 本机照片库：同一配置目录的 `library.db`
- 已核对的 NAS 照片目录示例：`Y:\Photos\LightRoomDoc\sony\2026\2026-10-05`

这些是本机位置，不是其他电脑必须使用的路径。有 `--configdir` 时以实际启动参数为准。只更改上面三项；保留其余设置并先备份配置。

darktable 在退出时会把内存配置写回磁盘。正在运行时直接修改文件不会改变当前会话，且可能被退出保存覆盖。不要强制结束或擅自重启用户正在编辑的程序；采用以下方式：

1. 先把三项值写入配置文件。
2. Windows 上运行 [scripts/apply-darktable-sync.ps1](scripts/apply-darktable-sync.ps1) 的 `-AfterExit` 模式，作为隐藏的一次性任务，在 darktable 自然退出后重读最新配置并只补写这三项。
3. 检查它的状态文件，区分 `file_written_pending_exit`、`waiting_for_darktable_exit` 和 `applied`。这次启动检查在下次启动生效，不宣称当前会话已开始同步。

脚本参数为 `-ConfigPath`、`-StatusPath` 和可选的 `-AfterExit`。默认模式写文件并报告是否有运行实例；后台模式等待运行实例退出后再写，保留退出时保存的其他偏好。用 `Start-Process -WindowStyle Hidden` 启动后台模式，日志和备份放在个人目录，不放进源码仓库。避免为同一配置创建多个重复等待任务。

此脚本使用 Windows 进程名和文件替换语义；2026-10-11 已实测直接写入及正常退出后的补写，确认只更改目标三项。macOS/Linux 不运行此 Windows 辅助脚本；在用户正常退出应用后，用该系统的文件接口修改真实配置目录，保留其余内容并重新读取核对。

本机可用 `gdbus.exe` 的 `org.darktable.service.Remote.Lua` 只读查询运行时设置，先解析真实 D-Bus owner，并读取实际 ConfigDir。`dt.preferences.read("darktable", key, type)` 支持原生偏好；本分支的 `dt.preferences.write()` 只写 `lua/<script>/<key>`，不能把它当成原生设置写入接口。

### 换电脑继续编辑

两台电脑使用兼容的 darktable 版本，特别是个人分支的 AI 模块和参数。轮流编辑同一张照片；另一台编辑完正常退出、确认 NAS 上 XMP 已写回后，再在当前电脑启动 darktable。

发现更新时，选中对应照片并选择“保留 XMP 中的修改”，把 NAS 的调整读入本机数据库。这是用户决定以另一台修改为准时的操作，不是所有冲突一律覆盖本地编辑。需要单张手动读取时，用“载入附属文件…”加载对应 XMP。

若使用存入 `.dtdata` 的外部栅格/AI 蒙版数据，保留同目录同照片的 `.dtdata` 文件，不能只带 XMP；其他外部资源也须可访问。NAS 离线并使用本地副本时，修改可能在本地副本旁，需重新连接 NAS 后完成 darktable 的本地副本同步。

照片的暗房调整与软件偏好分开处理。快捷键、界面设定、预设和风格不会因读取照片 XMP 而自动同步；只迁移用户点名的个人设置，避免覆盖另一台电脑的路径、GPU 和缓存配置。

### 核对依据

- [官方多电脑使用指南](https://docs.darktable.org/usermanual/development/en/special-topics/multiple-computers/)
- 本机代码：`D:\darktable\data\darktableconfig.xml.in` 的上述键；`src/control/crawler.c` 的启动判断；`src/control/conf.c` 的 `_conf_save()` / `dt_conf_cleanup()`；`src/lua/preferences.c` 的 `read_pref()` / `write_pref()`
- `.dtdata` 行为：`D:\darktable\dev-doc\dtdata_sidecar.md`

应用前以当前安装及配置重新核对，不把以上文件位置或分支行为当成所有版本的共同保证。
