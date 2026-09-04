# 鼠标指示 (Mouse Pointer)

Windows 上的白手套鼠标指针定位工具。按下全局热键，在指针位置弹出一只米老鼠风格的白手套，食指精确指向指针；移动鼠标或再按一次热键即隐藏。平时无覆盖层，不影响正常操作。

## 功能

- **一键定位**：默认 `F10` 弹 / 收白手套
- **指向精准**：指尖像素级对齐指针
- **热键可改**：面板内按一键即替换，支持字母 / 数字 / F1–F12 / 方向键 / 标点 / 编辑区 / 小键盘
- **开机启动**、**系统托盘**、**中英双语**界面

## 使用

**方式一 · 直接运行 exe（推荐）**
从 [Releases](../../releases) 下载 `MousePointer.exe` 双击运行，无需 Python。
首次运行会在 exe 同目录生成 `hotkeys.json`（配置）与 `mouse_highlight.log`（日志）。

**方式二 · 源码运行**
需要 Python 3.10+ 及 `Pillow`、`pywin32`（无其他依赖）：

```bash
pip install pillow pywin32
python mouse_highlight.py
```

## 操作

| 操作 | 说明 |
| --- | --- |
| 按热键（默认 `F10`） | 显示 / 隐藏白手套 |
| 移动鼠标 | 自动隐藏手套 |
| 点击「热键」框后按一键 | 更换热键 |
| 点击「保存」 | 保存并立即生效 |
| 标题栏 `—` | 最小化到托盘 |
| 标题栏 `✕` | 退出 |

## 技术

- **GDI 分层窗口**：`WS_EX_LAYERED` + `UpdateLayeredWindow` 直接合成 PNG 的 per-pixel alpha，规避透明色合成异常
- **指尖检测**：分析 alpha 通道定位食指尖端像素（纯 Pillow 实现，不依赖 numpy）
- **热键与托盘**：热键注册 / 注销在同一线程完成；托盘 WndProc 只写标志位、由主线程轮询执行，规避 Tcl 重入崩溃

### 打包 exe

```bash
pyinstaller --noconsole --onefile ^
  --exclude-module numpy --exclude-module PIL._avif --exclude-module PIL._webp --exclude-module PIL._imagingcms ^
  --name MousePointer --icon tray_icon.ico ^
  --add-data "glove.png;." --add-data "tray_icon.ico;." mouse_highlight.py
```

- 程序只读 PNG，AVIF / WebP / 色彩管理这三个 Pillow 解码器用不上，排除后 exe 从 33.8 MB 降到约 17 MB
- `numpy` 会被 Pillow 隐式引入，即使代码不 import 也需显式排除
- 不要对 onefile 产物再用 UPX：PyInstaller 已压缩内部归档，UPX 几乎压不动（实测仅省 0.7%），且需先关闭 CFG 加固

## 许可

MIT
