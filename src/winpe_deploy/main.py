from __future__ import annotations

import sys

from .gui import WinPEImageDeployerApp
from .startup_preflight import run_startup_preflight


def main() -> None:
    if "--startup-preflight" in sys.argv[1:]:
        if not run_startup_preflight():
            app = WinPEImageDeployerApp()
            app.mainloop()
        return
    app = WinPEImageDeployerApp()
    app.mainloop()


if __name__ == "__main__":
    main()