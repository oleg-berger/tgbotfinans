def pytest_addoption(parser):
    parser.addoption("--cloud-storage", action="store_true", help="Run shared scenarios against the Durable Object SQL adapter")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--cloud-storage"):
        return
    from cloud_storage import LocalDurableDatabase
    for item in items:
        if item.module.__name__ in ("test_application", "test_finance", "test_guide"):
            item.module.Database = LocalDurableDatabase
