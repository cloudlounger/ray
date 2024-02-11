import os
import sys

# coding: utf-8
from typing import Dict, List, Optional, Tuple

import pytest

import ray
from ray.autoscaler.v2.scheduler import (
    NodeTypeConfig,
    ResourceDemandScheduler,
    SchedulingReply,
    SchedulingRequest,
    logger,
)
from ray.autoscaler.v2.schema import AutoscalerInstance, NodeType
from ray.autoscaler.v2.tests.util import make_autoscaler_instance
from ray.autoscaler.v2.utils import ResourceRequestUtil
from ray.core.generated.autoscaler_pb2 import (
    ClusterResourceConstraint,
    GangResourceRequest,
    NodeState,
    NodeStatus,
    ResourceRequest,
)
from ray.core.generated.instance_manager_pb2 import Instance, TerminationRequest

ResourceMap = Dict[str, float]

logger.setLevel("DEBUG")


def sched_request(
    node_type_configs: Dict[NodeType, NodeTypeConfig],
    max_num_nodes: Optional[int] = None,
    resource_requests: Optional[List[ResourceRequest]] = None,
    gang_resource_requests: Optional[List[List[ResourceRequest]]] = None,
    cluster_resource_constraints: Optional[List[ResourceRequest]] = None,
    instances: Optional[List[AutoscalerInstance]] = None,
    idle_timeout_s: Optional[int] = None,
) -> SchedulingRequest:

    if resource_requests is None:
        resource_requests = []
    if gang_resource_requests is None:
        gang_resource_requests = []
    if cluster_resource_constraints is None:
        cluster_resource_constraints = []
    if instances is None:
        instances = []

    return SchedulingRequest(
        resource_requests=ResourceRequestUtil.group_by_count(resource_requests),
        gang_resource_requests=[
            GangResourceRequest(requests=reqs) for reqs in gang_resource_requests
        ],
        cluster_resource_constraints=[
            ClusterResourceConstraint(
                min_bundles=ResourceRequestUtil.group_by_count(
                    cluster_resource_constraints
                )
            )
        ],
        current_instances=instances,
        node_type_configs=node_type_configs,
        max_num_nodes=max_num_nodes,
        idle_timeout_s=idle_timeout_s,
    )


def _launch_and_terminate(
    reply: SchedulingReply,
) -> Tuple[Dict[NodeType, int], List[str]]:
    actual_to_launch = {req.instance_type: req.count for req in reply.to_launch}
    actual_to_terminate = [
        (req.instance_id, req.ray_node_id, req.cause) for req in reply.to_terminate
    ]

    return actual_to_launch, actual_to_terminate


def test_min_worker_nodes():
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=1,
            max_worker_nodes=10,
        ),
        "type_2": NodeTypeConfig(
            name="type_2",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=10,
        ),
        "type_3": NodeTypeConfig(
            name="type_3",
            resources={"CPU": 1},
            min_worker_nodes=2,
            max_worker_nodes=10,
        ),
    }
    # With empty cluster
    request = sched_request(
        node_type_configs=node_type_configs,
    )

    reply = scheduler.schedule(request)

    expected_to_launch = {"type_1": 1, "type_3": 2}
    reply = scheduler.schedule(request)
    actual_to_launch, _ = _launch_and_terminate(reply)
    assert sorted(actual_to_launch) == sorted(expected_to_launch)

    # With existing ray nodes
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=[
            make_autoscaler_instance(ray_node=NodeState(ray_node_type_name="type_1")),
            make_autoscaler_instance(ray_node=NodeState(ray_node_type_name="type_1")),
        ],
    )

    expected_to_launch = {"type_3": 2}
    reply = scheduler.schedule(request)
    actual_to_launch, _ = _launch_and_terminate(reply)
    assert sorted(actual_to_launch) == sorted(expected_to_launch)

    # With existing instances pending.
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=[
            make_autoscaler_instance(
                im_instance=Instance(instance_type="type_1", status=Instance.REQUESTED)
            ),
            make_autoscaler_instance(
                im_instance=Instance(instance_type="type_1", status=Instance.ALLOCATED)
            ),
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_type="type_no_longer_exists",
                    status=Instance.REQUESTED,
                    instance_id="0",
                )
            ),
        ],
    )
    expected_to_launch = {"type_3": 2}
    reply = scheduler.schedule(request)
    actual_to_launch, _ = _launch_and_terminate(reply)
    assert sorted(actual_to_launch) == sorted(expected_to_launch)


