"""Deliberately broken baseline for attrs-21-to-23-slots."""
import versioned_api


def main():
    getattr(versioned_api, 'attr_legacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:attrs:23.2.0:attr_legacy") from exc
