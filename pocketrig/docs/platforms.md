# 按宿主平台部署

工作站（中继入口）可以是三种宿主之一。成员侧的使用方式完全相同：连 Tailscale → 打开本人 Paseo 端口 → SSH 到计算节点。工作站在公司内网时，也可以用公司 VPN 代替 Tailscale，见[第四节](#四用公司-vpn-代替-tailscale)。差别只在管理员侧：用户如何创建、实例由谁托管、Tailscale 入口如何进入 Paseo。

| | Windows + WSL2 | 原生 Linux | macOS |
| --- | --- | --- | --- |
| Tailscale 装在 | Windows | 本机 | 本机 |
| Paseo 监听 | WSL 内 `127.0.0.1:<端口>` | 本机 Tailscale 地址，或 127.0.0.1 + `tailscale serve` | 同原生 Linux |
| 入口转发 | Windows portproxy（`windows/` 自动维护） | 不需要 | 不需要 |
| 实例托管 | systemd `paseo@<user>` | systemd `paseo@<user>` | launchd LaunchDaemon（每用户一个 plist） |
| 新增用户 | `paseo-user add` | `paseo-user add` | 手动步骤（见下文） |
| 常驻 | Windows 计划任务保持 WSL 存活 | systemd 开机自启 | launchd 开机自启 + 关闭睡眠 |
| SSH 入口 | `<TS_IP>:2222 → WSL:2223` | 本机 sshd（通常 22） | “远程登录” |
| 可选出口网关 | 适用 WSL2 NAT，真实节点仍待验收 | 本机运行 tailscaled 时不支持 | 不支持（依赖 nftables） |
| 验证状态 | 参考部署运行在此平台；仓库安装流程待完整验收 | 安装分支有模拟测试，待实机验收 | 安装分支与 plist 有静态/模拟测试，待实机验收 |

公共部分（Node、Paseo、Claude Code、Codex、包装脚本、启动前检查、使用约定）在三种平台上相同，都由 `linux/install.sh` 安装（该脚本也支持 macOS）。

---

## 一、Windows + WSL2

见[管理员部署指南](admin-guide.md)第 1–3 节，这是本项目的主要场景。要点：WSL 不能直接绑定 Windows 的 Tailscale 地址，所以由 `windows/` 中的计划任务把 `<Tailscale IP>:<端口>` 转发进 WSL，并保持 WSL 实例不被回收。

---

## 二、原生 Linux

适用于常开的 Linux 台式机或服务器（Ubuntu / Debian / Fedora 等，需要 systemd）。不需要 `windows/` 目录下的任何内容。

### 1. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up --advertise-tags=tag:paseo-ws
tailscale ip -4          # 记下 100.x.y.z，下文记作 <TS_IP>
```

在 Tailscale 管理后台核对这台常驻机器的 key expiry，并按组织要求安排续期。先在策略中定义 `tag:paseo-ws`，再发布这个标签。按 [`templates/tailscale-policy.example.hujson`](../templates/tailscale-policy.example.hujson) 配置授权，SSH 端口改为本机实际端口（通常 `tcp:22`）。

### 2. 公共部分

```bash
sudo ./linux/install.sh --install-node 24.22.1 --install-packages
```

### 3. 选择监听方式

**方式 A：直接监听 Tailscale 地址（简单）**

编辑 `/etc/paseo-workstation/workstation.conf`：

```bash
PW_LISTEN_HOST=<TS_IP>
```

实例只在 tailnet 地址上可达，局域网和公网都访问不到。开机时 tailscaled 还没就绪的话，服务会按 `Restart=on-failure` 每 10 秒重试一次，直到成功。如果本机 Tailscale 地址变了（比如重新注册设备），修改此项后对每位成员重新执行一次 `paseo-user add <user> <port>`。

**方式 B：保持 127.0.0.1，由 `tailscale serve` 转发（地址变化无需改配置）**

保持 `PW_LISTEN_HOST=127.0.0.1`，为每个端口配置一条 TCP 转发：

```bash
sudo tailscale serve --bg --tcp=6767 tcp://127.0.0.1:6767
sudo tailscale serve --bg --tcp=6768 tcp://127.0.0.1:6768
sudo tailscale serve status
```

`--bg` 让配置在退出终端及设备重启后继续生效，访问范围仍由 tailnet 策略控制。停用成员时执行 `sudo tailscale serve --bg --tcp=6767 off`，只移除该端口。不要用 `serve reset` 清除其他成员的转发。参数适用于现代 Tailscale（1.52+）；参见[官方 Serve 文档](https://tailscale.com/docs/reference/tailscale-cli/serve)。

### 4. SSH

参考 [`linux/sshd-00-paseo-workstation.conf.example`](../linux/sshd-00-paseo-workstation.conf.example)，但把 `Port 2223` 改成本机实际端口（如 22），并先把管理员写进 `AllowUsers`：

```bash
sudo sshd -t && sudo systemctl reload ssh     # Fedora 等为 sshd
```

也可以改用 Tailscale SSH，由 tailnet 的独立 `ssh` 策略控制允许登录的 Linux 用户；模板中的 TCP grants 本身不授予 Tailscale SSH 登录权限。启用前按[官方 Tailscale SSH 文档](https://tailscale.com/docs/features/tailscale-ssh)配置，并对 `paseo-user add` 使用 `--no-ssh`。

### 5. 新增成员

```bash
sudo paseo-user add alice 6767
sudo passwd alice
```

其余步骤（成员登录模型账号、配置计算节点 SSH）与[部署指南](admin-guide.md)第 2 节相同。

### 6. 主机防火墙（如已启用）

方式 A 下 Paseo 只绑定在 Tailscale 地址上。如果启用了 ufw 或 firewalld，需要放行 `tailscale0` 接口上的端口：

```bash
sudo ufw allow in on tailscale0 to any port 6767 proto tcp
```

firewalld 为 `tailscale0` 使用单独的 zone，只开放成员端口和实际 SSH 端口；不要把整个接口直接加入全端口放行的 trusted zone。

### 7. 常驻与验收

- `paseo@<user>`、`tailscaled`、`ssh` 都设为 enable 状态，开机后自动启动，无需任何人登录。
- 按设备用途设置电源策略，确保部署期间不自动休眠。
- 按[验收清单](acceptance-checklist.md)验收，确认冷启动无人登录、服务重启和 Tailscale 重连后都能访问。网络重连测试通过本机控制台进行，避免断开唯一的管理入口。

### 8. 出口网关

[可选出口网关模块](egress-proxy.md)的防直连规则会拦截本机 tailscaled 的出站流量，导致 tailnet 断开。原生 Linux 上请不要启用该模块，网络出口改用宿主或路由器层面的方案。

---

## 三、macOS

适用于常开的 Mac mini / Mac Studio 等台式机。本文选用的 Node 24 官方二进制要求 macOS 13.5 或更新版本，支持 Intel x64 和 Apple Silicon arm64，见 [Node 24 平台要求](https://github.com/nodejs/node/blob/v24.22.1/BUILDING.md#platform-list)。每位成员是一个 macOS 本地用户，实例由 launchd 以该用户身份托管。`paseo-user` 和 systemd 相关部分不适用，按下面的步骤手动配置。

> 本节按 macOS 与 Paseo 的公开机制整理，尚未在实机上完整验收。部署后请严格按验收清单逐项测试，并欢迎提交修正。

### 1. 电源与常驻

```bash
pmset -g custom                # 先检查现有电源策略
sudo pmset -c sleep 0          # 接电时不自动系统睡眠，按实际用途设置
```

在“系统设置 → 通用 → 共享”中打开“远程登录”，只授权实际成员。FileVault 启用时，冷启动通常需要先解锁磁盘，不能仅靠 LaunchDaemon 保证无人值守恢复；保留原有加密策略，把解锁流程纳入运维安排。笔记本合盖也可能休眠，不能仅凭 `sleep 0` 保证合盖在线。

### 2. Tailscale

无人登录也需要联网的场景使用开源的 `tailscaled` 版本，由系统守护进程启动。App Store 版和独立 GUI 版不提供登录前运行；不要在同一台机器同时运行多个变体。参见[官方 macOS 变体对照](https://tailscale.com/docs/concepts/macos-variants)。

```bash
brew install tailscale
sudo "$(command -v brew)" services start tailscale
sudo "$(command -v tailscale)" up --advertise-tags=tag:paseo-ws
tailscale ip -4                     # <TS_IP>
```

先安装 [Homebrew](https://brew.sh)，上述服务注册方式见 [Homebrew Tailscale formula](https://formulae.brew.sh/formula/tailscale)。Apple Silicon 的 Homebrew 通常位于 `/opt/homebrew`，所以用完整路径执行 sudo。授权配置与原生 Linux 相同（SSH 为 `tcp:22`）。

### 3. 公共部分

先安装 Xcode 命令行工具（`xcode-select --install`），确认 `python3`、`git`、`curl` 可用。安装脚本使用 `.tar.gz`，不需要额外安装 xz。脚本语法兼容系统 Bash 3.2；实际平台行为仍需在目标 Mac 验收。

```bash
sudo ./linux/install.sh --install-node 24.22.1 --install-packages
sudo dseditgroup -o create ai-cli-users          # 首次创建公共 CLI 用户组；已存在则跳过
```

如果开启了 macOS 防火墙，需要放行公共 Node，否则外部连接会被拦截：

```bash
NODE=/usr/local/lib/node-shared/bin/node
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$NODE"
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$NODE"
```

### 4. 新增成员（以 alice、6767 为例）

**管理员：创建用户与目录**

```bash
U=alice
sudo sysadminctl -addUser "$U" -fullName "$U" -password -     # 按提示设置登录密码
sudo dseditgroup -o edit -a "$U" -t user ai-cli-users
sudo dseditgroup -o edit -a "$U" -t user com.apple.access_ssh   # “远程登录”仅允许指定用户时需要
sudo chmod 700 "/Users/$U"                                       # macOS 默认 HOME 为 755
sudo -u "$U" mkdir -p "/Users/$U/.paseo" "/Users/$U/projects" "/Users/$U/results"
sudo chmod 700 "/Users/$U/.paseo" "/Users/$U/projects" "/Users/$U/results"
sudo install -d -m 700 -o root -g wheel /var/log/paseo-workstation   # launchd 日志目录，仅管理员可读
```

**成员本人（或管理员 `sudo -iu alice`）：配置实例与密码**

macOS 的默认 shell 是 zsh，下面的密码保存命令使用 Bash，所以先输入 `bash`，再执行下面的命令。把 PATH 这一行也加入本人登录 shell 的配置（zsh 是 `~/.zprofile`，Bash 是 `~/.bash_profile`），让终端使用公共入口：

```bash
export PATH="/usr/local/lib/paseo-workstation/bin:/usr/local/lib/node-shared/bin:/usr/local/bin:$PATH"
umask 077
paseo daemon config set daemon.listen '<TS_IP>:6767'
paseo daemon config set daemon.relay.enabled false
paseo daemon config set features.webUi.enabled true
paseo daemon config set features.dictation.enabled false
paseo daemon config set features.voiceMode.enabled false
paseo daemon config set agents.providers.claude.command '["/usr/local/lib/paseo-workstation/bin/claude"]'
paseo daemon config set agents.providers.codex.command '["/usr/local/lib/paseo-workstation/bin/codex"]'
paseo daemon set-password              # 交互输入两次连接密码
# 把同一个密码保存到本人 client.env，供终端里的 paseo 命令自动认证
read -r -s -p 'Paseo password: ' P; echo
printf 'export PASEO_PASSWORD=%q\n' "$P" > ~/.paseo/client.env; chmod 600 ~/.paseo/client.env; unset P
/usr/local/share/paseo-workstation/sync-agent-policy.sh
claude auth login
codex login
```

`<TS_IP>:6767` 替换为该成员的实际监听地址，与下一步的 `LISTEN` 保持一致。采用 Serve 时，两处都写 `127.0.0.1:6767`，再按 Linux 方式 B 配置转发。`PW_NODE` 使用自定义路径时，PATH 中的 Node 目录也相应修改。

**管理员：注册 launchd 实例**

`__LISTEN__` 填 `<TS_IP>:6767`（直接监听 Tailscale 地址），或填 `127.0.0.1:6767` 并配合 `tailscale serve`，两种方式的取舍同原生 Linux 第 3 节。

```bash
U=alice; LISTEN="<TS_IP>:6767"
PLIST="/Library/LaunchDaemons/sh.paseo.workstation.$U.plist"
sed -e "s/__USER__/$U/g" -e "s/__LISTEN__/$LISTEN/g" macos/sh.paseo.workstation.USER.plist.template | sudo tee "$PLIST" >/dev/null
sudo chown root:wheel "$PLIST" && sudo chmod 644 "$PLIST"
plutil -lint "$PLIST"
sudo launchctl bootstrap system "$PLIST"
sudo launchctl print "system/sh.paseo.workstation.$U" | grep -E 'state|pid'
```

### 5. 日常运维

| 操作 | 命令 |
| --- | --- |
| 查看状态 | `sudo launchctl print system/sh.paseo.workstation.alice` |
| 重启实例 | `sudo launchctl kickstart -k system/sh.paseo.workstation.alice` |
| 查看日志 | 管理员 `sudo tail -n 80 /var/log/paseo-workstation/alice.log`；成员本人 `tail -n 80 ~/.paseo/daemon.log` |
| 重设连接密码 | 成员执行 `paseo daemon set-password` 并更新 `client.env`，管理员 kickstart 重启实例 |
| 修改端口 | 修改 plist 中的 `PASEO_LISTEN`，先 `bootout` 再 `bootstrap` |
| 停用成员 | `sudo launchctl bootout system/sh.paseo.workstation.alice`，删除 plist；按需执行 `dseditgroup -o edit -d alice -t user ai-cli-users`。HOME 保留 |

注意事项：

- 如果管理员本人也在这台 Mac 上用 Paseo 桌面应用，桌面应用会为当前登录用户另起一个实例。请避开它占用的端口，也不要再为管理员注册同端口的 LaunchDaemon。
- `PASEO_LISTEN` 由 plist 提供，优先级高于 `~/.paseo/config.json` 中的监听设置。
- 只有 `/Users` 下的内容受上述权限保护。外接数据盘若格式化时选择了“忽略此宗卷上的所有权”，chmod 不会生效。
- LaunchDaemon 不读取 `.zprofile` / `.bash_profile`，也不等同于成员的图形登录会话。必须在该服务里实际验证模型认证；终端登录成功不代表后台服务能访问同样的凭据或钥匙串。不要为此共享其他成员凭据，或把令牌写进公开可读的 plist。
- 启动后用 `paseo provider diagnostic claude` / `codex` 检查包装脚本路径，再通过 Paseo 完成一次模型调用。失败时先查 `/var/log/paseo-workstation/<user>.log`（管理员）和本人 `~/.paseo/daemon.log` 中的认证错误，并按本人所用账号的后台认证方式配置。

### 6. 验收

按[验收清单](acceptance-checklist.md)执行，并另外确认以下几点：

- 实例进程属于成员本人：`ps -o user= -p <pid>`；
- 冷启动后无人登录时，SSH 和全部 Paseo 端口都可以访问；
- 长时间空闲后仍可连接；FileVault 解锁、笔记本合盖、电源恢复按实际设备分别记录结果。

---

## 四、用公司 VPN 代替 Tailscale

工作站放在公司内网、手机和笔记本本来就能通过公司 VPN 进入这个内网时，可以不装 Tailscale：Paseo 直接监听工作站的内网地址，设备连上公司 VPN 后访问它。其余部分（公共安装、每人独立实例、包装脚本、计算节点 SSH）不变。

> 这条路线由现有配置项支持，尚未实机验收；部署后按本节末尾的验收项逐条确认。

### 1. 先确认

| 项 | 说明 |
| --- | --- |
| 公司政策 | 允许在这台机器上长期运行服务并开放端口；不确定就先问 IT |
| 内网地址 `<LAN_IP>` | 工作站在公司内网的 IPv4 地址，最好请 IT 固定（静态地址或 DHCP 保留） |
| VPN 网段 `<VPN_CIDR>` | VPN 客户端拿到的地址范围，例如 `10.8.0.0/16`，向 IT 确认 |
| 可达性 | 手机连上公司 VPN 后，能访问 `<LAN_IP>` 的端口（有的 VPN 只放行部分网段或端口） |

### 2. 让实例监听内网地址

原生 Linux：编辑 `/etc/paseo-workstation/workstation.conf`，然后为每位成员执行一次 `paseo-user add`（已有成员重新执行即可，连接密码保持不变，服务会自动重启）：

```bash
PW_LISTEN_HOST=<LAN_IP>
```

```bash
sudo paseo-user add alice 6767
```

macOS：第三节里的 `daemon.listen` 和 launchd 的 `__LISTEN__` 都写成 `<LAN_IP>:6767`。

**不要**改成监听 `0.0.0.0`：那样实例会出现在工作站的所有网卡上，包括可能存在的公网或访客网络。

### 3. 用主机防火墙代替按人授权

Tailscale 可以按人放行端口，公司 VPN 通常做不到：同一个 VPN 里的所有同事都能连到这个地址。所以 Paseo 密码成了主要门槛，并且建议在工作站上只放行 VPN 网段：

```bash
# ufw
sudo ufw allow from <VPN_CIDR> to any port 6767 proto tcp
# firewalld
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="<VPN_CIDR>" port port="6767" protocol="tcp" accept'
sudo firewall-cmd --reload
```

SSH 端口同样只放行 VPN 网段。macOS 自带的应用防火墙按程序而不是网段过滤，需要按网段限制时请 IT 在网络侧处理，或自行配置 pf。

### 4. 客户端

- 手机和笔记本先连公司 VPN，再在 Paseo 中添加 Host：`<LAN_IP>` / 本人端口，密码同前。
- Use SSL 仍保持关闭。注意加密范围的变化：公司 VPN 只加密设备到公司网络这一段，公司内网里的这一段没有额外加密，安全程度取决于内网本身。对此有顾虑时继续使用 Tailscale。
- 成员不需要安装 Tailscale，交付单里把 Tailscale 相关项换成「公司 VPN + `<LAN_IP>`」。

### 5. WSL2 上的限制

Windows 侧的同步脚本（`windows/`）只读取 Tailscale 地址，不会为内网地址建立转发。WSL2 部署想走公司 VPN，需要自己在 Windows 上建 `netsh interface portproxy`（`<Windows 内网 IP>:6767 → 127.0.0.1:6767`）和只放行 VPN 网段的防火墙规则；常驻任务在未安装 Tailscale 时的行为也未验证。建议这条路线优先用在原生 Linux 或 macOS 上。

### 6. 验收

- 手机连上公司 VPN 后能打开本人 Paseo，并完成[首个任务与接续验证](../skills/pocketrig-connect/SKILL.md)；
- 断开 VPN 后连不上；
- 用另一台不在 VPN 网段的内网机器访问端口，应被防火墙拒绝；
- 工作站重启、内网地址续租后仍能访问（地址变了要改 `PW_LISTEN_HOST` 并重新执行 `paseo-user add`）。

## 参考

- [Apple：Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- [Paseo：前台服务与配置](https://paseo.sh/docs/cli)
- [Node.js 官方发行版本](https://nodejs.org/dist/index.json)
