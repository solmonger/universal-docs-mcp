"""Deliberately broken baseline; the replacement API is intentionally hidden."""
import api_surface


def main():
    return api_surface.legacy_call()

if __name__ == "__main__":
    main()