def test_max_workers_per_type():
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=2,
            max_worker_nodes=2,
        ),
    }

    request = sched_request(
        node_type_configs=node_type_configs,
    )

    reply = scheduler.schedule(request)

    expected_to_terminate = []
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert sorted(actual_to_terminate) == sorted(expected_to_terminate)

    instances = [
        make_autoscaler_instance(
            im_instance=Instance(
                instance_type="type_1", status=Instance.ALLOCATED, instance_id="0"
            ),
        ),
        make_autoscaler_instance(
            ray_node=NodeState(
                ray_node_type_name="type_1",
                available_resources={"CPU": 1},
                total_resources={"CPU": 1},
                node_id=b"1",
            ),
            im_instance=Instance(
                instance_type="type_1", status=Instance.RAY_RUNNING, instance_id="1"
            ),
        ),
        make_autoscaler_instance(
            ray_node=NodeState(
                ray_node_type_name="type_1",
                available_resources={"CPU": 0.5},
                total_resources={"CPU": 1},
                node_id=b"2",
            ),
            im_instance=Instance(
                instance_type="type_1", status=Instance.RAY_RUNNING, instance_id="2"
            ),
        ),
    ]

    # 3 running instances with max of 2 allowed for type 1.
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
    )

    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert actual_to_terminate == [
        ("0", "", TerminationRequest.Cause.MAX_NUM_NODE_PER_TYPE)
    ]

    # 3 running instances with max of 1 allowed for type 1.
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
    )

    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert sorted(actual_to_terminate) == sorted(
        [
            ("0", "", TerminationRequest.Cause.MAX_NUM_NODE_PER_TYPE),
            # Lower resource util.
            (
                "1",
                "1",
                TerminationRequest.Cause.MAX_NUM_NODE_PER_TYPE,
            ),
        ]
    )


