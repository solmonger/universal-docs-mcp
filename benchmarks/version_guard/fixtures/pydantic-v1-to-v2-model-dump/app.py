"""Deliberately broken baseline for pydantic-v1-to-v2-model-dump."""
import versioned_api

def main():
    getattr(versioned_api, 'dict')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:pydantic:2.6.1:dict") from exc
