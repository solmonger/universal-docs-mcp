"""Deliberately broken baseline for packaging-22-to-24-version."""
import versioned_api

def main():
    getattr(versioned_api, 'LegacyVersion')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:packaging:24.0:LegacyVersion") from exc