def test_max_num_nodes():
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=2,
        ),
        "type_2": NodeTypeConfig(
            name="type_2",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=2,
        ),
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        max_num_nodes=1,
    )

    reply = scheduler.schedule(request)

    expected_to_terminate = []
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert sorted(actual_to_terminate) == sorted(expected_to_terminate)

    instances = [
        make_autoscaler_instance(
            im_instance=Instance(
                instance_type="type_1", status=Instance.ALLOCATED, instance_id="0"
            ),
        ),
        make_autoscaler_instance(
            ray_node=NodeState(
                ray_node_type_name="type_1",
                available_resources={"CPU": 1},
                total_resources={"CPU": 1},
                node_id=b"1",
                idle_duration_ms=10,
            ),
            im_instance=Instance(
                instance_type="type_1", status=Instance.RAY_RUNNING, instance_id="1"
            ),
        ),
        make_autoscaler_instance(
            ray_node=NodeState(
                ray_node_type_name="type_2",
                available_resources={"CPU": 0.5},
                total_resources={"CPU": 1},
                node_id=b"2",
            ),
            im_instance=Instance(
                instance_type="type_2", status=Instance.RAY_RUNNING, instance_id="2"
            ),
        ),
        make_autoscaler_instance(
            ray_node=NodeState(
                ray_node_type_name="type_2",
                available_resources={"CPU": 0.0},
                total_resources={"CPU": 1},
                node_id=b"3",
            ),
            im_instance=Instance(
                instance_type="type_2", status=Instance.RAY_RUNNING, instance_id="3"
            ),
        ),
    ]

    # 4 running with 4 max => no termination
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
        max_num_nodes=4,
    )

    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert actual_to_terminate == []

    # 4 running with 3 max => terminate 1
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
        max_num_nodes=3,
    )

    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    # Terminate one non-ray running first.
    assert actual_to_terminate == [("0", "", TerminationRequest.Cause.MAX_NUM_NODES)]

    # 4 running with 2 max => terminate 2
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
        max_num_nodes=2,
    )
    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    # Terminate one non-ray running first.
    assert sorted(actual_to_terminate) == sorted(
        [
            ("0", "", TerminationRequest.Cause.MAX_NUM_NODES),  # non-ray running
            ("1", "1", TerminationRequest.Cause.MAX_NUM_NODES),  # idle
        ]
    )

    # 4 running with 1 max => terminate 3
    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
        max_num_nodes=1,
    )
    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert sorted(actual_to_terminate) == sorted(
        [
            ("0", "", TerminationRequest.Cause.MAX_NUM_NODES),  # non-ray running
            ("1", "1", TerminationRequest.Cause.MAX_NUM_NODES),  # idle
            ("2", "2", TerminationRequest.Cause.MAX_NUM_NODES),  # less resource util
        ]
    )

    # Combine max_num_nodes with max_num_nodes_per_type
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=2,
        ),
        "type_2": NodeTypeConfig(
            name="type_2",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=0,
        ),
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        instances=instances,
        max_num_nodes=1,
    )
    reply = scheduler.schedule(request)
    _, actual_to_terminate = _launch_and_terminate(reply)
    assert sorted(actual_to_terminate) == sorted(
        [
            ("0", "", TerminationRequest.Cause.MAX_NUM_NODES),  # non-ray running
            ("2", "2", TerminationRequest.Cause.MAX_NUM_NODE_PER_TYPE),  # type-2
            ("3", "3", TerminationRequest.Cause.MAX_NUM_NODE_PER_TYPE),  # type-2
        ]
    )


def test_single_resources():
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=10,
        ),
    }

    # Request 1 CPU should start a node.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({"type_1": 1})

    # Request multiple CPUs should start multiple nodes
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})] * 3,
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({"type_1": 3})

    # Request resources with already existing nodes should not launch new nodes.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
        instances=[
            make_autoscaler_instance(
                ray_node=NodeState(
                    ray_node_type_name="type_1",
                    available_resources={"CPU": 1},
                    total_resources={"CPU": 1},
                ),
            ),
        ],
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({})

    # Request resources with already existing nodes not sufficient should launch
    # new nodes.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
        instances=[
            make_autoscaler_instance(
                ray_node=NodeState(
                    ray_node_type_name="type_1",
                    available_resources={"CPU": 0.9},
                    total_resources={"CPU": 1},
                ),
            ),
        ],
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({"type_1": 1})

    # Request resources with already pending nodes should NOT launch new nodes
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
        instances=[
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_type="type_1", status=Instance.REQUESTED, instance_id="0"
                ),
            ),
        ],
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({})


def test_max_worker_num_enforce_with_resource_requests():
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=10,
        ),
    }
    max_num_nodes = 2

    # Request 10 CPUs should start at most 2 nodes.
    request = sched_request(
        node_type_configs=node_type_configs,
        max_num_nodes=max_num_nodes,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})] * 3,
        instances=[
            make_autoscaler_instance(
                ray_node=NodeState(
                    ray_node_type_name="type_1",
                    available_resources={"CPU": 1},
                    total_resources={"CPU": 1},
                ),
            ),
        ],
    )
    reply = scheduler.schedule(request)
    to_lauch, _ = _launch_and_terminate(reply)
    assert sorted(to_lauch) == sorted({"type_1": 1})


