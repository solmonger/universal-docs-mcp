"""Deliberately broken general-coding baseline for the no-lift control."""


def main():
    values = ["stable-api"]
    return values[1]


if __name__ == "__main__":
    try:
        main()
    except IndexError as exc:
        raise RuntimeError("GENERAL_CODING_ERROR:out_of_bounds_index") from exc
