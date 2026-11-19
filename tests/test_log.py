import logging

import pytest

from featherweight.log import setup_logging


@pytest.fixture(autouse=True)
def clean_featherweight_logger():
    yield
    logger = logging.getLogger("featherweight")
    for handler in [h for h in logger.handlers if getattr(h, "_featherweight_handler", False)]:
        logger.removeHandler(handler)


def test_setup_is_idempotent_goes_to_stdout_and_takes_the_level_from_the_env(capsys, monkeypatch):
    logger = setup_logging("INFO")
    setup_logging("INFO")
    assert len([h for h in logger.handlers if getattr(h, "_featherweight_handler", False)]) == 1

    logging.getLogger("featherweight.test").info("hello key=value")
    assert "INFO featherweight.test hello key=value" in capsys.readouterr().out

    monkeypatch.setenv("FTW_LOG_LEVEL", "WARNING")
    assert setup_logging().level == logging.WARNING
    assert setup_logging("DEBUG").level == logging.DEBUG
