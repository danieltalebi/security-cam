import unittest
from unittest.mock import patch

from camera_config import rtsp_url


class RtspUrlTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "camera": {
                "name": "garage",
                "credential_prefix": "CAMERA_GARAGE",
                "rtsp_env": "CAMERA_GARAGE_RTSP_URL",
                "host": "garage-camera.example",
            }
        }

    def test_injects_and_encodes_camera_credentials(self):
        environment = {
            "CAMERA_GARAGE_USER": "admin@example",
            "CAMERA_GARAGE_PASSWORD": "p:a/ss",
            "CAMERA_GARAGE_RTSP_URL": "rtsp://garage-camera.example:554/channel=0_stream=0&onvif=0.sdp?real_stream",
        }
        with patch.dict("os.environ", environment, clear=True):
            result = rtsp_url(self.config, "garage")
        self.assertEqual(
            result,
            "rtsp://admin%40example:p%3Aa%2Fss@garage-camera.example:554/"
            "channel=0_stream=0&onvif=0.sdp?real_stream",
        )

    def test_preserves_url_with_complete_authentication(self):
        environment = {
            "CAMERA_GARAGE_RTSP_URL": "rtsp://already:present@camera.example/stream",
        }
        with patch.dict("os.environ", environment, clear=True):
            self.assertEqual(rtsp_url(self.config, "garage"), environment["CAMERA_GARAGE_RTSP_URL"])


if __name__ == "__main__":
    unittest.main()
