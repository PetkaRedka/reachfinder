from conan import ConanFile


class DemoB(ConanFile):
    name = "demob"
    version = "1.0"
    requires = "democ/1.0"
    exports_sources = "src/*"
