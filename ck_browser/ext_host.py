"""Native-messaging host entry: Chrome runs the wrapper `ext install` wrote, which runs this module
(`python3 -m <package>.ext_host <origin>`)."""

import sys

from .ext import host_main

if __name__ == "__main__":
    sys.exit(host_main(sys.argv[1:]))
