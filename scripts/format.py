#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Formats all code in this repository with black and isort

You must run this script from the root of the repository:

    python3 scripts/format.py

or

    ./scripts/format.py

or

    python3 -m scripts.format

"""

import subprocess


def main() -> None:
    subprocess.run(("black", "boot.py", "plugin", "tests"))
    subprocess.run(("isort", "boot.py", "plugin", "tests"))


if __name__ == '__main__':
    main()
