"""Deliberately broken baseline for pytest-6-to-8-raises."""
import versioned_api

def main():
    getattr(versioned_api, 'raises_legacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:pytest:8.2.0:raises_legacy") from exc
