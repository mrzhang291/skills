# 目标网页归档与 PDF 协议

## 归档层级

每个正式来源目录必须包含：

| 文件 | 作用 |
|---|---|
| `page.singlefile.raw.html` | SingleFile CLI 的原始输出，保留作归档副本 |
| `page.singlefile.html` | 去除脚本并加入严格 CSP 的安全离线阅读件 |
| `page.mhtml` | 与在线截图同一次 Chrome 会话生成的高保真主归档 |
| `response.html` | 可取得时保存服务器最终 HTML 响应 |
| `rendered-dom.html` | JS 执行后的 DOM；不是离线主件 |
| `body-text.txt` | 可见正文，用于相关性和离线保留率比较 |
| `fullpage.png` | 在线整页视觉记录 |
| `offline.png` | 禁网回放截图 |
| `page.pdf` | 单页来源的派生阅读件 |
| `page-images.json` | 图片 URL、尺寸和替代文字索引 |
| `page-links.json` | 页面链接索引 |
| `response-headers.json` | 去除敏感字段后的响应头 |
| `offline-validation.json` | 禁网回放结果 |
| `metadata.json` | URL、时间、重定向、状态、哈希和证据链 |

## SingleFile

固定调用 Skill `node_modules` 内的 `single-file-cli@2.0.83` 作为未修改外部进程，运行时不通过 npx 临时下载。启用 JS 加载和懒加载图片，再生成不压缩的单文件 HTML。保留其原始输出，并另生成移除脚本、加入严格 CSP 的安全回放件。

SingleFile 会二次访问页面并处理 DOM 和资源，某些防盗链图片可能变成占位图，因此不能替代同会话 MHTML。三者分别记录，不声称 SingleFile 是服务器原始源码。

在线截图、渲染 DOM 和打印 PDF 之前必须先遍历完整页面，提升常见懒加载属性（`data-src`、`data-lazy-src`、`data-ks-lazyload`、`data-original`、`data-srcset`），等待实质图片的 `naturalWidth`/`naturalHeight` 与解码结果稳定。初次检查未达到 85% 加载率且仍有两个以上实质图片未加载时，只允许一次受控补偿滚动；初始值、最终值、加载比例和是否补偿必须写入 `metadata.lazy_image_hydration`。这项处理只能恢复页面已声明的资源地址，不能为旧截图或服务器未返回的图片编造内容。

以下交互登录能力仅限用户明确启用的 Forensic；销售平台 Quick 登录态走 SKILL.md 的专用分支。登录态页面应使用运行目录之外的专用非默认 `user-data-dir`。Playwright 的在线抓取 context 必须先关闭，随后 Skill 先启动不含 `--single-process` 的浏览器 CDP，再让未修改的 SingleFile 2.0.83 通过 `--browser-server` 顺序复用该 profile，避免 profile lock 与 Edge 150 崩溃。`--headed --wait-for-unblock-ms <N>` 同时让在线抓取与 SingleFile 二次归档具备明确的人工解锁窗口；等待为零时不暂停。不要直接复用仍在运行的日常 Profile，也不要复制、解密或把 Profile、Cookie、storage state 放进证据运行目录。

SingleFile 报告必须记录是否应用 profile 参数、headless 状态、等待毫秒数、加载与捕获超时，但不得把 Cookie 内容写入报告。

## 离线回放

使用 Playwright：

1. 对浏览器设置 offline；
2. 拦截所有 `http://` 和 `https://` 请求；
3. 用 `file://` 打开 SingleFile HTML；
4. 记录任何外部请求尝试；
5. 比较在线/离线可见文本三字符片段；
6. 比较实质图片数量和加载状态；
7. 检查目标文字断言；
8. 保存离线截图。

正式来源要求 MHTML 与安全 HTML 的外部请求数为 0、正文保留率不低于 0.65；MHTML 实质图片保留率不低于 0.8，且 MHTML 离线截图与在线截图像素相似度不低于 0.90。纯图形/图片主导页面可以不要求文字命中，但必须进入视觉人工复核。

## PDF

先消除动画、fixed 和 sticky 重复布局，再按浏览器打印为 A4；原生打印失败时才从整页截图生成 PDF。打印后检查每一页，而不是只删除连续尾页。

硬空白页参考：可提取文字少于 40 且非白像素比例低于 0.015。重复低信息页参考：相邻页文字少于 500、正文集合相似度不低于 0.95。任何正式来源仍含空白、重复导航或占位页时，验证失败。

最终卷宗包含封面、目录和正式来源 PDF。目录页码、构建记录和实际页数必须完全一致。搜索页和阻断页不得用占位页进入卷宗。

用户明确要求“全部检测 HTML 自包含 PDF”时，汇编器还必须：

- 把每个已验收来源的原始 HTML 全量嵌入为 PDF 附件并逐项复核 SHA-256；
- 在平台目录和每个来源页页脚创建可点击的 URI 链接，不能只画蓝色文字；
- 每一页页脚记录存储时间、来源网址和全局页码；目录/说明页没有单一网址时明确写“本页为目录或汇编说明”；
- 每个非参考平台的目录加截图正文总页数默认不超过 25 页，调用者只可在 20–30 页内调整；视觉页按来源公平分配，超预算只截短截图正文，不删除 HTML 附件；
- 在 manifest 中记录各平台预算、实际页数、截短页数、可点击网址验证、逐页时间戳验证和阻断页排除结果。

## 完整性与隐私

- 为每个文件记录 SHA-256 和字节数；提升候选、生成清单和最终验证时分别重算。
- 记录 requested、normalized、final URL 和重定向链。
- 响应头中的 `set-cookie`、authorization 和 API key 字段必须删去或标为 `[redacted]`。
- Cookie、storage state、浏览器 profile、密码和 API Key 不得位于运行目录或 manifest。
- WACZ/Scoop 可作为未来更强归档后端，但 WACZ 签名本身不自动等于法律上的真实性认定。
- 完成阶段使用运行目录同卷的持久 journal 和备份恢复多文件更新；这是可恢复提交，不是文件系统提供的一条多文件原子指令。异常中断后必须先恢复或清理 `.finalize-transaction`，最终交付中不得残留该目录。
- Manifest 和 SHA-256 只能发现交付包内的意外变化或错配；本 Skill 不提供外部签名、可信时间戳或第三方存证。用于法律取证时，应另行接入可信时间戳、数字签名/电子存证并保留签名验证材料。
