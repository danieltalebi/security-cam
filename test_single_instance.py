import uuid
import unittest

from single_instance import SingleInstanceLock


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_lock_is_rejected_until_first_is_released(self):
        name = f"security-cam-test-{uuid.uuid4().hex}"
        first = SingleInstanceLock(name)
        second = SingleInstanceLock(name)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()


if __name__ == "__main__":
    unittest.main()
