"""Windows 图形界面启动入口：双击此文件不会弹出终端窗口。"""

import sys

from ttml_to_lrcn import main


if __name__ == "__main__":
    # Windows passes a dropped file path as argv when it is dragged onto this
    # launcher.  Keep the source file in place and only prefill the GUI input.
    raise SystemExit(main(["--interactive", *sys.argv[1:]]))
