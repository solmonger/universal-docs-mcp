"""Deliberately broken baseline for urllib3-1-to-2-timeout."""
import versioned_api

def main():
    getattr(versioned_api, 'TimeoutLegacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:urllib3:2.2.1:TimeoutLegacy") from exc