def test_multi_requests_fittable():
    """
    Test multiple requests can be fit into a single node.
    """
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 1, "GPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
        "type_2": NodeTypeConfig(
            name="type_2",
            resources={"CPU": 3},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1, "GPU": 1}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_1": 1, "type_2": 1})
    assert reply.infeasible_resource_requests == []

    # Change the ordering of requests should not affect the result.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[
            ResourceRequestUtil.make({"CPU": 1, "GPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_1": 1, "type_2": 1})
    assert reply.infeasible_resource_requests == []

    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[
            ResourceRequestUtil.make({"CPU": 2}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 0.5, "GPU": 0.5}),
            ResourceRequestUtil.make({"CPU": 0.5, "GPU": 0.5}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_1": 1, "type_2": 1})
    assert reply.infeasible_resource_requests == []

    # However, if we already have fragmentation. We should not be able
    # to fit more requests.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1, "GPU": 1}),
        ],
        instances=[
            make_autoscaler_instance(
                ray_node=NodeState(
                    ray_node_type_name="type_1",
                    available_resources={"CPU": 0, "GPU": 1},
                    total_resources={"CPU": 1, "GPU": 1},
                ),
            ),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_2": 1})
    assert len(reply.infeasible_resource_requests) == 1


def test_multi_node_types_score():
    """
    Test that when multiple node types are possible, choose the best scoring ones:
    1. The number of resources utilized.
    2. The amount of utilization.
    """
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_large": NodeTypeConfig(
            name="type_large",
            resources={"CPU": 10},  # Large machines
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
        "type_small": NodeTypeConfig(
            name="type_small",
            resources={"CPU": 5},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
        "type_gpu": NodeTypeConfig(
            name="type_gpu",
            resources={"CPU": 2, "GPU": 2},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
    }

    # Request 1 CPU should just start the small machine and not the GPU machine
    # since it has more types of resources.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_small": 1})

    # type_small should be preferred over type_large.
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 2})],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_small": 1})


