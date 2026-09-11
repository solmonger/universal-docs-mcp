"""Deliberately broken baseline for pydantic-v1-to-v2-validator."""
import versioned_api


def main():
    getattr(versioned_api, 'validator')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:pydantic:2.6.1:validator") from exc
