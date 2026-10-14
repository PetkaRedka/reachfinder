from conan import ConanFile


class DemoC(ConanFile):
    name = "democ"
    version = "1.0"
    exports_sources = "src/*"
