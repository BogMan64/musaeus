"""Placeholder test — verifies MUSAEUS package imports correctly."""

def test_musaeus_imported():
    """Verify MUSAEUS package can be imported."""
    import musaeus
    assert musaeus.__version__ == "0.1.0"
    assert hasattr(musaeus, '__author__')

def test_musaeus_cli_available():
    """Verify CLI module exists."""
    from musaeus import cli
    assert hasattr(cli, 'main')
