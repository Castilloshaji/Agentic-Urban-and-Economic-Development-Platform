"""Build every feature layer, in dependency order.

Run this after ingestion and before the decision engine. The layers are ordered
because later ones read earlier ones: the economic allocator needs road density,
transit stop density and metro distance to build its activity signal, so it runs
last.

Layers that need the 532 MB OSM extract are skipped with a message rather than
failing, because the engine is designed to run without them and report the
parameters they feed as unavailable. Anything else that fails is a real failure
and stops the build.

    python3 -m src.features.build_all            # everything available
    python3 -m src.features.build_all --only econ
"""

from __future__ import annotations

import argparse
import sys

from . import econ, hazard, metro, roads, row, taluks, transit, water

# (name, module, needs_osm)
LAYERS = [
    ("hazard", hazard, False),
    ("transit", transit, False),
    ("taluks", taluks, False),
    ("metro", metro, False),
    ("roads", roads, True),
    ("water", water, True),
    ("row", row, True),
    ("econ", econ, False),      # last: reads roads, transit and metro
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", metavar="LAYER",
                        choices=[name for name, _, _ in LAYERS],
                        help="build just these layers")
    args = parser.parse_args(argv)

    selected = [row_ for row_ in LAYERS if not args.only or row_[0] in args.only]
    skipped, failed = [], []
    for name, module, needs_osm in selected:
        print(f"\n[{name}]")
        try:
            module.build()
        except FileNotFoundError as error:
            if needs_osm:
                # The documented, supported case: no OSM extract on this machine.
                print(f"  skipped — {error}")
                skipped.append(name)
                continue
            print(f"  FAILED — {error}")
            failed.append(name)
        except Exception as error:
            print(f"  FAILED — {type(error).__name__}: {error}")
            failed.append(name)

    print(f"\nbuilt {len(selected) - len(skipped) - len(failed)} layer(s)"
          + (f", skipped {', '.join(skipped)}" if skipped else "")
          + (f", FAILED {', '.join(failed)}" if failed else ""))
    if skipped:
        print("  skipped layers leave their parameters 'unavailable', which is "
              "the intended behaviour — run ingest.py --url gis --include-large "
              "to acquire the OSM extract.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
