"""Synthetic fixture: terminal output uses a pre-target entry point."""
from rich import legacy_output


def main():
    return legacy_output("Hello", "World!")


if __name__ == "__main__":
    main()
