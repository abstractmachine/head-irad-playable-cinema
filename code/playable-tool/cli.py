# cli.py — defer to playable_parser to keep one canonical CLI

import sys

def build_parser():
    # Use your existing, simple parser
    import playable_parser as PP
    if hasattr(PP, "build_parser"):
        return PP.build_parser()
    raise RuntimeError("playable_parser.build_parser() not found")

def main(argv=None):
    import playable_parser as PP
    if argv is None:
        argv = sys.argv[1:]

    # Prefer build_parser() if available
    if hasattr(PP, "build_parser"):
        parser = PP.build_parser()
        ns = parser.parse_args(argv)
        func = getattr(ns, "func", None)
        return func(ns) if callable(func) else 0

    # Or fall back to main()
    if hasattr(PP, "main"):
        return PP.main(argv)

    print("cli.py: no CLI entrypoint found. Expected playable_parser.build_parser() or playable_parser.main().")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())