def test_multi_node_types_score_with_gpu(monkeypatch):
    """
    Test that when multiple node types are possible, choose the best scoring ones:
    - The GPU scoring.
    """
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_gpu": NodeTypeConfig(
            name="type_gpu",
            resources={"CPU": 1, "GPU": 2},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
        "type_multi": NodeTypeConfig(
            name="type_multi",
            resources={"CPU": 2, "XXX": 2},  # Some random resource.
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
    }
    request = sched_request(
        node_type_configs=node_type_configs,
        resource_requests=[ResourceRequestUtil.make({"CPU": 1})],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_multi": 1})

    with monkeypatch.context() as m:
        m.setattr(ray.autoscaler.v2.scheduler, "AUTOSCALER_CONSERVE_GPU_NODES", 0)
        # type_multi should now be preferred over type_gpu.
        reply = scheduler.schedule(request)
        to_launch, _ = _launch_and_terminate(reply)
        assert sorted(to_launch) == sorted({"type_gpu": 1})


def test_resource_constrains():
    scheduler = ResourceDemandScheduler()

    node_type_configs = {
        "type_cpu": NodeTypeConfig(
            name="type_cpu",
            resources={"CPU": 1},
            min_worker_nodes=1,
            max_worker_nodes=5,
        ),
        "type_gpu": NodeTypeConfig(
            name="type_gpu",
            resources={"CPU": 1, "GPU": 2},
            min_worker_nodes=0,
            max_worker_nodes=1,
        ),
    }

    # Resource constraints should not launch extra with min_nodes
    request = sched_request(
        node_type_configs=node_type_configs,
        cluster_resource_constraints=[
            ResourceRequestUtil.make({"CPU": 1}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_cpu": 1})

    # Constraints should launch extra nodes.
    request = sched_request(
        node_type_configs=node_type_configs,
        cluster_resource_constraints=[
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"GPU": 1}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_cpu": 1, "type_gpu": 1})

    # Resource constraints should not launch extra with max_nodes
    # fails to atomically ensure constraints.
    request = sched_request(
        node_type_configs=node_type_configs,
        cluster_resource_constraints=[
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"GPU": 2}),
            ResourceRequestUtil.make({"GPU": 2}),
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted({"type_cpu": 1})
    assert len(reply.infeasible_cluster_resource_constraints) == 1


def test_outdated_nodes():
    """
    Test that nodes with outdated node configs are terminated.
    """
    scheduler = ResourceDemandScheduler()

    node_type_configs = {
        "type_cpu": NodeTypeConfig(
            name="type_cpu",
            resources={"CPU": 1},
            min_worker_nodes=2,
            max_worker_nodes=5,
            launch_config_hash="hash1",
        )
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        instances=[
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_type="type_cpu",
                    status=Instance.RAY_RUNNING,
                    launch_config_hash="hash2",
                    instance_id="i-1",
                ),
                ray_node=NodeState(
                    ray_node_type_name="type_cpu",
                    available_resources={"CPU": 1},
                    total_resources={"CPU": 1},
                    node_id=b"r-1",
                ),
                cloud_instance_id="c-1",
            ),
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_type="type_cpu",
                    status=Instance.RAY_RUNNING,
                    launch_config_hash="hash1",  # matched
                    instance_id="i-2",
                ),
                ray_node=NodeState(
                    ray_node_type_name="type_cpu",
                    available_resources={"CPU": 1},
                    total_resources={"CPU": 1},
                    node_id=b"r-2",
                ),
                cloud_instance_id="c-2",
            ),
        ],
    )

    reply = scheduler.schedule(request)
    to_launch, to_terminate = _launch_and_terminate(reply)
    assert to_terminate == [("i-1", "r-1", TerminationRequest.Cause.OUTDATED)]
    assert to_launch == {"type_cpu": 1}  # Launch 1 to replace the outdated node.


@pytest.mark.parametrize("idle_timeout_s", [1, 2, 10])
@pytest.mark.parametrize("has_resource_constraints", [True, False])
def test_idle_termination(idle_timeout_s, has_resource_constraints):
    """
    Test that idle nodes are terminated.
    """
    scheduler = ResourceDemandScheduler()

    node_type_configs = {
        "type_cpu": NodeTypeConfig(
            name="type_cpu",
            resources={"CPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=5,
            launch_config_hash="hash1",
        )
    }

    idle_time_s = 5
    constraints = (
        []
        if not has_resource_constraints
        else [ResourceRequestUtil.make({"CPU": 1})] * 2
    )

    request = sched_request(
        node_type_configs=node_type_configs,
        instances=[
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_type="type_cpu",
                    status=Instance.RAY_RUNNING,
                    launch_config_hash="hash1",
                    instance_id="i-1",
                ),
                ray_node=NodeState(
                    node_id=b"r-1",
                    ray_node_type_name="type_cpu",
                    available_resources={"CPU": 0},
                    total_resources={"CPU": 1},
                    idle_duration_ms=0,  # Non idle
                    status=NodeStatus.RUNNING,
                ),
                cloud_instance_id="c-1",
            ),
            make_autoscaler_instance(
                im_instance=Instance(
                    instance_id="i-2",
                    instance_type="type_cpu",
                    status=Instance.RAY_RUNNING,
                    launch_config_hash="hash1",
                ),
                ray_node=NodeState(
                    ray_node_type_name="type_cpu",
                    node_id=b"r-2",
                    available_resources={"CPU": 1},
                    total_resources={"CPU": 1},
                    idle_duration_ms=idle_time_s * 1000,
                    status=NodeStatus.IDLE,
                ),
                cloud_instance_id="c-2",
            ),
        ],
        idle_timeout_s=idle_timeout_s,
        cluster_resource_constraints=constraints,
    )

    reply = scheduler.schedule(request)
    _, to_terminate = _launch_and_terminate(reply)
    if idle_timeout_s <= idle_time_s and not has_resource_constraints:
        assert len(to_terminate) == 1
        assert to_terminate == [("i-2", "r-2", TerminationRequest.Cause.IDLE)]
    else:
        assert len(to_terminate) == 0


