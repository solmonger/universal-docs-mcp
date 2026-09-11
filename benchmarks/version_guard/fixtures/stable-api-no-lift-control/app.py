"""Deliberately broken baseline for stable-api-no-lift-control."""
import versioned_api

def main():
    getattr(versioned_api, 'LegacyVersion')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:packaging:24.0:LegacyVersion") from exc
