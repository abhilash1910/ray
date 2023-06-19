import importlib.util
import logging
import os
import re
import subprocess
import sys
from collections import namedtuple
from typing import Optional

import ray
import ray._private.ray_constants as ray_constants

try:
    import GPUtil
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Prefix for the node id resource that is automatically added to each node.
# For example, a node may have id `node:172.23.42.1`.
NODE_ID_PREFIX = "node:"
# The system resource that head node has.
HEAD_NODE_RESOURCE_NAME = NODE_ID_PREFIX + "__internal_head__"


class ResourceSpec(
    namedtuple(
        "ResourceSpec",
        [
            "num_cpus",
            "num_gpus",
            "memory",
            "object_store_memory",
            "resources",
            "redis_max_memory",
        ],
    )
):
    """Represents the resource configuration passed to a raylet.

    All fields can be None. Before starting services, resolve() should be
    called to return a ResourceSpec with unknown values filled in with
    defaults based on the local machine specifications.

    Attributes:
        num_cpus: The CPUs allocated for this raylet.
        num_gpus: The GPUs allocated for this raylet.
        memory: The memory allocated for this raylet.
        object_store_memory: The object store memory allocated for this raylet.
            Note that when calling to_resource_dict(), this will be scaled down
            by 30% to account for the global plasma LRU reserve.
        resources: The custom resources allocated for this raylet.
        redis_max_memory: The max amount of memory (in bytes) to allow each
            redis shard to use. Once the limit is exceeded, redis will start
            LRU eviction of entries. This only applies to the sharded redis
            tables (task, object, and profile tables). By default, this is
            capped at 10GB but can be set higher.
    """

    def __new__(
        cls,
        num_cpus=None,
        num_gpus=None,
        memory=None,
        object_store_memory=None,
        resources=None,
        redis_max_memory=None,
    ):
        return super(ResourceSpec, cls).__new__(
            cls,
            num_cpus,
            num_gpus,
            memory,
            object_store_memory,
            resources,
            redis_max_memory,
        )

    def resolved(self):
        """Returns if this ResourceSpec has default values filled out."""
        for v in self._asdict().values():
            if v is None:
                return False
        return True

    def to_resource_dict(self):
        """Returns a dict suitable to pass to raylet initialization.

        This renames num_cpus / num_gpus to "CPU" / "GPU", translates memory
        from bytes into 100MB memory units, and checks types.
        """
        assert self.resolved()

        resources = dict(
            self.resources,
            CPU=self.num_cpus,
            GPU=self.num_gpus,
            memory=int(self.memory),
            object_store_memory=int(self.object_store_memory),
        )

        resources = {
            resource_label: resource_quantity
            for resource_label, resource_quantity in resources.items()
            if resource_quantity != 0
        }

        # Check types.
        for resource_label, resource_quantity in resources.items():
            assert isinstance(resource_quantity, int) or isinstance(
                resource_quantity, float
            ), (
                f"{resource_label} ({type(resource_quantity)}): " f"{resource_quantity}"
            )
            if (
                isinstance(resource_quantity, float)
                and not resource_quantity.is_integer()
            ):
                raise ValueError(
                    "Resource quantities must all be whole numbers. "
                    "Violated by resource '{}' in {}.".format(resource_label, resources)
                )
            if resource_quantity < 0:
                raise ValueError(
                    "Resource quantities must be nonnegative. "
                    "Violated by resource '{}' in {}.".format(resource_label, resources)
                )
            if resource_quantity > ray_constants.MAX_RESOURCE_QUANTITY:
                raise ValueError(
                    "Resource quantities must be at most {}. "
                    "Violated by resource '{}' in {}.".format(
                        ray_constants.MAX_RESOURCE_QUANTITY, resource_label, resources
                    )
                )

        return resources

    def resolve(self, is_head: bool, node_ip_address: Optional[str] = None):
        """Returns a copy with values filled out with system defaults.

        Args:
            is_head: Whether this is the head node.
            node_ip_address: The IP address of the node that we are on.
                This is used to automatically create a node id resource.
        """

        resources = (self.resources or {}).copy()
        assert "CPU" not in resources, resources
        assert "GPU" not in resources, resources
        assert "memory" not in resources, resources
        assert "object_store_memory" not in resources, resources

        if node_ip_address is None:
            node_ip_address = ray.util.get_node_ip_address()

        # Automatically create a node id resource on each node. This is
        # queryable with ray._private.state.node_ids() and
        # ray._private.state.current_node_id().
        resources[NODE_ID_PREFIX + node_ip_address] = 1.0

        # Automatically create a head node resource.
        if HEAD_NODE_RESOURCE_NAME in resources:
            raise ValueError(
                f"{HEAD_NODE_RESOURCE_NAME}"
                " is a reserved resource name, use another name instead."
            )
        if is_head:
            resources[HEAD_NODE_RESOURCE_NAME] = 1.0

        # Get cpu num
        num_cpus = self.num_cpus
        if num_cpus is None:
            num_cpus = ray._private.utils.get_num_cpus()

        # Get accelerate device info
        accelerator = ray._private.utils.get_current_accelerator()
        if accelerator == "CUDA": # get cuda device num
            num_gpus, gpu_types = _get_cuda_info(self.num_gpus)
            resources.update(gpu_types)
        elif accelerator == "XPU": # get xpu device num
            # here we take xpu as gpu, so no need to develop core's scheduling policy
            # If we don't want to take xpu as gpu, ray core need to develop new scheduling policy
            num_gpus, gpu_types = _get_xpu_info(self.num_gpus)
            resources.update(gpu_types)

        # Choose a default object store size.
        system_memory = ray._private.utils.get_system_memory()
        avail_memory = ray._private.utils.estimate_available_memory()
        object_store_memory = self.object_store_memory
        if object_store_memory is None:
            object_store_memory = int(
                avail_memory * ray_constants.DEFAULT_OBJECT_STORE_MEMORY_PROPORTION
            )

            # Set the object_store_memory size to 2GB on Mac
            # to avoid degraded performance.
            # (https://github.com/ray-project/ray/issues/20388)
            if sys.platform == "darwin":
                object_store_memory = min(
                    object_store_memory, ray_constants.MAC_DEGRADED_PERF_MMAP_SIZE_LIMIT
                )

            max_cap = ray_constants.DEFAULT_OBJECT_STORE_MAX_MEMORY_BYTES
            # Cap by shm size by default to avoid low performance, but don't
            # go lower than REQUIRE_SHM_SIZE_THRESHOLD.
            if sys.platform == "linux" or sys.platform == "linux2":
                # Multiple by 0.95 to give a bit of wiggle-room.
                # https://github.com/ray-project/ray/pull/23034/files
                shm_avail = ray._private.utils.get_shared_memory_bytes() * 0.95
                max_cap = min(
                    max(ray_constants.REQUIRE_SHM_SIZE_THRESHOLD, shm_avail), max_cap
                )
            # Cap memory to avoid memory waste and perf issues on large nodes
            if object_store_memory > max_cap:
                logger.debug(
                    "Warning: Capping object memory store to {}GB. ".format(
                        max_cap // 1e9
                    )
                    + "To increase this further, specify `object_store_memory` "
                    "when calling ray.init() or ray start."
                )
                object_store_memory = max_cap

        redis_max_memory = self.redis_max_memory
        if redis_max_memory is None:
            redis_max_memory = min(
                ray_constants.DEFAULT_REDIS_MAX_MEMORY_BYTES,
                max(int(avail_memory * 0.1), ray_constants.REDIS_MINIMUM_MEMORY_BYTES),
            )
        if redis_max_memory < ray_constants.REDIS_MINIMUM_MEMORY_BYTES:
            raise ValueError(
                "Attempting to cap Redis memory usage at {} bytes, "
                "but the minimum allowed is {} bytes.".format(
                    redis_max_memory, ray_constants.REDIS_MINIMUM_MEMORY_BYTES
                )
            )

        memory = self.memory
        if memory is None:
            memory = (
                avail_memory
                - object_store_memory
                - (redis_max_memory if is_head else 0)
            )
            if memory < 100e6 and memory < 0.05 * system_memory:
                raise ValueError(
                    "After taking into account object store and redis memory "
                    "usage, the amount of memory on this node available for "
                    "tasks and actors ({} GB) is less than {}% of total. "
                    "You can adjust these settings with "
                    "ray.init(memory=<bytes>, "
                    "object_store_memory=<bytes>).".format(
                        round(memory / 1e9, 2), int(100 * (memory / system_memory))
                    )
                )

        spec = ResourceSpec(
            num_cpus, num_gpus, memory, object_store_memory, resources, redis_max_memory
        )
        assert spec.resolved()
        return spec


