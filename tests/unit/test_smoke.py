"""Smoke test: the installed packages import cleanly."""


def test_imports() -> None:
    import nooa  # noqa: F401

    import intel  # noqa: F401
