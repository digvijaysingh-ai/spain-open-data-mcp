def pytest_addoption(parser):
    parser.addoption("--site", default="build/site", help="directory with index.html and data/")
