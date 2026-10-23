from discovery import settings


def test_placeholder_secret_counts_as_unset(monkeypatch):
    monkeypatch.setenv("GRANOLA_API_KEY", "placeholder")
    monkeypatch.setenv("CIRCLEBACK_API_KEY_ALEX", "placeholder")
    assert settings.env("GRANOLA_API_KEY") is None
    assert settings.granola_client() is None
    assert settings.circleback_client("alex") is None
