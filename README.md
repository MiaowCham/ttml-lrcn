# Apple Music TTML 转 LRCN

将 Apple Music 使用的 TTML 歌词转换为 Lyrics Next v2（`.lrcn`）。脚本只依赖 Python 3.9 或更高版本的标准库。

关于 Lyrics Next，[点我查看](https://docs.miaowcham.com/docs/Lyrics_Next/v2)

Codex 写的，可能会在未来重构。

<details>
<summary>点击查看使用说明</summary>

## 使用

Windows 下可直接双击 `启动GUI.pyw` 启动固定大小的图形界面，不会显示终端窗口；也可以使用 `pythonw 启动GUI.pyw`。将 `.ttml` 文件拖到仓库中的 `启动GUI.pyw` 上，会直接打开表单并预填输入路径，原文件不会被移动或复制。

```powershell
# 不提供输入文件：直接打开表单，在其中输入路径或点击“浏览”选择 TTML
python .\ttml_to_lrcn.py

# 交互模式：读取 TTML 后显示可鼠标点击、键盘操作的图形表单
python .\ttml_to_lrcn.py input.ttml

# 强制使用传统终端问答
python .\ttml_to_lrcn.py input.ttml --text-interactive

# 非交互批量转换
python .\ttml_to_lrcn.py input.ttml --non-interactive -o output.lrcn

# 输出纯 LRC（兼容扩展）
python .\ttml_to_lrcn.py input.ttml --non-interactive `
  --translation-output none --transliteration none --background-mode omit `
  --no-metadata --no-lyrics-marker --no-song-part `
  --no-line-end --no-agent --no-line-id --no-syllable-end --no-first-syllable-tag `
  --trailing-end-marker -o output.lrc --force

python .\ttml_to_lrcn.py input.ttml -o output.lrcn
python .\ttml_to_lrcn.py input.ttml --stdout

# Lyricify Quick Export
python .\ttml_to_lrcn.py input.ttml --non-interactive --fake-lqe
```

无论是否在命令行提供输入文件，图形表单顶部都会提供 TTML 路径输入框和“浏览”按钮；确认后才会读取并生成文件。表单可用鼠标点击，也可通过 Tab、方向键和空格键操作。标准输入不是终端（例如 CI 或管道）时会自动使用非交互默认值，且必须提供输入文件。`--text-interactive` 可强制传统终端问答；图形环境或 `tkinter` 不可用时，脚本会自动降级到终端问答。输出使用 UTF-8 编码和 LF 换行。

在图形表单中，普通点击“开始转换”会导出文件；按住 Shift 点击则只将结果写入系统剪贴板，不创建输出文件。两种操作完成后表单都会保持打开，可继续转换；关闭窗口才会结束 GUI 运行。

## 兼容选项

| 参数 | 作用 |
| --- | --- |
| `--translation lnt-full\|lnt-short\|lrc` | 附属歌词标签格式：LNT 完整、LNT 精简或 LRC |
| `--transliteration lrcn\|lrc\|both\|none` | 发音输出：按节拍划分、逐行、按节拍划分加逐行或不输出；标签格式为 LRC 时只能逐行或不输出 |
| `--[no-]metadata` | 保留或省略顶部 Lyrics Next 元数据 |
| `--[no-]lyrics-marker` | 保留或省略主歌词格式声明 |
| `--[no-]song-part` | 保留或省略 `[Verse]`、`[Chorus]` 等结构 |
| `--[no-]line-end` | 保留或省略主歌词行的 end |
| `--[no-]agent` | 保留或省略演唱者 ID |
| `--[no-]line-id` | 保留或省略歌词行 ID |
| `--[no-]syllable-end` | 保留或省略 `<start,end>` 中的 end |
| `--[no-]first-syllable-tag` | 保留或省略每行首个可推断的节拍标签 |
| `--background-mode keep\|normal\|omit` | 保留 `[x-bg]`、改为普通歌词行或省略背景人声 |
| `--no-background` / `--background` | 分别省略/保留背景人声的兼容旧参数 |
| `--[no-]trailing-end-marker` | 未保留行 end 时，是否在行尾追加结束时间戳 |
| `--timing-tag-style angle\|square\|parenthesis` | 兼容旧用法；`parenthesis` 等同于选择 QRC 逐拍格式 |
| `--compatibility-format enhanced\|eslrc\|qrc\|lys\|lqe` | 自动调整主歌词为增强 LRC、ESLRC、QRC、LYS 或 LQE 格式 |
| `--translation-output lrc\|none` | 是否输出翻译内容 |
| `--no-embed-attachments` | 不将翻译、音译内嵌在主歌词中 |
| `--write-translation-track` / `--write-transliteration-track` | 额外生成 `_trans.lrc` / `_pron.lrc` 独立轨道 |
| `--fake-lqe` | 使用 Lyricify Quick Export 兼容预设，输出 `.lqe` |
| `--lqe-format qrc\|lys` | 已弃用；LQE 固定使用 Lys |
| `--[no-]attachment-language` | 保留或省略翻译、发音区段的 `[lang:...]` |
| `--[no-]translation-language` / `--[no-]transliteration-language` | 分别控制翻译、音译标签是否写入语言信息 |
| `--force` | 覆盖已存在的输出文件 |

“保留翻译和发音区段的语言”位于表单的“翻译与发音”区块。未指定的选项在交互模式下逐项询问；在非交互模式下附属歌词标签默认采用 LNT 完整，发音默认同时输出按节拍和逐行格式。若标签格式为 LRC，发音只能逐行或不输出；若不输出附属歌词，发音也会关闭。若省略主歌词 `line`，LNT 完整/LNT 精简和逐拍发音均不可用，脚本会拒绝冲突的参数。

主行扩展字段按需紧凑省略，例如：

```lrcn
[8.175,11.604,v1,L1]  # 完整
[8.175,11.604,,L1]    # 省略 agent
[8.175,,v1,L1]        # 省略 end
[8.175]               # 省略 end、agent、line
```

按照最新版 v2 规范，只要保留 `line`，省略的 `end` 或 `agent` 必须保留其空字段和逗号。启用 `--no-first-syllable-tag` 时，仅当首个节拍与行同时开始且后面还有节拍可用于推断结束时间，脚本才会安全省略首个 `<start,end>` 标签。

保留 `line` 时，脚本会拒绝 `x-bg`、纯数字、类似 SMIL 时间值或重复的行 ID，避免生成无法可靠关联附属歌词的 LRCN Trans。

交互模式中的背景人声选项包含“保留 `[x-bg]`”“改为普通歌词行”和“不输出”。普通行会生成 `B1`、`B2` 等独立行 ID，继承所属主句的 agent；翻译和发音中的对应背景条目也会同步改为该普通行的关联格式。

兼容格式会自动切换为所需字段组合：增强 LRC 与 ESLRC 省略行 `end`、`agent`、`line`、逐字 `end`、首个节拍标签，并将背景人声改为普通行（或省略），随后在行尾追加结束时间戳；QRC 与 Lys 改用毫秒与后置 `(开始,持续时间)` 标签。保留主歌词格式声明时，分别写入对应的格式名称。若仍保留主歌词字段或输出翻译/发音，默认后缀仍为 `.lrcn`；只有纯 LRC 内容才默认使用 `.lrc`。

转换内容包括：

- TTML 行时间、逐词/逐节拍时间及原始空格；
- `itunes:song-part` 歌曲段落（使用 `[Verse]` 等简写格式）；
- `ttm:agent` 演唱者和 `itunes:key` 行 ID；
- `ttm:role="x-bg"` 背景人声；
- Apple `iTunesMetadata` 中按行 ID 关联的翻译和发音；
- 秒数、SMIL 时钟值以及 TTML 的帧、tick 等时间表达式。

输出采用完整 LRCN v2 标签，`end` 按绝对结束时间处理。若输入行没有 `itunes:key`，脚本会按正文顺序生成 `L1`、`L2` 等 ID。
输出时间使用无前导零的紧凑 SMIL 表示，例如 `8.175`、`1:02.330`、`1:02:03.004`。

逐拍歌词的空格会按 XML 原样保留：空格既可以位于 `span` 文本内部，也可以位于相邻两个 `span` 之间。脚本不会自动添加、删除或重新分配音节间空格。

内嵌翻译和逐拍发音共用所选标签格式：LNT 完整为 `[start,line]…`，LNT 精简为 `[line]…`；背景附属歌词使用 `[x-bg]`。

每个逐拍发音区段之后还会自动生成一个 `[transliteration: format@LRC]` 逐行发音区段。脚本提取非背景的叶子音节、清理音节边缘已有空白，再用单个空格拼接，例如 `[8.175]ashi moto ni chirabaru kotoba`。

“兼容格式”可直接选择“不使用（LRCN）”、增强 LRC、ESLRC、QRC 或 Lys；强制字段在表单中会灰显。增强 LRC / ESLRC 会自动省略主行扩展字段、将背景人声改为普通行（或省略）并追加对应样式的行尾结束时间；QRC / Lys 会自动使用整数毫秒、行和逐字的持续时间，并把逐字 `(开始,持续时间)` 放在音节之后。未内嵌翻译或音译时，四种兼容格式的主文件后缀依次为 `.lrc`、`.lrc`、`.qrc`、`.lys`。

翻译与音译可内嵌到主歌词，也可额外导出独立轨道；仅导出独立轨道且未选择兼容格式时，主歌词使用 `.lnt`，翻译和音译文件分别以 `_trans.lrc`、`_pron.lrc` 结尾。翻译、音译的语言信息使用各自独立的开关。

选择兼容格式中的 “LQE” 时，输出头固定为 `[Lyricify Quick Export]` 与 `[version:1.0]`，主歌词使用 Lyricify Syllable（Lys）格式，输出后缀固定为 `.lqe`。该预设会固定保留顶部数据、主歌词声明、行 `end`、逐字 `end`，并省略歌曲结构、agent、line ID；主歌词所有时间戳均使用整数毫秒，行和逐字的第二个时间值均改为持续时间，逐字标签以 `(开始,持续时间)` 的形式置于音节之后。不会追加行尾时间戳，也不会省略首个时间戳。翻译保持标准 LRC 的补零秒制时间标签，发音可选逐行或不输出。背景人声不能保留为 `[x-bg]`，可改为普通行或省略。

LQE 使用 Lyricify Syllable 行属性：普通歌词为 `[4]`（左）或 `[5]`（右），背景人声为 `[7]`（左）或 `[8]`（右），并继承所属主句的方向。首个正常 Agent 默认左侧，但 Agent ID 为 `v2` 时默认右侧；后续正常歌词的 Agent ID 改变时切换方向。`type="group"` 固定左侧、`type="other"` 固定右侧，二者均不会影响下一句正常歌词的比较基准。

</details>

## License

本项目使用 [MIT License](LICENSE) 进行许可。
