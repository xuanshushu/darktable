# 个人包检查、下载与安装

## 本机配置与目标

本 Skill 位于仓库 `.agents/skills/darktable-package-install`。另一台电脑拉取仓库后在该项目中使用 `$darktable-package-install`。从 `settings.example.json` 创建 Git 忽略的 `settings.local.json`，发现并填写本机程序、安装、配置、下载及备份目录；不用安装本地编译工具，也不自动安装其他依赖。

默认比较个人 remote `origin` 的 `codex/dev`，安装时不自动同步 upstream。先读取实际 `git remote -v`，从 GitHub remote 得到真实 `owner/repo`；不要把示例账号写死到命令。源码工作区的未提交改动不属于云端目标，不能假装它们已经进了安装包。

Windows 发现正在运行的进程路径/命令行、现有快捷方式和安装项，避免 PATH 指向另一份安装。macOS 检查实际 app bundle 的 `Contents/MacOS/darktable`，支持机器为 Apple Silicon。配置目录优先采用真实进程/快捷方式的 `--configdir`；默认采用 GLib 用户配置目录下的 `darktable`（`src/common/file_location.c:215`）。Windows 安装器开发版默认目录为用户 LocalAppData 下 `darktable`（`packaging/windows/darktable.iss.in:284`），自定义配置页会改变快捷方式参数（`packaging/windows/darktable.iss.in:333`）。不能仅凭安装目录猜照片库位置。

读取 `.github/workflows/personal-packages.yml`，检查目标 SHA 上的工作流也提供所需平台和 AI。当前工作流是 `codex/dev` push，Windows x64 `.exe`、macOS arm64 `.dmg`，AI 和模型下载编译开关开启；macOS deployment target 为 15.0。Skill/Markdown 改动会被 push paths-ignore 跳过。不要向不存在的 workflow_dispatch 接口发请求；不要为了安装创建空提交。

## 只读版本检查

发现 Python 3.10+ 和 Git。工作目录在仓库根时，PowerShell 示例：

```powershell
$skillRoot = (Resolve-Path '.agents/skills/darktable-package-install').Path
$settings = Get-Content -LiteralPath (Join-Path $skillRoot 'settings.local.json') -Raw | ConvertFrom-Json
$pythonExe = (Get-Command python -ErrorAction Stop).Source
$remote = $settings.remote
$branch = $settings.branch
$installedExe = [Environment]::ExpandEnvironmentVariables($settings.installed_executable)
$remoteRows = @(git ls-remote --exit-code $remote "refs/heads/$branch")
if ($LASTEXITCODE -ne 0 -or $remoteRows.Count -ne 1) { throw 'Cannot verify the selected remote branch' }
$remoteTip = ($remoteRows[0] -split '\s+')[0]
git fetch --no-tags $remote $branch
if ($LASTEXITCODE -ne 0) { throw 'Cannot fetch comparison history' }
& $pythonExe -B -X utf8 "$skillRoot/scripts/check_version.py" --repo . --target-ref "$remote/$branch" --remote-tip $remoteTip --installed-executable $installedExe
```

macOS 使用发现的 Python 3 和实际 app 路径，例如：

```bash
git ls-remote --exit-code origin refs/heads/codex/dev
git fetch --no-tags origin codex/dev
python3 -B .agents/skills/darktable-package-install/scripts/check_version.py \
  --repo . --target-ref origin/codex/dev --remote-tip "$remote_tip" \
  --installed-executable "$installed_executable"
```

上例的两个变量需要先从已核实的远端输出和实际安装位置赋值。远端在 ls-remote 与 fetch 之间更新会得到 `stale_target`，重新只读查询即可。Python 路径由本机发现；Windows 的商店占位 `python.exe` 不算已安装解释器。

脚本识别 `darktable <hash>` 和带 `~g<hash>` 的 git-describe 版本，解析规则对应 `tools/get_git_version_string.sh:3`。release 版本号、无法解析的 SHA、dirty 版本都标为 unknown，不做字典序或数值版本猜测。缺少提交对象时先补齐历史。`--version-file` 可检查已保存输出；`--installed-commit` 只接受有实际安装/包来源证据的完整 SHA，不能从未经验证的文件名推断。

