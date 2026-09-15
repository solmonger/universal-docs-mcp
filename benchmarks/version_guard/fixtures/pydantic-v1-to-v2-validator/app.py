"""Synthetic fixture: a model integration using the pre-target decorator."""
from pydantic import BaseModel, validator


class User(BaseModel):
    name: str

    @validator("name")
    def normalize_name(cls, value):
        return value.strip()


def main():
    return User(name=" Ada ").name


if __name__ == "__main__":
    main()
