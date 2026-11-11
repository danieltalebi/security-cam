import unittest
from pathlib import Path

import yaml


class HomeAssistantPackageTests(unittest.TestCase):
    def test_interior_presence_bridge_handles_sensor_edge_and_arming_while_occupied(self):
        package = yaml.safe_load(
            (Path(__file__).parent / "home-assistant" / "security_cam_package.yaml")
            .read_text(encoding="utf-8")
        )
        automation = next(
            item for item in package["automation"]
            if item["id"] == "security_camera_bridge_interior_presence"
        )

        triggers = {item["id"]: item for item in automation["triggers"]}
        self.assertEqual(
            triggers["sensor_turned_on"],
            {
                "trigger": "state",
                "id": "sensor_turned_on",
                "entity_id": "binary_sensor.sensor_de_presencia_humana_motion",
                "to": "on",
            },
        )
        self.assertEqual(
            triggers["alarm_armed_away"]["entity_id"],
            "sensor.security_camera_alarm_mode",
        )
        self.assertEqual(triggers["alarm_armed_away"]["to"], "armed_away")
        self.assertEqual(
            automation["conditions"][0]["entity_id"],
            "binary_sensor.sensor_de_presencia_humana_motion",
        )
        self.assertEqual(automation["conditions"][0]["state"], "on")


if __name__ == "__main__":
    unittest.main()
