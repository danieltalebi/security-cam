import unittest

from router_presence import (
    AsusRouterSource,
    HuaweiRouterSource,
    PresenceObservation,
    asus_online_macs,
    combined_presence,
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


if __name__ == "__main__":
    unittest.main()
