"""Smoke test: the installed packages import cleanly."""


def test_imports() -> None:
    import intel  # noqa: F401
    import nooa  # noqa: F401
