import sys

if len(sys.argv) > 1 and sys.argv[1] in ("install", "uninstall", "status"):
    from .macos import main as macos_main

    sys.exit(macos_main(sys.argv[1:]))

from .server import main

main()
