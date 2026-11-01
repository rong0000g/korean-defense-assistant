# 韩语答辩助手

这是在 Windows 电脑运行的韩语学术答辩文字助手。iPhone Safari 负责收音和显示；电脑调用 API 将韩语转写、翻译成中文，并为问句生成简短的韩语和中文回答。没有语音播放功能。

## 朋友自己运行

1. 在 Windows 电脑上解压整个文件夹，安装 Python 3.10 或更新版、Cloudflare 的 cloudflared。
2. 将 `defense_context.example.txt` 复制为 `defense_context.txt`，将 `defense_qa.example.txt` 复制为 `defense_qa.txt`。在自己的文件里写研究题目、方法、已核实的事实、结论边界和常见问答，不要写未经核实的系数或结果。这两个文件在运行时会发给所选模型，请只放愿意提交给 API 服务商处理的材料。个人材料文件已被 `.gitignore` 排除。
3. 在 Groq Console 自己创建 API Secret Key。不要使用分享者的密钥，也不要把密钥发给别人。
4. 在解压目录打开 PowerShell，执行 `powershell -ExecutionPolicy Bypass -File .\start.ps1 -Tunnel`。在弹窗中粘贴自己的完整 `gsk_` 密钥。
5. 扫描电脑出现的新二维码，在 iPhone Safari 打开，允许麦克风。先点“测试完整提问”，再用韩语语音测试。每次重启程序都要用新的二维码。

答辩前用真实手机和现场网络测试收音、转写与回答，并准备离线问答备份。Cloudflare Quick Tunnel 是临时连接，没有在线保证；Groq 免费 API 有速率和额度限制。电脑、网络、PowerShell 窗口和手机页面都必须保持可用，iPhone 收音页面需在前台。此程序不保证回答正确，涉及研究事实与统计结果时应以自己的论文和原始分析为准。

仓库只含空白研究材料模板，没有论文正文、API 密钥、证书、二维码或历史答辩记录。填写个人材料后不要强制提交到公开仓库。
