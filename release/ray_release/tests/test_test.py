import sys
import os
import pytest

from ray_release.test import (
    Test,
    DATAPLANE_ECR_REPO,
    RAY_CI_ERC_REPO,
)


def _stub_test(val: dict) -> Test:
    test = Test(
        {
            "name": "test",
            "cluster": {},
        }
    )
    test.update(val)
    return test


def test_get_python_version():
    assert _stub_test({}).get_python_version() == "3.7"
    assert _stub_test({"python": "3.8"}).get_python_version() == "3.8"


def test_get_ray_image():
    os.environ.pop("BUILDKITE_COMMIT", None)
    assert (
        _stub_test({"python": "3.8"}).get_ray_image() == "rayproject/ray:nightly-py38"
    )
    assert (
        _stub_test(
            {
                "python": "3.8",
                "cluster": {
                    "byod": {
                        "type": "gpu",
                    }
                },
            }
        ).get_ray_image()
        == "rayproject/ray-ml:nightly-py38-gpu"
    )
    os.environ["BUILDKITE_COMMIT"] = "1234567890"
    assert _stub_test().get_ray_image() == "rayproject/ray:123456-py37"
    os.environ["BUILDKITE_PULL_REQUEST"] = "1234"
    assert _stub_test().get_ray_image() == f"{RAY_CI_ERC_REPO}:oss-ci-build_1234567890"


def test_get_anyscale_byod_image():
    os.environ.pop("BUILDKITE_PULL_REQUEST", None)
    os.environ.pop("BUILDKITE_COMMIT", None)
    assert (
        _stub_test().get_anyscale_byod_image()
        == f"{DATAPLANE_ECR_REPO}:ray-nightly-py37"
    )
    os.environ["BUILDKITE_COMMIT"] = "1234567890"
    assert (
        _stub_test().get_anyscale_byod_image()
        == f"{DATAPLANE_ECR_REPO}:ray-123456-py37"
    )
    os.environ["BUILDKITE_PULL_REQUEST"] = "1234"
    assert (
        _stub_test().get_anyscale_byod_image()
        == f"{DATAPLANE_ECR_REPO}:oss-ci-build_1234567890"
    )


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