def test_gang_scheduling():
    """
    Test that gang scheduling works.
    """
    scheduler = ResourceDemandScheduler()
    AFFINITY = ResourceRequestUtil.PlacementConstraintType.AFFINITY
    ANTI_AFFINITY = ResourceRequestUtil.PlacementConstraintType.ANTI_AFFINITY

    node_type_configs = {
        "type_cpu": NodeTypeConfig(
            name="type_cpu",
            resources={"CPU": 2},
            min_worker_nodes=0,
            max_worker_nodes=5,
            launch_config_hash="hash1",
        )
    }

    request = sched_request(
        node_type_configs=node_type_configs,
        gang_resource_requests=[
            [
                ResourceRequestUtil.make({"CPU": 1}, [(AFFINITY, "pg", "")]),
                ResourceRequestUtil.make({"CPU": 1}, [(AFFINITY, "pg", "")]),
            ]
        ],
    )

    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    # Should be grouped on the same node.
    assert sorted(to_launch) == sorted({"type_cpu": 1})

    request = sched_request(
        node_type_configs=node_type_configs,
        gang_resource_requests=[
            [
                ResourceRequestUtil.make({"CPU": 1}, [(ANTI_AFFINITY, "pg", "")]),
                ResourceRequestUtil.make({"CPU": 1}, [(ANTI_AFFINITY, "pg", "")]),
            ]
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    # Should be placed on different nodes.
    assert sorted(to_launch) == sorted({"type_cpu": 2})

    # Atomic gang scheduling
    request = sched_request(
        node_type_configs=node_type_configs,
        gang_resource_requests=[
            [
                # Couldn't fit on a node.
                ResourceRequestUtil.make({"CPU": 3}, [(AFFINITY, "pg", "")]),
                ResourceRequestUtil.make({"CPU": 3}, [(AFFINITY, "pg", "")]),
            ]
        ],
    )
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert to_launch == {}
    assert len(reply.infeasible_gang_resource_requests) == 1


def test_gang_scheduling_with_others():
    """
    Test that a mix of the various demands:
    - resource requests from tasks/actors
    - gang requests from placement groups
    - cluster resource constraints
    - min/max worker counts
    - existing nodes.
    """
    scheduler = ResourceDemandScheduler()
    node_type_configs = {
        "type_1": NodeTypeConfig(
            name="type_1",
            resources={"CPU": 4},
            min_worker_nodes=2,
            max_worker_nodes=4,
            launch_config_hash="hash1",
        ),
        "type_2": NodeTypeConfig(
            name="type_2",
            resources={"CPU": 1, "GPU": 1},
            min_worker_nodes=0,
            max_worker_nodes=10,
            launch_config_hash="hash2",
        ),
    }

    # Placement constraints
    AFFINITY = ResourceRequestUtil.PlacementConstraintType.AFFINITY
    ANTI_AFFINITY = ResourceRequestUtil.PlacementConstraintType.ANTI_AFFINITY
    gang_requests = [
        [
            ResourceRequestUtil.make({"CPU": 2}, [(ANTI_AFFINITY, "ak", "av")]),
            ResourceRequestUtil.make({"CPU": 2}, [(ANTI_AFFINITY, "ak", "av")]),
            ResourceRequestUtil.make({"CPU": 2}, [(ANTI_AFFINITY, "ak", "av")]),
            ResourceRequestUtil.make({"CPU": 2}, [(ANTI_AFFINITY, "ak", "av")]),
        ],
        [
            ResourceRequestUtil.make({"CPU": 3}, [(AFFINITY, "c", "c1")]),
            ResourceRequestUtil.make({"CPU": 3}, [(AFFINITY, "c", "c1")]),
        ],
        [
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
            ResourceRequestUtil.make({"CPU": 1}),
        ],
    ]

    # Resource requests
    resource_requests = [
        ResourceRequestUtil.make({"CPU": 2}),
        ResourceRequestUtil.make({"GPU": 1, "CPU": 1}),
        ResourceRequestUtil.make({"GPU": 1}),
    ]

    # Cluster constraints
    cluster_constraints = [ResourceRequestUtil.make({"CPU": 1})] * 10

    instances = [
        make_autoscaler_instance(
            im_instance=Instance(
                instance_type="type_1",
                status=Instance.RAY_RUNNING,
                launch_config_hash="hash1",
                instance_id="i-1",
            ),
            ray_node=NodeState(
                node_id=b"r-1",
                ray_node_type_name="type_1",
                available_resources={"CPU": 2},
                total_resources={"CPU": 4},
                idle_duration_ms=0,
                status=NodeStatus.RUNNING,
            ),
            cloud_instance_id="c-1",
        ),
        make_autoscaler_instance(
            im_instance=Instance(
                instance_type="type_2",
                status=Instance.RAY_RUNNING,
                launch_config_hash="hash2",
                instance_id="i-2",
            ),
            ray_node=NodeState(
                node_id=b"r-2",
                ray_node_type_name="type_2",
                available_resources={"CPU": 1, "GPU": 1},
                total_resources={"CPU": 1, "GPU": 1},
                idle_duration_ms=0,
                status=NodeStatus.RUNNING,
            ),
            cloud_instance_id="c-2",
        ),
    ]

    request = sched_request(
        node_type_configs=node_type_configs,
        gang_resource_requests=gang_requests,
        resource_requests=resource_requests,
        cluster_resource_constraints=cluster_constraints,
        instances=instances,
        idle_timeout_s=999,
    )
    # Calculate the expected number of nodes to launch:
    # - 1 type_1, 1 type_2 to start with => CPU: 2/5, GPU: 1/1
    # - added 1 type_1 for minimal request -> +1 type_1
    # ==> 2 type_1, 1 type_2 (CPU: 6/9, GPU: 1/1)
    # - enforce cluster constraint (10 CPU) -> +1 type_1, CPU: 10/13, GPU: 1/1
    # ==> 3 type_1, 1 type_2 (CPU: 10/13, GPU: 1/1)
    # - sched gang requests:
    #   - anti affinity (8CPU) => +1 type_1, CPU: 6/17, GPU: 1/1
    #   - no constraint (3CPU) => CPU: 3/17, GPU: 1/1
    #   - affinity (not feasible)
    # ==> 4 type_1, 1 type_2 (CPU: 3/17, GPU: 1/1)
    # - sched resource requests:
    #   - 2CPU => CPU: 1/17, GPU: 1/1
    #   - 1GPU, 1CPU => CPU: 0/17, GPU: 0/1
    #   - 1GPU => adding a new type_2
    # ==> 4 type_1, 2 type_2 (CPU: 0/17, GPU: 0/2)
    # Therefore:
    # - added nodes: 3 type_1, 1 type_2
    # - infeasible: 1 gang request, 1 resource request
    expected_to_launch = {"type_1": 3, "type_2": 1}
    reply = scheduler.schedule(request)
    to_launch, _ = _launch_and_terminate(reply)
    assert sorted(to_launch) == sorted(expected_to_launch)
    assert len(reply.infeasible_gang_resource_requests) == 1
    assert len(reply.infeasible_resource_requests) == 0


if __name__ == "__main__":
    if os.environ.get("PARALLEL_CI"):
        sys.exit(pytest.main(["-n", "auto", "--boxed", "-vs", __file__]))
    else:
        sys.exit(pytest.main(["-sv", __file__]))