| relation | 操作 |
| --- | --- |
| current | 无需重建或重装，检查功能异常时走诊断 |
| behind，software_changed=false | 仅 Skill/Markdown 或已识别的触发过滤改动，复用原版本，不重建 |
| behind，software_changed=true | 先找本地/云端已有包和进行中的构建 |
| ahead | 保留本机较新版，不默认降级 |
| diverged / unknown / stale_target | 核实来源、完整历史或远端，不重建 |
| missing | 先寻找已有包；不能仅因未安装就重建 |

`build_allowed_by_version=true` 只表明版本条件满足，不表示缺包、不表示 GitHub 查询完整、不表示获授权。首次安装缺少可复用包时，可把最新成功包的已核实 SHA 传给 `--installed-commit`，比较它与目标的关系。没有可核实的任何版本基线时先报告，不能凭空判定需要重建。

## 查找已有包与运行

先看 Git 忽略的 `build/personal-packages` 或本机 downloads_dir，并验证安装包来源记录、完整构建 SHA、文件哈希；单凭名称相似不足以信任它。再用当前可用的 GitHub connector 或已认证 CLI/API 检查云端。

REST 查询顺序是指定工作流的 runs、候选 run 的 jobs、artifacts。示例 endpoint：`GET /repos/{owner}/{repo}/actions/workflows/personal-packages.yml/runs?branch=codex%2Fdev&per_page=100&page=1`，随后 `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs` 和 `.../{run_id}/artifacts`。处理分页，不能因第一页为空就认定没有包。有些 connector 的 commit workflow runs 工具仅筛选 PR 事件，不能用它证明 push 工作流没有运行；先读当前工具说明。

包优先采用目标 SHA，也可采用版本脚本确认没有软件改变的祖先 SHA。脚本只忽略已识别的 Skill/文档 paths-ignore 块，不忽略工作流中的编译参数、依赖、打包步骤等变化。核实 run 的 head_branch/head_sha、仓库、工作流版本和对应平台 job 成功。另一个平台失败不代表已成功的平台包失效。要求 artifact 未过期（expired/expires_at），平台/架构正确，且实际包命名与内容一致；本地已校验缓存不因云端保留期到期而自动失效。相关 queued/in_progress/waiting 运行存在时等待，避免重复构建。不要安装另一个仓库、上游发布或其他分支的包来冒充个人版本。

