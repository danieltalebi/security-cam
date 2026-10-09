import unittest
from unittest.mock import MagicMock, patch

from router_presence import (
    AsusRouterSource,
    HuaweiRouterSource,
    PresenceObservation,
    RouterClient,
    RouterPresenceMonitor,
    asus_online_macs,
    combined_presence,
    huawei_ethernet_clients,
    huawei_online_macs,
    normalize_mac,
)


class RouterPresenceTests(unittest.TestCase):
    def test_asus_login_sends_required_referer(self):
        source = object.__new__(AsusRouterSource)
        source.host = "http://192.168.50.1"
        source.username = "admin"
        source.password = "secret"
        source._new_session = lambda: None
        requests = []

        def request(path, data=None, headers=None):
            requests.append((path, data, headers))
            return ""

        source._request = request
        source._login()

        self.assertEqual(requests[0][0], "/login.cgi")
        self.assertEqual(
            requests[0][2],
            {"Referer": "http://192.168.50.1/Main_Login.asp"},
        )

    def test_asus_client_query_sends_required_referer(self):
        source = object.__new__(AsusRouterSource)
        source.host = "http://192.168.50.1"
        requests = []
        source._request = lambda path, data=None, headers=None: requests.append(
            (path, data, headers)
        ) or "clientList=[];"

        source._client_page()

        self.assertEqual(
            requests,
            [
                (
                    "/update_clients.asp",
                    None,
                    {"Referer": "http://192.168.50.1/index.asp"},
                )
            ],
        )

    def test_huawei_login_strips_utf8_bom_from_token(self):
        source = object.__new__(HuaweiRouterSource)
        source.username = "root"
        source.password = "secret"
        source._new_session = lambda: None
        requests = []

        def request(path, data=None):
            requests.append((path, data))
            return "\ufeff0123456789abcdef0123456789abcdef\n" if data is None else ""

        source._request = request
        source._login()

        self.assertEqual(
            requests[1][1]["x.X_HW_Token"],
            "0123456789abcdef0123456789abcdef",
        )

    def test_normalize_mac(self):
        self.assertEqual(normalize_mac("FA-07-D7-02-B1-BB"), "fa:07:d7:02:b1:bb")
        with self.assertRaises(ValueError):
            normalize_mac("not-a-mac")

    def test_huawei_extracts_online_wifi_and_downstream_ap_records(self):
        page = r'''
        new USERDeviceV6("x","v6","GUA","iPhone","--",
          "fa\x3a07\x3ad7\x3a02\x3ab1\x3abb","Online","WIFI","SSID1"),
        new USERDeviceV6("y","v6","GUA","iPhone","--",
          "82\x3a4b\x3a40\x3aa9\x3ad3\x3a5d","Offline","WIFI","SSID5"),
        new USERDeviceV6("z","v6","GUA","wired","--",
          "11\x3a22\x3a33\x3a44\x3a55\x3a66","Online","ETH","LAN1")
        '''
        self.assertEqual(
            huawei_online_macs(page),
            {"fa:07:d7:02:b1:bb", "11:22:33:44:55:66"},
        )

    def test_huawei_extracts_active_ipv4_ethernet_clients(self):
        page = r'''
        new USERDev("path.1","192\x2e168\x2e100\x2e134",
          "fc\x3a34\x3a97\x3a60\x3ac7\x3ad0","LAN2","DHCP","",
          "Online","ETH","1:00","router","0","100"),
        new USERDev("path.2","192\x2e168\x2e100\x2e203",
          "aa\x3abb\x3acc\x3add\x3aee\x3aff","LAN2","DHCP","",
          "Offline","ETH","1:00","phone","0","100"),
        new USERDev("path.3","192\x2e168\x2e100\x2e50",
          "11\x3a22\x3a33\x3a44\x3a55\x3a66","SSID1","DHCP","",
          "Online","WIFI","1:00","phone","0","100")
        '''
        self.assertEqual(
            huawei_ethernet_clients(page),
            [RouterClient("192.168.100.134", "fc:34:97:60:c7:d0", "LAN2", "router")],
        )

    def test_unavailable_asus_is_rediscovered_through_huawei(self):
        monitor = object.__new__(RouterPresenceMonitor)
        huawei = object.__new__(HuaweiRouterSource)
        huawei.name = "huawei"
        huawei.ethernet_clients = MagicMock(return_value=[
            RouterClient("192.168.100.134", "fc:34:97:60:c7:d0", "LAN2"),
        ])
        asus = object.__new__(AsusRouterSource)
        asus.name = "asus"
        asus.discover_via = "huawei"
        asus.device_ouis = {"fc:34:97"}
        asus.use_discovered_host = MagicMock()
        asus.poll = MagicMock(return_value=PresenceObservation("asus", True, True, "matched phone"))
        monitor.sources_by_name = {"huawei": huawei, "asus": asus}

        result = monitor._retry_discovered_asus(
            asus, PresenceObservation("asus", False, detail="old address failed"),
        )

        asus.use_discovered_host.assert_called_once_with("192.168.100.134")
        self.assertTrue(result.available)
        self.assertTrue(result.present)
        self.assertIn("discovered via huawei", result.detail)

    def test_asus_rejects_login_page(self):
        with self.assertRaises(PermissionError):
            asus_online_macs("<title>ASUS Login</title>")

    def test_asus_extracts_current_client_macs(self):
        self.assertEqual(
            asus_online_macs(r'''
                originData = {
                  fromNetworkmapd : [{
                    "36:16:73:48:1F:FC": {
                      "mac": "36:16:73:48:1F:FC", "isOnline": "1"
                    },
                    "AA:BB:CC:DD:EE:FF": {
                      "mac": "AA:BB:CC:DD:EE:FF", "isOnline": "0"
                    }
                  }],
                  nmpClient : [{
                    "11:22:33:44:55:66": {
                      "mac": "11:22:33:44:55:66", "name": "historical"
                    }
                  }]
                };
            '''),
            {"36:16:73:48:1f:fc"},
        )

    def test_combined_presence_is_fail_safe(self):
        self.assertEqual(combined_presence([
            PresenceObservation("asus", True, False),
            PresenceObservation("huawei", True, True),
        ]), "home")
        self.assertEqual(combined_presence([
            PresenceObservation("asus", True, False),
            PresenceObservation("huawei", True, False),
        ]), "away")
        self.assertEqual(combined_presence([
            PresenceObservation("asus", True, False),
            PresenceObservation("huawei", False),
        ]), "unavailable")

    def test_logs_only_phone_and_router_state_changes(self):
        monitor = object.__new__(RouterPresenceMonitor)
        monitor.notify = MagicMock()
        monitor.devices = {
            "Daniel iPhone": {"11:22:33:44:55:66"},
            "Second iPhone": {"aa:bb:cc:dd:ee:ff"},
        }
        monitor._last_sources = None
        monitor._source_offline_since = {}
        monitor._last_devices = None
        monitor._last_state = None
        online = [
            PresenceObservation(
                "asus", True, True, matched_macs=("11:22:33:44:55:66",),
            ),
            PresenceObservation("huawei", True, False),
        ]

        with patch("builtins.print") as log:
            monitor._report_transitions("home", online, 1000)
            initial_count = log.call_count
            monitor._report_transitions("home", online, 1030)
            self.assertEqual(log.call_count, initial_count)

            disconnected = [
                PresenceObservation("asus", True, False),
                PresenceObservation("huawei", True, False),
            ]
            monitor._report_transitions("away", disconnected, 1060)

        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertTrue(any("Daniel iPhone disconnected" in line for line in lines))
        self.assertTrue(any("All configured iPhones are offline" in line for line in lines))

    def test_router_outage_and_recovery_send_telegram_transitions(self):
        monitor = object.__new__(RouterPresenceMonitor)
        monitor.notify = MagicMock()
        monitor.devices = {}
        monitor._last_sources = {"asus": True, "huawei": True}
        monitor._source_offline_since = {}
        monitor._last_devices = {}
        monitor._last_state = "home"

        monitor._report_transitions(
            "home",
            [PresenceObservation("asus", False), PresenceObservation("huawei", True, True)],
            1000,
        )
        monitor._report_transitions(
            "home",
            [PresenceObservation("asus", True, True), PresenceObservation("huawei", True, True)],
            1090,
        )

        messages = [call.args[0] for call in monitor.notify.call_args_list]
        self.assertTrue(any("cannot be contacted" in item for item in messages))
        self.assertTrue(any("1m 30s" in item for item in messages))

    def test_phone_is_not_marked_disconnected_while_one_router_is_unavailable(self):
        monitor = object.__new__(RouterPresenceMonitor)
        monitor.notify = MagicMock()
        monitor.devices = {"Daniel iPhone": {"11:22:33:44:55:66"}}
        monitor._last_sources = {"asus": True, "huawei": True}
        monitor._source_offline_since = {}
        monitor._last_devices = {"Daniel iPhone": True}
        monitor._last_state = "home"

        with patch("builtins.print") as log:
            monitor._report_transitions(
                "unavailable",
                [PresenceObservation("asus", False), PresenceObservation("huawei", True, False)],
                1000,
            )

        lines = [str(call.args[0]) for call in log.call_args_list]
        self.assertFalse(any("Daniel iPhone disconnected" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
