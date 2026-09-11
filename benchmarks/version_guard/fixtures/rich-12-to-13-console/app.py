"""Deliberately broken baseline for rich-12-to-13-console."""
import versioned_api


def main():
    getattr(versioned_api, 'ConsoleLegacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:rich:13.7.1:ConsoleLegacy") from exc
