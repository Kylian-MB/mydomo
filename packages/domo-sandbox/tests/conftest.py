def pytest_configure(config):
    # Bandit's plugin loader (stevedore) still passes an argument that stevedore deprecated.
    config.addinivalue_line(
        "filterwarnings", "ignore:The verify_requirements argument:DeprecationWarning"
    )