已有包时直接下载到本机配置目录之外的缓存；保留原文件、run URL、完整构建 SHA、artifact ID、校验值和时间。当前 `upload-artifact@v7` 设置 `archive: false`，可能直接返回 `.exe/.dmg`；不要一律按 ZIP 解压。若返回 ZIP，验证摘要属于原下载对象，并检查解压路径、期望安装包数量，再校验包文件。对于直接上传的原文件，可把其 SHA-256 与 GitHub artifact digest 比较；zip 摘要不等于里面 exe 的摘要。[GitHub artifact API](https://docs.github.com/en/rest/actions/artifacts)、[upload-artifact 原文件说明](https://github.com/actions/upload-artifact/blob/main/README.md#upload-an-individual-file-unzipped)。没有服务器摘要时记录本地 SHA-256 与已核实来源，不能声称独立完整性校验通过。

只有版本落后、无可用包、无相关运行时才准备重建方案。若目标 SHA 已有运行但缺少该平台产物，获得当前会话授权后重跑该平台 job，并再次检查 SHA；不要无故重跑另一个平台。重跑仍失败时读相关 job 日志，定位一次再重试，持续相同失败就停止自动循环。新代码推送可触发构建，但提交、推送按 AGENTS.md 和本次授权办理。为了创建本 Skill，不启动构建。GitHub CLI 没安装不意味着必须安装它；connector 或已登录浏览器的产物下载也可使用，下载权限与公开元数据读取权限不同。

构建等待使用 bounded 状态查询，每次最多 60 秒，状态没变化则退避；给用户对应 run 链接和平台 job/失败摘要。下载采用能提供已下载字节数和总量的工具，结束后报告校验结果，不伪造进度和 ETA。

## 安装和备份

用户已要求安装时，准备好校验后的包，再核对安装位置、实际配置目录及权限。正在编辑/导入的应用不能强制结束；等待用户正常退出或明确允许关闭之后再升级。创建唯一的备份目录，复制真实配置目录，包括 library.db、data.db、相关 SQLite WAL/SHM（若存在）、darktablerc、Lua 和用户 presets/styles。应用关闭后复制，避免运行中跨文件不一致。不要复制 NAS 原片；不要把数据库放 Git。保留原版安装包或 app 及来源，记录版本和 SHA。

数据库可能在新版本首次启动时升级；恢复旧软件可能还需要恢复升级前数据库。不要未经要求覆盖新产生的编辑或数据库，不把安装文件回退等同于完整数据库回退。

### Windows x64

使用现有安装器升级目标目录，保留 release 与 dev 的不同 AppId，避免误覆盖另一份安装（`packaging/windows/darktable.iss.in:19`）。静默安装适用于现有默认配置已核实时；示例变量应采用本机的具体已核实路径：

```powershell
$arguments = @('/VERYSILENT', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', "/DIR=`"$installDir`"", "/LOG=`"$installLog`"")
$process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Installer returned $($process.ExitCode); inspect the log before proceeding" }
```

不能为绕过 UAC 更改系统安全设置；需要用户响应安装系统授权时说明当前操作。[Inno Setup 参数说明](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)。`/LOADINF` 不保证填入 darktable 自定义配置页；不要发明 `/CONFIGDIR` 参数。开发版安装器自定义页用于快捷方式 configdir，静默默认值可能与原有配置不同；用文件/快捷方式接口核对并保留实际 `--configdir`，启动时也传入该目录。禁止默认重新指向空库。

安装后从实际 installDir/bin 的程序重新读 `--version`，不能只看下载名、注册表或退出码。核对 GUI 版本输出 `AI -> ENABLED`，以及 bin 下 onnxruntime.dll。同版本禁用 AI 不自动构建；先核对设置与现有 AI 包。用户开启 AI 设置并提供模型后，成功加载/推理才算运行验证。

### macOS arm64

核对 `uname -m`、`sw_vers -productVersion` 与本包 deployment target；不把 arm64 包装到 Intel Mac，不为旧系统不兼容自动重建。用 `hdiutil attach -readonly -nobrowse -plist "$dmg"` 获取真实挂载点，检查其中唯一 darktable.app。检查 bundle 的实际版本、架构和 Contents/Resources/lib 下 libonnxruntime*.dylib。

正常退出 app 并完成配置备份后，把旧 app 保存到唯一备份位置；校验所有解析后的绝对路径都属于明确的安装/备份目录。用 `ditto` 将 app 复制到用户确认的目标（如 /Applications/darktable.app 或用户 Applications）；不要递归删除旧 app。完成后用 hdiutil detach 卸载本次挂载。保留现有启动配置和数据库路径。

个人云端包的签名、公证和 Gatekeeper 状态要实际检查；不默认移除 quarantine，不关闭 Gatekeeper。实际新 app 的 `--version`、库文件以及 AI 推理分别验证。当前 Windows 上不能证明 macOS 安装步骤已经跑通。

## 验证修改

运行 `python -B -X utf8 scripts/test_check_version.py`（工作目录为本 Skill）。测试用临时真实 Git 历史验证同版本、仅文档更新、代码更新、新版本、分叉、无法识别/dirty、远端不一致和包来源冲突。它不连接 GitHub、不构建、不安装。另用实际程序和刚核实的远端 SHA 执行 check_version.py。创建/修改此 Skill 时只做只读检查，不把安装升级作为验证副作用。
