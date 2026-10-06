#!/usr/bin/env python3

'''
Created on Sep 15, 2017

@author: 4x1md
'''

import argparse
from datetime import datetime
import time

from serial import SerialException

if __package__:
    from .de5000 import DE5000
else:
    from de5000 import DE5000

PORT = "/dev/ttyUSB0"
SLEEP_TIME = 1.0


def main():
    parser = argparse.ArgumentParser(description="Monitor a DE-5000 LCR meter.")
    parser.add_argument("port", nargs="?", default=PORT, help="serial port (default: %(default)s)")
    args = parser.parse_args()
    print("Starting DE-5000 monitor...")

    try:
        with DE5000(args.port) as lcr:
            while True:
                print()
                print(datetime.now())
                lcr.pretty_print(disp_norm_val=True)
                # time.sleep(SLEEP_TIME)
    except SerialException:
        print("Serial port error.")
    except KeyboardInterrupt:
        print()
        print("Exiting DE-5000 monitor.")


if __name__ == '__main__':
    main()