def _get_cuda_info(num_gpus):
    """ Attemp to process the number and type of GPUs
        Notice:
            If gpu id not specified in CUDA_VISIBLE_DEVICES,
            and num_gpus is defined in task or actor,
            this function will return the input num_gpus, not 0

    Returns:
        (num_gpus, gpu_types)
    """
    gpu_ids = ray._private.utils.get_cuda_visible_devices()
    # Check that the number of GPUs that the raylet wants doesn't
    # exceed the amount allowed by CUDA_VISIBLE_DEVICES.
    if num_gpus is not None and gpu_ids is not None and num_gpus > len(gpu_ids):
        raise ValueError(
                "Attempting to start raylet with {} GPUs, "
                "but CUDA_VISIBLE_DEVICES contains {}.".format(num_gpus, gpu_ids)
                )
    if num_gpus is None:
        # Try to automatically detect the number of GPUs.
        num_gpus = _autodetect_num_gpus()
        # Don't use more GPUs than allowed by CUDA_VISIBLE_DEVICES.
        if gpu_ids is not None:
            num_gpus = min(num_gpus, len(gpu_ids))

    gpu_types = ""
    try:
        if importlib.util.find_spec("GPUtil") is not None:
            gpu_types = _get_gpu_types_gputil()
        else:
            info_string = _get_gpu_info_string()
            gpu_types = _constraints_from_gpu_info(info_string)
    except Exception:
        logger.exception("Could not parse gpu information.")

    return num_gpus, gpu_types


