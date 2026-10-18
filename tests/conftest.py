import logging

import pytest

import agents


@pytest.fixture(autouse=True, scope="session")
def _keep_test_errors_out_of_real_log():
    """Tests trigger failures on purpose; don't write them to logs/pipeline.log."""
    for handler in list(agents.logger.handlers):
        if isinstance(handler, logging.FileHandler):
            agents.logger.removeHandler(handler)
            handler.close()
    yield
