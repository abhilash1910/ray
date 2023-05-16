import os

DEFAULT_PYTHON_VERSION = tuple(
    int(v) for v in os.environ.get("RELEASE_PY", "3.7").split(".")
)


class Test(dict):
    """A class represents a test to run on buildkite"""

    def get_python_version(self) -> str:
        """
        Returns the python version to use for this test. If not specified, use
        the default python version.
        """
        return self.get("python", DEFAULT_PYTHON_VERSION)

    def get_ray_image(self) -> str:
        """
        Returns the ray docker image to use for this test. If the commit hash is not
        specified, use the nightly ray image.
        """
        ray_version = os.environ.get("BUILDKITE_COMMIT", "")[:6] or "nightly"
        python_version = f"py-{self.get_python_version().replace('.',   '')}"
        return f"rayproject/ray:{ray_version}-{python_version}"

    def get_anyscale_byod_image(self) -> str:
        """
        Returns the anyscale byod image to use for this test.
        """
        return self.get_ray_image().replace("rayproject", "anyscale")