def _get_xpu_info(num_xpus):
    """Attempt to process the number of XPUs as GPUs

    Here we use `dpctl` to detect XPU device:
      Enumrate all device by API dpctl.get_devices
      Notice that ONEAPI_DEVICE_SELECTOR environment variable should be unset
      Or dpctl.get_devices will only return filtered device set by ONEAPI_DEVICE_SELECTOR
    Another method to enumrate XPU device is to use C++ API, maybe can upgrade later
 
    Returns:
        The number of XPUs that detected by dpctl with specific backend and device type
    """
    xpu_ids = ray._private.utils.get_xpu_visible_devices()
    if num_xpus is not None and xpu_ids is not None and num_xpus > len(xpu_ids):
        raise ValueError(
                "Attempting to start raylet with {} XPUs, "
                "but XPU_VISIBLE_DEVICES contains {}.".format(num_xpus, xpu_ids)
                )
    if num_xpus is None:
        try:
            import dpctl
            num_xpus = len(dpctl.get_devices(backend=ray_constants.RAY_DEVICE_XPU_BACKEND_TYPE,
                                             device_type=ray_constants.RAY_DEVICE_XPU_DEVICE_TYPE))
        except ImportError:
            num_xpus = 0

        if xpu_ids is not None:
            num_xpus = min(num_xpus, len(xpu_ids))
    xpu_types = {f"{ray_constants.RESOURCE_CONSTRAINT_PREFIX}" "xpu": 1}
    return num_xpus, xpu_types


def _autodetect_num_gpus():
    """Attempt to detect the number of GPUs on this machine.

    TODO(rkn): Only detects NVidia GPUs (except when using WMIC on windows)

    Returns:
        The number of GPUs if any were detected, otherwise 0.
    """
    result = 0
    if importlib.util.find_spec("GPUtil"):
        gpu_list = GPUtil.getGPUs()
        result = len(gpu_list)
    elif sys.platform.startswith("linux"):
        proc_gpus_path = "/proc/driver/nvidia/gpus"
        if os.path.isdir(proc_gpus_path):
            result = len(os.listdir(proc_gpus_path))
    elif sys.platform == "win32":
        props = "AdapterCompatibility"
        cmdargs = ["WMIC", "PATH", "Win32_VideoController", "GET", props]
        lines = subprocess.check_output(cmdargs).splitlines()[1:]
        result = len([x.rstrip() for x in lines if x.startswith(b"NVIDIA")])
    return result


def _get_gpu_types_gputil():
    gpu_list = GPUtil.getGPUs()
    if len(gpu_list) > 0:
        gpu_list_names = [gpu.name for gpu in gpu_list]
        info_str = gpu_list_names.pop()
        pretty_name = _pretty_gpu_name(info_str)
        if pretty_name:
            constraint_name = (
                f"{ray_constants.RESOURCE_CONSTRAINT_PREFIX}" f"{pretty_name}"
            )
            return {constraint_name: 1}
    return {}


def _constraints_from_gpu_info(info_str: str):
    """Parse the contents of a /proc/driver/nvidia/gpus/*/information to get the
    gpu model type.

        Args:
            info_str: The contents of the file.

        Returns:
            (str) The full model name.
    """
    if info_str is None:
        return {}
    lines = info_str.split("\n")
    full_model_name = None
    for line in lines:
        split = line.split(":")
        if len(split) != 2:
            continue
        k, v = split
        if k.strip() == "Model":
            full_model_name = v.strip()
            break
    pretty_name = _pretty_gpu_name(full_model_name)
    if pretty_name:
        constraint_name = f"{ray_constants.RESOURCE_CONSTRAINT_PREFIX}" f"{pretty_name}"
        return {constraint_name: 1}
    return {}


def _get_gpu_info_string():
    """Get the gpu type for this machine.

    TODO: Detects maximum one NVidia gpu type on linux

    Returns:
        (str) The gpu's model name.
    """
    if sys.platform.startswith("linux"):
        proc_gpus_path = "/proc/driver/nvidia/gpus"
        if os.path.isdir(proc_gpus_path):
            gpu_dirs = os.listdir(proc_gpus_path)
            if len(gpu_dirs) > 0:
                gpu_info_path = f"{proc_gpus_path}/{gpu_dirs[0]}/information"
                info_str = open(gpu_info_path).read()
                return info_str
    return None


# TODO(Alex): This pattern may not work for non NVIDIA Tesla GPUs (which have
# the form "Tesla V100-SXM2-16GB" or "Tesla K80").
GPU_NAME_PATTERN = re.compile(r"\w+\s+([A-Z0-9]+)")


def _pretty_gpu_name(name):
    if name is None:
        return None
    match = GPU_NAME_PATTERN.match(name)
    return match.group(1) if match else None
