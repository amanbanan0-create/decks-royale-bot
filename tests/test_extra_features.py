from types import SimpleNamespace

import extra_features


def _message(language_code: str):
    return SimpleNamespace(from_user=SimpleNamespace(language_code=language_code))


def test_shortcut_localization_and_fallback():
    assert "Актуальные" in extra_features._t(_message("ru"), "decks_title")
    assert "Mazos" in extra_features._t(_message("es-ES"), "decks_title")
    assert "Live decks" in extra_features._t(_message("fr"), "decks_title")


def test_app_button_uses_configured_mini_app_url():
    button = extra_features._app_button(_message("en"))
    assert button is not None
    assert button.web_app is not None
    assert button.web_app.url == extra_features.MINI_APP_URL


def test_top_alias_and_top10_are_both_registered():
    # Router observer registration is part of the public aiogram router state.
    callbacks = {handler.callback.__name__ for handler in extra_features.router.message.handlers}
    assert "top10" in callbacks
    assert "top_alias" in callbacks
    assert "decks_shortcut" in callbacks
