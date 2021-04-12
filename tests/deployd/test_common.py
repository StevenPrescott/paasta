import time
import unittest
from queue import Empty
from unittest import mock

from pytest import fixture
from pytest import raises

from paasta_tools.deployd.common import DelayDeadlineQueue
from paasta_tools.deployd.common import exponential_back_off
from paasta_tools.deployd.common import get_marathon_clients_from_config
from paasta_tools.deployd.common import get_service_instances_needing_update
from paasta_tools.deployd.common import PaastaQueue
from paasta_tools.deployd.common import PaastaThread
from paasta_tools.deployd.common import ServiceInstance
from paasta_tools.marathon_tools import MarathonClients
from paasta_tools.mesos.exceptions import NoSlavesAvailableError
from paasta_tools.utils import DEFAULT_SOA_DIR
from paasta_tools.utils import InvalidJobNameError
from paasta_tools.utils import NoDeploymentsAvailable
from paasta_tools.utils import NoDockerImageError


class TestPaastaThread(unittest.TestCase):
    def setUp(self):
        self.thread = PaastaThread()

    def test_log(self):
        self.thread.log.info("HAAAALP ME")


class TestPaastaQueue(unittest.TestCase):
    def setUp(self):
        self.queue = PaastaQueue("AtThePostOffice")

    def test_log(self):
        self.queue.log.info("HAAAALP ME")

    def test_put(self):
        with mock.patch(
            "paasta_tools.deployd.common.Queue.put", autospec=True
        ) as mock_q_put:
            self.queue.put("human")
            mock_q_put.assert_called_with(self.queue, "human")


def make_si(wait_until, bounce_by):
    """Just using mock.Mock(wait_until=wait_until, bounce_by=bounce_by) mostly works, but our PriorityQueues
    occasionally will compare two ServiceInstances directly, and Mocks aren't comparable unless you define an __eq__."""
    return ServiceInstance(
        service="service",
        instance="instance",
        bounce_by=bounce_by,
        wait_until=wait_until,
        watcher="watcher",
        failures=0,
        processed_count=0,
        enqueue_time=1,
        bounce_start_time=1,
    )


class TestDelayDeadlineQueue:
    @fixture
    def queue(self):
        yield DelayDeadlineQueue()

    def test_log(self, queue):
        queue.log.info("HAAAALP ME")

    def test_put(self, queue):
        with mock.patch.object(
            queue.unavailable_service_instances,
            "put",
            wraps=queue.unavailable_service_instances.put,
        ) as mock_unavailable_service_instances_put:
            si1 = make_si(wait_until=6, bounce_by=4)
            queue.put(si1)
            mock_unavailable_service_instances_put.assert_called_with((6, 4, si1))

            mock_unavailable_service_instances_put.reset_mock()
            si2 = make_si(wait_until=3, bounce_by=4)
            queue.put(si2)
            mock_unavailable_service_instances_put.assert_called_with((3, 4, si2))

    def test_get_empty(self, queue):
        with raises(Empty):
            with queue.get(block=False) as result:
                print(f"Should have raised, got {result}")

        start_time = time.time()
        with raises(Empty):
            with queue.get(timeout=0.01) as result:
                print(f"Should have raised, got {result}")
        assert time.time() > start_time + 0.01

    def test_get(self, queue):
        with mock.patch.object(
            queue.available_service_instances, "get", autospec=True
        ) as mock_available_service_instances_get:
            mock_available_service_instances_get.side_effect = [(2, "human"), Empty]
            with queue.get(block=False) as result:
                assert result == "human"

    def test_dont_block_indefinitely_when_wait_until_is_in_future(self, queue):
        """Regression test for a specific bug in the first implementation of DelayDeadlineQueue"""
        # First, put an item with a distant wait_until
        queue.put(make_si(wait_until=time.time() + 100, bounce_by=time.time() + 100))
        # an immediate get should fail.
        with raises(Empty):
            with queue.get(block=False) as result:
                print(f"Should have raised, got {result}")
        # a get with a short timeout should fail.
        with raises(Empty):
            with queue.get(timeout=0.0001) as result:
                print(f"Should have raised, got {result}")

        wait_until = time.time() + 0.01
        queue.put(make_si(wait_until=wait_until, bounce_by=wait_until))
        # but if we wait a short while it should return.
        with queue.get(
            timeout=1.0
        ):  # This timeout is only there so that if this test fails it doesn't take forever.
            pass
        assert time.time() > wait_until


def test_exponential_back_off():
    assert exponential_back_off(0, 60, 2, 6000) == 60
    assert exponential_back_off(2, 60, 2, 6000) == 240
    assert exponential_back_off(99, 60, 2, 6000) == 6000


def test_get_service_instances_needing_update():
    with mock.patch(
        "paasta_tools.deployd.common.get_all_marathon_apps", autospec=True
    ) as mock_get_marathon_apps, mock.patch(
        "paasta_tools.deployd.common.load_marathon_service_config_no_cache",
        autospec=True,
    ) as mock_load_marathon_service_config:
        mock_marathon_apps = [
            mock.Mock(id="/universe.c137.c1.g1", instances=2),
            mock.Mock(id="/universe.c138.c1.g1", instances=2),
        ]
        mock_get_marathon_apps.return_value = mock_marathon_apps
        mock_service_instances = [("universe", "c137"), ("universe", "c138")]
        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c137.c1.g1", "instances": 2}
                )
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert mock_get_marathon_apps.called
        calls = [
            mock.call(
                service="universe",
                instance="c137",
                cluster="westeros-prod",
                soa_dir=DEFAULT_SOA_DIR,
            ),
            mock.call(
                service="universe",
                instance="c138",
                cluster="westeros-prod",
                soa_dir=DEFAULT_SOA_DIR,
            ),
        ]
        mock_load_marathon_service_config.assert_has_calls(calls)
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]

        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c137.c1.g1", "instances": 3}
                )
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [
            ("universe", "c137", mock.ANY, "/universe.c137.c1.g1"),
            ("universe", "c138", mock.ANY, "/universe.c138.c2.g2"),
        ]

        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(side_effect=NoDockerImageError)
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]

        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(side_effect=NoSlavesAvailableError)
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]

        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(side_effect=InvalidJobNameError)
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]

        mock_configs = [
            mock.Mock(
                format_marathon_app_dict=mock.Mock(side_effect=NoDeploymentsAvailable)
            ),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]
        mock_configs = [
            mock.Mock(format_marathon_app_dict=mock.Mock(side_effect=Exception)),
            mock.Mock(
                format_marathon_app_dict=mock.Mock(
                    return_value={"id": "universe.c138.c2.g2", "instances": 2}
                )
            ),
        ]
        mock_load_marathon_service_config.side_effect = mock_configs
        mock_client = mock.Mock(servers=["foo"])
        fake_clients = MarathonClients(current=[mock_client], previous=[mock_client])
        ret = get_service_instances_needing_update(
            fake_clients, mock_service_instances, "westeros-prod"
        )
        assert ret == [("universe", "c138", mock.ANY, "/universe.c138.c2.g2")]


def test_get_marathon_clients_from_config():
    with mock.patch(
        "paasta_tools.deployd.common.load_system_paasta_config", autospec=True
    ), mock.patch(
        "paasta_tools.deployd.common.get_marathon_servers", autospec=True
    ), mock.patch(
        "paasta_tools.deployd.common.get_marathon_clients", autospec=True
    ) as mock_marathon_clients:
        assert get_marathon_clients_from_config() == mock_marathon_clients.return_value
