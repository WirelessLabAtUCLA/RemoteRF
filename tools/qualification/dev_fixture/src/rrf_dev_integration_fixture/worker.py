"""Benign owned child for transport/lifecycle verification; never imports UHD."""
import math
import sys
import time


def main():
    delay = float(sys.argv[1])
    if not math.isfinite(delay) or not 0 <= delay <= 10:
        raise ValueError('invalid dev child lifetime')
    print('NON_RADIO_TEST_FIXTURE_READY', flush=True)
    if delay:
        time.sleep(delay)
        return
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
