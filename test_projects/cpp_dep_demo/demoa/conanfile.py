from conan import ConanFile


class DemoA(ConanFile):
    name = "demoa"
    version = "1.0"
    requires = "demob/1.0"
    exports_sources = "src/*"
