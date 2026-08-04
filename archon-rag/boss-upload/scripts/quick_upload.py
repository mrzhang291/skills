"""Quick upload helper.

Pass files on the command line; this wrapper keeps the old entry name while
using the normal boss-upload step 1 pipeline.
"""

from run_step1 import main


if __name__ == "__main__":
    raise SystemExit(main())
